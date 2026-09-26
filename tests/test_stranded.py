"""Derived stranded-work diagnostics for the funnel brief."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
REPO = "owner/repo"


def issue(number, **kwargs):
    values = {
        "repo": REPO,
        "number": number,
        "title": "issue {}".format(number),
        "url": "https://example.invalid/{}".format(number),
        "state": "OPEN",
        "origin": "agent",
        "risk": "standard",
        "needs": "none",
    }
    values.update(kwargs)
    return Item(**values)


def test_stranded_json_reports_the_four_first_release_shapes():
    conflicting = issue(
        1,
        parent="{}#10".format(REPO),
    )
    stale = issue(
        2,
        parent="{}#10".format(REPO),
        in_motion_since=NOW - funnel.LOCK_TTL - timedelta(minutes=1),
    )
    childless = issue(3, status="Building")
    dead_dependency = issue(
        4,
        parent="{}#10".format(REPO),
        dead_blockers=["other/repo#99"],
    )

    rows = funnel.stranded_json(
        [conflicting, stale, childless, dead_dependency],
        NOW,
        pr_facts={
            conflicting.ref: {
                "state": "OPEN",
                "mergeable": "CONFLICTING",
                "headRefOid": "head-sha",
                "verdict": {"verdict": "approved", "head_sha": "head-sha"},
            },
            stale.ref: None,
            dead_dependency.ref: None,
        },
    )

    assert rows == [
        {
            "ref": conflicting.ref,
            "title": "issue 1",
            "url": "https://example.invalid/1",
            "reason": "approved verdict against an unmergeable branch",
        },
        {
            "ref": stale.ref,
            "title": "issue 2",
            "url": "https://example.invalid/2",
            "reason": "claim past its TTL with no PR",
        },
        {
            "ref": childless.ref,
            "title": "issue 3",
            "url": "https://example.invalid/3",
            "reason": "Building project has no tickets",
        },
        {
            "ref": dead_dependency.ref,
            "title": "issue 4",
            "url": "https://example.invalid/4",
            "reason": "blocked on blocker that will never close: other/repo#99",
        },
    ]


def test_brief_keeps_stranded_diagnostics_out_of_decision_counts(
    monkeypatch, capsys
):
    project = issue(
        10,
        title="Ship it",
        status="Building",
        children_total=1,
        children_done=1,
    )
    child = issue(
        11,
        title="Conflicting branch",
        parent=project.ref,
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    assert funnel.cmd_brief(
        [project, child],
        NOW,
        pr_facts={child.ref: {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "headRefOid": "head-sha",
            "verdict": {"verdict": "approved", "head_sha": "head-sha"},
        }},
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["total_needing_nate"] == 1
    assert [row["ref"] for row in brief["items"]] == [project.ref]
    assert brief["stranded"] == [{
        "ref": child.ref,
        "title": "Conflicting branch",
        "url": "https://example.invalid/11",
        "reason": "approved verdict against an unmergeable branch",
    }]


def test_classify_blockers_preserves_not_planned_blockers():
    assert funnel.classify_blockers([
        {"number": 9, "state": "open", "repository": {"full_name": REPO}},
        {
            "number": 10,
            "state": "closed",
            "state_reason": "not_planned",
            "repository": {"full_name": REPO},
        },
        {
            "number": 11,
            "state": "closed",
            "state_reason": "completed",
            "repository": {"full_name": REPO},
        },
    ], REPO) == {
        "open": ["owner/repo#9"],
        "dead": ["owner/repo#10"],
    }


def test_conflicting_pr_with_a_moved_head_is_not_called_stranded():
    ticket = issue(20, parent="{}#10".format(REPO))

    assert funnel.stranded_json(
        [ticket],
        NOW,
        pr_facts={ticket.ref: {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "headRefOid": "new-head",
            "verdict": {"verdict": "approved", "head_sha": "old-head"},
        }},
    ) == []


def _comment_block(number, blocker):
    references, _, reason = funnel.parse_block_comment([
        "**Blocked on #{}:** waiting for the other ticket.".format(blocker)
    ])
    return issue(
        number,
        block_references=references,
        block_reason=reason,
    )


def test_native_block_cycle_is_reported_once_on_lowest_numbered_member():
    first = issue(21, open_blockers=["{}#31".format(REPO)])
    second = issue(31, open_blockers=["{}#21".format(REPO)])

    assert funnel.stranded_json([second, first], NOW) == [{
        "ref": first.ref,
        "title": "issue 21",
        "url": "https://example.invalid/21",
        "reason": "block cycle: #21 → #31 → #21",
    }]


def test_comment_block_cycle_is_reported_once_on_lowest_numbered_member():
    first = _comment_block(41, 51)
    second = _comment_block(51, 41)

    assert funnel.stranded_json([first, second], NOW) == [{
        "ref": first.ref,
        "title": "issue 41",
        "url": "https://example.invalid/41",
        "reason": "block cycle: #41 → #51 → #41",
    }]


def test_mixed_native_and_comment_block_cycle_is_reported_once():
    first = issue(61, open_blockers=["{}#71".format(REPO)])
    second = _comment_block(71, 81)
    third = issue(81, open_blockers=["{}#61".format(REPO)])

    rows = funnel.stranded_json([third, first, second], NOW)

    assert [row["ref"] for row in rows] == [first.ref]
    assert rows[0]["reason"] == "block cycle: #61 → #71 → #81 → #61"


def test_block_chain_is_not_reported_as_a_cycle():
    first = issue(91, open_blockers=["{}#101".format(REPO)])
    second = issue(101, open_blockers=["{}#111".format(REPO)])
    third = issue(111)

    assert funnel.stranded_json([first, second, third], NOW) == []


def test_pr_side_strands_report_closed_and_non_building_tickets():
    closed_ticket = issue(
        338,
        parent="{}#89".format(REPO),
        state="CLOSED",
    )
    ready_project = issue(
        343,
        title="Ready project",
        status="Ready",
        children_total=1,
    )
    waiting_ticket = issue(
        346,
        title="Waiting ticket",
        parent=ready_project.ref,
    )
    building_project = issue(
        350,
        title="Building project",
        status="Building",
        children_total=1,
    )
    healthy_ticket = issue(
        351,
        title="Healthy ticket",
        parent=building_project.ref,
    )

    rows = funnel.stranded_json(
        [closed_ticket, ready_project, waiting_ticket,
         building_project, healthy_ticket],
        NOW,
        pr_facts={
            closed_ticket.ref: {"state": "OPEN", "number": 341},
            waiting_ticket.ref: {"state": "OPEN", "number": 348},
            healthy_ticket.ref: {"state": "OPEN", "number": 349},
        },
    )

    assert rows == [
        {
            "ref": closed_ticket.ref,
            "title": "issue 338",
            "url": "https://example.invalid/338",
            "reason": "open PR on closed ticket",
        },
        {
            "ref": waiting_ticket.ref,
            "title": "Waiting ticket",
            "url": "https://example.invalid/346",
            "reason": "open PR on open ticket whose project Status is Ready; "
                      "merge gate will refuse it",
        },
    ]


def test_pr_side_strands_are_absent_without_pr_facts():
    project = issue(355, status="Ready", children_total=1)
    ticket = issue(356, parent=project.ref)
    closed_ticket = issue(
        357,
        parent=project.ref,
        state="CLOSED",
    )

    assert funnel.stranded_json(
        [project, ticket, closed_ticket], NOW
    ) == []


def test_stranded_reports_finished_upkeep_projects_without_acceptance():
    finished = issue(
        86,
        title="Finished upkeep",
        status="Building",
        klass="Improve",
        body=funnel.origin_block("agent", at=NOW, run="stranded-run", agent="codex"),
        children_total=5,
        children_done=5,
    )
    second_finished = issue(
        87,
        title="Second finished upkeep",
        status="Building",
        klass="Improve",
        body=funnel.origin_block("agent", at=NOW, run="stranded-run", agent="codex"),
        children_total=5,
        children_done=5,
    )
    new_project = issue(
        88,
        title="New project",
        status="Building",
        klass="New",
        children_total=5,
        children_done=5,
    )
    incomplete = issue(
        89,
        title="Incomplete upkeep",
        status="Building",
        klass="Improve",
        children_total=5,
        children_done=4,
    )

    assert funnel.stranded_json(
        [finished, second_finished, new_project, incomplete], NOW
    ) == [
        {
            "ref": finished.ref,
            "title": finished.title,
            "url": finished.url,
            "reason": "finished upkeep project not closed",
        },
        {
            "ref": second_finished.ref,
            "title": second_finished.title,
            "url": second_finished.url,
            "reason": "finished upkeep project not closed",
        },
    ]
    assert funnel.gate_question(second_finished) is None


def test_open_ticket_under_a_closed_parent_is_stranded():
    """No gate watches it and nothing will close it (#1212)."""
    parent = issue(20, state="CLOSED", status="Done", klass="Broken",
                   children_total=1, children_done=0)
    orphan = issue(21, parent="{}#20".format(REPO))

    rows = funnel.stranded_items([parent, orphan], NOW)

    assert [row["ref"] for row in rows] == ["{}#21".format(REPO)]
    assert rows[0]["reason"] == (
        "parent {}#20 is closed with Status Done".format(REPO)
    )


def test_open_ticket_under_a_parked_parent_is_not_stranded():
    """Parking is a decision; its tickets are meant to sit."""
    parent = issue(30, state="CLOSED", status="Parked", klass="New",
                   children_total=1)
    child = issue(31, parent="{}#30".format(REPO))

    assert funnel.stranded_items([parent, child], NOW) == []


def test_open_ticket_under_an_open_parent_is_not_stranded():
    parent = issue(40, status="Building", klass="Broken", children_total=1)
    child = issue(41, parent="{}#40".format(REPO))

    assert funnel.stranded_items([parent, child], NOW) == []


def test_a_closed_parent_with_no_status_is_still_named():
    parent = issue(50, state="CLOSED", klass="Broken", children_total=1)
    child = issue(51, parent="{}#50".format(REPO))

    rows = funnel.stranded_items([parent, child], NOW)

    assert rows[0]["reason"] == (
        "parent {}#50 is closed with Status unset".format(REPO)
    )


# #1432: a block that nothing can lift and nobody is asked about.


def _building(number):
    return issue(number, status="Building", klass="Broken", children_total=2)


def test_a_codex_decline_with_no_condition_is_stranded():
    """FF#289 on 2026-09-24: label, Needs agent, a Declined comment."""
    parent = _building(60)
    declined = issue(61, parent="{}#60".format(REPO), labels=["blocked"],
                     needs="agent", decline_reason="prerequisite unlanded")

    rows = funnel.stranded_items([parent, declined], NOW)

    assert [row["ref"] for row in rows] == ["{}#61".format(REPO)]
    assert rows[0]["reason"] == (
        "blocked with no condition that can clear it, and no one is asked "
        "(Needs: agent)"
    )


def test_a_reason_only_external_event_wait_without_a_spec_asks_nate():
    """Needs external-event cannot replace a parseable condition."""
    parent = _building(62)
    waiting = issue(63, parent="{}#62".format(REPO), labels=["blocked"],
                    needs="external-event",
                    block_reason="an external event")

    rows = funnel.stranded_items([parent, waiting], NOW)

    assert rows == []
    assert funnel.gate_question(waiting) == "Unblock?"


def test_blocks_with_a_clearable_condition_or_an_asker_are_not_stranded():
    parent = _building(70)
    ticket = "{}#70".format(REPO)
    prerequisite = issue(71, parent=ticket)
    rows = [
        parent,
        prerequisite,
        issue(72, parent=ticket, labels=["blocked"], needs="external-event",
              block_reason="wait", blocked_until=NOW.date()),
        issue(73, parent=ticket, labels=["blocked"], needs="external-event",
              block_reason="wait", block_references=["#71"]),
        issue(74, parent=ticket, needs="agent",
              open_blockers=["{}#71".format(REPO)]),
        issue(75, parent=ticket, labels=["blocked"], needs="human"),
        issue(76, parent=ticket, labels=["blocked"],
              needs="claude-code-environment"),
        # Needs none asks Nate "Unblock?", so someone is asked.
        issue(77, parent=ticket, labels=["blocked"], needs="none"),
    ]

    assert funnel.stranded_items(rows, NOW) == []
    assert funnel.gate_question(rows[-1]) == "Unblock?"


def test_a_ticket_of_a_parked_project_strands_a_live_dependent():
    """#227/#229 waited on #165, a ticket of parked #15."""
    parked = issue(80, state="CLOSED", status="Parked", klass="Improve",
                   children_total=2)
    abandoned = issue(81, parent="{}#80".format(REPO))
    sibling = issue(82, parent="{}#80".format(REPO),
                    open_blockers=["{}#81".format(REPO)])
    live = _building(83)
    dependent = issue(84, parent="{}#83".format(REPO),
                      open_blockers=["{}#81".format(REPO)])

    rows = funnel.stranded_items(
        [parked, abandoned, sibling, live, dependent], NOW)

    assert [row["ref"] for row in rows] == ["{}#84".format(REPO)]
    assert rows[0]["reason"] == (
        "blocked on blocker that will never close: {}#81".format(REPO)
    )
