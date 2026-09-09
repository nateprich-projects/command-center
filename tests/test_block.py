"""The block-comment convention and its fail-closed parser."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def comment_item():
    return funnel.Item(
        repo="nateprich/beta", number=42, title="A ticket",
        url="https://github.com/nateprich/beta/issues/42", state="OPEN",
    )


def test_named_block_comment_returns_references_and_reason():
    assert funnel.parse_block_comment([
        "**Blocked on #84 and #90:** Wait for both decisions.",
    ]) == (["#84", "#90"], "Wait for both decisions.")


def test_block_comment_without_a_condition_returns_empty_references():
    assert funnel.parse_block_comment([
        "**Blocked:** Nate needs to decide whether this still matters.",
    ]) == ([], "Nate needs to decide whether this still matters.")


def test_latest_matching_block_comment_wins():
    assert funnel.parse_block_comment([
        "**Blocked on #84:** Older condition.",
        "An unrelated comment.",
        "**Blocked:** Newer decision needed.",
    ]) == ([], "Newer decision needed.")


def test_unparseable_block_comment_returns_no_condition():
    assert funnel.parse_block_comment([
        "**Blocked on #84, 2026-09-07.** Legacy format.",
    ]) is None


def test_embedded_block_prefix_does_not_match():
    assert funnel.parse_block_comment([
        "A sentence before **Blocked on #84:** is not a header.",
    ]) is None


def test_comment_posts_a_canonical_single_block_header(monkeypatch):
    monkeypatch.setattr(funnel, "load_items", lambda: [comment_item()])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--blocked-on", "77", "--because", "waiting on X",
        "--voice", "agent", "--run", "run-block", "--agent", "codex",
    ]) == 0

    posted = calls[0][-1]
    assert posted.startswith("**Blocked on #77:** waiting on X\n\n")
    assert funnel.parse_block_comment([posted])[0] == ["#77"]


def test_comment_joins_multiple_block_references(monkeypatch):
    monkeypatch.setattr(funnel, "load_items", lambda: [comment_item()])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--blocked-on", "77", "--blocked-on", "78",
        "--because", "waiting on both", "--voice", "agent",
        "--run", "run-block", "--agent", "codex",
    ]) == 0

    posted = calls[0][-1]
    assert posted.startswith("**Blocked on #77 and #78:** waiting on both\n\n")
    assert funnel.parse_block_comment([posted])[0] == ["#77", "#78"]


def test_blocked_comment_requires_because_before_loading_github(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(funnel, "load_items", lambda: called.append("loaded"))

    with pytest.raises(SystemExit) as exc:
        funnel.main([
            "comment", "42", "--blocked-on", "77", "--voice", "agent",
        ])

    assert exc.value.code != 0
    assert called == []
    assert "--because" in capsys.readouterr().err


def test_ordinary_body_comments_are_not_validated_as_block_comments(monkeypatch):
    monkeypatch.setattr(funnel, "load_items", lambda: [comment_item()])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--body", "**Blocked on #77, 2026-09-07.** legacy",
        "--voice", "agent", "--run", "run-body", "--agent", "codex",
    ]) == 0

    assert calls[0][-1].startswith(
        "**Blocked on #77, 2026-09-07.** legacy\n\n"
    )


def test_load_items_fetches_comments_only_for_open_blocked_items(monkeypatch):
    def node(number, repo="owner/repo", state="OPEN", labels=None):
        return {
            "status": {"name": "Ready"},
            "class": {"name": "New"},
            "content": {
                "number": number,
                "title": "issue {}".format(number),
                "url": "https://github.com/{}/issues/{}".format(repo, number),
                "state": state,
                "stateReason": None,
                "closedAt": None,
                "repository": {"nameWithOwner": repo},
                "labels": {"nodes": [{"name": label} for label in labels or []]},
                "assignees": {"nodes": []},
                "parent": None,
                "subIssuesSummary": {"total": 0, "completed": 0},
                "timelineItems": {"nodes": []},
            },
        }

    nodes = [
        node(1, labels=["blocked"]),
        node(2),
        node(3, state="CLOSED", labels=["blocked"]),
        node(4, repo="outside/repo", labels=["blocked"]),
    ]
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
        return {"comments": [
            {"body": "**Blocked on #84:** Wait for the decision."},
        ]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    items = funnel.load_items()

    assert [item.number for item in items] == [1, 2, 3]
    assert calls == [(
        "gh", "issue", "view", "1", "--repo", "owner/repo",
        "--json", "comments",
    )]
    blocked = items[0]
    assert blocked.block_references == ["#84"]
    assert blocked.block_reason == "Wait for the decision."
