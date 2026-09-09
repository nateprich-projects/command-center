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
    question_since,
    stale_locks,
    startable,
)

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "project_items.json"


def at(days_ago: float) -> datetime:
    return NOW - timedelta(days=days_ago)


def project(number, status, klass, days=1.0, children=1, done=0, **kw) -> Item:
    """A parentless item with tickets under it — what the gates act on."""
    if status == "Ready" and children:
        kw.setdefault("first_child_created_at", at(days))
    return item(number, status, klass, days=days,
                children_total=children, children_done=done, **kw)


def ticket(number, parent, days=1.0, **kw) -> Item:
    """A sub-issue. Carries no Status or Class of its own; it inherits."""
    return item(number, None, None, days=days,
                parent="nateprich/beta#{}".format(parent), **kw)


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
        project(1, "Shaped", "New"),
        project(2, "Ready", "New"),
        project(3, "Building", "New", children=1, done=1),
    ]
    assert [i.number for i in awaiting_decision(items)] == [3, 1]


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


def test_building_question_starts_when_the_last_ticket_closes():
    building = project(
        1, "Building", "New", children=2, done=2,
        last_child_closed_at=NOW - timedelta(hours=3),
    )

    assert question_since(building) == NOW - timedelta(hours=3)
    assert building.waited(NOW) == timedelta(hours=3)


def test_ready_never_asks_start_now():
    ready = project(
        1, "Ready", "New", days=30, children=2,
        first_child_created_at=NOW - timedelta(hours=1),
    )

    assert gate_question(ready) is None


def test_shaped_question_starts_when_the_status_does():
    shaped = project(1, "Shaped", "New", days=12, children=0)

    assert question_since(shaped) == shaped.status_since
    assert shaped.waited(NOW) == timedelta(days=12)


def test_shaped_decision_order_uses_time_at_the_plan_gate():
    older = project(
        1, "Shaped", "New", days=30,
        body="## Needs you\n\nWhich repository should this use?\n",
    )
    newer = project(
        2, "Shaped", "New", days=2,
        body="## Needs you\n\nWhich repository should this use?\n",
    )

    assert [i.number for i in awaiting_decision([newer, older])] == [1, 2]


def test_oldest_at_gate_wins_within_a_stage():
    """The longest-waiting item is the most likely park candidate."""
    items = [
        project(1, "Shaped", "New", days=2),
        project(2, "Shaped", "New", days=30),
        project(3, "Shaped", "New", days=9),
    ]
    assert [i.number for i in awaiting_decision(items)] == [2, 3, 1]


def test_broken_does_not_outrank_a_deeper_gate():
    items = [project(1, "Shaped", "Broken", days=30),
             project(2, "Building", "New", children=1, done=1, days=1)]
    assert [i.number for i in awaiting_decision(items)] == [2, 1]


def test_broken_outranks_older_work_at_the_same_gate():
    items = [
        project(1, "Shaped", "New", days=30),
        project(2, "Shaped", "Broken", days=1),
    ]
    assert [i.number for i in awaiting_decision(items)] == [2, 1]


def test_ticket_inherits_broken_for_decision_ordering():
    parent = project(1, "Ideas", "Broken")
    child = item(2, "Shaped", None, days=1, parent=parent.ref)
    older = project(3, "Shaped", "New", days=30)
    assert [i.number for i in awaiting_decision([parent, child, older])] == [2, 3]


def test_a_closed_item_waits_on_nobody():
    assert gate_question(item(1, "Ready", "New", state="CLOSED")) is None


def test_blocked_surfaces_as_its_own_question_and_leads_its_stage():
    blocked = project(1, "Ready", "New", days=1, labels=["blocked"])
    older = project(2, "Ready", "New", days=20)
    order = awaiting_decision([older, blocked])
    assert order[0].number == 1
    assert gate_question(blocked) == "Unblock or park?"


def test_a_blocked_ticket_asks_only_whether_to_unblock():
    blocked = ticket(1, 9, labels=["blocked"])

    assert gate_question(blocked) == "Unblock?"


def test_blocked_ticket_without_status_starts_when_the_label_is_applied():
    blocked = ticket(
        1, 9, labels=["blocked"],
        blocked_since=NOW - timedelta(hours=2),
    )
    blocked.status = None
    blocked.status_since = None

    assert question_since(blocked) == NOW - timedelta(hours=2)
    assert blocked.waited(NOW) == timedelta(hours=2)


def test_a_named_block_condition_waits_on_the_system_for_projects_and_tickets():
    project_with_condition = project(
        1, "Ready", "New", labels=["blocked"], block_references=["#84"]
    )
    ticket_with_condition = ticket(
        2, 1, labels=["blocked"], block_references=["#84"]
    )

    assert gate_question(project_with_condition) is None
    assert gate_question(ticket_with_condition) is None
    assert awaiting_decision([project_with_condition, ticket_with_condition]) == []


def test_block_condition_state_does_not_change_non_blocked_questions():
    ready = project(1, "Ready", "New", children=2,
                    block_references=["#84"])

    assert gate_question(ready) is None


def test_unknown_status_still_appears_rather_than_vanishing():
    """An item with an unrecognised Status must not be silently dropped."""
    items = [project(1, "Shaped", "New"),
             project(2, "Building", "New", children=1, done=1)]
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
    items = []
    for n, klass in ((1, "Replace"), (2, "Broken"), (3, "New"), (4, "Maintenance"),
                     (5, "Improve")):
        items += [project(n, "Building", klass), ticket(10 + n, n)]
    assert [i.number for i in startable(items)] == [12, 14, 15, 13, 11]


def test_in_flight_work_finishes_before_anything_new_starts():
    """Passing a gate is a commitment; nothing may silently un-commit it.

    Both parents are Building — the only startable state — so this isolates the
    ladder: the in-flight Replace ticket still precedes the newer New one,
    because its project is already committed to.
    """
    older = project(1, "Building", "Replace", days=30)
    in_flight = ticket(2, 1, days=30)
    newer = project(3, "Building", "New", days=1)
    fresh = ticket(4, 3, days=1)
    order = [i.number for i in startable([older, in_flight, newer, fresh])]
    assert order == [4, 2]  # ladder first: New outranks Replace


def test_tickets_inherit_their_parents_class():
    """The ladder ranks projects, not individual tickets."""
    broken_parent = project(1, "Building", "Broken")
    broken_ticket = ticket(2, 1)
    new_parent = project(3, "Building", "New")
    new_ticket = ticket(4, 3)
    order = startable([broken_parent, broken_ticket, new_parent, new_ticket])
    assert [i.number for i in order] == [2, 4]


def test_a_parent_is_not_itself_a_startable_ticket():
    parent = item(1, "Ready", "New", children_total=2, children_done=0)
    assert [i.number for i in startable([parent])] == []


def test_ready_is_not_startable_until_the_parent_is_building():
    """Ready means broken into issues; only Building exposes tickets to Codex."""
    assert startable([project(1, "Ready", "New"), ticket(2, 1)]) == []
    assert [i.number for i in startable(
        [project(1, "Building", "New"), ticket(2, 1)])] == [2]


def test_shaped_work_is_not_startable():
    """Shaped has not been broken into tickets for Codex to work."""
    assert startable([project(1, "Shaped", "New"), ticket(2, 1)]) == []


def test_blocked_work_is_not_startable_at_either_level():
    assert startable([project(1, "Building", "New"), ticket(2, 1, labels=["blocked"])]) == []
    parent = project(3, "Building", "New", labels=["blocked"])
    assert startable([parent, ticket(4, 3)]) == []


def test_a_ticket_with_an_open_native_blocker_is_not_startable():
    rows = [project(1, "Building", "New"),
            ticket(2, 1, open_blockers=["other/repo#9"])]

    assert startable(rows) == []


def test_a_ticket_with_a_closed_native_blocker_is_startable():
    rows = [project(1, "Building", "New"), ticket(2, 1, open_blockers=[])]

    assert [i.number for i in startable(rows)] == [2]


def test_a_ticket_with_no_native_blockers_is_startable():
    rows = [project(1, "Building", "New"), ticket(2, 1)]

    assert [i.number for i in startable(rows)] == [2]


def test_a_blocker_inherits_rank_from_a_higher_numbered_broken_ticket():
    broken_parent = project(1, "Building", "Broken")
    waiting = ticket(3, 1, open_blockers=["nateprich/beta#4"])
    improve_parent = project(2, "Building", "Improve")
    blocker = ticket(4, 2)
    maintenance_parent = project(5, "Building", "Maintenance")
    other = ticket(6, 5)

    assert [i.number for i in startable(
        [broken_parent, waiting, improve_parent, blocker,
         maintenance_parent, other]
    )] == [4, 6]


def test_transitive_dependency_chain_lifts_the_deepest_rank():
    broken_parent = project(1, "Building", "Broken")
    waiting = ticket(10, 1, open_blockers=["nateprich/beta#20"])
    improve_parent = project(2, "Building", "Improve")
    middle = ticket(20, 2, open_blockers=["nateprich/beta#30"])
    new_parent = project(3, "Building", "New")
    deepest_blocker = ticket(30, 3)
    maintenance_parent = project(4, "Building", "Maintenance")
    other = ticket(40, 4)

    assert [i.number for i in startable([
        broken_parent, waiting, improve_parent, middle, new_parent,
        deepest_blocker, maintenance_parent, other,
    ])] == [30, 40]


def test_dependency_cycle_terminates_while_ranking_reachable_work():
    new_parent = project(1, "Building", "New")
    root = ticket(11, 1)
    improve_parent = project(2, "Building", "Improve")
    cycle_a = ticket(21, 2, open_blockers=["nateprich/beta#31"])
    broken_parent = project(3, "Building", "Broken")
    cycle_b = ticket(31, 3, open_blockers=["nateprich/beta#21",
                                           "nateprich/beta#11"])
    maintenance_parent = project(4, "Building", "Maintenance")
    other = ticket(41, 4)

    assert [i.number for i in startable([
        new_parent, root, improve_parent, cycle_a, broken_parent, cycle_b,
        maintenance_parent, other,
    ])] == [11, 41]


def test_dependency_ranking_does_not_mutate_class_or_upkeep_share():
    broken_parent = project(1, "Building", "Broken")
    waiting = ticket(3, 1, open_blockers=["nateprich/beta#4"])
    improve_parent = project(2, "Building", "Improve")
    blocker = ticket(4, 2)
    closed_broken = item(
        90, "Done", "Broken", state="CLOSED", state_reason="COMPLETED",
        closed_at=at(2),
    )
    closed_new = item(
        91, "Done", "New", state="CLOSED", state_reason="COMPLETED",
        closed_at=at(3),
    )
    rows = [broken_parent, waiting, improve_parent, blocker,
            closed_broken, closed_new]
    classes = {row.ref: row.klass for row in rows}
    before = funnel.maintenance_load(rows, NOW)

    startable(rows)

    assert {row.ref: row.klass for row in rows} == classes
    assert funnel.maintenance_load(rows, NOW) == before


def test_oldest_at_gate_breaks_ties_in_the_ladder_too():
    items = [project(1, "Building", "New"), ticket(11, 1, days=3),
             project(2, "Building", "New"), ticket(12, 2, days=40)]
    assert [i.number for i in startable(items)] == [12, 11]


# -- The lock ---------------------------------------------------------------


def claimed(minutes_ago):
    return NOW - timedelta(minutes=minutes_ago)


def test_a_fresh_claim_holds_the_lock():
    held = ticket(1, 9, in_motion_since=claimed(30))
    assert lock_holder([held], NOW).number == 1
    assert stale_locks([held], NOW) == []


def test_a_claim_past_the_ttl_is_stale_and_takeable():
    stale = ticket(1, 9, in_motion_since=claimed(180))
    assert lock_holder([stale], NOW) is None
    assert [i.number for i in stale_locks([stale], NOW)] == [1]


def test_assignment_no_longer_has_anything_to_do_with_the_lock():
    """Codex acts as Nate, so an assignment says nothing about who is working."""
    rows = [project(1, "Building", "New"), ticket(2, 1, assignees=["nateprich"])]
    assert lock_holder(rows, NOW) is None
    assert next_ticket(rows, NOW) is not None


def test_a_closed_ticket_does_not_hold_the_lock():
    """A run that closed its ticket without releasing must not wedge the queue."""
    held = ticket(1, 9, state="CLOSED", in_motion_since=claimed(5))
    assert lock_holder([held], NOW) is None


def test_a_claim_below_the_limit_does_not_block_the_next_run():
    """The cap was split from the claim on 2026-09-06. One ticket in motion no
    longer stops another starting — that was policy hiding inside a correctness
    check, and `WIP_LIMIT` now says it out loud."""
    rows = [project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(5)),
            project(3, "Building", "New"), ticket(4, 3)]
    assert next_ticket(rows, NOW).number == 4


def test_a_claimed_ticket_is_never_handed_out_twice():
    """The half of the old lock that was correctness, and still is."""
    rows = [project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(5))]
    assert next_ticket(rows, NOW) is None


def test_next_returns_nothing_at_the_work_in_progress_limit():
    rows = []
    for k in range(funnel.WIP_LIMIT):
        rows += [project(10 + k, "Building", "New"),
                 ticket(20 + k, 10 + k, in_motion_since=claimed(5))]
    rows += [project(1, "Building", "New"), ticket(2, 1)]
    assert next_ticket(rows, NOW) is None


def _at_limit(extra):
    """Fill every WIP slot with ordinary work, then offer `extra`."""
    rows = []
    for k in range(funnel.WIP_LIMIT):
        rows += [project(10 + k, "Building", "New"),
                 ticket(20 + k, 10 + k, in_motion_since=claimed(5))]
    return rows + extra


def test_broken_preempts_the_limit():
    """The one sanctioned preemption, and only because Broken is finite."""
    rows = _at_limit([project(3, "Building", "Broken"), ticket(4, 3)])
    assert next_ticket(rows, NOW).number == 4


def test_maintenance_does_not_preempt_the_limit():
    """Maintenance may preempt in-flight *ranking*, but not exceed the cap."""
    rows = _at_limit([project(3, "Building", "Maintenance"), ticket(4, 3)])
    assert next_ticket(rows, NOW) is None


def test_broken_does_not_stack_on_broken_work_already_running():
    """Preemption is for getting a fix moving, not for piling fixes on fixes."""
    rows = [project(1, "Building", "Broken"),
            ticket(2, 1, in_motion_since=claimed(5)),
            project(3, "Building", "Broken"), ticket(4, 3)]
    while len(funnel.in_motion(rows, NOW)) < funnel.WIP_LIMIT:
        k = 50 + len(rows)
        rows += [project(k, "Building", "New"),
                 ticket(k + 1, k, in_motion_since=claimed(5))]
    assert next_ticket(rows, NOW) is None


def test_a_stale_claim_does_not_block_the_next_run():
    rows = [project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(300))]
    assert next_ticket(rows, NOW) is not None


def test_an_unparseable_claim_reads_as_unlocked():
    """A garbled field must not wedge the queue until somebody notices."""
    assert funnel.parse_time("whenever") is None
    assert funnel.parse_time("") is None


def test_next_returns_nothing_when_there_is_nothing_to_do():
    assert next_ticket([item(1, "Ideas", None)], NOW) is None


def test_next_excludes_declined_refs_without_changing_queue_order():
    rows = [
        project(1, "Building", "New"), ticket(2, 1),
        project(3, "Building", "New"), ticket(4, 3),
        project(5, "Building", "New"), ticket(6, 5),
    ]

    assert [candidate.number for candidate in startable(rows)] == [2, 4, 6]
    assert next_ticket(rows, NOW).number == 2
    assert next_ticket(rows, NOW, excluded={rows[1].ref}).number == 4
    assert next_ticket(
        rows, NOW, excluded={rows[1].ref, rows[3].ref}
    ).number == 6


def test_next_returns_nothing_when_every_candidate_is_excluded():
    rows = [
        project(1, "Building", "New"), ticket(2, 1),
        project(3, "Building", "New"), ticket(4, 3),
    ]

    assert next_ticket(rows, NOW, excluded={rows[1].ref, rows[3].ref}) is None


def test_next_cli_accepts_repeatable_not_filters(monkeypatch, capsys):
    rows = [
        project(1, "Building", "New"), ticket(2, 1),
        project(3, "Building", "New"), ticket(4, 3),
        project(5, "Building", "New"), ticket(6, 5),
    ]
    monkeypatch.setattr(funnel, "load_items", lambda: rows)
    monkeypatch.setattr(funnel, "awaiting_review", lambda items: set())

    assert funnel.main([
        "next", "--not", rows[1].ref, "--not", rows[3].ref,
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == rows[5].ref


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
    assert [i.number for i in items] == [10, 11, 12, 13, 14, 15, 16]


def test_a_draft_issue_is_skipped():
    nodes = json.loads(FIXTURE.read_text())
    assert funnel._from_node(nodes[-1]) is None


def test_time_at_gate_ignores_other_projects():
    """An issue may sit in several Projects; only this one counts."""
    nodes = json.loads(FIXTURE.read_text())
    beta11 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 11)
    assert beta11.status_since == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert beta11.first_child_created_at == datetime(2026, 9, 5, 11, tzinfo=timezone.utc)


def test_fixture_parses_the_last_child_close_time():
    nodes = json.loads(FIXTURE.read_text())
    alpha10 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 10)

    assert alpha10.last_child_closed_at == datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert question_since(alpha10) == alpha10.last_child_closed_at


def test_fixture_parses_a_blocked_label_time_without_status():
    nodes = json.loads(FIXTURE.read_text())
    blocked16 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 16)

    assert blocked16.status is None
    assert blocked16.blocked_since == datetime(2026, 9, 4, 11, tzinfo=timezone.utc)
    assert blocked16.waited(datetime(2026, 9, 5, 12, tzinfo=timezone.utc)) == timedelta(days=1, hours=1)


def test_time_at_gate_uses_the_last_move_into_the_current_status():
    nodes = json.loads(FIXTURE.read_text())
    alpha10 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 10)
    assert alpha10.status_since == datetime(2026, 8, 26, tzinfo=timezone.utc)


def test_a_claim_is_parsed_from_the_project_field():
    nodes = json.loads(FIXTURE.read_text())
    items = [i for i in (funnel._from_node(n) for n in nodes) if i]
    holder = lock_holder(items, datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc))
    assert holder is not None and holder.number == 13


def test_an_empty_lock_field_is_not_a_claim():
    nodes = json.loads(FIXTURE.read_text())
    beta12 = next(i for i in (funnel._from_node(n) for n in nodes) if i and i.number == 12)
    assert beta12.in_motion_since is None


# -- rejected merges: the feedback loop on unattended merging ---------------


def regression(number, days_ago, pr=99):
    return item(number, None, None, days=days_ago,
                title="{}{}: something".format(funnel.REGRESSION_PREFIX, pr))


def test_rejected_merges_are_counted_from_the_issues_themselves():
    """The regression issues are the counter — GitHub is the state, so there is
    nothing separate to keep in step."""
    verdict = funnel.rejected_merges([regression(1, 1), regression(2, 3)], NOW)
    assert verdict["count"] == 2
    assert not verdict["stop_auto_merging"]


def test_three_in_a_week_says_stop_auto_merging():
    """Not 'there are bugs' — the auto-merge bar has failed."""
    rows = [regression(1, 1), regression(2, 3), regression(3, 6)]
    assert funnel.rejected_merges(rows, NOW)["stop_auto_merging"]


def test_older_rejections_fall_out_of_the_window():
    rows = [regression(1, 1), regression(2, 30), regression(3, 60)]
    verdict = funnel.rejected_merges(rows, NOW)
    assert verdict["count"] == 1
    assert not verdict["stop_auto_merging"]


def test_ordinary_issues_are_not_counted_as_rejections():
    assert funnel.rejected_merges([item(1, "Ready", "New")], NOW)["count"] == 0


# -- Shaped to Ready: the breakdown ----------------------------------------


def test_ready_with_no_tickets_waits_on_the_funnel_not_on_nate():
    """Ready is not a human gate, whether or not it has tickets."""
    assert gate_question(item(1, "Ready", "New", children_total=0)) is None
    assert gate_question(item(2, "Ready", "New", children_total=3)) is None


def test_approved_plans_with_no_tickets_are_owed_a_breakdown():
    rows = [item(1, "Ready", "New", children_total=0),
            item(2, "Ready", "New", children_total=2),
            item(3, "Shaped", "New", children_total=0)]
    assert [i.number for i in funnel.awaiting_breakdown(rows)] == [1]


def test_a_blocked_plan_is_not_owed_a_breakdown():
    rows = [item(1, "Ready", "New", children_total=0, labels=["blocked"])]
    assert funnel.awaiting_breakdown(rows) == []


def test_breakdowns_are_ordered_oldest_first_like_everything_else():
    rows = [item(1, "Ready", "New", days=2, children_total=0),
            item(2, "Ready", "New", days=20, children_total=0)]
    assert [i.number for i in funnel.awaiting_breakdown(rows)] == [2, 1]


def test_an_unbroken_plan_never_reaches_codex():
    """A parentless item is a project, never a ticket. One with no children is
    awaiting its breakdown, not waiting to be worked — treating it as both is
    what put an issue in two queues at once."""
    assert startable([item(1, "Ready", "New", children_total=0)]) == []


def test_shaped_plan_with_an_open_needs_you_question_waits_on_nate():
    shaped = item(
        1, "Shaped", "New",
        body="## Needs you\n\nWhich repository should this use?\n",
    )

    assert gate_question(shaped) == "Is the plan good?"


def test_shaped_plan_declaring_nothing_open_does_not_wait_on_nate():
    shaped = item(1, "Shaped", "New", body="## Needs you\n\nNothing.\n")

    assert gate_question(shaped) is None


def test_shaped_plan_without_needs_you_fails_closed_at_the_plan_gate():
    shaped = item(
        1, "Shaped", "New",
        body="## Decided from precedent\n\nUse the existing command.\n",
    )

    assert gate_question(shaped) == "Is the plan good?"


# -- the Ideas stage: what feeds everything else ---------------------------


def test_broken_ideas_come_first_then_oldest():
    rows = [
        item(1, "Ideas", "New", days=1),
        item(2, "Ideas", "New", days=30, labels=["needs-shaping"]),
        item(3, "Ideas", "Broken", days=5, labels=["needs-shaping"]),
    ]
    assert [i.number for i in funnel.ideas(rows)] == [3, 2, 1]


def test_needs_shaping_does_not_change_idea_order():
    rows = [
        item(1, "Ideas", "New", days=30),
        item(2, "Ideas", "New", days=1, labels=["needs-shaping"]),
    ]
    assert [i.number for i in funnel.ideas(rows)] == [1, 2]


def test_only_open_ideas_are_listed():
    rows = [item(1, "Ideas", None), item(2, "Ideas", None, state="CLOSED"),
            item(3, "Shaped", "New")]
    assert [i.number for i in funnel.ideas(rows)] == [1]


def test_ideas_are_listed_but_never_counted_as_waiting():
    """Unbounded and guilt-free: askable on request, absent from every gate
    count, and never a decision pending on Nate."""
    rows = [item(n, "Ideas", None) for n in range(1, 20)]
    assert len(funnel.ideas(rows)) == 19
    assert awaiting_decision(rows) == []
    assert [i for i in rows if needs_class(i)] == []


# -- answering a gate is Nate's, and is a dry run by default ---------------


def test_every_gate_has_exactly_one_answering_command():
    """Each question the brief asks must be answerable, and only from the stage
    that asks it."""
    assert set(funnel.ANSWERS) == {"approve", "accept"}
    for verb, (frm, to, _) in funnel.ANSWERS.items():
        assert frm in funnel.STAGES and to in funnel.STAGES
        # never skips a gate
        assert funnel.STAGES.index(to) == funnel.STAGES.index(frm) + 1


# -- work already sitting in a PR ---------------------------------------------


def test_a_ticket_with_an_open_pr_is_not_handed_out_again():
    """Codex built #19 at 08:00 on 2026-09-06, then spent the 09:00 and 10:00
    runs re-verifying the same branch. A ticket's issue stays open until review
    merges it, and review was blocked by the budget gate, so `next` kept
    returning finished work."""
    items = [project(1, "Building", "Improve", children=2),
             ticket(19, 1), ticket(20, 1)]
    assert [i.number for i in startable(items)] == [19, 20]
    assert [i.number for i in startable(
        items, awaiting_review={"nateprich/beta#19"})] == [20]


def test_every_ticket_awaiting_review_means_nothing_to_do():
    items = [project(1, "Building", "Improve"), ticket(19, 1)]
    blocked = {"nateprich/beta#19"}
    assert startable(items, awaiting_review=blocked) == []
    assert next_ticket(items, at(0), blocked=blocked) is None


def test_a_tier_takes_only_its_own_work_in_both_directions():
    """`standard` walking past risky tickets is obvious. `escalated` walking past
    ordinary ones is the same rule the other way: Sol idling beats Sol spending
    its quota on bounded work the continuous cheap schedule already handles."""
    assert funnel.required_tier("t", "Risk: escalated \u2014 concurrency") == "escalated"
    assert funnel.required_tier("t", "Risk: standard") == "standard"

    # An escalated caller wants tickets whose reasons are non-empty; a standard
    # caller wants the opposite. That predicate is what cmd_next applies.
    for body, tier, wanted in (("Risk: escalated", "escalated", True),
                               ("Risk: escalated", "standard", False),
                               ("Risk: standard", "escalated", False),
                               ("Risk: standard", "standard", True)):
        found = funnel.escalation_reasons("t", body)
        assert (bool(found) if tier == "escalated" else not found) is wanted


def test_a_project_with_no_tickets_is_not_the_same_as_one_with_open_tickets():
    """#26's work shipped outside the pipeline on Nate's instruction, so it sat
    at Ready with no children: invisible to his queue because gate_question
    returns None, permanently in awaiting_breakdown burning a routine run every
    time, and refused by accept's own guard. The guard cannot tell "not broken
    down yet" from "built another way", so accept asks him to say which."""
    childless = project(1, "Building", "New", children=0)
    with_open = project(2, "Building", "New", children=3, done=1)

    assert not childless.children_all_closed   # unchanged: not vacuously complete
    assert not with_open.children_all_closed
    assert gate_question(childless) is None    # never reaches his queue on its own
