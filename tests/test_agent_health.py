"""The funnel's watchdog condition reader uses the shared assessment."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


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
