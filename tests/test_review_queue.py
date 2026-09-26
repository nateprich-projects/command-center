"""The review queue hands out the oldest unreviewed PR, not the newest (#320)."""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

REPO = "nateprich/beta"


def _ticket(number, risk="standard"):
    return SimpleNamespace(
        ref="{}#{}".format(REPO, number), repo=REPO, number=number,
        title="Ticket {}".format(number),
        url="https://github.com/{}/issues/{}".format(REPO, number),
        risk=risk,
    )


def _wire(monkeypatch, rows, verdicts=None):
    by_ref = {}
    for row in rows:
        row = dict(row)
        row["verdict"] = (verdicts or {}).get(row.get("number"))
        branch = row.get("headRefName") or ""
        ref = funnel.ticket_ref_from_branch(REPO, branch)
        if ref:
            by_ref.setdefault(ref, []).append(row)
    facts = funnel.TicketPRFacts(rows_by_ref=by_ref)
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: facts)
    return facts


def _capture_conflict_writes(monkeypatch, facts):
    writes = []

    def write(repo, pr, sha, verdict, ci, blocking, note,
              run=None, agent=None):
        writes.append({
            "repo": repo,
            "pr": pr,
            "head_sha": sha,
            "verdict": verdict,
            "ci": ci,
            "blocking": list(blocking),
            "agent": agent,
        })
        for rows in facts.rows_by_ref.values():
            for row in rows:
                if row.get("number") == pr:
                    row["verdict"] = {
                        "verdict": verdict,
                        "ci": ci,
                        "head_sha": sha,
                        "blocking": list(blocking),
                    }
        return 0

    monkeypatch.setattr(funnel, "_write_verdict", write)
    return writes


def _row(pr, ticket, opened, head="abc"):
    return {"number": pr, "headRefName": "ticket/{}".format(ticket),
            "headRefOid": head, "createdAt": opened}


def _queue_with_verdict_history(monkeypatch, verdict, verdict_at,
                                comment_times=()):
    comments = [{
        "body": funnel.REVIEW_MARKER + "\n" + json.dumps(verdict),
        "createdAt": verdict_at,
    }]
    comments.extend({"body": "later PR comment", "createdAt": created_at}
                     for created_at in comment_times)
    row = _row(10, 1, "2026-09-10T05:00:00Z", head="same")
    row["comments"] = comments
    parsed = funnel._latest_verdict_from_comments(comments)
    facts = _wire(monkeypatch, [row], verdicts={10: parsed})
    return facts, parsed, funnel.review_queue([_ticket(1)])


def test_the_oldest_pr_comes_first_whatever_order_gh_returns(monkeypatch):
    rows = [  # newest first, as `gh pr list` returns them
        _row(30, 3, "2026-09-09T12:16:00Z"),
        _row(20, 2, "2026-09-09T10:47:00Z"),
        _row(10, 1, "2026-09-09T10:25:00Z"),
    ]
    _wire(monkeypatch, rows)

    queue = funnel.review_queue([_ticket(1), _ticket(2), _ticket(3)])

    assert [entry["pr"] for entry in queue] == [10, 20, 30]


def test_a_pr_already_judged_at_its_head_is_still_skipped(monkeypatch):
    rows = [_row(20, 2, "2026-09-09T10:47:00Z", head="judged"),
            _row(10, 1, "2026-09-09T10:25:00Z")]
    _wire(monkeypatch, rows, verdicts={20: {"head_sha": "judged"}})

    queue = funnel.review_queue([_ticket(1), _ticket(2)])

    assert [entry["pr"] for entry in queue] == [10]


def test_later_pr_comment_requeues_a_requirement_unsure_head(monkeypatch):
    verdict_at = "2026-09-10T05:00:00Z"
    unsure = {"verdict": "rejected", "head_sha": "same",
              "blocking": ["requirement unsure: verify the run"]}
    facts, parsed, queue = _queue_with_verdict_history(
        monkeypatch, unsure, verdict_at, ("2026-09-10T05:01:00Z",))

    assert parsed["comment_created_at"] == verdict_at
    assert [entry["pr"] for entry in queue] == [10]
    # The engineer still owns the rejected head until a fresh review verdict.
    assert REPO + "#1" not in funnel.awaiting_review(
        [_ticket(1)], pr_facts=facts)


def test_requirement_unsure_stays_covered_without_a_later_comment(monkeypatch):
    unsure = {"verdict": "rejected", "head_sha": "same",
              "blocking": ["requirement unsure: verify the run"]}
    _, _, queue = _queue_with_verdict_history(
        monkeypatch, unsure, "2026-09-10T05:00:00Z")

    assert queue == []


def test_comment_before_requirement_unsure_verdict_does_not_reopen(monkeypatch):
    unsure = {"verdict": "rejected", "head_sha": "same",
              "blocking": ["requirement unsure: verify the run"]}
    _, _, queue = _queue_with_verdict_history(
        monkeypatch, unsure, "2026-09-10T05:00:00Z",
        ("2026-09-10T04:59:00Z",))

    assert queue == []


def test_approved_head_stays_covered_after_a_later_comment(monkeypatch):
    approved = {"verdict": "approved", "head_sha": "same", "blocking": []}
    _, _, queue = _queue_with_verdict_history(
        monkeypatch, approved, "2026-09-10T05:00:00Z",
        ("2026-09-10T05:01:00Z",))

    assert queue == []


def test_other_rejection_stays_covered_after_a_later_comment(monkeypatch):
    rejected = {"verdict": "rejected", "head_sha": "same",
                "blocking": ["requirement unmet: fix the behavior"]}
    _, _, queue = _queue_with_verdict_history(
        monkeypatch, rejected, "2026-09-10T05:00:00Z",
        ("2026-09-10T05:01:00Z",))

    assert queue == []


def test_the_tier_filter_still_applies_after_sorting(monkeypatch):
    rows = [_row(20, 2, "2026-09-09T10:47:00Z"),
            _row(10, 1, "2026-09-09T10:25:00Z")]
    _wire(monkeypatch, rows)
    assert [e["pr"] for e in funnel.review_queue(
        [_ticket(1, "escalated"), _ticket(2)], tier="standard")] == [20]
    assert [e["pr"] for e in funnel.review_queue(
        [_ticket(1, "escalated"), _ticket(2)], tier="escalated")] == [10]


def test_ties_on_creation_time_break_by_pr_number(monkeypatch):
    same = "2026-09-09T10:25:00Z"
    rows = [_row(12, 2, same), _row(11, 1, same)]
    _wire(monkeypatch, rows)

    assert [e["pr"] for e in funnel.review_queue([_ticket(1), _ticket(2)])] == [11, 12]


# -- A rejected verdict binds to the head it judged (#487) --------------------

def _both(monkeypatch, rows, verdicts):
    """Run both selectors over one snapshot of PRs and verdicts."""
    _wire(monkeypatch, rows, verdicts)
    tickets = [_ticket(n) for n in range(1, 4)]
    review = {e["ref"] for e in funnel.review_queue(tickets)}
    blocked = funnel.awaiting_review(tickets)
    return review, blocked


def test_a_rejection_on_the_current_head_hands_the_ticket_back_to_engineering(monkeypatch):
    rows = [_row(10, 1, "2026-09-10T05:00:00Z", head="h1")]
    review, blocked = _both(monkeypatch, rows,
                            {10: {"verdict": "rejected", "head_sha": "h1"}})
    ref = "{}#1".format(REPO)
    assert ref not in review      # the reviewer already judged this head
    assert ref not in blocked     # so the engineer owes the fix


def test_a_push_after_a_rejection_makes_the_ticket_review_work_only(monkeypatch):
    rows = [_row(10, 1, "2026-09-10T05:00:00Z", head="h2")]
    review, blocked = _both(monkeypatch, rows,
                            {10: {"verdict": "rejected", "head_sha": "h1"}})
    ref = "{}#1".format(REPO)
    assert ref in review          # the new head has no verdict
    assert ref in blocked         # and the engineer is not offered it again


def test_the_observed_396_facts_route_to_review_not_engineering(monkeypatch):
    """Run 17a0efb511ec, 2026-09-10: verdict rejected on 7cb32a2, branch at f62425d."""
    rows = [_row(423, 1, "2026-09-09T19:00:00Z", head="f62425d")]
    review, blocked = _both(monkeypatch, rows,
                            {423: {"verdict": "rejected", "head_sha": "7cb32a2"}})
    ref = "{}#1".format(REPO)
    assert ref in review and ref in blocked


def test_no_snapshot_offers_the_same_ticket_head_to_both_selectors(monkeypatch):
    rows = [_row(10, 1, "2026-09-10T05:00:00Z", head="same"),
            _row(20, 2, "2026-09-10T05:01:00Z", head="moved"),
            _row(30, 3, "2026-09-10T05:02:00Z", head="fresh")]
    verdicts = {10: {"verdict": "rejected", "head_sha": "same"},
                20: {"verdict": "rejected", "head_sha": "old"}}
    review, blocked = _both(monkeypatch, rows, verdicts)
    engineering = {"{}#{}".format(REPO, n) for n in (1, 2, 3)} - blocked
    assert not (review & engineering)
    assert engineering == {"{}#1".format(REPO)}
    assert review == {"{}#2".format(REPO), "{}#3".format(REPO)}


def test_the_predicates_fail_closed_without_a_head():
    assert not funnel.verdict_covers_head({"verdict": "rejected", "head_sha": "h"}, None)
    assert not funnel.rejected_at_current_head(None, "h")
    assert funnel.rejected_at_current_head({"verdict": "rejected", "head_sha": "h"}, "h")
    assert not funnel.rejected_at_current_head({"verdict": "approved", "head_sha": "h"}, "h")


# -- Checks still running are waited for, not rejected (#900) -----------------
#
# Two shadow reviews on 2026-09-15 rejected a PR with "ci: CI not green (state
# unknown)" seconds after a push — command-center#898 at c2aeb227 (23:11Z) and
# jeffy-finance-agent#76 at c6e7a880 (22:36Z) — and the live reviewer approved
# and merged both within three minutes. Because a recorded verdict covers that
# head for good, those rejections would have been final once the engine reviewer
# went live.

def _rollup_row(pr, ticket, opened, rollup, head="abc"):
    row = _row(pr, ticket, opened, head=head)
    row["statusCheckRollup"] = rollup
    return row


RUNNING = [{"name": "tests", "status": "IN_PROGRESS", "conclusion": None}]
GREEN = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}]


def test_a_pr_whose_checks_are_still_running_is_not_offered(monkeypatch):
    _wire(monkeypatch, [_rollup_row(10, 1, "2026-09-15T23:10:00Z", RUNNING)])

    assert funnel.review_queue([_ticket(1)]) == []


def test_the_same_pr_is_offered_once_its_checks_report(monkeypatch):
    _wire(monkeypatch, [_rollup_row(10, 1, "2026-09-15T23:10:00Z", GREEN)])

    assert [e["pr"] for e in funnel.review_queue([_ticket(1)])] == [10]


def test_red_ci_is_still_offered_so_the_precheck_can_reject_it(monkeypatch):
    _wire(monkeypatch, [_rollup_row(10, 1, "2026-09-15T23:10:00Z", RED)])

    assert [e["pr"] for e in funnel.review_queue([_ticket(1)])] == [10]


def test_a_rollup_with_no_checks_is_still_offered(monkeypatch):
    """No checks at all is an answer, and must stay visible as a refusal."""
    _wire(monkeypatch, [_rollup_row(10, 1, "2026-09-15T23:10:00Z", [])])

    queue = funnel.review_queue([_ticket(1)])
    assert [e["pr"] for e in queue] == [10]
    assert "blocking" not in queue[0]


def test_conflict_with_an_empty_rollup_is_rejected_without_review(monkeypatch):
    row = _rollup_row(10, 1, "2026-09-15T23:10:00Z", [])
    row.update(state="OPEN", mergeable="CONFLICTING")
    facts = _wire(monkeypatch, [row])
    writes = _capture_conflict_writes(monkeypatch, facts)

    queue = funnel.review_queue([_ticket(1)], pr_facts=facts)

    reason = "branch 'ticket/1'" + funnel.CONFLICTING_BRANCH_SUFFIX
    assert queue == []
    assert writes == [{
        "repo": REPO,
        "pr": 10,
        "head_sha": "abc",
        "verdict": "rejected",
        "ci": "unknown",
        "blocking": [reason],
        "agent": funnel.MERGE_GATE_AGENT,
    }]


def test_dirty_merge_state_outweighs_pending_ci(monkeypatch):
    row = _rollup_row(10, 1, "2026-09-15T23:10:00Z", RUNNING)
    row.update(state="OPEN", mergeable="UNKNOWN", mergeStateStatus="DIRTY")
    facts = _wire(monkeypatch, [row])
    writes = _capture_conflict_writes(monkeypatch, facts)

    queue = funnel.review_queue([_ticket(1)], pr_facts=facts)

    assert queue == []
    assert len(writes) == 1
    assert writes[0]["blocking"] == [
        "branch 'ticket/1'" + funnel.CONFLICTING_BRANCH_SUFFIX
    ]


def test_ci_unknown_rejection_at_a_conflicting_head_is_replaced_without_review(
        monkeypatch):
    row = _rollup_row(10, 1, "2026-09-15T23:10:00Z", [])
    row.update(state="OPEN", mergeable="CONFLICTING")
    facts = _wire(monkeypatch, [row], verdicts={10: {
        "verdict": "rejected",
        "ci": "unknown",
        "head_sha": "abc",
        "blocking": ["ci: CI not green (state unknown)"],
    }})
    writes = _capture_conflict_writes(monkeypatch, facts)

    queue = funnel.review_queue([_ticket(1)], pr_facts=facts)

    reason = "branch 'ticket/1'" + funnel.CONFLICTING_BRANCH_SUFFIX
    assert queue == []
    assert writes[0]["blocking"] == [
        "branch 'ticket/1'" + funnel.CONFLICTING_BRANCH_SUFFIX
    ]
    assert facts.rows_by_ref[REPO + "#1"][0]["verdict"]["blocking"] == [
        reason
    ]


def test_canonical_conflict_rejection_is_not_reoffered(monkeypatch):
    row = _rollup_row(10, 1, "2026-09-15T23:10:00Z", [])
    row.update(state="OPEN", mergeable="CONFLICTING")
    reason = "branch 'ticket/1'" + funnel.CONFLICTING_BRANCH_SUFFIX
    facts = _wire(monkeypatch, [row], verdicts={10: {
        "verdict": "rejected",
        "ci": "unknown",
        "head_sha": "abc",
        "blocking": [reason],
    }})
    writes = _capture_conflict_writes(monkeypatch, facts)

    assert funnel.review_queue([_ticket(1)], pr_facts=facts) == []
    assert writes == []


def test_a_conflict_rejection_is_idempotent_across_review_ticks(monkeypatch):
    row = _rollup_row(10, 1, "2026-09-15T23:10:00Z", GREEN)
    row.update(state="OPEN", mergeable="CONFLICTING")
    facts = _wire(monkeypatch, [row])
    writes = _capture_conflict_writes(monkeypatch, facts)

    first_tick = funnel.review_queue([_ticket(1)], pr_facts=facts)
    second_tick = funnel.review_queue([_ticket(1)], pr_facts=facts)

    reason = "branch 'ticket/1'" + funnel.CONFLICTING_BRANCH_SUFFIX
    expected = {
        "repo": REPO,
        "pr": 10,
        "head_sha": "abc",
        "verdict": "rejected",
        "ci": "unknown",
        "blocking": [reason],
        "agent": funnel.MERGE_GATE_AGENT,
    }
    assert first_tick == second_tick == []  # neither tick starts model review
    assert writes == [expected]
    assert facts.rows_by_ref[REPO + "#1"][0]["verdict"]["blocking"] == [
        reason
    ]


def test_conflict_is_removed_without_holding_back_another_review(monkeypatch):
    conflict = _rollup_row(10, 1, "2026-09-15T23:10:00Z", [])
    conflict.update(state="OPEN", mergeable="CONFLICTING")
    clean = _rollup_row(20, 2, "2026-09-15T23:11:00Z", GREEN)
    clean["state"] = "OPEN"
    facts = _wire(monkeypatch, [conflict, clean])
    _capture_conflict_writes(monkeypatch, facts)

    queue = funnel.review_queue([_ticket(1), _ticket(2)], pr_facts=facts)

    assert [entry["pr"] for entry in queue] == [20]


def test_one_pending_pr_does_not_hold_back_the_rest(monkeypatch):
    rows = [_rollup_row(20, 2, "2026-09-15T23:11:00Z", GREEN),
            _rollup_row(10, 1, "2026-09-15T23:10:00Z", RUNNING)]
    _wire(monkeypatch, rows)

    assert [e["pr"] for e in funnel.review_queue([_ticket(1), _ticket(2)])] == [20]


def test_a_failing_check_beside_a_running_one_is_decided_already(monkeypatch):
    rows = [_rollup_row(10, 1, "2026-09-15T23:10:00Z",
                        RED + [{"name": "lint", "status": "QUEUED"}])]
    _wire(monkeypatch, rows)

    assert [e["pr"] for e in funnel.review_queue([_ticket(1)])] == [10]


def test_checks_still_running_reads_both_wire_shapes():
    assert funnel.checks_still_running(RUNNING)
    assert funnel.checks_still_running([{"context": "ci", "state": "PENDING"}])
    assert funnel.checks_still_running([{"name": "tests"}])  # no signal yet
    assert not funnel.checks_still_running(GREEN)
    assert not funnel.checks_still_running([{"context": "ci", "state": "SUCCESS"}])
    assert not funnel.checks_still_running(RED)
    assert not funnel.checks_still_running([])
    assert not funnel.checks_still_running(None)


def test_the_rollup_reader_is_the_one_the_review_engine_uses():
    from engine import review

    for rollup in (RUNNING, GREEN, RED, [], [{"context": "ci", "state": "PENDING"}]):
        assert review.ci_state(rollup) == funnel.ci_rollup_state(rollup)
