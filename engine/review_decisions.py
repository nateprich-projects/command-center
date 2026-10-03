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
from datetime import datetime
from math import sqrt
from typing import Literal, Sequence

import usage


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


def wilson_interval(successes: int, total: int) -> tuple[float, float]:
    """Return the two-sided 95% Wilson score interval for a binomial rate."""
    _validate_count(successes, total, "Wilson")
    rate = successes / total
    z2 = _WILSON_Z * _WILSON_Z
    denominator = 1 + z2 / total
    center = (rate + z2 / (2 * total)) / denominator
    radius = (_WILSON_Z * sqrt(
        rate * (1 - rate) / total + z2 / (4 * total * total)
    ) / denominator)
    return max(0.0, center - radius), min(1.0, center + radius)


def _wilson(successes: int, total: int) -> tuple[float, float]:
    """Compatibility wrapper for the review-margin helpers below."""
    return wilson_interval(successes, total)


def _rate(successes: int, total: int) -> dict[str, object]:
    """Describe a rate with counts and its 95% Wilson interval."""
    if total == 0:
        return {"successes": successes, "total": total,
                "rate": None, "interval_95": None}
    low, high = wilson_interval(successes, total)
    return {"successes": successes, "total": total,
            "rate": successes / total, "interval_95": [low, high]}


def _timestamp(value: object, label: str) -> datetime:
    """Parse an offset-bearing ISO timestamp from a run record."""
    if not isinstance(value, str) or not value:
        raise ValueError("{} must be an ISO timestamp with a UTC offset".format(
            label))
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError("{} must be an ISO timestamp with a UTC offset".format(
            label)) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("{} must include a UTC offset".format(label))
    return parsed


def _score_run(record: object, label: str) -> tuple[str, float, datetime,
                                                        datetime]:
    """Validate one review-run record and price its observed token usage."""
    if not isinstance(record, dict):
        raise ValueError("{} run must be an object".format(label))
    verdict = record.get("verdict")
    if verdict not in ("approved", "rejected"):
        raise ValueError("{} verdict must be approved or rejected".format(
            label))
    tokens = usage._muse_tokens(record.get("token_usage"))
    if tokens is None:
        raise ValueError("{} token usage must be completely reported".format(
            label))
    cost = usage._muse_price(tokens, usage._muse_rates(record.get("model")))
    started_at = _timestamp(record.get("started_at"),
                            "{}.started_at".format(label))
    ended_at = _timestamp(record.get("ended_at"),
                          "{}.ended_at".format(label))
    if ended_at < started_at:
        raise ValueError("{}.ended_at must not precede started_at".format(
            label))
    return verdict, round(cost, 6), started_at, ended_at


def _paired_run_record(record: object, index: int) -> dict[str, object]:
    """Validate and summarize a paired A/B run for one versioned change."""
    label = "paired_runs[{}]".format(index)
    if not isinstance(record, dict):
        raise ValueError("{} must be an object".format(label))
    change_id = record.get("change_id")
    version = record.get("version")
    truth = record.get("truth")
    if not isinstance(change_id, str) or not change_id:
        raise ValueError("{}.change_id must be a non-empty string".format(label))
    if not isinstance(version, str) or not version:
        raise ValueError("{}.version must be a non-empty string".format(label))
    if truth not in ("bad", "good"):
        raise ValueError("{}.truth must be bad or good".format(label))

    a_verdict, a_cost, a_start, a_end = _score_run(
        record.get("a"), label + ".a")
    b_verdict, b_cost, b_start, b_end = _score_run(
        record.get("b"), label + ".b")
    return {
        "change_id": change_id,
        "version": version,
        "truth": truth,
        "a_verdict": a_verdict,
        "b_verdict": b_verdict,
        "a": {"cost_usd": a_cost,
              "latency_seconds": (a_end - a_start).total_seconds()},
        "b": {"cost_usd": b_cost,
              "latency_seconds": (b_end - b_start).total_seconds()},
        "paired_latency_seconds": (
            max(a_end, b_end) - min(a_start, b_start)).total_seconds(),
    }


def score_paired_runs(paired_runs: Sequence[dict]) -> dict[str, object]:
    """Score paired A/B verdicts and attribute each run's cost and latency.

    Each input row is one paired execution of reviewers A and B against the
    same change and packet-set ``version``. It contains ``change_id``,
    ``version``, ``truth`` (``bad`` or ``good``), and nested ``a``/``b`` run
    records. A run has ``verdict`` (``approved`` or ``rejected``), optional
    ``model``, complete ``token_usage`` (``input_tokens``, ``cached_tokens``,
    ``output_tokens``), and offset-bearing ``started_at``/``ended_at`` values.

    Bad-change misses are approvals. Good-change false blocks are rejections.
    Every measured rate carries its own Wilson interval; undefined rates and
    correlations remain ``None`` instead of being represented as zero.
    """
    if isinstance(paired_runs, (str, bytes)) or not isinstance(
            paired_runs, Sequence):
        raise ValueError("paired_runs must be a sequence of paired run records")
    scored = [_paired_run_record(record, index)
              for index, record in enumerate(paired_runs)]
    bad = [row for row in scored if row["truth"] == "bad"]
    good = [row for row in scored if row["truth"] == "good"]
    if not bad or not good:
        raise ValueError("paired runs must include bad and good changes")

    a_misses = both_miss = b_catches = joint_detected = 0
    only_a_misses = only_b_misses = both_detected = 0
    for row in bad:
        a_missed = row["a_verdict"] == "approved"
        b_missed = row["b_verdict"] == "approved"
        a_misses += a_missed
        both_miss += a_missed and b_missed
        b_catches += a_missed and not b_missed
        joint_detected += not (a_missed and b_missed)
        only_a_misses += a_missed and not b_missed
        only_b_misses += not a_missed and b_missed
        both_detected += not a_missed and not b_missed

    denominator = sqrt(
        (both_miss + only_a_misses)
        * (only_b_misses + both_detected)
        * (both_miss + only_b_misses)
        * (only_a_misses + both_detected)
    )
    phi = ((both_miss * both_detected - only_a_misses * only_b_misses)
           / denominator if denominator else None)
    if phi is not None:
        phi = max(-1.0, min(1.0, phi))

    false_blocks = sum(
        row["a_verdict"] == "rejected" or row["b_verdict"] == "rejected"
        for row in good)
    runs = []
    for row in scored:
        runs.append({
            "change_id": row["change_id"],
            "version": row["version"],
            "truth": row["truth"],
            "a": row["a"],
            "b": row["b"],
            "paired_latency_seconds": row["paired_latency_seconds"],
        })
    total_cost = sum(row["a"]["cost_usd"] + row["b"]["cost_usd"]
                     for row in scored)
    return {
        "bad_changes": len(bad),
        "good_changes": len(good),
        "b_catch_rate_among_a_misses": _rate(b_catches, a_misses),
        "both_miss_overlap": {
            **_rate(both_miss, len(bad)),
            "phi_correlation": phi,
        },
        "joint_detection_on_bad_changes": _rate(joint_detected, len(bad)),
        "joint_false_block_on_good_changes": _rate(false_blocks, len(good)),
        "paired_runs": runs,
        "total_cost_usd": round(total_cost, 6),
        "total_paired_latency_seconds": sum(
            row["paired_latency_seconds"] for row in scored),
    }


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
