"""Native issue dependency reads used by the item loader."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def test_load_items_attaches_dependencies_to_open_tickets(monkeypatch):
    """Dependencies ride in on the Project query, costing no extra request.

    The loader used to call the REST endpoint once per open ticket, which
    exhausted the API budget on 2026-09-08 and took `funnel brief` down. The
    `calls == []` assertion below is the guard: a per-item read reintroduced
    into `load_items` fails here.
    """

    def node(number, *, parent=None, state="OPEN", blocked_by=()):
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
                "blockedBy": {
                    "totalCount": len(blocked_by),
                    "nodes": list(blocked_by),
                },
                "timelineItems": {"nodes": []},
            },
        }

    # GraphQL's spelling, which is not REST's: upper-case state, `stateReason`,
    # `nameWithOwner`. Feeding the real wire shape is the point of the fixture.
    blocker = {
        "number": 9,
        "state": "OPEN",
        "stateReason": None,
        "repository": {"nameWithOwner": "owner/repo"},
    }
    dead = {
        "number": 10,
        "state": "CLOSED",
        "stateReason": "NOT_PLANNED",
        "repository": {"nameWithOwner": "other/repo"},
    }

    nodes = [
        node(1),
        node(2, parent=1, blocked_by=[blocker, dead]),
        node(3, parent=1, state="CLOSED", blocked_by=[blocker]),
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
        return [{"number": 9, "state": "open"}]

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    items = funnel.load_items()

    assert [item.open_blockers for item in items] == [[], ["owner/repo#9"], []]
    assert [item.dead_blockers for item in items] == [[], ["other/repo#10"], []]
    assert [item.blocked_by_refs for item in items] == [
        [], ["owner/repo#9", "other/repo#10"], ["owner/repo#9"]
    ]
    assert calls == []


def test_blocked_by_refs_require_a_complete_connection():
    item_node = {
        "content": {
            "number": 1,
            "title": "issue 1",
            "url": "https://github.com/owner/repo/issues/1",
            "state": "OPEN",
            "repository": {"nameWithOwner": "owner/repo"},
            "labels": {"nodes": []},
            "assignees": {"nodes": []},
            "blockedBy": {"totalCount": 2, "nodes": [{
                "number": 9,
                "repository": {"nameWithOwner": "owner/repo"},
            }]},
        }
    }
    item = funnel._from_node(item_node)
    assert item.blocked_by_refs is None


def test_project_query_requests_a_complete_blocked_by_page():
    assert "blockedBy(first: 50)" in funnel.ITEM_QUERY
    assert "blockedBy(first: 50) {\n                totalCount" in funnel.ITEM_QUERY
