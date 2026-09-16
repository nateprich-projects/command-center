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


def _graphql_node(row):
    node = dict(row)
    node["commits"] = {
        "nodes": [{
            "commit": {
                "statusCheckRollup": {
                    "contexts": {
                        "nodes": list(row.get("statusCheckRollup") or [])
                    }
                }
            }
        }]
    }
    if "comments" in row:
        node["comments"] = {"nodes": list(row["comments"])}
    if "reviews" in row:
        node["reviews"] = {"nodes": list(row["reviews"])}
    if "closingIssuesReferences" in row:
        node["closingIssuesReferences"] = {
            "nodes": list(row["closingIssuesReferences"])
        }
    return node


def repo_graphql_reads(prs, branches=(), calls=None, refs_has_next=False):
    prs = list(prs)
    branches = list(branches)

    def gh_graphql(query, **variables):
        if calls is not None:
            calls.append((query, variables))
        first = int(query.split("pullRequests(first: ", 1)[1].split(",", 1)[0])
        offset = first if variables else 0
        page = prs[offset:offset + first]
        return {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
            "repo0": {
                "pullRequests": {
                    "nodes": [_graphql_node(row) for row in page],
                    "pageInfo": {
                        "hasNextPage": offset + first < len(prs),
                        "endCursor": "cursor-1" if offset + first < len(prs)
                        else None,
                    },
                },
                "refs": {
                    "nodes": [
                        {"name": row["ref"].replace("refs/heads/", "")}
                        for row in branches
                    ],
                    "pageInfo": {"hasNextPage": refs_has_next},
                },
            },
        }

    return gh_graphql


def test_one_scan_per_repo_regardless_of_ticket_count(monkeypatch):
    """The whole point: cost scales with repos, not with tickets."""
    calls = []

    rows = [pr_row(n, "ticket/{}".format(n)) for n in range(10, 40)]
    monkeypatch.setattr(
        funnel, "gh_graphql",
        repo_graphql_reads(rows, [branch_row(n) for n in range(10, 40)], calls),
    )

    few = funnel.ticket_pr_facts([ticket(10), ticket(11)])
    calls_for_few = len(calls)
    calls.clear()
    many = funnel.ticket_pr_facts([ticket(n) for n in range(10, 40)])

    assert calls_for_few == 1
    assert len(calls) == 1, "call count must not grow with ticket count"
    assert few["nateprich/beta#10"]["number"] == 10
    assert few["nateprich/beta#10"]["branch_exists"] is True
    assert len(many) == 30


def test_branch_absence_survives_a_truncated_pr_scan(monkeypatch):
    """Unknown PR absence must not erase definitive branch absence."""
    limit = funnel.MERGED_PR_SCAN_LIMIT
    rows = [pr_row(n, "ticket/{}".format(n)) for n in range(1000, 1000 + limit + 1)]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert facts["nateprich/beta#42"] == {"branch_exists": False}


def test_a_truncated_branch_scan_never_implies_branch_absence(monkeypatch):
    limit = funnel.MERGED_PR_SCAN_LIMIT
    branches = [branch_row(n) for n in range(1000, 1000 + limit)]
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([], branches, refs_has_next=True)
    )

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert "nateprich/beta#42" not in facts


def test_a_missing_pr_inside_a_complete_scan_is_none(monkeypatch):
    """The other half of the contract: looked up, genuinely no PR."""
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([pr_row(10, "ticket/10")])
    )

    facts = funnel.ticket_pr_facts([ticket(10), ticket(42)])

    assert facts["nateprich/beta#42"] is None
    assert facts["nateprich/beta#10"]["number"] == 10


def test_a_remote_branch_without_a_pr_is_still_recorded(monkeypatch):
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([], [branch_row(42)])
    )

    facts = funnel.ticket_pr_facts([ticket(42)])

    assert facts["nateprich/beta#42"] == {
        "headRefName": "ticket/42",
        "branch_exists": True,
    }


def test_outcome_scan_can_opt_into_pr_comments_and_closing_refs(monkeypatch):
    calls = []

    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        repo_graphql_reads(
            [pr_row(42, "ticket/42", comments=[], closingIssuesReferences=[])],
            calls=calls,
        ),
    )

    funnel.ticket_pr_index("nateprich/beta", limit=77, include_comments=True)

    assert len(calls) == 1
    query = calls[0][0]
    assert "comments(last: 100)" in query
    assert "closingIssuesReferences(first: 100)" in query
    assert "rateLimit { cost remaining resetAt }" in query


def test_default_index_keeps_review_rows_for_show(monkeypatch):
    review = {"body": "looks good", "state": "APPROVED"}
    monkeypatch.setattr(
        funnel, "gh_graphql",
        repo_graphql_reads([pr_row(42, "ticket/42", reviews=[review])]),
    )

    index, truncated = funnel.ticket_pr_index("nateprich/beta")

    assert truncated is False
    assert index["nateprich/beta#42"]["reviews"] == [review]


def test_batch_replaces_per_pr_fanout_and_measures_saving_against_655(
    monkeypatch,
):
    """#658: #655's before number was 42 calls and 47 measured points."""
    rows = [
        pr_row(number, "ticket/{}".format(number), comments=[])
        for number in range(10, 40)
    ]
    calls = []
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads(rows, calls=calls)
    )

    funnel.ticket_pr_facts([ticket(number) for number in range(10, 40)])

    before_calls = 1 + len(rows)  # pr list plus one comment read per open PR
    after_calls = len(calls)      # one measured batch, including rateLimit.cost
    assert before_calls == 31
    assert after_calls == 1
    assert before_calls - after_calls == 30
    assert "pullRequests(first: 100" in calls[0][0]
    assert "rateLimit { cost remaining resetAt }" in calls[0][0]
    assert 'refs(refPrefix: "refs/heads/", first: 100)' in calls[0][0]


def test_a_closed_ticket_with_an_open_pr_is_included(monkeypatch):
    closed = ticket(338, state="CLOSED")
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([pr_row(341, "ticket/338")])
    )

    facts = funnel.ticket_pr_facts([closed])

    assert facts[closed.ref]["number"] == 341
    assert facts[closed.ref]["state"] == "OPEN"


def test_verdict_is_looked_up_only_for_an_open_conflicting_pr(monkeypatch):
    """A verdict lookup per ticket would undo the saving the scan exists for."""
    body = funnel.REVIEW_MARKER + "\n\n```json\n{\"verdict\": \"changes\"}\n```"
    rows = [
        pr_row(10, "ticket/10", mergeable="CONFLICTING", comments=[{"body": body}]),
        pr_row(11, "ticket/11", mergeable="MERGEABLE"),
        pr_row(12, "ticket/12", state="MERGED", mergeable="CONFLICTING"),
    ]
    monkeypatch.setattr(
        funnel, "latest_verdict",
        lambda repo, number: (_ for _ in ()).throw(
            AssertionError("batch verdicts must not call gh pr view")
        ),
    )
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))

    facts = funnel.ticket_pr_facts([ticket(10), ticket(11), ticket(12)])

    assert facts["nateprich/beta#10"]["verdict"] == {"verdict": "changes"}
    assert facts["nateprich/beta#11"]["verdict"] is None
    assert "verdict" not in facts["nateprich/beta#12"]


def test_an_unreadable_response_fails_closed(monkeypatch):
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **kw: None)

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
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([
            pr_row(n, "ticket/{}".format(n)) for n in range(1, limit + 2)
        ])
    )

    index, truncated = funnel.ticket_pr_index("nateprich/beta")

    assert truncated is True
    assert len(index) == limit


def test_index_keeps_all_rows_for_history_consumers(monkeypatch):
    rows = [pr_row(10, "ticket/10"), pr_row(11, "ticket/10")]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))

    index, truncated = funnel.ticket_pr_index("nateprich/beta", limit=10)

    assert truncated is False
    assert index["nateprich/beta#10"]["number"] == 10
    assert [row["number"] for row in index.all_rows] == [10, 11]
