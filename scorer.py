"""Scorer seam alias (#2427). Canonical implementation lives in ``funnel.py``."""

from __future__ import annotations

from hypothesis_scorer import (  # noqa: F401
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
