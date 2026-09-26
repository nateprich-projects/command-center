"""Guard the funnel's fixture-load API shape against new per-item reads."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"


def _node(
    number, *, parent=None, labels=(), blocked_by=(), children_total=0
):
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
            "subIssuesSummary": {
                "total": children_total,
                "completed": 0,
            },
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
            if "pullRequests(first:" in args[0]:
                return {
                    "rateLimit": {
                        "cost": 1, "remaining": 99, "resetAt": "later"
                    },
                    "repo0": {
                        "pullRequests": {
                            "nodes": [],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        },
                        "refs": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False},
                        },
                    },
                }
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
            1 for query, _variables in calls["graphql"]
            if "pullRequests(first:" in query[0]
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


def test_project_item_query_uses_maximum_bounded_page():
    """The Project read uses one bounded request for every 100 items."""
    compact = " ".join(funnel.ITEM_QUERY.split())

    assert "items(first: 100, after: $cursor)" in compact
    assert funnel.PROJECT_ITEM_PAGE_SIZE == 100


def test_begin_load_adds_phase_durations_to_the_existing_timings_map(
    monkeypatch,
):
    """Begin instrumentation reports phases without changing the query path."""
    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda query, **variables: {
            "user": {
                "projectV2": {
                    "items": {
                        "nodes": [],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        },
    )
    timings = {}

    assert funnel.load_items(include_details=False, timings=timings) == []

    assert set(timings) == {
        "begin_load.member_repos",
        "begin_load.project_items",
        "begin_load.block_comments",
    }
    assert all(isinstance(value, float) and value >= 0 for value in timings.values())


def test_doctor_reports_same_count_pagination_saving():
    """The doctor quotes #655 and measures the first:100 reduction."""
    result = funnel.check_project_pagination(319, 4)

    assert result == funnel.Check(
        "Project pagination", True,
        "319 Project row(s) used 4 page request(s) at first:100; first:50 "
        "would require 7 page request(s) for the same count "
        "(3 fewer, 42.9% fewer); #655 before: 42 API calls and 47 "
        "GraphQL points",
        "",
    )


def test_project_item_list_is_compact_and_detail_read_is_candidate_bounded(
    monkeypatch,
):
    """The #655 42-call/47-point before number is not paid for every row.

    A 100-item paged list carries no child or timeline connections. Hydrating
    one candidate asks for one detail id, so the nested payload stays bounded
    as the board grows.
    """
    nodes = [_node(1, children_total=1)]
    nodes.extend(_node(number, parent=1) for number in range(2, 101))
    for node in nodes:
        node["id"] = "project-item-{}".format(node["content"]["number"])

    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if "history: nodes(ids:" in query:
            assert variables == {
                "ids": ["project-item-1"],
                "childIds": ["project-item-1"],
            }
            return {
                "history": [{
                    "id": "project-item-1",
                    "content": {
                        "timelineItems": {
                            "nodes": [{
                                "__typename": "ProjectV2ItemStatusChangedEvent",
                                "createdAt": "2026-09-09T00:00:00Z",
                                "previousStatus": "Ready",
                                "status": "Building",
                                "project": {"number": funnel.PROJECT_NUMBER},
                            }]
                        },
                    },
                }],
                "children": [{
                    "id": "project-item-1",
                    "content": {
                        "subIssues": {
                            "nodes": [{
                                "createdAt": "2026-09-10T00:00:00Z",
                                "closedAt": None,
                            }]
                        },
                    },
                }]
            }
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

    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    items = funnel.load_items(include_details=False)
    assert len(items) == 100
    assert len(calls) == 1
    list_query = " ".join(calls[0][0].split())
    assert "subIssues(" not in list_query
    assert "timelineItems(" not in list_query

    funnel.hydrate_item_details(items, [items[0]])

    assert len(calls) == 2
    detail_query = " ".join(calls[1][0].split())
    assert "subIssues(first: 50)" in detail_query
    assert "timelineItems(last: 60" in detail_query
    assert "history: nodes(ids: $ids)" in detail_query
    assert "children: nodes(ids: $childIds)" in detail_query
    assert items[0].first_child_created_at is not None
    assert items[0].status_since is not None


def test_detail_query_only_requests_child_times_for_items_with_children(
    monkeypatch,
):
    parent_numbers = {1, 50}
    nodes = [
        _node(number, children_total=(1 if number in parent_numbers else 0))
        for number in range(1, 101)
    ]
    for node in nodes:
        node["id"] = "project-item-{}".format(node["content"]["number"])
    items = [funnel._from_node(node) for node in nodes]
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        return {
            "history": [
                {
                    "id": item_id,
                    "content": {
                        "timelineItems": {
                            "nodes": [{
                                "__typename": "ProjectV2ItemStatusChangedEvent",
                                "createdAt": "2026-09-09T00:00:00Z",
                                "previousStatus": "Ready",
                                "status": "Building",
                                "project": {"number": funnel.PROJECT_NUMBER},
                            }]
                        }
                    },
                }
                for item_id in variables["ids"]
            ],
            "children": [
                {
                    "id": item_id,
                    "content": {
                        "subIssues": {
                            "nodes": [{
                                "createdAt": "2026-09-10T00:00:00Z",
                                "closedAt": None,
                            }]
                        }
                    },
                }
                for item_id in variables["childIds"]
            ],
        }

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    funnel.hydrate_item_details(items)

    assert len(calls) == 1
    query, variables = calls[0]
    assert len(variables["ids"]) == 100
    assert variables["childIds"] == ["project-item-1", "project-item-50"]
    assert "subIssues(first: 50)" in query
    assert "timelineItems(last: 60" in query
    assert all(item.status_since is not None for item in items)
    assert items[0].first_child_created_at is not None
    assert items[49].first_child_created_at is not None
    assert items[1].first_child_created_at is None


def test_item_detail_request_assembles_combined_batch_document():
    query, variables, history_field, child_field = funnel._item_detail_request(
        ["project-item-1", "project-item-2", "project-item-3"],
        ["project-item-1"],
    )

    assert query == funnel.ITEM_DETAILS_QUERY
    assert variables == {
        "ids": ["project-item-1", "project-item-2", "project-item-3"],
        "childIds": ["project-item-1"],
    }
    assert history_field == "history"
    assert child_field == "children"
    assert "history: nodes(ids: $ids)" in query
    assert "children: nodes(ids: $childIds)" in query


def test_detail_batches_keep_child_nodes_with_their_history_batch(monkeypatch):
    nodes = [
        _node(number, children_total=(1 if number == 101 else 0))
        for number in range(1, 102)
    ]
    for node in nodes:
        node["id"] = "project-item-{}".format(node["content"]["number"])
    items = [funnel._from_node(node) for node in nodes]
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if query == funnel.ITEM_DETAILS_QUERY:
            return {
                "history": [],
                "children": [{
                    "id": item_id,
                    "content": {"subIssues": {"nodes": []}},
                } for item_id in variables["childIds"]],
            }
        return {"nodes": []}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    funnel.hydrate_item_details(items)

    assert len(calls) == 2
    assert calls[0][0] == funnel.ITEM_TIMELINE_DETAILS_QUERY
    assert calls[0][1]["ids"] == [
        "project-item-{}".format(number) for number in range(1, 101)
    ]
    assert calls[1][0] == funnel.ITEM_DETAILS_QUERY
    assert calls[1][1] == {
        "ids": ["project-item-101"],
        "childIds": ["project-item-101"],
    }


def test_timeline_only_detail_query_handles_batches_without_children(
    monkeypatch,
):
    nodes = [_node(number) for number in range(1, 3)]
    for node in nodes:
        node["id"] = "project-item-{}".format(node["content"]["number"])
    items = [funnel._from_node(node) for node in nodes]
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        assert query == funnel.ITEM_TIMELINE_DETAILS_QUERY
        assert variables["ids"] == ["project-item-1", "project-item-2"]
        return {
            "nodes": [
                {
                    "id": item_id,
                    "content": {
                        "timelineItems": {
                            "nodes": [{
                                "__typename": "ProjectV2ItemStatusChangedEvent",
                                "createdAt": "2026-09-09T00:00:00Z",
                                "previousStatus": "Ready",
                                "status": "Building",
                                "project": {"number": funnel.PROJECT_NUMBER},
                            }]
                        }
                    },
                }
                for item_id in variables["ids"]
            ]
        }

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    funnel.hydrate_item_details(items)

    assert len(calls) == 1
    assert "subIssues(" not in calls[0][0]
    assert all(item.status_since is not None for item in items)


def test_time_at_gate_uses_latest_current_project_status_event(monkeypatch):
    node = _node(1)
    node["id"] = "project-item-1"
    item = funnel._from_node(node)
    events = [
        {
            "__typename": "ProjectV2ItemStatusChangedEvent",
            "createdAt": "2026-09-11T00:00:00Z",
            "previousStatus": "Ready",
            "status": "Building",
            "project": {"number": funnel.PROJECT_NUMBER + 1},
        },
        {
            "__typename": "ProjectV2ItemStatusChangedEvent",
            "createdAt": "2026-09-08T00:00:00Z",
            "previousStatus": "Ready",
            "status": "Building",
            "project": {"number": funnel.PROJECT_NUMBER},
        },
        {
            "__typename": "ProjectV2ItemStatusChangedEvent",
            "createdAt": "2026-09-10T00:00:00Z",
            "previousStatus": "Building",
            "status": "Ready",
            "project": {"number": funnel.PROJECT_NUMBER},
        },
        {
            "__typename": "ProjectV2ItemStatusChangedEvent",
            "createdAt": "2026-09-09T00:00:00Z",
            "previousStatus": "Ready",
            "status": "Building",
            "project": {"number": funnel.PROJECT_NUMBER},
        },
    ]

    def graphql(query, **variables):
        assert query == funnel.ITEM_TIMELINE_DETAILS_QUERY
        assert variables["ids"] == ["project-item-1"]
        return {
            "nodes": [{
                "id": "project-item-1",
                "content": {"timelineItems": {"nodes": events}},
            }]
        }

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    funnel.hydrate_item_details([item])

    assert item.status_since == funnel.parse_time("2026-09-09T00:00:00Z")
    assert [event["at"] for event in item.status_events] == [
        funnel.parse_time("2026-09-08T00:00:00Z"),
        funnel.parse_time("2026-09-10T00:00:00Z"),
        funnel.parse_time("2026-09-09T00:00:00Z"),
    ]


def test_load_items_follows_the_cursor_after_a_full_page(monkeypatch):
    """The larger page does not drop items when the Project still continues."""
    funnel.reset_api_usage()
    pages = [
        [_node(number) for number in range(1, 101)],
        [_node(101)],
    ]
    calls = []

    def graphql(query, **variables):
        calls.append(variables)
        page = pages.pop(0)
        return {
            "user": {
                "projectV2": {
                    "items": {
                        "nodes": page,
                        "pageInfo": {
                            "hasNextPage": bool(pages),
                            "endCursor": "cursor-1" if pages else None,
                        },
                    }
                }
            }
        }

    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    items = funnel.load_items()

    assert [item.number for item in items] == list(range(1, 102))
    assert funnel.project_item_load_measurement() == (2, 101)
    assert calls == [
        {"login": funnel.PROJECT_OWNER, "number": funnel.PROJECT_NUMBER},
        {
            "login": funnel.PROJECT_OWNER,
            "number": funnel.PROJECT_NUMBER,
            "cursor": "cursor-1",
        },
    ]


def test_shape_item_query_reads_and_paginates_the_full_issue_thread(monkeypatch):
    node = _node(42)
    node["content"]["body"] = "Current idea body."
    first = {
        "author": {"login": "nate"},
        "body": "First comment.",
        "createdAt": "2026-09-25T01:00:00Z",
    }
    second = {
        "author": {"login": "muse"},
        "body": "Second comment.",
        "createdAt": "2026-09-25T02:00:00Z",
    }
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if "shapeIssue:" in query:
            return {
                "user": {"projectV2": {"items": {
                    "nodes": [node],
                    "pageInfo": {"hasNextPage": False,
                                 "endCursor": None},
                }}},
                "shapeIssue": {"issue": {"comments": {
                    "nodes": [first],
                    "pageInfo": {"hasNextPage": True,
                                 "endCursor": "comment-cursor-1"},
                }}},
            }
        assert query == funnel.SHAPE_ISSUE_COMMENTS_PAGE_QUERY
        assert variables == {
            "owner": "owner", "name": "repo", "number": 42,
            "cursor": "comment-cursor-1",
        }
        return {"repository": {"issue": {"comments": {
            "nodes": [second],
            "pageInfo": {"hasNextPage": False,
                         "endCursor": "comment-cursor-2"},
        }}}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    items = funnel.load_items(
        include_details=False, member_repo_names=[REPO],
        shape_issue=(REPO, 42),
    )
    assert len(calls) == 2
    first_query = " ".join(calls[0][0].split())
    assert "content { ... on Issue { number title url body state" in first_query
    assert 'shapeIssue: repository(owner: "owner", name: "repo")' \
        in first_query
    assert items[0].issue_comments == [first, second]


def test_shape_item_query_fails_closed_when_thread_is_unreadable(monkeypatch):
    node = _node(42)
    response = {
        "user": {"projectV2": {"items": {
            "nodes": [node],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}
    }
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: response)
    with pytest.raises(funnel.GitHubError, match="could not read comments"):
        funnel.load_items(
            include_details=False, member_repo_names=[REPO],
            shape_issue=(REPO, 42),
        )
