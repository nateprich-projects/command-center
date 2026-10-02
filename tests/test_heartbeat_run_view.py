"""The per-run heartbeat view that open starts and API cost read through (#2174).

Plan #1750. A record that reached GitHub while its PUT reply was lost stays in
the spool too, so `heartbeat.read` returns it twice (#1225). Counting rows
rather than runs then reports two unfinished runs as four dying ones, and an
API cost that is null cannot say whether nothing was measured or something
could not be read.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
from agent_health import assess  # noqa: E402

NOW = 1_788_600_000.0
MIN = 60
HOUR = 3600


def start(run, at, agent="codex", **extra):
    record = {"run": run, "agent": agent, "phase": "start", "ts": int(at),
              "ticket": "t-" + run}
    record.update(extra)
    return record


def bind(run, at, agent="codex"):
    return {"run": run, "agent": agent, "phase": "bind", "ts": int(at),
            "do": "ticket",
            "work": "nateprich-projects/command-center#{}".format(run)}


def finish(run, at, outcome="done", agent="codex", **extra):
    record = {"run": run, "agent": agent, "phase": "finish", "ts": int(at),
              "outcome": outcome}
    record.update(extra)
    return record


def unresolved(at, candidates, agent="codex"):
    return {"run": None, "agent": agent, "phase": "finish", "ts": int(at),
            "outcome": "done", "unresolved": True, "candidates": candidates}


def api_event(run, at, agent="codex", **values):
    return {"run": run, "agent": agent, "phase": "api_cost", "ts": int(at),
            "api_cost": values}


def job(run, at, name, agent="codex"):
    return {"run": run, "agent": agent, "phase": "job", "ts": int(at),
            "job": name}


def read_twice(monkeypatch, agent, records):
    """Return `heartbeat.read` over records both on GitHub and still spooled.

    The shape #1225 measured: the PUT landed, its reply was lost, and the
    spool was never truncated. Nothing here is a hand-made duplicate; the
    second copy comes from the real spool reader.
    """
    content = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    monkeypatch.setattr(
        heartbeat, "_fetch", lambda _agent, *args, **kwargs: (content, "sha")
    )
    for record in records:
        heartbeat._spool(agent, record)
    rows = heartbeat.read(agent)
    assert len(rows) == 2 * len(records)
    return rows


# -- Accept: two bound unfinished runs read twice are two runs ----------------

def test_open_starts_reports_two_runs_read_twice_as_two(monkeypatch):
    rows = read_twice(monkeypatch, "codex", [
        start("one", NOW - 3 * HOUR), bind("one", NOW - 3 * HOUR + 5),
        start("two", NOW - 150 * MIN), bind("two", NOW - 150 * MIN + 5),
    ])

    assert [r["run"] for r in heartbeat.open_starts(rows)] == ["one", "two"]


def test_assess_reports_two_dying_runs_read_twice_as_two(monkeypatch):
    rows = read_twice(monkeypatch, "codex", [
        start("one", NOW - 3 * HOUR), bind("one", NOW - 3 * HOUR + 5),
        start("two", NOW - 150 * MIN), bind("two", NOW - 150 * MIN + 5),
    ])

    # Two dying runs are under the three-run alarm (main read four).
    assert [c for c in assess("codex", rows, NOW)
            if "never finished" in c] == []
    # With the bar at two, the condition counts runs: two, not four.
    dying = [c for c in assess("codex", rows, NOW, dying_threshold=2)
             if "never finished" in c]
    assert len(dying) == 1
    assert "has 2 runs this week" in dying[0]


def test_one_open_run_read_twice_resolves_and_is_closed_once(monkeypatch):
    rows = read_twice(monkeypatch, "codex", [
        start("one", NOW - 10 * MIN, session_id="s-1"),
        bind("one", NOW - 10 * MIN + 5),
    ])

    assert heartbeat.resolve_run(rows, None) == ("one", None)

    written = []
    monkeypatch.setattr(
        heartbeat, "append", lambda agent, record: written.append(record)
        or "pushed")
    closed = heartbeat.close_rebegun_starts("codex", rows, "fresh", "s-1")
    assert [c["run"] for c in closed] == ["one"]
    assert [w["run"] for w in written] == ["one"]


# -- Accept: API cost states --------------------------------------------------

def test_a_run_with_no_api_cost_event_is_missing_not_zero():
    view = heartbeat.run_view([start("a", NOW), finish("a", NOW + MIN)], "a")

    assert view["api_cost"] == {
        "graphql_points": {"state": "missing", "value": None},
        "gh_calls": {"state": "missing", "value": None},
    }


def test_events_summing_to_zero_are_measured_zero():
    view = heartbeat.run_view([
        start("a", NOW),
        api_event("a", NOW + 1, graphql_points=0, gh_calls=0),
        api_event("a", NOW + 2, graphql_points=0, gh_calls=0),
    ], "a")

    assert view["api_cost"] == {
        "graphql_points": {"state": "measured", "value": 0},
        "gh_calls": {"state": "measured", "value": 0},
    }


def test_one_null_field_is_lost_while_the_other_is_measured():
    view = heartbeat.run_view([
        start("a", NOW),
        api_event("a", NOW + 1, graphql_points=5, gh_calls=2),
        api_event("a", NOW + 2, graphql_points=None, gh_calls=3),
    ], "a")

    assert view["api_cost"] == {
        "graphql_points": {"state": "lost", "value": None},
        "gh_calls": {"state": "measured", "value": 5},
    }


def test_an_event_without_a_readable_cost_object_is_lost():
    unreadable = {"run": "a", "agent": "codex", "phase": "api_cost",
                  "ts": int(NOW), "api_cost": "garbled"}
    view = heartbeat.run_view([start("a", NOW), unreadable], "a")

    assert {name: field["state"] for name, field in view["api_cost"].items()} \
        == {"graphql_points": "lost", "gh_calls": "lost"}


def test_a_duplicated_event_counts_once(monkeypatch):
    rows = read_twice(monkeypatch, "codex", [
        start("a", NOW),
        api_event("a", NOW + 1, graphql_points=7, gh_calls=2),
    ])

    view = heartbeat.run_view(rows, "a")
    assert view["api_cost"] == {
        "graphql_points": {"state": "measured", "value": 7},
        "gh_calls": {"state": "measured", "value": 2},
    }
    assert len(view["api_cost_events"]) == 1


def test_finish_api_cost_value_is_unchanged_for_every_state():
    """The finish record keeps today's shape: a number, or null."""
    rows = [
        start("a", NOW),
        api_event("a", NOW + 1, graphql_points=None, gh_calls=3),
        start("b", NOW),
        api_event("b", NOW + 1, graphql_points=0, gh_calls=0),
        start("c", NOW),
    ]

    assert heartbeat.api_cost_for_run(rows, "a") == {
        "graphql_points": None, "gh_calls": 3}
    assert heartbeat.api_cost_for_run(rows, "b") == {
        "graphql_points": 0, "gh_calls": 0}
    assert heartbeat.api_cost_for_run(rows, "c") == {
        "graphql_points": None, "gh_calls": None}
    assert heartbeat.api_cost_for_run(rows, "unknown") == {
        "graphql_points": None, "gh_calls": None}
    assert heartbeat.api_cost_for_run(rows, None) == {
        "graphql_points": None, "gh_calls": None}


def test_events_with_and_without_a_caller_map_both_read():
    rows = [
        start("a", NOW),
        api_event("a", NOW + 1, graphql_points=4, gh_calls=1),
        dict(api_event("a", NOW + 2, graphql_points=6, gh_calls=2),
             graphql_by_caller={"standard": {"calls": 2, "points": 6,
                                             "remaining": 90,
                                             "readings": []}}),
    ]

    view = heartbeat.run_view(rows, "a")
    assert view["api_cost"]["graphql_points"] == {
        "state": "measured", "value": 10}
    assert view["api_cost"]["gh_calls"] == {"state": "measured", "value": 3}
    assert len(view["api_cost_events"]) == 2


# -- pairing ------------------------------------------------------------------

def test_pairing_names_open_finished_rebegun_and_unresolved_closes():
    rows = [
        start("open", NOW),
        start("done", NOW + 1), finish("done", NOW + 5 * MIN),
        start("rebegun", NOW + 2),
        finish("rebegun", NOW + 3 * MIN, outcome="skipped-blocked",
               re_begun_by="fresh", session_id="s-1"),
        start("fresh", NOW + 3 * MIN, session_id="s-1"),
        start("cleared", NOW - MIN),
        unresolved(NOW + 9 * MIN, ["cleared", "open"]),
        api_event("orphan", NOW + 1, graphql_points=1, gh_calls=1),
    ]

    views = heartbeat.run_views(rows)
    assert {run: view["pairing"] for run, view in views.items()} == {
        "cleared": "closed-unresolved",
        "open": "open",
        "done": "finished",
        "rebegun": "re-begun",
        "fresh": "open",
        "orphan": "no-start",
    }
    # Runs with a start come oldest first, as open_starts reads them.
    assert list(views)[:5] == ["cleared", "open", "done", "rebegun", "fresh"]
    assert views["rebegun"]["finish"]["re_begun_by"] == "fresh"
    assert views["cleared"]["finish"] is None
    assert views["cleared"]["closed_by"]["candidates"] == ["cleared", "open"]
    assert [r["run"] for r in heartbeat.open_starts(rows)] == ["open", "fresh"]


def test_a_rebegin_close_read_twice_still_pairs_once(monkeypatch):
    """`heartbeat start` closes the same session's earlier bound start."""
    rows = read_twice(monkeypatch, "codex", [
        start("old", NOW, session_id="s-1"), bind("old", NOW + 5),
        finish("old", NOW + MIN, outcome="skipped-blocked",
               re_begun_by="new", session_id="s-1"),
        start("new", NOW + MIN, session_id="s-1"),
    ])

    views = heartbeat.run_views(rows)
    assert views["old"]["pairing"] == "re-begun"
    assert views["old"]["binding"]["work"].endswith("#old")
    assert views["new"]["pairing"] == "open"
    assert [r["run"] for r in heartbeat.open_starts(rows)] == ["new"]


def test_a_duplicated_unresolved_finish_clears_one_candidate(monkeypatch):
    rows = read_twice(monkeypatch, "codex", [
        start("aaa", NOW), start("bbb", NOW + 3 * MIN),
        unresolved(NOW + 9 * MIN, ["aaa", "bbb"]),
    ])

    assert [r["run"] for r in heartbeat.open_starts(rows)] == ["bbb"]


def test_view_carries_binding_job_and_outcome():
    rows = [
        start("a", NOW), bind("a", NOW + 1), job("a", NOW + 2, "hourly"),
        finish("a", NOW + 5 * MIN, outcome="done"),
    ]

    view = heartbeat.run_view(rows, "a")
    assert view["agent"] == "codex"
    assert view["start"]["ts"] == int(NOW)
    assert view["binding"]["do"] == "ticket"
    assert view["job"] == "hourly"
    assert view["outcome"] == "done"
    assert view["error_class"] is None
    assert heartbeat.run_view(rows, "missing") is None


def test_an_old_errored_finish_takes_its_class_from_classify_error():
    note = "tests failed: 2 failed"
    runtime = {"root": "/x", "head": "abc123"}
    old = finish("old", NOW + MIN, outcome="errored", note=note,
                 runtime=runtime)
    recorded = finish("new", NOW + MIN, outcome="errored", note=note,
                      runtime=runtime, error_class="floor")

    views = heartbeat.run_views([start("old", NOW), old,
                                 start("new", NOW), recorded])
    assert "error_class" not in old
    assert views["old"]["error_class"] == heartbeat.classify_error(
        note, runtime) == "regression"
    # A recorded class is read as written, never recomputed or edited.
    assert views["new"]["error_class"] == "floor"
    assert recorded["error_class"] == "floor"


def test_non_object_rows_are_not_runs():
    rows = [start("a", NOW), "not a record", ["also", "not"], None]

    assert [r["run"] for r in heartbeat.open_starts(rows)] == ["a"]


@pytest.mark.parametrize("phase", ["start", "finish", "api_cost", "bind"])
def test_records_without_a_run_id_are_not_runs(phase):
    rows = [{"agent": "codex", "phase": phase, "ts": int(NOW)}]

    assert heartbeat.run_views(rows) == {}
