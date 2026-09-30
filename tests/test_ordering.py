"""Ordering rules — the part most likely to be subtly wrong.

Everything here is a pure function over Items. No network, no clock beyond the
`NOW` passed in explicitly.
"""

from __future__ import annotations

import copy
import json
import pathlib
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import (  # noqa: E402
    Item,
    effective_class,
    awaiting_decision,
    awaiting_breakdown,
    disposal,
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

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _next_reads_no_live_heartbeat(monkeypatch):
    """`cmd_next` now consults the heartbeat for tickets finished by comments
    (#498); the fixtures here describe the queue, not the spool."""
    monkeypatch.setattr(funnel, "finished_by_comments", lambda items: set())
    monkeypatch.setattr(
        funnel, "ticket_pr_facts", lambda items: funnel.TicketPRFacts()
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
    body = kw.get("body") or ""
    legacy_origin = funnel.parse_origin(body)
    kw.setdefault(
        "origin",
        ("agent" if legacy_origin and legacy_origin["voice"] == "agent"
         else "Nate" if legacy_origin else None),
    )
    kw.setdefault("risk", "escalated" if "Risk: escalated" in body else "standard")
    kw.setdefault("needs", "human" if status == "Shaped" else "none")
    return Item(
        number=number, status=status, klass=klass, status_since=at(days), **kw
    )


def repo_project(repo, number, status="Building", klass="New") -> Item:
    return Item(
        repo=repo, number=number, title="project {}".format(number), url="",
        state="OPEN", status=status, klass=klass, status_since=at(1),
        children_total=1, origin="agent", risk="standard", needs="none",
    )


def repo_ticket(repo, number, parent) -> Item:
    return Item(
        repo=repo, number=number, title="ticket {}".format(number), url="",
        state="OPEN", parent="{}#{}".format(repo, parent),
        origin="agent", risk="standard", needs="none",
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


def _completion_policy_body(origin, override=None):
    parts = []
    if origin in funnel.ORIGIN_VOICES:
        parts.append(
            funnel.origin_block(origin, at=NOW, run="gate-run", agent="codex")
        )
    if override is not None:
        parts.append(
            funnel.ORIGIN_OVERRIDE_MARKER
            + "\n\n```json\n"
            + json.dumps({"target": override})
            + "\n```"
        )
        if override == "agents":
            parts.append(
                funnel.provenance_block(
                    "nate-relayed", at=NOW, run="override-run", agent="claude"
                )
            )
    return "\n\n".join(parts) or None


@_pytest.mark.parametrize(
    ("klass", "origin", "override", "can_close"),
    [
        ("Investigate", None, None, True),
        ("Investigate", "nate-relayed", None, True),
        ("Broken", None, None, True),
        ("Broken", "nate-relayed", None, True),
        ("Maintenance", None, None, True),
        ("Maintenance", "nate-relayed", None, True),
        # Bug closes itself exactly as Broken does, by #987's rule (#1845).
        ("Bug", "agent", None, True),
        ("Bug", "nate-relayed", None, True),
        ("Bug", None, None, True),
        ("Improve", "agent", None, True),
        ("Improve", "nate-relayed", None, False),
        ("Improve", None, None, False),
        ("Improve", "nate-relayed", "agents", True),
        ("Improve", "agent", "nate", False),
        ("New", "agent", None, False),
        ("Replace", "agent", None, False),
        (None, "agent", None, False),
    ],
)
def test_building_gate_and_close_share_class_origin_policy(
    klass, origin, override, can_close
):
    finished = project(
        1,
        "Building",
        klass,
        children=2,
        done=2,
        body=_completion_policy_body(origin, override),
    )

    assert (gate_question(finished) is None) is can_close
    assert funnel._could_carry_closed_itself_marker(finished) is can_close
    assert funnel._auto_closeable_project(finished) is can_close


@_pytest.mark.parametrize(
    ("klass", "origin", "marker", "can_close"),
    [
        (
            "Maintenance", None,
            funnel.ANALYSIS_MARKER + '\n\n```json\n{"analysis": true}\n```',
            False,
        ),
        ("Maintenance", None, None, True),
        (
            "Maintenance", None,
            funnel.ANALYSIS_MARKER + "\n\n```json\n{not json}\n```",
            False,
        ),
        ("Improve", "agent", None, True),
    ],
)
def test_analysis_marker_waits_and_preserves_other_close_rules(
    klass, origin, marker, can_close
):
    body = _completion_policy_body(origin)
    if marker is not None:
        body = "\n\n".join(part for part in (body, marker) if part)
    finished = project(
        1, "Building", klass, children=1, done=1, body=body,
    )

    assert funnel._can_close_itself(finished) is can_close
    assert gate_question(finished) == (None if can_close else "Accept it?")


@_pytest.mark.parametrize(
    ("klass", "body", "expected_reason"),
    [
        (
            "Maintenance",
            funnel.ANALYSIS_MARKER + '\n```json\n{"analysis": true}\n```',
            "Analysis review",
        ),
        ("New", "", "Ordinary accept"),
        (
            "Maintenance",
            funnel.ANALYSIS_MARKER + "\n```json\n{not json}\n```",
            "Analysis review",
        ),
    ],
)
def test_brief_item_explains_why_building_waits_for_acceptance(
    klass, body, expected_reason
):
    finished = project(
        1, "Building", klass, children=1, done=1, body=body,
    )

    rendered = funnel.item_json(finished, NOW)

    assert rendered["waiting_on"] == "Accept it?"
    assert rendered["waiting_reason"] == expected_reason


def test_new_replace_and_unset_class_still_wait_for_acceptance():
    for number, klass in enumerate(("New", "Replace", None), start=1):
        finished = project(number, "Building", klass, children=1, done=1)

        assert gate_question(finished) == "Accept it?"


def test_drift_is_reported_without_changing_the_building_gate():
    drifted = project(1, "Building", "New", children=2, done=2)
    facts = funnel.DriftFacts(
        ready_at=NOW - timedelta(days=2),
        plan_edit_times=(NOW - timedelta(days=1),),
        review_verdicts=({"verdict": "rejected"},),
        regression_pr_numbers=(20,),
        building_at=NOW - timedelta(hours=2),
        ticket_created_at=(NOW - timedelta(hours=1),),
    )

    assert funnel.drift_since_approval(drifted, facts) == list(
        funnel.DRIFT_SIGNAL_NAMES
    )
    assert gate_question(drifted) == "Accept it?"
    assert gate_question(
        project(2, "Building", "Broken", children=2, done=2)
    ) is None


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


def test_pinned_work_outranks_unpinned_work_at_the_same_gate():
    items = [
        project(1, "Shaped", "Improve", days=30),
        project(2, "Shaped", "Improve", days=1, pinned=True),
    ]
    assert [i.number for i in awaiting_decision(items)] == [2, 1]


def test_broken_work_outranks_pinned_work_at_the_same_gate():
    items = [
        project(1, "Shaped", "Improve", days=30, pinned=True),
        project(2, "Shaped", "Broken", days=1),
    ]
    assert [i.number for i in awaiting_decision(items)] == [2, 1]


def test_pinned_work_does_not_cross_decision_gates():
    items = [
        project(1, "Shaped", "Improve", days=30, pinned=True),
        project(2, "Building", "New", children=1, done=1, days=1),
    ]
    assert [i.number for i in awaiting_decision(items)] == [2, 1]


def test_ticket_inherits_broken_for_decision_ordering():
    parent = project(1, "Ideas", "Broken")
    child = item(2, "Shaped", None, days=1, parent=parent.ref)
    older = project(3, "Shaped", "New", days=30)
    assert [i.number for i in awaiting_decision([parent, child, older])] == [2, 3]


def test_ticket_inherits_investigate_for_decision_ordering():
    parent = project(1, "Ideas", "Investigate")
    child = item(2, "Shaped", None, days=1, parent=parent.ref)
    older = project(3, "Shaped", "Broken", days=30)
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


def test_a_blocked_ticket_with_a_breakdown_question_still_asks_to_unblock():
    blocked = ticket(
        1, 9, labels=["blocked"], needs_decision="Where should this live?"
    )

    assert gate_question(blocked) == "Unblock?"


def test_a_ready_project_with_a_breakdown_question_leaves_breakdown_queue():
    blocked = project(
        1, "Ready", "New", children=0, labels=["blocked"],
        needs_decision="Where should this connector live?",
    )

    assert awaiting_breakdown([blocked]) == []
    assert awaiting_decision([blocked]) == [blocked]
    assert gate_question(blocked) == "Answer the breakdown's question?"


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
    assert ladder_index(None) > ladder_index("Bug")
    assert ladder_index(None) != ladder_index("Broken")
    assert ladder_index("nonsense") > ladder_index("Replace")


def test_ladder_is_in_the_documented_order():
    ranks = [ladder_index(c) for c in [
        "Investigate", "Broken", "Maintenance", "Improve", "New", "Replace",
        "Bug",
    ]]
    assert ranks == sorted(ranks) and len(set(ranks)) == 7


def test_investigate_is_first_without_gaining_preemption():
    assert ladder_index("Investigate") == 0
    assert funnel.PREEMPTING_CLASSES == frozenset({"Broken", "Maintenance"})
    assert funnel.SELF_APPROVABLE_CLASSES == frozenset(
        {"Investigate", "Broken", "Maintenance", "Improve", "Bug"}
    )


def test_anything_past_ideas_must_carry_a_class():
    assert needs_class(item(1, "Shaped", None))
    assert needs_class(item(2, "Ready", None))
    assert needs_class(item(3, "Building", None))
    assert not needs_class(item(4, "Building", "Investigate"))


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
                     (5, "Improve"), (6, "Investigate")):
        items += [project(n, "Building", klass), ticket(10 + n, n)]
    # Finite Broken/Maintenance work preempts in-flight work; Investigate is
    # first among the non-preempting classes.
    assert [i.number for i in startable(items)] == [12, 14, 16, 15, 13, 11]


def test_an_investigate_ticket_is_startable_without_preempting_broken_work():
    investigate_parent = project(1, "Building", "Investigate")
    investigate_ticket = ticket(2, 1)
    broken_parent = project(3, "Building", "Broken")
    broken_ticket = ticket(4, 3)

    assert [i.number for i in startable([
        broken_parent, broken_ticket, investigate_parent, investigate_ticket,
    ])] == [4, 2]


def test_a_pinned_projects_tickets_lead_the_engineers_queue():
    """Reversed 2026-09-12 on Nate's ruling: a pin was always meant to move the
    work, not only the decision (#673). Older in-flight work yields to it."""
    rows = [
        project(1, "Ready", "Improve", days=1, pinned=True), ticket(2, 1),
        project(3, "Building", "Improve", days=30), ticket(4, 3),
    ]
    assert [i.number for i in startable(rows)] == [2, 4]


def test_finite_work_leads_a_pin():
    """Nate, 2026-09-25: preemption and the pin swap places. Finite work ends,
    so it cannot starve the pinned project for long."""
    rows = [
        project(1, "Ready", "Improve", days=1, pinned=True), ticket(2, 1),
        project(3, "Building", "Broken", days=30), ticket(4, 3),
    ]
    assert [i.number for i in startable(rows)] == [4, 2]


def tier_project(repo, number, status, klass, days=1.0, **kw) -> Item:
    return item(number, status, klass, days=days, repo=repo,
                children_total=1, children_done=0, **kw)


def tier_ticket(repo, number, parent, days=1.0, **kw) -> Item:
    return item(number, None, None, days=days, repo=repo,
                parent="{}#{}".format(repo, parent), **kw)


TOOLING = "nateprich-projects/command-center"
IMPACT = "nateprich-projects/career-toolset"
HOBBY = "nateprich-projects/The-League"


def test_repo_tiers_name_the_tooling_and_the_real_world_repos():
    assert [funnel.repo_tier("nateprich-projects/" + name) for name in (
        "command-center", "github-runners", "workbench",
        "career-toolset", "jeffy-finance-agent",
        "The-League", "Fantasy-GM", "AFL",
    )] == [1, 1, 1, 2, 2, 3, 3, 3]


def test_higher_tier_work_does_not_wait_for_a_lower_tiers_building_project():
    """Nate, 2026-09-25: "Non-finite work in a higher tier repo shouldn't have
    to wait for non-finite work in a lower tier repo to complete just because
    that work happens to already be building." """
    rows = [
        tier_project(HOBBY, 1, "Building", "Improve", days=30),
        tier_ticket(HOBBY, 2, 1, days=30),
        tier_project(IMPACT, 3, "Ready", "New", days=1),
        tier_ticket(IMPACT, 4, 3, days=1),
        tier_project(TOOLING, 5, "Ready", "Replace", days=1),
        tier_ticket(TOOLING, 6, 5, days=1),
    ]
    assert [i.number for i in startable(rows)] == [6, 4, 2]


def test_within_a_tier_the_building_commitment_still_holds():
    rows = [
        tier_project(TOOLING, 1, "Building", "Replace", days=1),
        tier_ticket(TOOLING, 2, 1, days=1),
        tier_project(TOOLING, 3, "Ready", "Improve", days=30),
        tier_ticket(TOOLING, 4, 3, days=30),
    ]
    assert [i.number for i in startable(rows)] == [2, 4]


def test_finite_work_in_a_hobby_repo_still_preempts_higher_tier_work():
    rows = [
        tier_project(TOOLING, 1, "Building", "Improve", days=30),
        tier_ticket(TOOLING, 2, 1, days=30),
        tier_project(HOBBY, 3, "Ready", "Broken", days=1),
        tier_ticket(HOBBY, 4, 3, days=1),
    ]
    assert [i.number for i in startable(rows)] == [4, 2]


def test_among_finite_work_the_tier_comes_before_the_building_commitment():
    rows = [
        tier_project(HOBBY, 1, "Building", "Broken", days=30),
        tier_ticket(HOBBY, 2, 1, days=30),
        tier_project(TOOLING, 3, "Ready", "Maintenance", days=1),
        tier_ticket(TOOLING, 4, 3, days=1),
    ]
    assert [i.number for i in startable(rows)] == [4, 2]


def test_a_ticket_blocking_higher_tier_work_takes_that_tier():
    rows = [
        tier_project(HOBBY, 1, "Building", "Improve", days=30),
        tier_ticket(HOBBY, 2, 1, days=30),
        tier_project(HOBBY, 3, "Ready", "Improve", days=1),
        tier_ticket(HOBBY, 4, 3, days=1),
        tier_project(TOOLING, 5, "Ready", "Improve", days=1),
        tier_ticket(TOOLING, 6, 5, days=1,
                    open_blockers=[HOBBY + "#4"]),
    ]
    # #6 waits on #4, so #4 ranks as tier 1 and passes the hobby's
    # in-flight #2.
    assert [i.number for i in startable(rows)] == [4, 2]


def test_two_pinned_projects_keep_the_ladders_order_between_them():
    rows = [
        project(1, "Ready", "Improve", days=1, pinned=True), ticket(2, 1),
        project(3, "Building", "Broken", days=30, pinned=True), ticket(4, 3),
    ]
    assert [i.number for i in startable(rows)] == [4, 2]


def test_shared_startable_order_puts_finite_work_before_pins_and_pins_before_class():
    rows = [
        project(1, "Building", "Improve"), ticket(2, 1),
        project(3, "Building", "Replace", pinned=True), ticket(4, 3),
        project(5, "Building", "Broken"), ticket(6, 5),
    ]

    assert [i.number for i in startable(rows)] == [6, 4, 2]


def test_shared_startable_order_puts_pins_before_repository_tier():
    rows = [
        tier_project(HOBBY, 1, "Ready", "Improve", pinned=True),
        tier_ticket(HOBBY, 2, 1),
        tier_project(TOOLING, 3, "Ready", "Improve"),
        tier_ticket(TOOLING, 4, 3),
    ]

    assert [item.number for item in funnel.startable_listing(rows)] == [2, 4]


def test_shared_startable_order_uses_tier_before_building_then_ladder():
    rows = [
        repo_project("nateprich/command-center", 1, "Ready", "Improve"),
        repo_ticket("nateprich/command-center", 2, 1),
        repo_project("nateprich/career-toolset", 3, "Building", "New"),
        repo_ticket("nateprich/career-toolset", 4, 3),
        repo_project("nateprich/career-toolset", 5, "Building", "Replace"),
        repo_ticket("nateprich/career-toolset", 6, 5),
        repo_project("nateprich/career-toolset", 7, "Ready", "New"),
        repo_ticket("nateprich/career-toolset", 8, 7),
    ]

    assert [item.number for item in startable(rows)] == [2, 4, 6, 8]


def test_shared_startable_order_keeps_building_commitment_ahead_of_ladder():
    rows = [
        project(1, "Building", "Replace"), ticket(2, 1),
        project(3, "Ready", "Investigate"), ticket(4, 3),
    ]

    assert [item.number for item in funnel.startable_listing(rows)] == [2, 4]


def test_shared_startable_order_keeps_ladder_ahead_of_unblock_count():
    investigate_project = project(1, "Building", "Investigate")
    investigate = ticket(2, 1)
    improve_project = project(3, "Building", "Improve")
    improve = ticket(4, 3)
    dependent_a = ticket(6, 5, open_blockers=[improve.ref])
    dependent_b = ticket(8, 7, open_blockers=[improve.ref])
    rows = [
        investigate_project, investigate,
        improve_project, improve,
        project(5, "Building", "New"), dependent_a,
        project(7, "Building", "New"), dependent_b,
    ]

    assert [item.number for item in funnel.startable_listing(rows)] == [2, 4]


def test_shared_startable_order_inherits_blocker_class_and_higher_tier():
    dependent_project = repo_project(
        "nateprich/command-center", 1, "Building", "Broken"
    )
    dependent = repo_ticket("nateprich/command-center", 2, 1)
    blocker_project = repo_project(
        "nateprich/hobby", 3, "Building", "New"
    )
    blocker = repo_ticket("nateprich/hobby", 4, 3)
    dependent.open_blockers = [blocker.ref]
    other_project = repo_project(
        "nateprich/career-toolset", 5, "Building", "Broken"
    )
    other = repo_ticket("nateprich/career-toolset", 6, 5)
    rows = [
        dependent_project, dependent, blocker_project, blocker,
        other_project, other,
    ]

    assert funnel.queue_classes(
        rows, funnel.dependency_descendants(rows)
    )[blocker.ref] == "Broken"
    assert [item.ref for item in startable(rows)] == [blocker.ref, other.ref]


def test_shared_startable_order_unblocks_more_then_uses_longest_wait():
    two_dependents_project = project(1, "Building", "Improve")
    two_dependents = ticket(2, 1, days=2)
    one_dependent_project = project(3, "Building", "Improve")
    one_dependent = ticket(4, 3, days=3)
    oldest_project = project(5, "Building", "Improve")
    oldest = ticket(6, 5, days=40)
    newest_project = project(7, "Building", "Improve")
    newest = ticket(8, 7, days=2)
    dependent_a_project = project(9, "Building", "Improve")
    dependent_a = ticket(
        10, 9, open_blockers=[two_dependents.ref],
    )
    dependent_b_project = project(11, "Building", "Improve")
    dependent_b = ticket(
        12, 11, open_blockers=[two_dependents.ref],
    )
    dependent_c_project = project(13, "Building", "Improve")
    dependent_c = ticket(
        14, 13, open_blockers=[one_dependent.ref],
    )
    rows = [
        two_dependents_project, two_dependents,
        one_dependent_project, one_dependent,
        oldest_project, oldest, newest_project, newest,
        dependent_a_project, dependent_a,
        dependent_b_project, dependent_b,
        dependent_c_project, dependent_c,
    ]

    assert [item.number for item in startable(rows)] == [2, 4, 6, 8]


def test_an_unpinned_queue_is_unchanged_by_the_pin_rule():
    rows = [
        project(1, "Building", "New"), ticket(2, 1),
        project(3, "Building", "Broken"), ticket(4, 3),
    ]
    assert [i.number for i in startable(rows)] == [4, 2]


def test_a_pin_does_not_preempt_the_wip_cap_for_an_unbounded_class():
    """A pin outranks the class ladder but does not grant a WIP exception."""
    pinned_project = project(1, "Ready", "Improve", days=1, pinned=True)
    pinned_ticket = ticket(2, 1)
    unpinned_project = project(3, "Ready", "New", days=1)
    unpinned_ticket = ticket(4, 3)
    rows = [
        pinned_project, pinned_ticket, unpinned_project, unpinned_ticket,
    ]
    in_flight = [project(n, "Building", "Improve") for n in (10, 20, 30, 40)]
    in_flight_tickets = [
        ticket(n + 1, n, in_motion_since=at(0.01)) for n in (10, 20, 30, 40)
    ]
    assert [i.ref for i in startable(rows)] == [
        pinned_ticket.ref, unpinned_ticket.ref,
    ]
    assert next_ticket(rows + in_flight + in_flight_tickets, at(0)) is None


def test_in_flight_work_finishes_before_anything_new_starts():
    """With both parents Building, this isolates the ladder within that state."""
    older = project(1, "Building", "Replace", days=30)
    in_flight = ticket(2, 1, days=30)
    newer = project(3, "Building", "New", days=1)
    fresh = ticket(4, 3, days=1)
    order = [i.number for i in startable([older, in_flight, newer, fresh])]
    assert order == [4, 2]  # ladder first: New outranks Replace


def test_a_ready_broken_ticket_preempts_in_flight_improve_work():
    """plan.md: "Broken and Maintenance preempt in-flight work — only classes
    that are finite may preempt." Nate, 2026-09-09: broken items in Ready move
    past improvement items in Building (#435)."""
    rows = [project(1, "Building", "Improve", days=30), ticket(2, 1, days=30),
            project(3, "Ready", "Broken", days=1), ticket(4, 3, days=1)]
    assert [i.number for i in startable(rows)] == [4, 2]


def test_in_flight_broken_work_still_finishes_before_a_ready_broken_start():
    """Among the finite classes, commitment still comes first."""
    rows = [project(1, "Building", "Broken", days=1), ticket(2, 1, days=1),
            project(3, "Ready", "Broken", days=30), ticket(4, 3, days=30)]
    assert [i.number for i in startable(rows)] == [2, 4]


def test_a_ready_improve_ticket_still_waits_behind_in_flight_improve_work():
    """plan.md's rejection stands: an unbounded class never preempts."""
    rows = [project(1, "Building", "Improve", days=1), ticket(2, 1, days=1),
            project(3, "Ready", "Improve", days=30), ticket(4, 3, days=30)]
    assert [i.number for i in startable(rows)] == [2, 4]


def test_maintenance_preempts_in_flight_ranking_like_broken():
    rows = [project(1, "Building", "New"), ticket(2, 1),
            project(3, "Ready", "Maintenance"), ticket(4, 3)]
    assert [i.number for i in startable(rows)] == [4, 2]


def test_new_never_preempts_in_flight_work():
    rows = [project(1, "Building", "Replace"), ticket(2, 1),
            project(3, "Ready", "New"), ticket(4, 3)]
    assert [i.number for i in startable(rows)] == [2, 4]


def test_a_blocker_of_a_broken_ticket_preempts_with_it():
    """The descendants rule carries preemption to whatever a Broken fix waits on."""
    rows = [project(1, "Building", "Improve", days=30), ticket(2, 1, days=30),
            project(3, "Ready", "Broken"), ticket(4, 3),
            project(5, "Ready", "New"), ticket(6, 5)]
    rows[3].open_blockers = [rows[5].ref]          # #4 (Broken) is blocked by #6 (New)
    order = [i.number for i in startable(rows)]
    assert order.index(6) < order.index(2)


def test_a_class_above_broken_on_the_ladder_does_not_preempt_by_position(monkeypatch):
    """Preemption is granted by name (plan.md: only finite classes), not by
    where a class sits on the ladder — so #130 landing Investigate first does
    not widen it unless Investigate is added to PREEMPTING_CLASSES."""
    monkeypatch.setattr(funnel, "LADDER", ["Investigate"] + funnel.LADDER)
    rows = [project(1, "Building", "Improve"), ticket(2, 1),
            project(3, "Ready", "Investigate"), ticket(4, 3)]
    assert [i.number for i in startable(rows)] == [2, 4]


# -- Bug: a latent defect, last on the ladder, never preempting (#1845) -----


def test_a_bug_never_outranks_startable_work_of_another_class():
    """#1832: Broken is what was seen to fail and preempts; Bug is latent and
    ranks below Replace. The Bug is the oldest work here, so neither age nor
    number can be what puts it last."""
    rows = []
    for n, klass in ((1, "Bug"), (2, "Replace"), (3, "Broken"), (4, "New"),
                     (5, "Maintenance"), (6, "Improve"), (7, "Investigate")):
        rows += [project(n, "Building", klass, days=30 - n),
                 ticket(10 + n, n, days=30 - n)]
    assert [i.number for i in startable(rows)] == [13, 15, 17, 16, 14, 12, 11]


def test_a_ready_bug_waits_behind_in_flight_work_that_ready_broken_passes():
    """Bug is unbounded, so it is not in PREEMPTING_CLASSES: the Building
    commitment holds against it, as against any other unbounded class."""
    rows = [project(1, "Building", "Replace", days=1), ticket(2, 1, days=1),
            project(3, "Ready", "Bug", days=30), ticket(4, 3, days=30),
            project(5, "Ready", "Broken", days=1), ticket(6, 5, days=1)]
    assert [i.number for i in startable(rows)] == [6, 2, 4]


def test_finite_work_outranks_a_bug_whatever_else_favours_it():
    """Pinned, tier 1 and already Building, a Bug still follows Ready Broken
    and Maintenance work in a hobby repo: only finite classes preempt."""
    rows = [
        tier_project(TOOLING, 1, "Building", "Bug", pinned=True),
        tier_ticket(TOOLING, 2, 1),
        tier_project(HOBBY, 3, "Ready", "Broken"), tier_ticket(HOBBY, 4, 3),
        tier_project(HOBBY, 5, "Ready", "Maintenance"),
        tier_ticket(HOBBY, 6, 5),
    ]
    assert [i.number for i in startable(rows)] == [4, 6, 2]


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


def test_ready_and_building_parents_are_both_startable():
    """plan.md: "Codex draws tickets from any `Ready` or `Building` parent".

    This asserted the opposite until #343. That assertion arrived in #287, the
    same commit that deleted the `start` gate — the only writer of `Building`.
    Requiring a stage while removing its sole producer left every `Ready`
    project unstartable and its accept gate unreachable, and the test passed
    because it exercised the half that survived.

    There is no coherent reading in which `Ready` stays unstartable: a claim
    cannot promote a ticket nothing will hand out, and nothing else writes the
    stage. `Building` is the record that work began, written by `cmd_claim`.
    """
    assert [i.number for i in startable(
        [project(1, "Ready", "New"), ticket(2, 1)])] == [2]
    assert [i.number for i in startable(
        [project(1, "Building", "New"), ticket(2, 1)])] == [2]


def test_an_unclassed_parent_is_not_startable():
    """plan.md: an unset Class is invalid and must not enter Building."""
    for status in ("Ready", "Building"):
        rows = [project(1, status, None), ticket(2, 1)]
        assert startable(rows) == [], status


def test_a_classed_parent_remains_startable():
    rows = [project(1, "Ready", "Improve"), ticket(2, 1)]

    assert [candidate.number for candidate in startable(rows)] == [2]


def test_a_ready_unclassed_no_proposal_parent_stays_with_nate():
    """The no-Proposed-class residual cannot hand its ticket to an agent."""
    parent = item(
        1,
        "Ready",
        None,
        body="# Plan\n\n## Needs you\n\nChoose a direction.\n",
        children_total=1,
    )
    child = ticket(2, 1)

    assert needs_class(parent)
    assert startable([parent, child]) == []
    assert parent.status == "Ready"


def test_startable_withholds_only_repos_missing_blocking_readiness():
    no_ci = "owner/no-ci"
    advisory = "owner/advisory"
    no_topic = "owner/no-topic"
    rows = [
        repo_project(no_ci, 1), repo_ticket(no_ci, 2, 1),
        repo_project(advisory, 3), repo_ticket(advisory, 4, 3),
        repo_project(no_topic, 5), repo_ticket(no_topic, 6, 5),
    ]
    readiness = {
        no_ci: funnel.MemberRepoReadiness(
            no_ci, topic=True, ci_workflow=False,
            stock_labels=("bug",), dependabot=False,
        ),
        advisory: funnel.MemberRepoReadiness(
            advisory, topic=True, ci_workflow=True,
            stock_labels=("bug",), dependabot=False,
        ),
        no_topic: funnel.MemberRepoReadiness(
            no_topic, topic=False, ci_workflow=True,
            stock_labels=(), dependabot=True,
        ),
    }

    assert [candidate.ref for candidate in startable(
        rows, repo_readiness=readiness
    )] == ["owner/advisory#4"]
    assert funnel.readiness_blockers(rows, repo_readiness=readiness) == [
        {"ref": "owner/no-ci#2", "repo": no_ci,
         "reasons": ["no CI workflow"]},
        {"ref": "owner/no-topic#6", "repo": no_topic,
         "reasons": ["missing command-center topic"]},
    ]


def test_shaped_work_is_not_startable():
    """Shaped has not been broken into tickets for Codex to work."""
    assert startable([project(1, "Shaped", "New"), ticket(2, 1)]) == []


def test_blocked_work_is_not_startable_at_either_level():
    assert startable([project(1, "Building", "New"), ticket(2, 1, labels=["blocked"])]) == []
    parent = project(3, "Building", "New", labels=["blocked"])
    assert startable([parent, ticket(4, 3)]) == []


def test_a_machine_local_ticket_is_startable_by_claude_only():
    rows = [
        project(1, "Building", "New"),
        ticket(2, 1, needs="claude-code-environment"),
    ]

    assert startable(rows, agent="codex") == []
    assert [candidate.number for candidate in startable(
        rows, agent="claude"
    )] == [2]


def test_a_human_step_ticket_is_not_startable_by_any_agent():
    rows = [
        project(1, "Building", "New"),
        ticket(2, 1, needs="human"),
    ]

    assert startable(rows, agent="codex") == []
    assert startable(rows, agent="claude") == []


def test_an_unmarked_ticket_is_startable_by_both_agents():
    rows = [
        project(1, "Building", "New"),
        ticket(2, 1, needs="none",
               body="Enter a value supplied through the environment."),
    ]

    assert [candidate.number for candidate in startable(
        rows, agent="codex"
    )] == [2]
    assert [candidate.number for candidate in startable(
        rows, agent="claude"
    )] == [2]


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


def test_a_claim_just_before_the_ttl_stays_live_with_a_ticket_branch():
    claimed_ticket = ticket(1, 9, in_motion_since=claimed(119))
    facts = {claimed_ticket.ref: {"branch_exists": True}}

    assert funnel.in_motion([claimed_ticket], NOW, pr_facts=facts) == [
        claimed_ticket
    ]
    assert stale_locks([claimed_ticket], NOW, pr_facts=facts) == []


def test_a_31_minute_claim_without_a_ticket_branch_is_stale():
    claimed_ticket = ticket(1, 9, in_motion_since=claimed(31))
    facts = {claimed_ticket.ref: None}

    assert funnel.in_motion([claimed_ticket], NOW, pr_facts=facts) == []
    assert stale_locks([claimed_ticket], NOW, pr_facts=facts) == [claimed_ticket]


def test_a_31_minute_claim_with_a_ticket_branch_is_live():
    claimed_ticket = ticket(1, 9, in_motion_since=claimed(31))
    facts = {claimed_ticket.ref: {"branch_exists": True}}

    assert funnel.in_motion([claimed_ticket], NOW, pr_facts=facts) == [
        claimed_ticket
    ]
    assert stale_locks([claimed_ticket], NOW, pr_facts=facts) == []


def test_a_20_minute_claim_without_a_ticket_branch_is_live():
    claimed_ticket = ticket(1, 9, in_motion_since=claimed(20))
    facts = {claimed_ticket.ref: None}

    assert funnel.in_motion([claimed_ticket], NOW, pr_facts=facts) == [
        claimed_ticket
    ]
    assert stale_locks([claimed_ticket], NOW, pr_facts=facts) == []


def test_the_two_hour_ttl_stays_stale_even_with_a_ticket_branch():
    claimed_ticket = ticket(1, 9, in_motion_since=claimed(120))
    facts = {claimed_ticket.ref: {"branch_exists": True}}

    assert funnel.in_motion([claimed_ticket], NOW, pr_facts=facts) == []
    assert stale_locks([claimed_ticket], NOW, pr_facts=facts) == [claimed_ticket]


def test_the_five_ghost_claims_replay_as_five_takeovers():
    claims = [
        ticket(number, 9, in_motion_since=claimed(31 + offset))
        for offset, number in enumerate((221, 214, 223, 224, 301))
    ]
    facts = {item.ref: None for item in claims}

    assert {item.number for item in stale_locks(claims, NOW, pr_facts=facts)} == {
        221, 214, 223, 224, 301,
    }
    assert funnel.in_motion(claims, NOW, pr_facts=facts) == []


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
    assert len(funnel.in_motion(rows, NOW)) == funnel.WIP_LIMIT
    assert funnel.stale_locks(rows, NOW) == []
    assert next_ticket(rows, NOW).number == 4


def test_shared_listing_preserves_finite_order_and_investigate_position():
    rows = [
        project(1, "Building", "Broken"), ticket(2, 1),
        project(3, "Building", "Maintenance"), ticket(4, 3),
        project(5, "Building", "Investigate"), ticket(6, 5),
        project(7, "Building", "Improve"), ticket(8, 7),
        project(9, "Building", "New"), ticket(10, 9),
        project(11, "Building", "Replace"), ticket(12, 11),
    ]

    assert [item.number for item in funnel.startable_listing(rows)] == [
        2, 4, 6, 8, 10, 12,
    ]


@_pytest.mark.parametrize(
    ("klass", "should_start"),
    [("Broken", True), ("Maintenance", False), ("Investigate", False)],
)
def test_shared_order_keeps_the_wip_exception_broken_only(klass, should_start):
    rows = _at_limit([project(3, "Building", klass), ticket(4, 3)])
    shared_order = funnel.startable_listing(rows)

    selected = next_ticket(rows, NOW, startable_order=shared_order)

    assert (selected is not None) is should_start


def test_maintenance_does_not_preempt_the_limit():
    """Maintenance may preempt in-flight *ranking*, but not exceed the cap."""
    rows = _at_limit([project(3, "Building", "Maintenance"), ticket(4, 3)])
    assert next_ticket(rows, NOW) is None


def test_investigate_does_not_preempt_the_limit():
    rows = _at_limit([project(3, "Building", "Investigate"), ticket(4, 3)])
    assert next_ticket(rows, NOW) is None


def test_a_bug_waits_for_a_free_slot_and_then_takes_it():
    """Only Broken may exceed the cap (#1845 keeps Bug out of it)."""
    rows = _at_limit([project(3, "Building", "Bug"), ticket(4, 3)])
    assert next_ticket(rows, NOW) is None
    one_slot_free = rows[2:]
    assert getattr(next_ticket(one_slot_free, NOW), "number", None) == 4


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
    garbled = ticket(
        2, 1, in_motion_since=funnel.parse_time("hand edited"),
    )
    assert funnel.in_motion([garbled], NOW) == []
    assert lock_holder([garbled], NOW) is None


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


def test_next_releases_the_claim_held_on_a_declined_ref(monkeypatch, capsys):
    """A declined ticket's claim is released by the decline itself (#395)."""
    released = []
    monkeypatch.setattr(funnel, "write_lock",
                        lambda item, value: released.append((item.ref, value)))
    rows = [
        project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(60)),
        project(3, "Building", "New"), ticket(4, 3),
    ]

    assert funnel.cmd_next(rows, NOW, excluded={rows[1].ref}) == 0

    assert released == [(rows[1].ref, "")]
    assert rows[1].in_motion_since is None
    assert "released {} (declined)".format(rows[1].ref) in capsys.readouterr().err
    assert funnel.in_motion(rows, NOW) == []


def test_next_excludes_a_declined_ticket_given_as_a_bare_number(monkeypatch, capsys):
    """#436: the routine may pass the number it copied; it must still exclude."""
    released = []
    monkeypatch.setattr(funnel, "write_lock",
                        lambda item, value: released.append((item.ref, value)))
    rows = [
        project(1, "Building", "New"), ticket(2, 1, in_motion_since=claimed(60)),
        project(3, "Building", "New"), ticket(4, 3),
    ]

    assert funnel.cmd_next(rows, NOW, excluded={"2"}) == 0

    assert '"ref": "nateprich/beta#4"' in capsys.readouterr().out
    assert released == [(rows[1].ref, "")]


def test_next_leaves_an_unclaimed_declined_ref_alone(monkeypatch, capsys):
    released = []
    monkeypatch.setattr(funnel, "write_lock",
                        lambda item, value: released.append((item.ref, value)))
    rows = [project(1, "Building", "New"), ticket(2, 1),
            project(3, "Building", "New"), ticket(4, 3)]

    assert funnel.cmd_next(rows, NOW, excluded={rows[1].ref}) == 0

    assert released == []
    capsys.readouterr()


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
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: {})
    monkeypatch.setattr(
        funnel,
        "repo_readiness_for_items",
        lambda items: {
            "nateprich/beta": funnel.MemberRepoReadiness(
                "nateprich/beta", topic=True, ci_workflow=True,
                stock_labels=(), dependabot=True,
            ),
        },
    )

    assert funnel.main([
        "next", "--not", rows[1].ref, "--not", rows[3].ref,
    ]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == rows[5].ref


def test_next_cli_filters_machine_local_work_by_requesting_agent(
    monkeypatch, capsys
):
    rows = [
        project(1, "Building", "New"),
        ticket(2, 1, needs="claude-code-environment"),
    ]
    monkeypatch.setattr(funnel, "load_items", lambda: rows)
    monkeypatch.setattr(funnel, "awaiting_review", lambda items: set())
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: {})
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: rows[1].body)
    monkeypatch.setattr(
        funnel,
        "repo_readiness_for_items",
        lambda items: {
            rows[0].repo: funnel.MemberRepoReadiness(
                rows[0].repo, topic=True, ci_workflow=True,
                stock_labels=(), dependabot=True,
            ),
        },
    )

    assert funnel.main(["next", "--agent", "codex"]) == 1
    capsys.readouterr()

    assert funnel.main(["next", "--agent", "claude"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == rows[1].ref


# -- The Bug share: one start in four, counted over eight (#1846) ------------


def _share_board(bug_repo):
    """Five single-ticket Bug projects and sixteen Improve ones, all Ready.

    The Improve work is in a hobby repo. With the Bugs there too the ladder
    puts every Bug last; with them in the tooling tier, repo tier puts every
    Bug first. Either way only the share can space them.
    """
    rows = []
    for k in range(5):
        rows += [tier_project(bug_repo, 100 + k, "Ready", "Bug"),
                 tier_ticket(bug_repo, 200 + k, 100 + k)]
    for k in range(16):
        rows += [tier_project(HOBBY, 300 + k, "Ready", "Improve"),
                 tier_ticket(HOBBY, 400 + k, 300 + k)]
    return rows


def _started(rows, starts, chosen, lane="codex"):
    """Record a start the way begin's binding does, with its class."""
    starts.append({
        "run": "run-{}".format(len(starts)), "agent": lane, "phase": "bind",
        "ts": 1_000 + len(starts), "do": "ticket", "work": chosen.ref,
        "class": effective_class(chosen, {row.ref: row for row in rows}),
    })
    return starts[-1]["class"]


def _one_lane_pulls(rows, starts, pulls):
    """A lane asks, starts what it is given, and finishes it before asking
    again, so no Bug is ever in flight when it asks."""
    classes = []
    for _ in range(pulls):
        chosen = next_ticket(
            rows, NOW, recent_starts=funnel.recent_ticket_starts(starts))
        classes.append(_started(rows, starts, chosen))
        chosen.state = "CLOSED"
    return classes


@_pytest.mark.parametrize("bug_repo", [HOBBY, TOOLING])
@_pytest.mark.parametrize("prior", [0, 8])
def test_a_single_lane_gets_one_bug_in_every_four_starts(bug_repo, prior):
    """Nate's worry, 2026-09-28: a lane that starts up, sees no Bug running
    and grabs one. This lane never has a Bug running, from an empty history
    or one of eight Improve starts, and still gets three Bugs in twelve."""
    rows = _share_board(bug_repo)
    starts = []
    for k in range(prior):
        starts.append({"run": "old-{}".format(k), "agent": "codex",
                       "phase": "bind", "ts": k, "do": "ticket",
                       "work": "{}#9{}".format(HOBBY, k), "class": "Improve"})

    assert _one_lane_pulls(rows, starts, 12) == (
        ["Bug", "Improve", "Improve", "Improve"] * 3)


def test_a_bug_just_started_makes_the_next_three_starts_something_else():
    rows = _share_board(TOOLING)
    starts = []
    _started(rows, starts, rows[1])
    rows[1].state = "CLOSED"

    assert _one_lane_pulls(rows, starts, 8) == (
        ["Improve", "Improve", "Improve", "Bug"] * 2)


def test_observed_broken_and_maintenance_still_go_first_on_the_bugs_turn():
    """Finite work leads, and its starts do not use up the Bugs' turn: the
    Bug, which the ladder alone puts last, goes straight after it."""
    rows = [
        project(1, "Ready", "Bug"), ticket(2, 1),
        project(3, "Ready", "Improve"), ticket(4, 3),
        project(5, "Ready", "Maintenance"), ticket(6, 5),
        project(7, "Ready", "Broken"), ticket(8, 7),
    ]
    assert funnel.bug_share_due([])
    starts, order = [], []
    for _ in range(4):
        chosen = next_ticket(
            rows, NOW, recent_starts=funnel.recent_ticket_starts(starts))
        _started(rows, starts, chosen)
        order.append(chosen.number)
        chosen.state = "CLOSED"

    assert order == [8, 6, 2, 4]

    favoured = [
        tier_project(TOOLING, 11, "Building", "Bug", pinned=True),
        tier_ticket(TOOLING, 12, 11),
        tier_project(HOBBY, 13, "Ready", "Broken"),
        tier_ticket(HOBBY, 14, 13),
    ]
    assert next_ticket(favoured, NOW, recent_starts=[]).number == 14


def test_a_bug_fills_a_pull_that_nothing_else_can():
    """Off its turn a Bug waits for other work, but never leaves a lane idle."""
    rows = [
        project(1, "Ready", "Bug"), ticket(2, 1),
        project(3, "Ready", "Improve"),
        ticket(4, 3, in_motion_since=claimed(5)),
    ]
    just_started = ["Bug"]
    assert not funnel.bug_share_due(just_started)

    assert next_ticket(rows, NOW, recent_starts=just_started).number == 2

    rows[3].in_motion_since = None
    assert next_ticket(rows, NOW, recent_starts=just_started).number == 4


def test_four_lanes_pulling_together_do_not_exceed_the_share():
    """Each lane's starts are in its own heartbeat. The count reads them all
    through the same rows the backoff reads, so four lanes share one quarter
    rather than taking a quarter each."""
    import heartbeat

    rows = _share_board(TOOLING)
    by_ref = {row.ref: row for row in rows}
    lanes = ["claude", "codex", "muse", "zcode"]
    running = {}
    classes = []
    for pull in range(16):
        lane = lanes[pull % len(lanes)]
        if lane in running:
            finished = running.pop(lane)
            finished.state, finished.in_motion_since = "CLOSED", None
        history = funnel.recent_ticket_starts(funnel._backoff_rows())
        chosen = next_ticket(rows, NOW, recent_starts=history)
        chosen.in_motion_since = NOW
        running[lane] = chosen
        klass = effective_class(chosen, by_ref)
        heartbeat._spool(lane, {
            "run": "{}-{}".format(lane, pull), "agent": lane, "phase": "bind",
            "ts": 1_000 + pull, "do": "ticket", "work": chosen.ref,
            "class": klass,
        })
        classes.append(klass)

    assert classes == ["Bug", "Improve", "Improve", "Improve"] * 4
    assert all(classes[k:k + 8].count("Bug") <= 2 for k in range(9))


def test_an_empty_or_short_history_counts_what_exists():
    rows = [project(1, "Ready", "Bug"), ticket(2, 1),
            project(3, "Ready", "Improve"), ticket(4, 3)]

    assert funnel.recent_ticket_starts([]) == []
    assert next_ticket(rows, NOW, recent_starts=[]).number == 2
    assert next_ticket(rows, NOW).number == 2
    assert next_ticket(
        rows, NOW, recent_starts=["Improve", "Bug", "Improve"]).number == 4
    assert next_ticket(
        rows, NOW, recent_starts=["Bug", "Improve", "Improve", "Improve"],
    ).number == 2


def test_two_bugs_together_hold_the_next_until_one_leaves_the_last_eight():
    """Counting the start being decided: two lanes that took Bugs at once
    leave room for the next only when the window of eight holds one of
    them. Worked by hand: B, B and five others is three Bugs in eight."""
    assert [
        funnel.bug_share_due(["Bug", "Bug"] + ["Improve"] * others)
        for others in range(8)
    ] == [False] * 6 + [True] * 2


def test_a_building_or_pinned_bug_keeps_its_place_off_the_bugs_turn():
    """Both are commitments: the share counted the Building project's first
    start, and a pin is Nate's ordering call (#673). A Ready Bug that ranks
    first only by repo tier waits."""
    just_started = ["Bug"]
    ready_improve = [tier_project(HOBBY, 3, "Ready", "Improve"),
                     tier_ticket(HOBBY, 4, 3)]

    building = [tier_project(TOOLING, 1, "Building", "Bug"),
                tier_ticket(TOOLING, 2, 1)]
    assert next_ticket(building + ready_improve, NOW,
                       recent_starts=just_started).number == 2

    pinned = [tier_project(HOBBY, 5, "Ready", "Bug", pinned=True),
              tier_ticket(HOBBY, 6, 5)]
    assert next_ticket(pinned + ready_improve, NOW,
                       recent_starts=just_started).number == 6

    by_tier = [tier_project(TOOLING, 7, "Ready", "Bug"),
               tier_ticket(TOOLING, 8, 7)]
    assert [i.number for i in startable(by_tier + ready_improve)] == [8, 4]
    assert next_ticket(by_tier + ready_improve, NOW,
                       recent_starts=just_started).number == 4


def test_a_bug_ticket_that_blocks_improve_work_goes_with_that_work():
    """It ranks as Improve, so the share does not hold it back: holding it
    would leave the Improve ticket waiting on its own prerequisite."""
    rows = [
        project(1, "Ready", "Bug"), ticket(2, 1),
        project(3, "Ready", "Improve"),
        ticket(4, 3, open_blockers=["nateprich/beta#2"]),
        project(5, "Ready", "Improve"), ticket(6, 5),
    ]
    assert next_ticket(rows, NOW, recent_starts=["Bug"]).number == 2


def test_the_bugs_turn_goes_to_the_highest_tier_bug_then_the_oldest():
    """Nate, 2026-09-28: "Tier, then oldest" (#1877). Worked by hand: the
    tooling Bugs first, #60 before #4 as it is older, then the tier-2 Bug,
    then the hobby Bug, though that one is the oldest of all. Age alone
    would give 2, 8, 60, 4, and ``startable()``'s order 4, 60, 8, 2."""
    rows = [
        tier_project(HOBBY, 1, "Ready", "Bug"),
        tier_ticket(HOBBY, 2, 1, created_at=at(40)),
        tier_project(TOOLING, 3, "Ready", "Bug"),
        tier_ticket(TOOLING, 4, 3, created_at=at(5)),
        tier_project(TOOLING, 5, "Ready", "Bug"),
        tier_ticket(TOOLING, 60, 5, created_at=at(20)),
        tier_project(IMPACT, 7, "Ready", "Bug"),
        tier_ticket(IMPACT, 8, 7, created_at=at(30)),
        tier_project(HOBBY, 9, "Ready", "Improve"), tier_ticket(HOBBY, 10, 9),
    ]
    assert [i.number for i in startable(rows)] == [4, 60, 8, 10, 2]
    order = []
    for _ in range(4):
        chosen = next_ticket(rows, NOW, recent_starts=[])
        order.append(chosen.number)
        chosen.state = "CLOSED"

    assert order == [60, 4, 8, 2]


def test_a_bug_ticket_blocking_a_tier_1_bug_takes_that_tier_on_the_bugs_turn():
    """The tier is ``startable()``'s: a hobby Bug ticket that a tooling Bug
    waits on ranks in tier 1, so it goes before a newer tooling Bug, and
    both go before an older hobby Bug that blocks nothing."""
    rows = [
        tier_project(HOBBY, 1, "Ready", "Bug"),
        tier_ticket(HOBBY, 2, 1, created_at=at(20)),
        tier_project(TOOLING, 3, "Ready", "Bug"),
        tier_ticket(TOOLING, 4, 3, created_at=at(1),
                    open_blockers=[HOBBY + "#2"]),
        tier_project(TOOLING, 5, "Ready", "Bug"),
        tier_ticket(TOOLING, 6, 5, created_at=at(5)),
        tier_project(HOBBY, 7, "Ready", "Bug"),
        tier_ticket(HOBBY, 8, 7, created_at=at(40)),
    ]
    assert next_ticket(rows, NOW, recent_starts=[]).number == 2


def test_a_bug_filling_an_idle_pull_is_also_chosen_by_tier_then_age():
    """Off the Bugs' turn with nothing else startable, the Bug that fills
    the pull is the one the Bugs' turn would take."""
    rows = [
        tier_project(HOBBY, 1, "Ready", "Bug"),
        tier_ticket(HOBBY, 2, 1, created_at=at(40)),
        tier_project(TOOLING, 3, "Ready", "Bug"),
        tier_ticket(TOOLING, 4, 3, created_at=at(5)),
    ]
    assert not funnel.bug_share_due(["Bug"])
    assert next_ticket(rows, NOW, recent_starts=["Bug"]).number == 4


def test_pinned_work_goes_before_the_bugs_turn():
    """Nate, 2026-09-28: "Pins win" (#1877). The pinned hobby Improve goes
    before a tier-1 Bug that has waited a month, while the Bugs' turn is
    due; its starts are not Bugs, so the turn is still due once the pinned
    project has nothing left to start."""
    rows = [
        tier_project(HOBBY, 1, "Ready", "Improve", pinned=True),
        tier_ticket(HOBBY, 2, 1), tier_ticket(HOBBY, 3, 1),
        tier_project(TOOLING, 4, "Ready", "Bug"),
        tier_ticket(TOOLING, 5, 4, created_at=at(30)),
        tier_project(TOOLING, 6, "Ready", "Improve"),
        tier_ticket(TOOLING, 7, 6),
    ]
    assert funnel.bug_share_due([])
    starts, order = [], []
    for _ in range(4):
        chosen = next_ticket(
            rows, NOW, recent_starts=funnel.recent_ticket_starts(starts))
        _started(rows, starts, chosen)
        order.append(chosen.number)
        chosen.state = "CLOSED"

    assert order == [2, 3, 5, 7]


def test_a_pinned_bug_takes_the_bugs_turn_before_a_higher_tier_one():
    """A pin is pinned work whatever its class: on the Bugs' turn the pinned
    hobby Bug goes, not the older tier-1 Bug. Taking the tier-1 Bug instead
    would start the pinned one on the very next pull, which it keeps its
    place for, and put two Bugs together."""
    rows = [
        tier_project(HOBBY, 1, "Ready", "Bug", pinned=True),
        tier_ticket(HOBBY, 2, 1, created_at=at(1)),
        tier_project(TOOLING, 3, "Ready", "Bug"),
        tier_ticket(TOOLING, 4, 3, created_at=at(30)),
        tier_project(HOBBY, 5, "Ready", "Improve"), tier_ticket(HOBBY, 6, 5),
    ]
    assert next_ticket(rows, NOW, recent_starts=[]).number == 2


def test_the_start_history_reads_ticket_bindings_from_every_lane_in_order():
    def bind(run, agent, ts, work, klass=None, do="ticket"):
        row = {"run": run, "agent": agent, "phase": "bind", "ts": ts,
               "do": do, "work": work}
        if klass:
            row["class"] = klass
        return row

    rows = [
        bind("a", "codex", 5, "o/r#1", "Bug"),
        bind("b", "claude", 3, "o/r#2", "Improve"),
        bind("c", "muse", 4, "96", do="review"),
        {"run": "a", "agent": "codex", "phase": "start", "ts": 1},
        # Bound before #1846 recorded a class: not a Bug, as none existed.
        bind("d", "codex", 6, "o/r#3"),
        # A run bound twice is one start, its latest, and in that place.
        bind("e", "codex", 2, "o/r#4", "Bug"),
        bind("e", "codex", 8, "o/r#5", "New"),
    ]
    assert funnel.recent_ticket_starts(rows) == ["Improve", "Bug", None, "New"]

    many = [bind("r{}".format(k), "codex", k, "o/r#{}".format(k), "Improve")
            for k in range(10)]
    many[1]["class"] = "Bug"
    many[2]["class"] = "Bug"
    assert funnel.recent_ticket_starts(many) == ["Bug"] + ["Improve"] * 7


def test_next_cli_counts_another_lanes_bug_start_from_the_heartbeat(
    monkeypatch, capsys
):
    """`funnel next` reads the history from the rows it reads the backoff
    from: a Bug the Saturday lane just started is this pull's reason to
    take Improve work over a Bug that repo tier would put first."""
    import heartbeat

    heartbeat._spool("claude", {
        "run": "sat", "agent": "claude", "phase": "bind", "ts": 5,
        "do": "ticket", "work": TOOLING + "#90", "class": "Bug",
    })
    rows = [tier_project(TOOLING, 1, "Ready", "Bug"),
            tier_ticket(TOOLING, 2, 1),
            tier_project(HOBBY, 3, "Ready", "Improve"),
            tier_ticket(HOBBY, 4, 3)]
    monkeypatch.setattr(funnel, "load_items", lambda: rows)
    monkeypatch.setattr(funnel, "awaiting_review", lambda items: set())
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: {})
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: "")
    monkeypatch.setattr(
        funnel,
        "repo_readiness_for_items",
        lambda items: {
            repo: funnel.MemberRepoReadiness(
                repo, topic=True, ci_workflow=True,
                stock_labels=(), dependabot=True,
            )
            for repo in (TOOLING, HOBBY)
        },
    )

    assert funnel.main(["next"]) == 0
    assert json.loads(capsys.readouterr().out)["ref"] == rows[3].ref


# -- A board with no Bug pulls exactly as it did before #1877 ---------------

BUG_FREE_PICKS = FIXTURE.parent / "bug_free_next_ticket.json"


def _bug_free_board(seed):
    """A seeded board with no Bug on it, and the lane and history pulling it.

    Everything that feeds the pull is varied: pins, all three repo tiers,
    Ready and Building projects of every other class, claims up to and past
    the WIP limit, blockers across repositories (which carry tier and class
    to their prerequisite), machine-local tickets, and a start history that
    is often the Bugs' turn. Only a Bug is missing.
    """
    rng = random.Random(seed)
    repos = [TOOLING, "nateprich-projects/workbench", IMPACT, HOBBY,
             "nateprich-projects/AFL"]
    classes = [klass for klass in funnel.LADDER if klass != "Bug"]
    projects, tickets = [], []
    number = 1
    for _ in range(rng.randint(1, 6)):
        repo, parent = rng.choice(repos), number
        projects.append(tier_project(
            repo, parent, rng.choice(["Ready", "Building"]),
            rng.choice(classes), days=rng.randint(0, 30),
            pinned=rng.random() < 0.2))
        number += 1
        for _ in range(rng.randint(1, 3)):
            tickets.append(tier_ticket(
                repo, number, parent, days=rng.randint(0, 30),
                created_at=at(rng.randint(0, 60)),
                in_motion_since=(claimed(rng.randint(1, 50))
                                 if rng.random() < 0.25 else None),
                needs=rng.choice(["none"] * 5 + ["claude-code-environment"]),
            ))
            number += 1
    for dependent in tickets:
        blocker = rng.choice(tickets)
        if rng.random() < 0.2 and blocker is not dependent:
            dependent.open_blockers = [blocker.ref]
    history = [rng.choice(["Bug", "Improve", "New", "Broken", None])
               for _ in range(rng.randint(0, 8))]
    # #794 closed keeps the freeze inert. These tickets have no body, so it
    # would withhold nothing, and an open freeze parses engine/review.py
    # once for every ticket it checks.
    owner_repo, owner_number = funnel.FREEZE_OWNER_REF.split("#")
    freeze_owner = item(int(owner_number), "Done", "Improve",
                        repo=owner_repo, state="CLOSED")
    return (projects + tickets + [freeze_owner], history,
            rng.choice(["codex", "claude"]))


def _bug_free_picks(boards):
    """The ticket number each seeded board's pull takes, or None."""
    picks = []
    for seed in range(boards):
        rows, history, agent = _bug_free_board(seed)
        assert all(row.klass != "Bug" for row in rows)
        chosen = next_ticket(rows, NOW, agent=agent, recent_starts=history)
        picks.append(chosen.number if chosen else None)
    return picks


def test_a_bug_free_board_pulls_exactly_as_it_did_before_the_bug_turn_order():
    """#1877 changes only what a pull does with Bugs on the board. The picks
    were recorded from origin/main at b2c619a60, before #1877, and each
    board is pulled again now. A deliberate change to the ordinary order
    re-records them with ``_bug_free_picks`` and says why in its PR."""
    recorded = json.loads(BUG_FREE_PICKS.read_text())["picks"]
    assert _bug_free_picks(len(recorded)) == recorded


# -- The projected order runs the Bug share forward (#1878) ------------------
#
# The dashboard and ``funnel queue`` show ``projected_pull_order``. Each of
# its turns is the pick ``next_ticket`` makes, so a Bug shows where begin
# takes it, not where ``startable()`` alone would rank it.


def _readiness_bug_board():
    """An ineligible tier-1 Bug ahead of a startable tier-3 Bug."""
    rows = [
        tier_project(TOOLING, 1, "Building", "Bug", days=12),
        tier_ticket(TOOLING, 2, 1, days=12),
        tier_project(HOBBY, 3, "Building", "Bug", days=10),
        tier_ticket(HOBBY, 4, 3, days=10),
    ]
    for parent, child in ((5, 6), (7, 8), (9, 10)):
        rows += [
            tier_project(HOBBY, parent, "Building", "Improve"),
            tier_ticket(HOBBY, child, parent),
        ]
    readiness = {
        TOOLING: funnel.MemberRepoReadiness(
            TOOLING, topic=True, ci_workflow=False,
            stock_labels=(), dependabot=False,
        ),
        HOBBY: funnel.MemberRepoReadiness(
            HOBBY, topic=True, ci_workflow=True,
            stock_labels=(), dependabot=False,
        ),
    }
    return rows, readiness


def test_projection_gives_no_turn_or_start_to_repo_readiness_withheld_work():
    rows, readiness = _readiness_bug_board()

    # The projection must not count the readiness-withheld tier-1 Bug as a
    # start; one-in-four and then three Improvements give this hand-worked order.
    assert funnel.projected_pull_order(
        rows, NOW, recent_starts=[], repo_readiness=readiness
    ) == [
        HOBBY + "#4", HOBBY + "#6", HOBBY + "#8", HOBBY + "#10",
    ]


def test_begin_still_withholds_repo_readiness_blocked_tickets():
    rows, readiness = _readiness_bug_board()

    assert next_ticket(
        rows, NOW, repo_readiness=readiness, recent_starts=[]
    ).ref == HOBBY + "#4"
    assert funnel.readiness_blockers(rows, repo_readiness=readiness) == [
        {"ref": TOOLING + "#2", "repo": TOOLING,
         "reasons": ["no CI workflow"]},
    ]


def test_queue_lists_the_readiness_bug_on_the_turn_begin_takes_it(
    monkeypatch, capsys
):
    rows, readiness = _readiness_bug_board()
    monkeypatch.setattr(funnel, "finished_by_comments_runs", lambda _items: set())
    monkeypatch.setattr(funnel, "_backoff_rows", lambda: [])
    monkeypatch.setattr(
        funnel, "_backed_off_work", lambda _items, _now, rows=None: {}
    )
    monkeypatch.setattr(funnel, "recent_ticket_starts", lambda _rows: [])

    assert funnel.cmd_queue(
        rows, NOW, repo_readiness=readiness, pr_facts={}
    ) == 0
    listed = capsys.readouterr().out.split("Startable by Codex", 1)[1]

    assert [
        int(line.split(HOBBY + "#", 1)[1].split()[0])
        for line in listed.splitlines() if HOBBY + "#" in line
    ] == [4, 6, 8, 10]


def _successive_starts(rows, history):
    """What one lane starts, pull after pull, found without the projection.

    The lane asks ``next_ticket``, starts what it is given and finishes it
    before asking again. A start moves a Ready project to Building, as
    ``claim`` does, and a finished ticket stops blocking, as on GitHub.
    Returns the refs started and how many were not ``startable()``'s first.
    """
    rows = [copy.copy(row) for row in rows]
    by_ref = {row.ref: row for row in rows}
    history, starts, moved = list(history), [], 0
    while True:
        chosen = next_ticket(rows, NOW, recent_starts=history)
        if chosen is None:
            return starts, moved
        moved += chosen is not startable(rows)[0]
        starts.append(chosen.ref)
        history.append(effective_class(chosen, by_ref))
        chosen.state = "CLOSED"
        parent = by_ref[chosen.parent]
        if parent.status == "Ready":
            parent.status = "Building"
        for row in rows:
            row.open_blockers = [
                ref for ref in row.open_blockers if ref != chosen.ref]


def _bug_board(seed):
    """A seeded board with Bugs on it, and the start history before it.

    Pins, all three repo tiers, Ready and Building projects of every class
    with Bug the most common, up to three tickets to a project, and blockers
    between tickets, which carry class and tier to their prerequisite.
    Nothing is under way, so every turn is a start begin makes.
    """
    rng = random.Random(seed)
    classes = list(funnel.LADDER) + ["Bug"] * 4
    projects, tickets = [], []
    number = 1
    for _ in range(rng.randint(2, 7)):
        repo, parent, count = (
            rng.choice([TOOLING, IMPACT, HOBBY]), number, rng.randint(1, 3))
        projects.append(tier_project(
            repo, parent, rng.choice(["Ready", "Building"]),
            rng.choice(classes), days=rng.randint(0, 30),
            pinned=rng.random() < 0.1))
        projects[-1].children_total = count
        number += 1
        for _ in range(count):
            tickets.append(tier_ticket(
                repo, number, parent, days=rng.randint(0, 30),
                created_at=at(rng.randint(0, 60))))
            number += 1
    for dependent in tickets:
        blocker = rng.choice(tickets)
        if rng.random() < 0.15 and blocker is not dependent:
            dependent.open_blockers = [blocker.ref]
    history = [rng.choice(["Bug", "Improve", "New", None])
               for _ in range(rng.randint(0, 8))]
    # #794 closed keeps the freeze inert, as on ``_bug_free_board``.
    owner_repo, owner_number = funnel.FREEZE_OWNER_REF.split("#")
    freeze_owner = item(int(owner_number), "Done", "Improve",
                        repo=owner_repo, state="CLOSED")
    return projects + tickets + [freeze_owner], history


def test_the_projected_order_is_the_starts_next_ticket_makes_in_turn():
    """#1878's Accept: from a known start history, the projection is the
    sequence of ``next_ticket`` picks, each start joining the history as
    begin's binding adds it. On most of these boards the share moves a Bug
    off ``startable()``'s order at least once."""
    moved_boards = 0
    for seed in range(300):
        rows, history = _bug_board(seed)
        starts, moved = _successive_starts(rows, history)
        assert funnel.projected_pull_order(
            rows, NOW, recent_starts=history) == starts, seed
        moved_boards += moved > 0

    assert moved_boards >= 150


@_pytest.mark.parametrize("bug_repo, history, expected", [
    # #1878's report: a hobby Bug, which the ladder puts last, is taken on
    # every fourth start. From an empty history: a Bug, three Improve, five
    # times over, then the last Improve.
    (HOBBY, [], [200, 400, 401, 402, 201, 403, 404, 405, 202, 406, 407, 408,
                 203, 409, 410, 411, 204, 412, 413, 414, 415]),
    # And a tier-1 Bug, which repo tier puts first, waits three starts after
    # a Bug has just started, then every fourth.
    (TOOLING, ["Bug"], [400, 401, 402, 200, 403, 404, 405, 201, 406, 407,
                        408, 202, 409, 410, 411, 203, 412, 413, 414, 204,
                        415]),
])
def test_bugs_show_on_the_turns_begin_takes_them(bug_repo, history, expected):
    """Worked by hand from one in four. ``startable()``, which the board ran
    forward before #1878, puts all five Bugs together at one end."""
    rows = _share_board(bug_repo)
    ordinary = [i.number for i in startable(rows)]
    together = ordinary[:5] if bug_repo == TOOLING else ordinary[-5:]
    assert together == [200, 201, 202, 203, 204]

    assert [
        int(ref.rsplit("#", 1)[1])
        for ref in funnel.projected_pull_order(
            rows, NOW, recent_starts=history)
    ] == expected


@_pytest.mark.parametrize("needs", ["human", "claude-code-environment"])
def test_unclaimable_ready_step_does_not_commit_sibling_bugs_off_turn(needs):
    """Finishing an underway step releases Improve work, not Bug commitment."""
    ready_bug = project(1, "Ready", "Bug", children=3)
    unclaimable = ticket(2, 1, needs=needs)
    bug_siblings = [ticket(3, 1), ticket(4, 1)]
    newly_unblocked = [
        (project(parent, "Ready", "Improve"),
         ticket(number, parent, open_blockers=[unclaimable.ref]))
        for parent, number in ((10, 11), (12, 13), (14, 15))
    ]
    rows = [ready_bug, unclaimable, *bug_siblings]
    for parent, child in newly_unblocked:
        rows.extend((parent, child))

    projected = funnel.projected_pull_order(
        rows, NOW, recent_starts=["Bug"]
    )
    assert [int(ref.rsplit("#", 1)[1]) for ref in projected] == [
        2, 11, 13, 15, 3, 4,
    ]

    begin_rows = [copy.copy(row) for row in rows]
    begin_by_ref = {row.ref: row for row in begin_rows}
    begin_by_ref[unclaimable.ref].state = "CLOSED"
    begin_by_ref[ready_bug.ref].children_done = 1
    for row in begin_rows:
        row.open_blockers = [
            ref for ref in row.open_blockers if ref != unclaimable.ref
        ]
    begin_picks, _moved = _successive_starts(begin_rows, ["Bug"])
    assert [int(ref.rsplit("#", 1)[1]) for ref in begin_picks] == [
        11, 13, 15, 3, 4,
    ]
    assert [ref for ref in projected if ref != unclaimable.ref] == begin_picks


@_pytest.mark.parametrize("under_way", ["claimed", "in review", "human"])
def test_a_turn_on_work_already_under_way_is_not_a_new_start(under_way):
    """A claimed Bug's start is in the history already: begin wrote it when
    it issued the ticket, and so for one whose PR is in review. A human step
    is never begin's to issue. Such a turn shows the work being taken now,
    and the next Bug turn still comes three starts after the history's Bug.
    Worked by hand: #2 under way, the Improve #4, #6 and #8, the hobby Bug
    #10 on its turn, then #12. Counting #2 again would put #10 last."""
    running = tier_ticket(TOOLING, 2, 1)
    in_review = []
    if under_way == "claimed":
        running.in_motion_since = claimed(20)
    elif under_way == "in review":
        in_review = [running.ref]
    else:
        running.needs = "human"
    rows = [tier_project(TOOLING, 1, "Building", "Bug"), running]
    for number in (3, 5, 7, 9, 11):
        rows += [tier_project(HOBBY, number, "Ready",
                              "Bug" if number == 9 else "Improve"),
                 tier_ticket(HOBBY, number + 1, number)]

    assert [
        int(ref.rsplit("#", 1)[1])
        for ref in funnel.projected_pull_order(
            rows, NOW, in_review=in_review, recent_starts=["Bug"])
    ] == [2, 4, 6, 8, 10, 12]


BUG_FREE_PROJECTIONS = FIXTURE.parent / "bug_free_projected_order.json"


def _bug_free_projection(seed):
    """One seeded Bug-free board's projection, as ticket numbers.

    The board and history are ``_bug_free_board``'s, whose claims and
    machine-local tickets are work under way. A second seed also pauses
    some tickets, finishes some by comments and puts some in review.
    """
    rows, history, _agent = _bug_free_board(seed)
    rng = random.Random("projection-{}".format(seed))
    tickets = [row.ref for row in rows if row.parent]
    paused, finished, in_review = (
        [ref for ref in tickets if rng.random() < share]
        for share in (0.1, 0.1, 0.2))
    return [
        int(ref.rsplit("#", 1)[1])
        for ref in funnel.projected_pull_order(
            rows, NOW, paused=paused, finished=finished,
            in_review=in_review, recent_starts=history)
    ]


def test_a_bug_free_board_projects_exactly_as_it_did_before_the_share():
    """#1878 changes the projection only where Bugs are on the board. The
    orders were recorded from origin/main at c408f02a2, before #1878, with
    the same paused and finished tickets and no history or review set, which
    that projection did not take."""
    recorded = json.loads(BUG_FREE_PROJECTIONS.read_text())["orders"]
    assert [_bug_free_projection(seed)
            for seed in range(len(recorded))] == recorded


def test_the_queue_lists_the_tickets_in_their_projected_turns(
    monkeypatch, capsys
):
    """``funnel queue`` lists the order begin takes, from the projection the
    dashboard reads, with the holds and start history ``next`` reads.
    Worked by hand: the Bug in review (#6) takes the first turn but is not a
    new start, so the history's Bug holds the Ready Bug #2 for three Improve
    starts, #8, #10 and #12; #14 follows it, and the paused #4 waits for
    everything available now. #6 is not listed, being in review.
    ``startable()`` alone lists every Improve ticket first and #2 last."""
    import heartbeat

    heartbeat._spool("claude", {
        "run": "sat", "agent": "claude", "phase": "bind", "ts": 5,
        "do": "ticket", "work": HOBBY + "#90", "class": "Bug",
    })
    rows = [tier_project(HOBBY, 1, "Ready", "Bug"), tier_ticket(HOBBY, 2, 1),
            tier_project(HOBBY, 5, "Building", "Bug"),
            tier_ticket(HOBBY, 6, 5)]
    for number in (3, 7, 9, 11, 13):
        rows += [tier_project(HOBBY, number, "Ready", "Improve"),
                 tier_ticket(HOBBY, number + 1, number)]
    monkeypatch.setattr(funnel, "awaiting_review",
                        lambda _items, pr_facts=None: {HOBBY + "#6"})
    monkeypatch.setattr(
        funnel, "_backed_off_work",
        lambda items, now, rows=None: {HOBBY + "#4": {"failures": 3}})

    assert funnel.cmd_queue(rows, NOW, pr_facts={}) == 0
    listed = capsys.readouterr().out.split("Startable by Codex", 1)[1]

    assert [
        int(line.split(HOBBY + "#", 1)[1].split()[0])
        for line in listed.splitlines() if HOBBY + "#" in line
    ] == [8, 10, 12, 2, 14, 4]


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


def test_disposal_empty_window_reports_unknown_ratio_and_no_growth():
    report = disposal([], NOW)

    assert report == {
        "window_days": 30,
        "done": 0,
        "parked": 0,
        "finished_vs_abandoned": None,
        "net_open_growth": 0,
    }


def test_disposal_counts_parentless_parks_and_excludes_child_tickets():
    parked = item(
        20, "Parked", None, state="CLOSED", state_reason="NOT_PLANNED",
        created_at=at(5), closed_at=at(2),
    )
    child = item(
        21, "Done", "New", state="CLOSED", state_reason="COMPLETED",
        parent=parked.ref, created_at=at(4), closed_at=at(1),
    )

    report = disposal([parked, child], NOW)

    assert report["done"] == 0
    assert report["parked"] == 1
    assert report["finished_vs_abandoned"] == 0.0
    assert report["net_open_growth"] == 0


def test_disposal_counts_done_projects_but_no_parked_ratio_is_unknown():
    done = [
        item(
            30 + number, "Done", "New", state="CLOSED",
            state_reason="COMPLETED", created_at=at(5), closed_at=at(2),
        )
        for number in range(2)
    ]

    report = disposal(done, NOW)

    assert report["done"] == 2
    assert report["parked"] == 0
    assert report["finished_vs_abandoned"] is None
    assert report["net_open_growth"] == 0


def test_disposal_uses_native_parent_not_parent_prose():
    project = item(
        40, "Done", "New", state="CLOSED", state_reason="COMPLETED",
        body="Parent: another-project#999", created_at=at(4), closed_at=at(1),
    )
    child = item(
        41, "Done", "New", state="CLOSED", state_reason="COMPLETED",
        parent=project.ref, body="", created_at=at(4), closed_at=at(1),
    )

    report = disposal([project, child], NOW)

    assert report["done"] == 1
    assert report["net_open_growth"] == 0


# -- Parsing the GraphQL shape ---------------------------------------------


def test_fixture_parses_into_the_expected_items():
    nodes = json.loads(FIXTURE.read_text())
    items = [i for i in (funnel._from_node(n) for n in nodes) if i]
    assert [i.number for i in items] == [10, 11, 12, 13, 14, 15, 16]
    assert next(i for i in items if i.number == 14).created_at == datetime(
        2026, 8, 20, tzinfo=timezone.utc
    )


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


def shaped_body(origin=None, klass="Improve", extra=""):
    body = (
        "# Plan\n\nProposed class: {}\n\n{}"
        "## Needs you\n"
        "Exposure: nothing outstanding. no new surface.\n"
        "Gates: nothing outstanding. no gate change.\n"
        "Scope and priority: nothing outstanding. bounded.\n"
        "Preference: nothing outstanding. no user-facing choice.\n"
    ).format(klass, extra)
    if origin is not None:
        body += "\n" + funnel.origin_block(
            origin, at=NOW, run="shape-run", agent="claude"
        )
    return body


def test_nate_origin_all_clear_plan_waits_on_the_shaped_gate():
    shaped = item(
        1, "Shaped", "Improve", body=shaped_body("nate-relayed")
    )

    assert gate_question(shaped) == "Is the plan good?"


def test_agent_origin_new_all_clear_plan_waits_on_the_shaped_gate():
    shaped = item(1, "Shaped", "New", body=shaped_body("agent", "New"))

    assert gate_question(shaped) == "Is the plan good?"


def test_escalated_all_clear_plan_waits_on_the_shaped_gate():
    shaped = item(
        1,
        "Shaped",
        "Broken",
        body=shaped_body(
            "agent", "Broken", "Risk: escalated — destructive\n"
        ),
    )

    assert gate_question(shaped) == "Is the plan good?"


@_pytest.mark.parametrize(
    ("number", "klass", "origin"),
    [(518, "Improve", "nate-relayed"),
     (519, "New", "nate-relayed"),
     (643, "New", "nate-relayed")],
)
def test_known_shaped_plan_witnesses_reach_the_shaped_gate(
    number, klass, origin
):
    shaped = item(number, "Shaped", klass, body=shaped_body(origin, klass))

    assert gate_question(shaped) == "Is the plan good?"


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
