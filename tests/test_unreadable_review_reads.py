"""An unreadable verdict or CI-run read stops the review packet (#2194).

Plan #1747: unreadable or unprovable candidates fail closed with a recorded
reason, never a silent skip or an empty packet. Before #2194 three reads did
the opposite:

- ``latest_verdict`` read a failed comment read as no verdict, so ``collect``
  built a packet with ``verdict: null`` and a passing precheck, and a head
  that already had a verdict was judged again without its prior rejection;
- ``fetch_ci_runs`` read a failed run list as no runs, so a merged overlap
  that a green run covers rejected as stale on one bad read;
- ``_row_verdict`` ignored a fact's own comment tail and read again, and when
  that second read failed ``merge_blockers`` said "no review verdict
  recorded".

An empty comment list or run list is still an answer, and reads as none.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402
from engine import review  # noqa: E402

REPO = "owner/repo"
SHA = "abc123def456"
OTHER_SHA = "7890fedcba98"
OWNER = "nateprich"
NOW = datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)
HEAD_DATE = "2026-09-13T12:00:00Z"
NEWER = "2026-09-13T13:00:00Z"
COVERING = "2026-09-13T13:30:00Z"


def verdict_comment(**kw):
    """A trusted ``--json comments`` row carrying one review verdict."""
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA,
            "blocking": []}
    body.update(kw)
    return {"body": funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(body)
            + "\n```", "author": {"login": OWNER},
            "createdAt": "2026-09-13T12:30:00Z"}


def ci_run(started_at=COVERING):
    """One ``gh run list`` row: a green pull_request run on the head."""
    return {"databaseId": 11, "event": "pull_request", "headSha": SHA,
            "headBranch": "ticket/9", "conclusion": "success",
            "status": "completed", "createdAt": started_at,
            "startedAt": started_at, "updatedAt": started_at}


#: A newer merge touching the PR's file: the merged-overlap row then needs a
#: green run on the head started after it (#1019).
OVERLAP = [{"number": 5, "mergedAt": NEWER, "title": "other",
            "headRefName": "ticket/5", "files": [{"path": "funnel.py"}]}]


def wire_collect(monkeypatch, *, comments, runs, merged=()):
    """Stub every packet read except the verdict and run-list reads.

    Those two go through the real ``fetch_verdict``/``latest_verdict`` and
    ``fetch_ci_runs``; only ``gh`` is faked, so ``comments`` and ``runs`` are
    the wire answers (``None`` is a failed ``gh`` call).
    """
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: {
        "number": 7, "title": "do the thing", "headRefName": "ticket/9",
        "headRefOid": SHA, "baseRefName": "main", "state": "OPEN",
        "mergedAt": None, "closedAt": None, "mergeable": "MERGEABLE",
        "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS",
                               "status": "COMPLETED"}],
        "commits": [{"oid": SHA, "committedDate": HEAD_DATE}],
        "files": [{"path": "funnel.py"}],
        # The funnel's own PR: same-repository head, owner author (#1794).
        "isCrossRepository": False,
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "owner"},
        "author": {"login": OWNER},
    })
    monkeypatch.setattr(review, "fetch_scope", lambda *args: (
        (_ for _ in ()).throw(funnel.GitHubError("compare unavailable"))))
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(review, "fetch_ticket", lambda repo, number: {
        "ref": REPO + "#9", "number": 9, "title": "the ticket", "url": "",
        "body": "Parent: #1.\n\nWhat: do the thing.\n\nRisk: standard",
        "risk": "standard"})
    monkeypatch.setattr(review, "fetch_plan_md",
                        lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs",
                        lambda repo: [dict(row) for row in merged])
    monkeypatch.setattr(review, "fetch_pr_comments", lambda repo, pr: {
        "status": "empty", "message": "No PR comments.", "comments": []})
    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "load_project_items_by_refs",
                        lambda refs, member_repo_names=None: [])
    monkeypatch.setattr(funnel, "load_regression_items",
                        lambda member_repo_names=None: [])

    def gh_json(*args):
        if args[:3] == ("gh", "pr", "view") and "comments" in args:
            return comments
        if args[:3] == ("gh", "run", "list"):
            return runs
        raise AssertionError("unexpected gh read: {}".format(args))

    monkeypatch.setattr(funnel, "_gh_json", gh_json)


def collect():
    return review.collect(REPO, 7, items_loader=lambda: [], now=NOW)


# -- the verdict read ----------------------------------------------------------

def test_collect_stops_when_the_verdict_comment_read_fails(monkeypatch):
    """Main built ``verdict: null`` with a passing precheck here."""
    wire_collect(monkeypatch, comments=None, runs=[])
    with pytest.raises(funnel.GitHubError, match="verdict"):
        collect()


@pytest.mark.parametrize("payload", [
    {}, {"comments": None}, {"comments": "nonsense"}, ["not", "an", "object"],
])
def test_collect_stops_when_the_verdict_comment_read_is_malformed(
        monkeypatch, payload):
    wire_collect(monkeypatch, comments=payload, runs=[])
    with pytest.raises(funnel.GitHubError, match="verdict"):
        collect()


def test_an_empty_comment_list_still_reads_as_no_verdict(monkeypatch):
    wire_collect(monkeypatch, comments={"comments": []}, runs=[])
    found = collect()
    assert found["verdict"] is None
    assert found["precheck"] == {"pass": True, "reasons": []}


def test_a_readable_prior_rejection_reaches_the_packet(monkeypatch):
    wire_collect(monkeypatch, runs=[], comments={"comments": [
        verdict_comment(verdict="rejected", head_sha=OTHER_SHA,
                        blocking=["fix the thing"])]})
    found = collect()
    assert found["verdict"]["verdict"] == "rejected"
    assert found["verdict"]["head_sha"] == OTHER_SHA


def test_the_cli_records_no_packet_when_the_verdict_read_fails(
        monkeypatch, capsys):
    """The runner's review-packet failure path: exit 1, nothing on stdout."""
    wire_collect(monkeypatch, comments=None, runs=[])
    monkeypatch.setattr(funnel, "resolve_repo", lambda repo: repo or REPO)
    assert review.main(["7", "--repo", REPO]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("review-packet: ")
    assert "verdict" in captured.err


def test_latest_verdict_raises_on_a_failed_read_and_reads_empty_as_none(
        monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)
    with pytest.raises(funnel.GitHubError, match="PR #7 in owner/repo"):
        funnel.latest_verdict(REPO, 7)
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})
    assert funnel.latest_verdict(REPO, 7) is None


# -- the CI-run read -----------------------------------------------------------

def test_collect_stops_when_the_run_list_read_fails_on_a_merged_overlap(
        monkeypatch):
    """Main rejected the head as a merged overlap on this one bad read."""
    wire_collect(monkeypatch, comments={"comments": []}, runs=None,
                 merged=OVERLAP)
    with pytest.raises(funnel.GitHubError, match="CI runs"):
        collect()


def test_a_readable_covering_run_passes_the_same_overlap(monkeypatch):
    wire_collect(monkeypatch, comments={"comments": []}, runs=[ci_run()],
                 merged=OVERLAP)
    found = collect()
    assert found["ci"]["green_run_at"] == COVERING
    assert found["precheck"] == {"pass": True, "reasons": []}


@pytest.mark.parametrize("payload", [None, {"runs": []}, "nonsense"])
def test_fetch_ci_runs_raises_on_a_failed_or_malformed_read(
        monkeypatch, payload):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: payload)
    with pytest.raises(funnel.GitHubError, match="ticket/9 in owner/repo"):
        review.fetch_ci_runs(REPO, "ticket/9")


def test_fetch_ci_runs_reads_an_empty_list_as_no_runs(monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: [])
    assert review.fetch_ci_runs(REPO, "ticket/9") == []


# -- the merge gate's verdict read ---------------------------------------------

def gate_items():
    project = Item(repo=REPO, number=1, title="p", url="", state="OPEN",
                   status="Building", klass="Improve", origin="agent",
                   risk="standard", needs="none", item_id="project-id",
                   children_total=1, children_done=0)
    ticket = Item(repo=REPO, number=9, title="t", url="", state="OPEN",
                  parent=REPO + "#1", origin="agent", risk="standard",
                  needs="none")
    return [project, ticket]


def gate_fact(**kw):
    data = {"number": 5, "state": "OPEN", "headRefName": "ticket/9",
            "headRefOid": SHA, "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}],
            "isCrossRepository": False, "author": {"login": OWNER}}
    data.update(kw)
    return data


def record_gh_calls(monkeypatch, answer=None):
    """Record every GitHub call; each one answers ``answer``."""
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return answer

    monkeypatch.setattr(funnel, "_gh_json", record)
    monkeypatch.setattr(funnel, "gh_graphql", record)
    monkeypatch.setattr(funnel, "_run_gh", record)
    return calls


def test_the_merge_gate_reads_the_facts_comment_tail_without_another_call(
        monkeypatch):
    """Main read the PR's comments again and, failing, refused the merge."""
    calls = record_gh_calls(monkeypatch)
    fact = gate_fact(comments=[verdict_comment()])
    assert "verdict" not in fact
    assert funnel.merge_blockers(REPO, 5, gate_items(), NOW,
                                 pr_fact=fact) == []
    assert calls == []


def test_the_merge_gate_names_an_unreadable_verdict_as_unreadable(
        monkeypatch):
    calls = record_gh_calls(monkeypatch)
    why = funnel.merge_blockers(REPO, 5, gate_items(), NOW,
                                pr_fact=gate_fact())
    assert len(calls) == 1
    assert "no review verdict recorded" not in why
    assert why == [
        "review verdict unreadable: could not read the review verdict on "
        "PR #5 in owner/repo"]


def test_the_merge_gate_still_reads_an_empty_tail_as_no_verdict(monkeypatch):
    calls = record_gh_calls(monkeypatch)
    why = funnel.merge_blockers(REPO, 5, gate_items(), NOW,
                                pr_fact=gate_fact(comments=[]))
    assert calls == []
    assert why == ["no review verdict recorded"]


def test_row_verdict_prefers_the_batch_verdict_then_the_comment_tail(
        monkeypatch):
    calls = record_gh_calls(monkeypatch)
    rejected = funnel._latest_verdict_from_comments(
        [verdict_comment(verdict="rejected")])
    tail = [verdict_comment()]
    # The batch's own verdict stands, even beside a tail that disagrees.
    assert funnel._row_verdict(
        {"number": 5, "verdict": rejected, "comments": tail}, REPO) == rejected
    assert funnel._row_verdict(
        {"number": 5, "verdict": None, "comments": tail}, REPO) is None
    assert funnel._row_verdict(
        {"number": 5, "comments": tail}, REPO)["verdict"] == "approved"
    assert calls == []
    # Only a row with neither reads, and a failed read raises.
    with pytest.raises(funnel.GitHubError):
        funnel._row_verdict({"number": 5}, REPO)
    assert len(calls) == 1
