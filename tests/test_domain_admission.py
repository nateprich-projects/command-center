"""Domain admission ordering: interim gates and turns (#2407, #2429).

Pure over fixtures. Interim domains admit only urgent work, Bugs and
Nate's explicit asks; phase work takes domain turns with no tier effect
and asks first; urgent and Bugs stay outside turns; BUG_SHARE and
finite-class preemption are unchanged.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
import muse_model  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
CC = "owner/command-center"
WB = "owner/workbench"
CT = "owner/career-toolset"
JFA = "owner/jeffy-finance-agent"
FGM = "owner/Fantasy-GM"


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def proj(repo, number, status, klass, days=1.0, children=1, done=0, **kw):
    kw.setdefault("title", "project {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("origin", "agent")
    kw.setdefault("risk", "standard")
    kw.setdefault("needs", "none")
    kw.setdefault("status_since", at(days))
    return funnel.Item(
        repo=repo, number=number, status=status, klass=klass,
        children_total=children, children_done=done, **kw)


def tick(repo, number, parent_ref, days=1.0, **kw):
    kw.setdefault("title", "ticket {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("origin", "agent")
    kw.setdefault("risk", "standard")
    kw.setdefault("needs", "none")
    kw.setdefault("status_since", at(days))
    return funnel.Item(repo=repo, number=number, parent=parent_ref, **kw)


def league_proj(number, status, klass, days=1.0, **kw):
    kw.setdefault("domain", "League")
    return proj(FGM, number, status, klass, days=days, **kw)


def league_tick(number, parent_ref, days=1.0, **kw):
    return tick(FGM, number, parent_ref, days=days, **kw)


def afl_proj(number, status, klass, days=1.0, **kw):
    kw.setdefault("domain", "AFL")
    return proj(FGM, number, status, klass, days=days, **kw)


def afl_tick(number, parent_ref, days=1.0, **kw):
    return tick(FGM, number, parent_ref, days=days, **kw)


def test_phase_classes_are_the_five():
    assert funnel.PHASE_CLASSES == (
        "Curate", "Describe", "Hypothesize", "Test", "Implement")


def test_interim_domains_are_the_three_without_overseers():
    assert funnel.INTERIM_DOMAINS == (
        "workbench", "career-toolset", "jeffy-finance-agent")


def test_is_phase_class_names_phases_only():
    for klass in funnel.PHASE_CLASSES:
        assert funnel.is_phase_class(klass) is True
    for klass in ("Improve", "New", "Replace", "Investigate", "Broken",
                  "Maintenance", "Bug", None, "nonsense"):
        assert funnel.is_phase_class(klass) is False


def test_explicit_ask_is_a_pin_not_a_voice():
    parent = proj(CC, 1, "Ready", "Curate")
    pinned_ticket = tick(CC, 2, parent.ref, pinned=True)
    plain_ticket = tick(CC, 3, parent.ref)
    rows = [parent, pinned_ticket, plain_ticket]
    by_ref = {item.ref: item for item in rows}
    assert funnel.is_explicit_ask(pinned_ticket, by_ref) is True
    assert funnel.is_explicit_ask(plain_ticket, by_ref) is False
    parent.pinned = True
    assert funnel.is_explicit_ask(plain_ticket, by_ref) is True
    parent.pinned = False
    thought = tick(CC, 4, parent.ref, origin="Nate")
    assert funnel.is_explicit_ask(thought, by_ref) is False


def test_interim_phase_work_without_ask_is_refused():
    for repo in (WB, CT, JFA):
        parent = proj(repo, 10, "Ready", "Curate", days=1)
        ticket = tick(repo, 11, parent.ref, days=1)
        rows = [parent, ticket]
        assert funnel.interim_entry_allowed(rows, ticket) is False
        assert funnel.domain_entry_allowed(rows, ticket) is False


def test_interim_admits_ask_urgent_and_bug():
    asked = proj(WB, 10, "Ready", "Curate", days=1, pinned=True)
    asked_ticket = tick(WB, 11, asked.ref, days=1)
    urgent = proj(WB, 20, "Ready", "Broken", days=1)
    urgent_ticket = tick(WB, 21, urgent.ref, days=1)
    bug = proj(WB, 30, "Ready", "Bug", days=1)
    bug_ticket = tick(WB, 31, bug.ref, days=1)
    rows = [asked, asked_ticket, urgent, urgent_ticket, bug, bug_ticket]
    assert funnel.domain_entry_allowed(rows, asked_ticket) is True
    assert funnel.domain_entry_allowed(rows, urgent_ticket) is True
    assert funnel.domain_entry_allowed(rows, bug_ticket) is True


def test_interim_gates_legacy_work_the_same_way():
    parent = proj(WB, 10, "Ready", "Improve", days=1)
    ticket = tick(WB, 11, parent.ref, days=1)
    assert funnel.domain_entry_allowed([parent, ticket], ticket) is False


def test_interim_in_flight_and_waiting_stay_allowed():
    building = proj(WB, 10, "Building", "Curate", days=2)
    building_ticket = tick(WB, 11, building.ref, days=2)
    assert funnel.domain_entry_allowed(
        [building, building_ticket], building_ticket) is True
    waiting = proj(
        WB, 20, "Ready", "Curate", days=2,
        labels=["blocked"], needs="human",
    )
    waiting_ticket = tick(WB, 21, waiting.ref, days=2)
    rows = [waiting, waiting_ticket]
    assert funnel.domain_entry_allowed(rows, waiting_ticket) is True


def test_non_interim_phase_work_without_ask_is_allowed():
    parent = proj(CC, 10, "Ready", "Curate", days=1)
    ticket = tick(CC, 11, parent.ref, days=1)
    assert funnel.domain_entry_allowed([parent, ticket], ticket) is True


def test_turns_rotate_across_domains():
    c1 = proj(CC, 10, "Ready", "Curate", days=30)
    t_c1 = tick(CC, 11, c1.ref, days=30)
    c2 = proj(CC, 20, "Ready", "Describe", days=29)
    t_c2 = tick(CC, 21, c2.ref, days=29)
    l1 = league_proj(30, "Ready", "Hypothesize", days=28)
    t_l1 = league_tick(31, l1.ref, days=28)
    a1 = afl_proj(40, "Ready", "Test", days=27)
    t_a1 = afl_tick(41, a1.ref, days=27)
    a2 = afl_proj(50, "Ready", "Implement", days=26)
    t_a2 = afl_tick(51, a2.ref, days=26)
    a3 = afl_proj(60, "Ready", "Curate", days=25)
    t_a3 = afl_tick(61, a3.ref, days=25)
    rows = [c1, t_c1, c2, t_c2, l1, t_l1, a1, t_a1, a2, t_a2, a3, t_a3]
    tickets = [t_c1, t_c2, t_l1, t_a1, t_a2, t_a3]
    expected = [11, 31, 41, 21, 51, 61]
    assert [item.number for item in funnel.startable(rows)] == expected
    assert [item.number for item in funnel.phase_turn_order(
        rows, tickets)] == expected
    assert [item.number for item in funnel.phase_turn_order(rows)] == expected


def test_explicit_asks_go_first_within_their_domain():
    old = proj(CC, 10, "Ready", "Curate", days=30)
    t_old = tick(CC, 11, old.ref, days=30)
    asked = proj(CC, 20, "Ready", "Describe", days=1, pinned=True)
    t_asked = tick(CC, 21, asked.ref, days=1)
    other = league_proj(30, "Ready", "Test", days=28)
    t_other = league_tick(31, other.ref, days=28)
    rows = [old, t_old, asked, t_asked, other, t_other]
    assert [item.number for item in funnel.startable(rows)] == [21, 31, 11]


def test_phase_order_ignores_repo_tiers():
    tooling = proj(CC, 10, "Ready", "Implement", days=1)
    t_tooling = tick(CC, 11, tooling.ref, days=1)
    hobby = league_proj(20, "Ready", "Curate", days=30)
    t_hobby = league_tick(21, hobby.ref, days=30)
    rows = [tooling, t_tooling, hobby, t_hobby]
    assert funnel.repo_tier(CC) == 1
    assert funnel.repo_tier(FGM) == 3
    assert [item.number for item in funnel.startable(rows)] == [21, 11]


def test_scorer_orders_within_domain_ahead_of_age():
    old = proj(CC, 10, "Ready", "Curate", days=30)
    t_old = tick(CC, 11, old.ref, days=30)
    new = proj(CC, 20, "Ready", "Curate", days=1)
    t_new = tick(CC, 21, new.ref, days=1)
    rows = [old, t_old, new, t_new]
    tickets = [t_old, t_new]
    hypotheses = {
        t_old.ref: {"impact": 10, "p": 0.5, "cost": 10},   # 0.5
        t_new.ref: {"impact": 100, "p": 0.5, "cost": 10},  # 5.0
    }
    assert [item.number for item in funnel.phase_turn_order(
        rows, tickets, hypotheses)] == [21, 11]
    assert [item.number for item in funnel._order_startable_items(
        rows, tickets, hypotheses)] == [21, 11]
    # Without estimates age still decides.
    assert [item.number for item in funnel.phase_turn_order(
        rows, tickets)] == [11, 21]


def test_scorer_never_passes_an_explicit_ask():
    asked = proj(CC, 10, "Ready", "Curate", days=30, pinned=True)
    t_asked = tick(CC, 11, asked.ref, days=30)
    plain = proj(CC, 20, "Ready", "Curate", days=1)
    t_plain = tick(CC, 21, plain.ref, days=1)
    rows = [asked, t_asked, plain, t_plain]
    hypotheses = {
        asked.ref: funnel.Hypothesis(key="ask", impact=1, p=0.5, cost=10),
        t_plain.ref: {"impact": 100, "p": 0.5, "cost": 10},
    }
    assert [item.number for item in funnel.phase_turn_order(
        rows, [t_asked, t_plain], hypotheses)] == [11, 21]


def test_unscored_and_zero_value_keep_age_order():
    old = proj(CC, 10, "Ready", "Curate", days=30)
    t_old = tick(CC, 11, old.ref, days=30)
    mid = proj(CC, 20, "Ready", "Curate", days=20)
    t_mid = tick(CC, 21, mid.ref, days=20)
    new = proj(CC, 30, "Ready", "Curate", days=1)
    t_new = tick(CC, 31, new.ref, days=1)
    rows = [old, t_old, mid, t_mid, new, t_new]
    hypotheses = {
        t_new.ref: {"impact": 100, "p": 0.5, "cost": 10},
        t_mid.ref: {"impact": 999, "p": 0.5, "cost": 1,
                    "changes_decision": False},  # zero value
    }
    assert [item.number for item in funnel.phase_turn_order(
        rows, [t_old, t_mid, t_new], hypotheses)] == [31, 11, 21]


def test_thoughts_carry_no_boost_and_later_asks_do_not_move():
    thought = proj(CC, 10, "Ready", "Curate", days=5, origin="Nate")
    t_thought = tick(CC, 11, thought.ref, days=5, origin="Nate")
    agent = proj(CC, 20, "Ready", "Curate", days=5, origin="agent")
    t_agent = tick(CC, 21, agent.ref, days=5, origin="agent")
    rows = [thought, t_thought, agent, t_agent]
    tickets = [t_thought, t_agent]
    assert [item.number for item in funnel.startable(rows)] == [11, 21]
    t_thought.body = "Nate asked about this later — still waiting?"
    t_thought.needs_decision = "Where should this live?"
    assert [item.number for item in funnel.startable(rows)] == [11, 21]
    assert [item.number for item in funnel.phase_turn_order(
        rows, tickets)] == [11, 21]


def test_urgent_and_bugs_stay_outside_turns():
    urgent = proj(CC, 10, "Ready", "Broken", days=1)
    t_urgent = tick(CC, 11, urgent.ref, days=1)
    phase = league_proj(20, "Ready", "Curate", days=30)
    t_phase = league_tick(21, phase.ref, days=30)
    bug = afl_proj(30, "Ready", "Bug", days=30)
    t_bug = afl_tick(31, bug.ref, days=30)
    rows = [urgent, t_urgent, phase, t_phase, bug, t_bug]
    assert [item.number for item in funnel.startable(rows)] == [11, 21, 31]
    due = funnel.next_ticket(rows, NOW, recent_starts=[])
    assert due is not None and due.number == 11
    phases_only = [phase, t_phase, bug, t_bug]
    held = funnel.next_ticket(phases_only, NOW, recent_starts=["Bug", "Bug"])
    assert held is not None and held.number == 21
    owed = funnel.next_ticket(phases_only, NOW, recent_starts=[])
    assert owed is not None and owed.number == 31


def test_bug_share_and_finite_preemption_survive_phases():
    assert funnel.BUG_SHARE == (2, 8)
    assert funnel.PREEMPTING_CLASSES == frozenset({"Broken", "Maintenance"})
    assert funnel.bug_share_due([]) is True
    assert funnel.bug_share_due(["Bug", "Bug"]) is False
    finite = proj(CC, 10, "Ready", "Maintenance", days=1)
    t_finite = tick(CC, 11, finite.ref, days=1)
    phase = league_proj(20, "Ready", "Curate", days=30)
    t_phase = league_tick(21, phase.ref, days=30)
    rows = [finite, t_finite, phase, t_phase]
    assert [item.number for item in funnel.startable(rows)] == [11, 21]
    chosen = funnel.next_ticket(rows, NOW)
    assert chosen is not None and chosen.number == 11


def test_contributor_repos_lists_repos_by_name():
    assert muse_model.CONTRIBUTOR_REPOS == frozenset({
        "command-center", "github-runners", "workbench",
        "Fantasy-GM", "The-League", "AFL",
    })


def test_admission_seam_accepts_aliases():
    assert funnel.is_phase is funnel.is_phase_class
    assert funnel.is_nate_explicit_ask is funnel.is_explicit_ask
    assert funnel.is_interim is funnel.is_interim_domain
    assert funnel.interim_allowed is funnel.interim_entry_allowed
    assert funnel.domain_turn_order is funnel.phase_turn_order
    assert funnel.phase_admission_order is funnel.phase_turn_order
    assert funnel.domain_admission_order is funnel.phase_turn_order
