"""`ticket_pr_facts` uses bounded repo scans, not one call per ticket.

The per-ticket form was 85% of a full brief's GraphQL cost and grew with the
board (#272). These tests pin the three things the rewrite can get wrong.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
#: The owner account, the only comment author whose verdict counts (#1787).
OWNER = {"login": "nateprich"}


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
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "mergedAt": None, "reviews": [],
        # The funnel's own PR: same-repository head, owner author (#1794).
        "isCrossRepository": False, "author": OWNER,
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
    """The whole point: cost scales with repos, not with tickets.

    Two reads per scan since #1986, not one: open rows with their comment
    tails, then the closed-and-merged history without them. Both are bounded
    by repositories and pages, so neither grows with the ticket count.
    """
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

    assert calls_for_few == 2
    assert len(calls) == 2, "call count must not grow with ticket count"
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


def test_a_truncated_pr_scan_leaves_a_ticket_without_a_pr_startable(monkeypatch):
    """Branch-only facts are not open PRs (#968)."""
    limit = funnel.MERGED_PR_SCAN_LIMIT
    rows = [
        pr_row(n, "ticket/{}".format(n), state="MERGED")
        for n in range(1000, 1000 + limit + 1)
    ]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))
    items = [ticket(42)]

    facts = funnel.ticket_pr_facts(items)

    assert facts["nateprich/beta#42"] == {"branch_exists": False}
    assert funnel.awaiting_review(items, pr_facts=facts) == set()
    assert funnel.review_queue(items, pr_facts=facts) == []


def test_a_branch_without_a_pr_is_neither_awaiting_nor_offered_for_review(
    monkeypatch,
):
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([], [branch_row(42)])
    )
    items = [ticket(42)]

    facts = funnel.ticket_pr_facts(items)

    assert funnel.awaiting_review(items, pr_facts=facts) == set()
    assert funnel.review_queue(items, pr_facts=facts) == []


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
    # Two measured batches since #1986, each including rateLimit.cost: the
    # open rows with their comment tails, and the history without them.
    after_calls = len(calls)
    assert before_calls == 31
    assert after_calls == 2
    assert before_calls - after_calls == 29
    for query, _variables in calls:
        assert "pullRequests(first: {}".format(
            funnel.PR_GRAPHQL_PR_PAGE_SIZE) in query
        assert "rateLimit { cost remaining resetAt }" in query
    # Branch refs ride the open read alone; asking twice would double the cost.
    assert 'refs(refPrefix: "refs/heads/", first: 100)' in calls[0][0]
    assert 'refs(refPrefix: "refs/heads/", first: 100)' not in calls[1][0]
    assert "comments(last:" in calls[0][0]
    assert "comments(last:" not in calls[1][0]


def test_scan_preserves_merge_state_status_for_conflict_routing(monkeypatch):
    calls = []
    row = pr_row(10, "ticket/10", mergeable="UNKNOWN",
                 mergeStateStatus="DIRTY")
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([row], calls=calls)
    )

    facts = funnel.ticket_pr_facts([ticket(10)])

    assert facts["nateprich/beta#10"]["mergeStateStatus"] == "DIRTY"
    assert "mergeStateStatus" in calls[0][0]


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
        pr_row(10, "ticket/10", mergeable="CONFLICTING",
               comments=[{"body": body, "author": OWNER}]),
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


def _verdict_body(verdict):
    return (funnel.REVIEW_MARKER + "\n\n```json\n"
            "{{\"verdict\": \"{}\", \"head_sha\": \"abc123\"}}\n```"
            .format(verdict))


def test_a_forged_approval_in_the_batch_is_no_merge_candidate(monkeypatch):
    """Reconcile merges on the batch verdict: only the owner's count (#1787)."""
    rows = [
        pr_row(10, "ticket/10", comments=[
            {"body": _verdict_body("rejected"), "author": OWNER},
            {"body": _verdict_body("approved"),
             "author": {"login": "mallory"}},
            {"body": _verdict_body("approved"), "author": None},
        ]),
        pr_row(11, "ticket/11", comments=[
            {"body": _verdict_body("approved"), "author": OWNER},
        ]),
    ]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))
    items = [ticket(10), ticket(11)]

    facts = funnel.ticket_pr_facts(items)

    assert facts["nateprich/beta#10"]["verdict"]["verdict"] == "rejected"
    assert facts["nateprich/beta#11"]["verdict"]["verdict"] == "approved"
    assert funnel.approved_merge_candidates(items, pr_facts=facts) == [
        {"repo": "nateprich/beta", "pr": 11, "ref": "nateprich/beta#11"},
    ]


# -- only the funnel's own PRs pair with tickets (#1794) -----------------------
#
# The scan matched PRs to tickets by branch name alone. command-center is
# public, so a fork's PR named ticket/<n> became the ticket's PR: offered for
# review, holding the ticket as awaiting review, and a merge candidate.

#: A fork's PR as the batched read returns it.
FORK = {"isCrossRepository": True,
        "headRepository": {"nameWithOwner": "mallory/beta"},
        "author": {"login": "mallory"}}

NOT_THE_FUNNELS = [
    pytest.param(FORK, id="fork"),
    pytest.param({"author": {"login": "mallory"}}, id="another-author"),
    pytest.param({"author": None}, id="author-unreadable"),
    pytest.param({"isCrossRepository": None}, id="head-unreadable"),
]


def test_the_scan_asks_who_opened_each_pr_and_where_its_head_lives(
        monkeypatch):
    calls = []
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([], calls=calls))

    funnel.ticket_pr_facts([ticket(10)])

    query = " ".join(calls[0][0].split())
    assert "isCrossRepository" in query
    assert "headRepository { nameWithOwner }" in query
    assert "author { login }" in query


def test_the_scan_carries_the_trust_fields_into_each_row(monkeypatch):
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads([
        pr_row(10, "ticket/10",
               headRepository={"nameWithOwner": "nateprich/beta"}),
    ]))

    facts = funnel.ticket_pr_facts([ticket(10)])

    fact = facts["nateprich/beta#10"]
    assert fact["isCrossRepository"] is False
    assert fact["headRepository"] == {"nameWithOwner": "nateprich/beta"}
    assert fact["author"] == OWNER


@pytest.mark.parametrize("trust", NOT_THE_FUNNELS)
def test_a_foreign_pr_on_a_ticket_branch_is_not_the_tickets_pr(
        monkeypatch, trust):
    rows = [
        pr_row(30, "ticket/10", comments=[
            {"body": _verdict_body("approved"), "author": OWNER},
        ], **trust),
        pr_row(11, "ticket/11"),
    ]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))
    items = [ticket(10), ticket(11)]

    facts = funnel.ticket_pr_facts(items)

    # A complete scan with no funnel PR and no branch: no PR at all.
    assert facts["nateprich/beta#10"] is None
    assert "nateprich/beta#10" not in facts.rows_by_ref
    assert facts["nateprich/beta#11"]["number"] == 11
    assert funnel.awaiting_review(items, pr_facts=facts) == {
        "nateprich/beta#11"}
    assert [entry["ref"] for entry in funnel.review_queue(
        items, pr_facts=facts)] == ["nateprich/beta#11"]
    assert funnel.approved_merge_candidates(items, pr_facts=facts) == []


@pytest.mark.parametrize("trust", NOT_THE_FUNNELS)
def test_a_foreign_pr_beside_the_owners_leaves_the_owners_as_the_fact(
        monkeypatch, trust):
    # Newest first, as GitHub returns them: the stranger's PR is newer.
    rows = [pr_row(30, "ticket/10", **trust), pr_row(10, "ticket/10")]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))

    facts = funnel.ticket_pr_facts([ticket(10)])

    assert facts["nateprich/beta#10"]["number"] == 10
    assert [row["number"] for row in
            facts.rows_by_ref["nateprich/beta#10"]] == [10]


@pytest.mark.parametrize("trust", NOT_THE_FUNNELS)
def test_an_approved_foreign_pr_in_an_injected_map_is_never_merged(
        monkeypatch, trust):
    """The row accessor holds even when a caller supplies its own map."""
    row = pr_row(30, "ticket/10", **trust)
    row["verdict"] = {"verdict": "approved", "head_sha": "abc123"}
    facts = funnel.TicketPRFacts(
        {"nateprich/beta#10": row},
        rows_by_ref={"nateprich/beta#10": [row]},
    )
    merged = []
    monkeypatch.setattr(
        funnel, "cmd_merge",
        lambda *args, **kwargs: merged.append(args) or 0)
    items = [ticket(10)]

    assert funnel.approved_merge_candidates(items, pr_facts=facts) == []
    assert funnel.reconcile_approved_merges(items, NOW, pr_facts=facts) == []
    assert merged == []
    assert funnel.awaiting_review(items, pr_facts=facts) == set()
    assert funnel.review_queue(items, pr_facts=facts) == []

    # A legacy map without rows_by_ref goes through the same accessor.
    legacy = {"nateprich/beta#10": row}
    assert funnel.approved_merge_candidates(items, pr_facts=legacy) == []
    assert funnel.awaiting_review(items, pr_facts=legacy) == set()


def test_the_owners_approved_pr_is_still_merged_by_reconciliation(
        monkeypatch):
    row = pr_row(10, "ticket/10")
    row["verdict"] = {"verdict": "approved", "head_sha": "abc123"}
    facts = funnel.TicketPRFacts(
        {"nateprich/beta#10": row},
        rows_by_ref={"nateprich/beta#10": [row]},
    )
    merged = []
    monkeypatch.setattr(
        funnel, "cmd_merge",
        lambda items, now, repo, pr, confirmed, **kwargs:
        merged.append((repo, pr)) or 0)

    results = funnel.reconcile_approved_merges(
        [ticket(10)], NOW, pr_facts=facts)

    assert merged == [("nateprich/beta", 10)]
    assert [entry["result"] for entry in results] == ["merged"]


@pytest.mark.parametrize("trust", NOT_THE_FUNNELS)
def test_the_index_and_its_history_rows_hold_only_funnel_prs(
        monkeypatch, trust):
    rows = [pr_row(30, "ticket/10", **trust), pr_row(10, "ticket/10"),
            pr_row(31, "ticket/12", **trust)]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))

    index, truncated = funnel.ticket_pr_index("nateprich/beta", limit=10)

    assert truncated is False
    assert index["nateprich/beta#10"]["number"] == 10
    assert "nateprich/beta#12" not in index
    assert [row["number"] for row in index.all_rows] == [10]


def test_the_index_still_reports_truncation_from_the_whole_scan(monkeypatch):
    """Dropping foreign rows must not hide that the window was full."""
    limit = funnel.MERGED_PR_SCAN_LIMIT
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads([
            pr_row(n, "ticket/{}".format(n), **FORK)
            for n in range(1, limit + 2)
        ])
    )

    index, truncated = funnel.ticket_pr_index("nateprich/beta")

    assert truncated is True
    assert len(index) == 0


@pytest.mark.parametrize("trust", NOT_THE_FUNNELS)
def test_a_merged_foreign_pr_does_not_finish_an_open_ticket(
        monkeypatch, trust):
    rows = [pr_row(30, "ticket/10", state="MERGED", **trust),
            pr_row(11, "ticket/11", state="MERGED")]
    monkeypatch.setattr(funnel, "gh_graphql", repo_graphql_reads(rows))

    facts = funnel.merged_pr_facts([ticket(10), ticket(11)])

    assert facts.ticket_refs == frozenset({"nateprich/beta#11"})


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


def test_a_page_never_asks_for_more_prs_than_the_measured_page_size(monkeypatch):
    """#1217: the document has to stay inside GitHub's gateway timeout.

    One page of 100 PRs per member repository, each with its comment tail,
    body, refs and check rollup, stopped being served on 2026-09-21 — HTTP
    502/504 at about 37 seconds, six attempts out of six. The scan limit still
    decides how many rows arrive; this decides only how many are asked for at
    once, so a limit above the page size must page rather than widen the
    request.
    """
    assert funnel.PR_GRAPHQL_PR_PAGE_SIZE < funnel.PR_GRAPHQL_PAGE_SIZE
    assert funnel.MERGED_PR_SCAN_LIMIT > funnel.PR_GRAPHQL_PR_PAGE_SIZE

    calls = []
    count = funnel.PR_GRAPHQL_PR_PAGE_SIZE + 5
    rows = [pr_row(n, "ticket/{}".format(n)) for n in range(2000, 2000 + count)]
    monkeypatch.setattr(
        funnel, "gh_graphql", repo_graphql_reads(rows, [], calls)
    )

    found = funnel.ticket_pr_facts(
        [ticket(n) for n in range(2000, 2000 + count)]
    )

    requested = [
        int(query.split("pullRequests(first: ", 1)[1].split(",", 1)[0])
        for query, _ in calls
    ]
    assert requested, "the batched read made no request"
    assert max(requested) <= funnel.PR_GRAPHQL_PR_PAGE_SIZE
    assert len(calls) > 1, "a board past one page must page, not widen"
    assert len(found) == count, "paging must not lose rows"
