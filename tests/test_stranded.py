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
