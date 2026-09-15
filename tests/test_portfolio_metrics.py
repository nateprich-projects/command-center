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


def test_command_center_ticket_pr_share_counts_all_recent_merged_prs(monkeypatch):
    rows = [
        {
            "state": "MERGED",
            "mergedAt": "2026-09-13T10:00:00Z",
            "headRefName": "ticket/1",
        },
        {
            "state": "MERGED",
            "mergedAt": "2026-09-12T10:00:00+00:00",
            "headRefName": "main",
        },
        {
            "state": "OPEN",
            "mergedAt": None,
            "headRefName": "ticket/2",
        },
        {
            "state": "MERGED",
            "mergedAt": "2026-07-01T10:00:00Z",
            "headRefName": "ticket/3",
        },
    ]
    monkeypatch.setattr(
        funnel,
        "ticket_pr_index",
        lambda repo: (funnel.TicketPRIndex(all_rows=rows), False),
    )

    report = funnel.command_center_ticket_pr_share([], NOW)

    assert report["status"] == "available"
    assert report["merged_prs"] == 2
    assert report["ticket_merged_prs"] == 1
    assert report["share"] == 0.5
    assert report["value"] == 0.5


def test_command_center_ticket_pr_share_refuses_truncated_scan(monkeypatch):
    monkeypatch.setattr(
        funnel,
        "ticket_pr_index",
        lambda repo: (funnel.TicketPRIndex(all_rows=[]), True),
    )

    report = funnel.command_center_ticket_pr_share([], NOW)

    assert report["status"] == "unavailable"
    assert report["share"] is None
    assert "truncated" in report["reason"]


@pytest.mark.parametrize("value", ["", "notes", "owner/repo"])
def test_normalise_cause_reference_rejects_non_issue_values(value):
    assert funnel._normalise_cause_reference(value) is None
