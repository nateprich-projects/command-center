"""Open member issues that are in no Project item (#1169, #1213)."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
CC = "nateprich-projects/command-center"


def item(repo, number):
    return funnel.Item(
        repo=repo, number=number, title="known", url="", state="OPEN",
        status="Ready", klass="Broken", status_since=NOW,
    )


def _listing(monkeypatch, payloads):
    def fake(*args):
        repo = args[args.index("--repo") + 1]
        return payloads.get(repo)

    monkeypatch.setattr(funnel, "_gh_json", fake)


def test_an_issue_with_no_project_item_is_listed(monkeypatch):
    _listing(monkeypatch, {CC: [
        {"number": 900, "title": "Never added", "url": "u900", "parent": None},
        {"number": 901, "title": "Known", "url": "u901", "parent": None},
    ]})

    result = funnel.member_issues_without_project_items(
        [item(CC, 901)], repos=[CC]
    )

    assert result["status"] == "read"
    assert [row["ref"] for row in result["issues"]] == ["{}#900".format(CC)]
    assert result["issues"][0]["title"] == "Never added"


def test_the_watch_logs_are_excluded_by_number(monkeypatch):
    """They live as issues so the check-ins have somewhere to write; adding
    them to the Project would put a running commentary in the queue."""
    _listing(monkeypatch, {CC: [
        {"number": 579, "title": "Camping watch", "url": "u", "parent": None},
        {"number": 684, "title": "Funnel watch", "url": "u", "parent": None},
    ]})

    assert funnel.member_issues_without_project_items([], repos=[CC]) == {
        "status": "read", "issues": [],
    }


def test_794_sub_issues_are_excluded_by_parent(monkeypatch):
    """By parent, not by number, so the exclusion follows the breakdown."""
    _listing(monkeypatch, {CC: [
        {"number": 950, "title": "Phase work", "url": "u",
         "parent": {"number": 794}},
        {"number": 951, "title": "Unrelated", "url": "u",
         "parent": {"number": 800}},
    ]})

    result = funnel.member_issues_without_project_items([], repos=[CC])

    assert [row["ref"] for row in result["issues"]] == ["{}#951".format(CC)]


def test_an_unreadable_repo_reads_degraded_rather_than_empty(monkeypatch):
    """Zero orphans and an unread scan are the same shape and opposite news."""
    _listing(monkeypatch, {CC: None})

    result = funnel.member_issues_without_project_items([], repos=[CC])

    assert result["status"] == "degraded"
    assert CC in result["reason"]


def test_a_partial_scan_keeps_what_it_read_and_still_degrades(monkeypatch):
    other = "nateprich-projects/The-League"
    _listing(monkeypatch, {
        CC: [{"number": 900, "title": "Orphan", "url": "u", "parent": None}],
        other: None,
    })

    result = funnel.member_issues_without_project_items([], repos=[CC, other])

    assert result["status"] == "degraded"
    assert [row["ref"] for row in result["issues"]] == ["{}#900".format(CC)]


def test_an_unreadable_membership_read_degrades(monkeypatch):
    def explode():
        raise funnel.GitHubError("topic read offline")

    monkeypatch.setattr(funnel, "member_repos", explode)

    result = funnel.member_issues_without_project_items([])

    assert result["status"] == "degraded"
    assert "could not read member repositories" in result["reason"]


def test_jeffy_53_is_listed_rather_than_excluded_in_code(monkeypatch):
    """A known honest exception. An exclusion is a claim that something can
    never be wrong, and this one is judged each time it is seen."""
    jeffy = "nateprich-projects/jeffy-finance-agent"
    _listing(monkeypatch, {jeffy: [
        {"number": 53, "title": "Known exception", "url": "u", "parent": None},
    ]})

    result = funnel.member_issues_without_project_items([], repos=[jeffy])

    assert [row["ref"] for row in result["issues"]] == ["{}#53".format(jeffy)]


def test_the_brief_carries_the_section_and_is_null_without_the_read(
    monkeypatch, capsys
):
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(funnel, "_read_portfolio_metrics", lambda i, n: None)

    assert funnel.cmd_brief([], NOW) == 0
    assert json.loads(
        capsys.readouterr().out
    )["member_issues_without_project_items"] is None

    assert funnel.cmd_brief([], NOW, orphan_issues={
        "status": "read",
        "issues": [{"ref": "{}#900".format(CC), "repo": CC,
                    "title": "Orphan", "url": "u"}],
    }) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["member_issues_without_project_items"]["status"] == "read"
