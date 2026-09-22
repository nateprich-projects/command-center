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


def _start(run, age, tier="standard"):
    record = {"run": run, "agent": "codex", "phase": "start", "ts": NOW - age}
    if tier is not None:
        record["tier"] = tier
    return record


def _bind(run, age):
    """`begin` bound a ticket to the run: it worked."""
    return {"run": run, "agent": "codex", "phase": "bind", "do": "ticket",
            "work": "nateprich-projects/command-center#1", "ts": NOW - age + 30}


def _empty(run, age):
    """`begin` found no work waiting for the tier and said so itself."""
    return {"run": run, "agent": "codex", "phase": "event",
            "outcome": "nothing-to-do", "queue": "empty", "tier": "standard",
            "ts": NOW - age + 30}


def _finish(run, age, outcome):
    return {"run": run, "agent": "codex", "phase": "finish",
            "outcome": outcome, "ts": NOW - age + 60}


_LANES = iter(range(10_000))


def _lane(worked, empty, *, gap=600, tier="standard", first_age=HOUR):
    """Runs with ids unique across calls, starting ``gap`` seconds apart."""
    lane = next(_LANES)
    records = []
    age = first_age
    for index in range(worked):
        run = "L{}w{}".format(lane, index)
        records += [_start(run, age, tier), _bind(run, age),
                    _finish(run, age, "done")]
        age += gap
    for index in range(empty):
        run = "L{}e{}".format(lane, index)
        records += [_start(run, age, tier), _empty(run, age),
                    _finish(run, age, "nothing-to-do")]
        age += gap
    return records


# --- the share ------------------------------------------------------------


def test_the_share_counts_standard_runs_that_found_the_queue_empty():
    stats = funnel.codex_empty_run_share(_lane(6, 4), NOW)

    assert stats["runs"] == 10
    assert stats["empty"] == 4
    assert stats["share"] == 0.4
    assert stats["cadence_seconds"] == 600


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


@pytest.mark.parametrize("outcome", [
    "nothing-to-do", "skipped-over-pace", "skipped-api-reserve",
    "skipped-usage-unknown", "errored", "config-drift",
])
def test_a_finish_alone_says_nothing_about_the_queue(outcome):
    """The routine files a GitHub failure, a held lock and the WIP cap as
    `nothing-to-do` too (#1216: 160 of them in one outage). Only begin's own
    records say whether there was work."""
    records = _lane(2, 1) + [_start("gated", HOUR), _finish(
        "gated", HOUR, outcome)]

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 3


def test_an_event_that_is_not_an_empty_queue_is_left_out():
    reserve = {"run": "r", "agent": "codex", "phase": "event",
               "outcome": "skipped-api-reserve", "ts": NOW - HOUR}
    records = _lane(2, 1) + [_start("r", HOUR), reserve]

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 3


def test_it_counts_runs_not_rows():
    """`heartbeat.one_record_per_run` is the canonical counting rule; a
    run can carry the same record several times."""
    records = _lane(1, 1)
    tripled = records + records + records + [dict(record, ts=record["ts"] + 1)
                                             for record in records]

    stats = funnel.codex_empty_run_share(tripled, NOW)

    assert (stats["runs"], stats["empty"], stats["share"]) == (2, 1, 0.5)


def test_a_run_that_was_bound_is_worked_whatever_else_it_recorded():
    records = [_start("r", HOUR), _bind("r", HOUR), _empty("r", HOUR)]

    assert funnel.codex_empty_run_share(records, NOW)["empty"] == 0


def test_only_the_last_24_hours_count():
    records = _lane(1, 1) + _lane(0, 5, first_age=25 * HOUR)

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 2


@pytest.mark.parametrize("stamp", [None, "yesterday", True])
def test_a_record_without_a_usable_timestamp_is_skipped(stamp):
    records = [_start("odd", HOUR), dict(_empty("odd", HOUR), ts=stamp)]

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 0


def test_records_that_are_not_objects_are_skipped():
    """A `null` line in the heartbeat file must not abort the doctor."""
    records = [None, "text", 7] + _lane(1, 1)

    assert funnel.codex_empty_run_share(records, NOW)["runs"] == 2


def test_no_runs_is_no_share():
    stats = funnel.codex_empty_run_share([], NOW)

    assert (stats["runs"], stats["empty"], stats["share"],
            stats["cadence_seconds"]) == (0, 0, None, None)


# --- the doctor row -------------------------------------------------------


def test_a_mostly_worked_lane_passes():
    check = funnel.check_codex_empty_runs(_lane(10, 4), now=NOW)

    assert check.ok is True
    assert check.name == "codex empty runs"
    assert "4 of 14 standard-lane runs" in check.found
    assert "starts every 10 min" in check.found
    assert check.fix == ""


def test_half_or_more_empty_at_ten_minutes_says_slow_the_lane():
    """Nate's instruction: keep ten minutes until the backlog starts
    clearing (#1315). Half the runs finding nothing is that point."""
    check = funnel.check_codex_empty_runs(_lane(6, 6), now=NOW)

    assert check.ok is False
    assert "6 of 12" in check.found
    assert "50%" in check.found
    assert check.fix == funnel.CODEX_EMPTY_RUNS_FIX
    assert "20 minutes" in check.fix


def test_the_row_stops_asking_once_the_lane_is_slower():
    """After the fix the share can stay high with nothing left to change;
    a row that failed forever would train everyone to ignore it."""
    check = funnel.check_codex_empty_runs(_lane(4, 10, gap=1200), now=NOW)

    assert check.ok is True
    assert "already at the slower cadence" in check.found
    assert check.fix == ""


def test_a_thin_window_passes_with_a_note():
    """Too few runs to judge, including while the automations are paused."""
    check = funnel.check_codex_empty_runs(_lane(0, 11), now=NOW)

    assert check.ok is True
    assert "too few" in check.found


def test_budget_stops_do_not_make_a_braked_lane_look_idle():
    records = _lane(2, 2)
    for index in range(40):
        run = "brake{}".format(index)
        records += [_start(run, HOUR), _finish(run, HOUR, "skipped-over-pace")]

    check = funnel.check_codex_empty_runs(records, now=NOW)

    assert check.ok is True
    assert "too few" in check.found


def test_an_unreadable_heartbeat_passes_with_a_note(monkeypatch):
    """A cadence signal, not a health check: the heartbeat's own row
    reports whether it can be read."""
    def unreadable(agent, timeout=None):
        raise heartbeat.HeartbeatError("offline")

    monkeypatch.setattr(heartbeat, "read", unreadable)

    check = funnel.check_codex_empty_runs(now=NOW)

    assert check.ok is True
    assert "could not be read" in check.found


def test_the_heartbeat_read_is_bounded(monkeypatch):
    seen = {}

    def read(agent, timeout=None):
        seen.update(agent=agent, timeout=timeout)
        return []

    monkeypatch.setattr(heartbeat, "read", read)

    funnel.check_codex_empty_runs(now=NOW)

    assert seen == {"agent": "codex",
                    "timeout": funnel.CODEX_EMPTY_READ_TIMEOUT}


def test_a_one_argument_reader_double_still_works(monkeypatch):
    monkeypatch.setattr(heartbeat, "read", lambda agent: _lane(9, 3))

    assert funnel.check_codex_empty_runs(now=NOW).ok is True


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
