"""A direct claim can carry the run identity used by finish-ticket (#2164)."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from engine import implement  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
REPO = "nateprich/beta"


def _item(number, *, status=None, klass=None, parent=None, **fields):
    values = {
        "repo": REPO,
        "title": "issue {}".format(number),
        "url": "https://example.invalid/{}".format(number),
        "state": "OPEN",
        "item_id": "PVTI_{}".format(number),
        "status_since": NOW,
    }
    values.update(fields)
    return Item(number=number, status=status, klass=klass, parent=parent,
                **values)


def _claim_rows():
    parent = _item(1, status="Building", klass="Broken")
    ticket = _item(42, parent=parent.ref, in_motion_since=None)
    return [parent, ticket], ticket


def _prepare_claim(monkeypatch, ticket):
    items = [
        _item(1, status="Building", klass="Broken"),
        ticket,
    ]
    records = []
    monkeypatch.setattr(funnel, "in_motion", lambda *_a, **_kw: [])
    monkeypatch.setattr(funnel, "stale_locks", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        funnel, "write_lock",
        lambda item, value: setattr(
            item, "in_motion_since", funnel.parse_time(value) if value else None
        ),
    )
    monkeypatch.setattr(funnel, "_begin_parent", lambda *_a: None)
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda _items: {})
    monkeypatch.setattr(
        funnel, "load_project_items_by_refs", lambda _refs: [ticket]
    )
    monkeypatch.setattr(
        heartbeat, "append",
        lambda _agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda _kept: None)
    monkeypatch.setattr(
        heartbeat, "read_github_strict",
        lambda agent: records if agent == "codex" else [],
    )
    return items, records


def test_claim_with_run_binds_the_session_before_finish(monkeypatch, capsys):
    """The public claim flags write the binding the finish guard already reads."""
    _rows, ticket = _claim_rows()
    items, records = _prepare_claim(monkeypatch, ticket)

    result = funnel.main(
        ["claim", ticket.ref, "--run", "run-42", "--agent", "codex"],
        _items_loader=lambda **_kwargs: items,
        _reset_api_usage=False,
    )

    assert result == 0
    assert capsys.readouterr().out == ticket.url + "\n"
    assert implement._require_current_claim(ticket.ref, "run-42", "codex") == "owned"
    assert len(records) == 1
    record = records[0]
    assert {key: value for key, value in record.items() if key != "ts"} == {
        "run": "run-42",
        "agent": "codex",
        "phase": "bind",
        "do": "ticket",
        "work": ticket.ref,
        "repo": REPO,
        "class": "Broken",
    }
    assert isinstance(record["ts"], int)
    assert record["ts"] >= int(ticket.in_motion_since.timestamp())


def test_claim_requires_run_and_agent_together_before_claiming(monkeypatch):
    """A partial binding request must not create an unowned live claim."""
    _rows, ticket = _claim_rows()
    items, records = _prepare_claim(monkeypatch, ticket)

    with pytest.raises(SystemExit):
        funnel.main(
            ["claim", ticket.ref, "--run", "run-42"],
            _items_loader=lambda **_kwargs: items,
            _reset_api_usage=False,
        )

    assert ticket.in_motion_since is None
    assert records == []


def test_claim_without_a_binding_reproduces_finish_refusal(monkeypatch):
    """A direct claim lacks the heartbeat record finish-ticket requires."""
    _rows, ticket = _claim_rows()
    items, records = _prepare_claim(monkeypatch, ticket)

    result = funnel.main(
        ["claim", ticket.ref],
        _items_loader=lambda **_kwargs: items,
        _reset_api_usage=False,
    )

    assert result == 0
    assert records == []
    with pytest.raises(implement.SupersededRunError) as refusal:
        implement._require_current_claim(ticket.ref, "run-42", "codex")
    assert refusal.value.reason == "claim holder is unknown"


def test_later_run_binding_still_supersedes_the_first_claim(monkeypatch):
    """The newest post-claim bind remains the owner used by finish-ticket."""
    _rows, ticket = _claim_rows()
    ticket.in_motion_since = NOW
    monkeypatch.setattr(
        funnel, "load_project_items_by_refs", lambda _refs: [ticket]
    )
    records = {
        "codex": [{
            "run": "first-run", "agent": "codex", "phase": "bind",
            "ts": int(NOW.timestamp()), "do": "ticket", "work": ticket.ref,
        }],
        "muse": [],
        "claude": [{
            "run": "later-run", "agent": "claude", "phase": "bind",
            "ts": int(NOW.timestamp()) + 1, "do": "ticket",
            "work": ticket.ref,
        }],
    }
    monkeypatch.setattr(
        heartbeat, "read_github_strict", lambda agent: records[agent]
    )

    with pytest.raises(implement.SupersededRunError) as refusal:
        implement._require_current_claim(ticket.ref, "first-run", "codex")

    assert refusal.value.reason == "another run holds the claim"


def test_owned_release_cli_refuses_an_old_run_without_writing(monkeypatch, capsys):
    rows, ticket = _claim_rows()
    ticket.in_motion_since = NOW
    monkeypatch.setattr(
        implement, "_claim_state",
        lambda ref, run, agent: ("other", rows),
    )
    monkeypatch.setattr(
        funnel, "write_lock",
        lambda *args: pytest.fail("a superseded run cleared the newer claim"),
    )

    result = funnel.main(
        ["release", ticket.ref, "--run", "old-run", "--agent", "muse"],
        _items_loader=lambda **kwargs: rows,
        _reset_api_usage=False,
    )

    assert result != 0
    assert "another run holds the claim" in capsys.readouterr().err
    assert ticket.in_motion_since == NOW


def test_release_refuses_a_partial_owner_identity_before_writing(monkeypatch):
    rows, ticket = _claim_rows()
    monkeypatch.setattr(
        funnel, "write_lock",
        lambda *args: pytest.fail("partial identity cleared the claim"),
    )

    with pytest.raises(funnel.GitHubError,
                       match="--run and --agent together"):
        funnel.cmd_release(rows, NOW, ticket.ref, run="old-run")
