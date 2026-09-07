"""Fixture-only tests for the context printed by ``funnel show``."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

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
