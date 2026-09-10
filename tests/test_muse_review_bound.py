"""scripts/muse-review kills a run that outlives its wall-clock bound (#392)."""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "muse-review"


def _stub_repo(tmp_path):
    """A fake clone: the real routine text plus a heartbeat.py that records."""
    repo = tmp_path / "repo"
    (repo / "routines").mkdir(parents=True)
    shutil.copy(ROOT / "routines" / "muse.md", repo / "routines" / "muse.md")
    (repo / "heartbeat.py").write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[0]).with_name('heartbeat.log').write_text(' '.join(sys.argv[1:]))\n"
    )
    return repo


def _stub_muse(tmp_path, seconds):
    muse = tmp_path / "muse"
    muse.write_text("#!/bin/bash\nsleep {}\nexit 0\n".format(seconds))
    muse.chmod(muse.stat().st_mode | stat.S_IEXEC)
    return muse


def _run(tmp_path, muse_seconds, bound_seconds):
    repo = _stub_repo(tmp_path)
    env = dict(os.environ,
               MUSE_REVIEW_REPO=str(repo),
               MUSE_BIN=str(_stub_muse(tmp_path, muse_seconds)),
               MUSE_REVIEW_BOUND_SECONDS=str(bound_seconds),
               HOME=str(tmp_path))
    proc = subprocess.run(["/bin/bash", str(SCRIPT), "standard", "high"],
                          env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30)
    log = repo / "heartbeat.log"
    return proc, (log.read_text() if log.exists() else "")


def test_a_run_past_the_bound_is_killed_and_finished_errored(tmp_path):
    proc, heartbeat = _run(tmp_path, muse_seconds=30, bound_seconds=2)

    assert proc.returncode == 124
    assert "killing run after 2s" in proc.stderr
    assert heartbeat.startswith("finish --agent muse --outcome errored")
    assert "killed after 0 minutes" in heartbeat


def test_a_run_inside_the_bound_is_left_alone(tmp_path):
    proc, heartbeat = _run(tmp_path, muse_seconds=1, bound_seconds=20)

    assert proc.returncode == 0
    assert "killing run" not in proc.stderr
    assert heartbeat == ""
