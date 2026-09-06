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


def project(number, status, klass, days=1.0, children=1, done=0, **kw) -> Item:
    """A parentless item with tickets under it — what the gates act on."""
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
    items = [project(1, "Ready", "New", days=2), project(2, "Ready", "New", days=30),
             project(3, "Ready", "New", days=9)]
    assert [i.number for i in awaiting_decision(items)] == [2, 3, 1]


def test_a_closed_item_waits_on_nobody():
    assert gate_question(item(1, "Ready", "New", state="CLOSED")) is None


def test_blocked_surfaces_as_its_own_question_and_leads_its_stage():
    blocked = project(1, "Ready", "New", days=1, labels=["blocked"])
    older = project(2, "Ready", "New", days=20)
    order = awaiting_decision([older, blocked])
    assert order[0].number == 1
    assert gate_question(blocked) == "Unblock or park?"


def test_unknown_status_still_appears_rather_than_vanishing():
    """An item with an unrecognised Status must not be silently dropped."""
    items = [project(1, "Shaped", "New"), project(2, "Ready", "New")]
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


def test_ready_is_not_startable_until_nate_says_start_now():
    """Ready means broken into issues and still waiting on his gate. Starting
    there jumps it — he answers by moving the parent to Building."""
    assert startable([project(1, "Ready", "New"), ticket(2, 1)]) == []
    assert [i.number for i in startable(
        [project(1, "Building", "New"), ticket(2, 1)])] == [2]


def test_shaped_work_is_not_startable():
    """Ready is the gate that says 'start now'. Shaped has not passed it."""
    assert startable([project(1, "Shaped", "New"), ticket(2, 1)]) == []


def test_blocked_work_is_not_startable_at_either_level():
    assert startable([project(1, "Building", "New"), ticket(2, 1, labels=["blocked"])]) == []
    parent = project(3, "Building", "New", labels=["blocked"])
    assert startable([parent, ticket(4, 3)]) == []


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


def test_next_returns_nothing_while_the_lock_is_held():
    rows = [project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(5)),
            project(3, "Building", "New"), ticket(4, 3)]
    assert next_ticket(rows, NOW) is None


def test_broken_preempts_a_held_lock():
    """The one sanctioned preemption, and only because Broken is finite."""
    rows = [project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(5)),
            project(3, "Building", "Broken"), ticket(4, 3)]
    assert next_ticket(rows, NOW).number == 4


def test_maintenance_does_not_preempt_a_held_lock():
    """Maintenance may preempt in-flight *ranking*, but not seize a live lock."""
    rows = [project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(5)),
            project(3, "Building", "Maintenance"), ticket(4, 3)]
    assert next_ticket(rows, NOW) is None


def test_broken_does_not_preempt_a_lock_already_held_for_broken_work():
    rows = [project(1, "Building", "Broken"), ticket(2, 1, in_motion_since=claimed(5)),
            project(3, "Building", "Broken"), ticket(4, 3)]
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
    """Nate writing Ready *is* his answer to 'is the plan good?'. Asking 'start
    now?' about something with nothing to start asks him to approve an empty
    box."""
    assert gate_question(item(1, "Ready", "New", children_total=0)) is None
    assert gate_question(item(2, "Ready", "New", children_total=3)) == "Start now?"


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


# -- the Ideas stage: what feeds everything else ---------------------------


def test_flagged_ideas_come_first_then_oldest():
    """`needs-shaping` means Nate already decided it is worth thinking through."""
    rows = [
        item(1, "Ideas", None, days=1),
        item(2, "Ideas", None, days=30),
        item(3, "Ideas", None, days=5, labels=["needs-shaping"]),
    ]
    assert [i.number for i in funnel.ideas(rows)] == [3, 2, 1]


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
    assert set(funnel.ANSWERS) == {"approve", "start", "accept"}
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
