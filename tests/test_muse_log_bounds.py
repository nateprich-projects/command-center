"""Muse lane stdout logs keep their newest bytes at runner startup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent

LANES = (
    ("review-standard", "muse-review", "muse.md", "standard", "high",
     "MUSE_REVIEW_REPO", "command-center-muse-review-standard.log"),
    ("review-escalated", "muse-review", "muse.md", "escalated", "max",
     "MUSE_REVIEW_REPO", "command-center-muse-review.log"),
    ("implement", "muse-implement", "muse-implement.md", "escalated", "max",
     "MUSE_IMPLEMENT_REPO", "command-center-muse-implement.log"),
)


def _runner(tmp_path: Path, lane):
    _, script_name, routine_name, tier, effort, repo_variable, log_name = lane
    repo = tmp_path / "repo"
    (repo / "routines").mkdir(parents=True)
    routine = ROOT / "routines" / routine_name
    (repo / "routines" / routine_name).write_text(routine.read_text())
    (repo / "begin.json").write_text(json.dumps({
        "agent": "muse",
        "run": "log-bound-run",
        "gate": "ok",
        "do": "stop",
    }))
    (repo / "funnel.py").write_text(
        "import pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "command = sys.argv[1]\n"
        "if command == 'session-server':\n"
        "    print('127.0.0.1:1:stub', flush=True)\n"
        "elif command == 'begin':\n"
        "    print((root / 'begin.json').read_text(), end='')\n"
        "elif command == 'session-stop':\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('unexpected funnel command: ' + command)\n"
    )
    (repo / "heartbeat.py").write_text(
        "import pathlib\n"
        "with (pathlib.Path(__file__).parent / 'heartbeat.log').open('a') as fh:\n"
        "    fh.write('finished\\n')\n"
        "print('runner-output')\n"
    )

    log_dir = tmp_path / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / log_name
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        MUSE_LOG_CAP_BYTES="8",
        **{repo_variable: str(repo)},
    )
    proc = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / script_name), tier, effort],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc, log_path


@pytest.mark.parametrize("lane", LANES, ids=[lane[0] for lane in LANES])
@pytest.mark.parametrize(
    ("initial", "expected"),
    (
        (b"0123456789", b"23456789"),
        (b"01234567", b"01234567"),
        (None, None),
    ),
)
def test_runner_bounds_its_log_without_touching_noop_cases(
    tmp_path, lane, initial, expected
):
    _, _, _, _, _, _, log_name = lane
    log_path = tmp_path / "Library" / "Logs" / log_name
    if initial is not None:
        log_path.parent.mkdir(parents=True)
        log_path.write_bytes(initial)
        inode_before = log_path.stat().st_ino

    proc, resolved_path = _runner(tmp_path, lane)

    assert proc.returncode == 0, proc.stderr
    assert resolved_path == log_path
    if expected is None:
        assert not log_path.exists()
    else:
        expected_bytes = expected
        if len(initial) > 8:
            expected_bytes += b"runner-output\n"
        assert log_path.read_bytes() == expected_bytes
        if len(initial) == len(expected):
            assert log_path.stat().st_ino == inode_before
    assert not list(log_path.parent.glob(log_path.name + ".trim.*"))
