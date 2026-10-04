"""Run the pre-registered review evaluation as a nonblocking observation."""

from __future__ import annotations

import math
import pathlib
from typing import Callable, Optional, Sequence

from engine import replay
from engine import review_decisions
from engine import review_packets
from engine import review_prompts


CHECKOUT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_ROOT = replay.RUNTIME_ROOT
PACKET_VERSION = review_packets.DEFAULT_VERSION
PACKET_NAMES = ("must_reject", "must_approve")


class ReviewEvaluationError(ValueError):
    """The evaluation inputs cannot be scored safely."""


def _integer_count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewEvaluationError("{} must be a non-negative integer".format(
            label))
    return value


def fisher_decline_p_value(head_rejected: int, head_total: int,
                           main_rejected: int, main_total: int) -> float:
    """One-sided Fisher exact p-value for lower head rejection than main."""
    head_rejected = _integer_count(head_rejected, "head rejected count")
    head_total = _integer_count(head_total, "head total")
    main_rejected = _integer_count(main_rejected, "main rejected count")
    main_total = _integer_count(main_total, "main total")
    if (head_total == 0 or main_total == 0
            or head_rejected > head_total or main_rejected > main_total):
        raise ReviewEvaluationError("Fisher counts are out of range")

    draws = head_total
    population = head_total + main_total
    rejected = head_rejected + main_rejected
    denominator = math.comb(population, draws)
    first = max(0, draws - (population - rejected))
    last = min(head_rejected, draws, rejected)
    numerator = sum(
        math.comb(rejected, selected)
        * math.comb(population - rejected, draws - selected)
        for selected in range(first, last + 1)
    )
    return numerator / denominator


def _replay_run_counts(summary: dict, packet_name: str, runs: int
                       ) -> tuple[int, int, list[dict], list[dict]]:
    if not isinstance(summary, dict) or summary.get("packet") != packet_name:
        raise replay.ReplayError("review replay returned an unexpected packet")
    head = summary.get("head")
    main = summary.get("main")
    if not isinstance(head, dict) or not isinstance(main, dict):
        raise replay.ReplayError("review replay returned incomplete sides")
    head_runs = head.get("runs")
    main_runs = main.get("runs")
    if (not isinstance(head_runs, list) or len(head_runs) != runs
            or not isinstance(main_runs, list) or len(main_runs) != runs):
        raise replay.ReplayError("review replay returned an incomplete sample")
    for records in (head_runs, main_runs):
        if (any(not isinstance(record, dict)
                or record.get("verdict") not in replay.EXPECTED_VERDICTS
                for record in records)):
            raise replay.ReplayError("review replay returned an invalid verdict")
    return (
        sum(record["verdict"] == "rejected" for record in head_runs),
        sum(record["verdict"] == "rejected" for record in main_runs),
        head_runs,
        main_runs,
    )


def _rate_text(rate: dict) -> str:
    successes = rate.get("successes")
    total = rate.get("total")
    value = rate.get("rate")
    if (not isinstance(successes, int) or not isinstance(total, int)
            or total <= 0 or not isinstance(value, (int, float))):
        return "n/a"
    text = "{}/{} ({:.1%})".format(successes, total, value)
    interval = rate.get("interval_95")
    if (isinstance(interval, (list, tuple)) and len(interval) == 2
            and all(isinstance(bound, (int, float)) for bound in interval)):
        text = "{}/{} ({:.1%}; 95% CI {:.1%}–{:.1%})".format(
            successes, total, value, *interval)
    return text


def render_cost_table(variant: str, scored_pairs: dict) -> str:
    """Render one cost, latency, and joint-rate table for paired A+B runs."""
    if variant not in ("baseline", "r1", "r2", "r4"):
        raise ReviewEvaluationError("unknown reviewer-A variant")
    paired_runs = scored_pairs.get("paired_runs") if isinstance(
        scored_pairs, dict) else None
    if not isinstance(paired_runs, list) or not paired_runs:
        raise ReviewEvaluationError("scored paired runs are unavailable")

    a_cost = sum(run["a"]["cost_usd"] for run in paired_runs)
    b_cost = sum(run["b"]["cost_usd"] for run in paired_runs)
    a_latency = sum(run["a"]["latency_seconds"] for run in paired_runs)
    b_latency = sum(run["b"]["latency_seconds"] for run in paired_runs)
    paired_latency = sum(run["paired_latency_seconds"]
                         for run in paired_runs)
    pair_count = len(paired_runs)
    overlap = scored_pairs["both_miss_overlap"]
    phi = overlap.get("phi_correlation")
    overlap_text = _rate_text(overlap)
    if isinstance(phi, (int, float)):
        overlap_text += "; phi={:.3f}".format(phi)

    rows = [
        "| Measure | Runs | Cost (USD) | Latency (s) | Rate (95% CI) |",
        "|---|---:|---:|---:|---:|",
        "| Reviewer A ({}) | {} | ${:.3f} | {:.1f} | — |".format(
            variant, pair_count, a_cost, a_latency),
        "| Reviewer B | {} | ${:.3f} | {:.1f} | — |".format(
            pair_count, b_cost, b_latency),
        "| A+B paired total | {} | ${:.3f} | {:.1f} | — |".format(
            pair_count * 2, a_cost + b_cost, paired_latency),
        "| B catches among A misses | {} | — | — | {} |".format(
            scored_pairs["b_catch_rate_among_a_misses"].get("total", 0),
            _rate_text(scored_pairs["b_catch_rate_among_a_misses"])),
        "| Both-miss overlap | {} | — | — | {} |".format(
            overlap.get("total", 0), overlap_text),
        "| Joint detection on bad changes | {} | — | — | {} |".format(
            scored_pairs["joint_detection_on_bad_changes"].get("total", 0),
            _rate_text(scored_pairs["joint_detection_on_bad_changes"])),
        "| Joint false blocks on good changes | {} | — | — | {} |".format(
            scored_pairs["joint_false_block_on_good_changes"].get("total", 0),
            _rate_text(scored_pairs["joint_false_block_on_good_changes"])),
    ]
    return "\n".join(rows)


def _count_text(value: object, total: object) -> str:
    if (isinstance(value, bool) or not isinstance(value, int) or value < 0
            or isinstance(total, bool) or not isinstance(total, int)
            or total <= 0 or value > total):
        raise ReviewEvaluationError("regression evidence counts are invalid")
    return "{}/{} ({:.1%})".format(value, total, value / total)


def _comparison_evidence(label: str, counts: dict, decision: dict) -> str:
    if not isinstance(counts, dict) or not isinstance(decision, dict):
        raise ReviewEvaluationError("regression comparison is unavailable")
    total = counts.get("runs_per_side")
    head = counts.get("head_rejected")
    main = counts.get("main_rejected")
    difference = decision.get("difference")
    interval = decision.get("interval_95")
    margin = decision.get("margin")
    status = decision.get("status")
    if (not isinstance(difference, (int, float))
            or not isinstance(interval, (list, tuple)) or len(interval) != 2
            or not all(isinstance(item, (int, float)) for item in interval)
            or not isinstance(margin, (int, float))
            or status not in ("safe", "regression", "inconclusive")):
        raise ReviewEvaluationError("regression comparison is malformed")
    return (
        "- {}: head {}, main {}; head−main {:+.1f} pp "
        "(95% Newcombe CI {:+.1f} to {:+.1f} pp; margin {:+.1f} pp; {})."
    ).format(
        label, _count_text(head, total), _count_text(main, total),
        difference * 100, interval[0] * 100, interval[1] * 100,
        margin * 100, status,
    )


def render_parent_issue_evidence(result: dict) -> str:
    """Render aggregate-only evidence for one comment on the trial parent."""
    if not isinstance(result, dict) or result.get("status") not in (
            "complete", "skipped"):
        raise ReviewEvaluationError("review evaluation result is unavailable")
    variant = result.get("variant")
    version = result.get("packet_version")
    checksums = result.get("packet_checksums")
    definition = result.get("trial_definition")
    policy = result.get("evaluation_policy")
    if (variant not in ("baseline", "r1", "r2", "r4")
            or not isinstance(version, str)
            or not isinstance(checksums, dict)
            or not isinstance(definition, dict)
            or not isinstance(policy, dict)):
        raise ReviewEvaluationError("review evaluation metadata is incomplete")
    required_checksums = ("must_reject", "must_approve")
    if any(not isinstance(checksums.get(name), str)
           or len(checksums[name]) != 64
           for name in required_checksums):
        raise ReviewEvaluationError("review packet checksums are unavailable")

    sequential = policy.get("sequential")
    if not isinstance(sequential, dict):
        raise ReviewEvaluationError("review sample plan is unavailable")
    lines = [
        "### Reviewer trial run evidence",
        "",
        "- Scope: {}; window: {} days; reviewer variant: `{}`.".format(
            definition.get("scope"), definition.get("duration_days"), variant),
        "- Packet set: `{}`; must-reject SHA-256 `{}`; must-approve SHA-256 `{}`.".format(
            version, checksums["must_reject"], checksums["must_approve"]),
        "- Evaluation status: `{}`; trial enabled: {}; merge blocking: no.".format(
            result["status"], "yes" if result.get("trial_enabled") else "no"),
        "- Sample plan: {} initial runs per side; batches of {}; maximum {} per side.".format(
            sequential.get("initial_runs_per_side"),
            sequential.get("additional_runs_per_side"),
            sequential.get("max_runs_per_side")),
        "- Sequential sample-size signals: one-sided Fisher p < {:.2f} for decline; p > {:.2f} for futility. The p-value selects sample size only.".format(
            sequential.get("stop_for_decline_fisher_p_below"),
            sequential.get("stop_for_futility_fisher_p_above")),
    ]

    head_sha = result.get("head_commit_sha")
    main_sha = result.get("main_commit_sha")
    if result["status"] == "complete":
        if (not isinstance(head_sha, str) or not head_sha
                or not isinstance(main_sha, str) or not main_sha):
            raise ReviewEvaluationError("review commit identities are unavailable")
        outcome = result.get("outcome")
        evidence = result.get("regression_evidence")
        if (not isinstance(outcome, dict)
                or outcome.get("outcome") not in (
                    "pass", "regression", "inconclusive")
                or not isinstance(evidence, dict)):
            raise ReviewEvaluationError("review outcome evidence is incomplete")
        lines.extend([
            "- Commit pair: head `{}`; current main `{}`.".format(
                head_sha, main_sha),
            "- Outcome: `{}`; classification is observational and nonblocking.".format(
                outcome["outcome"]),
            "",
            "#### Head versus current main",
            "",
            "| Look | Runs per side | Must-reject head / main | One-sided Fisher p |",
            "|---:|---:|---:|---:|",
        ])
        for index, look in enumerate(result.get("looks", []), start=1):
            if not isinstance(look, dict):
                raise ReviewEvaluationError("review sample look is malformed")
            lines.append("| {} | {} | {} / {} | {:.4f} |".format(
                index, look.get("runs_per_side"),
                look.get("must_reject_head_rejected"),
                look.get("must_reject_main_rejected"),
                look.get("decline_p_value")))
        lines.extend([
            "",
            _comparison_evidence(
                "Must-reject rejection rate", evidence.get("must_reject"),
                outcome.get("must_reject")),
            _comparison_evidence(
                "Must-approve false-block rate",
                evidence.get("must_approve_false_blocks"),
                outcome.get("must_approve_false_blocks")),
        ])
        joint = evidence.get("joint_false_blocks")
        joint_decision = outcome.get("joint_false_blocks")
        if not isinstance(joint, dict) or not isinstance(joint_decision, dict):
            raise ReviewEvaluationError("paired false-block evidence is unavailable")
        joint_total = joint.get("good_changes")
        lines.append(
            "- Joint false blocks versus reviewer A: A+B {}; A {}; "
            "head−main {:+.1f} pp (95% Newcombe CI {:+.1f} to {:+.1f} pp; "
            "margin {:+.1f} pp; {}).".format(
                _count_text(joint.get("joint_rejected"), joint_total),
                _count_text(joint.get("reviewer_a_rejected"), joint_total),
                joint_decision["difference"] * 100,
                joint_decision["interval_95"][0] * 100,
                joint_decision["interval_95"][1] * 100,
                joint_decision["margin"] * 100,
                joint_decision["status"],
            ))
        paired = result.get("paired")
        if not isinstance(paired, dict):
            raise ReviewEvaluationError("paired A+B metrics are unavailable")
        lift = outcome.get("joint_detection_lift")
        if (not isinstance(lift, dict)
                or not isinstance(lift.get("rate"), (int, float))
                or not isinstance(lift.get("interval_95"), (list, tuple))
                or len(lift["interval_95"]) != 2
                or not isinstance(lift.get("status"), str)):
            raise ReviewEvaluationError("joint detection lift is unavailable")
        lines.extend([
            "",
            "#### Paired A+B system measurements",
            "",
            "- Added detection over reviewer A: {:.1f} pp (95% Wilson CI {:.1f} to {:.1f} pp; meaningful margin {:.1f} pp; {}).".format(
                lift["rate"] * 100,
                lift["interval_95"][0] * 100,
                lift["interval_95"][1] * 100,
                lift["meaningful_margin"] * 100,
                lift["status"]),
            "- B catches among A misses: {}.".format(
                _rate_text(paired.get("b_catch_rate_among_a_misses", {}))),
            "- Both-miss overlap and phi correlation: {}.".format(
                _rate_text(paired.get("both_miss_overlap", {}))
                + ("; phi={:.3f}".format(
                    paired["both_miss_overlap"]["phi_correlation"])
                   if isinstance(paired.get("both_miss_overlap"), dict)
                   and isinstance(paired["both_miss_overlap"].get(
                       "phi_correlation"), (int, float)) else "")),
            "- Joint detection on bad changes: {}.".format(
                _rate_text(paired.get("joint_detection_on_bad_changes", {}))),
            "- Joint false blocks on good changes: {}.".format(
                _rate_text(paired.get("joint_false_block_on_good_changes", {}))),
            "",
            "Cost uses token-priced USD equivalents and is not a provider bill.",
            "",
            result.get("cost_table", ""),
            "",
            "No packet contents or owner-local replay records are included.",
        ])
        if outcome["outcome"] == "inconclusive":
            lines.append(
                "Inconclusive is not a pass; report the result without treating it as evidence to ship a prompt change.")
    else:
        reason = result.get("skip_reason", "replay unavailable")
        lines.extend([
            "- Commit pair: head `{}`; current main `{}`.".format(
                head_sha or "unavailable", main_sha or "unavailable"),
            "- Quality outcome: not measured; replay was skipped (`{}`).".format(
                reason),
            "- No regression, pass, or inconclusive quality classification is claimed.",
            "",
            result.get("cost_table", ""),
            "",
            "No packet contents or owner-local replay records are included.",
        ])
    return "\n".join(lines)


def run_eval_nonblocking(
        variant: str, paired_runs: Sequence[dict], *,
        head_checkout: str | pathlib.Path = CHECKOUT,
        main_checkout: str | pathlib.Path | None = None,
        runtime_root: str | pathlib.Path = RUNTIME_ROOT,
        trial_spent_dollars=None,
        replay_fn: Optional[Callable[..., dict]] = None) -> dict:
    """Replay head vs main and score A+B records without gating a merge.

    Muse replay calls use ``engine.replay``'s existing quota hold and pace
    brake. The main-side run pool remains keyed by the exact main commit in
    ``eval-replay/`` under the runtime root. ``paired_runs`` contains already
    collected A/B records; this function neither selects reviewer B nor
    enables the prompt trial. ``trial_spent_dollars`` is the prior total read
    from the durable GitHub trial record; if it is absent or unreadable, the
    replay budget gate refuses before starting an engine run.
    """
    try:
        head_path, manifest = review_prompts._manifest(head_checkout)
        review_prompts.render_variant(head_path, variant)
    except (OSError, review_prompts.ReviewPromptError) as exc:
        raise ReviewEvaluationError("reviewer-A variant is unavailable") from exc

    trial_definition = manifest["trial_definition"]
    evaluation_policy = manifest["round_2_selection"]["evaluation"]
    packet_version = evaluation_policy["packet_version"]
    try:
        packet_checksums = review_packets.verify_checksums(packet_version)
    except review_packets.PacketSetError as exc:
        raise ReviewEvaluationError(
            "the versioned trial packet set is unavailable"
        ) from exc

    scored_pairs = review_decisions.score_paired_runs(paired_runs)
    table = render_cost_table(variant, scored_pairs)
    root = pathlib.Path(runtime_root).expanduser().resolve()
    main_path = (pathlib.Path(main_checkout).expanduser().resolve()
                 if main_checkout is not None else root / "command-center-run")
    if replay_fn is None:
        replay_fn = replay.replay_packet

    result = {
        "status": "skipped",
        "variant": variant,
        "trial_enabled": manifest["trial_enabled"],
        "trial_definition": trial_definition,
        "evaluation_policy": evaluation_policy,
        "packet_version": packet_version,
        "packet_checksums": packet_checksums,
        "merge_blocking": False,
        "looks": [],
        "look_count": 0,
        "outcome": None,
        "regression_evidence": None,
        "paired": scored_pairs,
        "cost_table": table,
    }
    head_samples = {name: [] for name in PACKET_NAMES}
    main_samples = {name: [] for name in PACKET_NAMES}
    prior_head_sha = None
    prior_main_sha = None
    runs_per_side = 0
    decline_p_value = None
    while True:
        additional = review_decisions.next_batch(
            runs_per_side, decline_p_value)
        if additional == 0:
            break
        runs_per_side += additional
        packet_results = {}
        try:
            for packet_name in PACKET_NAMES:
                summary = replay_fn(
                    packet_name, head_path, main_path, runs=runs_per_side,
                    version=packet_version, runtime_root=root,
                    head_runs=head_samples[packet_name],
                    replay_run_p90_dollars=(
                        replay.REVIEW_EVALUATION_REPLAY_P90_DOLLARS),
                    trial_spent_dollars=trial_spent_dollars,
                )
                head_rejected, main_rejected, observed_head, observed_main = (
                    _replay_run_counts(summary, packet_name, runs_per_side))
                head_sha = summary["head"].get("commit_sha")
                main_sha = summary["main"].get("commit_sha")
                if (not isinstance(head_sha, str) or not head_sha
                        or not isinstance(main_sha, str) or not main_sha):
                    raise replay.ReplayError(
                        "review replay returned no commit identity")
                if (prior_head_sha is not None and head_sha != prior_head_sha
                        or prior_main_sha is not None
                        and main_sha != prior_main_sha):
                    raise replay.ReplayError(
                        "head or main moved during evaluation")
                if (observed_head[:len(head_samples[packet_name])]
                        != head_samples[packet_name]
                        or observed_main[:len(main_samples[packet_name])]
                        != main_samples[packet_name]):
                    raise replay.ReplayError(
                        "review replay changed earlier samples")
                prior_head_sha, prior_main_sha = head_sha, main_sha
                head_samples[packet_name] = observed_head
                main_samples[packet_name] = observed_main
                packet_results[packet_name] = (
                    head_rejected, main_rejected)
        except replay.ReplayError:
            result["look_count"] = len(result["looks"])
            result["skip_reason"] = "replay unavailable under the existing lane limits"
            return result

        must_reject_head, must_reject_main = packet_results["must_reject"]
        decline_p_value = fisher_decline_p_value(
            must_reject_head, runs_per_side,
            must_reject_main, runs_per_side,
        )
        result["looks"].append({
            "runs_per_side": runs_per_side,
            "must_reject_head_rejected": must_reject_head,
            "must_reject_main_rejected": must_reject_main,
            "decline_p_value": decline_p_value,
        })
        result["look_count"] = len(result["looks"])

    must_reject_head, must_reject_main = packet_results["must_reject"]
    must_approve_head, must_approve_main = packet_results["must_approve"]
    pair_good = scored_pairs["good_changes"]
    a_false_blocks = sum(
        row["truth"] == "good" and row["a"]["verdict"] == "rejected"
        for row in paired_runs
    )
    joint_false_blocks = scored_pairs["joint_false_block_on_good_changes"]
    counts = review_decisions.EvaluationCounts(
        must_reject=review_decisions.RateComparison(
            must_reject_head, runs_per_side,
            must_reject_main, runs_per_side),
        must_approve_false_blocks=review_decisions.RateComparison(
            must_approve_head, runs_per_side,
            must_approve_main, runs_per_side),
        joint_false_blocks=review_decisions.RateComparison(
            joint_false_blocks["successes"], pair_good,
            a_false_blocks, pair_good),
        bad_changes=scored_pairs["bad_changes"],
        a_misses=scored_pairs["b_catch_rate_among_a_misses"]["total"],
        b_catches=scored_pairs["b_catch_rate_among_a_misses"]["successes"],
    )
    result.update({
        "status": "complete",
        "head_commit_sha": prior_head_sha,
        "main_commit_sha": prior_main_sha,
        "regression_evidence": {
            "must_reject": {
                "head_rejected": must_reject_head,
                "main_rejected": must_reject_main,
                "runs_per_side": runs_per_side,
            },
            "must_approve_false_blocks": {
                "head_rejected": must_approve_head,
                "main_rejected": must_approve_main,
                "runs_per_side": runs_per_side,
            },
            "joint_false_blocks": {
                "joint_rejected": joint_false_blocks["successes"],
                "reviewer_a_rejected": a_false_blocks,
                "good_changes": pair_good,
            },
        },
        "outcome": review_decisions.decide_outcome(counts),
    })
    return result
