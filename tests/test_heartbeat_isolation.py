"""Heartbeat writes from tests never reach the live spool (#876).

The check is by content, not by size: the live agents append to the live spool
concurrently, so a size comparison cannot tell a leak from ordinary operation.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

from conftest import LIVE_SPOOL_DIR

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_a_fresh_interpreter_writes_to_the_inherited_test_spool(heartbeat_isolation):
    marker = "isolation-canary-" + uuid.uuid4().hex
    code = (
        "import heartbeat\n"
        "def offline(*a, **k):\n"
        "    raise heartbeat.HeartbeatError('offline')\n"
        "heartbeat.gh = offline\n"
        "print(heartbeat.SPOOL_DIR)\n"
        "heartbeat.append({!r}, {{'phase': 'start', 'run': {!r}}})\n"
    ).format(marker, marker)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
        capture_output=True, text=True, check=True,
    )

    assert result.stdout.splitlines()[0] == str(heartbeat_isolation)
    written = "".join(p.read_text() for p in heartbeat_isolation.glob("*.jsonl"))
    assert marker in written
    for live in LIVE_SPOOL_DIR.glob("*.jsonl"):
        assert marker not in live.read_text(errors="replace")


def test_funnel_reads_the_same_override(heartbeat_isolation):
    result = subprocess.run(
        [sys.executable, "-c", "import funnel; print(funnel.HEARTBEAT_SPOOL)"],
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == str(heartbeat_isolation)
