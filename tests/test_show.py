"""Fixture-only tests for the context printed by ``funnel show``."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_show_does_not_assert_comment_authorship(monkeypatch, capsys):
    item = Item(
        repo="nateprich/beta",
        number=7,
        title="A project",
        url="https://github.com/nateprich/beta/issues/7",
        state="OPEN",
        status="Ideas",
        status_since=NOW,
    )

    calls = []

    def gh_json(*args):
        calls.append(args)
        assert args == (
            "gh", "issue", "view", "7", "--repo", "nateprich/beta",
            "--json", "comments",
        )
        return {
            "comments": [{
                "author": {"login": "nateprich"},
                "body": "A comment written under Nate's account.",
            }],
        }

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    assert funnel.cmd_show([item], NOW, item.ref) == 0

    output = capsys.readouterr().out
    assert "  UNATTRIBUTED: A comment written under Nate's account." in output
    assert "nateprich: A comment written under Nate's account." not in output
    assert len(calls) == 1


def test_show_does_not_offer_a_start_gate(monkeypatch, capsys):
    item = Item(
        repo="nateprich/beta",
        number=8,
        title="A ready project",
        url="https://github.com/nateprich/beta/issues/8",
        state="OPEN",
        status="Ready",
        status_since=NOW,
        children_total=0,
    )

    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})

    assert funnel.cmd_show([item], NOW, item.ref) == 0

    output = capsys.readouterr().out
    assert "Start now?" not in output
    assert "Answer it:" not in output


def test_show_carries_the_breakdown_question(monkeypatch, capsys):
    item = Item(
        repo="nateprich/beta",
        number=9,
        title="A project needing an answer",
        url="https://github.com/nateprich/beta/issues/9",
        state="OPEN",
        status="Ready",
        status_since=NOW,
        labels=["blocked"],
        needs_decision="Where should this connector live?",
    )

    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})

    assert funnel.cmd_show([item], NOW, item.ref) == 0

    output = capsys.readouterr().out
    assert "GATE: Answer the breakdown's question?" in output
    assert "NEEDS DECISION: Where should this connector live?" in output


def test_show_marks_missing_closed_tickets_unknown_on_truncated_scan(
    monkeypatch, capsys
):
    item = Item(
        repo="nateprich/beta",
        number=10,
        title="A project with old tickets",
        url="https://github.com/nateprich/beta/issues/10",
        state="OPEN",
        status="Ready",
        status_since=NOW,
        children_total=2,
        children_done=2,
    )
    children = [
        {
            "number": 101,
            "title": "An older merged ticket",
            "state": "CLOSED",
            "repository": {"nameWithOwner": "nateprich/beta"},
        },
        {
            "number": 102,
            "title": "Another older merged ticket",
            "state": "CLOSED",
            "repository": {"nameWithOwner": "nateprich/beta"},
        },
    ]

    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: {
            "repository": {"issue": {"subIssues": {"nodes": children}}}
        },
    )
    monkeypatch.setattr(
        funnel, "ticket_pr_index", lambda repo: ({}, True)
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})

    assert funnel.cmd_show([item], NOW, item.ref) == 0

    output = capsys.readouterr().out
    assert output.count(
        "PR state unknown -- ticket-PR scan truncated at 100 rows"
    ) == 2
    assert "closed with no ticket/* PR -- check why" not in output


def test_show_keeps_exact_complete_scan_paths(monkeypatch, capsys):
    item = Item(
        repo="nateprich/beta",
        number=11,
        title="A project with current tickets",
        url="https://github.com/nateprich/beta/issues/11",
        state="OPEN",
        status="Ready",
        status_since=NOW,
        children_total=2,
        children_done=2,
    )
    children = [
        {
            "number": 201,
            "title": "A ticket with its PR",
            "state": "CLOSED",
            "repository": {"nameWithOwner": "nateprich/beta"},
        },
        {
            "number": 202,
            "title": "A ticket without a ticket branch",
            "state": "CLOSED",
            "repository": {"nameWithOwner": "nateprich/beta"},
        },
    ]

    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: {
            "repository": {"issue": {"subIssues": {"nodes": children}}}
        },
    )
    monkeypatch.setattr(
        funnel,
        "ticket_pr_index",
        lambda repo: (
            {
                "nateprich/beta#201": {
                    "number": 301,
                    "state": "MERGED",
                    "mergedAt": "2026-09-01T00:00:00Z",
                    "reviews": [],
                }
            },
            False,
        ),
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})

    assert funnel.cmd_show([item], NOW, item.ref) == 0

    output = capsys.readouterr().out
    assert "PR #301 merged (merged)" in output
    assert "closed with no ticket/* PR -- check why" in output
    assert "PR state unknown -- ticket-PR scan truncated" not in output


def test_start_command_is_rejected_by_argument_parsing(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main(["start", "8"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
