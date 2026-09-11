"""Guard the funnel's fixture-load API shape against new per-item reads."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"


def _node(number, *, parent=None, labels=(), blocked_by=()):
    return {
        "status": {"name": "Building"},
        "class": {"name": "New"},
        "content": {
            "number": number,
            "title": "issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(REPO, number),
            "state": "OPEN",
            "stateReason": None,
            "closedAt": None,
            "repository": {"nameWithOwner": REPO},
            "labels": {"nodes": [{"name": label} for label in labels]},
            "assignees": {"nodes": []},
            "parent": (
                {"number": parent, "repository": {"nameWithOwner": REPO}}
                if parent is not None
                else None
            ),
            "subIssuesSummary": {"total": 0, "completed": 0},
            "blockedBy": {"nodes": list(blocked_by)},
            "timelineItems": {"nodes": []},
        },
    }


def _measure_fixture_load(monkeypatch, item_count):
    """Load a small or large board with the same number of pages and blockers."""
    with monkeypatch.context() as patch:
        blocker = {
            "number": 84,
            "state": "OPEN",
            "stateReason": None,
            "repository": {"nameWithOwner": REPO},
        }
        nodes = [_node(1)]
        nodes.append(_node(2, parent=1, labels=("blocked",), blocked_by=(blocker,)))
        nodes.extend(
            _node(number, parent=1)
            for number in range(3, item_count + 1)
        )

        calls = {"graphql": [], "json": []}

        def gh_graphql(*args, **kwargs):
            calls["graphql"].append((args, kwargs))
            return {
                "user": {
                    "projectV2": {
                        "items": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                    }
                }
            }

        def gh_json(*args):
            calls["json"].append(args)
            if (
                args[:3] == ("gh", "issue", "view")
                and args[3] == "2"
                and args[-1] == "comments"
            ):
                return {
                    "comments": [
                        {"body": "**Blocked on #84:** Wait for the decision."}
                    ]
                }
            if args[:3] == ("gh", "pr", "list"):
                return []
            # Keep unexpected reads countable. A reintroduced dependency or
            # per-ticket PR lookup must make the 10/100 counts diverge.
            return []

        patch.setattr(funnel, "member_repos", lambda: [REPO])
        patch.setattr(funnel, "gh_graphql", gh_graphql)
        patch.setattr(funnel, "_gh_json", gh_json)

        items = funnel.load_items()
        funnel.ticket_pr_facts(items)

        assert len(items) == item_count
        blocked = next(item for item in items if item.number == 2)
        assert blocked.block_references == ["#84"]
        assert sum(
            1
            for args in calls["json"]
            if args[:3] == ("gh", "issue", "view")
        ) == 1
        assert sum(
            1
            for args in calls["json"]
            if args[:3] == ("gh", "pr", "list")
        ) == 1
        return {kind: len(entries) for kind, entries in calls.items()}


def test_fixture_load_api_calls_do_not_scale_with_item_count(monkeypatch):
    """One page and one blocked item cost the same for 10 and 100 items.

    This exercises the three historical N+1 shapes together: ``load_items``
    reads the Project page, its one open blocked item goes through
    ``_load_block_comment``, and ``ticket_pr_facts`` exercises the replacement
    for the old per-candidate ``_ticket_pr`` lookup.
    """
    counts = {
        size: _measure_fixture_load(monkeypatch, size)
        for size in (10, 100)
    }

    assert counts[10] == counts[100], (
        "fixture-load API calls must be bounded by pages and fixed work, "
        "not by item count"
    )
