"""Fixture coverage for the question registry's sufficiency contract."""

from __future__ import annotations

import json
import pathlib
import sys
from io import StringIO

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import questions  # noqa: E402


def signal(sample_size, **fields):
    return {
        "status": "available",
        "sample_size": sample_size,
        **fields,
    }


def sufficient_signals():
    return {
        questions.COST_PER_RUN_BY_OUTCOME: signal(
            1,
            by_schedule=[{
                "schedule": "codex-standard",
                "empty_fire_cost": 2.0,
                "empty_fires": 3,
                "worked_cost": 4.0,
            }],
        ),
        questions.FIRES_BY_SCHEDULE: signal(
            1,
            by_schedule=[{
                "schedule": "codex-standard",
                "empty_fires": 3,
            }],
        ),
        questions.SESSIONS_PER_MERGED_PR: signal(
            30,
            by_lane=[{
                "lane": "codex/gpt-5.6-luna/high",
                "sessions_per_merged_pr": 1.5,
            }],
        ),
        questions.REVIEW_RUNS_PER_MERGE: signal(
            30,
            by_lane=[{
                "lane": "codex/gpt-5.6-luna/high",
                "review_runs_per_merge": 1.2,
            }],
        ),
        questions.COST_PER_MERGED_PR: signal(
            20,
            by_lane=[
                {
                    "lane": "codex/gpt-5.6-luna/high",
                    "unit": "USD",
                    "merged_prs": 10,
                    "cost_per_merged_pr": 2.0,
                },
                {
                    "lane": "codex/gpt-5.6-sol/max",
                    "unit": "USD",
                    "merged_prs": 10,
                    "cost_per_merged_pr": 25.0,
                },
            ],
        ),
        questions.POINTS_PER_HOUR_FULL_QUEUE: signal(
            8,
            by_pool=[{
                "pool": "codex",
                "projected_week_percent": 125.0,
            }],
        ),
        questions.FIRES_PER_HOUR: signal(
            8,
            by_pool=[{
                "pool": "codex",
                "fires_per_hour": 12.0,
            }],
        ),
        questions.SELF_REVIEW_REJECTION_RATE: signal(
            20,
            by_lane=[{
                "lane": "codex",
                "self_review_rejection_rate": 0.4,
                "other_rejection_rate": 0.1,
            }],
        ),
    }


def test_registry_seed_has_thresholds_and_named_dependencies():
    assert list(questions.QUESTION_REGISTRY) == [
        "empty_fire_cost",
        "sessions_per_merged_pr",
        "lane_cost_gap",
        "pool_week_projection",
        "self_review_rejection",
    ]
    assert questions.QUESTION_REGISTRY["empty_fire_cost"].minimum_sample == 1
    assert questions.QUESTION_REGISTRY["sessions_per_merged_pr"].minimum_sample == 30
    assert questions.QUESTION_REGISTRY["lane_cost_gap"].minimum_sample == 10
    assert questions.QUESTION_REGISTRY["lane_cost_gap"].sample_scope == "per_lane"
    assert questions.QUESTION_REGISTRY["pool_week_projection"].minimum_sample == 8
    assert questions.QUESTION_REGISTRY["self_review_rejection"].minimum_sample == 20
    assert all(
        entry.hypothesis and entry.signals and entry.sample_unit
        for entry in questions.QUESTION_REGISTRY.values()
    )


def test_below_minimum_abstains_with_each_signal_shortfall():
    names = {
        name
        for entry in questions.QUESTION_REGISTRY.values()
        for name in entry.signals
    }
    signals = {
        name: {"status": "insufficient_data", "sample_size": 0}
        for name in names
    }

    result = questions.evaluate_questions(signals)

    assert set(result["questions"]) == set(questions.QUESTION_REGISTRY)
    for name, row in result["questions"].items():
        assert row["status"] == questions.NOT_ENOUGH_EVIDENCE
        assert row["shortfalls"]
        assert row["reason"].startswith("not enough evidence yet:")
        assert "short by" in row["reason"]
        assert set(row["signals"]).issubset(names)


def test_scaled_fixture_evaluates_every_seed_question():
    result = questions.evaluate_questions({"signals": sufficient_signals()})

    rows = result["questions"]
    assert all(row["status"] == questions.FINDING for row in rows.values())
    assert rows["empty_fire_cost"]["supports_hypothesis"] is True
    assert rows["sessions_per_merged_pr"]["supports_hypothesis"] is True
    assert rows["lane_cost_gap"]["supports_hypothesis"] is True
    assert rows["pool_week_projection"]["supports_hypothesis"] is True
    assert rows["self_review_rejection"]["supports_hypothesis"] is True


def test_lane_scope_reports_the_exact_lane_shortfall():
    signals = sufficient_signals()
    signals[questions.COST_PER_MERGED_PR]["by_lane"][1]["merged_prs"] = 9

    row = questions.evaluate_question("lane_cost_gap", signals)

    assert row["status"] == questions.NOT_ENOUGH_EVIDENCE
    assert row["shortfalls"] == [{
        "signal": questions.COST_PER_MERGED_PR,
        "observed": 9,
        "required": 10,
        "shortfall": 1,
        "unit": "merged PRs per lane",
        "signal_status": "available",
        "reason": "sample is below the minimum",
        "scope": "codex/gpt-5.6-sol/max",
    }]
    assert "short by 1 merged PRs per lane" in row["reason"]


def test_partial_signal_does_not_turn_a_large_but_incomplete_sample_into_a_finding():
    signals = sufficient_signals()
    signals[questions.SESSIONS_PER_MERGED_PR]["status"] = "partial"

    row = questions.evaluate_question("sessions_per_merged_pr", signals)

    assert row["status"] == questions.NOT_ENOUGH_EVIDENCE
    assert row["shortfalls"][0]["observed"] == 30
    assert row["shortfalls"][0]["shortfall"] == 0
    assert "signal status is partial" in row["reason"]


def test_unregistered_question_cannot_be_asked():
    with pytest.raises(questions.UnknownQuestionError):
        questions.evaluate_question("invented_question", sufficient_signals())

    with pytest.raises(questions.UnknownQuestionError):
        questions.evaluate_questions(sufficient_signals(), ["empty_fire_cost", "invented_question"])


def test_stdin_cli_accepts_the_signal_summary_shape(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin", StringIO(json.dumps({"signals": sufficient_signals()}))
    )

    assert questions.main(["--question", "lane_cost_gap"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "question_registry"
    assert payload["questions"]["lane_cost_gap"]["status"] == questions.FINDING
