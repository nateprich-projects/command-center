"""A finish is filed under the run that was issued the work (#497, ticket #505).

Run 2593a91ac8f2 began, finished nothing-to-do, and the same id was later used
for ticket work; the explicit `done` finish was refused only because the stale
id happened to be finished first. `begin` now binds the work it issues to the
run, and `finish` checks the binding instead of trusting a bare id.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


def _start(run, agent="codex", ts=100):
    return {"run": run, "agent": agent, "phase": "start", "ts": ts}


def _bind(run, do, work, agent="codex", ts=101):
    return {"run": run, "agent": agent, "phase": "bind", "ts": ts,
            "do": do, "work": work}


def _finish(run, agent="codex", ts=200, outcome="done"):
    return {"run": run, "agent": agent, "phase": "finish", "ts": ts,
            "outcome": outcome}


@pytest.fixture
def spool(monkeypatch):
    """Records the heartbeat would read, and everything it appends."""
    state = {"records": [], "appended": []}
    monkeypatch.setattr(heartbeat, "read", lambda agent: list(state["records"]))
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: state["appended"].append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "runtime_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    return state


def test_record_binding_writes_its_own_phase(spool):
    assert heartbeat.record_binding("codex", "r1", "ticket", "o/r#9") == "spooled"
    (record,) = spool["appended"]
    assert record["phase"] == "bind"
    assert (record["run"], record["do"], record["work"]) == ("r1", "ticket", "o/r#9")
    assert heartbeat.bindings([record]) == {
        "r1": {"do": "ticket", "work": "o/r#9", "ts": record["ts"]},
    }


def test_bind_records_are_not_starts_or_finishes(spool):
    records = [_start("r1"), _bind("r1", "ticket", "o/r#9")]
    assert [r["run"] for r in heartbeat.open_starts(records)] == ["r1"]
    assert heartbeat.resolve_run(records, None) == ("r1", None)


def test_the_run_that_was_issued_the_work_finishes_as_before(spool, capsys):
    spool["records"] = [_start("a"), _bind("a", "review", "96")]
    assert heartbeat.main([
        "finish", "--agent", "muse", "--run", "a", "--outcome", "done",
        "--merged", "96",
    ]) == 0
    (record,) = spool["appended"]
    assert record["phase"] == "finish" and record["run"] == "a"
    assert record["merged"] == 96


def test_a_finish_naming_another_runs_work_is_refused_and_filed_on_that_run(
        spool, capsys):
    spool["records"] = [
        _start("a", ts=100), _bind("a", "ticket", "o/r#9", ts=101),
        _start("b", ts=110), _bind("b", "review", "96", ts=111),
    ]
    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "a", "--outcome", "done",
        "--merged", "96", "--note", "merged PR #96",
    ]) == 2
    err = capsys.readouterr().err
    assert "run a was issued ticket o/r#9, not a merge of PR 96" in err
    assert "Use --run b" in err

    (event,) = spool["appended"]
    assert event["phase"] == "event" and event["run"] == "b"
    assert event["outcome"] == "done" and event["misfiled_from"] == "a"
    assert event["merged"] == 96 and event["note"] == "merged PR #96"
    # No finish was written for either run.
    assert not [r for r in spool["appended"] if r["phase"] == "finish"]


def test_the_work_flag_is_checked_against_the_binding(spool, capsys):
    spool["records"] = [_start("a"), _bind("a", "ticket", "o/r#9")]
    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "a", "--outcome", "done",
        "--work", "o/r#12",
    ]) == 2
    err = capsys.readouterr().err
    assert "not o/r#12" in err and "unattributed event" in err
    (event,) = spool["appended"]
    assert event["run"] is None and event["candidates"] == ["a"]
    assert event["work"] == "o/r#12" and event["misfiled_from"] == "a"

    spool["appended"].clear()
    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "a", "--outcome", "done",
        "--work", "o/r#9",
    ]) == 0
    assert spool["appended"][0]["phase"] == "finish"


def test_a_run_with_no_binding_is_trusted(spool):
    """A stop run, or a start from before bindings existed: nothing to check."""
    spool["records"] = [_start("a")]
    assert heartbeat.main([
        "finish", "--agent", "muse", "--run", "a", "--outcome", "done",
        "--merged", "5",
    ]) == 0
    assert spool["appended"][0]["phase"] == "finish"


def test_a_second_finish_is_still_refused(spool, capsys):
    spool["records"] = [_start("a"), _bind("a", "review", "96"), _finish("a")]
    assert heartbeat.main([
        "finish", "--agent", "muse", "--run", "a", "--outcome", "done",
        "--merged", "96",
    ]) == 2
    assert "already finished" in capsys.readouterr().err
    assert spool["appended"] == []


def test_two_overlapping_starts_each_finish_under_their_own_id(spool):
    spool["records"] = [
        _start("a", ts=100), _bind("a", "review", "96", ts=101),
        _start("b", ts=110), _bind("b", "review", "97", ts=111),
    ]
    assert heartbeat.main(["finish", "--agent", "muse", "--run", "b",
                           "--outcome", "done", "--merged", "97"]) == 0
    assert heartbeat.main(["finish", "--agent", "muse", "--run", "a",
                           "--outcome", "done", "--merged", "96"]) == 0
    assert [r["run"] for r in spool["appended"]] == ["b", "a"]


def test_an_omitted_id_with_one_open_start_still_resolves(spool):
    spool["records"] = [_start("a"), _bind("a", "ticket", "o/r#9")]
    assert heartbeat.main(["finish", "--agent", "codex",
                           "--outcome", "nothing-to-do"]) == 0
    assert spool["appended"][0]["run"] == "a"


def test_begin_binds_the_work_it_issues(monkeypatch):
    bound = []
    monkeypatch.setattr(heartbeat, "record_binding",
                        lambda agent, run, do, work: bound.append((agent, run, do, work)) or "spooled")
    out = {"run": "r1", "do": "ticket", "work": {"ref": "o/r#9", "title": "t"}}
    funnel._bind_run("codex", out)
    assert bound == [("codex", "r1", "ticket", "o/r#9")]
    assert out["bound"] == {"do": "ticket", "work": "o/r#9"}

    review = {"run": "r2", "do": "review", "work": {"pr": 96, "ref": "o/r#9"}}
    funnel._bind_run("muse", review)
    assert bound[-1] == ("muse", "r2", "review", "96")

    stop = {"run": "r3", "do": "stop", "why": "nothing to do"}
    funnel._bind_run("codex", stop)
    assert len(bound) == 2 and "bound" not in stop


def test_a_binding_that_cannot_be_written_does_not_stop_the_run(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("spool unwritable")
    monkeypatch.setattr(heartbeat, "record_binding", boom)
    out = {"run": "r1", "do": "shape", "work": {"ref": "o/r#3"}}
    funnel._bind_run("muse", out)
    assert out["bound"] == {"do": "shape", "work": "o/r#3"}
