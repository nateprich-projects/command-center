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


def test_begin_does_not_consult_breakdown_when_review_work_exists(monkeypatch, capsys):
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])

    def unexpected_breakdown_lookup(items):
        raise AssertionError("breakdown queue was consulted after review work was found")

    monkeypatch.setattr(funnel, "awaiting_breakdown", unexpected_breakdown_lookup)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "review"
    assert result["work"] == work


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


def _idea(number, title, body):
    return SimpleNamespace(
        ref="nateprich-projects/command-center#{}".format(number),
        repo="nateprich-projects/command-center",
        number=number,
        url="https://github.com/nateprich-projects/command-center/issues/{}".format(number),
        title=title,
        body=body,
    )


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


def _ticket_project(number, klass="New", *, in_motion_since=None):
    return funnel.Item(
        repo="nateprich/beta",
        number=number,
        title="project {}".format(number),
        url="https://github.com/nateprich/beta/issues/{}".format(number),
        state="OPEN",
        status="Building",
        klass=klass,
        status_since=NOW,
        children_total=1,
        in_motion_since=in_motion_since,
    )


def _ticket(number, parent, body="Risk: standard", *, in_motion_since=None):
    return funnel.Item(
        repo="nateprich/beta",
        number=number,
        title="ticket {}".format(number),
        url="https://github.com/nateprich/beta/issues/{}".format(number),
        state="OPEN",
        body=body,
        parent="nateprich/beta#{}".format(parent),
        in_motion_since=in_motion_since,
    )


def _allow_codex_begin(monkeypatch):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "awaiting_review", lambda items: set())


def test_codex_begin_records_heartbeat_before_selecting_and_claiming(
    monkeypatch, capsys
):
    ticket = _ticket(2, 1)
    rows = [_ticket_project(1), ticket]
    events = []

    def start(*args, **kwargs):
        events.append("heartbeat")
        return SimpleNamespace(stdout="run-id\n")

    monkeypatch.setattr(funnel.subprocess, "run", start)
    monkeypatch.setattr(funnel, "awaiting_review", lambda items: set())
    monkeypatch.setattr(
        funnel,
        "next_ticket",
        lambda items, now, **kwargs: events.append("next") or ticket,
    )
    monkeypatch.setattr(
        funnel,
        "write_lock",
        lambda item, value: events.append(("claim", item.ref, value)),
    )

    assert funnel.cmd_begin(rows, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert events == [
        "heartbeat",
        "next",
        ("claim", ticket.ref, "2026-09-05T12:00:00Z"),
    ]


def test_codex_begin_honours_the_run_tier(monkeypatch, capsys):
    rows = [
        _ticket_project(1),
        _ticket(2, 1, body="Risk: escalated"),
        _ticket_project(3),
        _ticket(4, 3, body="Risk: standard"),
    ]
    claims = []
    _allow_codex_begin(monkeypatch)
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: claims.append(item.ref)
    )

    assert funnel.cmd_begin(rows, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == rows[3].ref
    assert claims == [rows[3].ref]


def test_codex_begin_stops_at_the_wip_limit(monkeypatch, capsys):
    rows = []
    claimed_at = NOW.replace(hour=11, minute=55)
    for index in range(funnel.WIP_LIMIT):
        parent = 100 + index
        rows.extend([
            _ticket_project(parent, in_motion_since=None),
            _ticket(200 + index, parent, in_motion_since=claimed_at),
        ])
    rows.extend([_ticket_project(1), _ticket(2, 1)])
    claims = []
    _allow_codex_begin(monkeypatch)
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: claims.append(item.ref)
    )

    assert funnel.cmd_begin(rows, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "stop"
    assert "lock held by" in result["why"]
    assert claims == []


def test_codex_begin_allows_broken_preemption_at_the_wip_limit(
    monkeypatch, capsys
):
    rows = []
    claimed_at = NOW.replace(hour=11, minute=55)
    for index in range(funnel.WIP_LIMIT):
        parent = 100 + index
        rows.extend([
            _ticket_project(parent, in_motion_since=None),
            _ticket(200 + index, parent, in_motion_since=claimed_at),
        ])
    rows.extend([
        _ticket_project(1, klass="Broken"),
        _ticket(2, 1, body="Risk: standard"),
    ])
    claims = []
    _allow_codex_begin(monkeypatch)
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: claims.append(item.ref)
    )

    assert funnel.cmd_begin(rows, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == rows[-1].ref
    assert claims == [rows[-1].ref]
