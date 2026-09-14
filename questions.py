#!/usr/bin/env python3
"""Question registry and sufficiency checks for the proposal loop.

The outcome signal producer and the proposal writer are deliberately separate.
This module is the contract between them: the registry says which questions may
be asked, what each question is trying to establish, which named signals it
needs, and how much evidence is required before it may produce a finding.

Signal producers may add fields without changing the registry.  A signal is a
mapping with a non-negative ``sample_size`` and a ``status`` of ``available``
when its observations are complete.  The question-specific rows are carried
under ``by_schedule``, ``by_lane`` or ``by_pool`` as appropriate.  Missing
signals and partial signals fail closed as ``not enough evidence yet``.

This module does not create funnel items or mutate GitHub.  That is the proposal
routine's job; this is only the mechanical decision about whether a question is
ready to be considered.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


NOT_ENOUGH_EVIDENCE = "not enough evidence yet"
FINDING = "finding"

# These names are the stable seam between the registry and the signal producer.
# Keep names here rather than spelling them independently in each question so a
# later question can be added without changing the evaluator's protocol.
COST_PER_RUN_BY_OUTCOME = "cost_per_run_by_outcome"
FIRES_BY_SCHEDULE = "fires_by_schedule"
SESSIONS_PER_MERGED_PR = "sessions_per_merged_pr"
REVIEW_RUNS_PER_MERGE = "review_runs_per_merge"
COST_PER_MERGED_PR = "cost_per_merged_pr"
POINTS_PER_HOUR_FULL_QUEUE = "points_per_hour_full_queue"
FIRES_PER_HOUR = "fires_per_hour"
SELF_REVIEW_REJECTION_RATE = "self_review_rejection_rate"

KNOWN_SIGNAL_NAMES = (
    COST_PER_RUN_BY_OUTCOME,
    FIRES_BY_SCHEDULE,
    SESSIONS_PER_MERGED_PR,
    REVIEW_RUNS_PER_MERGE,
    COST_PER_MERGED_PR,
    POINTS_PER_HOUR_FULL_QUEUE,
    FIRES_PER_HOUR,
    SELF_REVIEW_REJECTION_RATE,
)


class QuestionError(ValueError):
    """The registry or a question request is invalid."""


class UnknownQuestionError(QuestionError, KeyError):
    """Raised when a caller asks the loop to ask an unregistered question."""


Evaluator = Callable[[Mapping[str, Mapping[str, object]]], Optional[Dict[str, object]]]


@dataclass(frozen=True)
class QuestionSpec:
    """One declarative question in the proposal contract.

    ``minimum_sample`` applies to every named dependency unless
    ``sample_scope`` is ``per_lane``.  The latter is used for the lane-cost
    comparison, where every lane must have the threshold rather than allowing
    a large lane to hide a small one in an aggregate.
    """

    name: str
    hypothesis: str
    signals: Tuple[str, ...]
    minimum_sample: int
    sample_unit: str
    sample_scope: str = "overall"
    evaluator: Evaluator = lambda _signals: None


# Short alias for callers that naturally refer to an entry as a question.
Question = QuestionSpec


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _integer(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _text(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_spec(spec: QuestionSpec) -> None:
    if not isinstance(spec, QuestionSpec) or not spec.name.strip():
        raise QuestionError("a question needs a non-empty name")
    if not spec.hypothesis.strip():
        raise QuestionError("question {} needs a hypothesis".format(spec.name))
    if not spec.signals or len(set(spec.signals)) != len(spec.signals):
        raise QuestionError(
            "question {} needs one or more unique signal names".format(spec.name)
        )
    if any(not isinstance(signal, str) or not signal.strip() for signal in spec.signals):
        raise QuestionError(
            "question {} has an empty signal name".format(spec.name)
        )
    if not isinstance(spec.minimum_sample, int) or isinstance(spec.minimum_sample, bool):
        raise QuestionError(
            "question {} needs an integer minimum sample".format(spec.name)
        )
    if spec.minimum_sample < 1:
        raise QuestionError(
            "question {} needs a positive minimum sample".format(spec.name)
        )
    if spec.sample_scope not in ("overall", "per_lane"):
        raise QuestionError(
            "question {} has an unsupported sample scope".format(spec.name)
        )
    if not callable(spec.evaluator):
        raise QuestionError("question {} needs an evaluator".format(spec.name))


def register_question(spec: QuestionSpec, *, replace: bool = False) -> None:
    """Add one question to the registry for a future proposal routine.

    Registration is intentionally explicit.  ``replace`` is opt-in so a
    misspelled or duplicated question cannot silently change what the loop is
    allowed to ask.
    """
    _validate_spec(spec)
    if spec.name in QUESTION_REGISTRY and not replace:
        raise QuestionError("question {} is already registered".format(spec.name))
    QUESTION_REGISTRY[spec.name] = spec


def _rows(signal: Mapping[str, object], *keys: str) -> List[Mapping[str, object]]:
    for key in keys:
        value = signal.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    # A scalar signal is still a usable one-row input.  Keeping this fallback
    # makes extensions cheap: a producer can start with one overall value and
    # add scoped rows later without changing the question evaluator API.
    return [signal]


def _row_key(row: Mapping[str, object]) -> Optional[str]:
    for key in ("schedule", "lane", "pool", "name", "id"):
        value = _text(row.get(key))
        if value:
            return value
    return None


def _row_value(row: Mapping[str, object], *keys: str) -> Optional[float]:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _signal_sample(signal: Mapping[str, object]) -> Optional[int]:
    for key in ("sample_size", "sample", "observations", "records"):
        value = _integer(signal.get(key))
        if value is not None:
            return value
    return None


def _signal_status(signal: Mapping[str, object], sample: Optional[int]) -> str:
    status = _text(signal.get("status"))
    if status:
        return status
    # A small fixture or an extension can omit the redundant status; the
    # explicit sample still provides a bounded, inspectable contract.
    return "available" if sample is not None else "unknown"


def _shortfall(
    signal_name: str,
    spec: QuestionSpec,
    *,
    observed: Optional[int],
    status: str,
    scope: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, object]:
    amount = None if observed is None else max(0, spec.minimum_sample - observed)
    if reason is None:
        if observed is None:
            reason = "sample size is unavailable"
        elif amount:
            reason = "sample is below the minimum"
        elif status != "available":
            reason = "signal status is {}".format(status)
    result: Dict[str, object] = {
        "signal": signal_name,
        "observed": observed,
        "required": spec.minimum_sample,
        "shortfall": amount,
        "unit": spec.sample_unit,
        "signal_status": status,
        "reason": reason,
    }
    if scope is not None:
        result["scope"] = scope
    return result


def _scope_shortfalls(
    signal_name: str,
    signal: Mapping[str, object],
    spec: QuestionSpec,
) -> List[Dict[str, object]]:
    """Return missing evidence for one signal, preserving its scope."""
    sample = _signal_sample(signal)
    status = _signal_status(signal, sample)

    if spec.sample_scope != "per_lane":
        if sample is None or sample < spec.minimum_sample or status != "available":
            return [_shortfall(
                signal_name,
                spec,
                observed=sample,
                status=status,
            )]
        return []

    rows = _rows(signal, "by_lane", "rows")
    if rows == [signal]:
        # A per-lane question cannot honestly infer lane coverage from an
        # aggregate sample.  Name the missing scope rather than treating the
        # aggregate as every lane.
        return [_shortfall(
            signal_name,
            spec,
            observed=sample,
            status=status,
            scope="lane coverage",
            reason="per-lane sample is unavailable",
        )]

    found: List[Dict[str, object]] = []
    for index, row in enumerate(rows):
        lane = _row_key(row) or "lane {}".format(index + 1)
        observed = None
        for key in ("merged_prs", "sample_size", "sample", "records"):
            observed = _integer(row.get(key))
            if observed is not None:
                break
        row_status = _signal_status(row, observed)
        if observed is None or observed < spec.minimum_sample or row_status != "available":
            found.append(_shortfall(
                signal_name,
                spec,
                observed=observed,
                status=row_status,
                scope=lane,
            ))
    if not rows:
        found.append(_shortfall(
            signal_name,
            spec,
            observed=0,
            status=status,
            scope="lane coverage",
            reason="no lanes are present",
        ))
    return found


def _normalise_signals(value: Mapping[str, object]) -> Mapping[str, Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise QuestionError("signals must be a mapping")
    nested = value.get("signals")
    if isinstance(nested, Mapping):
        value = nested
    return {
        str(name): signal
        for name, signal in value.items()
        if isinstance(name, str) and isinstance(signal, Mapping)
    }


def _empty_fire_finding(
    signals: Mapping[str, Mapping[str, object]],
) -> Optional[Dict[str, object]]:
    costs = _rows(signals[COST_PER_RUN_BY_OUTCOME], "by_schedule", "rows")
    fires = _rows(signals[FIRES_BY_SCHEDULE], "by_schedule", "rows")
    fires_by_name = {_row_key(row): row for row in fires if _row_key(row)}
    details = []
    for cost_row in costs:
        schedule = _row_key(cost_row) or "overall"
        fire_row = fires_by_name.get(schedule, {})
        empty_cost = _row_value(
            cost_row, "empty_fire_cost", "empty_cost_per_run", "empty_cost"
        )
        empty_fires = _row_value(
            cost_row, "empty_fires", "empty_fire_count"
        )
        if empty_fires is None:
            empty_fires = _row_value(fire_row, "empty_fires", "empty_fire_count")
        worked_cost = _row_value(
            cost_row, "worked_cost", "worked_cost_total", "work_cost"
        )
        if empty_cost is None or empty_fires is None or worked_cost is None:
            continue
        empty_total = empty_cost * empty_fires
        details.append({
            "schedule": schedule,
            "empty_fire_cost": round(empty_cost, 6),
            "empty_fires": int(empty_fires),
            "empty_fire_total": round(empty_total, 6),
            "worked_cost": round(worked_cost, 6),
            "exceeds": empty_total > worked_cost,
        })
    if not details:
        return None
    matches = [row for row in details if row["exceeds"]]
    return {
        "supports_hypothesis": bool(matches),
        "finding": (
            "empty-fire cost exceeds worked cost for {}"
            .format(", ".join(row["schedule"] for row in matches))
            if matches else
            "no schedule has empty-fire cost above worked cost"
        ),
        "details": details,
    }


def _sessions_finding(
    signals: Mapping[str, Mapping[str, object]],
) -> Optional[Dict[str, object]]:
    sessions = _rows(signals[SESSIONS_PER_MERGED_PR], "by_lane", "rows")
    review_rows = _rows(signals[REVIEW_RUNS_PER_MERGE], "by_lane", "rows")
    review_by_name = {_row_key(row): row for row in review_rows if _row_key(row)}
    details = []
    for row in sessions:
        lane = _row_key(row) or "overall"
        value = _row_value(row, "sessions_per_merged_pr", "value", "rate")
        if value is None:
            continue
        review = review_by_name.get(lane, {})
        review_value = _row_value(
            review, "review_runs_per_merge", "value", "rate"
        )
        details.append({
            "lane": lane,
            "sessions_per_merged_pr": round(value, 6),
            "review_runs_per_merge": (
                round(review_value, 6) if review_value is not None else None
            ),
            "exceeds": value > 1.3,
        })
    if not details:
        return None
    matches = [row for row in details if row["exceeds"]]
    return {
        "supports_hypothesis": bool(matches),
        "finding": (
            "sessions per merged PR exceed 1.3 for {}"
            .format(", ".join(row["lane"] for row in matches))
            if matches else
            "sessions per merged PR do not exceed 1.3 in any lane"
        ),
        "details": details,
    }


def _lane_cost_finding(
    signals: Mapping[str, Mapping[str, object]],
) -> Optional[Dict[str, object]]:
    rows = _rows(signals[COST_PER_MERGED_PR], "by_lane", "rows")
    values = []
    for row in rows:
        value = _row_value(row, "cost_per_merged_pr", "value", "rate")
        if value is None:
            continue
        values.append({
            "lane": _row_key(row) or "overall",
            "unit": _text(row.get("unit")) or "unknown",
            "cost_per_merged_pr": value,
        })
    if not values:
        return None

    details = []
    matches = []
    for unit in sorted({row["unit"] for row in values}):
        group = [row for row in values if row["unit"] == unit]
        cheapest = min(row["cost_per_merged_pr"] for row in group)
        for row in group:
            if cheapest == 0:
                ratio = math.inf if row["cost_per_merged_pr"] > 0 else 1.0
            else:
                ratio = row["cost_per_merged_pr"] / cheapest
            detail = {
                "lane": row["lane"],
                "unit": unit,
                "cost_per_merged_pr": round(row["cost_per_merged_pr"], 6),
                "cheapest_cost_per_merged_pr": round(cheapest, 6),
                "ratio_to_cheapest": (
                    None if math.isinf(ratio) else round(ratio, 6)
                ),
                "exceeds_tenfold": ratio > 10,
            }
            details.append(detail)
            if detail["exceeds_tenfold"]:
                matches.append(detail)
    return {
        "supports_hypothesis": bool(matches),
        "finding": (
            "a lane costs more than 10x the cheapest lane: {}"
            .format(", ".join(row["lane"] for row in matches))
            if matches else
            "no lane costs more than 10x the cheapest comparable lane"
        ),
        "details": details,
    }


def _pool_projection_finding(
    signals: Mapping[str, Mapping[str, object]],
) -> Optional[Dict[str, object]]:
    projections = _rows(
        signals[POINTS_PER_HOUR_FULL_QUEUE], "by_pool", "rows"
    )
    fires = _rows(signals[FIRES_PER_HOUR], "by_pool", "rows")
    fires_by_name = {_row_key(row): row for row in fires if _row_key(row)}
    details = []
    for row in projections:
        pool = _row_key(row) or "overall"
        projected = _row_value(
            row, "projected_week_percent", "projected_week_pct"
        )
        if projected is None:
            points = _row_value(row, "points_per_hour", "value")
            capacity = _row_value(row, "week_capacity_points", "capacity_points")
            hours = _row_value(row, "stable_hours", "hours_per_week")
            if points is not None and capacity not in (None, 0) and hours is not None:
                projected = points * hours / capacity * 100
        fire_row = fires_by_name.get(pool, {})
        fires_value = _row_value(fire_row, "fires_per_hour", "value", "rate")
        if projected is None:
            continue
        details.append({
            "pool": pool,
            "projected_week_percent": round(projected, 6),
            "fires_per_hour": (
                round(fires_value, 6) if fires_value is not None else None
            ),
            "exceeds": projected > 100,
        })
    if not details:
        return None
    matches = [row for row in details if row["exceeds"]]
    return {
        "supports_hypothesis": bool(matches),
        "finding": (
            "projected week exceeds 100% for {}"
            .format(", ".join(row["pool"] for row in matches))
            if matches else
            "no pool projects above 100% of its week"
        ),
        "details": details,
    }


def _self_review_finding(
    signals: Mapping[str, Mapping[str, object]],
) -> Optional[Dict[str, object]]:
    rows = _rows(signals[SELF_REVIEW_REJECTION_RATE], "by_lane", "rows")
    details = []
    for row in rows:
        lane = _row_key(row) or "overall"
        self_rate = _row_value(
            row, "self_review_rejection_rate", "self_rate", "value"
        )
        other_rate = _row_value(
            row, "other_rejection_rate", "comparison_rate", "baseline_rate"
        )
        if self_rate is None or other_rate is None:
            continue
        details.append({
            "lane": lane,
            "self_review_rejection_rate": round(self_rate, 6),
            "other_rejection_rate": round(other_rate, 6),
            "exceeds": self_rate > other_rate,
        })
    if not details:
        return None
    matches = [row for row in details if row["exceeds"]]
    return {
        "supports_hypothesis": bool(matches),
        "finding": (
            "self-reviewed merges reject or revert more often for {}"
            .format(", ".join(row["lane"] for row in matches))
            if matches else
            "self-reviewed merges do not reject or revert more often"
        ),
        "details": details,
    }


QUESTION_REGISTRY: Dict[str, QuestionSpec] = {}


def _seed_registry() -> None:
    entries = (
        QuestionSpec(
            name="empty_fire_cost",
            hypothesis=(
                "empty-fire cost multiplied by fires exceeds worked cost "
                "for a schedule"
            ),
            signals=(COST_PER_RUN_BY_OUTCOME, FIRES_BY_SCHEDULE),
            minimum_sample=1,
            sample_unit="full reset cycles",
            evaluator=_empty_fire_finding,
        ),
        QuestionSpec(
            name="sessions_per_merged_pr",
            hypothesis=(
                "sessions per merged PR are above 1.3, so re-hands and "
                "re-reviews dominate"
            ),
            signals=(SESSIONS_PER_MERGED_PR, REVIEW_RUNS_PER_MERGE),
            minimum_sample=30,
            sample_unit="merged PRs",
            evaluator=_sessions_finding,
        ),
        QuestionSpec(
            name="lane_cost_gap",
            hypothesis=(
                "a lane's cost per merged PR exceeds the cheapest lane's "
                "by 10x"
            ),
            signals=(COST_PER_MERGED_PR,),
            minimum_sample=10,
            sample_unit="merged PRs per lane",
            sample_scope="per_lane",
            evaluator=_lane_cost_finding,
        ),
        QuestionSpec(
            name="pool_week_projection",
            hypothesis=(
                "a pool's projected week exceeds 100% at the current cadence"
            ),
            signals=(POINTS_PER_HOUR_FULL_QUEUE, FIRES_PER_HOUR),
            minimum_sample=8,
            sample_unit="stable hours with a full queue",
            evaluator=_pool_projection_finding,
        ),
        QuestionSpec(
            name="self_review_rejection",
            hypothesis=(
                "self-reviewed merges are rejected or reverted more often "
                "than other merges"
            ),
            signals=(SELF_REVIEW_REJECTION_RATE,),
            minimum_sample=20,
            sample_unit="self-reviewed merges",
            evaluator=_self_review_finding,
        ),
    )
    for entry in entries:
        register_question(entry)


_seed_registry()


def _shortfall_text(row: Mapping[str, object]) -> str:
    signal = str(row.get("signal"))
    scope = row.get("scope")
    label = signal if scope is None else "{} ({})".format(signal, scope)
    shortfall = row.get("shortfall")
    unit = row.get("unit") or "samples"
    if isinstance(shortfall, int):
        amount = "short by {} {}".format(shortfall, unit)
    else:
        amount = "has an unknown sample shortfall ({})".format(unit)
    reason = _text(row.get("reason"))
    return "{} {}".format(label, amount) + (
        "; {}".format(reason) if reason else ""
    )


def evaluate_question(
    name: str,
    signals: Mapping[str, object],
) -> Dict[str, object]:
    """Evaluate one registered question against a signal summary.

    The returned ``status`` is either ``finding`` or the literal
    ``not enough evidence yet``.  An unknown name raises instead of silently
    allowing a caller to ask an unreviewed question.
    """
    if name not in QUESTION_REGISTRY:
        raise UnknownQuestionError("question {} is not registered".format(name))
    spec = QUESTION_REGISTRY[name]
    signal_map = _normalise_signals(signals)
    shortfalls: List[Dict[str, object]] = []
    for signal_name in spec.signals:
        signal = signal_map.get(signal_name)
        if not isinstance(signal, Mapping):
            shortfalls.append(_shortfall(
                signal_name,
                spec,
                observed=0,
                status="missing",
                reason="signal is unavailable",
            ))
            continue
        shortfalls.extend(_scope_shortfalls(signal_name, signal, spec))

    result: Dict[str, object] = {
        "question": spec.name,
        "hypothesis": spec.hypothesis,
        "signals": list(spec.signals),
        "minimum_sample": spec.minimum_sample,
        "sample_unit": spec.sample_unit,
        "sample_scope": spec.sample_scope,
    }
    if shortfalls:
        result["status"] = NOT_ENOUGH_EVIDENCE
        result["shortfalls"] = shortfalls
        result["reason"] = "{}: {}".format(
            NOT_ENOUGH_EVIDENCE,
            "; ".join(_shortfall_text(row) for row in shortfalls),
        )
        return result

    try:
        finding = spec.evaluator(signal_map)
    except (KeyError, TypeError, ValueError):
        finding = None
    if finding is None:
        # A complete sample with no computable value is still not evidence.
        # Reuse the named dependencies so the caller knows what producer must
        # be extended, rather than returning a false negative finding.
        result["status"] = NOT_ENOUGH_EVIDENCE
        result["shortfalls"] = [
            _shortfall(
                signal_name,
                spec,
                observed=_signal_sample(signal_map.get(signal_name, {})),
                status=_signal_status(
                    signal_map.get(signal_name, {}),
                    _signal_sample(signal_map.get(signal_name, {})),
                ),
                reason="required measurement fields are unavailable",
            )
            for signal_name in spec.signals
        ]
        result["reason"] = "{}: required measurement fields are unavailable".format(
            NOT_ENOUGH_EVIDENCE
        )
        return result

    result["status"] = FINDING
    result.update(finding)
    return result


def evaluate_questions(
    signals: Mapping[str, object],
    question_names: Optional[Iterable[str]] = None,
) -> Dict[str, object]:
    """Evaluate all registered questions, or an explicit registered subset."""
    names = list(QUESTION_REGISTRY) if question_names is None else list(question_names)
    unknown = [name for name in names if name not in QUESTION_REGISTRY]
    if unknown:
        raise UnknownQuestionError(
            "question(s) not registered: {}".format(", ".join(unknown))
        )
    return {
        "schema_version": 1,
        "source": "question_registry",
        "questions": {
            name: evaluate_question(name, signals)
            for name in names
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Read a JSON signal summary from stdin and print question results."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--question", action="append", dest="questions", default=None,
        help="evaluate only this registered question; repeat for more",
    )
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        print(json.dumps(
            evaluate_questions(payload, args.questions),
            indent=2,
            sort_keys=True,
        ))
        return 0
    except (OSError, ValueError, QuestionError) as exc:
        print("questions: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
