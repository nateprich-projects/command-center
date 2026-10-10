"""Hypothesis scorer — shared ordering for slot admission (#2407, #2427).

Thin re-export of the canonical implementation in ``funnel.py``. Both
agents call the shared code; neither ranks anything itself.
"""

from __future__ import annotations

from funnel import (  # noqa: F401
    HYPOTHESIS_LIKELY_P,
    Hypothesis,
    ScoredHypothesis,
    admission_order,
    hypothesis_likely,
    hypothesis_score,
    hypothesis_try_and_watch,
    hypothesis_value,
    is_try_and_watch,
    normalize_hypothesis,
    order_for_admission,
    order_hypotheses,
    rank_hypotheses,
    score_hypotheses,
    score_hypothesis,
    slot_admission_order,
    sort_hypotheses,
    try_and_watch,
    try_and_watch_list,
)

__all__ = [
    "HYPOTHESIS_LIKELY_P",
    "Hypothesis",
    "ScoredHypothesis",
    "admission_order",
    "hypothesis_likely",
    "hypothesis_score",
    "hypothesis_try_and_watch",
    "hypothesis_value",
    "is_try_and_watch",
    "normalize_hypothesis",
    "order_for_admission",
    "order_hypotheses",
    "rank_hypotheses",
    "score_hypotheses",
    "score_hypothesis",
    "slot_admission_order",
    "sort_hypotheses",
    "try_and_watch",
    "try_and_watch_list",
]
