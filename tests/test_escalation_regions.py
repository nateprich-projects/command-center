"""The escalation scan reads asserted prose only (#1167, #1235).

Nate answered the gate question on 2026-09-21: skip quoted text, having been
shown the cost — a plan that describes its real risk only inside a code fence
would drop to the standard lane.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def scan_everything(text: str) -> list:
    """The pre-change scan: every pattern against the whole text.

    Kept here rather than in funnel.py so these fixtures can pin what the old
    behaviour was without leaving a second scanner in the shipped code.
    """
    return [name for name, pattern in sorted(funnel.ESCALATION_PATTERNS.items())
            if re.search(pattern, text, re.IGNORECASE)]


def test_the_recorded_capture_family_escalates_on_quoted_evidence_alone():
    """Four of the six false matches #1167 recorded, in one body: a pasted
    operator log line, an OS error string, the name of a scheduled job in a
    code span, and a rejected alternative in a block quote. Not one of them is
    a statement about what the work will do, and the plan says these four are
    the ones that clear on this change alone."""
    body = (FIXTURES / "escalation_scan_capture_family.md").read_text()

    assert scan_everything(body) == [
        "concurrency", "data-migration", "destructive",
    ]


def test_the_same_body_clears_once_quoted_evidence_is_excluded():
    body = (FIXTURES / "escalation_scan_capture_family.md").read_text()

    assert funnel.escalation_reasons("", body) == []


def test_the_1167_plan_keeps_only_its_one_double_quoted_match():
    """#1167's own body, as it stands. Two of its matches are inside quoted
    evidence and clear; the survivor is a phrase in ordinary double quotes,
    `"authorises … no lineup change"`. Excluding quoted prose would narrow the
    gate to a region that can hold an assertion about the work, which is the
    line #1167 draws — so it is left matching."""
    body = (FIXTURES / "escalation_scan_1167.md").read_text()

    assert scan_everything(body) == ["authorisation", "concurrency"]
    assert funnel.escalation_reasons("", body) == ["authorisation"]


def test_the_ff225_plan_scores_nothing_either_way():
    """The plan written for FF#225's work, without the pasted line, scored
    nothing before this change and still does."""
    body = (FIXTURES / "escalation_scan_ff225.md").read_text()

    assert scan_everything(body) == []
    assert funnel.escalation_reasons("", body) == []


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_fenced_block_is_not_scanned(fence):
    body = "A report.\n{}\nResource deadlock avoided\n{}\n".format(fence, fence)

    assert funnel.escalation_reasons("x", body) == []


def test_a_fence_with_an_info_string_still_opens_and_closes():
    body = "A report.\n```text\nhard delete\n```\nNothing else.\n"

    assert funnel.escalation_reasons("x", body) == []


def test_a_tilde_fence_is_not_closed_by_backticks():
    body = "~~~\nrewrite history\n```\nstill quoted\n~~~\n"

    assert funnel.escalation_reasons("x", body) == []


def test_a_block_quote_is_not_scanned():
    body = "The plan says:\n\n> we will migrate the schema\n\nIt will not.\n"

    assert funnel.escalation_reasons("x", body) == []


def test_an_inline_code_span_is_not_scanned():
    body = "The OS error was `Resource deadlock avoided`, which is not ours.\n"

    assert funnel.escalation_reasons("x", body) == []


def test_prose_outside_a_quoted_region_still_escalates():
    """Nothing is removed from the pattern set; only the region changes."""
    assert funnel.escalation_reasons(
        "x", "This step will migrate the schema in place.\n"
    ) == ["data-migration"]
    assert funnel.escalation_reasons(
        "x", "It runs destructive operations on the branch.\n"
    ) == ["destructive"]


def test_a_risk_sentence_after_a_fence_is_still_read():
    body = (
        "```\nResource deadlock avoided\n```\n\n"
        "This ticket will migrate the schema.\n"
    )

    assert funnel.escalation_reasons("x", body) == ["data-migration"]


def test_the_marker_still_outranks_the_regex_in_both_directions():
    assert funnel.escalation_reasons(
        "x", "Risk: standard\n\nThis will migrate the schema.\n"
    ) == []
    assert funnel.escalation_reasons(
        "x", "Risk: escalated — concurrency\n\nNothing risky in prose.\n"
    ) == ["declared: concurrency"]


def test_a_marker_inside_a_fence_is_not_a_marker():
    """It is being shown, like everything else in there."""
    body = "```\nRisk: standard\n```\n\nThis will migrate the schema.\n"

    assert funnel.escalation_reasons("x", body) == ["data-migration"]


def test_lines_are_blanked_rather_than_deleted():
    """A marker must not be joined to the sentence above it."""
    stripped = funnel.asserted_text("one\n```\ntwo\n```\nthree\n")

    assert stripped.splitlines() == ["one", "", "", "", "three"]


def test_an_unclosed_fence_quiets_the_rest_of_the_body():
    """Fail towards the cheap lane only where the author opened a fence and
    never closed it; the alternative is scanning a block they meant to quote."""
    body = "```\nThis will migrate the schema.\n"

    assert funnel.escalation_reasons("x", body) == []


def test_empty_and_missing_text_are_unchanged():
    assert funnel.asserted_text("") == ""
    assert funnel.escalation_reasons("", "") == []


def test_a_negated_sentence_in_prose_still_matches_and_is_not_this_fix():
    """The other two recorded false positives are asserted prose that says the
    opposite — "authorises no lineup change", "performs no destructive
    operation". The patterns' negation lookaheads do not catch those forms, and
    narrowing the scanned *region* cannot: the sentence is an assertion about
    the work, which is exactly what this scan is meant to read. Left matching,
    and pinned here so the gap is recorded rather than assumed fixed."""
    assert funnel.escalation_reasons(
        "x", "This plan authorises no lineup change.\n"
    ) == ["authorisation"]
    assert funnel.escalation_reasons(
        "x", "It performs no destructive operation.\n"
    ) == ["destructive"]
