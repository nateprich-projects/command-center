"""The two-tier GraphQL reserve gate: reviewers keep going (#273, #296)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def spend(calls=1, cost=12, remaining=1000):
    funnel._GRAPHQL_SPEND.update(
        {"calls": calls, "cost": cost, "remaining": remaining,
         "reset_at": "2026-09-09T04:52:19Z"})


def test_engineering_stops_at_the_higher_floor_while_review_continues():
    """Nate's call, 2026-09-08. New work consumes; review drains."""
    # 12-point load: engineering floor 240, review floor 60.
    spend(cost=12, remaining=100)

    engineering = funnel._reserve_verdict("breakdown")
    review = funnel._reserve_verdict("review")

    assert engineering is not None and engineering["do"] == "stop"
    assert engineering["gate"] == "reserve"
    assert review is None, "a reviewer above its own floor must proceed"


def test_both_decline_below_the_review_floor():
    spend(cost=12, remaining=10)

    assert funnel._reserve_verdict("review") is not None
    assert funnel._reserve_verdict("breakdown") is not None


def test_both_proceed_with_ample_budget():
    spend(cost=12, remaining=4900)

    assert funnel._reserve_verdict("review") is None
    assert funnel._reserve_verdict("breakdown") is None


def test_an_unreadable_budget_fails_closed():
    """AGENTS.md: a run that cannot read its budget does not work."""
    funnel._GRAPHQL_SPEND.update(
        {"calls": 1, "cost": 12, "remaining": None, "reset_at": None})

    verdict = funnel._reserve_verdict("review")

    assert verdict is not None and verdict["do"] == "stop"
    assert "could not be read" in verdict["why"]


def test_a_run_with_nothing_to_do_is_not_relabelled_a_budget_decline():
    """Reporting a refusal the system never had to make is a false record."""
    spend(cost=12, remaining=0)

    assert funnel._reserve_verdict("stop") is None
    assert funnel._reserve_verdict(None) is None


def test_the_floor_is_loads_remaining_not_a_fixed_point_count():
    """Higher load costs still scale until the point ceiling is reached."""
    spend(cost=100, remaining=824)
    assert funnel._reserve_verdict("breakdown") is not None, (
        "20 x 100 = 2000, capped at 826"
    )

    spend(cost=12, remaining=800)
    assert funnel._reserve_verdict("breakdown") is None, "20 x 12 = 240"


def test_the_proportional_floor_is_bounded_by_the_point_ceiling():
    """The calibrated product is 840 before the configured ceiling applies."""
    assert funnel.ENGINEERING_RESERVE_LOADS * 42 == 840
    assert funnel.ENGINEERING_RESERVE_LOADS * 81 == 1620
    assert funnel._reserve_floor(funnel.ENGINEERING_RESERVE_LOADS, 42) == 826
    assert funnel._reserve_floor(funnel.ENGINEERING_RESERVE_LOADS, 81) == 826
    assert funnel._reserve_floor(funnel.REVIEW_RESERVE_LOADS, 81) == 405
    assert funnel.GRAPHQL_RESERVE_POINT_CEILING < 5000


def test_measured_stand_down_budgets_clear_the_capped_engineering_floor():
    for remaining in (1259, 826):
        spend(cost=81, remaining=remaining)
        assert funnel._reserve_verdict("breakdown") is None
        assert funnel._reserve_verdict("shape") is None


def test_the_decline_outcome_is_in_the_skipped_family():
    """`errored` would make the watchdog alarm on the system working."""
    import heartbeat
    assert "skipped-api-reserve" in heartbeat.OUTCOMES


def test_no_graphql_call_is_not_the_same_as_an_unreadable_budget():
    """Absence of a question, not absence of an answer.

    The CLI's early begin preflight asks before `load_items`, but direct
    `cmd_begin` callers can still arrive without a GraphQL reading. Failing
    closed here would refuse runs that never asked.
    """
    funnel._GRAPHQL_SPEND.update(
        {"calls": 0, "cost": 0, "remaining": None, "reset_at": None})

    assert funnel._reserve_verdict("review") is None
    assert funnel._reserve_verdict("breakdown") is None


def test_shape_takes_the_engineering_floor_not_the_reviewer_s():
    """`shape` arrived on main after this gate was written (#306).

    It opens new work, so it is a consumer, not a drainer.
    """
    spend(cost=12, remaining=100)   # engineering floor 240, review floor 60

    assert funnel._reserve_verdict("shape") is not None
    assert funnel._reserve_verdict("review") is None
