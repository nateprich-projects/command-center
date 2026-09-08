"""The model decides approval; Python decides merge.

A model adds value judging whether a diff matches the plan. It adds none by
being the component that types `gh pr merge` — and being that component is what
makes an unattended merge impossible to audit afterwards, which is why v0 is
still unaccepted. So the reviewer writes a verdict in one checkable shape, and
every merge condition is enforced outside the model.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
REPO = "owner/repo"
SHA = "abc123def456"


def verdict(**kw):
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA, "blocking": []}
    body.update(kw)
    return funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(body) + "\n```"


def items():
    project = Item(repo=REPO, number=1, title="p", url="", state="OPEN",
                   status="Building", klass="Improve", children_total=1)
    ticket = Item(repo=REPO, number=9, title="t", url="", state="OPEN",
                  parent=REPO + "#1")
    return [project, ticket]


def wire(monkeypatch, pr_json, comments):
    def fake(*args):
        if "comments" in args:
            return {"comments": [{"body": b} for b in comments]}
        return pr_json
    monkeypatch.setattr(funnel, "_gh_json", fake)


def pr(**kw):
    data = {"state": "OPEN", "headRefName": "ticket/9", "headRefOid": SHA,
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}]}
    data.update(kw)
    return data


# -- the verdict format -------------------------------------------------------

def test_a_verdict_is_read_out_of_the_comment():
    found = funnel.parse_verdict(verdict(note="looks right"))
    assert found["verdict"] == "approved" and found["head_sha"] == SHA


def test_ordinary_prose_is_not_a_verdict():
    assert funnel.parse_verdict("Looks good to me, merging.") is None


def test_a_malformed_verdict_is_not_half_read():
    assert funnel.parse_verdict(funnel.REVIEW_MARKER + "\n{not json") is None


def test_the_newest_verdict_wins(monkeypatch):
    """A re-review after a fix is a fresh read; the older one must not linger."""
    wire(monkeypatch, pr(), [verdict(verdict="rejected"), verdict()])
    assert funnel.latest_verdict(REPO, 5)["verdict"] == "approved"


# -- the gate -----------------------------------------------------------------

def test_everything_in_order_merges(monkeypatch):
    wire(monkeypatch, pr(), [verdict()])
    assert funnel.merge_blockers(REPO, 5, items(), NOW) == []


def test_a_conflicting_branch_blocks_with_a_rebase_reason(monkeypatch):
    wire(monkeypatch, pr(mergeable="CONFLICTING"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any(
        "ticket/9" in reason
        and "conflicting" in reason
        and "base" in reason
        and "rebase" in reason
        for reason in why
    )


def test_unknown_mergeability_blocks_with_retry_reason(monkeypatch):
    wire(monkeypatch, pr(mergeable="UNKNOWN"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any("not been computed" in reason and "retry" in reason for reason in why)
    assert not any("rebase" in reason for reason in why)


def test_missing_mergeability_blocks_with_retry_reason(monkeypatch):
    data = pr()
    del data["mergeable"]
    wire(monkeypatch, data, [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any("not been computed" in reason and "retry" in reason for reason in why)


def test_a_mixed_review_and_provenance_comment_still_allows_merge(monkeypatch):
    mixed = verdict() + "\n\n" + funnel.provenance_block(
        "agent", at=NOW, run="run-review", agent="zcode"
    )
    wire(monkeypatch, pr(), [mixed])
    assert funnel.merge_blockers(REPO, 5, items(), NOW) == []


def test_a_commit_pushed_after_approval_blocks_the_merge(monkeypatch):
    """The decisive check. Without it an approval authorises a diff it never saw:
    approve, push anything, merge."""
    wire(monkeypatch, pr(headRefOid="9999newcommit"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any("not the head" in w for w in why)


def test_no_verdict_blocks(monkeypatch):
    wire(monkeypatch, pr(), [])
    assert "no review verdict recorded" in funnel.merge_blockers(REPO, 5, items(), NOW)


def test_a_rejected_verdict_blocks(monkeypatch):
    wire(monkeypatch, pr(), [verdict(verdict="rejected")])
    assert any("rejected" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_red_ci_blocks(monkeypatch):
    wire(monkeypatch, pr(statusCheckRollup=[
        {"name": "tests", "conclusion": "FAILURE"}]), [verdict()])
    assert any("CI not green" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_absent_ci_blocks_rather_than_passing_vacuously(monkeypatch):
    """No checks is not the same as checks passing."""
    wire(monkeypatch, pr(statusCheckRollup=[]), [verdict()])
    assert any("no CI checks" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_a_branch_that_is_not_a_ticket_blocks(monkeypatch):
    wire(monkeypatch, pr(headRefName="feature/whatever"), [verdict()])
    assert any("not a ticket/" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_a_ticket_whose_project_is_not_building_blocks(monkeypatch):
    rows = items()
    rows[0].status = "Ready"
    wire(monkeypatch, pr(), [verdict()])
    assert any("not Building" in w for w in funnel.merge_blockers(REPO, 5, rows, NOW))


def test_every_failure_is_reported_not_just_the_first(monkeypatch):
    """A gate that says only 'no' makes the reviewer guess which one to fix."""
    wire(monkeypatch, pr(headRefName="nope", statusCheckRollup=[]), [])
    assert len(funnel.merge_blockers(REPO, 5, items(), NOW)) >= 3


# -- closing the ticket the PR finished ---------------------------------------
#
# `gh pr merge` closes an issue only when the PR body carries a `Closes #N`
# link, written by hand. On 2026-09-08 thirteen merged ticket PRs omitted it,
# and each left its ticket open, startable, and re-served to the engineer on
# every run — 38 of 176 Codex runs that day did nothing but re-verify a PR that
# had already merged. These tests hold the close to the merge itself.

def _merge_wired(monkeypatch, issue_state="OPEN", close_rc=0, close_err=""):
    """Run cmd_merge past a clean gate, recording the subprocesses it runs."""
    calls = []

    def fake_gh_json(*args):
        if "issue" in args and "view" in args:
            return {"state": issue_state}
        if "comments" in args:
            return {"comments": [{"body": verdict()}]}
        return pr()

    def fake_run(argv, **kwargs):
        calls.append(argv)
        rc = close_rc if argv[:2] == ["gh", "issue"] else 0
        err = close_err if argv[:2] == ["gh", "issue"] else ""
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": err})()

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    monkeypatch.setattr(funnel.subprocess, "run", fake_run)
    rc = funnel.cmd_merge(items(), NOW, REPO, 7, True)
    return rc, calls


def test_merge_closes_the_ticket_the_branch_names(monkeypatch):
    rc, calls = _merge_wired(monkeypatch)
    assert rc == 0
    assert ["gh", "issue", "close", "9", "--repo", REPO,
            "--reason", "completed"] in calls


def test_merge_does_not_reclose_a_ticket_github_already_closed(monkeypatch):
    rc, calls = _merge_wired(monkeypatch, issue_state="CLOSED")
    assert rc == 0
    assert not [c for c in calls if c[:3] == ["gh", "issue", "close"]]


def test_a_failed_close_says_the_merge_landed_and_still_exits_non_zero(
        monkeypatch, capsys):
    # #236's plan: report loudly *and* exit non-zero. A ticket left open is the
    # failure this close exists to prevent, so a silent 0 would hide it. A retry
    # is harmless — the gate refuses a PR that is no longer open.
    rc, _ = _merge_wired(monkeypatch, close_rc=1, close_err="gh: nope")
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not be closed" in err and "owner/repo#9" in err
    assert "the merge succeeded" in err


def test_ticket_ref_from_branch_is_the_one_parser():
    assert funnel.ticket_ref_from_branch(REPO, "ticket/9") == REPO + "#9"
    assert funnel.ticket_ref_from_branch(REPO, "main") is None
    assert funnel.ticket_ref_from_branch(REPO, "ticket/not-a-number") is None
