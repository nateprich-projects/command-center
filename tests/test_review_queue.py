"""The review queue hands out the oldest unreviewed PR, not the newest (#320)."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

REPO = "nateprich/beta"


def _ticket(number):
    return SimpleNamespace(
        ref="{}#{}".format(REPO, number), repo=REPO, number=number,
        title="Ticket {}".format(number),
        url="https://github.com/{}/issues/{}".format(REPO, number),
    )


def _wire(monkeypatch, rows, verdicts=None):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: rows)
    monkeypatch.setattr(funnel, "latest_verdict",
                        lambda repo, pr: (verdicts or {}).get(pr))
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, n: "Risk: standard")


def _row(pr, ticket, opened, head="abc"):
    return {"number": pr, "headRefName": "ticket/{}".format(ticket),
            "headRefOid": head, "createdAt": opened}


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


def test_the_tier_filter_still_applies_after_sorting(monkeypatch):
    rows = [_row(20, 2, "2026-09-09T10:47:00Z"),
            _row(10, 1, "2026-09-09T10:25:00Z")]
    _wire(monkeypatch, rows)
    monkeypatch.setattr(funnel, "required_tier",
                        lambda title, body, failed_before=False:
                        "escalated" if "1" in title else "standard")

    assert [e["pr"] for e in funnel.review_queue(
        [_ticket(1), _ticket(2)], tier="standard")] == [20]
    assert [e["pr"] for e in funnel.review_queue(
        [_ticket(1), _ticket(2)], tier="escalated")] == [10]


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

    assert [e["pr"] for e in funnel.review_queue([_ticket(1)])] == [10]


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
