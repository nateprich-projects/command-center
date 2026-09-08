"""The routine literal is stable and drift is visible without stopping work."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
import usage  # noqa: E402


NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)


def _allow_begin(monkeypatch):
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: {"over_pace": False},
    )


def _routine(tmp_path, body):
    routines = tmp_path / "routines"
    routines.mkdir(parents=True)
    path = routines / "zcode.md"
    path.write_text(body)
    return path


def test_routine_sha_is_unchanged_by_its_own_literal(tmp_path):
    body = """# routine

python3 funnel.py begin --agent zcode --tier standard
"""
    plain = _routine(tmp_path, body)
    with_literal = _routine(
        tmp_path / "literal",
        body.replace(
            "--tier standard",
            "--tier standard --routine-sha deadbeef",
        ),
    )

    assert funnel.routine_sha(plain) == funnel.routine_sha(with_literal)


def test_matching_routine_sha_reports_ok_and_does_not_record_drift(
    tmp_path, monkeypatch, capsys
):
    path = _routine(
        tmp_path,
        "python3 funnel.py begin --agent zcode --tier standard\n",
    )
    monkeypatch.setattr(funnel, "CHECKOUT_ROOT", tmp_path)
    _allow_begin(monkeypatch)
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    events = []
    monkeypatch.setattr(
        heartbeat,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert funnel.cmd_begin(
        [], NOW, "zcode", "standard", False,
        routine_sha_literal=funnel.routine_sha(path),
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["routine_sha"]["status"] == "ok"
    assert result["routine_sha"]["actual"] == result["routine_sha"]["expected"]
    assert result["do"] == "review"
    assert result["work"] == work
    assert events == []


def test_mismatching_routine_sha_records_prompt_drift_and_keeps_working(
    tmp_path, monkeypatch, capsys
):
    path = _routine(
        tmp_path,
        "python3 funnel.py begin --agent zcode --tier standard\n",
    )
    monkeypatch.setattr(funnel, "CHECKOUT_ROOT", tmp_path)
    _allow_begin(monkeypatch)
    work = {"pr": 8, "repo": "nateprich/gamma", "ref": "nateprich/gamma#20"}
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    events = []
    monkeypatch.setattr(
        heartbeat,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert funnel.cmd_begin(
        [], NOW, "zcode", "standard", False,
        routine_sha_literal="0" * 64,
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["routine_sha"]["status"] == "drift"
    assert result["routine_sha"]["actual"] == funnel.routine_sha(path)
    assert result["do"] == "review"
    assert result["work"] == work
    assert len(events) == 1
    assert events[0][0] == ("zcode", "run-id", "prompt-drift")
    assert events[0][1]["routine_sha"] == result["routine_sha"]


def test_prompt_drift_event_is_non_terminal(monkeypatch):
    records = []
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append((agent, record)) or "pushed",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.record_event("zcode", "run-id", "prompt-drift") == "pushed"
    assert records[0][0] == "zcode"
    assert records[0][1]["run"] == "run-id"
    assert records[0][1]["phase"] == "event"
    assert records[0][1]["outcome"] == "prompt-drift"
