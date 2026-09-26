"""The explicit, reasoned transition from an active project to Parked."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
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


def test_park_with_wake_date_records_status_reason_and_instruction(monkeypatch):
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
    instruction = "Park this through the study, then resume on its wake date."

    assert funnel.main([
        "park", "42", "--reason", "Resume after the study",
        "--wake-date", wake_date.isoformat(),
        "--instruction", instruction,
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
    provenance = funnel.parse_provenance(posted)
    assert provenance["voice"] == "nate-relayed"
    assert provenance["instruction"] == instruction

    monkeypatch.setattr(
        funnel, "_issue_comments", lambda item: [{"body": posted}]
    )
    brief_item = funnel._parked_item_json(target)
    assert brief_item["reason"] == "Resume after the study"
    assert brief_item["wake_date"] == wake_date.isoformat()
    assert brief_item["wake_status"] == "Ready"


def _wake_candidate():
    return funnel.Item(
        repo="nateprich/beta",
        number=43,
        title="A project to resume",
        url="https://github.com/nateprich/beta/issues/43",
        state="CLOSED",
        state_reason="NOT_PLANNED",
        status="Parked",
        klass="New",
        children_total=1,
        children_done=1,
        item_id="project-item-43",
    )


def _wake_comment(wake_date, status="Building"):
    status_line = " status={}".format(status) if status is not None else ""
    return (
        "{}date={}{}\n{}Resume after the study".format(
            funnel.PARK_WAKE_PREFIX, wake_date, status_line,
            funnel.PARK_COMMENT_PREFIX,
        )
    )


def _wire_wake_writes(monkeypatch, item, comment, events):
    monkeypatch.setattr(
        funnel, "_issue_comments", lambda current: [{"body": comment}]
    )

    def run(args, capture_output, text=True):
        events.append(("gh", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        events.append(("graphql", variables))
        return {
            "updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id}
            }
        }

    monkeypatch.setattr(funnel, "_run_gh", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "building-option")
    monkeypatch.setattr(funnel, "live_issue_state", lambda current: current.state)


def test_park_wake_stays_parked_before_the_utc_date(monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    item = _wake_candidate()
    comment = _wake_comment("2026-09-24")
    events = []
    _wire_wake_writes(monkeypatch, item, comment, events)

    assert funnel.reconcile_parked_wakes([item], now) == []

    assert item.status == "Parked"
    assert item.state == "CLOSED"
    assert events == []
    assert funnel.gate_question(item) is None
    assert funnel.awaiting_decision([item]) == []


@pytest.mark.parametrize("wake_date", ["2026-09-23", "2026-09-22"])
def test_park_wake_restores_prior_status_on_and_after_the_utc_date(
    monkeypatch, wake_date
):
    now = datetime(2026, 9, 23, 23, 59, tzinfo=timezone.utc)
    item = _wake_candidate()
    comment = _wake_comment(wake_date)
    events = []
    _wire_wake_writes(monkeypatch, item, comment, events)

    assert funnel.reconcile_parked_wakes([item], now) == [item.ref]

    assert [event[0] for event in events] == ["gh", "graphql"]
    assert events[0][1] == (
        "gh", "issue", "reopen", "43", "--repo", "nateprich/beta"
    )
    assert events[1][1] == {
        "project": funnel.PROJECT_ID,
        "item": item.item_id,
        "field": funnel.STATUS_FIELD_ID,
        "option": "building-option",
    }
    assert item.state == "OPEN"
    assert item.state_reason == "REOPENED"
    assert item.status == "Building"
    parsed = funnel.parse_park_comment(comment)
    assert parsed["reason"] == "Resume after the study"
    assert funnel.gate_question(item) == "Accept it?"


def test_park_wake_without_prior_status_fails_closed(monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    item = _wake_candidate()
    comment = _wake_comment("2026-09-22", status=None)
    events = []
    _wire_wake_writes(monkeypatch, item, comment, events)

    assert funnel.reconcile_parked_wakes([item], now) == []

    assert item.status == "Parked"
    assert item.state == "CLOSED"
    assert events == []
