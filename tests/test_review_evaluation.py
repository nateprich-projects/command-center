"""Acceptance for the nonblocking review-evaluation wiring."""

from __future__ import annotations

import json
import pathlib

import pytest

from engine import replay
from engine.review_decisions import score_paired_runs
from engine.review_evaluation import (
    fisher_decline_p_value,
    render_cost_table,
    run_eval_nonblocking,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "paired_runs_v1.json"
ZERO_USAGE = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
HEAD_SHA = "a" * 40
MAIN_SHA = "b" * 40


def paired_run_records():
    outcomes = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = []
    for index, outcome in enumerate(outcomes):
        if index == 0:
            a_usage = {"input_tokens": 1_000_000,
                       "cached_tokens": 200_000, "output_tokens": 100_000}
            b_usage = {"input_tokens": 2_000_000,
                       "cached_tokens": 1_000_000, "output_tokens": 500_000}
            a_times = ("2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z")
            b_times = ("2026-01-01T00:01:00Z", "2026-01-01T00:05:00Z")
        else:
            a_usage = b_usage = ZERO_USAGE
            minute = index + 1
            a_times = (f"2026-01-01T00:{minute:02d}:00Z",
                       f"2026-01-01T00:{minute:02d}:02Z")
            b_times = (f"2026-01-01T00:{minute:02d}:01Z",
                       f"2026-01-01T00:{minute:02d}:04Z")

        def run(verdict, tokens, times):
            return {
                "verdict": verdict,
                "model": "muse-spark-1.3",
                "token_usage": tokens,
                "started_at": times[0],
                "ended_at": times[1],
            }

        records.append({
            "change_id": outcome["change_id"],
            "version": outcome["version"],
            "truth": outcome["truth"],
            "a": run(outcome["a_verdict"], a_usage, a_times),
            "b": run(outcome["b_verdict"], b_usage, b_times),
        })
    return records


def _run_records(total, rejected, commit_sha, *, prior_total=0,
                 prior_rejected=0):
    records = []
    for index in range(total):
        was_rejected = (index < prior_rejected if index < prior_total else
                        index - prior_total < rejected - prior_rejected)
        records.append({
            "verdict": "rejected" if was_rejected else "approved",
            "failed_parts": [],
            **({"head_commit_sha": commit_sha}
               if commit_sha == HEAD_SHA else {}),
        })
    return records


def test_run_eval_nonblocking_uses_registered_looks_and_never_blocks(
        tmp_path):
    calls = []
    manifest_path = ROOT / "engine" / "review_variants" / "manifest.json"
    manifest_before = manifest_path.read_bytes()

    def replay_fn(packet_name, head_checkout, main_checkout, *, runs,
                  version, runtime_root, head_runs=()):
        calls.append((packet_name, runs, len(head_runs)))
        if packet_name == "must_reject" and runs == 40:
            head_rejected, main_rejected = 30, 36
            prior_head = prior_main = 0
        elif packet_name == "must_reject":
            head_rejected, main_rejected = 30, 54
            prior_head, prior_main = 30, 36
        elif runs == 40:
            head_rejected, main_rejected = 2, 3
            prior_head = prior_main = 0
        else:
            head_rejected, main_rejected = 3, 4
            prior_head, prior_main = 2, 3
        return {
            "packet": packet_name,
            "version": version,
            "head": {"commit_sha": HEAD_SHA,
                     "runs": _run_records(
                         runs, head_rejected, HEAD_SHA,
                         prior_total=runs - 20 if runs > 40 else 0,
                         prior_rejected=prior_head)},
            "main": {"commit_sha": MAIN_SHA,
                     "runs": _run_records(
                         runs, main_rejected, MAIN_SHA,
                         prior_total=runs - 20 if runs > 40 else 0,
                         prior_rejected=prior_main)},
        }

    result = run_eval_nonblocking(
        "baseline", paired_run_records(),
        head_checkout=ROOT, main_checkout=ROOT, runtime_root=tmp_path,
        replay_fn=replay_fn,
    )

    assert [call[1] for call in calls] == [40, 40, 60, 60]
    assert [call[2] for call in calls] == [0, 0, 40, 40]
    assert [look["runs_per_side"] for look in result["looks"]] == [40, 60]
    assert result["looks"][0]["decline_p_value"] > 0.05
    assert result["looks"][1]["decline_p_value"] < 0.05
    assert result["variant"] == "baseline"
    assert result["trial_enabled"] is False
    assert result["merge_blocking"] is False
    assert result["outcome"]["merge_blocking"] is False
    assert "reviewer_b" not in result
    assert manifest_path.read_bytes() == manifest_before


def test_eval_stops_at_existing_pace_brake_without_blocking(tmp_path):
    def replay_fn(*_args, **_kwargs):
        raise replay.ReplayError("pace brake closed")

    result = run_eval_nonblocking(
        "baseline", paired_run_records(),
        head_checkout=ROOT, main_checkout=ROOT, runtime_root=tmp_path,
        replay_fn=replay_fn,
    )

    assert result["status"] == "skipped"
    assert result["merge_blocking"] is False
    assert result["look_count"] == 0
    assert result["outcome"] is None


def test_fisher_decline_uses_the_one_sided_exact_reference():
    # Reference result for the 30/40 vs 36/40 contingency table.
    assert fisher_decline_p_value(30, 40, 36, 40) == pytest.approx(
        0.06973472125252754, abs=1e-15)


def test_render_cost_table_shows_pair_cost_latency_and_joint_rates():
    score = score_paired_runs(paired_run_records())

    table = render_cost_table("r1", score)

    assert table.count(
        "| Measure | Runs | Cost (USD) | Latency (s) | Rate (95% CI) |"
    ) == 1
    assert "| Reviewer A (r1) | 8 | $1.455 | 134.0 | — |" in table
    assert "| Reviewer B | 8 | $3.525 | 261.0 | — |" in table
    assert "| A+B paired total | 16 | $4.980 | 328.0 | — |" in table
    assert "B catches among A misses" in table
    assert "2/3 (66.7%; 95% CI 20.8%–93.9%)" in table
    assert "1/5 (20.0%; 95% CI 3.6%–62.4%); phi=-0.167" in table
    assert "4/5 (80.0%; 95% CI 37.6%–96.4%)" in table
    assert "Joint detection on bad changes" in table
    assert "Joint false blocks on good changes" in table
    assert table.count("\n|---") == 1
