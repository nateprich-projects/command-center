"""Fixture-driven tests for GitHub-derived ticket outcome records."""

from __future__ import annotations

import base64
import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import outcomes  # noqa: E402


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
REPO = "owner/repo"
SHA = "abc123"


def ticket(number=42, **extra):
    row = {
        "repo": REPO,
        "number": number,
        "title": "Ticket {}".format(number),
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "closedAt": "2026-09-10T10:00:00Z",
        "stateReason": "COMPLETED",
    }
    row.update(extra)
    return row


def pr(number, *, created="2026-09-10T08:00:00Z", state="CLOSED",
       merged=None, head=SHA, checks=None, branch="ticket/42"):
    return {
        "number": number,
        "state": "MERGED" if merged else state,
        "url": "https://github.com/{}/pull/{}".format(REPO, number),
        "headRefName": branch,
        "headRefOid": head,
        "createdAt": created,
        "closedAt": merged or "2026-09-10T09:00:00Z",
        "mergedAt": merged,
        "statusCheckRollup": checks,
    }


def verdict_comment(verdict, *, at="2026-09-10T08:30:00Z", head=SHA,
                    author="nateprich"):
    body = funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps({
        "verdict": verdict,
        "ci": "green",
        "head_sha": head,
        "blocking": [],
        "reviewed_at": at,
    }) + "\n```\n\n" + agent_provenance(at)
    return {"author": {"login": author}, "body": body, "createdAt": at}


def agent_provenance(at="2026-09-10T08:30:00Z"):
    return funnel.provenance_block(
        "agent", at=outcomes._timestamp(at), run="run-1", agent="muse"
    )


def test_attempts_and_turns_cover_every_pr_on_one_ticket_branch():
    first = pr(10, created="2026-09-10T08:00:00Z", checks=[
        {"name": "pytest", "conclusion": "SUCCESS"},
    ])
    second = pr(11, created="2026-09-10T09:00:00Z", merged="2026-09-10T09:30:00Z",
                checks=[{"name": "pytest", "conclusion": "SUCCESS"}])
    record = outcomes.derive_outcome(
        ticket(),
        [first, second],
        {
            10: {"comments": [verdict_comment("rejected")]},
            11: {"comments": [verdict_comment("approved", at="2026-09-10T09:15:00Z")]},
        },
        now=NOW,
    )

    assert record["attempts"] == 2
    assert record["turns"] == 2
    assert record["ci_green"] is True
    assert record["ci_result"] == "green"
    assert record["review_result"] == "approved"
    assert record["merged"] is True
    assert record["merged_prs"] == [11]
    assert [row["number"] for row in record["prs"]] == [10, 11]


def test_latest_verdict_is_chronological_not_pr_list_order():
    older = pr(10, created="2026-09-10T08:00:00Z")
    newer = pr(11, created="2026-09-10T09:00:00Z")
    record = outcomes.derive_outcome(
        ticket(),
        [newer, older],
        {
            10: {"comments": [verdict_comment("approved", at="2026-09-10T08:30:00Z")]},
            11: {"comments": [verdict_comment("rejected", at="2026-09-10T09:30:00Z")]},
        },
        now=NOW,
    )

    assert record["review_result"] == "rejected"
    assert record["turns"] == 2


def test_no_verdicts_are_unknown_turns_not_zero():
    record = outcomes.derive_outcome(
        ticket(),
        [pr(10, checks=[{"name": "pytest", "conclusion": "SUCCESS"}])],
        {10: {"comments": []}},
        now=NOW,
    )

    assert record["turns"] is None
    assert record["review_result"] is None
    assert record["merged"] is False


@pytest.mark.parametrize(
    "checks, expected",
    [
        ([{"conclusion": "FAILURE"}], False),
        ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], True),
        ([{"conclusion": "IN_PROGRESS"}], None),
        ([], None),
        (None, None),
    ],
)
def test_ci_is_aggregated_fail_closed(checks, expected):
    assert outcomes.ci_green(checks) is expected


def test_reopen_after_a_merge_is_derived_from_issue_events():
    merged = pr(10, merged="2026-09-10T09:00:00Z")
    record = outcomes.derive_outcome(
        ticket(), [merged], {10: {"comments": [verdict_comment("approved")] }},
        issue_events=[
            {"event": "closed", "created_at": "2026-09-10T09:00:01Z"},
            {"event": "reopened", "created_at": "2026-09-10T09:30:00Z"},
        ],
        now=NOW,
    )

    assert record["reopened_after_merge"] is True


def test_direct_nate_comment_counts_but_agent_comment_under_his_login_does_not():
    direct = {"author": {"login": "nateprich"}, "body": "Please change the scope."}
    agent = {
        "author": {"login": "nateprich"},
        "body": "reviewed by the automation\n\n" + agent_provenance(),
    }
    no_intervention = outcomes.derive_outcome(
        ticket(),
        [pr(10, merged="2026-09-10T09:00:00Z")],
        {10: {"comments": [verdict_comment("approved")] }},
        issue_comments=[agent],
        now=NOW,
    )
    intervention = outcomes.derive_outcome(
        ticket(),
        [pr(10, merged="2026-09-10T09:00:00Z")],
        {10: {"comments": [verdict_comment("approved")] }},
        issue_comments=[direct],
        now=NOW,
    )

    assert no_intervention["human_intervention_required"] is False
    assert intervention["human_intervention_required"] is True


def test_merge_without_an_approving_verdict_is_intervention():
    record = outcomes.derive_outcome(
        ticket(),
        [pr(10, merged="2026-09-10T09:00:00Z")],
        {10: {"comments": [verdict_comment("rejected")] }},
        now=NOW,
    )

    assert record["human_intervention_required"] is True


def test_append_encoder_preserves_one_record_per_ticket():
    first = outcomes.derive_outcome(ticket(1), now=NOW)
    duplicate = dict(first)
    duplicate["derived_at"] = "later"
    second = outcomes.derive_outcome(ticket(2), now=NOW)

    fresh = outcomes._new_records([first], [duplicate, second])

    assert [row["ticket"] for row in fresh] == ["owner/repo#2"]
    assert outcomes._decode_records(outcomes._encode_records([first, second])) == [
        first, second
    ]


def test_repository_walk_uses_index_rows_once_and_writes_no_partial_scan(
    monkeypatch,
):
    calls = []
    ticket_row = ticket(42)
    first = pr(10, created="2026-09-10T08:00:00Z")
    second = pr(11, created="2026-09-10T09:00:00Z", merged="2026-09-10T09:30:00Z")

    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [ticket_row])

    class Index(dict):
        all_rows = (first, second)

    def index(repo, limit):
        calls.append((repo, limit))
        return Index(), False

    monkeypatch.setattr(funnel, "ticket_pr_index", index)

    def gh_json(*args):
        if args[0] == "pr":
            return {"number": args[2], "comments": []}
        if args[0] == "issue":
            return {"comments": []}
        return [[]]

    monkeypatch.setattr(outcomes, "gh_json", gh_json)

    records = outcomes.derive_repository(REPO, limit=77, now=NOW)

    assert calls == [(REPO, 77)]
    assert records[0]["attempts"] == 2
    assert records[0]["merged"] is True


def test_repository_walk_rejects_a_truncated_pr_scan(monkeypatch):
    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [])
    monkeypatch.setattr(funnel, "ticket_pr_index", lambda repo, limit: ({}, True))

    with pytest.raises(outcomes.OutcomeError, match="refusing partial"):
        outcomes.derive_repository(REPO, limit=77, now=NOW)


def test_remote_append_uses_sha_and_retries_a_contents_conflict(monkeypatch):
    existing = outcomes.derive_outcome(ticket(1), now=NOW)
    addition = outcomes.derive_outcome(ticket(2), now=NOW)
    encoded = base64.b64encode(
        outcomes._encode_records([existing]).encode()
    ).decode()
    responses = [
        SimpleNamespace(returncode=0, stdout=json.dumps({"content": encoded, "sha": "old"}), stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="HTTP 409 conflict"),
        SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content": encoded, "sha": "new"}),
            stderr="",
        ),
        SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    ]
    commands = []

    def run(args):
        commands.append(list(args))
        return responses.pop(0)

    monkeypatch.setattr(outcomes, "_run_gh", run)
    monkeypatch.setattr(outcomes.time, "sleep", lambda seconds: None)

    assert outcomes.append_records([existing, addition]) == 1
    assert any("sha=new" in command for command in commands[-1])
    assert len(commands) == 4
