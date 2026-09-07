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
