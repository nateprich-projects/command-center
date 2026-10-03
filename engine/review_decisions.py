"""Pure decisions for the pre-registered review-quality evaluation.

Rate comparisons use independent head/main counts and Newcombe's Wilson
interval for their difference. The joint-detection lift is the fraction of
bad changes newly caught by B, with a Wilson interval. These decisions are
observational only; no result blocks a merge.

Power note: as a fixed-horizon reference, a one-sided two-proportion normal
approximation at alpha=.05 needs about 109 independent runs per side for 80%
power to detect an 80% to 65% must-reject drop. The 120-per-side cap gives
about 83% under that assumption. At 73% to 58%, it gives about 79%, so the
cap can leave the measured-baseline case inconclusive. This calculation is
not a claim about the power of repeated unadjusted interim tests: next_batch
expects the sequentially calibrated p-value for the pre-registered decline.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Literal


START_RUNS = 40
BATCH_RUNS = 20
MAX_RUNS = 120
DECLINE_P = 0.05
FUTILITY_P = 0.30

MUST_REJECT_DROP = 0.15
MUST_APPROVE_FALSE_BLOCK_RISE = 0.10
JOINT_DETECTION_LIFT = 0.10
JOINT_FALSE_BLOCK_CHANGE = 0.05

_WILSON_Z = 1.959963984540054


@dataclass(frozen=True)
class RateComparison:
    """Success counts for the ticket head and same-main baseline."""

    head: int
    head_total: int
    main: int
    main_total: int

    def __post_init__(self) -> None:
        _validate_count(self.head, self.head_total, "head")
        _validate_count(self.main, self.main_total, "main")


@dataclass(frozen=True)
class EvaluationCounts:
    """Aggregate counts needed to decide the registered review margins."""

    must_reject: RateComparison
    must_approve_false_blocks: RateComparison
    joint_false_blocks: RateComparison
    bad_changes: int
    a_misses: int
    b_catches: int

    def __post_init__(self) -> None:
        _validate_count(self.bad_changes, self.bad_changes, "bad changes")
        if (isinstance(self.a_misses, bool)
                or not isinstance(self.a_misses, int)
                or not 0 <= self.a_misses <= self.bad_changes):
            raise ValueError("A misses must be between 0 and bad_changes")
        if (isinstance(self.b_catches, bool)
                or not isinstance(self.b_catches, int)
                or not 0 <= self.b_catches <= self.a_misses):
            raise ValueError("B catches must be between 0 and A misses")


def _validate_count(successes: int, total: int, label: str) -> None:
    if isinstance(successes, bool) or isinstance(total, bool):
        raise ValueError("{} counts must be integers".format(label))
    if not isinstance(successes, int) or not isinstance(total, int):
        raise ValueError("{} counts must be integers".format(label))
    if total <= 0 or successes < 0 or successes > total:
        raise ValueError("{} counts must satisfy 0 <= successes <= total".format(
            label))


def _wilson(successes: int, total: int) -> tuple[float, float]:
    rate = successes / total
    z2 = _WILSON_Z * _WILSON_Z
    denominator = 1 + z2 / total
    center = (rate + z2 / (2 * total)) / denominator
    radius = (_WILSON_Z * sqrt(
        rate * (1 - rate) / total + z2 / (4 * total * total)
    ) / denominator)
    return max(0.0, center - radius), min(1.0, center + radius)


def _difference(comparison: RateComparison) -> tuple[float, tuple[float, float]]:
    """Return head-minus-main rate and Newcombe's 95% difference interval."""
    head_rate = comparison.head / comparison.head_total
    main_rate = comparison.main / comparison.main_total
    head_low, head_high = _wilson(comparison.head, comparison.head_total)
    main_low, main_high = _wilson(comparison.main, comparison.main_total)
    difference = head_rate - main_rate
    low = difference - sqrt(
        (head_rate - head_low) ** 2 + (main_high - main_rate) ** 2
    )
    high = difference + sqrt(
        (head_high - head_rate) ** 2 + (main_rate - main_low) ** 2
    )
    return difference, (max(-1.0, low), min(1.0, high))


def _comparison_result(
        comparison: RateComparison, margin: float, *, adverse: Literal["low", "high"]
        ) -> dict[str, object]:
    difference, (low, high) = _difference(comparison)
    if adverse == "low":
        status = "regression" if high < margin else (
            "safe" if low > margin else "inconclusive")
    else:
        status = "regression" if low > margin else (
            "safe" if high < margin else "inconclusive")
    return {
        "difference": difference,
        "interval_95": [low, high],
        "margin": margin,
        "status": status,
    }


def decide_outcome(counts: EvaluationCounts) -> dict[str, object]:
    """Classify quality counts as regression, pass, or inconclusive.

    A pass rules out each registered adverse margin using the full 95%
    Newcombe interval. A regression requires the whole interval to exceed an
    adverse margin. Everything else is inconclusive, and every outcome is
    nonblocking. The 10pp joint-detection lift is reported separately because
    it is a meaningful-improvement target, not an adverse margin.
    """
    must_reject = _comparison_result(
        counts.must_reject, -MUST_REJECT_DROP, adverse="low")
    must_approve = _comparison_result(
        counts.must_approve_false_blocks,
        MUST_APPROVE_FALSE_BLOCK_RISE,
        adverse="high")
    joint_false_blocks = _comparison_result(
        counts.joint_false_blocks,
        JOINT_FALSE_BLOCK_CHANGE,
        adverse="high")
    regression = any(result["status"] == "regression" for result in (
        must_reject, must_approve, joint_false_blocks))
    safe = all(result["status"] == "safe" for result in (
        must_reject, must_approve, joint_false_blocks))

    lift = counts.b_catches / counts.bad_changes
    lift_low, lift_high = _wilson(counts.b_catches, counts.bad_changes)
    if lift_low > JOINT_DETECTION_LIFT:
        lift_status = "meaningful"
    elif lift_high < JOINT_DETECTION_LIFT:
        lift_status = "below_meaningful_margin"
    else:
        lift_status = "inconclusive"

    if joint_false_blocks["interval_95"][1] < -JOINT_FALSE_BLOCK_CHANGE:
        joint_false_block_change = "improvement"
    elif joint_false_blocks["status"] == "regression":
        joint_false_block_change = "regression"
    elif joint_false_blocks["status"] == "safe":
        joint_false_block_change = "within_margin"
    else:
        joint_false_block_change = "inconclusive"

    return {
        "outcome": "regression" if regression else (
            "pass" if safe else "inconclusive"),
        "merge_blocking": False,
        "must_reject": must_reject,
        "must_approve_false_blocks": must_approve,
        "joint_detection_lift": {
            "rate": lift,
            "b_catch_rate_among_a_misses": counts.b_catches / counts.a_misses
            if counts.a_misses else None,
            "interval_95": [lift_low, lift_high],
            "meaningful_margin": JOINT_DETECTION_LIFT,
            "status": lift_status,
        },
        "joint_false_blocks": {
            **joint_false_blocks,
            "change": joint_false_block_change,
        },
    }


def next_batch(runs_per_side: int, decline_p_value: float | None = None) -> int:
    """Return additional runs per side under the registered sequential bounds.

    The initial call uses ``runs_per_side=0`` and returns 40. Later calls pass
    the sequentially calibrated p-value for an unacceptable decline: below
    .05 stops for decline; above .30 stops for futility. Otherwise add 20,
    capped at 120 per side. A return of 0 means stop, not pass.
    """
    if isinstance(runs_per_side, bool) or not isinstance(runs_per_side, int):
        raise ValueError("runs_per_side must be an integer")
    if runs_per_side < 0 or runs_per_side > MAX_RUNS:
        raise ValueError("runs_per_side must be between 0 and 120")
    if runs_per_side == 0:
        return START_RUNS
    if runs_per_side < START_RUNS or (runs_per_side - START_RUNS) % BATCH_RUNS:
        raise ValueError("runs_per_side must follow the 40 + 20n schedule")
    if runs_per_side == MAX_RUNS:
        return 0
    if decline_p_value is None or isinstance(decline_p_value, bool):
        raise ValueError("an interim decline p-value is required")
    if not isinstance(decline_p_value, (int, float)) or not 0 <= decline_p_value <= 1:
        raise ValueError("decline_p_value must be between 0 and 1")
    if decline_p_value < DECLINE_P or decline_p_value > FUTILITY_P:
        return 0
    return min(BATCH_RUNS, MAX_RUNS - runs_per_side)
