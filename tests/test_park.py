"""The explicit, reasoned transition from an active project to Parked."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta
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


def _assert_wake_date_refused(monkeypatch, capsys, wake_date):
    calls = _no_github(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        funnel.main([
            "park", "42", "--reason", "Not now",
            "--wake-date", wake_date,
        ])

    assert exc.value.code != 0
    assert "wake date" in capsys.readouterr().err.lower()
    assert calls == []


def test_park_refuses_an_absent_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42"])


def test_park_refuses_an_empty_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42", "--reason", ""])


def test_park_refuses_a_whitespace_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42", "--reason", "   "])


@pytest.mark.parametrize("wake_date", ["not-a-date", "2026-02-30"])
def test_park_refuses_a_malformed_wake_date_before_calling_github(
    monkeypatch, capsys, wake_date
):
    _assert_wake_date_refused(monkeypatch, capsys, wake_date)


def test_park_refuses_a_past_wake_date_before_calling_github(monkeypatch, capsys):
    yesterday = (funnel._block_condition_date() - timedelta(days=1)).isoformat()
    _assert_wake_date_refused(monkeypatch, capsys, yesterday)


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

    assert funnel.main([
        "park", "42", "--reason", "No longer worth the cost",
        "--run", "run-42", "--agent", "claude",
    ]) == 0

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
    assert events[3][1][:6] == (
        "gh", "issue", "comment", "42", "--repo", "nateprich/beta",
    )
    posted = events[3][1][-1]
    assert funnel._visible_comment(posted) == (
        funnel.PARK_COMMENT_PREFIX + "No longer worth the cost"
    )
    provenance = funnel.parse_provenance(posted)
    assert posted == funnel.append_provenance(
        funnel.PARK_COMMENT_PREFIX + "No longer worth the cost",
        "nate-relayed", at=datetime.fromisoformat(provenance["at"]),
        run="run-42", agent="claude",
    )
    assert funnel.render_voice(posted) == "Nate (relayed by claude)"
    assert funnel.parse_provenance(posted)["run"] == "run-42"


def test_park_with_wake_date_records_and_reads_status_and_reason(monkeypatch):
    target = funnel.Item(
        repo="nateprich/beta",
        number=42,
        title="A project to resume",
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
    wake_date = funnel._block_condition_date() + timedelta(days=1)

    assert funnel.main([
        "park", "42", "--reason", "Resume after the study",
        "--wake-date", wake_date.isoformat(),
        "--run", "run-42", "--agent", "claude",
    ]) == 0

    posted = events[3][1][-1]
    parsed = funnel.parse_park_comment(posted)
    assert parsed == {
        "reason": "Resume after the study",
        "wake_date": wake_date,
        "prior_status": "Ready",
    }
    assert funnel._visible_comment(posted).splitlines() == [
        "{}date={} status=Ready".format(
            funnel.PARK_WAKE_PREFIX, wake_date.isoformat()
        ),
        "{}Resume after the study".format(funnel.PARK_COMMENT_PREFIX),
    ]

    monkeypatch.setattr(
        funnel, "_issue_comments", lambda item: [{"body": posted}]
    )
    brief_item = funnel._parked_item_json(target)
    assert brief_item["reason"] == "Resume after the study"
    assert brief_item["wake_date"] == wake_date.isoformat()
    assert brief_item["wake_status"] == "Ready"
