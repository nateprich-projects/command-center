"""The launchd keeper records a bounded, parseable machine-health line."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run-keeper"


def run_git(*args, cwd=None):
    return subprocess.run(
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def make_heartbeat_remote(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    run_git("init", "--bare", str(bare))
    run_git("init", str(seed))
    run_git("-C", str(seed), "config", "user.name", "test")
    run_git("-C", str(seed), "config", "user.email", "test@example.com")
    (seed / "README").write_text("main\n")
    run_git("-C", str(seed), "add", "README")
    run_git("-C", str(seed), "commit", "-m", "main")
    run_git("-C", str(seed), "branch", "-M", "main")
    run_git("-C", str(seed), "remote", "add", "origin", str(bare))
    run_git("-C", str(seed), "push", "-u", "origin", "main")
    run_git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")

    run_git("-C", str(seed), "checkout", "--orphan", "heartbeat")
    (seed / "README").unlink()
    (seed / ".keep").write_text("heartbeat\n")
    run_git("-C", str(seed), "add", "-A")
    run_git("-C", str(seed), "commit", "-m", "heartbeat")
    run_git("-C", str(seed), "push", "origin", "heartbeat")
    run_git("-C", str(seed), "checkout", "main")

    checkout = tmp_path / "run"
    run_git("clone", str(bare), str(checkout))
    return bare, checkout


def test_keeper_appends_one_parseable_line_per_run(tmp_path):
    bare, checkout = make_heartbeat_remote(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()

    write_executable(
        tools / "ps",
        """
case "$*" in
  *lstart*) printf '%s\\n' 'Sat Sep 12 21:00:00 2026' ;;
  *-A*) printf '%s\\n' '501' '501' '502' ;;
  *) printf '%s\\n' '501' ;;
esac
""",
    )
    write_executable(tools / "sysctl", "printf '%s\\n' 2666\n")
    write_executable(
        tools / "uptime",
        "printf '%s\\n' '21:00 up 8 days, 1 user, load averages: 1.00 2.00 3.00'\n",
    )
    write_executable(
        tools / "pgrep",
        "case \"$*\" in *codex*) exit 0 ;; *muse*) exit 0 ;; *) exit 1 ;; esac\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "COMMAND_CENTER_RUN_REPO": str(checkout),
            "COMMAND_CENTER_SENTINEL_PS": str(tools / "ps"),
            "COMMAND_CENTER_SENTINEL_PGREP": str(tools / "pgrep"),
            "COMMAND_CENTER_SENTINEL_SYSCTL": str(tools / "sysctl"),
            "COMMAND_CENTER_SENTINEL_UPTIME": str(tools / "uptime"),
            "COMMAND_CENTER_SENTINEL_FILE": "sentinel.log",
        }
    )

    first = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
    second = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    content = run_git("--git-dir", str(bare), "show", "heartbeat:sentinel.log").stdout
    lines = content.splitlines()
    assert len(lines) == 2
    fields = [dict(part.split("=", 1) for part in line.split()) for line in lines]
    assert all(
        record == {
            "timestamp": "Sat_Sep_12_21:00:00_2026",
            "user_processes": "2",
            "maxprocperuid": "2666",
            "load_1": "1.00",
            "load_5": "2.00",
            "load_15": "3.00",
            "codex": "1",
            "claude": "0",
            "muse": "1",
        }
        for record in fields
    )
