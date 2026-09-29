"""Questions the funnel watch answers leave Nate's decision list (#1891).

Nate, 2026-09-28: every silent-block question (``Unblock?`` and ``Unblock or
park?``) except one waiting on his hands, and ``Is the plan good?`` on an
agent-origin Broken or Bug plan except one still holding an Exposure or
Preference question, are the funnel watch's to answer. They leave the brief's
``items`` and ``total_needing_nate`` and appear under ``watch_gates``;
``gate_question`` itself does not change.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)
REPO = "nateprich/beta"


@pytest.fixture(autouse=True)
def offline_brief(monkeypatch):
    """Keep ``cmd_brief`` on the fixture items: no GitHub, no heartbeat."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", lambda items, now: None
    )
    monkeypatch.setattr(
        funnel, "decline_routing_metric",
        lambda items, now: {"status": "available", "declines": 0},
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})
    yield
    funnel.reset_api_usage()


def plan_body(needs_lines=()):
    """A rendered plan body, with a Needs Nate section when lines are given."""
    body = (
        "## What\n\nFix the thing.\n\n## Premises\n\nNone recorded.\n\n"
        "Proposed class: Broken\n\n## Decided from precedent\n\n"
        "None recorded.\n"
    )
    if needs_lines:
        body += "\n## Needs Nate\n\n" + "\n".join(needs_lines) + "\n"
    return body


def shaped_plan(number=40, *, origin="agent", klass="Broken",
                needs_lines=(), body=None):
    return funnel.Item(
        repo=REPO, number=number, title="Plan {}".format(number),
        url="https://example.invalid/{}".format(number), state="OPEN",
        status="Shaped", klass=klass, origin=origin, risk="standard",
        needs="human",
        body=plan_body(needs_lines) if body is None else body,
        status_since=NOW - timedelta(days=2),
    )


def building_project(number=30):
    return funnel.Item(
        repo=REPO, number=number, title="Project {}".format(number),
        url="https://example.invalid/{}".format(number), state="OPEN",
        status="Building", klass="Broken", origin="agent", risk="standard",
        needs="none", children_total=1,
        status_since=NOW - timedelta(days=3),
    )


def silent_blocked_ticket(number=31, parent=30, needs="none"):
    return funnel.Item(
        repo=REPO, number=number, title="Ticket {}".format(number),
        url="https://example.invalid/{}".format(number), state="OPEN",
        parent="{}#{}".format(REPO, parent), risk="standard", needs=needs,
        labels=["blocked"], blocked_since=NOW - timedelta(days=1),
    )


def brief_for(items, capsys):
    assert funnel.cmd_brief(items, NOW) == 0
    return json.loads(capsys.readouterr().out)


def routed(item, *others):
    by_ref = {i.ref: i for i in (item,) + others}
    return funnel.watch_owns_gate(item, funnel.gate_question(item), by_ref)


def test_a_silently_blocked_ticket_leaves_the_total_for_watch_gates(capsys):
    project = building_project()
    ticket = silent_blocked_ticket()
    assert funnel.gate_question(ticket) == "Unblock?"

    brief = brief_for([project, ticket], capsys)

    assert brief["total_needing_nate"] == 0
    assert brief["items"] == []
    assert brief["watch_gates"] == [{
        "ref": ticket.ref,
        "title": ticket.title,
        "url": ticket.url,
        "class": "Broken",
        "question": "Unblock?",
        "waited": funnel.humanise(ticket.waited(NOW)),
    }]


def test_a_silently_blocked_project_asking_unblock_or_park_is_the_watchs():
    project = building_project()
    project.labels = ["blocked"]
    assert funnel.gate_question(project) == "Unblock or park?"
    assert routed(project)


def test_a_blocked_human_step_stays_with_nate(capsys):
    project = building_project()
    ticket = silent_blocked_ticket(needs="human")
    assert funnel.gate_question(ticket) == "Unblock?"
    assert not routed(ticket, project)

    brief = brief_for([project, ticket], capsys)

    assert brief["total_needing_nate"] == 1
    assert [row["ref"] for row in brief["items"]] == [ticket.ref]
    assert brief["watch_gates"] == []


def test_agent_broken_plan_with_only_a_scope_question_leaves(capsys):
    plan = shaped_plan(needs_lines=(
        "- Scope and priority: should this include the sibling's fix?",
    ))
    assert funnel.gate_question(plan) == "Is the plan good?"

    brief = brief_for([plan], capsys)

    assert brief["total_needing_nate"] == 0
    assert brief["items"] == []
    assert [row["ref"] for row in brief["watch_gates"]] == [plan.ref]
    assert brief["watch_gates"][0]["question"] == "Is the plan good?"
    assert brief["watch_gates"][0]["class"] == "Broken"


def test_the_same_plan_with_a_preference_question_stays(capsys):
    plan = shaped_plan(needs_lines=(
        "- Scope and priority: should this include the sibling's fix?",
        "- Preference: which wording should the alert use?",
    ))

    brief = brief_for([plan], capsys)

    assert brief["total_needing_nate"] == 1
    assert [row["ref"] for row in brief["items"]] == [plan.ref]
    assert brief["watch_gates"] == []


def test_an_exposure_question_stays_with_nate():
    plan = shaped_plan(needs_lines=(
        "- Exposure: this sends the log to a public repository.",
    ))
    assert not routed(plan)


def test_a_gates_question_and_a_declared_risk_plan_are_the_watchs():
    gates = shaped_plan(needs_lines=("- Gates: who closes this?",))
    assert routed(gates)
    risk_only = shaped_plan(41)
    risk_only.risk = "escalated"
    assert routed(risk_only)


def test_answered_exposure_and_preference_lines_do_not_hold():
    plan = shaped_plan(needs_lines=(
        "- Exposure: answered 2026-09-27 by Nate. Private only.",
        "- Preference: nothing outstanding",
    ))
    assert routed(plan)


def test_a_bug_plan_is_the_watchs_too():
    assert routed(shaped_plan(klass="Bug"))


def test_a_nate_origin_broken_plan_stays(capsys):
    plan = shaped_plan(origin="Nate", needs_lines=(
        "- Scope and priority: should this include the sibling's fix?",
    ))

    brief = brief_for([plan], capsys)

    assert brief["total_needing_nate"] == 1
    assert [row["ref"] for row in brief["items"]] == [plan.ref]
    assert brief["watch_gates"] == []


def test_an_improve_plan_stays(capsys):
    plan = shaped_plan(klass="Improve", needs_lines=(
        "- Scope and priority: should this include the sibling's fix?",
    ))

    brief = brief_for([plan], capsys)

    assert brief["total_needing_nate"] == 1
    assert [row["ref"] for row in brief["items"]] == [plan.ref]
    assert brief["watch_gates"] == []


def test_an_unreadable_needs_section_or_an_unloaded_body_stays():
    unreadable = shaped_plan(needs_lines=(
        "- Scope and priority: include it?",
        "Something the parser cannot read.",
    ))
    assert funnel.open_needs_nate_categories(unreadable.body) is None
    assert not routed(unreadable)

    unloaded = shaped_plan(41)
    unloaded.body = None
    assert not routed(unloaded)


def test_accept_and_the_breakdown_question_stay_with_nate():
    accept = building_project(50)
    accept.klass = "New"
    accept.children_done = 1
    assert funnel.gate_question(accept) == "Accept it?"
    assert not routed(accept)

    breakdown = building_project(51)
    breakdown.labels = ["blocked"]
    breakdown.needs_decision = "Which repo should hold the fixture?"
    assert funnel.gate_question(breakdown) == "Answer the breakdown's question?"
    assert not routed(breakdown)


def test_gate_question_is_unchanged_for_watch_owned_items():
    """Lanes and sweeps still read the live question (#1891)."""
    plan = shaped_plan(needs_lines=("- Scope and priority: include it?",))
    ticket = silent_blocked_ticket()
    assert funnel.gate_question(plan) == "Is the plan good?"
    assert funnel.gate_question(ticket) == "Unblock?"
    assert funnel.awaiting_decision([plan, ticket]) != []


def test_split_keeps_the_queue_order_and_drops_nothing():
    project = building_project()
    items = [
        project,
        silent_blocked_ticket(),
        shaped_plan(40, needs_lines=("- Scope and priority: include it?",)),
        shaped_plan(41, origin="Nate"),
        shaped_plan(42, needs_lines=("- Preference: which name?",)),
    ]
    ordered = funnel.awaiting_decision(items)
    nate, watched = funnel.split_decisions(items)

    assert sorted(i.ref for i in nate + watched) == sorted(
        i.ref for i in ordered
    )
    assert nate == [i for i in ordered if i in nate]
    assert watched == [i for i in ordered if i in watched]
    assert [i.number for i in watched] == [
        i.number for i in ordered if i.number in (31, 40)
    ]


def test_queue_lists_watch_owned_items_apart_from_nates(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "awaiting_review", lambda items, **kw: [])
    monkeypatch.setattr(funnel, "_backoff_rows", lambda: [])
    plan = shaped_plan(needs_lines=("- Scope and priority: include it?",))
    nate_plan = shaped_plan(41, origin="Nate")

    funnel.cmd_queue([plan, nate_plan], NOW)
    out = capsys.readouterr().out

    nate_section, rest = out.split("Answered by the funnel watch", 1)
    assert "Waiting on Nate (1)" in nate_section
    assert nate_plan.ref in nate_section
    assert plan.ref not in nate_section
    assert plan.ref in rest.split("Startable by", 1)[0]
