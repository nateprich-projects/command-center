"""Recent input re-send ratios surfaced by the funnel brief."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def finish(agent, total, fresh, hours_ago=1):
    return {
        "agent": agent,
        "phase": "finish",
        "ts": int((NOW - timedelta(hours=hours_ago)).timestamp()),
        "input_usage": {
            "total_input_tokens": total,
            "fresh_input_tokens": fresh,
            "ratio": total / fresh if fresh else None,
        },
    }


def test_recent_ratio_is_weighted_and_omits_unmetered_agents(monkeypatch):
    rows = {
        "codex": [finish("codex", 100, 25), finish("codex", 20, 20)],
        "zcode": [finish("zcode", 90, 30)],
        "claude": [finish("claude", 500, 50)],
        "muse": [finish("muse", 400, 40)],
    }
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows[agent])

    assert funnel.recent_resend_ratio(NOW) == {
        "codex": 120 / 45,
        "zcode": 3.0,
    }


def test_old_or_incomplete_records_do_not_create_a_figure(monkeypatch):
    old = finish("codex", 100, 25, hours_ago=24 * 31)
    incomplete = finish("codex", 100, 25)
    incomplete["input_usage"].pop("fresh_input_tokens")
    malformed = finish("codex", 100, 25)
    malformed["input_usage"]["fresh_input_tokens"] = 101
    monkeypatch.setattr(
        heartbeat,
        "read",
        lambda agent: [old, incomplete, malformed] if agent == "codex" else [],
    )

    assert funnel.recent_resend_ratio(NOW) == {}


def test_brief_reports_the_ratios(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {"codex": 4.0})

    assert funnel.cmd_brief([], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["resend_ratio"] == {"codex": 4.0}
