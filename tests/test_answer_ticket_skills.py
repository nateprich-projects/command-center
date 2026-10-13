"""Mandate the ticket answer command in the decision skills (#2496).

On 2026-10-10 Fantasy-GM#154 kept its blocked label and Needs-a-decision
block after a decision-review session answered in prose: prose is not a
record any reader understands. ``funnel answer-ticket`` (ticket #2495) is
the sole ticket path, so both skills that put a decision to Nate or record
his answer must require it, with no detached-prose or hand-edit alternate
path, while plan Gates questions stay on ``answer-gates``.
"""

from __future__ import annotations

import pathlib

FUNNEL_SKILL = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skills" / "funnel" / "SKILL.md"
)
DECISION_REVIEW_SKILL = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skills" / "decision-review" / "SKILL.md"
)


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def test_both_skills_mandate_answer_ticket_with_its_flags():
    """Accept 1: no prose bypass in either skill; every ticket question
    goes through answer-ticket with a verbatim answer and lift-or-replace
    outcome."""
    for path in (FUNNEL_SKILL, DECISION_REVIEW_SKILL):
        skill = _read(path)
        missing = [
            phrase
            for phrase in (
                "answer-ticket",
                "--answer",
                "--outcome",
                "lift",
                "replace",
                "--condition",
                "verbatim",
                "sub-issue",
                "answer-gates",
            )
            if phrase not in skill
        ]
        assert not missing, (
            "{} does not mandate the ticket answer command, missing: {}".format(
                path, ", ".join(missing)
            )
        )


def test_both_skills_close_the_prose_and_hand_edit_path():
    """The observed failure mechanism must read as forbidden, not optional,
    in both skills."""
    for path in (FUNNEL_SKILL, DECISION_REVIEW_SKILL):
        skill = _read(path).lower()
        assert "prose" in skill, "{} names no prose path to forbid".format(path)
        assert "never" in skill, "{} forbids nothing".format(path)
        assert "hand" in skill, (
            "{} does not forbid hand label or block edits".format(path)
        )


def test_both_skills_name_only_canonical_replacements():
    """A replacement the brief and the gates cannot parse must not be
    writable through the skills."""
    for path in (FUNNEL_SKILL, DECISION_REVIEW_SKILL):
        skill = _read(path)
        for form in (
            "Blocked until YYYY-MM-DD",
            "Blocked on #N",
            "Blocked until event",
        ):
            assert form in skill, "{} omits replacement form {}".format(
                path, form
            )
