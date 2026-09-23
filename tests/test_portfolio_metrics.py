"""Portfolio metrics use durable causes and bounded PR facts."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _item(number, title, *, repo=funnel.REPO, parent=None, **kwargs):
    values = {
        "repo": repo,
        "number": number,
        "title": title,
        "url": "https://github.com/{}/issues/{}".format(repo, number),
        "state": "OPEN",
        "parent": parent,
        "created_at": NOW - timedelta(days=1),
    }
    values.update(kwargs)
    return funnel.Item(**values)


def test_recorded_cause_regressions_joins_capture_and_reject_records():
    captured = _item(
        1,
        "Captured broken project",
        klass="Broken",
        body=funnel.append_caused_by("A note", ["#700"], at=NOW),
    )
    rejected = _item(2, "Broken without cause", klass="Broken")
    regression_ticket = _item(
        702,
        "Ticket from rejected merge",
        parent=rejected.ref,
    )
    regression = _item(
        703,
        "{}703: failed merge".format(funnel.REGRESSION_PREFIX),
        body="- Merged PR: https://github.com/example/repo/pull/4\n"
             "- Ticket: {}".format(regression_ticket.ref),
    )
    unrecorded = _item(3, "Broken with no durable cause", klass="Broken")

    report = funnel.recorded_cause_regressions(
        [captured, rejected, regression_ticket, regression, unrecorded], NOW
    )

    assert report["broken_projects"] == 3
    assert report["with_recorded_cause"] == 2
    assert report["without_recorded_cause"] == 1
    assert report["count"] == 2


def test_recorded_cause_regressions_ignores_prose_references():
    project = _item(
        4,
        "Broken with prose only",
        klass="Broken",
        body="See PR #700 and ticket #701.",
    )

    report = funnel.recorded_cause_regressions([project], NOW)

    assert report["broken_projects"] == 1
    assert report["count"] == 0


def _recent_merged_prs(rows, calls):
    rows = list(rows)

    def gh_graphql(query, **variables):
        calls.append((query, variables))
        offset = 100 if variables.get("cursor") == "cursor-1" else 0
        page = rows[offset:offset + 100]
        has_next = offset + len(page) < len(rows)
        return {
            "rateLimit": {
                "cost": 1, "remaining": 4999 - len(calls), "resetAt": "later"
            },
            "repo0": {
                "pullRequests": {
                    "nodes": page,
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": "cursor-1" if has_next else None,
                    },
                },
            },
        }

    return gh_graphql


@pytest.mark.parametrize(
    ("row_count", "ticket_count", "expected_calls"),
    [(100, 50, 1), (101, 51, 2)],
)
def test_command_center_ticket_pr_share_pages_recent_merged_prs(
    monkeypatch, row_count, ticket_count, expected_calls
):
    rows = [
        {
            "state": "MERGED",
            "mergedAt": (NOW - timedelta(hours=1)).isoformat(),
            "updatedAt": (NOW - timedelta(hours=1)).isoformat(),
            "headRefName": (
                "ticket/{}".format(number)
                if number % 2
                else "main"
            ),
        }
        for number in range(1, row_count + 1)
    ]
    calls = []
    monkeypatch.setattr(funnel, "gh_graphql", _recent_merged_prs(rows, calls))

    report = funnel.command_center_ticket_pr_share([], NOW)

    assert report["status"] == "available"
    assert report["merged_prs"] == row_count
    assert report["ticket_merged_prs"] == ticket_count
    assert report["share"] == round(ticket_count / row_count, 3)
    assert report["value"] == round(ticket_count / row_count, 3)
    assert len(calls) == expected_calls
    assert sum(1 for _query, _variables in calls) <= 2
    assert all("rateLimit { cost remaining resetAt }" in query
               for query, _variables in calls)
    assert all("states: [MERGED]" in query for query, _variables in calls)
    assert all("statusCheckRollup" not in query
               for query, _variables in calls)


def test_command_center_ticket_pr_share_stops_after_window(monkeypatch):
    rows = [
        {
            "state": "MERGED",
            "mergedAt": (NOW - timedelta(hours=1)).isoformat(),
            "updatedAt": (NOW - timedelta(hours=1)).isoformat(),
            "headRefName": "ticket/{}".format(number),
        }
        for number in range(1, 101)
    ] + [{
        "state": "MERGED",
        "mergedAt": (NOW - timedelta(days=31)).isoformat(),
        "updatedAt": (NOW - timedelta(days=31)).isoformat(),
        "headRefName": "ticket/old",
    }]
    calls = []
    monkeypatch.setattr(funnel, "gh_graphql", _recent_merged_prs(rows, calls))

    report = funnel.command_center_ticket_pr_share([], NOW)

    assert report["status"] == "available"
    assert report["merged_prs"] == 100
    assert report["ticket_merged_prs"] == 100
    assert len(calls) == 2


@pytest.mark.parametrize("value", ["", "notes", "owner/repo"])
def test_normalise_cause_reference_rejects_non_issue_values(value):
    assert funnel._normalise_cause_reference(value) is None
