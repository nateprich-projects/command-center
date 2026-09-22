"""A closed issue may be Done or Parked, and nothing else (#1206, #1207)."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
REPO = "nateprich-projects/command-center"


def project(number=900, status="Shaped", klass="Broken", state="OPEN"):
    return funnel.Item(
        repo=REPO, number=number, title="A project", url="",
        state=state, status=status, klass=klass, status_since=NOW,
        item_id="PVTI_fake",
    )


@pytest.fixture
def no_writes(monkeypatch):
    """Nothing in these tests may reach GitHub with a mutation."""
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError("a Status mutation was attempted")

    monkeypatch.setattr(funnel, "gh_graphql", refuse)
    return calls


def _state(monkeypatch, value):
    monkeypatch.setattr(funnel, "live_issue_state", lambda item: value)


def _capture_comments(monkeypatch):
    posted = []

    def run_gh(args, **kwargs):
        posted.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(funnel, "_run_gh", run_gh)
    return posted


def test_a_non_terminal_write_on_a_closed_issue_is_refused(monkeypatch, no_writes):
    _state(monkeypatch, "CLOSED")
    posted = _capture_comments(monkeypatch)

    refusal = funnel._write_status(project(), "Ready", NOW)

    assert refusal is not None
    assert "is CLOSED on GitHub" in refusal
    assert "Ready" in refusal
    assert no_writes == []
    # The refusal is left where it can be read after the session ends.
    assert any("comment" in args for args in posted)
    body = posted[0][posted[0].index("--body") + 1]
    assert "Status write refused" in body


def test_done_is_allowed_on_a_closed_issue(monkeypatch):
    _state(monkeypatch, "CLOSED")
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: {})
    monkeypatch.setattr(funnel, "_status_write_confirmed", lambda *a: True)

    item = project()
    assert funnel._write_status(item, "Done", NOW) is None
    assert item.status == "Done"


def test_parked_is_allowed_on_a_closed_issue(monkeypatch):
    _state(monkeypatch, "CLOSED")
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: {})
    monkeypatch.setattr(funnel, "_status_write_confirmed", lambda *a: True)

    item = project()
    assert funnel._write_status(item, "Parked", NOW) is None
    assert item.status == "Parked"


def test_open_behaviour_is_unchanged(monkeypatch):
    _state(monkeypatch, "OPEN")
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: {})
    monkeypatch.setattr(funnel, "_status_write_confirmed", lambda *a: True)

    item = project()
    assert funnel._write_status(item, "Ready", NOW) is None
    assert item.status == "Ready"


def test_an_unreadable_state_falls_back_to_the_loaded_one(monkeypatch, no_writes):
    """One GitHub hiccup must not block every approve and every claim, and
    the loaded state was already CLOSED in the case this guards (#1206)."""
    _state(monkeypatch, None)
    _capture_comments(monkeypatch)

    refusal = funnel._write_status(
        project(state="CLOSED"), "Building", NOW
    )

    assert "is CLOSED on GitHub" in refusal
    assert no_writes == []


def test_an_unreadable_state_on_a_loaded_open_issue_still_writes(monkeypatch):
    _state(monkeypatch, None)
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: {})
    monkeypatch.setattr(funnel, "_status_write_confirmed", lambda *a: True)

    item = project(state="OPEN")
    assert funnel._write_status(item, "Ready", NOW) is None
    assert item.status == "Ready"


def test_a_live_close_beats_a_loaded_open(monkeypatch, no_writes):
    """The whole point of the live read: the object can be minutes old."""
    _state(monkeypatch, "CLOSED")
    _capture_comments(monkeypatch)

    refusal = funnel._write_status(project(state="OPEN"), "Ready", NOW)

    assert "is CLOSED on GitHub" in refusal
    assert no_writes == []


def test_an_unreadable_state_still_allows_a_terminal_write(monkeypatch):
    """Done and Parked never consult the read at all."""
    calls = []
    monkeypatch.setattr(funnel, "live_issue_state",
                        lambda item: calls.append(item) or None)
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: {})
    monkeypatch.setattr(funnel, "_status_write_confirmed", lambda *a: True)

    assert funnel._write_status(project(), "Done", NOW) is None
    assert calls == []


def test_the_state_read_is_live_rather_than_the_loaded_item(monkeypatch):
    """The loaded object can be minutes old; a close in between is the case."""
    seen = {}

    def fake_json(*args):
        seen["args"] = args
        return {"state": "CLOSED"}

    monkeypatch.setattr(funnel, "_gh_json", fake_json)

    item = project(state="OPEN")
    assert funnel.live_issue_state(item) == "CLOSED"
    assert "issue" in seen["args"] and "view" in seen["args"]


def test_a_refusal_is_reported_rather_than_raised(monkeypatch, no_writes):
    _state(monkeypatch, "CLOSED")
    _capture_comments(monkeypatch)

    assert funnel.status_write_refusal(project(), "Ideas") is not None
    assert funnel.status_write_refusal(project(), "Done") is None
    assert funnel.status_write_refusal(project(), "Parked") is None


def test_the_gate_refuses_to_approve_a_closed_project(monkeypatch, capsys):
    """approve, shaped and claim share one predicate, not three copies."""
    _state(monkeypatch, "CLOSED")
    _capture_comments(monkeypatch)
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a Status mutation was attempted")))

    item = project(number=2, status="Shaped", klass="Broken")
    assert funnel.cmd_answer([item], NOW, "approve", item.ref, True) == 1
    assert "is CLOSED on GitHub" in capsys.readouterr().err


def test_a_claim_does_not_drag_a_closed_project_to_building(monkeypatch, capsys):
    _state(monkeypatch, "CLOSED")
    _capture_comments(monkeypatch)
    monkeypatch.setattr(funnel, "gh_graphql", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a Status mutation was attempted")))

    parent = project(number=3, status="Ready", klass="Broken")
    ticket = funnel.Item(
        repo=REPO, number=4, title="A ticket", url="", state="OPEN",
        parent=parent.ref, item_id="PVTI_ticket",
    )

    funnel._begin_parent([parent, ticket], ticket)

    assert "is CLOSED on GitHub" in capsys.readouterr().err
    assert parent.status == "Ready"
