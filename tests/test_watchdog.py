"""The watchdog's judgement, and the heartbeat's dead-run detection.

An alarm that fires on healthy behaviour trains its reader to ignore it, so the
tests here are as much about what must NOT be reported as what must.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "watchdog", ROOT / ".github" / "scripts" / "watchdog.py"
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

NOW = 1_788_600_000.0
HOUR = 3600


def start(run, ago_hours, ticket=1):
    return {"run": run, "phase": "start", "ts": NOW - ago_hours * HOUR,
            "agent": "codex", "ticket": ticket}


def finish(run, ago_hours, outcome="done", note=None):
    return {"run": run, "phase": "finish", "ts": NOW - ago_hours * HOUR,
            "agent": "codex", "outcome": outcome, "note": note}


# -- what must NOT be reported ---------------------------------------------


def test_being_over_pace_is_not_an_alarm():
    """It is the system working. Paging on it trains the alert to be ignored."""
    rows = [start("a", 1), finish("a", 1, "skipped-over-pace")]
    assert watchdog.assess("codex", rows, NOW) == []


def test_an_empty_funnel_is_not_an_alarm():
    rows = [start("a", 1), finish("a", 1, "nothing-to-do")]
    assert watchdog.assess("codex", rows, NOW) == []


def test_a_held_lock_is_not_an_alarm():
    rows = [start("a", 1), finish("a", 1, "skipped-locked")]
    assert watchdog.assess("codex", rows, NOW) == []


def test_one_dying_run_is_noise_not_an_alarm():
    """One is noise; three in a week means runs are dying.

    The trailing recent run keeps this isolated to the dying check — without it
    the fixture is also silent, and would trip that alarm instead.
    """
    rows = [start("a", 5), finish("a", 5), start("b", 4), start("c", 0.2)]
    assert watchdog.assess("codex", rows, NOW) == []


def test_a_run_still_in_flight_is_not_counted_as_dying():
    """It started twenty minutes ago. It is working."""
    rows = [start("a", 0.3)]
    assert watchdog.assess("codex", rows, NOW) == []


# -- what must be reported --------------------------------------------------


def test_silence_is_reported_because_a_closed_app_has_no_other_signal():
    rows = [start("a", 9), finish("a", 9)]
    problems = watchdog.assess("codex", rows, NOW)
    assert len(problems) == 1 and "recorded nothing" in problems[0]


def test_claude_is_allowed_to_be_quiet_far_longer_than_codex():
    rows = [start("a", 6), finish("a", 6)]
    assert watchdog.assess("codex", rows, NOW) != []
    assert watchdog.assess("claude", rows, NOW) == []


def test_three_unfinished_runs_are_reported_as_dying():
    """The rate-limit death signature — and the one condition a single outcome
    line could never detect."""
    rows = [start("a", 5, 11), start("b", 4, 12), start("c", 3, 13)]
    problems = watchdog.assess("codex", rows, NOW)
    assert any("started and never finished" in p for p in problems)
    assert any("11, 12, 13" in p for p in problems)


def test_the_dying_report_points_at_the_reserves():
    """The likely cause is a budget reserve set too low, so say so."""
    rows = [start(str(i), 5 - i, i) for i in range(3)]
    problems = watchdog.assess("codex", rows, NOW)
    assert any("usage.py" in p for p in problems)


def test_repeated_errors_are_reported_with_their_notes():
    rows = []
    for i in range(3):
        rows += [start(str(i), 5), finish(str(i), 5, "errored", "boom %d" % i)]
    problems = watchdog.assess("codex", rows, NOW)
    assert any("errored 3 times" in p for p in problems)
    assert any("boom 2" in p for p in problems)


def test_old_failures_age_out_of_the_weekly_count():
    rows = []
    for i in range(3):
        rows += [start(str(i), 24 * 30), finish(str(i), 24 * 30, "errored")]
    assert not any("errored" in p for p in watchdog.assess("codex", rows, NOW))


def test_never_having_run_does_not_file_an_issue():
    """It may simply not be scheduled yet. Filing an alarm before the thing
    exists is how a watchdog teaches its reader to ignore it."""
    assert watchdog.assess("codex", [], NOW) == []


def test_never_having_run_is_still_reported_in_the_log():
    """Silent about it in the issue tracker, not silent altogether."""
    assert "never recorded a run" in watchdog.note("codex", [])
    assert watchdog.note("codex", [start("a", 1)]) == ""


# -- the heartbeat's own view ----------------------------------------------


def test_unfinished_ignores_runs_that_completed():
    rows = [start("a", 5), finish("a", 5), start("b", 5)]
    assert [r["run"] for r in heartbeat.unfinished(rows, NOW, 2 * HOUR)] == ["b"]


def test_unfinished_respects_the_ttl():
    assert heartbeat.unfinished([start("a", 1)], NOW, 2 * HOUR) == []
    assert len(heartbeat.unfinished([start("a", 3)], NOW, 2 * HOUR)) == 1


def test_the_unfinished_ttl_matches_the_lock_ttl():
    """Both answer the same question — is this run still plausibly alive — so
    they must not drift apart."""
    import funnel

    assert watchdog.UNFINISHED_SECONDS == funnel.LOCK_TTL.total_seconds()
