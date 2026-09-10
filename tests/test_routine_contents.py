"""Routine prompts keep the unattended shaping boundary explicit."""

from __future__ import annotations

import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("routine", "tier_phrase"),
    (("zcode", "standard-tier idea"), ("claude", "escalated idea"),
     ("muse", "standard-tier idea")),
)
def test_shaping_is_the_third_ordered_job_with_a_safe_unattended_boundary(
    routine, tier_phrase
):
    body = (ROOT / "routines" / (routine + ".md")).read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "review, then breakdown, then shaping" in normalized
    assert tier_phrase in normalized
    assert "do not grill" in normalized
    assert "settle what precedent covers" in normalized
    assert "cite the source" in normalized
    assert "needs you" in normalized
    assert "shaped is not approval" in normalized
    # Muse runs with --disable-write and pipes the plan instead (#366).
    assert ("funnel.py shaped <ref> --plan <file>" in normalized
            or "funnel.py shaped <ref> --plan -" in normalized)


def test_breakdown_docs_record_and_resume_needs_decisions():
    """An undecidable breakdown must become visible to Nate and resume from
    his answer, rather than repeatedly consuming Muse runs on the same plan."""
    documents = (
        ROOT / "skills" / "breakdown" / "SKILL.md",
        ROOT / "routines" / "muse.md",
    )
    for path in documents:
        normalized = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert (
            "python3 /users/nateprich/.claude/command-center-run/funnel.py "
            "comment <ref> --voice agent --needs-decision"
        ) in normalized
        assert "read the issue's comments" in normalized
        assert "an earlier `**needs a decision:**` header" in normalized
        assert "the answer that followed it" in normalized
        assert "act on that answer" in normalized
        assert "create no tickets" in normalized
        assert "--outcome done" in normalized or "finish `done`" in normalized
        assert "answer the breakdown's question?" in normalized
        assert "removes the `blocked` label" in normalized
        assert (
            "comment on the issue saying precisely what is undecided, and leave it"
            not in normalized
        )
