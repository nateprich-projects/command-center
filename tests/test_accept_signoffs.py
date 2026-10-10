"""Accept sign-off rules: finished tests and phase self-close (#2407, #2431).

Pure over fixtures. Every finished Test waits, whether it proposes a change
or concludes "leave it"; top goals, KPI changes judged by live advice,
explicit-ask plans and results, and autonomy climbs wait; Curate, Describe
and Hypothesize close when they ship with findings in the domain's records;
all else closes on ship and a shipped Verify check returns later as a
finished Test. Phase names are strings, before the Class migration (#2425).
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item, gate_question  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def finished(number, klass, body="", **kw) -> Item:
    kw.setdefault("repo", "owner/command-center")
    kw.setdefault("title", "project {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("status", "Building")
    kw.setdefault("origin", "agent")
    kw.setdefault("risk", "standard")
    kw.setdefault("needs", "none")
    kw.setdefault("status_since", at(1))
    return Item(
        number=number, klass=klass, body=body,
        children_total=1, children_done=1, **kw)


def shaped(number, klass, body="", **kw) -> Item:
    kw.setdefault("repo", "owner/command-center")
    kw.setdefault("title", "project {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("status", "Shaped")
    kw.setdefault("origin", "agent")
    kw.setdefault("risk", "standard")
    kw.setdefault("needs", "none")
    kw.setdefault("status_since", at(1))
    return Item(number=number, klass=klass, body=body, **kw)


def test_finished_test_waits_whether_change_or_leave_it():
    proposes = finished(1, "Test", body="Finding: ship it.\nTested finding: x")
    leave_it = finished(
        2, "Test", body="Conclusion: leave it.\nThe data says leave it alone.")
    plain = finished(3, "Test", body="")

    for item in (proposes, leave_it, plain):
        assert funnel._can_close_itself(item) is False
        assert gate_question(item) == "Accept it?"
        assert funnel._auto_closeable_project(item) is False


def test_top_goal_waits():
    top = finished(1, "Implement", body="Top goal: championships")
    assert funnel._can_close_itself(top) is False
    assert gate_question(top) == "Accept it?"
    rendered = funnel.item_json(top, NOW)
    assert rendered["waiting_reason"] == "Top goal"


def test_top_goal_waits_over_upkeep_that_would_close():
    broken_top = finished(1, "Broken", body="Top goal: championships")
    assert funnel._can_close_itself(broken_top) is False
    assert gate_question(broken_top) == "Accept it?"


def test_kpi_judged_by_live_advice_waits():
    judged = finished(
        1, "Implement",
        body="KPI: win rate\nLive advice is judged by this KPI.")
    assert funnel._can_close_itself(judged) is False
    assert gate_question(judged) == "Accept it?"
    rendered = funnel.item_json(judged, NOW)
    assert rendered["waiting_reason"] == "KPI change"


def test_kpi_without_live_advice_closes():
    unjudged = finished(1, "Implement", body="KPI: dashboard visits")
    assert funnel._can_close_itself(unjudged) is True
    assert gate_question(unjudged) is None


def test_explicit_ask_result_waits_at_accept():
    pinned = finished(1, "Implement", body="", pinned=True)
    cited = finished(2, "Implement", body="Explicit ask: do this now")
    assert funnel._can_close_itself(pinned) is False
    assert gate_question(pinned) == "Accept it?"
    assert funnel._can_close_itself(cited) is False
    assert gate_question(cited) == "Accept it?"
    assert funnel.item_json(pinned, NOW)["waiting_reason"] == "Explicit ask"
    assert funnel.item_json(cited, NOW)["waiting_reason"] == "Explicit ask"


def test_explicit_ask_plan_waits_at_shaped():
    plain = shaped(1, "Implement", body="")
    assert gate_question(plain) is None
    pinned = shaped(2, "Implement", body="", pinned=True)
    assert gate_question(pinned) == "Is the plan good?"
    cited = shaped(3, "Implement", body="Explicit ask: file it")
    assert gate_question(cited) == "Is the plan good?"


def test_explicit_ask_waits_over_self_closing_phases():
    pinned_curate = finished(1, "Curate", body="", pinned=True)
    assert funnel._can_close_itself(pinned_curate) is False
    assert gate_question(pinned_curate) == "Accept it?"


def test_autonomy_climb_waits():
    climb = finished(
        1, "Implement", body="Autonomy climb: report to recommend")
    assert funnel._can_close_itself(climb) is False
    assert gate_question(climb) == "Accept it?"
    assert funnel.item_json(climb, NOW)["waiting_reason"] == "Autonomy climb"


def test_curate_describe_hypothesize_self_close():
    for number, klass in enumerate(
            ("Curate", "Describe", "Hypothesize"), start=1):
        item = finished(number, klass, body="Findings: measured.")
        assert funnel._can_close_itself(item) is True
        assert gate_question(item) is None
        assert funnel._auto_closeable_project(item) is True
        assert funnel._finished_close_candidate(item) is True


def test_implement_closes_like_improve_by_owner():
    agent = finished(1, "Implement", body="")
    agent.origin = "agent"
    assert funnel._can_close_itself(agent) is True
    assert gate_question(agent) is None
    nate = finished(2, "Implement", body="")
    nate.origin = "Nate"
    assert funnel._can_close_itself(nate) is False
    assert gate_question(nate) == "Accept it?"


def test_other_ships_close_and_checks_return_as_tests():
    shipped = finished(
        1, "Implement", body="Verify: check win rate next week")
    assert funnel._can_close_itself(shipped) is True
    assert gate_question(shipped) is None
    check = finished(
        2, "Test", body="Verify: check win rate next week")
    assert funnel._can_close_itself(check) is False
    assert gate_question(check) == "Accept it?"
    assert funnel.item_json(check, NOW)["waiting_reason"] == "Finished test"


def test_signoff_lines_are_case_insensitive_and_blank_cites_nothing():
    mixed = finished(1, "Implement", body="TOP GOAL: championships")
    assert funnel._can_close_itself(mixed) is False
    blank = finished(2, "Implement", body="Top goal:   ")
    assert funnel._can_close_itself(blank) is True


def test_ordinary_accept_keeps_its_reason():
    assert funnel.item_json(
        finished(1, "New", body=""), NOW)["waiting_reason"] == (
            "Ordinary accept")
    assert funnel.item_json(
        finished(2, "Replace", body=""), NOW)["waiting_reason"] == (
            "Ordinary accept")
