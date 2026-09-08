"""Native issue dependency reads used by the item loader."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def test_open_blockers_returns_refs_for_open_issues_only(monkeypatch):
    calls = []

    def gh_json(*args):
        calls.append(args)
        return [
            {
                "number": 42,
                "state": "open",
                "repository": {"full_name": "other/repo"},
            },
            {
                "number": 43,
                "state": "closed",
                "repository": {"full_name": "owner/repo"},
            },
        ]

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    assert funnel.open_blockers("owner/repo", 7) == ["other/repo#42"]
    assert calls == [("gh", "api", "repos/owner/repo/issues/7/dependencies/blocked_by")]


def test_open_blockers_returns_none_for_a_ticket_without_dependencies(monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: [])

    assert funnel.open_blockers("owner/repo", 7) == []


def test_load_items_attaches_dependencies_to_open_tickets(monkeypatch):
    def node(number, *, parent=None, state="OPEN"):
        return {
            "status": {"name": "Building"},
            "class": {"name": "New"},
            "content": {
                "number": number,
                "title": "issue {}".format(number),
                "url": "https://github.com/owner/repo/issues/{}".format(number),
                "state": state,
                "stateReason": None,
                "closedAt": None,
                "repository": {"nameWithOwner": "owner/repo"},
                "labels": {"nodes": []},
                "assignees": {"nodes": []},
                "parent": (
                    {"number": parent, "repository": {"nameWithOwner": "owner/repo"}}
                    if parent is not None
                    else None
                ),
                "subIssuesSummary": {"total": 0, "completed": 0},
                "timelineItems": {"nodes": []},
            },
        }

    nodes = [node(1), node(2, parent=1), node(3, parent=1, state="CLOSED")]
    monkeypatch.setattr(funnel, "member_repos", lambda: ["owner/repo"])
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: {
            "user": {"projectV2": {"items": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}
        },
    )
    calls = []

    def gh_json(*args):
        calls.append(args)
        return [{"number": 9, "state": "open"}]

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    items = funnel.load_items()

    assert [item.open_blockers for item in items] == [[], ["owner/repo#9"], []]
    assert calls == [
        ("gh", "api", "repos/owner/repo/issues/2/dependencies/blocked_by"),
    ]


def test_open_blockers_fails_closed_when_the_endpoint_cannot_be_read(monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)

    with pytest.raises(funnel.GitHubError, match="could not read blockers"):
        funnel.open_blockers("owner/repo", 7)
