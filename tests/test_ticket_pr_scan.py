"""`ticket_pr_facts` reads PRs with one bounded scan per repo, not one per ticket.

The per-ticket form was 85% of a full brief's GraphQL cost and grew with the
board (#272). These tests pin the three things the rewrite can get wrong.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def ticket(number, parent=1, repo="nateprich/beta", **kw) -> Item:
    kw.setdefault("title", "issue {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    return Item(
        number=number, status=None, klass=None,
        status_since=NOW - timedelta(days=1), repo=repo,
        parent="{}#{}".format(repo, parent), **kw
    )


def pr_row(number, branch, state="OPEN", **kw):
    row = {
        "number": number, "state": state,
        "url": "https://example.invalid/pr/{}".format(number),
        "headRefName": branch, "headRefOid": "abc123",
        "mergeable": "MERGEABLE", "mergedAt": None, "reviews": [],
    }
    row.update(kw)
    return row


def test_one_scan_per_repo_regardless_of_ticket_count(monkeypatch):
    """The whole point: cost scales with repos, not with tickets."""
    calls = []

    def gh_json(*args):
        calls.append(args)
        return [pr_row(n, "ticket/{}".format(n)) for n in range(10, 40)]

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    few = funnel.ticket_pr_facts([ticket(10), ticket(11)])
    calls_for_few = len(calls)
    calls.clear()
    many = funnel.ticket_pr_facts([ticket(n) for n in range(10, 40)])

    assert calls_for_few == 1
    assert len(calls) == 1, "call count must not grow with ticket count"
    assert few["nateprich/beta#10"]["number"] == 10
    assert len(many) == 30


def test_a_ticket_beyond_the_scan_window_is_omitted_not_none(monkeypatch):
    """A truncated scan's blind spot must not read as a defect in the work.

    `None` would reach `stranded_items` as "claim past its TTL with no PR".
    """
    limit = funnel.MERGED_PR_SCAN_LIMIT
    rows = [pr_row(n, "ticket/{}".format(n)) for n in range(1000, 1000 + limit + 1)]
    monkeypatch.setattr(funnel, "_gh_json", lambda *a: rows)

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert "nateprich/beta#42" not in facts, "beyond-window ticket must be omitted"
    assert facts.get("nateprich/beta#42", "absent") == "absent"


def test_a_missing_pr_inside_a_complete_scan_is_none(monkeypatch):
    """The other half of the contract: looked up, genuinely no PR."""
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *a: [pr_row(10, "ticket/10")]
    )

    facts = funnel.ticket_pr_facts([ticket(10), ticket(42)])

    assert facts["nateprich/beta#42"] is None
    assert facts["nateprich/beta#10"]["number"] == 10


def test_verdict_is_looked_up_only_for_an_open_conflicting_pr(monkeypatch):
    """A verdict lookup per ticket would undo the saving the scan exists for."""
    verdicts = []

    def latest_verdict(repo, number):
        verdicts.append((repo, number))
        return {"verdict": "changes"}

    monkeypatch.setattr(funnel, "latest_verdict", latest_verdict)
    monkeypatch.setattr(funnel, "_gh_json", lambda *a: [
        pr_row(10, "ticket/10", mergeable="CONFLICTING"),
        pr_row(11, "ticket/11", mergeable="MERGEABLE"),
        pr_row(12, "ticket/12", state="MERGED", mergeable="CONFLICTING"),
    ])

    facts = funnel.ticket_pr_facts([ticket(10), ticket(11), ticket(12)])

    assert verdicts == [("nateprich/beta", 10)]
    assert facts["nateprich/beta#10"]["verdict"] == {"verdict": "changes"}
    assert "verdict" not in facts["nateprich/beta#11"]
    assert "verdict" not in facts["nateprich/beta#12"]


def test_an_unreadable_response_fails_closed(monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json", lambda *a: None)

    try:
        funnel.ticket_pr_facts([ticket(10)])
    except funnel.GitHubError:
        return
    raise AssertionError("an unreadable PR list must raise")
