"""The funnel's watchdog condition reader uses the shared assessment."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from agent_health import assess  # noqa: E402


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _start(run, minutes_ago):
    return {
        "run": run,
        "phase": "start",
        "ts": NOW.timestamp() - minutes_ago * 60,
        "agent": "codex",
    }


def _finish(run, minutes_ago):
    return {
        "run": run,
        "phase": "finish",
        "ts": NOW.timestamp() - minutes_ago * 60,
        "agent": "codex",
        "outcome": "done",
    }


def _error_rows():
    return [
        {
            "run": str(i),
            "phase": "finish",
            "ts": NOW.timestamp() - (i + 1) * 3600,
            "agent": "codex",
            "outcome": "errored",
            "note": "boom {}".format(i),
        }
        for i in range(3)
    ]


def test_agent_health_names_the_agent_and_preserves_watchdog_wording(monkeypatch):
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"codex": "openai"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: _error_rows())

    assert funnel.agent_health(NOW) == [{
        "agent": "codex",
        "condition": "`codex` errored 3 times this week. Most recent: boom 0; boom 1; boom 2",
    }]


def test_healthy_heartbeat_rows_render_no_agent_health(monkeypatch):
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"codex": "openai"})
    monkeypatch.setattr(
        heartbeat,
        "read",
        lambda agent: [{
            "run": "healthy",
            "phase": "finish",
            "ts": NOW.timestamp() - 3600,
            "agent": "codex",
            "outcome": "nothing-to-do",
        }],
    )

    assert funnel.agent_health(NOW) == []


def test_one_open_start_older_than_ten_times_median_is_reported():
    rows = [
        _start("completed", 2),
        _finish("completed", 1.7),
        _start("hung", 16),
    ]

    conditions = assess("codex", rows, NOW.timestamp())

    assert len(conditions) == 1
    assert "one open start `hung`" in conditions[0]
    assert "aged 16m" in conditions[0]
    assert "median completed-run length 18s" in conditions[0]


def test_one_open_start_under_the_floor_is_not_reported():
    rows = [
        _start("completed", 2),
        _finish("completed", 1.7),
        _start("working", 5),
    ]

    assert assess("codex", rows, NOW.timestamp()) == []


def test_finished_start_is_not_reported_as_open():
    rows = [
        _start("completed", 16),
        _finish("completed", 15.7),
    ]

    assert assess("codex", rows, NOW.timestamp()) == []


def test_three_open_starts_keep_the_dying_condition():
    rows = [
        _start("one", 180),
        _start("two", 150),
        _start("three", 130),
    ]

    conditions = assess("codex", rows, NOW.timestamp())

    assert len(conditions) == 1
    assert "started and never finished" in conditions[0]
