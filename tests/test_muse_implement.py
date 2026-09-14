"""The Muse implementer gets one disposable clone and a bounded writer run."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "muse-implement"


def _executable(path: pathlib.Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _stubbed_runner(
    tmp_path: pathlib.Path,
    begin: dict,
    *,
    bound_seconds: int = 20,
    gh_status: int = 0,
    muse_body: str | None = None,
    gh_body: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    repo = tmp_path / "runner-repo"
    (repo / "routines").mkdir(parents=True)
    (repo / "routines" / "muse-implement.md").write_text(
        "# Muse implementer\n\n---\n\nOpening result:\n\n```json\nBEGIN_JSON\n```\n"
    )
    (repo / "begin.json").write_text(json.dumps(begin))
    (repo / "funnel.py").write_text(
        "import pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "command = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "with (root / 'funnel.calls').open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if command == 'session-server':\n"
        "    print('127.0.0.1:1:stub', flush=True)\n"
        "elif command == 'begin':\n"
        "    print((root / 'begin.json').read_text(), end='')\n"
        "elif command == 'session-stop':\n"
        "    pass\n"
        "elif command == 'release':\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('unexpected funnel command: ' + command)\n"
    )
    (repo / "heartbeat.py").write_text(
        "import pathlib, sys\n"
        "with (pathlib.Path(__file__).parent / 'heartbeat.log').open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    )

    gh = tmp_path / "gh"
    _executable(
        gh,
        gh_body
        or (
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
            f"if [[ {gh_status} -ne 0 ]]; then exit {gh_status}; fi\n"
            "mkdir -p \"$4/.git\"\n"
            "printf '%s' cloned > \"$4/clone-marker\"\n"
        ),
    )

    muse = tmp_path / "muse"
    _executable(
        muse,
        muse_body
        or (
            "#!/bin/bash\n"
            "printf '%s\\n' \"$@\" > \"$MUSE_ARGS\"\n"
            "printf '%s' \"${!#}\" > \"$MUSE_PROMPT\"\n"
            "previous=''\n"
            "for argument in \"$@\"; do\n"
            "  if [[ \"$previous\" == '--workspace' ]]; then\n"
            "    test -d \"$argument/.git\"\n"
            "    test -f \"$argument/clone-marker\"\n"
            "    printf '%s' \"$argument\" > \"$MUSE_WORKSPACE\"\n"
            "  fi\n"
            "  previous=\"$argument\"\n"
            "done\n"
        ),
    )

    workspace_root = tmp_path / "workspaces"
    env = dict(
        os.environ,
        MUSE_IMPLEMENT_REPO=str(repo),
        MUSE_BIN=str(muse),
        GH_BIN=str(gh),
        MUSE_IMPLEMENT_BOUND_SECONDS=str(bound_seconds),
        MUSE_IMPLEMENT_WORKSPACE_ROOT=str(workspace_root),
        GH_LOG=str(repo / "gh.log"),
        MUSE_ARGS=str(repo / "muse.args"),
        MUSE_PROMPT=str(repo / "muse.prompt"),
        MUSE_WORKSPACE=str(repo / "muse.workspace"),
        TMPDIR=str(tmp_path),
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), "escalated", "max"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=bound_seconds + 15,
    )
    return proc, repo


def test_the_runner_clones_the_ticket_repo_and_launches_the_exact_writer_shape(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path,
        {
            "agent": "muse",
            "run": "writer-run",
            "gate": "ok",
            "do": "ticket",
            "work": {"ref": "example/widgets#42", "repo": "example/widgets"},
        },
    )

    assert proc.returncode == 0, proc.stderr
    args = (repo / "muse.args").read_text().splitlines()
    assert args[0] == "exec"
    assert args[args.index("--model") + 1] == "muse-spark-1.3"
    assert args[args.index("--reasoning-effort") + 1] == "max"
    assert args[args.index("--sandbox-network") + 1] == "enabled"
    assert "--disable-sandbox" in args
    assert args[args.index("--approval-mode") + 1] == "never"
    assert "--disable-write" not in args
    assert "--yolo" not in args

    workspace = pathlib.Path((repo / "muse.workspace").read_text())
    assert (repo / "gh.log").read_text() == "repo clone example/widgets {}\n".format(
        workspace
    )
    assert not workspace.exists(), "the disposable clone must be removed after the run"
    assert not list((tmp_path / "workspaces").iterdir())

    prompt = (repo / "muse.prompt").read_text()
    assert '"run": "writer-run"' in prompt
    assert '"repo": "example/widgets"' in prompt
    assert "BEGIN_JSON" not in prompt
    assert "begin --agent muse --tier escalated --role implement" in (
        repo / "funnel.calls"
    ).read_text()


def test_a_fresh_workspace_resolves_uid_and_pushes_over_ssh(tmp_path):
    """The workspace boundary must preserve the uid and the provided SSH path.

    The fake Muse executable models the managed shell boundary: it refuses to
    enter the workspace probe unless the runner disables the nested sandbox.
    The probe then exercises the real uid lookup and Git SSH transport without
    requiring a network credential or a checkout-local URL rewrite.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(remote)],
        check=True,
    )

    probe = tmp_path / "workspace-probe.py"
    _executable(
        probe,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import pwd\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        "workspace = pathlib.Path(sys.argv[1])\n"
        "result = pathlib.Path(os.environ['MUSE_PROBE_RESULT'])\n"
        "ssh = os.environ['TEST_SSH']\n"
        "remote = pathlib.Path(os.environ['REMOTE_REPO'])\n"
        "uid = os.getuid()\n"
        "user = pwd.getpwuid(uid)\n"
        "assert user.pw_uid == uid\n"
        "origin = subprocess.check_output(\n"
        "    ['git', '-C', str(workspace), 'remote', 'get-url', 'origin'],\n"
        "    text=True,\n"
        ").strip()\n"
        "assert origin.startswith('ssh://'), origin\n"
        "instead_of = subprocess.run(\n"
        "    ['git', '-C', str(workspace), 'config', '--local',\n"
        "     '--get-regexp', r'^url\\..*\\.insteadOf$'],\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "assert instead_of.returncode == 1, instead_of.stdout\n"
        "handshake = subprocess.run(\n"
        "    [ssh, '-T', 'git@forge'], capture_output=True, text=True\n"
        ")\n"
        "assert handshake.returncode == 1, handshake\n"
        "assert 'authenticated' in handshake.stdout\n"
        "push_env = dict(os.environ, GIT_SSH_COMMAND=ssh)\n"
        "subprocess.run(\n"
        "    ['git', 'push', 'origin', 'HEAD:refs/heads/muse-regression'],\n"
        "    cwd=workspace, env=push_env, check=True,\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "present = subprocess.run(\n"
        "    ['git', '--git-dir', str(remote), 'show-ref', '--verify',\n"
        "     '--quiet', 'refs/heads/muse-regression']\n"
        ")\n"
        "assert present.returncode == 0\n"
        "subprocess.run(\n"
        "    ['git', 'push', 'origin', ':refs/heads/muse-regression'],\n"
        "    cwd=workspace, env=push_env, check=True,\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "removed = subprocess.run(\n"
        "    ['git', '--git-dir', str(remote), 'show-ref', '--verify',\n"
        "     '--quiet', 'refs/heads/muse-regression']\n"
        ")\n"
        "assert removed.returncode == 1\n"
        "result.write_text(json.dumps({\n"
        "    'uid': uid, 'user': user.pw_name, 'origin': origin,\n"
        "    'remote_ref_removed': True,\n"
        "}))\n",
    )

    ssh = tmp_path / "ssh"
    _executable(
        ssh,
        "#!/bin/bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$argument\" == '-T' ]]; then\n"
        "    printf '%s\\n' \"Hi regression! You have successfully authenticated.\"\n"
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "request=\"${!#}\"\n"
        "case \"$request\" in\n"
        "  *git-receive-pack*) exec git-receive-pack \"$REMOTE_REPO\" ;;\n"
        "  *git-upload-pack*) exec git-upload-pack \"$REMOTE_REPO\" ;;\n"
        "  *) echo \"unexpected SSH request: $request\" >&2; exit 2 ;;\n"
        "esac\n",
    )

    gh_body = (
        "#!/bin/bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
        "workspace=\"${4:?workspace argument missing}\"\n"
        "git init --quiet \"$workspace\"\n"
        "printf '%s' seed > \"$workspace/README.md\"\n"
        "git -C \"$workspace\" add README.md\n"
        "git -C \"$workspace\" -c user.name=runner -c user.email=runner@example.test commit --quiet -m seed\n"
        "git -C \"$workspace\" remote add origin ssh://git@forge/muse-regression.git\n"
    )
    muse_body = (
        "#!/bin/bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$@\" > \"$MUSE_ARGS\"\n"
        "workspace=''\n"
        "has_disable_sandbox=0\n"
        "previous=''\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$previous\" == '--workspace' ]]; then workspace=\"$argument\"; fi\n"
        "  if [[ \"$argument\" == '--disable-sandbox' ]]; then has_disable_sandbox=1; fi\n"
        "  previous=\"$argument\"\n"
        "done\n"
        "if [[ $has_disable_sandbox -ne 1 ]]; then\n"
        "  echo 'nested sandbox hid the system user database' >&2\n"
        "  exit 42\n"
        "fi\n"
        "\"$MUSE_WORKSPACE_PROBE\" \"$workspace\"\n"
    )

    proc, repo = _stubbed_runner(
        tmp_path,
        {
            "agent": "muse",
            "run": "ssh-workspace-run",
            "gate": "ok",
            "do": "ticket",
            "work": {"ref": "example/widgets#42", "repo": "example/widgets"},
        },
        muse_body=muse_body,
        gh_body=gh_body,
        extra_env={
            "MUSE_WORKSPACE_PROBE": str(probe),
            "MUSE_PROBE_RESULT": str(
                tmp_path / "runner-repo" / "workspace-probe.json"
            ),
            "REMOTE_REPO": str(remote),
            "TEST_SSH": str(ssh),
            "SSH_LOG": str(tmp_path / "runner-repo" / "ssh.log"),
        },
    )

    assert proc.returncode == 0, proc.stderr
    observed = json.loads((repo / "workspace-probe.json").read_text())
    assert observed["uid"] == os.getuid()
    assert observed["user"]
    assert observed["origin"] == "ssh://git@forge/muse-regression.git"
    assert observed["remote_ref_removed"] is True

    ssh_calls = (repo / "ssh.log").read_text().splitlines()
    assert "-T git@forge" in ssh_calls
    assert sum("git-receive-pack" in call for call in ssh_calls) == 2
    assert "--disable-sandbox" in (repo / "muse.args").read_text().splitlines()


@pytest.mark.parametrize(
    ("gate", "outcome"),
    (
        ("over", "skipped-over-pace"),
        ("unknown", "skipped-usage-unknown"),
        ("ok", "nothing-to-do"),
    ),
)
def test_a_stop_finishes_without_a_clone_or_model(tmp_path, gate, outcome):
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "stop-run", "gate": gate, "do": "stop"},
    )

    assert proc.returncode == 0, proc.stderr
    assert not (repo / "gh.log").exists()
    assert not (repo / "muse.args").exists()
    assert (repo / "heartbeat.log").read_text() == (
        "finish --agent muse --run stop-run --outcome {}\n".format(outcome)
    )


def test_the_runner_refuses_any_lane_other_than_escalated_max():
    bad_tier = subprocess.run(
        ["/bin/bash", str(SCRIPT), "standard", "max"],
        capture_output=True,
        text=True,
    )
    bad_effort = subprocess.run(
        ["/bin/bash", str(SCRIPT), "escalated", "high"],
        capture_output=True,
        text=True,
    )

    assert bad_tier.returncode == 1
    assert "tier must be escalated" in bad_tier.stderr
    assert bad_effort.returncode == 1
    assert "reasoning effort must be max" in bad_effort.stderr


def test_a_clone_failure_finishes_errored_and_never_launches_muse(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path,
        {
            "agent": "muse",
            "run": "clone-failure",
            "gate": "ok",
            "do": "ticket",
            "work": {"ref": "example/widgets#42", "repo": "example/widgets"},
        },
        gh_status=23,
    )

    assert proc.returncode == 1
    assert not (repo / "muse.args").exists()
    assert (repo / "heartbeat.log").read_text() == (
        "finish --agent muse --run clone-failure --outcome errored --note "
        "could not clone example/widgets into the fresh implementation workspace\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()
    assert not list((tmp_path / "workspaces").iterdir())


def test_the_wall_clock_bound_kills_the_process_group_and_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path,
        {
            "agent": "muse",
            "run": "hung-run",
            "gate": "ok",
            "do": "ticket",
            "work": {"ref": "example/widgets#42", "repo": "example/widgets"},
        },
        bound_seconds=1,
        muse_body=(
            "#!/bin/bash\n"
            "trap '' TERM\n"
            "while true; do sleep 1; done\n"
        ),
    )

    assert proc.returncode == 124
    assert "killing run after 1s" in proc.stderr
    assert (repo / "heartbeat.log").read_text() == (
        "finish --agent muse --run hung-run --outcome errored --note "
        "killed after 0 minutes: escalated implementation run exceeded the "
        "wall-clock bound (#392)\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()
    assert not list((tmp_path / "workspaces").iterdir())
