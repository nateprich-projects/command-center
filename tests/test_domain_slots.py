"""Two slots per domain with an entry-only cap (#2407, #2428).

Pure over fixtures: only agent-movable Building work counts,
waiting-on-Nate holds no slot, urgent (Broken) and Bugs stay outside,
and new entries block while over-cap in-flight finishes first.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
CC = "owner/command-center"
WB = "owner/workbench"


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


def test_domain_slots_is_two():
    assert funnel.DOMAIN_SLOTS == 2


def test_mixed_fixtures_count_only_agent_movable():
    """Waiting-on-Nate, urgent and Bugs hold no slot (#2428)."""
    movable = proj(CC, 10, "Building", "Improve", days=2)
    movable_ticket = tick(CC, 11, movable.ref, days=2)
    urgent = proj(CC, 20, "Building", "Broken", days=2)
    urgent_ticket = tick(CC, 21, urgent.ref, days=2)
    bug = proj(CC, 30, "Building", "Bug", days=2)
    bug_ticket = tick(CC, 31, bug.ref, days=2)
    waiting = proj(
        CC, 40, "Building", "Improve", days=2,
        labels=["blocked"], needs="human",
    )
    waiting_ticket = tick(CC, 41, waiting.ref, days=2)
    rows = [
        movable, movable_ticket, urgent, urgent_ticket,
        bug, bug_ticket, waiting, waiting_ticket,
    ]
    assert funnel.domain_slot_counts(rows) == {"command-center": 1}
    assert funnel._waits_on_nate(
        waiting, {item.ref: item for item in rows}) is True
    assert funnel._is_slot_urgent(
        urgent, {item.ref: item for item in rows}) is True


def test_fully_blocked_in_flight_holds_no_slot():
    """In-flight with only prerequisites outstanding has nothing agents can
    move, so it must not wedge new entries behind it (#2428)."""
    parent = proj(CC, 10, "Building", "Improve", days=2)
    blocked = tick(
        CC, 11, parent.ref, days=2, open_blockers=["owner/work#99"])
    assert funnel.domain_slot_counts([parent, blocked]) == {}
    human_only = tick(CC, 12, parent.ref, days=2, needs="human")
    assert funnel.domain_slot_counts([parent, human_only]) == {}


def test_finished_project_waiting_accept_holds_no_slot():
    """A Building project with no open work is done, not occupying."""
    finished = proj(CC, 10, "Building", "New", children=1, done=1)
    assert funnel.gate_question(finished) == "Accept it?"
    assert funnel.domain_slot_counts([finished]) == {}


def test_ready_projects_hold_no_slot_until_they_enter():
    ready = proj(CC, 10, "Ready", "Improve")
    assert funnel.domain_slot_counts([ready]) == {}


def test_blocked_building_holds_no_slot():
    blocked = proj(
        CC, 10, "Building", "Improve", labels=["blocked"], needs="agent",
        block_references=["owner/command-center#99"],
    )
    assert funnel.domain_slot_counts([blocked]) == {}


def test_unattributable_domain_holds_no_slot_and_never_blocks():
    """Fantasy-GM without an explicit Domain is a data fix, not a cap."""
    project = proj("owner/Fantasy-GM", 10, "Building", "Improve")
    ticket = tick("owner/Fantasy-GM", 11, project.ref)
    rows = [project, ticket]
    assert funnel.domain_slot_counts(rows) == {}
    assert funnel.domain_entry_allowed(rows, ticket) is True


def test_domains_count_independently():
    p10 = proj(CC, 10, "Building", "Improve")
    t11 = tick(CC, 11, p10.ref)
    p20 = proj(CC, 20, "Building", "New")
    t21 = tick(CC, 21, p20.ref)
    p30 = proj(WB, 30, "Building", "Improve")
    t31 = tick(WB, 31, p30.ref)
    rows = [p10, t11, p20, t21, p30, t31]
    assert funnel.domain_slot_counts(rows) == {
        "command-center": 2, "workbench": 1,
    }


def _full_domain(extra):
    """Two agent-movable Building projects in command-center, plus extra."""
    p10 = proj(CC, 10, "Building", "Improve", days=2)
    t11 = tick(CC, 11, p10.ref, days=2)
    p20 = proj(CC, 20, "Building", "New", days=2)
    t21 = tick(CC, 21, p20.ref, days=2)
    return [p10, t11, p20, t21] + extra


def test_new_entry_blocks_when_its_domain_is_full():
    p30 = proj(CC, 30, "Ready", "Improve", days=1)
    t31 = tick(CC, 31, p30.ref, days=1)
    rows = _full_domain([p30, t31])
    assert funnel.domain_slot_counts(rows) == {"command-center": 2}
    assert funnel.domain_entry_allowed(rows, t31) is False
    # In-flight tickets stay allowed even at the cap.
    assert funnel.domain_entry_allowed(rows, rows[1]) is True
    assert funnel.domain_entry_allowed(rows, rows[3]) is True


def test_urgent_and_bug_entries_stay_outside_a_full_domain():
    broken = proj(CC, 30, "Ready", "Broken", days=1)
    broken_ticket = tick(CC, 31, broken.ref, days=1)
    bug = proj(CC, 40, "Ready", "Bug", days=1)
    bug_ticket = tick(CC, 41, bug.ref, days=1)
    rows = _full_domain([broken, broken_ticket, bug, bug_ticket])
    assert funnel.domain_entry_allowed(rows, broken_ticket) is True
    assert funnel.domain_entry_allowed(rows, bug_ticket) is True


def test_maintenance_and_pins_do_not_bypass_the_cap():
    """Finite ranking preemption is not a slot exemption; pins are out of
    scope here, so a pinned new entry still blocks when full."""
    maintenance = proj(CC, 30, "Ready", "Maintenance", days=1)
    maintenance_ticket = tick(CC, 31, maintenance.ref, days=1)
    pinned = proj(CC, 40, "Ready", "Improve", days=1, pinned=True)
    pinned_ticket = tick(CC, 41, pinned.ref, days=1)
    rows = _full_domain(
        [maintenance, maintenance_ticket, pinned, pinned_ticket])
    assert funnel.domain_entry_allowed(rows, maintenance_ticket) is False
    assert funnel.domain_entry_allowed(rows, pinned_ticket) is False


def test_other_domains_enter_while_one_is_full():
    other = proj(WB, 30, "Ready", "Improve", days=1)
    other_ticket = tick(WB, 31, other.ref, days=1)
    same = proj(CC, 40, "Ready", "Improve", days=1)
    same_ticket = tick(CC, 41, same.ref, days=1)
    rows = _full_domain([other, other_ticket, same, same_ticket])
    assert funnel.domain_entry_allowed(rows, other_ticket) is True
    assert funnel.domain_entry_allowed(rows, same_ticket) is False


def test_in_flight_finishes_first_over_a_blocked_finite_entry():
    """Entry-only: a Ready Maintenance would preempt by rank, but its
    domain is full, so the Building Improve goes first."""
    maintenance = proj(CC, 30, "Ready", "Maintenance", days=1)
    maintenance_ticket = tick(CC, 31, maintenance.ref, days=1)
    rows = _full_domain([maintenance, maintenance_ticket])
    order = [item.number for item in funnel.startable(rows)]
    assert order.index(31) < order.index(11)
    chosen = funnel.next_ticket(rows, NOW)
    assert chosen is not None and chosen.number == 11


def test_over_cap_in_flight_still_finishes_while_new_entries_block():
    """Three Building projects over the cap of two: their tickets run,
    a same-class Ready entry waits."""
    p30 = proj(CC, 30, "Building", "Replace", days=2)
    t31 = tick(CC, 31, p30.ref, days=2)
    newcomer = proj(CC, 40, "Ready", "Improve", days=1)
    newcomer_ticket = tick(CC, 41, newcomer.ref, days=1)
    rows = _full_domain([p30, t31, newcomer, newcomer_ticket])
    assert funnel.domain_slot_counts(rows) == {"command-center": 3}
    assert funnel.domain_entry_allowed(rows, t31) is True
    assert funnel.domain_entry_allowed(rows, newcomer_ticket) is False
    chosen = funnel.next_ticket(rows, NOW)
    assert chosen is not None and chosen.number in (11, 21, 31)


def test_full_domain_with_only_a_new_entry_offers_nothing():
    """In-flight tickets claimed, only a blocked Ready entry left."""
    p30 = proj(CC, 30, "Ready", "Improve", days=1)
    t31 = tick(CC, 31, p30.ref, days=1)
    rows = _full_domain([p30, t31])
    rows[1].in_motion_since = at(0.01)
    rows[3].in_motion_since = at(0.01)
    assert funnel.next_ticket(rows, NOW) is None
    # An urgent entry still starts from the same full board.
    broken = proj(CC, 40, "Ready", "Broken", days=1)
    broken_ticket = tick(CC, 41, broken.ref, days=1)
    urgent_rows = rows + [broken, broken_ticket]
    chosen = funnel.next_ticket(urgent_rows, NOW)
    assert chosen is not None and chosen.number == 41


def test_bug_share_survives_among_slot_allowed_work():
    """Bugs stay outside slots but keep their 2-in-8 share of starts."""
    bug = proj(CC, 30, "Ready", "Bug", days=1)
    bug_ticket = tick(CC, 31, bug.ref, days=1)
    rows = _full_domain([bug, bug_ticket])
    due = funnel.next_ticket(rows, NOW, recent_starts=[])
    assert due is not None and due.number == 31
    held = funnel.next_ticket(rows, NOW, recent_starts=["Bug", "Bug"])
    assert held is not None and held.number == 11
