"""Deploy dashboard/ to Cloudflare when main changes under it.

Ticket #653, step 4 of the #643 dashboard plan. A launchd job in the
run-keeper mould ticks every five minutes on the Mac that holds the only
Cloudflare credential:

1. Fetch ``origin main``. The fetch never moves the checkout's own branch:
   the keeper owns the checkout's branch, and this job deploys from a
   detached worktree at the fetched tip instead.
2. Deploy only when dashboard/ changed since the last recorded deploy. A new
   tip with no dashboard/ change advances the recorded SHA without running
   wrangler and without touching Cloudflare.
3. On a deploy, resolve the KV namespace id and patch the worktree's
   ``dashboard/wrangler.toml`` copy with it before running ``npm ci`` and
   ``npx wrangler deploy`` with the scoped token. When the tip still carries
   the local-only placeholder id, the id comes from the state-recorded
   value or a find-or-create title lookup; an already-real id on main
   deploys as-is. The placeholder on main stays untouched: like the other
   launchd jobs, this one never writes to main. The declared
   ``funnel.nateprich.com`` route attaches as part of the deploy, and DNS
   needs no work (its record is already proxied, per #651).

Configuration, in precedence order (flag, environment, default):

- repo: ``--repo``, ``COMMAND_CENTER_DASHBOARD_DEPLOY_REPO``, the run clone.
- remote and branch: ``--remote``/``--branch`` (``origin``/``main``).
- token: ``CLOUDFLARE_API_TOKEN`` in the environment or the gitignored
  ``.env`` (the same variable ``wrangler`` reads, so #652 shares it).
- account: ``CLOUDFLARE_ACCOUNT_ID``, from the same sources as the token.
- env file: ``--env-file``, ``COMMAND_CENTER_DASHBOARD_ENV_FILE`` (shared
  with the publisher), then the #648 path ``~/.claude/command-center/.env``.
- state and lock: ``--state-file``/``--lock-file`` under
  ``~/.claude/command-center-dashboard-deploy/``.
- api base: ``--api-base``, ``COMMAND_CENTER_DASHBOARD_API_BASE`` (shared).

Exit codes: 0 when the tick completed (up-to-date, no-dashboard-change, and
lock-held skips all exit 0); 1 when the deployer itself could not do its
job; 2 on usage error. Everything is logged to stderr, which launchd
captures to the deployer's own log file. The token is never logged:
subprocess output is scrubbed before it reaches the log.

A whole-tick lockfile keeps two ticks from overlapping: ``npm ci`` plus a
deploy may run past the five-minute grid, so without the lock one dashboard
change could deploy twice. A tick that finds the lock held logs one line
and exits 0. Locking is mandatory: without it the deploy-once guarantee
cannot hold, so a tick that cannot lock fails instead of running unlocked.

Runs under the Mac's /usr/bin/python3 (3.9). Keep this module stdlib-only
and 3.9-compatible. It reuses the publisher's dotenv reader, wrangler id
parser, and lockfile; like the publisher it never imports funnel or
heartbeat, so it cannot touch agent runs even by accident.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from publisher import (
    PLACEHOLDER_NAMESPACE_ID,
    PublisherError,
    acquire_lock,
    namespace_id_from_wrangler,
    parse_dotenv,
)

KV_BINDING = "FUNNEL_SNAPSHOT"
KV_NAMESPACE_TITLE = "command-center-funnel-snapshot"
DASHBOARD_SUBDIR = "dashboard"
WRANGLER_TOML_REL = "dashboard/wrangler.toml"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
ENV_FILE_ENV = "COMMAND_CENTER_DASHBOARD_ENV_FILE"
API_BASE_ENV = "COMMAND_CENTER_DASHBOARD_API_BASE"
DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_REPO = "/Users/nateprich/.claude/command-center-run"
DEFAULT_BRANCH = "main"
DEFAULT_REMOTE = "origin"
KV_TIMEOUT_SECONDS = 30.0
GIT_TIMEOUT_SECONDS = 120.0
NPM_TIMEOUT_SECONDS = 600.0
WRANGLER_TIMEOUT_SECONDS = 600.0


class DeployError(Exception):
    """The deployer cannot do its job (config, git, Cloudflare, or deploy failure)."""


class CommandResult(NamedTuple):
    returncode: int
    stdout: bytes
    stderr: bytes


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("{} deploy: {}".format(stamp, message), file=sys.stderr)


def _scrub(text: str, secrets: List[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _tail(data: bytes, secrets: List[str], limit: int = 2000) -> str:
    return _scrub(data[-limit:].decode("utf-8", errors="replace"), secrets)


def _require_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise DeployError(
            "{} not found on PATH; the launchd job needs it to deploy".format(
                name))
    return path


def run_command(argv: List[str], cwd: Optional[Path],
                env: Optional[Dict[str, str]],
                timeout: float, label: str) -> CommandResult:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise DeployError(
            "{} timed out after {}s and was killed".format(label, timeout))
    except OSError as exc:
        raise DeployError("could not run {}: {}".format(" ".join(argv), exc))
    return CommandResult(returncode=completed.returncode,
                         stdout=completed.stdout, stderr=completed.stderr)


def _git(repo: Path, args: List[str],
         timeout: float = GIT_TIMEOUT_SECONDS) -> CommandResult:
    return run_command([_require_executable("git"), "-C", str(repo)] + args,
                       None, None, timeout,
                       "git {}".format(" ".join(args[:2])))


def _git_text(repo: Path, args: List[str]) -> str:
    result = _git(repo, args)
    if result.returncode != 0:
        raise DeployError("git {} failed: {}".format(
            " ".join(args), _tail(result.stderr, [])))
    return result.stdout.decode("utf-8", errors="replace")
def _snip(body: bytes, limit: int = 200) -> str:
    return body[:limit].decode("utf-8", errors="replace")


class NamespaceClient:
    """Find-or-create for the one KV namespace, over stdlib urllib."""

    def __init__(self, api_base: str, account_id: str, token: str,
                 timeout: float = KV_TIMEOUT_SECONDS) -> None:
        self._base = api_base.rstrip("/")
        self._account = account_id
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, query: str = "",
                 data: Optional[bytes] = None) -> Tuple[int, bytes]:
        url = "{}/accounts/{}/storage/kv/namespaces{}".format(
            self._base, urllib.parse.quote(self._account, safe=""), query)
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self._token)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise DeployError(
                "Cloudflare namespaces {} failed: {}".format(method, exc))
        with response:
            return response.status, response.read()

    def _payload(self, method: str, status: int, body: bytes) -> dict:
        if status // 100 != 2:
            raise DeployError(
                "Cloudflare namespaces {} returned {}: {}".format(
                    method, status, _snip(body)))
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            raise DeployError(
                "Cloudflare namespaces {} returned an unexpected "
                "response: {}".format(method, _snip(body)))
        if not isinstance(payload, dict) or not payload.get("success"):
            raise DeployError(
                "Cloudflare namespaces {} reported success=false: {}".format(
                    method, _snip(body)))
        return payload

    def find_by_title(self, title: str) -> List[str]:
        """Return every namespace id carrying this title, across pages."""
        found: List[str] = []
        page = 1
        while True:
            query = "?per_page=100&page={}".format(page)
            status, body = self._request("GET", query)
            payload = self._payload("GET", status, body)
            result = payload.get("result")
            if not isinstance(result, list):
                raise DeployError(
                    "Cloudflare namespaces GET returned no result "
                    "list: {}".format(_snip(body)))
            if not result:
                return found
            for entry in result:
                if (isinstance(entry, dict) and entry.get("title") == title
                        and entry.get("id")):
                    found.append(entry["id"])
            page += 1
            if page > 100:
                raise DeployError(
                    "Cloudflare namespaces list did not end after 100 pages")

    def create(self, title: str) -> str:
        status, body = self._request(
            "POST", "", json.dumps({"title": title}).encode("utf-8"))
        payload = self._payload("POST", status, body)
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("id"):
            raise DeployError(
                "Cloudflare namespaces POST returned no id: {}".format(
                    _snip(body)))
        return result["id"]


def load_credentials(env_file: Path) -> Tuple[str, str]:
    dotenv = parse_dotenv(env_file)
    token = os.environ.get(TOKEN_ENV) or dotenv.get(TOKEN_ENV)
    if not token:
        raise DeployError(
            "{} is not set and {} has no value for it".format(
                TOKEN_ENV, env_file))
    account = os.environ.get(ACCOUNT_ENV) or dotenv.get(ACCOUNT_ENV)
    if not account:
        raise DeployError(
            "{} is not set and {} has no value for it".format(
                ACCOUNT_ENV, env_file))
    return token, account


def load_state(path: Path) -> Dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise DeployError("cannot read state file {}: {}".format(path, exc))
    try:
        payload = json.loads(text)
    except ValueError:
        raise DeployError("state file {} is not parseable JSON".format(path))
    if not isinstance(payload, dict):
        raise DeployError("state file {} does not hold an object".format(path))
    return {key: value for key, value in payload.items()
            if isinstance(value, str)}


def save_state(path: Path, payload: Dict[str, str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise DeployError("cannot write state file {}: {}".format(path, exc))


def patch_wrangler_id(text: str, binding: str, new_id: str) -> str:
    """Return the TOML with this binding's namespace id replaced.

    Exactly one ``id`` line inside the matching ``[[kv_namespaces]]`` block
    must change; anything else means the file is not the shape this ticket
    owns, so fail instead of guessing.
    """
    parts = re.split(r"(^\s*\[\[kv_namespaces\]\]\s*$)",
                     text, flags=re.MULTILINE)
    for index in range(1, len(parts), 2):
        block = parts[index + 1] if index + 1 < len(parts) else ""
        found_binding = re.search(
            r"^\s*binding\s*=\s*[\"']([^\"']+)[\"']",
            block, flags=re.MULTILINE)
        if found_binding is None or found_binding.group(1) != binding:
            continue
        ids = re.findall(r"^\s*id\s*=",
                         block, flags=re.MULTILINE)
        if len(ids) != 1:
            raise DeployError(
                "binding {!r} has {} id lines; refusing to guess".format(
                    binding, len(ids)))
        patched, count = re.subn(
            r"^(\s*id\s*=\s*)[\"'][^\"']+[\"']",
            lambda match: '{}"{}"'.format(match.group(1), new_id),
            block, count=1, flags=re.MULTILINE)
        if count != 1:  # pragma: no cover - the findall above already proved one
            raise DeployError(
                "no id line for binding {!r} to replace".format(binding))
        parts[index + 1] = patched
        return "".join(parts)
    raise DeployError(
        "no [[kv_namespaces]] block for binding {!r}".format(binding))
def _ensure_namespace_id(api_base: str, account: str, token: str,
                        state: Dict[str, str]) -> str:
    """Return the production namespace id, creating it only when absent.

    An id recorded by an earlier tick wins outright, so a tick that created
    the namespace but failed before deploying never creates a second one.
    Otherwise the title lookup decides: reuse the match, create when empty.
    """
    if state.get("namespace_id"):
        log("reusing namespace {} recorded by an earlier tick".format(
            state["namespace_id"]))
        return state["namespace_id"]
    client = NamespaceClient(api_base, account, token)
    found = client.find_by_title(KV_NAMESPACE_TITLE)
    if found:
        if len(found) > 1:
            log("warning: {} namespaces share the title {!r}; using {}".format(
                len(found), KV_NAMESPACE_TITLE, found[0]))
        else:
            log("found namespace {} for title {!r}".format(
                found[0], KV_NAMESPACE_TITLE))
        return found[0]
    created = client.create(KV_NAMESPACE_TITLE)
    log("created namespace {} for title {!r}".format(
        created, KV_NAMESPACE_TITLE))
    return created


def _patch_worktree_namespace_id(worktree: Path, namespace_id: str) -> None:
    """Patch the worktree's wrangler.toml copy with the production id.

    The placeholder on main stays untouched: the job deploys merged
    dashboard/ changes but never commits or pushes itself.
    """
    toml_path = worktree / WRANGLER_TOML_REL
    try:
        text = toml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DeployError("cannot read {}: {}".format(toml_path, exc))
    try:
        toml_path.write_text(
            patch_wrangler_id(text, KV_BINDING, namespace_id),
            encoding="utf-8")
    except OSError as exc:
        raise DeployError("cannot write {}: {}".format(toml_path, exc))


def _deploy(dashboard_dir: Path, npm_bin: str, npx_bin: str,
            token: str, account: str, secrets: List[str]) -> None:
    """Run npm ci plus wrangler deploy with the scoped token in the env."""
    env = dict(os.environ)
    env[TOKEN_ENV] = token
    env[ACCOUNT_ENV] = account
    env["CI"] = "true"
    install = run_command([npm_bin, "ci"], dashboard_dir, env,
                          NPM_TIMEOUT_SECONDS, "npm ci")
    if install.returncode != 0:
        raise DeployError("npm ci failed: {}".format(
            _tail(install.stderr or install.stdout, secrets)))
    log("npm ci finished in the deploy worktree")
    deploy = run_command([npx_bin, "wrangler", "deploy"], dashboard_dir, env,
                         WRANGLER_TIMEOUT_SECONDS, "npx wrangler deploy")
    if deploy.returncode != 0:
        raise DeployError("npx wrangler deploy failed: {}".format(
            _tail(deploy.stderr or deploy.stdout, secrets)))


def tick(repo: Path, remote: str, branch: str, env_file: Path,
         state_file: Path, api_base: str) -> int:
    token, account = load_credentials(env_file)
    secrets = [token]
    fetch = _git(repo, ["fetch", remote, branch])
    if fetch.returncode != 0:
        raise DeployError("git fetch {} {} failed: {}".format(
            remote, branch, _tail(fetch.stderr, [])))
    tip = _git_text(repo, ["rev-parse", remote + "/" + branch]).strip()
    state = load_state(state_file)
    if state.get("deployed_sha") == tip:
        log("main is at {} and already deployed".format(tip[:12]))
        return 0
    npm_bin = _require_executable("npm")
    npx_bin = _require_executable("npx")

    old = state.get("deployed_sha")
    if old:
        exists = _git(repo, ["cat-file", "-e", old])
        if exists.returncode == 0:
            diff = _git(repo, ["diff", "--quiet", old, tip,
                               "--", DASHBOARD_SUBDIR])
            if diff.returncode == 0:
                state["deployed_sha"] = tip
                save_state(state_file, state)
                log("main moved to {} with no {} changes; "
                    "nothing to deploy".format(tip[:12], DASHBOARD_SUBDIR))
                return 0
            if diff.returncode != 1:
                raise DeployError(
                    "git diff {} {} failed: {}".format(
                        old[:12], tip[:12], _tail(diff.stderr, [])))
        else:
            log("warning: last deployed {} is not in this clone; "
                "deploying {}".format(old[:12], tip[:12]))
    parent = Path(tempfile.mkdtemp(prefix="dashboard-deploy-"))
    worktree = parent / "wt"
    try:
        added = _git(repo, ["worktree", "add", "--detach",
                            str(worktree), tip])
        if added.returncode != 0:
            raise DeployError("git worktree add failed: {}".format(
                _tail(added.stderr, [])))
        try:
            toml_id = namespace_id_from_wrangler(
                worktree / WRANGLER_TOML_REL)
        except PublisherError as exc:
            raise DeployError(str(exc))
        if toml_id == PLACEHOLDER_NAMESPACE_ID:
            namespace_id = _ensure_namespace_id(
                api_base, account, token, state)
            state["namespace_id"] = namespace_id
            save_state(state_file, state)
            _patch_worktree_namespace_id(worktree, namespace_id)
            log("patched the deploy worktree copy of {} with namespace "
                "{}; main is untouched".format(
                    WRANGLER_TOML_REL, namespace_id))
        else:
            namespace_id = toml_id
        _deploy(worktree / DASHBOARD_SUBDIR, npm_bin, npx_bin,
                token, account, secrets)
        state["deployed_sha"] = tip
        state["namespace_id"] = namespace_id
        save_state(state_file, state)
        log("deployed {} at {}".format(DASHBOARD_SUBDIR, tip[:12]))
        return 0
    finally:
        _git(repo, ["worktree", "remove", "--force", str(worktree)])
        shutil.rmtree(parent, ignore_errors=True)
def _option(cli_value: Optional[str], env_name: str,
            default: Optional[str] = None) -> Optional[str]:
    if cli_value is not None:
        return cli_value
    value = os.environ.get(env_name)
    if value:
        return value
    return default


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="dashboard_deploy.py",
        description="Deploy dashboard/ to Cloudflare when main changes "
                    "under it.",
    )
    parser.add_argument("--repo", default=None)
    parser.add_argument("--remote", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--lock-file", default=None)
    parser.add_argument("--api-base", default=None)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    repo = Path(_option(
        args.repo, "COMMAND_CENTER_DASHBOARD_DEPLOY_REPO",
        DEFAULT_REPO,
    )).expanduser()
    remote = _option(
        args.remote, "COMMAND_CENTER_DASHBOARD_DEPLOY_REMOTE",
        DEFAULT_REMOTE,
    )
    branch = _option(
        args.branch, "COMMAND_CENTER_DASHBOARD_DEPLOY_BRANCH",
        DEFAULT_BRANCH,
    )
    env_file = Path(_option(
        args.env_file, ENV_FILE_ENV,
        str(Path.home() / ".claude" / "command-center" / ".env"),
    )).expanduser()
    state_dir_default = Path.home() / ".claude" / "command-center-dashboard-deploy"
    state_file = Path(_option(
        args.state_file, "COMMAND_CENTER_DASHBOARD_DEPLOY_STATE_FILE",
        str(state_dir_default / "state.json"),
    )).expanduser()
    lock_file = Path(_option(
        args.lock_file, "COMMAND_CENTER_DASHBOARD_DEPLOY_LOCK_FILE",
        str(state_dir_default / "deploy.lock"),
    )).expanduser()
    api_base = _option(
        args.api_base, API_BASE_ENV,
        DEFAULT_API_BASE,
    )
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log("error: cannot create lock directory {}: {}".format(
            lock_file.parent, exc))
        return 1
    try:
        lock = acquire_lock(lock_file)
    except PublisherError as exc:
        log("error: {}".format(exc))
        return 1
    if lock is None:
        log("another deploy tick is already running; skipping this one")
        return 0
    try:
        return tick(
            repo=repo,
            remote=remote,
            branch=branch,
            env_file=env_file,
            state_file=state_file,
            api_base=api_base,
        )
    except DeployError as exc:
        log("error: {}".format(exc))
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
