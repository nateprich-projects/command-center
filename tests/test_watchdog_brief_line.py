"""A live watchdog-owned alert surfaces as a must-answer brief line (#2501).

The alert issues sit outside the Project, where nothing acts on them. The
fix derives one funnel-watch line per live owned alert into ``watch_gates``
— the list the funnel watch answers itself — with no Project item, Class,
or gate.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
CC = "nateprich-projects/command-center"


def _load_watchdog():
    path = (
        pathlib.Path(__file__).resolve().parent.parent
        / ".github" / "scripts" / "watchdog.py"
    )
    spec = importlib.util.spec_from_file_location("watchdog_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alert_row(number=2034, title="Scheduled runs need attention"):
    return {
        "ref": "{}#{}".format(CC, number),
        "repo": CC,
        "title": title,
        "url": "https://github.com/{}/issues/{}".format(CC, number),
    }


def orphans_with(*rows):
    return {"status": "read", "issues": list(rows)}


def owned_body(row):
    assert row["title"].startswith(funnel.WATCHDOG_ALERT_TITLE)
    return "\n".join([funnel.WATCHDOG_MARKER, "", "The watchdog found problems."])


@pytest.fixture(autouse=True)
def offline_brief(monkeypatch):
    """Keep ``cmd_brief`` on the fixture rows: no GitHub, no heartbeat."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", lambda items, now: None
    )
    monkeypatch.setattr(
        funnel,
        "decline_routing_metric",
        lambda items, now: {"status": "available", "declines": 0},
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    yield
    funnel.reset_api_usage()


def brief_for(items, capsys, **kwargs):
    assert funnel.cmd_brief(items, NOW, **kwargs) == 0
    return json.loads(capsys.readouterr().out)


def test_shared_title_and_marker_match_the_watchdog_script():
    """The brief recognises alerts with the watchdog's own strings, not a
    second copy that can drift from ``.github/scripts/watchdog.py``."""
    watchdog = _load_watchdog()
    assert funnel.WATCHDOG_ALERT_TITLE == watchdog.ALERT_TITLE
    assert funnel.WATCHDOG_MARKER == watchdog.MARKER


def test_live_owned_alert_emits_a_must_answer_line(capsys):
    """Reproduction: a live owned alert is a line the funnel watch must
    answer — on ``watch_gates``, never in Nate's list or total."""
    confirmed = funnel.live_watchdog_alerts(
        orphans_with(alert_row()), owned_body
    )
    assert [row["ref"] for row in confirmed] == ["{}#2034".format(CC)]

    brief = brief_for([], capsys, watchdog_alerts=confirmed)

    assert brief["items"] == []
    assert brief["total_needing_nate"] == 0
    assert brief["watch_gates"] == [{
        "ref": "{}#2034".format(CC),
        "title": "Scheduled runs need attention",
        "url": "https://github.com/{}/issues/2034".format(CC),
        "class": "Broken",
        "question": "Recover the scheduled runs?",
        "waited": "unknown",
    }]


def test_title_without_marker_confirms_nothing():
    """A foreign issue sharing the alert title is not watchdog-owned."""
    foreign = alert_row(number=9999)
    assert funnel.live_watchdog_alerts(
        orphans_with(foreign), lambda row: "no marker here"
    ) == []


def test_marker_without_title_confirms_nothing():
    """A quoted marker on an unrelated issue is not an alert."""
    unrelated = alert_row(number=1252, title="Unrelated work")
    assert funnel.live_watchdog_alerts(
        orphans_with(unrelated), lambda row: funnel.WATCHDOG_MARKER
    ) == []


def test_unreadable_body_confirms_nothing():
    """An alert whose body cannot be read stays listed only as an orphan,
    as today — never a derived line on a guess."""
    assert funnel.live_watchdog_alerts(
        orphans_with(alert_row()), lambda row: None
    ) == []


def test_no_live_alert_emits_no_watchdog_line(capsys):
    """A cleared alert (or a scan with no alert) leaves no must-answer
    line behind."""
    assert funnel.live_watchdog_alerts(
        orphans_with(alert_row(number=900, title="Never added")), owned_body
    ) == []

    for kwargs in ({}, {"watchdog_alerts": []}, {"watchdog_alerts": None}):
        assert brief_for([], capsys, **kwargs)["watch_gates"] == []


def test_degraded_scan_emits_no_watchdog_line():
    assert funnel.live_watchdog_alerts(
        {"status": "degraded", "reason": "unread", "issues": []}, owned_body
    ) == []
    assert funnel.live_watchdog_alerts(None, owned_body) == []
