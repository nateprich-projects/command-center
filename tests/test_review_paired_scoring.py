"""Fixture-driven acceptance for paired A/B review scoring."""

from __future__ import annotations

import json
import pathlib

import pytest

from engine.review_decisions import score_paired_runs, wilson_interval


ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "paired_runs_v1.json"
ZERO_USAGE = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}


@pytest.fixture
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


def test_b_catch_rate_uses_only_a_misses(paired_run_records):
    result = score_paired_runs(paired_run_records)

    assert result["b_catch_rate_among_a_misses"] == {
        "successes": 2,
        "total": 3,
        "rate": 2 / 3,
        "interval_95": pytest.approx((0.20766, 0.93851), abs=1e-5),
    }


def test_both_miss_overlap_and_phi_measure_paired_bad_changes(
        paired_run_records):
    result = score_paired_runs(paired_run_records)["both_miss_overlap"]

    assert result["successes"] == 1
    assert result["total"] == 5
    assert result["rate"] == 0.2
    assert result["phi_correlation"] == pytest.approx(-1 / 6)


def test_joint_detection_counts_either_reviewer_blocking_bad_changes(
        paired_run_records):
    result = score_paired_runs(paired_run_records)[
        "joint_detection_on_bad_changes"]

    assert result["successes"] == 4
    assert result["total"] == 5
    assert result["rate"] == 0.8


def test_joint_false_block_counts_either_reviewer_blocking_good_changes(
        paired_run_records):
    result = score_paired_runs(paired_run_records)[
        "joint_false_block_on_good_changes"]

    assert result["successes"] == 2
    assert result["total"] == 3
    assert result["rate"] == 2 / 3


def test_wilson_interval_matches_independent_95_percent_reference():
    # Reference Wilson score interval for 5/10 (two-sided 95%).
    assert wilson_interval(5, 10) == pytest.approx(
        (0.2365930905, 0.7634069095), abs=1e-9)


def test_every_reported_rate_has_its_95_percent_wilson_bounds(
        paired_run_records):
    result = score_paired_runs(paired_run_records)

    assert result["both_miss_overlap"]["interval_95"] == pytest.approx(
        (0.03622, 0.62447), abs=2e-5)
    assert result["joint_detection_on_bad_changes"]["interval_95"] == pytest.approx(
        (0.37553, 0.96378), abs=2e-5)
    assert result["joint_false_block_on_good_changes"]["interval_95"] == pytest.approx(
        (0.20766, 0.93851), abs=2e-5)


def test_score_attributes_usage_priced_cost_and_paired_wall_latency(
        paired_run_records):
    result = score_paired_runs(paired_run_records)
    pair = result["paired_runs"][0]

    # usage.py's standard card: $1.25 fresh, $0.15 cached, $4.25 output / 1M.
    assert pair["a"]["cost_usd"] == 1.455
    assert pair["b"]["cost_usd"] == 3.525
    assert pair["a"]["latency_seconds"] == 120
    assert pair["b"]["latency_seconds"] == 240
    assert pair["paired_latency_seconds"] == 300
    assert result["total_cost_usd"] == 4.98
