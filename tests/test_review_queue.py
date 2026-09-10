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
