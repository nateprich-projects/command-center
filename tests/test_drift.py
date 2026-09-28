"""Drift detection is pure over histories fetched from GitHub."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import DriftFacts, Item  # noqa: E402


REPO = "owner/repo"
READY = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
#: The owner account, the only comment author whose verdict counts (#1787).
OWNER = {"login": "nateprich"}
BUILDING = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)


def project(**kwargs):
    values = {
        "repo": REPO,
        "number": 1,
        "title": "Project",
        "url": "https://example.invalid/1",
        "state": "OPEN",
        "status": "Building",
        "status_since": BUILDING,
        "children_total": 2,
    }
    values.update(kwargs)
    return Item(**values)


def verdict(value):
    return {
        "verdict": value,
        "ci": "green",
        "head_sha": "abc123",
        "blocking": [],
    }


def test_clean_project_has_no_drift():
    facts = DriftFacts(ready_at=READY, building_at=BUILDING)

    assert funnel.drift_since_approval(project(), facts) == []


def test_each_signal_requires_a_change_after_the_relevant_boundary():
    facts = DriftFacts(
        ready_at=READY,
        plan_edit_times=(READY,),
        building_at=BUILDING,
        ticket_created_at=(BUILDING,),
    )

    assert funnel.drift_since_approval(project(), facts) == []

    assert funnel.drift_since_approval(
        project(),
        DriftFacts(
            ready_at=READY,
            plan_edit_times=(datetime(2026, 9, 1, 10, 1, tzinfo=timezone.utc),),
        ),
    ) == [funnel.DRIFT_PLAN_EDIT]
    assert funnel.drift_since_approval(
        project(), DriftFacts(review_verdicts=(verdict("rejected"),))
    ) == [funnel.DRIFT_REJECTED_REVIEW]
    assert funnel.drift_since_approval(
        project(), DriftFacts(regression_pr_numbers=(20,))
    ) == [funnel.DRIFT_REGRESSION]
    assert funnel.drift_since_approval(
        project(),
        DriftFacts(
            building_at=BUILDING,
            ticket_created_at=(
                datetime(2026, 9, 1, 11, 1, tzinfo=timezone.utc),
            ),
        ),
    ) == [funnel.DRIFT_LATE_TICKET]


def test_all_signals_are_reported_in_stable_order_and_old_rejection_counts():
    facts = DriftFacts(
        ready_at=READY,
        plan_edit_times=(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),),
        # A later approval does not erase a rejected verdict in the history.
        review_verdicts=(verdict("rejected"), verdict("approved")),
        regression_pr_numbers=(20,),
        building_at=BUILDING,
        ticket_created_at=(datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),),
    )

    assert funnel.drift_since_approval(project(), facts) == list(
        funnel.DRIFT_SIGNAL_NAMES
    )


def test_fetch_drift_facts_reads_all_ticket_pr_verdicts_and_histories(monkeypatch):
    item = project()
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if query == funnel.DRIFT_STATUS_QUERY:
            return {"repository": {"issue": {"timelineItems": {"nodes": [
                {
                    "status": "Ready",
                    "createdAt": "2026-09-01T10:00:00Z",
                    "project": {"number": funnel.PROJECT_NUMBER},
                },
                {
                    "status": "Building",
                    "createdAt": "2026-09-01T11:00:00Z",
                    "project": {"number": funnel.PROJECT_NUMBER},
                },
            ]}}}}
        if query == funnel.DRIFT_EDIT_QUERY:
            return {"repository": {"issue": {"userContentEdits": {"nodes": [
                {
                    "editedAt": "2026-09-01T12:00:00Z",
                },
            ]}}}}
        if query == funnel.SUB_ISSUES:
            return {"repository": {"issue": {"subIssues": {"nodes": [
                {
                    "number": 2,
                    "createdAt": "2026-09-01T09:00:00Z",
                    "repository": {"nameWithOwner": REPO},
                },
                {
                    "number": 3,
                    "createdAt": "2026-09-01T12:00:00Z",
                    "repository": {"nameWithOwner": REPO},
                },
            ]}}}}
        raise AssertionError("unexpected GraphQL query")

    review_rejected = funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(
        verdict("rejected")
    ) + "\n```"
    review_approved = funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(
        verdict("approved")
    ) + "\n```"

    def gh_json(*args):
        calls.append(args)
        if args[1:3] == ("pr", "list"):
            if "ticket/3" in args:
                return []
            # The funnel's own PRs: same-repository heads, owner author
            # (#1794).
            return [{"number": number, "isCrossRepository": False,
                     "author": OWNER} for number in (20, 21)]
        if args[1:3] == ("pr", "view"):
            number = args[3]
            if number == "20":
                return {"comments": [
                    {"body": review_rejected, "author": OWNER},
                    {"body": review_approved, "author": OWNER},
                ]}
            return {"comments": [{"body": review_approved, "author": OWNER}]}
        if args[1:3] == ("issue", "list"):
            return [{
                "title": funnel.REGRESSION_PREFIX + "21: old change",
                "body": "- Merged PR: https://github.com/{}/pull/21".format(REPO),
            }]
        raise AssertionError("unexpected gh command: {}".format(args))

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    facts = funnel.fetch_drift_facts(item)

    assert funnel.drift_since_approval(item, facts) == list(
        funnel.DRIFT_SIGNAL_NAMES
    )
    assert facts.regression_pr_numbers == (21,)
    assert len(facts.review_verdicts) == 3
    assert any(query == funnel.DRIFT_STATUS_QUERY for query, _ in calls)


def test_a_forged_rejection_raises_no_drift_signal(monkeypatch):
    """Only the owner's verdicts are review history (#1787)."""
    def body(value):
        return funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(
            verdict(value)) + "\n```"

    def gh_json(*args):
        assert args[1:3] == ("pr", "view")
        return {"comments": [
            {"body": body("approved"), "author": OWNER},
            {"body": body("approved"), "user": OWNER},
            {"body": body("rejected"), "author": {"login": "mallory"}},
            {"body": body("rejected")},
        ]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    verdicts = funnel._review_verdicts([(REPO, 20)])

    assert [found["verdict"] for found in verdicts] == ["approved", "approved"]
    facts = DriftFacts(ready_at=READY, building_at=BUILDING,
                       review_verdicts=verdicts)
    assert funnel.DRIFT_REJECTED_REVIEW not in funnel.drift_since_approval(
        project(), facts)


def test_a_foreign_pr_on_a_ticket_branch_is_not_the_tickets_history(
        monkeypatch):
    """``--head`` matches the branch in any fork; only the funnel's own PRs
    are the ticket's (#1794), so a stranger's PR adds no review history and
    no regression match to drift."""
    asked = []

    def gh_json(*args):
        asked.append(args)
        return [
            {"number": 20, "isCrossRepository": False, "author": OWNER},
            {"number": 21, "isCrossRepository": True,
             "headRepository": {"name": "repo"},
             "headRepositoryOwner": {"login": "mallory"},
             "author": {"login": "mallory"}},
            {"number": 22, "isCrossRepository": False,
             "author": {"login": "mallory"}},
            {"number": 23, "isCrossRepository": False},
            {"number": 24, "author": OWNER},
        ]

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    assert funnel._ticket_prs(REPO, 9) == [(REPO, 20)]
    fields = asked[0][asked[0].index("--json") + 1].split(",")
    assert {"isCrossRepository", "headRepository", "headRepositoryOwner",
            "author"} <= set(fields)
