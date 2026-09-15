"""The funnel's watchdog condition reader uses the shared assessment."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from agent_health import assess  # noqa: E402


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
SILENCE_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "heartbeat_silence_window.json"


def _silence_fixture():
    payload = json.loads(SILENCE_FIXTURE.read_text())
    now = datetime.fromisoformat(payload["now"].replace("Z", "+00:00"))
    rows = {
        agent: [
            dict(row, ts=datetime.fromisoformat(
                row["ts"].replace("Z", "+00:00")
            ).timestamp())
            for row in agent_rows
        ]
        for agent, agent_rows in payload["agents"].items()
    }
    return now, rows


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


def test_brief_run_summary_separates_rebegins_from_finishes(monkeypatch):
    rows = [
        _start("old", 4),
        {
            "run": "old",
            "phase": "finish",
            "ts": NOW.timestamp() - 3 * 60,
            "agent": "codex",
            "outcome": "skipped-blocked",
            "re_begun_by": "fresh",
        },
        _start("fresh", 2),
        _finish("fresh", 1),
    ]
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"codex": "openai"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)

    assert funnel.agent_run_summary(NOW) == [{
        "agent": "codex",
        "starts": 2,
        "finishes": 1,
        "re_begins": 1,
    }]


def test_retired_prompt_events_raise_no_condition():
    """`prompt-drift` and `prompt-mismatch` left the outcome vocabulary with
    `--routine-sha` (#821). Old rows may still sit in the heartbeat history;
    they raise nothing."""
    rows = [
        {
            "run": "drift",
            "phase": "event",
            "ts": NOW.timestamp() - 2 * 3600,
            "agent": "codex",
            "outcome": "prompt-drift",
        },
        {
            "run": "mismatch",
            "phase": "event",
            "ts": NOW.timestamp() - 3600,
            "agent": "codex",
            "outcome": "prompt-mismatch",
        },
    ]

    assert assess("codex", rows, NOW.timestamp()) == []


def test_sparse_history_uses_the_absolute_silence_floor_and_reaches_the_brief(
    monkeypatch, capsys,
):
    now, rows = _silence_fixture()

    conditions = assess("codex", rows["codex"], now.timestamp())

    assert len(conditions) == 1
    assert "absolute silence floor 6h exceeded" in conditions[0]
    assert "Nothing recorded for 1d8h1m" in conditions[0]
    assert "normal gap" not in conditions[0]

    monkeypatch.setattr(heartbeat, "PROVIDERS", {
        "codex": "openai",
        "muse": "meta",
    })
    monkeypatch.setattr(
        funnel, "_brief_heartbeat_rows", lambda agent: rows.get(agent, [])
    )
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    item = funnel.Item(
        repo="nateprich/beta", number=60, title="A quiet project",
        url="https://example.invalid/60", state="OPEN", status="Building",
        klass="Improve",
    )
    assert funnel.cmd_brief([item], now) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["agent_health"] == [{
        "agent": "codex",
        "condition": conditions[0],
    }]


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
