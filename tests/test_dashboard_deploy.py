"""The dashboard deployer ships merged dashboard/ changes without a keyboard.

Ticket #653: every five minutes the launchd job fetches origin main and, only
when dashboard/ changed since the last recorded deploy, runs npm ci plus npx
wrangler deploy from a detached worktree with the scoped token. On its first
tick it creates the KV namespace and commits the real id over the local-only
placeholder, so the first deploy follows in the same tick.

Every test runs against a fake namespaces API, fixture git repos on local
paths, and stub npm/npx scripts. No test reads the real .env, touches the
real checkout's branches, or reaches Cloudflare.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

import dashboard_deploy
import publisher
from dashboard_deploy import DeployError

FAKE_TOKEN = "test-token-653-not-a-credential"
FAKE_ACCOUNT = "test-account-653"
REAL_ID = "ns-real-653"
GIT_IDENTITY = [
    "-c", "user.name=Test Deploy",
    "-c", "user.email=test-deploy@example.invalid",
    "-c", "commit.gpgsign=false",
    "-c", "init.defaultBranch=main",
]
WRANGLER_TEMPLATE = """\
name = "command-center-funnel"
main = "worker.js"
compatibility_date = "2026-09-13"
workers_dev = false

[[kv_namespaces]]
binding = "FUNNEL_SNAPSHOT"
id = "{nsid}"
"""


@pytest.fixture(autouse=True)
def home_is_tmp(monkeypatch, tmp_path):
    """A forgotten path override must land in tmp, never in ~/.claude."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return tmp_path


@pytest.fixture(autouse=True)
def cloudflare_env_is_clean(monkeypatch):
    """The Mac's own Cloudflare variables must not leak into a tick."""
    for name in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "COMMAND_CENTER_DASHBOARD_ENV_FILE",
        "COMMAND_CENTER_DASHBOARD_API_BASE",
        "COMMAND_CENTER_DASHBOARD_DEPLOY_REPO",
        "COMMAND_CENTER_DASHBOARD_DEPLOY_REMOTE",
        "COMMAND_CENTER_DASHBOARD_DEPLOY_BRANCH",
        "COMMAND_CENTER_DASHBOARD_DEPLOY_STATE_FILE",
        "COMMAND_CENTER_DASHBOARD_DEPLOY_LOCK_FILE",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CEILING_DIRECTORIES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def namespaces():
    """A fake Cloudflare KV namespaces API speaking list and create."""
    server = FakeNamespacesServer()
    server.start()
    yield server
    server.stop()


class FakeNamespacesServer:
    """Minimal in-memory stand-in for the namespaces REST endpoints."""

    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.calls = []  # ("GET", page) or ("POST", title)
        self.auth_headers = []
        self.fail_next = None  # (status, body) returned once, then cleared
        self._created = 0
        self._server = None
        self._thread = None

    def start(self):
        handler = self._handler()
        self._server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def api_base(self):
        host, port = self._server.server_address
        return "http://{}:{}".format(host, port)

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def get_pages(self):
        return [c[1] for c in self.calls if c[0] == "GET"]

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                payload = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _fail_next(self):
                if outer.fail_next is None:
                    return False
                status, body = outer.fail_next
                outer.fail_next = None
                self._send(status, body)
                return True

            def _route(self):
                parsed = urllib.parse.urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if (len(parts) == 5 and parts[0] == "accounts"
                        and parts[2] == "storage" and parts[3] == "kv"
                        and parts[4] == "namespaces"):
                    return urllib.parse.parse_qs(parsed.query)
                return None

            def do_GET(self):
                outer.auth_headers.append(
                    self.headers.get("Authorization"))
                if self._fail_next():
                    return
                query = self._route()
                if query is None:
                    self._send(404, '{"success":false}')
                    return
                try:
                    per_page = int(query.get("per_page", ["20"])[0])
                    page = int(query.get("page", ["1"])[0])
                except ValueError:
                    self._send(400, '{"success":false}')
                    return
                outer.calls.append(("GET", page))
                start = (page - 1) * per_page
                result = outer.entries[start:start + per_page]
                self._send(200, json.dumps({
                    "success": True,
                    "result": result,
                    "result_info": {
                        "page": page,
                        "per_page": per_page,
                        "count": len(result),
                        "total_count": len(outer.entries),
                    },
                }))

            def do_POST(self):
                outer.auth_headers.append(
                    self.headers.get("Authorization"))
                if self._fail_next():
                    return
                if self._route() is None:
                    self._send(404, '{"success":false}')
                    return
                length = int(self.headers.get("Content-Length", "0"))
                try:
                    payload = json.loads(
                        self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    self._send(400, '{"success":false}')
                    return
                title = payload.get("title", "")
                outer._created += 1
                created = {"id": "ns-created-{}".format(outer._created),
                           "title": title}
                outer.entries.append(created)
                outer.calls.append(("POST", title))
                self._send(200, json.dumps({
                    "success": True, "result": created}))

        return Handler
def _git(args, cwd):
    return subprocess.run(["git"] + GIT_IDENTITY + args, cwd=str(cwd),
                          capture_output=True, text=True, check=True)


def _git_origin(args, origin, cwd):
    return subprocess.run(
        ["git"] + GIT_IDENTITY + ["--git-dir", str(origin)] + args,
        cwd=str(cwd), capture_output=True, text=True, check=True)


def make_origin(tmp_path, nsid="local-funnel-snapshot"):
    """A bare origin plus a clone, with dashboard/ committed on main."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(["init", "--bare", "-q", str(origin)], cwd=tmp_path)
    seed.mkdir()
    _git(["init", "-q"], cwd=seed)
    (seed / "dashboard").mkdir()
    (seed / "dashboard" / "wrangler.toml").write_text(
        WRANGLER_TEMPLATE.format(nsid=nsid))
    (seed / "dashboard" / "worker.js").write_text(
        "export default { fetch: () => new Response(1) };\n")
    (seed / "dashboard" / "package.json").write_text('{"name": "x"}\n')
    (seed / "README.md").write_text("seed\n")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-qm", "seed dashboard"], cwd=seed)
    _git(["remote", "add", "origin", str(origin)], cwd=seed)
    _git(["push", "-q", "origin", "HEAD:main"], cwd=seed)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin)
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(origin), str(clone)], cwd=tmp_path)
    return origin, clone


def commit_on(repo, relpath, content, message):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(["add", "--", relpath], cwd=repo)
    _git(["commit", "-qm", message], cwd=repo)
    _git(["push", "-q", "origin", "HEAD:main"], cwd=repo)


def tip_of(clone):
    _git(["fetch", "-q", "origin", "main"], cwd=clone)
    return _git(["rev-parse", "origin/main"], cwd=clone).stdout.strip()


def write_env(path, token=FAKE_TOKEN, account=FAKE_ACCOUNT):
    lines = ""
    if token is not None:
        lines += "CLOUDFLARE_API_TOKEN={}\n".format(token)
    if account is not None:
        lines += "CLOUDFLARE_ACCOUNT_ID={}\n".format(account)
    path.write_text(lines)
    return path


def make_stub_bin(tmp_path, npm=True, npx=True):
    """Stub npm/npx that record their calls without logging secrets."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    if npm:
        stub = bindir / "npm"
        stub.write_text('#!/bin/sh\necho "npm cwd=$(pwd) args=$*"'
                        ' >> "$STUB_LOG"\nexit ${STUB_NPM_EXIT:-0}\n')
        stub.chmod(0o755)
    if npx:
        stub = bindir / "npx"
        stub.write_text(
            '#!/bin/sh\n'
            '{\n'
            'echo "npx cwd=$(pwd) args=$*";\n'
            'if [ "$CLOUDFLARE_API_TOKEN" = "$EXPECT_TOKEN" ]; then'
            ' echo "npx token-ok"; else echo "npx token-mismatch"; fi;\n'
            'if [ "$CLOUDFLARE_ACCOUNT_ID" = "$EXPECT_ACCOUNT" ]; then'
            ' echo "npx account-ok"; else echo "npx account-mismatch"; fi;\n'
            'if [ "$CI" = "true" ]; then'
            ' echo "npx ci-ok"; else echo "npx ci-missing"; fi;\n'
            '} >> "$STUB_LOG"\n'
            'if [ "${STUB_NPX_EXIT:-0}" != "0" ]; then'
            ' echo "stub wrangler exploded: ${STUB_NPX_TAIL:-boom}" >&2;'
            ' exit "$STUB_NPX_EXIT"; fi\n'
            'exit 0\n')
        stub.chmod(0o755)
    return bindir


def stub_lines(tmp_path):
    log = tmp_path / "stub.log"
    if not log.exists():
        return []
    return log.read_text().splitlines()


def run_tick(clone, env_file, state_file, lock_file, api_base, **kwargs):
    argv = ["--repo", str(clone), "--env-file", str(env_file),
           "--state-file", str(state_file), "--lock-file", str(lock_file),
           "--api-base", api_base]
    for key, value in kwargs.items():
        argv += ["--" + key.replace("_", "-"), value]
    return dashboard_deploy.main(argv)


@pytest.fixture()
def tick_env(tmp_path, monkeypatch, namespaces):
    """Stub toolchain plus env wiring for one tick."""
    bindir = make_stub_bin(tmp_path)
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "stub.log"))
    monkeypatch.setenv("EXPECT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("EXPECT_ACCOUNT", FAKE_ACCOUNT)
    return {
        "env_file": write_env(tmp_path / "test.env"),
        "state_file": tmp_path / "state" / "state.json",
        "lock_file": tmp_path / "state" / "deploy.lock",
    }


def test_first_tick_creates_namespace_commits_id_and_deploys(
        tmp_path, monkeypatch, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path)

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert namespaces.get_pages() == [1]
    assert namespaces.posts() == [
        ("POST", dashboard_deploy.KV_NAMESPACE_TITLE)]
    assert namespaces.auth_headers == [
        "Bearer " + FAKE_TOKEN] * len(namespaces.calls)
    created = namespaces.entries[0]["id"]

    shown = _git_origin(
        ["show", "main:dashboard/wrangler.toml"], origin, tmp_path).stdout
    assert 'id = "{}"'.format(created) in shown
    assert "local-funnel-snapshot" not in shown
    names = _git_origin(
        ["show", "--name-only", "--format=", "main"],
        origin, tmp_path).stdout.split()
    assert names == ["dashboard/wrangler.toml"]
    author = _git_origin(
        ["log", "-1", "--format=%an%x00%ae%x00%s", "main"],
        origin, tmp_path).stdout.rstrip("\n").split("\x00")
    assert author == [dashboard_deploy.COMMIT_USER_NAME,
                      dashboard_deploy.COMMIT_USER_EMAIL,
                      dashboard_deploy.COMMIT_MESSAGE]

    lines = stub_lines(tmp_path)
    assert [line for line in lines if line.startswith("npm ")] != []
    npx_calls = [line for line in lines if line.startswith("npx cwd=")]
    assert len(npx_calls) == 1
    assert "args=wrangler deploy" in npx_calls[0]
    assert "npx token-ok" in lines
    assert "npx account-ok" in lines
    assert "npx ci-ok" in lines
    assert "npx token-mismatch" not in lines
    assert "npx account-mismatch" not in lines
    cwd = npx_calls[0][len("npx cwd="):].split(" args=")[0]
    assert Path(cwd).name == "dashboard"
    assert str(clone) not in cwd

    state = json.loads(tick_env["state_file"].read_text())
    assert state == {"deployed_sha": tip_of(clone), "namespace_id": created}

    worktrees = _git(["worktree", "list"], cwd=clone).stdout.splitlines()
    assert len(worktrees) == 1

    assert FAKE_TOKEN not in capsys.readouterr().err


def test_up_to_date_tick_does_nothing(
        tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"deployed_sha": tip_of(clone)}))

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert stub_lines(tmp_path) == []
    assert namespaces.calls == []
    assert "already deployed" in capsys.readouterr().err


def test_non_dashboard_change_advances_state_without_deploying(
        tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    first = tip_of(clone)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"deployed_sha": first, "namespace_id": REAL_ID}))
    commit_on(clone, "README.md", "changed\n", "docs touch")

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert stub_lines(tmp_path) == []
    assert namespaces.calls == []
    state = json.loads(tick_env["state_file"].read_text())
    assert state["deployed_sha"] == tip_of(clone)
    assert state["deployed_sha"] != first
    assert state["namespace_id"] == REAL_ID
    assert "nothing to deploy" in capsys.readouterr().err


def test_dashboard_change_triggers_deploy(
        tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    first = tip_of(clone)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"deployed_sha": first, "namespace_id": REAL_ID}))
    commit_on(clone, "dashboard/worker.js",
              "export default { fetch: () => new Response(2) };\n",
              "worker touch")

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    lines = stub_lines(tmp_path)
    assert len([l for l in lines if l.startswith("npx cwd=")]) == 1
    assert "npx token-ok" in lines
    assert namespaces.calls == []
    state = json.loads(tick_env["state_file"].read_text())
    assert state == {"deployed_sha": tip_of(clone), "namespace_id": REAL_ID}
    assert FAKE_TOKEN not in capsys.readouterr().err
def test_existing_namespace_title_is_reused(
        tmp_path, namespaces, tick_env):
    namespaces.entries.append(
        {"id": "ns-found-9", "title": dashboard_deploy.KV_NAMESPACE_TITLE})
    origin, clone = make_origin(tmp_path)

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert namespaces.posts() == []
    shown = _git_origin(
        ["show", "main:dashboard/wrangler.toml"], origin, tmp_path).stdout
    assert 'id = "ns-found-9"' in shown
    state = json.loads(tick_env["state_file"].read_text())
    assert state["namespace_id"] == "ns-found-9"
    assert len([l for l in stub_lines(tmp_path)
                if l.startswith("npx cwd=")]) == 1


def test_second_page_match_is_found(tmp_path, namespaces, tick_env):
    for index in range(101):
        namespaces.entries.append(
            {"id": "ns-other-{}".format(index), "title": "other"})
    namespaces.entries.append(
        {"id": "ns-page-two", "title": dashboard_deploy.KV_NAMESPACE_TITLE})
    origin, clone = make_origin(tmp_path)

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert namespaces.posts() == []
    assert namespaces.get_pages() == [1, 2, 3]
    state = json.loads(tick_env["state_file"].read_text())
    assert state["namespace_id"] == "ns-page-two"


def test_recorded_namespace_is_reused_without_api_calls(
        tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"namespace_id": "ns-stuck-7"}))

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert namespaces.calls == []
    shown = _git_origin(
        ["show", "main:dashboard/wrangler.toml"], origin, tmp_path).stdout
    assert 'id = "ns-stuck-7"' in shown
    assert "reusing namespace ns-stuck-7" in capsys.readouterr().err


def test_wrangler_failure_keeps_old_state_and_scrubs_token(
        tmp_path, monkeypatch, namespaces, tick_env, capsys):
    monkeypatch.setenv("STUB_NPX_EXIT", "1")
    monkeypatch.setenv("STUB_NPX_TAIL", "boom " + FAKE_TOKEN)
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    first = tip_of(clone)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"deployed_sha": first, "namespace_id": REAL_ID}))
    commit_on(clone, "dashboard/worker.js",
              "export default { fetch: () => new Response(3) };\n",
              "worker touch")

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 1
    err = capsys.readouterr().err
    assert "npx wrangler deploy failed" in err
    assert "[redacted]" in err
    assert FAKE_TOKEN not in err
    state = json.loads(tick_env["state_file"].read_text())
    assert state == {"deployed_sha": first, "namespace_id": REAL_ID}
    lines = stub_lines(tmp_path)
    assert [l for l in lines if l.startswith("npm ")] != []


def test_missing_token_fails_before_any_work(
        tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path)
    tick_env["env_file"].write_text(
        "CLOUDFLARE_ACCOUNT_ID={}\n".format(FAKE_ACCOUNT))

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 1
    err = capsys.readouterr().err
    assert "CLOUDFLARE_API_TOKEN is not set" in err
    assert str(tick_env["env_file"]) in err
    assert namespaces.calls == []
    assert stub_lines(tmp_path) == []
    assert not tick_env["state_file"].exists()


def test_lock_held_skips_tick(tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path)
    tick_env["lock_file"].parent.mkdir(parents=True, exist_ok=True)
    held = publisher.acquire_lock(tick_env["lock_file"])
    assert held is not None
    try:
        rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)
    finally:
        held.close()

    assert rc == 0
    assert "already running" in capsys.readouterr().err
    assert namespaces.calls == []
    assert stub_lines(tmp_path) == []


def test_missing_npx_fails_clearly(tmp_path, monkeypatch, namespaces,
                                   tick_env, capsys):
    git_bin = shutil.which("git")
    assert git_bin is not None
    npm_only = make_stub_bin(tmp_path / "npm-only", npm=True, npx=False)
    # Isolate git behind a symlink: git's own directory (homebrew's bin)
    # also carries a real npx, which must stay invisible to this tick.
    iso = tmp_path / "iso"
    iso.mkdir()
    os.symlink(git_bin, iso / "git")
    exec_path = subprocess.run(
        [git_bin, "--exec-path"], capture_output=True, text=True,
        check=True).stdout.strip()
    monkeypatch.setenv("GIT_EXEC_PATH", exec_path)
    monkeypatch.setenv("PATH", os.pathsep.join(
        [str(npm_only), str(iso), "/bin", "/usr/bin"]))
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    first = tip_of(clone)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"deployed_sha": first, "namespace_id": REAL_ID}))
    commit_on(clone, "dashboard/worker.js",
              "export default { fetch: () => new Response(4) };\n",
              "worker touch")

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 1
    assert "npx not found on PATH" in capsys.readouterr().err


def test_unknown_old_sha_deploys_with_warning(
        tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text(json.dumps(
        {"deployed_sha": "0" * 40, "namespace_id": REAL_ID}))

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 0
    assert "warning: last deployed" in capsys.readouterr().err
    assert len([l for l in stub_lines(tmp_path)
                if l.startswith("npx cwd=")]) == 1
    state = json.loads(tick_env["state_file"].read_text())
    assert state["deployed_sha"] == tip_of(clone)


def test_patch_wrangler_id_replaces_exactly_the_binding_id():
    text = ('[[kv_namespaces]]\n'
            'binding = "OTHER"\n'
            'id = "keep-me"\n'
            '\n'
            '[[kv_namespaces]]\n'
            'binding = "FUNNEL_SNAPSHOT"\n'
            '# a comment the patch must keep\n'
            'id = "local-funnel-snapshot"\n')
    patched = dashboard_deploy.patch_wrangler_id(
        text, "FUNNEL_SNAPSHOT", "ns-new")
    assert patched == text.replace(
        'id = "local-funnel-snapshot"', 'id = "ns-new"')

    with pytest.raises(DeployError):
        dashboard_deploy.patch_wrangler_id(text, "MISSING", "ns-new")
    with pytest.raises(DeployError):
        dashboard_deploy.patch_wrangler_id(
            '[[kv_namespaces]]\nbinding = "FUNNEL_SNAPSHOT"\n', "FUNNEL_SNAPSHOT",
            "ns-new")
    with pytest.raises(DeployError):
        dashboard_deploy.patch_wrangler_id(
            '[[kv_namespaces]]\nbinding = "FUNNEL_SNAPSHOT"\n'
            'id = "one"\nid = "two"\n', "FUNNEL_SNAPSHOT", "ns-new")


def test_environment_token_beats_dotenv_file(tmp_path, monkeypatch):
    env_file = write_env(tmp_path / "file.env", token="file-token-1",
                         account="file-account-1")

    token, account = dashboard_deploy.load_credentials(env_file)
    assert (token, account) == ("file-token-1", "file-account-1")

    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", FAKE_TOKEN)
    token, account = dashboard_deploy.load_credentials(env_file)
    assert (token, account) == (FAKE_TOKEN, "file-account-1")

    with pytest.raises(DeployError):
        dashboard_deploy.load_credentials(tmp_path / "absent.env")


def test_non_git_directory_fails_clearly(tmp_path, namespaces, tick_env,
                                         capsys):
    repo = tmp_path / "not-a-repo"
    repo.mkdir()

    rc = run_tick(repo, api_base=namespaces.api_base, **tick_env)

    assert rc == 1
    assert "git fetch" in capsys.readouterr().err


def test_corrupt_state_fails_closed(tmp_path, namespaces, tick_env, capsys):
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    tick_env["state_file"].parent.mkdir(parents=True, exist_ok=True)
    tick_env["state_file"].write_text("{nope")

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 1
    assert "not parseable JSON" in capsys.readouterr().err
    assert namespaces.calls == []
    assert stub_lines(tmp_path) == []


def test_namespace_api_failure_fails_before_commit(
        tmp_path, namespaces, tick_env, capsys):
    namespaces.fail_next = (500, '{"success":false,"errors":[{"code":1}]}')
    origin, clone = make_origin(tmp_path)
    before = tip_of(clone)

    rc = run_tick(clone, api_base=namespaces.api_base, **tick_env)

    assert rc == 1
    assert "namespaces GET returned 500" in capsys.readouterr().err
    assert tip_of(clone) == before
    assert stub_lines(tmp_path) == []
    assert not tick_env["state_file"].exists()


def test_default_env_file_is_the_648_path(tmp_path, monkeypatch, namespaces,
                                          capsys):
    home = Path(os.environ["HOME"])
    default_env = home / ".claude" / "command-center" / ".env"
    default_env.parent.mkdir(parents=True, exist_ok=True)
    write_env(default_env)
    origin, clone = make_origin(tmp_path, nsid=REAL_ID)
    bindir = make_stub_bin(tmp_path / "bin2")
    monkeypatch.setenv(
        "PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "stub2.log"))
    monkeypatch.setenv("EXPECT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("EXPECT_ACCOUNT", FAKE_ACCOUNT)

    rc = dashboard_deploy.main([
        "--repo", str(clone),
        "--state-file", str(tmp_path / "s2" / "state.json"),
        "--lock-file", str(tmp_path / "s2" / "deploy.lock"),
        "--api-base", namespaces.api_base,
    ])

    assert rc == 0
    assert "npx token-ok" in (tmp_path / "stub2.log").read_text().splitlines()
    assert FAKE_TOKEN not in capsys.readouterr().err
