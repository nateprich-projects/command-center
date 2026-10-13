"""A paused schedule's silence is the pause working, not a dying lane (#2499).

Ticket nateprich-projects/command-center#2499, parent #2487: the watchdog
alarmed on intentionally paused Codex silence (#2034: nothing for 3d11h while
the Codex automations sat paused per #2003). The silence check must skip
paused agents while a silent active agent still alarms, and a stale paused
flag must never suppress an agent that is active but stuck.

First-verify record (ticket precondition; read, not assumed):
- Canonical roster: heartbeat.py:259 `def retired_agents`, heartbeat.py:286
  `RETIRED_AGENTS` (module-level snapshot read once per process).
- Brief read for retired agents: funnel.py:7118 `def agent_health`
  (retired set read ~7128, `if agent in retired: continue` ~7130),
  funnel.py:7156 `def agent_run_summary` (same skip ~7166), plus the
  maintenance-window read ~6807 and the claim scan ~6111. Both found, so the
  paused roster joins them in heartbeat.py and the watchdog adapts it into
  its silence check; the brief itself is unchanged by this ticket.

The adapter contract under test: heartbeat.paused_agents() is the one roster
(a Codex Scheduled automation reading PAUSED instead of ACTIVE enters here,
never as a second source the watchdog reads directly). The watchdog passes
it as `paused` into the shared assessment, which drops only the silence
conditions — relative and absolute floor — and raises nothing in their
place. Dying, errored, degraded, open-start, unreadable and stale-runtime
conditions are unchanged by a pause, so an active stuck agent still alarms
even under a stale flag.
"""

from __future__ import annotations

import importlib.util
import json
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


def _busy_then_quiet(agent, every_minutes=6, count=40, quiet_hours=83):
    """Forty paired runs on a 6-minute cadence, then nothing for 3d11h."""
    rows = []
    end = NOW - quiet_hours * HOUR
    for i in range(count):
        ts = end - (count - i) * every_minutes * 60
        rows.append({"run": "r%d" % i, "phase": "start", "ts": ts,
                     "agent": agent})
        rows.append({"run": "r%d" % i, "phase": "finish", "ts": ts + 12,
                     "agent": agent, "outcome": "done"})
    return rows


def _bound_open_start(run, ago_minutes, agent="codex", ticket=7):
    return [
        {"run": run, "phase": "start", "ts": NOW - ago_minutes * 60,
         "agent": agent, "ticket": ticket},
        {"run": run, "phase": "bind", "ts": NOW - ago_minutes * 60 + 5,
         "agent": agent, "do": "ticket",
         "work": "nateprich-projects/command-center#{}".format(ticket)},
    ]


def test_canonical_paused_roster_names_codex_and_stays_inside_providers():
    """The adapter source: codex paused, nobody retired by it, no stranger."""
    paused = heartbeat.paused_agents()
    assert isinstance(paused, frozenset)
    assert paused == heartbeat.PAUSED_AGENTS
    assert paused <= set(heartbeat.PROVIDERS)
    assert "codex" in paused
    assert not ({"claude", "muse"} & paused)
    assert not (paused & set(heartbeat.retired_agents()))


def test_paused_silence_raises_no_alarm_while_live_silence_alarms():
    """Reproduction: paused codex silent 3d11h raises nothing; live muse alarms."""
    codex_rows = _busy_then_quiet("codex")
    live = watchdog.assess("codex", codex_rows, NOW)
    assert len(live) == 1
    assert "Nothing recorded for 3d11h" in live[0]

    assert watchdog.assess("codex", codex_rows, NOW, paused=True) == []

    muse_rows = _busy_then_quiet("muse")
    unpaused = watchdog.assess("muse", muse_rows, NOW)
    assert len(unpaused) == 1
    assert "Nothing recorded for 3d11h" in unpaused[0]


def test_stale_paused_flag_cannot_suppress_dying_runs():
    """Active but stuck under a stale flag still alarms: dying is not silence."""
    rows = []
    for run, ago in (("one", 180), ("two", 150), ("three", 130)):
        rows += _bound_open_start(run, ago)

    unpaused = watchdog.assess("codex", rows, NOW)
    assert any("started and never finished" in p for p in unpaused)

    paused = watchdog.assess("codex", rows, NOW, paused=True)
    assert any("started and never finished" in p for p in paused)
    assert not any("Nothing recorded" in p for p in paused)


def test_stale_paused_flag_cannot_suppress_repeated_errors():
    """Three errored runs alarm whether or not the lane claims to be paused."""
    rows = [
        {"run": "error-{}".format(i), "phase": "finish",
         "ts": NOW - (i + 1) * HOUR, "agent": "codex",
         "outcome": "errored", "note": "boom {}".format(i)}
        for i in range(watchdog.ERROR_THRESHOLD)
    ]
    assert any("errored 3 times" in p
               for p in watchdog.assess("codex", rows, NOW))
    assert any("errored 3 times" in p
               for p in watchdog.assess("codex", rows, NOW, paused=True))


def test_sparse_silence_floor_is_silence_too():
    """The absolute floor (thin history) is suppressed by a pause as well."""
    rows = [
        {"run": "only", "phase": "finish", "ts": NOW - 83 * HOUR,
         "agent": "codex", "outcome": "done"},
    ]
    live = watchdog.assess("codex", rows, NOW)
    assert any("absolute silence floor" in p for p in live)
    assert watchdog.assess("codex", rows, NOW, paused=True) == []


def test_healthy_skipped_and_over_pace_inputs_are_unchanged_by_pause():
    """Healthy, skipped-* and nothing-to-do stay quiet, paused or not."""
    recent = NOW - HOUR
    cases = [
        [  # healthy: a recent paired run
            {"run": "a", "phase": "start", "ts": recent,
             "agent": "codex", "ticket": 1},
            {"run": "a", "phase": "finish", "ts": recent + 12,
             "agent": "codex", "outcome": "done"},
        ],
        [  # over pace is the system working
            {"run": "a", "phase": "start", "ts": recent,
             "agent": "codex", "ticket": 1},
            {"run": "a", "phase": "finish", "ts": recent + 12,
             "agent": "codex", "outcome": "skipped-over-pace"},
        ],
        [  # empty funnel and held lock
            {"run": "a", "phase": "start", "ts": recent,
             "agent": "codex", "ticket": 1},
            {"run": "a", "phase": "finish", "ts": recent + 12,
             "agent": "codex", "outcome": "nothing-to-do"},
        ],
        [
            {"run": "blocked-{}".format(i), "phase": "finish",
             "ts": NOW - (i + 1) * HOUR, "agent": "codex",
             "outcome": "skipped-blocked"}
            for i in range(7)
        ],
    ]
    for rows in cases:
        assert watchdog.assess("codex", rows, NOW) == []
        assert watchdog.assess("codex", rows, NOW, paused=True) == []


def _main_calls(monkeypatch, providers, rows_by_agent, paused):
    calls = []

    def fake_gh(*args):
        calls.append(args)
        joined = " ".join(args)
        if "issues?state=open" in joined:
            return json.dumps([])
        if args[1:3] == ("-X", "POST") and args[3].endswith("/issues"):
            return json.dumps({"number": 2035})
        if "/comments" in joined or args[1:3] == ("-X", "PATCH"):
            return "{}"
        raise AssertionError("unexpected gh call: {}".format(args))

    monkeypatch.setattr(watchdog.time, "time", lambda: NOW)
    monkeypatch.setattr(watchdog.heartbeat, "PROVIDERS", dict(providers))
    monkeypatch.setattr(watchdog.heartbeat, "RETIRED_AGENTS", frozenset())
    monkeypatch.setattr(
        watchdog.heartbeat, "paused_agents",
        lambda now=None: frozenset(paused))
    monkeypatch.setattr(
        watchdog, "records", lambda agent: rows_by_agent.get(agent, []))
    monkeypatch.setattr(watchdog, "existing_issue", lambda: {})
    monkeypatch.setattr(watchdog, "gh", fake_gh)
    assert watchdog.main() == 0
    creates = [call for call in calls
               if call[1:3] == ("-X", "POST") and call[3].endswith("/issues")]
    assert len(creates) == 1
    return next(
        argument[len("body="):]
        for argument in creates[0]
        if argument.startswith("body="))


def test_watchdog_main_skips_paused_silence_but_names_live_silence(monkeypatch):
    """End to end: paused codex silence stays out of the alert issue."""
    providers = {"codex": "openai", "muse": "meta"}
    rows = {"codex": _busy_then_quiet("codex"),
            "muse": _busy_then_quiet("muse")}
    body = _main_calls(monkeypatch, providers, rows, paused={"codex"})
    assert "`muse`: " in body and "Nothing recorded for 3d11h" in body
    assert "`codex`" not in body


def test_watchdog_main_alarms_paused_silence_once_the_flag_lifts(monkeypatch):
    """The same silence alarms for codex when nothing marks it paused."""
    providers = {"codex": "openai", "muse": "meta"}
    rows = {"codex": _busy_then_quiet("codex"),
            "muse": _busy_then_quiet("muse")}
    body = _main_calls(monkeypatch, providers, rows, paused=set())
    assert "`codex`: " in body and "`muse`: " in body


def test_watchdog_main_still_files_dying_runs_for_a_paused_lane(monkeypatch):
    """A paused lane that is actively dying is filed, not skipped."""
    providers = {"codex": "openai"}
    rows = {"codex": [row for run, ago in (("one", 180), ("two", 150),
                                           ("three", 130))
                      for row in _bound_open_start(run, ago)]}
    body = _main_calls(monkeypatch, providers, rows, paused={"codex"})
    assert "started and never finished" in body


def test_paused_param_defaults_off_so_existing_callers_are_unchanged():
    """The brief calls assess without paused; its silence reads hold."""
    import inspect

    assert inspect.signature(watchdog.assess).parameters["paused"].default is False
    rows = _busy_then_quiet("codex")
    assert watchdog.assess("codex", rows, NOW) != []
