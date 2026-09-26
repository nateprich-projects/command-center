"""Review packet and review-apply read history only where it is used (#1621).

Both used to load the whole board with every item's history: about a minute
and a hundred GraphQL points each time. They now load the board without
history and read it back only for regression items (the rejected-merge
counter) and, before a merge, for the branch ticket and its project (the
drift fallback when the project closes itself).

These tests serve one fixture board, with real history, through a fake
``gh_graphql`` and check that the old full load and the new narrow load give
the same packet and the same merge decisions and writes.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import review, review_apply  # noqa: E402

REPO = "owner/repo"
SHA = "abc123def456"
PR = 5
# review-apply stamps the gate with the real clock, so the board is dated
# relative to it, with days of margin either side of the counter's window.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def status_event(status, at):
    return {
        "__typename": "ProjectV2ItemStatusChangedEvent",
        "createdAt": iso(at),
        "previousStatus": None,
        "status": status,
        "project": {"number": funnel.PROJECT_NUMBER},
    }


class Board:
    """A Project board served through a fake ``gh_graphql``.

    The list page is the compact live shape (no history). History and child
    timestamps arrive only through the detail queries, which are recorded.
    """

    def __init__(self, recent_regressions=0):
        self.nodes = []
        self.history = {}
        self.children = {}
        self.detail_calls = []
        self.writes = []
        self.fail_history = False

        building_at = NOW - timedelta(days=5)
        self.add(1, "the project", status="Building", klass="Maintenance",
                 origin="agent", children=(1, 0),
                 events=[status_event("Ready", NOW - timedelta(days=6)),
                         status_event("Building", building_at)],
                 child_rows=[{"createdAt": iso(NOW - timedelta(days=4)),
                              "closedAt": None}])
        self.add(9, "the ticket", status="Building", parent=1, risk="high",
                 lock=iso(NOW - timedelta(hours=2)),
                 events=[status_event("Building", NOW - timedelta(days=4))])
        self.add(2, "another project", status="Ready", klass="New",
                 origin="Nate", children=(1, 0),
                 events=[status_event("Ready", NOW - timedelta(days=2))],
                 child_rows=[{"createdAt": iso(NOW - timedelta(days=1)),
                              "closedAt": None}])
        self.add(10, "another ticket", status="Ready", parent=2,
                 events=[status_event("Ready", NOW - timedelta(days=1))])
        # One old regression outside the counter's window, one whose status
        # has no event in this Project, and the recent ones under test.
        self.add(100, "{}40 broke the brief".format(funnel.REGRESSION_PREFIX),
                 status="Ideas",
                 events=[status_event("Ideas", NOW - timedelta(days=40))])
        self.add(101, "{}41 broke begin".format(funnel.REGRESSION_PREFIX),
                 status="Ideas", events=[])
        for n in range(recent_regressions):
            self.add(110 + n,
                     "{}{} broke a gate".format(funnel.REGRESSION_PREFIX, 60 + n),
                     status="Ideas",
                     events=[status_event(
                         "Ideas", NOW - timedelta(days=1 + n))])

    def add(self, number, title, *, status, klass=None, origin=None,
            risk="standard", parent=None, children=(0, 0), lock=None,
            events=(), child_rows=None):
        item_id = "item-{}".format(number)
        self.nodes.append({
            "id": item_id,
            "lock": {"text": lock} if lock else None,
            "status": {"name": status},
            "class": {"name": klass} if klass else None,
            "origin": {"name": origin} if origin else None,
            "risk": {"name": risk},
            "pinned": None,
            "needs": {"name": "none"},
            "content": {
                "number": number,
                "title": title,
                "url": "https://github.com/{}/issues/{}".format(REPO, number),
                "body": None,
                "state": "OPEN",
                "stateReason": None,
                "createdAt": iso(NOW - timedelta(days=50)),
                "closedAt": None,
                "repository": {"nameWithOwner": REPO},
                "labels": {"nodes": []},
                "assignees": {"nodes": []},
                "parent": (
                    {"number": parent, "repository": {"nameWithOwner": REPO}}
                    if parent else None
                ),
                "subIssuesSummary": {
                    "total": children[0], "completed": children[1]},
                "blockedBy": {"totalCount": 0, "nodes": []},
            },
        })
        self.history[item_id] = list(events)
        if child_rows is not None:
            self.children[item_id] = list(child_rows)

    def graphql(self, query, **variables):
        if query == funnel.ITEM_QUERY:
            return {"user": {"projectV2": {"items": {
                "nodes": self.nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        if query in (funnel.ITEM_DETAILS_QUERY,
                     funnel.ITEM_TIMELINE_DETAILS_QUERY):
            self.detail_calls.append(
                (list(variables["ids"]), list(variables.get("childIds", []))))
            if self.fail_history:
                raise funnel.GitHubError("HTTP 502 from the history read")
            history = [
                {"id": item_id, "content": {"timelineItems": {
                    "nodes": self.history[item_id]}}}
                for item_id in variables["ids"]
            ]
            if query == funnel.ITEM_TIMELINE_DETAILS_QUERY:
                return {"nodes": history}
            return {
                "history": history,
                "children": [
                    {"id": item_id, "content": {"subIssues": {
                        "nodes": self.children.get(item_id, [])}}}
                    for item_id in variables["childIds"]
                ],
            }
        if "ProjectV2SingleSelectField" in query:
            return {"node": {"options": [
                {"id": "opt-{}".format(name), "name": name}
                for name in ("Ideas", "Shaped", "Ready", "Building", "Done",
                             "Parked")
            ]}}
        if query == funnel.SET_FIELD:
            self.writes.append(("set-field", variables["item"],
                                variables["option"]))
            return {}
        if query == funnel.SET_LOCK:
            self.writes.append(("set-lock", variables["item"],
                                variables["value"]))
            return {}
        raise AssertionError("unexpected GraphQL read: {}".format(query[:80]))

    def run_gh(self, args, **kwargs):
        self.writes.append(tuple(args))
        return subprocess.CompletedProcess(list(args), 0, "", "")

    def gh_json(self, *args):
        if args[:3] == ("gh", "issue", "view") and args[-1] == "state":
            return {"state": "OPEN"}
        raise AssertionError("unexpected gh read: {}".format(args))


def merge_fact():
    comments = [{"body": funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps({
        "verdict": "approved", "ci": "green", "head_sha": SHA,
        "blocking": []}) + "\n```"}]
    return {
        "number": PR,
        "title": "Do the ticket",
        "state": "OPEN",
        "headRefName": "ticket/9",
        "headRefOid": SHA,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
        "comments": comments,
        "verdict": funnel._latest_verdict_from_comments(comments),
    }


def install(monkeypatch, board):
    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "resolve_repo", lambda repo: repo or REPO)
    monkeypatch.setattr(funnel, "gh_graphql", board.graphql)
    monkeypatch.setattr(funnel, "_run_gh", board.run_gh)
    monkeypatch.setattr(funnel, "_gh_json", board.gh_json)
    monkeypatch.setattr(funnel, "_pr_fact_for_number",
                        lambda repo, number, **kwargs: merge_fact())
    # Drift's own histories are read separately; a ticket added after the
    # approval is only drift when the project's `status_since` (the Building
    # fallback) is known, so the marker proves the project was hydrated.
    monkeypatch.setattr(
        funnel, "fetch_drift_facts",
        lambda item: funnel.DriftFacts(
            ticket_created_at=(NOW - timedelta(days=1),)))

    def tickets_of(project):
        return [item for item in funnel.load_items(include_details=False)
                if item.parent == project.ref]

    monkeypatch.setattr(funnel, "closed_itself_tickets", tickets_of)


# -- review packet -------------------------------------------------------------

def wire_packet(monkeypatch):
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: {
        "number": 7, "title": "do the thing", "headRefName": "ticket/9",
        "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
        "mergedAt": None, "closedAt": None, "body": "Closes #9",
        "files": [], "closingIssuesReferences": []})
    monkeypatch.setattr(review, "fetch_scope", lambda *args: (
        (_ for _ in ()).throw(funnel.GitHubError("compare unavailable"))))
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(review, "fetch_ticket", lambda repo, number: {
        "ref": "{}#{}".format(repo, number), "number": number,
        "title": "the ticket", "url": "", "body": "Parent: #1.",
        "risk": None})
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(review, "fetch_pr_comments", lambda repo, pr: {
        "status": "empty", "message": "No PR comments.", "comments": []})


@pytest.mark.parametrize("recent", [0, funnel.REJECTED_MERGE_ALARM])
def test_packet_is_identical_to_the_full_history_load(monkeypatch, recent):
    wire_packet(monkeypatch)
    old_board = Board(recent_regressions=recent)
    install(monkeypatch, old_board)
    old = review.collect(REPO, 7, items_loader=lambda: funnel.load_items(),
                         now=NOW)

    new_board = Board(recent_regressions=recent)
    install(monkeypatch, new_board)
    new = review.collect(REPO, 7, now=NOW)

    assert json.dumps(new, sort_keys=True) == json.dumps(old, sort_keys=True)
    assert new["stop_auto_merging"]["count"] == recent
    assert new["stop_auto_merging"]["stop_auto_merging"] is bool(recent)
    assert new["tickets"][0]["risk"] == "high"


def test_packet_reads_history_only_for_regression_items(monkeypatch):
    wire_packet(monkeypatch)
    board = Board(recent_regressions=funnel.REJECTED_MERGE_ALARM)
    install(monkeypatch, board)

    review.collect(REPO, 7, now=NOW)

    assert board.detail_calls == [(
        ["item-100", "item-101", "item-110", "item-111", "item-112"], [])]


def test_an_unreadable_regression_history_fails_the_packet(
        monkeypatch, capsys):
    wire_packet(monkeypatch)
    board = Board(recent_regressions=1)
    board.fail_history = True
    install(monkeypatch, board)

    with pytest.raises(funnel.GitHubError, match="HTTP 502"):
        review.collect(REPO, 7, now=NOW)
    assert review.main(["7", "--repo", REPO]) == 1
    assert "HTTP 502" in capsys.readouterr().err


# -- review-apply ---------------------------------------------------------------

def run_old_merge(monkeypatch, capsys, board):
    """What review-apply did before #1621: the full load, then the gate."""
    install(monkeypatch, board)
    code = funnel.cmd_merge(
        funnel.load_items(), datetime.now(timezone.utc), REPO, PR, True)
    return code, capsys.readouterr()


def run_new_merge(monkeypatch, capsys, board):
    install(monkeypatch, board)
    monkeypatch.setattr(funnel, "cmd_review", lambda *args, **kwargs: 0)
    monkeypatch.setattr(review_apply.review, "fetch_pr",
                        lambda repo, pr: {"state": "OPEN", "headRefOid": SHA})
    code = review_apply.apply_approved(
        REPO, PR, [], None, "green", None, None, approved_head=SHA)
    return code, capsys.readouterr()


def refusals(err):
    return [line for line in err.splitlines() if line.startswith("  - ")]


def test_tripped_counter_refuses_the_same_way(monkeypatch, capsys):
    old_board = Board(recent_regressions=funnel.REJECTED_MERGE_ALARM)
    old_code, old_out = run_old_merge(monkeypatch, capsys, old_board)
    new_board = Board(recent_regressions=funnel.REJECTED_MERGE_ALARM)
    new_code, new_out = run_new_merge(monkeypatch, capsys, new_board)

    assert old_code == new_code == 1
    assert refusals(new_out.err) == refusals(old_out.err)
    assert any("auto-merging is stopped: 3 rejected merges" in line
               for line in refusals(new_out.err))
    assert new_board.writes == old_board.writes == []


def test_clean_merge_closes_the_project_with_the_same_writes(
        monkeypatch, capsys):
    old_board = Board()
    old_code, old_out = run_old_merge(monkeypatch, capsys, old_board)
    new_board = Board()
    new_code, new_out = run_new_merge(monkeypatch, capsys, new_board)

    assert old_code == new_code == 0
    assert refusals(old_out.err) == refusals(new_out.err) == []
    assert new_board.writes == old_board.writes
    kinds = [write[:4] for write in new_board.writes]
    assert ("gh", "pr", "merge", str(PR)) in kinds
    assert ("set-lock", "item-9", "") in new_board.writes
    assert ("set-field", "item-1", "opt-Done") in new_board.writes
    comment = next(write for write in new_board.writes
                   if write[:4] == ("gh", "issue", "comment", "1"))
    # The late-ticket drift needs the project's Building time, which comes
    # only from its hydrated history.
    assert funnel.DRIFT_LATE_TICKET in comment[-1]


def test_merge_reads_history_only_for_the_subject_and_regressions(
        monkeypatch, capsys):
    board = Board(recent_regressions=1)
    run_new_merge(monkeypatch, capsys, board)

    assert board.detail_calls == [
        (["item-9", "item-1"], ["item-1"]),
        (["item-100", "item-101", "item-110"], []),
    ]


def test_an_unreadable_subject_history_refuses_the_merge(monkeypatch, capsys):
    board = Board()
    board.fail_history = True

    code, out = run_new_merge(monkeypatch, capsys, board)

    assert code == 1
    assert "HTTP 502" in out.err
    assert board.writes == []


def test_an_unreadable_regression_history_refuses_the_merge(
        monkeypatch, capsys):
    board = Board(recent_regressions=1)
    real = board.graphql

    def fail_second_history(query, **variables):
        if (query in (funnel.ITEM_DETAILS_QUERY,
                      funnel.ITEM_TIMELINE_DETAILS_QUERY)
                and board.detail_calls):
            board.fail_history = True
        return real(query, **variables)

    install(monkeypatch, board)
    monkeypatch.setattr(funnel, "cmd_review", lambda *args, **kwargs: 0)
    monkeypatch.setattr(review_apply.review, "fetch_pr",
                        lambda repo, pr: {"state": "OPEN", "headRefOid": SHA})
    monkeypatch.setattr(funnel, "gh_graphql", fail_second_history)

    code = review_apply.apply_approved(
        REPO, PR, [], None, "green", None, None, approved_head=SHA)

    assert code == 1
    assert any("could not read the rejected-merge history" in line
               for line in refusals(capsys.readouterr().err))
    assert board.writes == []
