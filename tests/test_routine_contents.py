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
