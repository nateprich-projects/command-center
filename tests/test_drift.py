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
        if args[:3] == (
            "gh", "api", "repos/owner/repo/issues/1/timeline"
        ):
            return [[
                {
                    "event": "edited",
                    "created_at": "2026-09-01T12:00:00Z",
                    "changes": {"body": {"from": "old"}},
                },
                {
                    "event": "edited",
                    "created_at": "2026-09-01T13:00:00Z",
                    "changes": {"title": {"from": "old"}},
                },
            ]]
        if args[1:3] == ("pr", "list"):
            if "ticket/3" in args:
                return []
            return [{"number": 20}, {"number": 21}]
        if args[1:3] == ("pr", "view"):
            number = args[3]
            if number == "20":
                return {"comments": [
                    {"body": review_rejected},
                    {"body": review_approved},
                ]}
            return {"comments": [{"body": review_approved}]}
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
