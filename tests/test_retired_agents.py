"""A retired agent's silence is not a dying run (#431, #1106).

zcode retired on 2026-09-09, runs again as the engine's z.ai standard tier
from 2026-09-23, and retires again by itself at `heartbeat.ZAI_STANDARD_UNTIL`.
"""

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


#: 2026-10-07 00:00 PDT, when the z.ai plan expires (Nate, 2026-09-23).
ZAI_CUTOFF = datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc).timestamp()


def test_the_zai_cutoff_is_the_start_of_the_expiry_day_in_pacific_time():
    assert heartbeat.ZAI_STANDARD_UNTIL == ZAI_CUTOFF == 1791356400


def test_zcode_is_live_until_the_zai_cutoff_and_retired_from_it():
    """zcode is the engine's z.ai standard tier from 2026-09-23; its
    silence alarms while it runs, and stops alarming by itself when the plan
    expires, with no edit to make."""
    assert heartbeat.retired_agents(ZAI_CUTOFF - 1) == frozenset()
    assert heartbeat.retired_agents(ZAI_CUTOFF) == {"zcode"}
    assert heartbeat.retired_agents(ZAI_CUTOFF + 86400) == {"zcode"}
    assert heartbeat.RETIRED_AGENTS == heartbeat.retired_agents()


def test_codex_claude_and_muse_are_never_retired():
    """Codex implements both tiers since #1315; its silence must alarm."""
    for now in (NOW.timestamp(), ZAI_CUTOFF - 1, ZAI_CUTOFF + 86400):
        retired = heartbeat.retired_agents(now)
        assert retired <= set(heartbeat.PROVIDERS)
        assert not {"claude", "codex", "muse"} & retired


def test_a_live_zcode_lane_alarms_on_silence_like_any_other(monkeypatch):
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS",
                        heartbeat.retired_agents(ZAI_CUTOFF - 1))
    rows = {a: _quiet_after_a_busy_cadence(a) for a in ("muse", "zcode")}
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows.get(agent, []))

    found = funnel.agent_health(NOW)

    assert {c["agent"] for c in found} == {"muse", "zcode"}


def test_the_same_silence_alarms_for_a_live_agent(monkeypatch):
    rows = _quiet_after_a_busy_cadence("muse")
    conditions = assess("muse", rows, NOW.timestamp())
    assert any("Nothing recorded" in c for c in conditions)


def test_the_brief_skips_a_retired_agent_but_not_a_live_one(monkeypatch):
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS",
                        heartbeat.retired_agents(ZAI_CUTOFF))
    rows = {a: _quiet_after_a_busy_cadence(a) for a in ("muse", "zcode")}
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows.get(agent, []))

    found = funnel.agent_health(NOW)

    assert {c["agent"] for c in found} == {"muse"}


def test_codex_silence_alarms_again_once_it_implements(monkeypatch):
    """Codex was retired while Muse implemented (#1106) and is live again
    since #1325: the same gap now alarms for it as for any running lane."""
    rows = _quiet_after_a_busy_cadence("codex")
    spools = {"codex": rows, "muse": [dict(row) for row in rows]}
    monkeypatch.setattr(heartbeat, "read", lambda agent: spools.get(agent, []))

    found = funnel.agent_health(NOW)

    assert {c["agent"] for c in found} == {"codex", "muse"}
    assert all("Nothing recorded" in c["condition"] for c in found)


def test_the_watchdog_skips_retired_agents(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "watchdog", ROOT / ".github" / "scripts" / "watchdog.py")
    watchdog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watchdog)
    seen = []
    monkeypatch.setattr(watchdog.heartbeat, "RETIRED_AGENTS",
                        watchdog.heartbeat.retired_agents(ZAI_CUTOFF))
    monkeypatch.setattr(watchdog, "records", lambda agent: seen.append(agent) or [])
    monkeypatch.setattr(watchdog, "existing_issue", lambda: {})
    monkeypatch.setattr(watchdog, "gh", lambda *a: "")

    watchdog.main()

    assert "zcode" not in seen
    assert {"muse", "claude", "codex"} <= set(seen)
