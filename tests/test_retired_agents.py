"""A retired agent's silence is not a dying run (#431)."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from agent_health import assess  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _quiet_after_a_busy_cadence(agent, minutes_silent=90, every=15, count=40):
    """Forty paired runs every 15 minutes, then nothing for an hour and a half."""
    rows = []
    end = NOW.timestamp() - minutes_silent * 60
    for i in range(count):
        ts = end - (count - i) * every * 60
        rows.append({"run": "r%d" % i, "phase": "start", "ts": ts, "agent": agent})
        rows.append({"run": "r%d" % i, "phase": "finish", "ts": ts + 12,
                     "agent": agent, "outcome": "nothing-to-do"})
    return rows


def test_zcode_is_retired_and_the_others_are_not():
    assert "zcode" in heartbeat.RETIRED_AGENTS
    assert heartbeat.RETIRED_AGENTS < set(heartbeat.PROVIDERS)
    assert not {"codex", "muse"} & heartbeat.RETIRED_AGENTS


def test_the_same_silence_alarms_for_a_live_agent(monkeypatch):
    rows = _quiet_after_a_busy_cadence("codex")
    conditions = assess("codex", rows, NOW.timestamp())
    assert any("Nothing recorded" in c for c in conditions)


def test_the_brief_skips_a_retired_agent_but_not_a_live_one(monkeypatch):
    rows = {a: _quiet_after_a_busy_cadence(a) for a in ("codex", "zcode")}
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows.get(agent, []))

    found = funnel.agent_health(NOW)

    assert {c["agent"] for c in found} == {"codex"}


def test_the_watchdog_skips_a_retired_agent(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "watchdog", ROOT / ".github" / "scripts" / "watchdog.py")
    watchdog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watchdog)
    seen = []
    monkeypatch.setattr(watchdog, "records", lambda agent: seen.append(agent) or [])
    monkeypatch.setattr(watchdog, "existing_issue", lambda: {})
    monkeypatch.setattr(watchdog, "gh", lambda *a: "")

    watchdog.main()

    assert "zcode" not in seen
    assert {"codex", "muse", "claude"} <= set(seen)
