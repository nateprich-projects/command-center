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


def test_start_command_is_rejected_by_argument_parsing(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main(["start", "8"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
