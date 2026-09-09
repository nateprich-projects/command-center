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


def test_dependency_facts_preserves_not_planned_blockers(monkeypatch):
    monkeypatch.setattr(
        funnel,
        "_gh_json",
        lambda *args: [
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
        ],
    )

    assert funnel.dependency_facts(REPO, 7) == {
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
