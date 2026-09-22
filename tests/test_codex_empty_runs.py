"""The Codex standard lane's empty-run share, the cadence signal (#1320)."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


NOW = 1_790_100_000.0
HOUR = 3600


def _run(run, outcome, *, tier="standard", age=HOUR, phase="finish"):
    """One run's start and its terminal record, ``age`` seconds ago."""
    start = {"run": run, "agent": "codex", "phase": "start",
             "ts": NOW - age - 60}
    if tier is not None:
        start["tier"] = tier
    end = {"run": run, "agent": "codex", "phase": phase,
           "ts": NOW - age, "outcome": outcome}
    return [start, end]


_LANES = iter(range(10_000))


def _lane(worked, empty, **kwargs):
    """Runs with ids unique across calls, so two lanes never share a run."""
    lane = next(_LANES)
    records = []
    for index in range(worked):
        records += _run("L{}w{}".format(lane, index), "done", **kwargs)
    for index in range(empty):
        records += _run("L{}e{}".format(lane, index), "nothing-to-do",
                        **kwargs)
    return records


# --- the share ------------------------------------------------------------


def test_the_share_counts_standard_runs_that_found_nothing():
    stats = funnel.codex_empty_run_share(_lane(6, 4), NOW)

    assert stats == {"runs": 10, "empty": 4, "share": 0.4}


def test_escalated_runs_are_not_the_standard_lane():
    """The escalated automation fires hourly and mostly finds nothing; its
    empties would push the standard lane toward a slowdown it has not
    earned."""
    records = _lane(6, 2) + _lane(0, 9, tier="escalated")

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 8


def test_starts_without_a_tier_are_left_out_not_guessed():
    """Every start written before #1320 has no tier."""
    records = _lane(3, 1) + _lane(5, 5, tier=None)

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 4


@pytest.mark.parametrize("outcome", sorted(funnel.CODEX_GATE_SKIPS))
def test_a_gate_skip_is_not_an_empty_run(outcome):
    """A braked lane is not an idle one: these runs never read a queue."""
    records = _lane(2, 1) + _run("gated", outcome)

    assert funnel.codex_empty_run_share(records, NOW) == {
        "runs": 3, "empty": 1, "share": pytest.approx(1 / 3)}


@pytest.mark.parametrize("outcome", [
    "skipped-locked", "skipped-blocked", "skipped-human-step", "errored"])
def test_runs_that_reached_the_queue_count_as_not_empty(outcome):
    records = _run("other", outcome)

    assert funnel.codex_empty_run_share(records, NOW) == {
        "runs": 1, "empty": 0, "share": 0.0}


def test_only_the_last_24_hours_count():
    records = _lane(1, 1) + _lane(0, 5, age=25 * HOUR)

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 2


def test_an_event_is_not_a_finish():
    """An event is a note on a run that is still open."""
    records = _lane(2, 0) + _run("noted", "nothing-to-do", phase="event")

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 2


@pytest.mark.parametrize("stamp", [None, "yesterday", True])
def test_a_finish_without_a_usable_timestamp_is_skipped(stamp):
    records = _run("odd", "nothing-to-do")
    records[1]["ts"] = stamp

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 0


def test_no_runs_is_no_share():
    assert funnel.codex_empty_run_share([], NOW) == {
        "runs": 0, "empty": 0, "share": None}


# --- the doctor row -------------------------------------------------------


def test_a_mostly_worked_lane_passes():
    check = funnel.check_codex_empty_runs(_lane(10, 4), now=NOW)

    assert check.ok is True
    assert check.name == "codex empty runs"
    assert "4 of 14 standard-lane runs" in check.found
    assert check.fix == ""


def test_half_or_more_empty_says_slow_the_lane():
    """Nate's instruction: keep ten minutes until the backlog starts
    clearing (#1315). Half the runs finding nothing is that point."""
    check = funnel.check_codex_empty_runs(_lane(6, 6), now=NOW)

    assert check.ok is False
    assert "6 of 12" in check.found
    assert "50%" in check.found
    assert check.fix == funnel.CODEX_EMPTY_RUNS_FIX
    assert "20 minutes" in check.fix


def test_a_thin_window_passes_with_a_note():
    """Too few runs to judge, including while the automations are paused."""
    check = funnel.check_codex_empty_runs(_lane(0, 11), now=NOW)

    assert check.ok is True
    assert "too few" in check.found


def test_budget_skips_do_not_make_a_braked_lane_look_idle():
    records = _lane(2, 2)
    for index in range(40):
        records += _run("brake{}".format(index), "skipped-over-pace")

    check = funnel.check_codex_empty_runs(records, now=NOW)

    assert check.ok is True
    assert "too few" in check.found


def test_an_unreadable_heartbeat_passes_with_a_note(monkeypatch):
    """A cadence signal, not a health check: the heartbeat's own row
    reports whether it can be read."""
    def unreadable(agent):
        raise heartbeat.HeartbeatError("offline")

    monkeypatch.setattr(heartbeat, "read", unreadable)

    check = funnel.check_codex_empty_runs(now=NOW)

    assert check.ok is True
    assert "could not be read" in check.found


# --- the tier on the start record -------------------------------------------


def _capture_start(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "runtime_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "append",
                        lambda agent, record: records.append(record) or "spooled")
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)
    return records


@pytest.mark.parametrize("tier", ["standard", "escalated"])
def test_a_start_records_its_tier(monkeypatch, capsys, tier):
    records = _capture_start(monkeypatch)

    assert heartbeat.main(["start", "--agent", "codex", "--tier", tier]) == 0

    assert records[0]["tier"] == tier


def test_a_start_without_a_tier_records_none(monkeypatch, capsys):
    records = _capture_start(monkeypatch)

    assert heartbeat.main(["start", "--agent", "codex"]) == 0

    assert records[0]["tier"] is None


def test_an_unknown_tier_is_refused(monkeypatch, capsys):
    _capture_start(monkeypatch)

    with pytest.raises(SystemExit):
        heartbeat.main(["start", "--agent", "codex", "--tier", "urgent"])


def _capture_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(
        funnel, "_run_bounded_subprocess",
        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(
            returncode=0, stdout="run-id\n", stderr=""))
    return calls


@pytest.mark.parametrize("tier", ["standard", "escalated"])
def test_begin_passes_its_tier_to_the_heartbeat(monkeypatch, tier):
    calls = _capture_argv(monkeypatch)

    assert funnel._start_begin_heartbeat("codex", tier) == "run-id"

    assert calls[0][-4:] == ["--agent", "codex", "--tier", tier]


@pytest.mark.parametrize("tier", [None, "", "urgent"])
def test_begin_passes_no_tier_it_does_not_know(monkeypatch, tier):
    calls = _capture_argv(monkeypatch)

    funnel._start_begin_heartbeat("codex", tier)

    assert "--tier" not in calls[0]


def test_the_preflight_hands_the_tier_through(monkeypatch):
    seen = []
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent, tier=None: seen.append(tier) or "run-id")
    import usage

    monkeypatch.setattr(usage, "read_agent", lambda *args: None)

    out, reading = funnel._begin_preflight(
        __import__("datetime").datetime(2026, 9, 22), "codex", False,
        "standard")

    assert seen == ["standard"]
    assert reading is None and out["gate"] == "unknown"


def test_a_one_argument_heartbeat_double_still_works(monkeypatch):
    """Dozens of existing tests stub the seam as `lambda agent: ...`."""
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent: "run-id")
    import usage

    monkeypatch.setattr(usage, "read_agent", lambda *args: None)

    out, _ = funnel._begin_preflight(
        __import__("datetime").datetime(2026, 9, 22), "codex", False,
        "standard")

    assert out["run"] == "run-id"
