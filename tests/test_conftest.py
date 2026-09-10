"""The shared heartbeat fixture keeps real appends off the live spool."""

from __future__ import annotations

import json
from pathlib import Path

import heartbeat


def test_real_heartbeat_append_uses_the_per_test_spool(heartbeat_isolation):
    live_files = {
        path: path.read_bytes()
        for path in Path("~/.claude/command-center-heartbeat")
        .expanduser()
        .glob("*.jsonl")
    }

    record = {"run": "fixture-run", "agent": "codex", "phase": "start", "ts": 1}
    assert heartbeat.append("codex", record) == "spooled"
    assert Path(heartbeat.SPOOL_DIR) == heartbeat_isolation

    spool_file = heartbeat_isolation / "codex.jsonl"
    assert [json.loads(line) for line in spool_file.read_text().splitlines()] == [record]
    assert {
        path: path.read_bytes()
        for path in live_files
    } == live_files
