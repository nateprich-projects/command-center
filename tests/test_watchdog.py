"""The watchdog's judgement, and the heartbeat's dead-run detection.

An alarm that fires on healthy behaviour trains its reader to ignore it, so the
tests here are as much about what must NOT be reported as what must.
"""

from __future__ import annotations

import importlib.util
import json
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


def event(run, ago_hours, outcome):
    return {"run": run, "phase": "event", "ts": NOW - ago_hours * HOUR,
            "agent": "codex", "outcome": outcome}


def history(gaps_hours, quiet_hours):
    """Return one heartbeat record per timestamp, in chronological order."""
    latest = NOW - quiet_hours * HOUR
    timestamps = [latest]
    for gap in reversed(gaps_hours):
        timestamps.append(timestamps[-1] - gap * HOUR)
    timestamps.reverse()
    return [
        {"run": str(i), "phase": "finish", "ts": timestamp,
         "agent": "codex", "outcome": "done"}
        for i, timestamp in enumerate(timestamps)
    ]


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


def test_nate_active_and_unknown_usage_skips_are_not_alarms():
    rows = [
        finish("a", 1, "skipped-nate-active"),
        finish("b", 1, "skipped-usage-unknown"),
    ]
    assert watchdog.assess("codex", rows, NOW) == []


def test_a_week_of_blocked_skips_is_healthy_but_three_errors_alarm():
    skipped = [
        finish("blocked-{}".format(i), i + 1, "skipped-blocked")
        for i in range(7)
    ]
    assert watchdog.assess("codex", skipped, NOW) == []

    errored = [
        finish("error-{}".format(i), i + 1, "errored", "boom {}".format(i))
        for i in range(watchdog.ERROR_THRESHOLD)
    ]
    problems = watchdog.assess("codex", errored, NOW)
    assert any("errored 3 times" in problem for problem in problems)


def test_a_human_step_skip_is_healthy():
    rows = [finish("human-step", 1, "skipped-human-step")]
    assert watchdog.assess("codex", rows, NOW) == []


def test_prompt_drift_is_reported_as_a_fault():
    problems = watchdog.assess("codex", [event("a", 1, "prompt-drift")], NOW)
    assert len(problems) == 1
    assert "reported prompt drift 1 time(s)" in problems[0]
    assert "checked-in file" in problems[0]


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


def test_silence_reports_the_observed_gap_without_guessing_a_cause():
    rows = history([1] * 8, quiet_hours=6)
    problems = watchdog.assess("codex", rows, NOW)
    assert len(problems) == 1
    assert "normal gap 1h" in problems[0]
    assert "p90 over 14 days, 9 records" in problems[0]
    assert "Nothing recorded for 6h" in problems[0]
    assert "6x normal" in problems[0]
    assert "alarm threshold 5x normal" in problems[0]
    assert "last at <t:" in problems[0]
    assert "Mac mini" not in problems[0]
    assert "probably" not in problems[0]


def test_steady_rhythm_under_threshold_is_not_an_alarm():
    rows = history([1, 1, 1, 1], quiet_hours=1)
    assert watchdog.assess("codex", rows, NOW) == []


def test_a_tight_rhythm_alarms_where_a_loose_rhythm_does_not():
    tight = history([0.25] * 8, quiet_hours=2)
    loose = history([6] * 8, quiet_hours=2)
    assert watchdog.assess("codex", tight, NOW) != []
    assert watchdog.assess("claude", loose, NOW) == []


def test_recurring_long_gaps_are_absorbed_by_the_high_percentile():
    rows = history([1] * 16 + [8] * 4, quiet_hours=8)
    assert watchdog.assess("claude", rows, NOW) == []


def test_thin_history_declines_to_alarm_and_prints_a_note():
    rows = history([1], quiet_hours=1)
    assert watchdog.assess("codex", rows, NOW) == []
    message = watchdog.note("codex", rows, NOW)
    assert "only 1 gap(s)" in message
    assert "silence threshold not inferred" in message


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
    assert "only 0 gap(s)" in watchdog.note("codex", [start("a", 1)], NOW)


def test_main_watches_every_registered_provider(monkeypatch):
    """A new heartbeat provider must not be silently left out of coverage."""
    providers = {
        "claude": "anthropic",
        "codex": "openai",
        "zcode": "zai",
        "future": "new-pool",
    }
    seen = []
    monkeypatch.setattr(watchdog.heartbeat, "PROVIDERS", providers)
    monkeypatch.setattr(
        watchdog, "records", lambda agent: seen.append(agent) or []
    )
    monkeypatch.setattr(watchdog, "existing_issue", lambda: {})

    assert watchdog.main() == 0
    assert seen == sorted(providers)


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


# -- instrumentation must not gate the thing it instruments -----------------


def _isolate_spool(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(heartbeat, "BACKOFF", [])          # no sleeping in tests
    monkeypatch.setattr(heartbeat, "_ensure_branch", lambda: None)


def _offline(monkeypatch):
    def boom(*a, **k):
        raise heartbeat.HeartbeatError("GitHub unreachable")
    monkeypatch.setattr(heartbeat, "gh", boom)


def test_a_github_outage_does_not_stop_a_run(tmp_path, monkeypatch):
    """This is the bug it was written for: a blip made `heartbeat start` raise,
    the run stopped at step one, and the thing that failed *was* the record — so
    it left no trace of having stopped."""
    _isolate_spool(tmp_path, monkeypatch); _offline(monkeypatch)
    assert heartbeat.append(
        "codex", {"run": "a", "phase": "start", "ts": 1}) == "spooled"


def test_a_record_survives_the_outage_locally(tmp_path, monkeypatch):
    _isolate_spool(tmp_path, monkeypatch); _offline(monkeypatch)
    heartbeat.append("codex", {"run": "a", "phase": "start", "ts": 1})
    assert [r["run"] for r in heartbeat._spooled("codex")] == ["a"]


def test_the_spool_drains_when_github_returns(tmp_path, monkeypatch):
    _isolate_spool(tmp_path, monkeypatch); _offline(monkeypatch)
    heartbeat.append("codex", {"run": "a", "phase": "start", "ts": 1})
    heartbeat.append("codex", {"run": "b", "phase": "finish", "ts": 2})
    assert len(heartbeat._spooled("codex")) == 2

    sent = {}
    monkeypatch.setattr(heartbeat, "_fetch", lambda agent: (None, None))
    def ok(*args, **k):
        sent["args"] = args
        return "{}"
    monkeypatch.setattr(heartbeat, "gh", ok)

    assert heartbeat.append(
        "codex", {"run": "c", "phase": "start", "ts": 3}) == "pushed"
    assert heartbeat._spooled("codex") == []
    body = [a for a in sent["args"] if a.startswith("content=")][0]
    import base64
    text = base64.b64decode(body.split("=", 1)[1]).decode()
    assert [json.loads(l)["run"] for l in text.splitlines()] == ["a", "b", "c"]


def test_read_includes_records_not_yet_pushed(tmp_path, monkeypatch):
    """A local reader must see the whole picture, not just what GitHub has."""
    _isolate_spool(tmp_path, monkeypatch); _offline(monkeypatch)
    heartbeat.append("codex", {"run": "a", "phase": "start", "ts": 1})
    monkeypatch.setattr(heartbeat, "_fetch", lambda agent: (None, None))
    assert [r["run"] for r in heartbeat.read("codex")] == ["a"]


def test_a_spool_write_failure_is_reported_as_lost_not_saved(tmp_path, monkeypatch):
    """Even the fallback failing must not take the run down — but it must not be
    called saved either.

    Two Codex runs on 2026-09-06 reported "spooled locally" while writing nothing
    anywhere: the sandbox denied writes outside its working directory, and the old
    boolean could not tell "on disk, awaiting a drain" from "gone". A gap in the
    record that is believed to be safe looks exactly like a run that never
    happened, which is the one state the heartbeat exists to rule out.
    """
    monkeypatch.setattr(heartbeat, "SPOOL_DIR", "/proc/nonexistent/nope")
    monkeypatch.setattr(heartbeat, "BACKOFF", [])
    _offline(monkeypatch)
    assert heartbeat.append(
        "codex", {"run": "a", "phase": "start", "ts": 1}) == "lost"


def test_an_unspoolable_record_goes_straight_to_github(tmp_path, monkeypatch):
    """The spool is a write-ahead buffer, not a prerequisite.

    Codex's sandbox denied writes to the spool on 2026-09-06 while GitHub was
    perfectly reachable, and two records were lost for no reason. When the buffer
    fails and the destination is up, use the destination.
    """
    sent = {}

    def ok(*args, **k):
        sent["args"] = args
        return "{}"

    monkeypatch.setattr(heartbeat, "SPOOL_DIR", "/proc/nonexistent/nope")
    monkeypatch.setattr(heartbeat, "_fetch", lambda agent: (None, None))
    monkeypatch.setattr(heartbeat, "gh", ok)
    assert heartbeat.append(
        "codex", {"run": "a", "phase": "start", "ts": 1}) == "pushed"

    import base64
    body = [a for a in sent["args"] if a.startswith("content=")][0]
    text = base64.b64decode(body.split("=", 1)[1]).decode()
    assert [json.loads(l)["run"] for l in text.splitlines()] == ["a"]


def test_a_record_is_lost_only_when_both_routes_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "SPOOL_DIR", "/proc/nonexistent/nope")
    monkeypatch.setattr(heartbeat, "BACKOFF", [])
    _offline(monkeypatch)
    assert heartbeat.append(
        "codex", {"run": "a", "phase": "start", "ts": 1}) == "lost"


def test_an_api_reserve_decline_is_not_an_alarm():
    """Coasting to a stop on budget is the design working, not a fault (#273)."""
    rows = [start("a", 1), finish("a", 1, "skipped-api-reserve")]
    assert watchdog.assess("codex", rows, NOW) == []
