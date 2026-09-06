"""The explicit, reasoned transition from an active project to Parked."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def _no_github(monkeypatch):
    calls = []

    def load_items():
        calls.append("load items")
        return []

    monkeypatch.setattr(funnel, "load_items", load_items)
    return calls


def _assert_reason_refused(monkeypatch, capsys, argv):
    calls = _no_github(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        funnel.main(argv)

    assert exc.value.code != 0
    assert "reason" in capsys.readouterr().err.lower()
    assert calls == []


def test_park_refuses_an_absent_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42"])


def test_park_refuses_an_empty_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42", "--reason", ""])


def test_park_refuses_a_whitespace_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42", "--reason", "   "])


def test_park_sets_status_closes_not_planned_then_posts_the_reason(monkeypatch):
    target = funnel.Item(
        repo="nateprich/beta",
        number=42,
        title="A project to stop",
        url="https://github.com/nateprich/beta/issues/42",
        state="OPEN",
        status="Ready",
        item_id="project-item-42",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [target])
    events = []

    def graphql(query, **variables):
        if query == funnel.SET_FIELD:
            events.append(("set field", variables))
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": target.item_id}}}
        events.append(("read options", variables))
        return {"node": {"options": [{"id": "parked-option", "name": "Parked"}]}}

    def run(args, capture_output, text=True):
        events.append((args[2], tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main(["park", "42", "--reason", "No longer worth the cost"]) == 0

    assert [event[0] for event in events] == [
        "read options", "set field", "close", "comment"
    ]
    assert events[1][1] == {
        "project": funnel.PROJECT_ID,
        "item": "project-item-42",
        "field": funnel.STATUS_FIELD_ID,
        "option": "parked-option",
    }
    assert events[2][1] == (
        "gh", "issue", "close", "42", "--repo", "nateprich/beta",
        "--reason", "not planned",
    )
    assert events[3][1] == (
        "gh", "issue", "comment", "42", "--repo", "nateprich/beta",
        "--body", funnel.PARK_COMMENT_PREFIX + "No longer worth the cost",
    )
