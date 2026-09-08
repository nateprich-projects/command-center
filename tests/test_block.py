"""The block-comment convention and its fail-closed parser."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


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
