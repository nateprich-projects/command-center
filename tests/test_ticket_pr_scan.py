"""`ticket_pr_facts` uses bounded repo scans, not one call per ticket.

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


def branch_row(number):
    return {"ref": "refs/heads/ticket/{}".format(number)}


def repo_reads(prs, branches=()):
    def gh_json(*args):
        return list(prs) if args[1] == "pr" else list(branches)
    return gh_json


def test_one_scan_per_repo_regardless_of_ticket_count(monkeypatch):
    """The whole point: cost scales with repos, not with tickets."""
    calls = []

    def gh_json(*args):
        calls.append(args)
        if args[1] == "pr":
            return [pr_row(n, "ticket/{}".format(n)) for n in range(10, 40)]
        return [branch_row(n) for n in range(10, 40)]

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    few = funnel.ticket_pr_facts([ticket(10), ticket(11)])
    calls_for_few = len(calls)
    calls.clear()
    many = funnel.ticket_pr_facts([ticket(n) for n in range(10, 40)])

    assert calls_for_few == 2
    assert len(calls) == 2, "call count must not grow with ticket count"
    assert few["nateprich/beta#10"]["number"] == 10
    assert few["nateprich/beta#10"]["branch_exists"] is True
    assert len(many) == 30


def test_branch_absence_survives_a_truncated_pr_scan(monkeypatch):
    """Unknown PR absence must not erase definitive branch absence."""
    limit = funnel.MERGED_PR_SCAN_LIMIT
    rows = [pr_row(n, "ticket/{}".format(n)) for n in range(1000, 1000 + limit + 1)]
    monkeypatch.setattr(funnel, "_gh_json", repo_reads(rows))

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert facts["nateprich/beta#42"] == {"branch_exists": False}


def test_a_truncated_branch_scan_never_implies_branch_absence(monkeypatch):
    limit = funnel.MERGED_PR_SCAN_LIMIT
    branches = [branch_row(n) for n in range(1000, 1000 + limit)]
    monkeypatch.setattr(funnel, "_gh_json", repo_reads([], branches))

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert "nateprich/beta#42" not in facts


def test_a_missing_pr_inside_a_complete_scan_is_none(monkeypatch):
    """The other half of the contract: looked up, genuinely no PR."""
    monkeypatch.setattr(
        funnel, "_gh_json", repo_reads([pr_row(10, "ticket/10")])
    )

    facts = funnel.ticket_pr_facts([ticket(10), ticket(42)])

    assert facts["nateprich/beta#42"] is None
    assert facts["nateprich/beta#10"]["number"] == 10


def test_a_remote_branch_without_a_pr_is_still_recorded(monkeypatch):
    monkeypatch.setattr(
        funnel, "_gh_json", repo_reads([], [branch_row(42)])
    )

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert facts["nateprich/beta#42"] == {
        "headRefName": "ticket/42",
        "branch_exists": True,
    }


def test_a_closed_ticket_with_an_open_pr_is_included(monkeypatch):
    closed = ticket(338, state="CLOSED")
    monkeypatch.setattr(
        funnel, "_gh_json", repo_reads([pr_row(341, "ticket/338")])
    )

    facts = funnel.ticket_pr_facts([closed])

    assert facts[closed.ref]["number"] == 341
    assert facts[closed.ref]["state"] == "OPEN"


def test_verdict_is_looked_up_only_for_an_open_conflicting_pr(monkeypatch):
    """A verdict lookup per ticket would undo the saving the scan exists for."""
    verdicts = []

    def latest_verdict(repo, number):
        verdicts.append((repo, number))
        return {"verdict": "changes"}

    monkeypatch.setattr(funnel, "latest_verdict", latest_verdict)
    monkeypatch.setattr(funnel, "_gh_json", repo_reads([
        pr_row(10, "ticket/10", mergeable="CONFLICTING"),
        pr_row(11, "ticket/11", mergeable="MERGEABLE"),
        pr_row(12, "ticket/12", state="MERGED", mergeable="CONFLICTING"),
    ]))

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


def test_the_per_ticket_helper_is_gone():
    """It was the N+1. Leaving it in the file invites the next reach for it."""
    assert not hasattr(funnel, "_ticket_pr")


def test_the_index_is_shared_rather_than_reimplemented():
    assert callable(funnel.ticket_pr_index)


def test_index_returns_truncation_so_callers_choose_their_own_safe_answer(monkeypatch):
    limit = funnel.MERGED_PR_SCAN_LIMIT
    monkeypatch.setattr(funnel, "_gh_json", lambda *a: [
        pr_row(n, "ticket/{}".format(n)) for n in range(1, limit + 2)])

    index, truncated = funnel.ticket_pr_index("nateprich/beta")

    assert truncated is True
    assert len(index) == limit
