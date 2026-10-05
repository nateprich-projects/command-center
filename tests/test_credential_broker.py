"""Contract tests for the two-verb credential broker."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import pathlib
import pwd
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import credential_broker as broker  # noqa: E402


RUN = "27668f694e9d"


def run_git(*args, cwd=None, env=None, input_text=None, check=True):
    return subprocess.run(
        ["/usr/bin/git"] + list(args),
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
    )


def git_clone(tmp_path):
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "ticket-7-20261005T001200465186Z"
    run_git("init", "--bare", "--quiet", str(remote))
    run_git("init", "--quiet", "-b", "main", str(seed))
    run_git("config", "user.name", "Fixture", cwd=seed)
    run_git("config", "user.email", "fixture@example.test", cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    run_git("add", "README.md", cwd=seed)
    run_git("commit", "--quiet", "-m", "seed", cwd=seed)
    run_git("remote", "add", "origin", str(remote), cwd=seed)
    run_git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    run_git(
        "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"
    )
    run_git("clone", "--quiet", str(remote), str(checkout))
    run_git("config", "user.name", "Fixture", cwd=checkout)
    run_git("config", "user.email", "fixture@example.test", cwd=checkout)
    run_git("switch", "--quiet", "-c", "ticket/7", cwd=checkout)
    checkout.chmod(0o700)
    return remote, checkout


def make_marker(path, marker):
    path.write_text(
        "#!/bin/sh\nprintf fired > '{}'\n".format(marker),
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def make_home(path):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".gitconfig").write_text(
        "[user]\n\tname = Fixture\n\temail = fixture@example.test\n",
        encoding="utf-8",
    )
    return path


def make_git_proxy(path):
    path.mkdir()
    module = path / "credential_broker.py"
    module.write_bytes(pathlib.Path(broker.__file__).read_bytes())
    module.chmod(0o700)
    (path / "manifest.json").write_text(json.dumps({
        "version": broker.BROKER_VERSION,
        "sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    (path / "git").symlink_to("credential_broker.py")
    return path


def test_protocol_refuses_every_verb_except_begin_and_finish():
    for verb in ("run", "git", "gh", "heartbeat", "install"):
        with pytest.raises(broker.BrokerError, match="unsupported broker verb"):
            broker.validate_request({"verb": verb})
    with pytest.raises(broker.BrokerError, match="tier option"):
        broker.validate_request({
            "verb": "begin", "tier": "standard", "command": "git push",
        })
    with pytest.raises(broker.BrokerError, match="only run and answer"):
        broker.validate_request({
            "verb": "finish", "run": RUN,
            "answer": {"done": True}, "checkout": "/tmp/other",
        })
    with pytest.raises(broker.BrokerError, match="valid heartbeat"):
        broker.validate_request({
            "verb": "finish", "run": "../bad", "answer": {},
        })


def test_finish_requires_open_run_matching_the_live_claim():
    snapshot = {
        "start": {"run": RUN, "agent": "codex"},
        "binding": {"do": "ticket", "work": "owner/repo#7"},
        "pairing": "open",
        "claim_state": "owned",
    }
    assert broker.validate_finish_snapshot(RUN, snapshot) == {
        "run": RUN,
        "repo": "owner/repo",
        "number": 7,
        "ref": "owner/repo#7",
    }

    mismatch = dict(snapshot)
    mismatch["start"] = {"run": "aaaaaaaaaaaa", "agent": "codex"}
    with pytest.raises(broker.BrokerError, match="open Codex run"):
        broker.validate_finish_snapshot(RUN, mismatch)

    finished = dict(snapshot, pairing="finished")
    with pytest.raises(broker.BrokerError, match="open Codex run"):
        broker.validate_finish_snapshot(RUN, finished)

    for released_state in ("empty", "other", "unknown"):
        released = dict(snapshot, claim_state=released_state)
        with pytest.raises(broker.BrokerError, match="no longer owns"):
            broker.validate_finish_snapshot(RUN, released)


def test_dispatch_does_not_accept_caller_paths_or_write_run_state(tmp_path):
    calls = []

    def fake_live(run, **kwargs):
        calls.append(("live", run))
        return {"run": run, "repo": "owner/repo", "number": 7, "ref": "owner/repo#7"}

    def fake_checkout(repo, number, **kwargs):
        calls.append(("checkout", repo, number))
        return tmp_path / "bound-checkout"

    def fake_finish(context, answer, **kwargs):
        calls.append(("finish", dict(context), dict(answer)))
        return {"ok": True}

    with pytest.raises(broker.BrokerError, match="only run and answer"):
        broker.dispatch_request(
            {"verb": "finish", "run": RUN, "answer": {},
             "checkout": str(tmp_path / "attacker")},
            peer_uid=broker.CODEX_UID,
            live_context_reader=fake_live,
            checkout_finder=fake_checkout,
            finish_runner=fake_finish,
        )

    response = broker.dispatch_request(
        {"verb": "finish", "run": RUN, "answer": {"declined": "prerequisite"}},
        peer_uid=broker.CODEX_UID,
        live_context_reader=fake_live,
        checkout_finder=fake_checkout,
        finish_runner=fake_finish,
        checkout_root=tmp_path,
        hooks_path=tmp_path,
    )
    assert response == {"ok": True}
    assert calls[0] == ("live", RUN)
    assert calls[1] == ("checkout", "owner/repo", 7)
    assert calls[2][0] == "finish"
    assert calls[2][1]["checkout"] == str(tmp_path / "bound-checkout")
    assert calls[2][2] == {"declined": "prerequisite"}
    assert list(tmp_path.iterdir()) == []


def test_begin_uses_only_fixed_codex_runner_arguments_and_no_local_state(tmp_path):
    calls = []

    def fake_process(args, **kwargs):
        calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(
            args, 0, stdout='{"run":"%s","do":"stop"}' % RUN, stderr=""
        )

    response = broker.run_begin(
        "escalated", runner_root=tmp_path, home=tmp_path / "home",
        runner=fake_process,
    )
    assert response == {
        "ok": True, "packet": {"run": RUN, "do": "stop"},
    }
    assert len(calls) == 1
    assert calls[0][0][1:] == [
        str(tmp_path / "funnel.py"), "begin", "--agent", "codex",
        "--tier", "escalated",
    ]
    env = calls[0][1]["env"]
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert list(tmp_path.iterdir()) == []


def test_finish_environment_pins_safe_directory_remote_and_hooks(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    home = make_home(tmp_path / "home")
    remote = (tmp_path / "origin.git").as_uri()
    env = broker.finish_environment(
        checkout, "owner/repo", 7, hooks,
        home=home, runner_root=tmp_path / "runner",
        remote_url=remote, allowed_protocols="https:file",
        git_proxy_dir=make_git_proxy(tmp_path / "broker"),
    )
    count = int(env["GIT_CONFIG_COUNT"])
    pairs = [
        (env["GIT_CONFIG_KEY_{}".format(i)],
         env["GIT_CONFIG_VALUE_{}".format(i)])
        for i in range(count)
    ]
    safe_values = [value for key, value in pairs if key == "safe.directory"]
    assert safe_values == [str(checkout.resolve())]
    assert "*" not in safe_values
    config = dict(pairs)
    assert config["core.hooksPath"] == str(hooks.resolve())
    assert config["core.fsmonitor"] == "false"
    assert config["core.sshCommand"] == "/usr/bin/false"
    assert config["remote.origin.url"] == remote
    assert config["remote.origin.pushurl"] == remote
    assert config["branch.ticket/7.remote"] == "origin"
    assert config["branch.ticket/7.merge"] == "refs/heads/ticket/7"
    assert ("remote.origin.push", "refs/heads/ticket/7:refs/heads/ticket/7") in pairs
    assert not any(
        key.lower().endswith("token") for key in env
    )
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https:file"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["PATH"].split(":")[0] == str(tmp_path / "broker")
    assert ("user.name", "Fixture") in pairs
    assert ("user.email", "fixture@example.test") in pairs


def test_finish_refuses_a_replaced_or_drifted_git_proxy(tmp_path):
    proxy_dir = make_git_proxy(tmp_path / "broker")
    proxy = proxy_dir / "git"
    proxy.unlink()
    proxy.symlink_to("/usr/bin/git")
    with pytest.raises(broker.BrokerError, match="trusted Git proxy"):
        broker.verify_git_proxy(proxy_dir)
    proxy.unlink()
    proxy.symlink_to("credential_broker.py")
    (proxy_dir / "credential_broker.py").write_text("drift\n")
    with pytest.raises(broker.BrokerError, match="drift detected"):
        broker.verify_git_proxy(proxy_dir)


def test_finish_ignores_hostile_repository_configuration(tmp_path):
    remote, checkout = git_clone(tmp_path)
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    hook_dir = tmp_path / "hostile-hooks"
    hook_dir.mkdir()
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    markers = {
        name: marker_dir / name
        for name in ("hook", "fsmonitor", "ssh", "helper", "alias")
    }
    (hook_dir / "pre-commit").write_text(
        "#!/bin/sh\nprintf fired > '{}'\n".format(markers["hook"]),
        encoding="utf-8",
    )
    (hook_dir / "pre-commit").chmod(0o700)
    scripts = {
        name: make_marker(tmp_path / (name + "-command"), marker)
        for name, marker in markers.items()
        if name != "hook"
    }
    run_git("config", "--local", "core.hooksPath", str(hook_dir), cwd=checkout)
    run_git("config", "--local", "core.fsmonitor", str(scripts["fsmonitor"]),
            cwd=checkout)
    run_git("config", "--local", "core.sshCommand", str(scripts["ssh"]),
            cwd=checkout)
    run_git("config", "--local", "--add", "credential.helper",
            "!{}".format(scripts["helper"]), cwd=checkout)
    run_git("config", "--local", "alias.foo",
            "!{}".format(scripts["alias"]), cwd=checkout)
    run_git("remote", "set-url", "origin", "ssh://attacker.invalid/repo.git",
            cwd=checkout)

    runner_root = tmp_path / "trusted-runner"
    runner_root.mkdir()
    safe_helper = make_marker(tmp_path / "safe-helper", tmp_path / "safe-helper-ran")
    finish = runner_root / "finish-ticket"
    finish.write_text(
        "# fake trusted finish entry point\n"
        "import subprocess\n"
        "alias = subprocess.run(['git', 'foo'], capture_output=True, text=True)\n"
        "assert alias.returncode != 0 and 'not a built-in' in alias.stderr\n"
        "subprocess.run(['git', 'status', '--short'], check=True)\n"
        "subprocess.run(['git', '-c', 'user.name=Fixture', "
        "'-c', 'user.email=fixture@example.test', 'commit', '--allow-empty', "
        "'-m', 'broker fixture'], check=True)\n"
        "subprocess.run(['git', 'credential', 'fill'], "
        "input='protocol=https\\nhost=github.com\\n\\n', "
        "text=True, capture_output=True, check=False)\n"
        "subprocess.run(['git', 'push', '--set-upstream', 'origin', "
        "'ticket/7'], check=True)\n"
        "from pathlib import Path\n"
        "nested = Path('broker-created/nested')\n"
        "nested.mkdir(parents=True)\n"
        "(nested / 'created-by-finish').write_text('done')\n",
        encoding="utf-8",
    )

    result = broker.run_finish(
        {
            "run": RUN,
            "repo": "owner/repo",
            "number": 7,
            "checkout": checkout,
            "expected_uid": os.getuid(),
        },
        {"done": True, "summary": "fixture", "departures": []},
        hooks_path=hooks,
        runner_root=runner_root,
        home=make_home(tmp_path / "home"),
        remote_url=remote.as_uri(),
        credential_helper="!{}".format(safe_helper),
        allowed_protocols="https:file",
        git_proxy_dir=make_git_proxy(tmp_path / "broker"),
    )
    assert result["ok"] is True, result
    assert run_git("--git-dir", str(remote), "show-ref",
                   "refs/heads/ticket/7", check=False).returncode == 0
    assert not any(path.exists() for path in markers.values())
    # Same-user simulation of the finish -> checkout removal sequence. The
    # actual Codex/Nate ACL inheritance still requires #2000's Mac proof.
    assert (checkout / "broker-created/nested/created-by-finish").exists()
    shutil.rmtree(checkout)
    assert not checkout.exists()


def test_installed_manifest_detects_drift_and_server_refuses_checkout_copy(tmp_path):
    module = tmp_path / "command-center-broker" / "credential_broker.py"
    module.parent.mkdir()
    source = pathlib.Path(broker.__file__).read_text(encoding="utf-8")
    module.write_text(source, encoding="utf-8")
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    manifest = module.parent / "manifest.json"
    manifest.write_text(
        json.dumps({"version": broker.BROKER_VERSION, "sha256": digest}),
        encoding="utf-8",
    )
    broker.verify_installed_copy(module, manifest)
    module.write_text(source + "# drift\n", encoding="utf-8")
    with pytest.raises(broker.BrokerError, match="drift detected"):
        broker.verify_installed_copy(module, manifest)
    with pytest.raises(broker.BrokerError, match="from a checkout"):
        broker.verify_installed_copy(
            pathlib.Path(broker.__file__), manifest, require_location=True
        )


def test_versioned_installer_repairs_drift_in_a_private_copy(tmp_path):
    home = tmp_path / "home"
    env = dict(os.environ)
    env["HOME"] = str(home)
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install.sh")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    installed = home / ".claude" / "command-center-broker"
    module = installed / "credential_broker.py"
    manifest_path = installed / "manifest.json"
    assert (installed / "git").is_symlink()
    assert (installed / "git").readlink() == pathlib.Path("credential_broker.py")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert installed.stat().st_mode & 0o777 == 0o700
    assert module.stat().st_mode & 0o777 == 0o700
    assert manifest["version"] == broker.BROKER_VERSION
    assert hashlib.sha256(module.read_bytes()).hexdigest() == manifest["sha256"]
    module.write_text("drift\n", encoding="utf-8")

    repaired = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install.sh")],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert repaired.returncode == 0, repaired.stderr
    assert module.read_text(encoding="utf-8") == pathlib.Path(
        broker.__file__
    ).read_text(encoding="utf-8")


def test_broker_access_cutover_has_a_read_only_dry_run():
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install-broker-access.sh"),
         "--dry-run"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "/Users/Shared/command-center-broker" in result.stdout
    assert "/Users/codex/.claude/command-center-heartbeat/codex-runs" in result.stdout
    assert "inherited read, write, and search" in result.stdout
    assert "codex inherited traversal and deletion" in result.stdout
    source = (ROOT / "scripts" / "install-broker-access.sh").read_text()
    assert "codex allow list,search,add_file,add_subdirectory,delete,delete_child" in source
    assert "file_inherit,directory_inherit" in source


def test_broker_restart_only_removes_an_owned_dead_socket(monkeypatch):
    class SocketPath:
        removed = False
        info = SimpleNamespace(
            st_mode=stat.S_IFSOCK | 0o600, st_uid=501, st_dev=1, st_ino=2,
        )

        def lstat(self):
            return self.info

        def unlink(self):
            self.removed = True

        def __str__(self):
            return "/fixture/broker.sock"

    class Probe:
        error = errno.ECONNREFUSED

        def settimeout(self, value):
            assert value == 1

        def connect(self, path):
            assert path == "/fixture/broker.sock"
            if self.error is not None:
                raise OSError(self.error, "probe")

        def close(self):
            pass

    probe = Probe()
    monkeypatch.setattr(broker.socket, "socket", lambda *args: probe)
    path = SocketPath()
    broker._clear_stale_socket(path, 501)
    assert path.removed

    path.removed = False
    probe.error = None
    with pytest.raises(broker.BrokerError, match="already listening"):
        broker._clear_stale_socket(path, 501)
    assert not path.removed

    probe.error = errno.EACCES
    with pytest.raises(broker.BrokerError, match="state is uncertain"):
        broker._clear_stale_socket(path, 501)
    assert not path.removed

    probe.error = errno.ECONNREFUSED
    path.info = SimpleNamespace(
        st_mode=stat.S_IFSOCK | 0o600, st_uid=506, st_dev=1, st_ino=2,
    )
    with pytest.raises(broker.BrokerError, match="not an owned private socket"):
        broker._clear_stale_socket(path, 501)
    assert not path.removed


def test_actual_codex_uid_can_delete_nate_created_finish_descendants(
        monkeypatch, tmp_path):
    """Opt-in Mac cutover proof; CI has no cross-user ACL or sudo authority.

    After #1999 applies the versioned access installer, Nate runs this one
    test with COMMAND_CENTER_TEST_CROSS_USER=1. The checkout is created by
    the actual codex user under the exact configured routine root; run_finish
    writes nested content as Nate, then codex deletes that whole checkout.
    """
    if os.environ.get("COMMAND_CENTER_TEST_CROSS_USER") != "1":
        pytest.skip("real Mac cross-user ACL proof is opt-in after cutover")
    if sys.platform != "darwin":
        pytest.fail("cross-user ACL proof requires the cutover Mac")
    assert os.getuid() == pwd.getpwnam("nateprich").pw_uid
    assert pwd.getpwnam("codex").pw_uid == broker.CODEX_UID
    root = broker.CODEX_CHECKOUT_ROOT
    assert root.is_dir() and not root.is_symlink()
    checkout = root / ("broker-acl-fixture-" + uuid.uuid4().hex)

    def as_codex(code):
        return subprocess.run(
            ["/usr/bin/sudo", "-n", "-u", "codex", "/usr/bin/python3",
             "-c", code, str(checkout)],
            capture_output=True, text=True, timeout=20, check=False,
        )

    created = as_codex(
        "import pathlib,sys; pathlib.Path(sys.argv[1]).mkdir(mode=0o700)")
    assert created.returncode == 0, created.stderr
    try:
        assert checkout.stat().st_uid == broker.CODEX_UID
        monkeypatch.setattr(
            broker, "verify_checkout", lambda *args, **kwargs: {"PATH": "/usr/bin"})

        def finish_as_nate(args, **kwargs):
            nested = checkout / "broker-created" / "nested"
            nested.mkdir(parents=True, mode=0o700)
            (nested / "created-by-finish").write_text("Nate-owned output\n")
            return subprocess.CompletedProcess(args, 0, stdout="done", stderr="")

        result = broker.run_finish(
            {"run": RUN, "repo": "owner/repo", "number": 7,
             "checkout": checkout, "expected_uid": broker.CODEX_UID},
            {"done": True, "summary": "cross-user fixture", "departures": []},
            hooks_path=tmp_path, runner_root=tmp_path,
            runner=finish_as_nate,
        )
        assert result["ok"] is True
        assert (checkout / "broker-created/nested/created-by-finish").stat().st_uid == os.getuid()
        removed = as_codex(
            "import shutil,sys; shutil.rmtree(sys.argv[1])")
        assert removed.returncode == 0, removed.stderr
        assert not checkout.exists()
    finally:
        if checkout.exists():
            try:
                shutil.rmtree(checkout)
            except OSError:
                # The negative result itself may deny Nate traversal; try
                # the fixture owner and leave the failure visible if both do.
                as_codex("import shutil,sys; shutil.rmtree(sys.argv[1])")
