"""Status and GitHub state contradicting each other (#1206, #1209)."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
REPO = "nateprich/beta"


def item(number, state, status, **kw):
    return funnel.Item(
        repo=REPO, number=number, title="issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state=state, status=status, status_since=NOW, **kw
    )


def test_a_closed_issue_at_ready_is_a_mismatch():
    """#1206's shape exactly: CLOSED and Ready at the same time."""
    rows = funnel.status_state_mismatches([item(1, "CLOSED", "Ready")])

    assert [row["ref"] for row in rows] == ["{}#1".format(REPO)]
    assert rows[0]["mismatch"] == "closed at Status Ready, which is not Done or Parked"


def test_an_open_issue_at_done_is_a_mismatch():
    rows = funnel.status_state_mismatches([item(2, "OPEN", "Done")])

    assert rows[0]["mismatch"] == "open at Status Done"


def test_closed_at_done_or_parked_is_not_a_mismatch():
    assert funnel.status_state_mismatches([
        item(3, "CLOSED", "Done"),
        item(4, "CLOSED", "Parked"),
    ]) == []


def test_ordinary_open_work_is_not_a_mismatch():
    assert funnel.status_state_mismatches([
        item(5, "OPEN", "Ideas"),
        item(6, "OPEN", "Shaped"),
        item(7, "OPEN", "Ready"),
        item(8, "OPEN", "Building"),
    ]) == []


def test_an_item_with_no_status_is_left_alone():
    """Unset Status is its own diagnostic (`needs_class` and the gate), not
    a contradiction between two recorded facts."""
    assert funnel.status_state_mismatches([item(9, "CLOSED", None)]) == []


def test_the_lane_filters_are_left_on_open():
    """A closed-at-Ready project must appear in this section and nowhere
    else — turning up as breakdown work is the failure being fixed."""
    closed_ready = item(10, "CLOSED", "Ready", klass="Broken")

    assert funnel.awaiting_breakdown([closed_ready]) == []
    assert funnel.startable([closed_ready]) == []
    assert len(funnel.status_state_mismatches([closed_ready])) == 1


def test_the_brief_carries_the_section(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(funnel, "_read_portfolio_metrics", lambda i, n: None)

    assert funnel.cmd_brief([item(11, "CLOSED", "Building")], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert [row["ref"] for row in brief["status_state_mismatches"]] == [
        "{}#11".format(REPO)
    ]
