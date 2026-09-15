"""A start whose PR merged under another run is closed, once (#497, ticket #506)."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from funnel import Item  # noqa: E402

REPO = "nateprich/example"
NOW = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def _project(number):
    return Item(repo=REPO, number=number, title="Project %d" % number,
                url="https://github.com/%s/issues/%d" % (REPO, number),
                state="OPEN", status="Building", klass="Broken", children_total=1)


def _ticket(number, parent, *, in_motion_since=None):
    return Item(repo=REPO, number=number, title="Ticket %d" % number,
                url="https://github.com/%s/issues/%d" % (REPO, number),
                state="OPEN", body="Risk: standard", parent=parent.ref,
                item_id="item-%d" % number,
                in_motion_since=in_motion_since)


def _wire(monkeypatch, spools, facts):
    monkeypatch.setattr(heartbeat, "PROVIDERS",
                        {"claude": "anthropic", "codex": "openai",
                         "muse": "meta", "zcode": "zai"})
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset({"zcode"}))
    monkeypatch.setattr(heartbeat, "read", lambda agent: list(spools.get(agent, [])))
    appended = []
    monkeypatch.setattr(heartbeat, "append",
                        lambda agent, record: appended.append((agent, record)) or "pushed")
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: facts)
    return appended


def _open_work_start(run, ref, ts=100, agent="codex"):
    return [{"run": run, "agent": agent, "phase": "start", "ts": ts},
            {"run": run, "agent": agent, "phase": "bind", "ts": ts + 1,
             "do": "ticket", "work": ref}]


def _merge_finish(run, pr, ts=900, agent="muse"):
    return [{"run": run, "agent": agent, "phase": "start", "ts": ts - 10},
            {"run": run, "agent": agent, "phase": "finish", "ts": ts,
             "outcome": "done", "merged": pr, "note": "merged PR #%d" % pr}]


def test_an_open_start_whose_pr_merged_elsewhere_is_closed_naming_that_run(monkeypatch):
    project = _project(1)
    ticket = _ticket(9, project)
    appended = _wire(
        monkeypatch,
        {"codex": _open_work_start("a", ticket.ref), "muse": _merge_finish("m1", 70)},
        {ticket.ref: {"number": 70, "state": "MERGED", "mergedAt": "2026-09-10T07:00:00Z"}},
    )

    closed = funnel.reconcile_orphaned_starts([project, ticket], NOW)

    assert [(c["run"], c["pr"], c["merged_under"]) for c in closed] == [("a", 70, "m1")]
    (agent, record), = appended
    assert agent == "codex" and record["run"] == "a" and record["phase"] == "finish"
    assert record["outcome"] == "done" and record["reconciled_from"] == "m1"
    assert "merged" not in record            # the merge is recorded once, on m1
    assert "run m1 (muse)" in record["note"]


def test_a_second_pass_writes_nothing(monkeypatch):
    project = _project(1)
    ticket = _ticket(9, project)
    finished = {"run": "a", "agent": "codex", "phase": "finish", "ts": 950,
                "outcome": "done", "reconciled_from": "m1"}
    appended = _wire(
        monkeypatch,
        {"codex": _open_work_start("a", ticket.ref) + [finished],
         "muse": _merge_finish("m1", 70)},
        {ticket.ref: {"number": 70, "state": "MERGED"}},
    )

    assert funnel.reconcile_orphaned_starts([project, ticket], NOW) == []
    assert appended == []


def test_a_start_whose_pr_is_still_open_is_left_for_the_watchdog(monkeypatch):
    project = _project(1)
    ticket = _ticket(9, project)
    appended = _wire(
        monkeypatch,
        {"codex": _open_work_start("a", ticket.ref)},
        {ticket.ref: {"number": 70, "state": "OPEN"}},
    )

    assert funnel.reconcile_orphaned_starts([project, ticket], NOW) == []
    assert appended == []


def test_a_merge_no_finish_record_claims_is_left_alone(monkeypatch):
    """Merged by hand, or by a run that died before its finish: the dead-run
    signal stays loud rather than being explained away."""
    project = _project(1)
    ticket = _ticket(9, project)
    appended = _wire(
        monkeypatch,
        {"codex": _open_work_start("a", ticket.ref)},
        {ticket.ref: {"number": 70, "state": "MERGED"}},
    )

    assert funnel.reconcile_orphaned_starts([project, ticket], NOW) == []
    assert appended == []


def test_the_run_that_merged_its_own_pr_is_not_an_orphan(monkeypatch):
    project = _project(1)
    ticket = _ticket(9, project)
    spool = _open_work_start("a", ticket.ref) + [
        {"run": "a", "agent": "codex", "phase": "finish", "ts": 900,
         "outcome": "done", "merged": 70}]
    appended = _wire(monkeypatch, {"codex": spool},
                     {ticket.ref: {"number": 70, "state": "MERGED"}})

    assert funnel.reconcile_orphaned_starts([project, ticket], NOW) == []
    assert appended == []


def test_no_candidates_means_no_pr_read(monkeypatch):
    project = _project(1)
    ticket = _ticket(9, project)
    reads = []
    _wire(monkeypatch, {"codex": []}, {})
    monkeypatch.setattr(funnel, "ticket_pr_facts",
                        lambda rows: reads.append(rows) or {})

    assert funnel.reconcile_orphaned_starts([project, ticket], NOW) == []
    assert reads == []


def test_reconcile_releases_a_bound_claim_with_no_branch_after_30_minutes(
    monkeypatch,
):
    project = _project(1)
    claimed_at = NOW - funnel.CLAIM_BRANCH_GRACE - timedelta(seconds=1)
    ticket = _ticket(9, project, in_motion_since=claimed_at)
    appended = _wire(
        monkeypatch,
        {"codex": _open_work_start(
            "abandoned", ticket.ref, ts=int(claimed_at.timestamp())
        )},
        {ticket.ref: None},
    )
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock",
        lambda item, value: writes.append((item.ref, value)),
    )

    result = funnel.reconcile_abandoned_claims(
        [project, ticket], NOW, pr_facts={ticket.ref: None}
    )

    assert writes == [(ticket.ref, "")]
    assert ticket.in_motion_since is None
    assert result == [{
        "run": "abandoned",
        "agent": "codex",
        "ref": ticket.ref,
        "result": "released",
        "kept": "pushed",
    }]
    (agent, finish), = appended
    assert agent == "codex"
    assert finish["run"] == "abandoned"
    assert finish["phase"] == "finish"
    assert finish["outcome"] == "errored"
    assert finish["reconciled_claim"] == ticket.ref
