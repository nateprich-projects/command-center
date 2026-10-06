"""Shared recorded review cases for the listing and packet (#2196)."""

from __future__ import annotations

import io
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import review  # noqa: E402

REPO = "nateprich-projects/command-center"
CORPUS = json.loads(
    (ROOT / "tests/fixtures/review_candidate_corpus.json").read_text()
)
CASES = CORPUS["cases"]


def _parsed_verdict(comments):
    return funnel._latest_verdict_from_comments(comments)


def _ticket(case):
    number = case["ticket_number"]
    risk = case["risk"]
    body = "What: replay the review case."
    if risk is not None:
        body += "\n\nRisk: {}".format(risk)
    return SimpleNamespace(
        ref="{}#{}".format(REPO, number),
        repo=REPO,
        number=number,
        title="Corpus ticket {}".format(number),
        url="https://github.com/{}/issues/{}".format(REPO, number),
        body=body,
        risk=risk,
        state="OPEN",
    )


def _packet_ticket(case):
    item = _ticket(case)
    return {
        "ref": item.ref,
        "number": item.number,
        "title": item.title,
        "url": item.url,
        "body": item.body,
        "risk": item.risk,
        "state": item.state,
    }


def _semantic_verdict(verdict):
    if verdict is None:
        return None
    return {key: verdict.get(key) for key in
            ("verdict", "ci", "head_sha", "blocking")}


def _wire_row(case):
    row = dict(case["pr_row"])
    row["comments"] = case["comments"]
    return row


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_recorded_candidates_agree_across_listing_and_packet(
        case, monkeypatch, capsys):
    row = _wire_row(case)
    comments = case["comments"]
    verdict = _parsed_verdict(comments)
    assert _semantic_verdict(verdict) == case["expected_verdict"]

    expected_standing = case["expected_standing"]
    direct = funnel.review_standing(REPO, row, verdict, comments)
    assert {"state": direct.state, "reason": direct.reason} == expected_standing

    ticket = _ticket(case)
    facts = funnel.TicketPRFacts(rows_by_ref={ticket.ref: [row]})
    skipped = []
    output = io.StringIO()
    writes = []
    original_write = funnel._write_verdict

    def capture_write(repo, pr, sha, result, ci, blocking, note,
                      run=None, agent=None, output_stream=None):
        writes.append({
            "verdict": result,
            "ci": ci,
            "blocking": list(blocking),
        })
        return original_write(
            repo, pr, sha, result, ci, blocking, note,
            run=run, agent=agent, output_stream=output_stream,
        )

    monkeypatch.setattr(funnel, "_write_verdict", capture_write)
    monkeypatch.setattr(
        funnel, "_run_gh",
        lambda args, **kwargs: SimpleNamespace(
            returncode=0, stderr="", stdout=""),
    )
    queue = funnel.review_queue(
        [ticket], tier=case["expected_listing"]["tier_filter"],
        pr_facts=facts, output_stream=output, skipped=skipped,
    )
    expected_listing = case["expected_listing"]
    actual_listing = {
        "listed": bool(queue),
        "tier_filter": expected_listing["tier_filter"],
        "candidate_tier": queue[0]["tier"] if queue else None,
        "skipped_reason": skipped[0]["reason"] if skipped else None,
    }
    assert actual_listing == expected_listing

    expected_rejection = case.get("expected_conflict_rejection")
    if expected_rejection is None:
        assert writes == []
        assert output.getvalue() == ""
    else:
        assert len(writes) == 1
        assert writes[0]["verdict"] == expected_rejection["verdict"]
        assert writes[0]["blocking"] == expected_rejection["blocking"]
        assert not any(reason.startswith("ci:")
                       for reason in writes[0]["blocking"])
        assert output.getvalue() == expected_rejection.get(
            "stream_message", output.getvalue())
        if case["fix"] == "#1503":
            assert capsys.readouterr().out == ""
            assert output.getvalue() == expected_rejection["stream_message"]

    shaped_comments = [
        review._shape_pr_comment(comment, "issue") for comment in comments
    ]
    comment_section = review._pr_comments_section(shaped_comments)
    if "expected_comment_section" in case:
        assert comment_section == case["expected_comment_section"]

    packet = review.build_packet(
        repo=REPO,
        pr_number=row["number"],
        pr_view=row,
        diff="",
        ticket=_packet_ticket(case),
        plan_md="# plan",
        plan_md_missing=False,
        open_prs=[],
        verdict=verdict,
        stop_counter={"window_days": 7, "count": 0, "refs": [],
                      "stop_auto_merging": False},
        collected_at="2026-09-30T12:00:00Z",
        pr_comments=comment_section,
    )
    assert packet["standing"] == expected_standing

