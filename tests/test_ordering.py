"""Ordering rules — the part most likely to be subtly wrong.

Everything here is a pure function over Items. No network, no clock beyond the
`NOW` passed in explicitly.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import (  # noqa: E402
    Item,
    effective_class,
    awaiting_decision,
    gate_question,
    ladder_index,
    lock_holder,
    maintenance_load,
    needs_class,
    next_ticket,
    stale_locks,
    startable,
)

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "project_items.json"


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def item(number, status=None, klass=None, days=1.0, **kw) -> Item:
    kw.setdefault("repo", "nateprich/beta")
    kw.setdefault("title", "issue {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    return Item(
        number=number, status=status, klass=klass, status_since=at(days), **kw
    )


# -- Nate's queue: bottom-up ------------------------------------------------


def test_decisions_run_bottom_up_not_top_down():
    """Clear the decision closest to shipping first."""
    items = [
        item(1, "Shaped", "New"),
        item(2, "Ready", "New"),
        item(3, "Building", "New", children_total=1, children_done=1),
    ]
    assert [i.number for i in awaiting_decision(items)] == [3, 2, 1]


def test_ideas_never_waits_on_anyone():
    """Ideas is unbounded and guilt-free. It is not a gate."""
    assert gate_question(item(1, "Ideas", None)) is None
    assert awaiting_decision([item(1, "Ideas"), item(2, "Done"), item(3, "Parked")]) == []


def test_building_waits_only_once_every_child_has_closed():
    assert gate_question(item(1, "Building", "New", children_total=3, children_done=2)) is None
    assert gate_question(item(2, "Building", "New", children_total=3, children_done=3)) == "Accept it?"


def test_building_with_no_children_does_not_count_as_complete():
    """children_all_closed must not be vacuously true for a childless item."""
    assert gate_question(item(1, "Building", "New", children_total=0, children_done=0)) is None


def test_oldest_at_gate_wins_within_a_stage():
    """The longest-waiting item is the most likely park candidate."""
    items = [item(1, "Ready", "New", days=2), item(2, "Ready", "New", days=30), item(3, "Ready", "New", days=9)]
    assert [i.number for i in awaiting_decision(items)] == [2, 3, 1]


def test_a_closed_item_waits_on_nobody():
    assert gate_question(item(1, "Ready", "New", state="CLOSED")) is None


def test_blocked_surfaces_as_its_own_question_and_leads_its_stage():
    blocked = item(1, "Ready", "New", days=1, labels=["blocked"])
    older = item(2, "Ready", "New", days=20)
    order = awaiting_decision([older, blocked])
    assert order[0].number == 1
    assert gate_question(blocked) == "Unblock or park?"


def test_unknown_status_still_appears_rather_than_vanishing():
    """An item with an unrecognised Status must not be silently dropped."""
    items = [item(1, "Shaped", "New"), item(2, "Ready", "New")]
    assert len(awaiting_decision(items)) == 2


# -- Class ------------------------------------------------------------------


def test_unset_class_sorts_last_and_never_preempts():
    """A forgotten field must never acquire preemption rights."""
    assert ladder_index(None) > ladder_index("Replace")
    assert ladder_index(None) != ladder_index("Broken")
    assert ladder_index("nonsense") > ladder_index("Replace")


def test_ladder_is_in_the_documented_order():
    ranks = [ladder_index(c) for c in ["Broken", "Maintenance", "Improve", "New", "Replace"]]
    assert ranks == sorted(ranks) and len(set(ranks)) == 5


def test_anything_past_ideas_must_carry_a_class():
    assert needs_class(item(1, "Shaped", None))
    assert needs_class(item(2, "Ready", None))
    assert needs_class(item(3, "Building", None))


def test_a_ticket_is_exempt_because_it_inherits():
    """Sub-issues join the parent's Project with blank fields. That is normal,
    not a forgotten field."""
    parent = item(1, "Ready", "New", children_total=1, children_done=0)
    ticket = item(2, None, None, parent="nateprich/beta#1")
    assert not needs_class(ticket)
    assert effective_class(ticket, {parent.ref: parent}) == "New"


def test_a_project_with_no_status_at_all_still_needs_a_class():
    """The forgotten-field case: added to the Project and never touched again.
    A missing Status must not excuse a missing Class."""
    assert needs_class(item(1, None, None))


def test_a_ticket_with_its_own_class_keeps_it_when_it_has_no_parent_record():
    orphan = item(1, "Ready", "Broken", parent="nateprich/beta#999")
    assert effective_class(orphan, {}) == "Broken"


def test_ideas_done_and_parked_are_exempt_from_class():
    assert not needs_class(item(1, "Ideas", None))
    assert not needs_class(item(2, "Done", None))
    assert not needs_class(item(3, "Parked", None))
    assert not needs_class(item(4, "Ready", None, state="CLOSED"))


# -- Codex's queue: the ladder ---------------------------------------------


def test_ladder_orders_what_to_start():
    items = [
        item(1, "Ready", "Replace"),
        item(2, "Ready", "Broken"),
        item(3, "Ready", "New"),
        item(4, "Ready", "Maintenance"),
        item(5, "Ready", "Improve"),
    ]
    assert [i.number for i in startable(items)] == [2, 4, 5, 3, 1]


def test_in_flight_work_finishes_before_anything_new_starts():
    """Passing a gate is a commitment; nothing may silently un-commit it."""
    parent = item(1, "Building", "Replace", children_total=1, children_done=0)
    in_flight = item(2, parent="nateprich/beta#1")
    fresh = item(3, "Ready", "New")
    assert [i.number for i in startable([parent, in_flight, fresh])] == [2, 3]


def test_tickets_inherit_their_parents_class():
    """The ladder ranks projects, not individual tickets."""
    broken_parent = item(1, "Ready", "Broken", children_total=1, children_done=0)
    broken_ticket = item(2, parent="nateprich/beta#1")
    new_parent = item(3, "Ready", "New", children_total=1, children_done=0)
    new_ticket = item(4, parent="nateprich/beta#3")
    order = startable([broken_parent, broken_ticket, new_parent, new_ticket])
    assert [i.number for i in order] == [2, 4]


def test_a_parent_is_not_itself_a_startable_ticket():
    parent = item(1, "Ready", "New", children_total=2, children_done=0)
    assert [i.number for i in startable([parent])] == []


def test_shaped_work_is_not_startable():
    """Ready is the gate that says 'start now'. Shaped has not passed it."""
    assert startable([item(1, "Shaped", "New")]) == []


def test_blocked_work_is_not_startable_at_either_level():
    assert startable([item(1, "Ready", "New", labels=["blocked"])]) == []
    parent = item(2, "Ready", "New", children_total=1, children_done=0, labels=["blocked"])
    assert startable([parent, item(3, parent="nateprich/beta#2")]) == []


def test_oldest_at_gate_breaks_ties_in_the_ladder_too():
    items = [item(1, "Ready", "New", days=3), item(2, "Ready", "New", days=40)]
    assert [i.number for i in startable(items)] == [2, 1]


# -- The lock ---------------------------------------------------------------


def test_a_fresh_assignment_holds_the_lock():
    held = item(1, "Ready", "New", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(minutes=30))
    assert lock_holder([held], NOW).number == 1
    assert stale_locks([held], NOW) == []


def test_an_assignment_past_the_ttl_is_stale_and_takeable():
    stale = item(1, "Ready", "New", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(hours=3))
    assert lock_holder([stale], NOW) is None
    assert [i.number for i in stale_locks([stale], NOW)] == [1]


def test_an_assignment_to_somebody_else_is_not_the_lock():
    other = item(1, "Ready", "New", assignees=["someone-else"], assigned_at=NOW)
    assert lock_holder([other], NOW) is None


def test_next_returns_nothing_while_the_lock_is_held():
    held = item(1, "Ready", "New", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(minutes=5))
    waiting = item(2, "Ready", "New")
    assert next_ticket([held, waiting], NOW) is None


def test_broken_preempts_a_held_lock():
    """The one sanctioned preemption, and only because Broken is finite."""
    held = item(1, "Ready", "New", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(minutes=5))
    urgent = item(2, "Ready", "Broken")
    assert next_ticket([held, urgent], NOW).number == 2


def test_maintenance_does_not_preempt_a_held_lock():
    """Maintenance may preempt in-flight *ranking*, but not seize a live lock."""
    held = item(1, "Ready", "New", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(minutes=5))
    upkeep = item(2, "Ready", "Maintenance")
    assert next_ticket([held, upkeep], NOW) is None


def test_broken_does_not_preempt_a_lock_already_held_for_broken_work():
    held = item(1, "Ready", "Broken", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(minutes=5))
    other = item(2, "Ready", "Broken")
    assert next_ticket([held, other], NOW) is None


def test_a_stale_lock_does_not_block_the_next_run():
    stale = item(1, "Ready", "New", assignees=[funnel.CODEX_LOGIN], assigned_at=NOW - timedelta(hours=5))
    assert next_ticket([stale, item(2, "Ready", "New", days=99)], NOW) is not None


def test_next_returns_nothing_when_there_is_nothing_to_do():
    assert next_ticket([item(1, "Ideas", None)], NOW) is None


# -- The portfolio signal ---------------------------------------------------


def test_upkeep_share_counts_only_completed_work_in_the_window():
    items = [
        item(1, "Done", "Broken", state="CLOSED", state_reason="COMPLETED", closed_at=at(2)),
        item(2, "Done", "Maintenance", state="CLOSED", state_reason="COMPLETED", closed_at=at(5)),
        item(3, "Done", "New", state="CLOSED", state_reason="COMPLETED", closed_at=at(9)),
        item(4, "Done", "New", state="CLOSED", state_reason="COMPLETED", closed_at=at(400)),
    ]
    load = maintenance_load(items, NOW)
    assert load["closed_in_window"] == 3
    assert load["upkeep_share"] == round(2 / 3, 3)


def test_parked_work_is_not_counted_as_a_run():
    """Parking is a decision, not work done."""
    items = [
        item(1, "Parked", "New", state="CLOSED", state_reason="NOT_PLANNED", closed_at=at(1)),
        item(2, "Done", "Broken", state="CLOSED", state_reason="COMPLETED", closed_at=at(1)),
    ]
    assert maintenance_load(items, NOW)["closed_in_window"] == 1


def test_upkeep_share_is_none_rather_than_zero_when_nothing_closed():
    """An empty window is unknown, not healthy."""
    assert maintenance_load([], NOW)["upkeep_share"] is None


def test_days_since_anything_new_started():
    items = [item(1, "Building", "New", days=12), item(2, "Building", "Maintenance", days=1)]
    assert maintenance_load(items, NOW)["days_since_anything_new_started"] == 12


# -- Parsing the GraphQL shape ---------------------------------------------


def test_fixture_parses_into_the_expected_items():
    nodes = json.loads(FIXTURE.read_text())
    items = [i for i in (funnel._from_node(n) for n in nodes) if i]
    assert [i.number for i in items] == [10, 11, 12, 13, 14, 15]


def test_a_draft_issue_is_skipped():
    nodes = json.loads(FIXTURE.read_text())
    assert funnel._from_node(nodes[-1]) is None


def test_time_at_gate_ignores_other_projects():
    """An issue may sit in several Projects; only this one counts."""
    nodes = json.loads(FIXTURE.read_text())
    beta11 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 11)
    assert beta11.status_since == datetime(2026, 9, 1, tzinfo=timezone.utc)


def test_time_at_gate_uses_the_last_move_into_the_current_status():
    nodes = json.loads(FIXTURE.read_text())
    alpha10 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 10)
    assert alpha10.status_since == datetime(2026, 8, 26, tzinfo=timezone.utc)


def test_an_unassign_releases_the_lock():
    nodes = json.loads(FIXTURE.read_text())
    beta12 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 12)
    assert beta12.assigned_at is None
    assert lock_holder([beta12], NOW) is None


def test_a_live_assignment_is_parsed_as_the_lock():
    nodes = json.loads(FIXTURE.read_text())
    items = [i for i in (funnel._from_node(n) for n in nodes) if i]
    holder = lock_holder(items, datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc))
    assert holder is not None and holder.number == 13
