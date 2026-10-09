"""Hypothesis scorer: value ordering and try-and-watch (#2427)."""

from __future__ import annotations

import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def test_value_is_impact_times_uncertainty_over_cost():
    assert funnel.hypothesis_value(
        {"impact": 100, "p": 0.25, "cost": 10}) == 2.5
    assert funnel.hypothesis_value(
        {"impact": 100, "p": 0.75, "cost": 10}) == 2.5
    assert funnel.hypothesis_value(
        {"impact": 100, "p": 0.5, "cost": 10}) == 5.0


def test_value_is_zero_when_no_answer_changes_the_decision():
    base = {"impact": 100, "p": 0.5, "cost": 1}
    assert funnel.hypothesis_value(dict(base, changes_decision=False)) == 0.0
    assert funnel.hypothesis_value(
        dict(base, no_answer_would_change_decision=True)) == 0.0
    assert funnel.hypothesis_value(base) > 0.0


def test_value_is_zero_when_certain_or_without_impact():
    assert funnel.hypothesis_value(
        {"impact": 100, "p": 0.0, "cost": 1}) == 0.0
    assert funnel.hypothesis_value(
        {"impact": 100, "p": 1.0, "cost": 1}) == 0.0
    assert funnel.hypothesis_value(
        {"impact": 0, "p": 0.5, "cost": 1}) == 0.0
    assert funnel.hypothesis_value(
        {"impact": 100, "p": 1.5, "cost": 1}) == 0.0


def test_free_test_ranks_first_when_a_decision_is_on_the_line():
    free = {"impact": 10, "p": 0.5, "cost": 0}
    paid = {"impact": 1000, "p": 0.5, "cost": 1}
    assert math.isinf(funnel.hypothesis_value(free))
    assert [item["key"] for item in funnel.order_hypotheses(
        [dict(paid, key="paid"), dict(free, key="free")])] == ["free", "paid"]
    assert funnel.hypothesis_value(
        {"impact": 10, "p": 1.0, "cost": 0}) == 0.0


def test_fixtures_order_by_value_with_zero_value_last():
    hypotheses = [
        {"key": "zero-decision", "impact": 999, "p": 0.5, "cost": 1,
         "changes_decision": False},
        {"key": "low", "impact": 10, "p": 0.5, "cost": 10},  # 0.5
        {"key": "high", "impact": 100, "p": 0.5, "cost": 10},  # 5.0
        {"key": "zero-certain", "impact": 100, "p": 1.0, "cost": 1},
        {"key": "mid", "impact": 40, "p": 0.5, "cost": 10},  # 2.0
    ]
    ordered = funnel.order_hypotheses(hypotheses)
    assert [item["key"] for item in ordered] == [
        "high", "mid", "low", "zero-decision", "zero-certain"]
    assert [item.key for item in funnel.score_hypotheses(hypotheses)] == [
        "high", "mid", "low", "zero-decision", "zero-certain"]


def test_equal_values_keep_fixture_order():
    hypotheses = [
        {"key": "first", "impact": 10, "p": 0.5, "cost": 10},
        {"key": "second", "impact": 20, "p": 0.5, "cost": 20},
    ]
    assert [item["key"] for item in funnel.order_hypotheses(hypotheses)] == [
        "first", "second"]


def test_likely_cheap_reversible_flags_try_and_watch():
    flagged = {"impact": 10, "p": 0.9, "cost": 5, "likely": True,
               "cheap": True, "reversible": True}
    assert funnel.hypothesis_try_and_watch(flagged) is True
    assert funnel.hypothesis_try_and_watch(
        dict(flagged, likely=False)) is False
    assert funnel.hypothesis_try_and_watch(
        dict(flagged, cheap=False)) is False
    assert funnel.hypothesis_try_and_watch(
        dict(flagged, reversible=False)) is False


def test_likely_falls_back_to_p_but_cheap_does_not_derive_from_cost():
    derived = {"impact": 10, "p": 0.9, "cost": 1,
               "cheap": True, "reversible": True}
    assert funnel.hypothesis_try_and_watch(derived) is True
    assert funnel.hypothesis_try_and_watch(
        {"impact": 10, "p": 0.9, "cost": 1, "likely": False,
         "cheap": True, "reversible": True}) is False
    assert funnel.hypothesis_try_and_watch(
        {"impact": 10, "p": 0.9, "cost": 1,
         "reversible": True}) is False
    assert funnel.hypothesis_try_and_watch(
        {"impact": 10, "p": 0.5, "cost": 1, "cheap": True,
         "reversible": True}) is False


def test_scored_record_keeps_estimates_and_reasons():
    scored = funnel.score_hypothesis({
        "key": "h1", "impact": 100, "impact_reason": "two titles",
        "p": 0.25, "p_reason": "split sample",
        "cost": 10, "cost_reason": "one session",
        "changes_decision": True, "changes_decision_reason": "picks test",
        "likely": True, "likely_reason": "prior wins",
        "cheap": True, "cheap_reason": "flag flip",
        "reversible": True, "reversible_reason": "revert in one PR",
    })
    assert scored.value == 2.5
    assert scored.try_and_watch is True
    record = scored.to_dict()
    assert (record["impact"], record["p"], record["cost"]) == (100, 0.25, 10)
    assert record["impact_reason"] == "two titles"
    assert record["p_reason"] == "split sample"
    assert record["cost_reason"] == "one session"
    assert record["likely_reason"] == "prior wins"
    assert record["cheap_reason"] == "flag flip"
    assert record["reversible_reason"] == "revert in one PR"


def test_try_and_watch_list_returns_flagged_in_value_order():
    hypotheses = [
        {"key": "plain", "impact": 100, "p": 0.5, "cost": 10},
        {"key": "watch-low", "impact": 10, "p": 0.8, "cost": 10,
         "likely": True, "cheap": True, "reversible": True},
        {"key": "watch-high", "impact": 60, "p": 0.8, "cost": 10,
         "likely": True, "cheap": True, "reversible": True},
    ]
    flagged = funnel.try_and_watch_list(hypotheses)
    assert [item.key for item in flagged] == ["watch-high", "watch-low"]


def test_seam_accepts_aliases_kwargs_and_nested_estimates():
    assert funnel.hypothesis_value(impact=100, p=0.5, cost=10) == 5.0
    assert funnel.hypothesis_value(
        {"impact": 100, "prob": 0.5, "test_cost": 10}) == 5.0
    assert funnel.hypothesis_value({
        "estimates": {"impact": 100, "p": 0.5, "cost": 10},
        "reasons": {"impact": "titles"},
    }) == 5.0
    scored = funnel.score_hypothesis(funnel.Hypothesis(
        key="h", impact=100, p=0.5, cost=10))
    assert (scored.key, scored.value) == ("h", 5.0)
    assert funnel.rank_hypotheses is funnel.order_hypotheses
    assert funnel.slot_admission_order is funnel.order_hypotheses
    assert funnel.is_try_and_watch is funnel.hypothesis_try_and_watch


def test_reexport_modules_expose_the_same_seam():
    import hypothesis_scorer
    import scorer

    assert hypothesis_scorer.order_hypotheses is funnel.order_hypotheses
    assert scorer.order_hypotheses is funnel.order_hypotheses
    assert scorer.hypothesis_value({"impact": 4, "p": 0.5, "cost": 1}) == 2.0
