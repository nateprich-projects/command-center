"""The opening command reports only queues it actually consulted."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import usage  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def _allow_begin(monkeypatch):
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: {"over_pace": False},
    )


def _begin(monkeypatch, capsys, *, breakdown):
    _allow_begin(monkeypatch)
    assert funnel.cmd_begin([], NOW, "zcode", "standard", False, breakdown) == 0
    return json.loads(capsys.readouterr().out)


def _ticket(number, parent, *, body="Risk: standard", klass="Improve",
            in_motion_since=None):
    project = funnel.Item(
        repo="nateprich/example",
        number=parent,
        title="Project {}".format(parent),
        url="https://github.com/nateprich/example/issues/{}".format(parent),
        state="OPEN",
        status="Building",
        klass=klass,
        children_total=1,
    )
    ticket = funnel.Item(
        repo=project.repo,
        number=number,
        title="Ticket {}".format(number),
        url="https://github.com/nateprich/example/issues/{}".format(number),
        state="OPEN",
        body=body,
        parent=project.ref,
        item_id="item-{}".format(number),
        in_motion_since=in_motion_since,
    )
    return project, ticket


def _codex_begin(monkeypatch, capsys, items, *, tier="standard"):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    bodies = {item.number: item.body for item in items}
    monkeypatch.setattr(
        funnel, "_ticket_body", lambda repo, number: bodies.get(number) or ""
    )
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )
    assert funnel.cmd_begin(items, NOW, "codex", tier, False) == 0
    return json.loads(capsys.readouterr().out), writes


def test_codex_begin_records_heartbeat_before_selecting_and_claiming(
    monkeypatch, capsys
):
    project, ticket = _ticket(7, 6)
    items = [project, ticket]
    events = []

    def heartbeat(*args, **kwargs):
        events.append("heartbeat")
        return SimpleNamespace(stdout="run-id\n")

    monkeypatch.setattr(funnel.subprocess, "run", heartbeat)
    monkeypatch.setattr(usage, "read_agent", lambda agent, timestamp: {"windows": {}})
    monkeypatch.setattr(
        usage, "pace", lambda reading, timestamp, provider: {"over_pace": False}
    )
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(
        funnel,
        "clear_satisfied_blocks",
        lambda *args, **kwargs: events.append("clear") or [],
    )
    monkeypatch.setattr(
        funnel,
        "next_ticket_for_tier",
        lambda *args, **kwargs: events.append("next") or ticket,
    )
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: events.append("claim")
    )

    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert events == ["heartbeat", "clear", "next", "claim"]


def test_codex_begin_skips_the_other_tier_before_claiming(monkeypatch, capsys):
    standard_project, standard = _ticket(8, 9, body="Risk: standard")
    escalated_project, escalated = _ticket(
        10, 11, body="Risk: escalated — concurrency"
    )
    result, writes = _codex_begin(
        monkeypatch, capsys,
        [escalated_project, escalated, standard_project, standard],
        tier="standard",
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == standard.ref
    assert [ref for ref, value in writes if value] == [standard.ref]


def test_codex_begin_respects_the_wip_limit(monkeypatch, capsys):
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            20 + index * 2, 21 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(40, 41)
    items.extend((project, ticket))

    result, writes = _codex_begin(monkeypatch, capsys, items)

    assert result["do"] == "stop"
    assert "lock held" in result["why"]
    assert writes == []


def test_codex_begin_allows_broken_preemption_at_the_wip_limit(
    monkeypatch, capsys
):
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            50 + index * 2, 51 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(70, 71, klass="Broken")
    items.extend((project, ticket))

    result, writes = _codex_begin(monkeypatch, capsys, items)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert [ref for ref, value in writes if value] == [ticket.ref]


def test_begin_stop_reason_omits_breakdown_when_it_was_not_requested(
    monkeypatch, capsys
):
    calls = []

    def unexpected_breakdown_lookup(items):
        calls.append(items)
        raise AssertionError("breakdown queue was consulted without --breakdown")

    monkeypatch.setattr(funnel, "awaiting_breakdown", unexpected_breakdown_lookup)

    result = _begin(monkeypatch, capsys, breakdown=False)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review"
    assert "break down" not in result["why"]
    assert calls == []


def test_begin_stop_reason_names_both_empty_queues_when_both_were_consulted(
    monkeypatch, capsys
):
    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"


def test_begin_keeps_review_first_against_same_class_later_jobs(monkeypatch, capsys):
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    pending = SimpleNamespace(
        ref="nateprich-projects/command-center#20",
        url="https://github.com/nateprich-projects/command-center/issues/20",
        title="Break down project",
        repo="nateprich-projects/command-center",
        number=20,
        klass="Improve",
    )
    idea = SimpleNamespace(klass="Improve")
    breakdown_calls = []
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    monkeypatch.setattr(
        funnel,
        "awaiting_breakdown",
        lambda items: breakdown_calls.append(items) or [pending],
    )
    monkeypatch.setattr(
        funnel, "shapeable_idea", lambda items, tier, reading: idea
    )

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "review"
    assert result["work"] == work
    assert breakdown_calls == [[]]


def test_breakdown_work_carries_plan_access_signals(monkeypatch, capsys):
    item = SimpleNamespace(
        ref="nateprich-projects/command-center#25",
        repo="nateprich-projects/command-center",
        number=25,
        url="https://github.com/nateprich-projects/command-center/issues/25",
        title="Reach the funnel from general chat",
    )
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda items: [item])
    monkeypatch.setattr(
        funnel,
        "_ticket_body",
        lambda repo, number: "Cloudflare Tunnel and a fine-grained token",
    )

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "breakdown"
    assert result["work"]["access_signals"] == ["token", "tunnel"]


def _idea(number, title, body, klass=None):
    return SimpleNamespace(
        ref="nateprich-projects/command-center#{}".format(number),
        repo="nateprich-projects/command-center",
        number=number,
        url="https://github.com/nateprich-projects/command-center/issues/{}".format(number),
        title=title,
        body=body,
        klass=klass,
    )


def _review_job(ticket, pr=7):
    return {"pr": pr, "repo": ticket.repo, "ref": ticket.ref}


def _reviewer_begin(
    monkeypatch,
    capsys,
    items,
    *,
    review=None,
    breakdown_item=None,
    idea=None,
    breakdown=False,
):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(
        funnel,
        "review_queue",
        lambda rows, tier: [review] if review is not None else [],
    )
    monkeypatch.setattr(
        funnel,
        "awaiting_breakdown",
        lambda rows: ([breakdown_item] if breakdown_item is not None else []),
    )
    monkeypatch.setattr(
        funnel,
        "shapeable_idea",
        lambda rows, tier, reading: idea,
    )
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: "")

    assert funnel.cmd_begin(
        items, NOW, "zcode", "standard", False, breakdown
    ) == 0
    return json.loads(capsys.readouterr().out)


def test_broken_idea_preempts_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(101, 100, klass="Improve")
    idea = _idea(102, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "shape"
    assert result["work"]["ref"] == idea.ref


def test_improve_idea_does_not_preempt_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(103, 100, klass="Improve")
    idea = _idea(104, "Improve idea", "Risk: standard", klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_broken_review_is_before_a_broken_idea(monkeypatch, capsys):
    project, ticket = _ticket(105, 100, klass="Broken")
    idea = _idea(106, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_unclassed_idea_does_not_preempt_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(107, 100, klass="Improve")
    idea = _idea(108, "Unclassed idea", "Risk: standard")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_same_class_candidates_keep_bottom_up_order(monkeypatch, capsys):
    project, ticket = _ticket(109, 100, klass="Improve")
    pending = funnel.Item(
        repo="nateprich/example",
        number=110,
        title="Project 110",
        url="https://github.com/nateprich/example/issues/110",
        state="OPEN",
        status="Ready",
        klass="Improve",
        children_total=0,
    )
    idea = _idea(111, "Improve idea", "Risk: standard", klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        breakdown_item=pending,
        idea=idea,
        breakdown=True,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_shape_is_not_offered_when_the_first_idea_is_the_other_tier(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: [_idea(31, "Risky idea", "Risk: escalated")],
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"


def test_shape_is_not_offered_without_headroom(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: calls.append(items) or [_idea(32, "An idea", "Risk: standard")],
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: False)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"
    assert calls == []


def test_shape_offers_only_the_first_idea_matching_the_run_tier(
    monkeypatch, capsys
):
    candidates = [
        _idea(33, "Escalated first", "Risk: escalated"),
        _idea(34, "Standard first", "Risk: standard"),
        _idea(35, "Standard second", "Risk: standard"),
    ]
    calls = []
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: calls.append(items) or candidates,
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "shape"
    assert result["work"] == {
        "ref": candidates[1].ref,
        "url": candidates[1].url,
        "title": candidates[1].title,
    }
    assert len(calls) == 1


def test_muse_standard_schedule_is_offered_a_standard_idea_on_an_unmetered_reading(
    monkeypatch, capsys
):
    """#86, revised by Nate 2026-09-09: Muse's standard schedule shapes too.
    The real headroom gate must admit an unmetered reading, not a patched one."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"unmetered": True, "windows": {}},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(36, "A standard idea", "Risk: standard")],
    )

    assert funnel.cmd_begin([], NOW, "muse", "standard", False, True) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["unmetered"] is True
    assert result["do"] == "shape"
    assert result["work"]["ref"] == "nateprich-projects/command-center#36"


def test_muse_escalated_schedule_is_never_offered_shaping(monkeypatch, capsys):
    """The hourly escalated schedule is review-only: no --breakdown, and an
    escalated idea is not offered either, so nothing routes shaping there."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"unmetered": True, "windows": {}},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(37, "A standard idea", "Risk: standard")],
    )

    assert funnel.cmd_begin([], NOW, "muse", "escalated", False, False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "stop"
