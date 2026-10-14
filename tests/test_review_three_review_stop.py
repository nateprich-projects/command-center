"""Stop the lane past three reviews on a PR (#2550).

The 09-28 stopping rule lived only in watch judgement: every pushed head
was offered for re-review, so PR #2539 reached 14 rejected of 14 and PR
#2521 reached 10 rejected of 10, each leading with
`requirement unmet: Prior blocking item addressed`. Past three recorded
reviews the lane requests no further re-review and the PR stays routed to
the ticket with the ticket open, instead of bouncing between implementer
and reviewer.
"""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

REPO = "nateprich-projects/command-center"
TICKET_NUMBER = 9001
REF = "{}#{}".format(REPO, TICKET_NUMBER)

H1 = "1" * 40
H2 = "2" * 40
H3 = "3" * 40
H4 = "4" * 40


def _verdict_comment(head, verdict="rejected", created="2026-10-13T05:00:00Z"):
    record = {
        "verdict": verdict,
        "ci": "green",
        "head_sha": head,
        "blocking": ["requirement unmet: something -- somewhere"],
    }
    body = ("{}\n\n```json\n{}\n```".format(
        funnel.REVIEW_MARKER, json.dumps(record)))
    return {"author": {"login": "nateprich"}, "body": body,
            "createdAt": created}


def _outsider_comment(head):
    row = _verdict_comment(head)
    return {"author": {"login": "someone-else"}, "body": row["body"],
            "createdAt": row["createdAt"]}


def _row(head, comments):
    return {
        "state": "OPEN",
        "number": 999,
        "headRefName": "ticket/{}".format(TICKET_NUMBER),
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
        "isCrossRepository": False,
        "author": {"login": "nateprich"},
        "createdAt": "2026-10-13T04:00:00Z",
        "comments": list(comments),
    }


def _ticket():
    return SimpleNamespace(
        ref=REF, repo=REPO, number=TICKET_NUMBER,
        title="Three-review stop ticket",
        url="https://github.com/{}/issues/{}".format(REPO, TICKET_NUMBER),
        risk="standard", state="OPEN",
    )


def _rejections(*heads):
    return [
        _verdict_comment(
            head, created="2026-10-13T0{}:00:00Z".format(index + 1))
        for index, head in enumerate(heads)
    ]


def test_fourth_review_head_requests_no_further_re_review():
    comments = _rejections(H1, H2, H3)
    row = _row(H4, comments)
    verdict = funnel._latest_verdict_from_comments(comments)

    standing = funnel.review_standing(REPO, row, verdict, comments)

    assert standing.state == "capped"

    facts = funnel.TicketPRFacts(rows_by_ref={REF: [row]})
    queue = funnel.review_queue([_ticket()], pr_facts=facts)

    assert queue == []

    blocked = funnel.awaiting_review([_ticket()], facts)

    assert REF in blocked


def test_cap_at_rejected_head_stays_routed_to_ticket():
    comments = _rejections(H1, H2, H3)
    row = _row(H3, comments)

    blocked = funnel.awaiting_review(
        [_ticket()], funnel.TicketPRFacts(rows_by_ref={REF: [row]}))

    assert REF in blocked


def test_second_review_head_still_requests_re_review():
    comments = _rejections(H1)
    row = _row(H2, comments)
    verdict = funnel._latest_verdict_from_comments(comments)

    standing = funnel.review_standing(REPO, row, verdict, comments)

    assert standing.state == "reviewable"

    facts = funnel.TicketPRFacts(rows_by_ref={REF: [row]})
    queue = funnel.review_queue([_ticket()], pr_facts=facts)

    assert [entry["pr"] for entry in queue] == [999]


def test_third_review_head_still_requests_re_review():
    comments = _rejections(H1, H2)
    row = _row(H3, comments)
    verdict = funnel._latest_verdict_from_comments(comments)

    standing = funnel.review_standing(REPO, row, verdict, comments)

    assert standing.state == "reviewable"

    facts = funnel.TicketPRFacts(rows_by_ref={REF: [row]})
    queue = funnel.review_queue([_ticket()], pr_facts=facts)

    assert [entry["pr"] for entry in queue] == [999]


def test_untrusted_comments_do_not_count_toward_the_cap():
    comments = [_outsider_comment(H1), _outsider_comment(H2),
                _outsider_comment(H3)]
    row = _row(H4, comments)

    assert funnel.count_lane_reviews(comments) == 0
    standing = funnel.review_standing(REPO, row, None, comments)

    assert standing.state == "reviewable"


def test_count_lane_reviews_counts_recorded_verdicts_only():
    comments = _rejections(H1, H2) + [
        {"author": {"login": "nateprich"}, "body": "just a note",
         "createdAt": "2026-10-13T05:00:00Z"},
        _outsider_comment(H3),
    ]

    assert funnel.count_lane_reviews(comments) == 2
    assert funnel.count_lane_reviews(None) == 0
    assert funnel.count_lane_reviews("not a list") == 0
