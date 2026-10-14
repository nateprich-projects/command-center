"""A normal 7-12h claude gap is the twelve-hourly watch working (#2585).

Ticket nateprich-projects/command-center#2585, parent #2584: the
hobby-linux watchdog filed Broken silence alerts (#2559, #2564, #2581)
for claude's normal overnight gaps between twelve-hourly funnel-watch
runs. The fix is a 13-hour per-agent silence floor for claude (12-hour
cadence plus 1 hour of slack), leaving PAUSED_AGENTS semantics
unchanged.

First-verify record (ticket precondition; read, not assumed):
- Silence rule: .github/scripts/watchdog.py:312 `def assess` wraps
  `agent_health.assess`, passing `silence_floor_seconds` and the shared
  absolute floor plus `paused`.
- Pause roster: heartbeat.py:298 `PAUSED_AGENTS = frozenset({"codex"})`,
  heartbeat.py:301 `def paused_agents` adapter; claude is not paused.
- Dying-run and repeated-errored-run checks live in
  agent_health.py `def assess` beside the silence check and run
  regardless of pause or floor.
- Twelve-hourly cadence: codex_run.py `AUTOMATIONS_WATCH_EXPECTED`
  rrule `RRULE:FREQ=DAILY;BYHOUR=4,16;BYMINUTE=13` (04:13/16:13).
- Claude implement routine off on purpose: routines/claude.md notes the
  Claude routine is off while Muse carries escalated shaping.

The contract under test: claude gaps of 7-12h stay quiet; a gap past
13h (a genuinely missed twelve-hourly run) still alarms, only ~1h late;
dying and repeated-errored runs still file; other agents are unchanged.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "watchdog", ROOT / ".github" / "scripts" / "watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

NOW = 1_788_600_000.0
HOUR = 3600


def _hourly_history(agent, quiet_hours, hours=72):
    """Hourly paired runs for three days, last finish exactly at the gap."""
    rows = []
    end = NOW - quiet_hours * HOUR
    for i in range(hours):
        finish_ts = end - (hours - 1 - i) * HOUR
        rows.append({"run": "h%d" % i, "phase": "start",
                     "ts": finish_ts - 60, "agent": agent})
        rows.append({"run": "h%d" % i, "phase": "finish", "ts": finish_ts,
                     "agent": agent, "outcome": "done"})
    return rows


def _bound_open_start(run, ago_hours, agent="claude", ticket=7):
    return [
        {"run": run, "phase": "start", "ts": NOW - ago_hours * HOUR,
         "agent": agent, "ticket": ticket},
        {"run": run, "phase": "bind", "ts": NOW - ago_hours * HOUR + 5,
         "agent": agent, "do": "ticket",
         "work": "nateprich-projects/command-center#{}".format(ticket)},
    ]


def _silence(problems):
    return [p for p in problems
            if "Nothing recorded" in p or "silence floor" in p]


def test_floor_value_covers_cadence_plus_slack():
    """13h: the 12-hour watch cadence plus 1 hour of slack."""
    assert watchdog.AGENT_SILENCE_FLOOR_SECONDS["claude"] == 13 * HOUR


def test_claude_is_not_paused():
    """The fix is a floor, not a pause: PAUSED_AGENTS still names codex."""
    assert "claude" not in heartbeat.paused_agents()
    assert heartbeat.PAUSED_AGENTS == frozenset({"codex"})


def test_normal_overnight_gaps_stay_quiet():
    """Reproduction: claude quiet 7h and 12h raises no silence alarm."""
    assert watchdog.assess("claude", _hourly_history("claude", 7), NOW) == []
    assert watchdog.assess("claude", _hourly_history("claude", 12), NOW) == []


def test_a_gap_past_the_floor_still_alarms():
    """Guard: a genuinely missed twelve-hourly run still files, ~1h late."""
    problems = watchdog.assess("claude", _hourly_history("claude", 14), NOW)
    assert len(problems) == 1
    assert "Nothing recorded for 14h" in problems[0]

    missed = watchdog.assess("claude", _hourly_history("claude", 20), NOW)
    assert len(missed) == 1
    assert "Nothing recorded for 20h" in missed[0]


def test_thin_history_gap_stays_quiet_under_the_floor():
    """The floor covers the absolute-silence path too, not just p90."""
    only = [{"run": "only", "phase": "finish", "ts": NOW - 10 * HOUR,
             "agent": "claude", "outcome": "done"}]
    assert watchdog.assess("claude", only, NOW) == []

    stale = [{"run": "only", "phase": "finish", "ts": NOW - 14 * HOUR,
              "agent": "claude", "outcome": "done"}]
    problems = watchdog.assess("claude", stale, NOW)
    assert len(problems) == 1
    assert "absolute silence floor 13h exceeded" in problems[0]


def test_other_agents_are_unchanged_by_the_floor():
    """A 7h codex gap still alarms: the floor names claude only."""
    problems = watchdog.assess("codex", _hourly_history("codex", 7), NOW)
    assert len(problems) == 1
    assert "Nothing recorded for 7h" in problems[0]


def test_floor_cannot_suppress_dying_runs():
    """Three bound open starts file even while the silence gap is normal."""
    rows = _hourly_history("claude", 0.5)
    for run, ago in (("one", 3.0), ("two", 2.6), ("three", 2.2)):
        rows += _bound_open_start(run, ago)
    problems = watchdog.assess("claude", rows, NOW)
    assert any("started and never finished" in p for p in problems)
    assert _silence(problems) == []


def test_floor_cannot_suppress_repeated_errors():
    """Three errored runs file even while the silence gap is normal."""
    # The errored finishes are the newest judged runs: minutes old, while
    # the healthy history ends half an hour back.
    rows = _hourly_history("claude", 0.5) + [
        {"run": "error-{}".format(i), "phase": "finish",
         "ts": NOW - (i + 1) * 5 * 60, "agent": "claude",
         "outcome": "errored", "note": "boom {}".format(i)}
        for i in range(watchdog.ERROR_THRESHOLD)
    ]
    problems = watchdog.assess("claude", rows, NOW)
    assert any("errored 3 times consecutively" in p for p in problems)
    assert _silence(problems) == []
