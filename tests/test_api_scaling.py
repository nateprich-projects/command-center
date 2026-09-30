"""Guard the funnel's fixture-load API shape against new per-item reads."""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

#: Block comments count only from the owner account (#1788).
OWNER = {"login": "nateprich"}


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
                        {"author": OWNER,
                         "body": "**Blocked on #84:** Wait for the decision."}
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
        # Two: open rows with their comment tails, and the closed-and-merged
        # history without them (#1986). Both are bounded by pages, not items.
        assert sum(
            1 for query, _variables in calls["graphql"]
            if "pullRequests(first:" in query[0]
        ) == 2
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


def test_shared_startable_candidates_are_filtered_before_hydration(monkeypatch):
    project = _node(1, children_total=1)
    ticket = _node(2, parent=1)
    for number, node in enumerate((project, ticket), start=1):
        node["id"] = "project-item-{}".format(number)
        node["needs"] = {"name": "none"}
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if "nodes(ids: $ids)" in query:
            assert variables == {"ids": ["project-item-2"]}
            return {
                "nodes": [{
                    "id": "project-item-2",
                    "content": {"timelineItems": {"nodes": []}},
                }],
            }
        if "open: items(" in query:
            aliases = re.findall(r"(\w+): items\(", query)
            return {
                "user": {
                    "projectV2": {
                        alias: {
                            "nodes": [project, ticket] if alias == "open" else [],
                            "pageInfo": {
                                "hasNextPage": False,
                                "endCursor": None,
                            },
                        }
                        for alias in aliases
                    },
                },
            }
        return {
            "user": {
                "projectV2": {
                    "items": {
                        "nodes": [project, ticket],
                        "pageInfo": {
                            "hasNextPage": False,
                            "endCursor": None,
                        },
                    },
                },
            },
        }

    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_load_begin_anchor_items", lambda *args: [])

    items = funnel.load_items(include_startable=True)

    assert [item.number for item in items.startable_items] == [1, 2]
    assert [item.number for item in items.startable_candidates] == [2]
    assert items.startable_candidates[0] is items.startable_items[1]
    assert funnel.STARTABLE_ITEM_NODE_FIELDS in calls[1][0]
    detail_calls = [
        variables for query, variables in calls
        if "nodes(ids: $ids)" in query
    ]
    assert [variables["ids"] for variables in detail_calls] == [
        ["project-item-2"]
    ]


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


def _item(number, *, parent=None):
    """One Item as ``ticket_pr_facts`` selects it: a ticket under a parent."""
    return funnel.Item(
        number=number,
        title="issue {}".format(number),
        url="https://github.com/{}/issues/{}".format(REPO, number),
        repo=REPO,
        state="OPEN",
        status="Building",
        parent=parent,
    )


def _pr_row(number, *, state, created_at, ticket=11, comments=None):
    row = {
        "number": number,
        "state": state,
        "createdAt": created_at,
        "headRefName": "ticket/{}".format(ticket),
        "headRefOid": "sha-{}".format(number),
        "author": OWNER,
        "isCrossRepository": False,
        "headRepository": {"nameWithOwner": REPO},
    }
    if comments is not None:
        row["comments"] = comments
    return row


def _split_pr_scan(monkeypatch, rows_by_state, *, branches=()):
    """Record every batched read ``ticket_pr_facts`` makes, and answer it."""
    reads = []

    def read_batched(repos, **kwargs):
        reads.append({"states": tuple(kwargs["states"]), **kwargs})
        rows = tuple(
            row for state in kwargs["states"]
            for row in rows_by_state.get(state, ())
        )
        return funnel.BatchedPRRead(
            rows_by_repo={repo: rows for repo in repos},
            branch_refs_by_repo={
                repo: set(branches) for repo in repos
            } if kwargs.get("include_refs") else {repo: set() for repo in repos},
            pr_truncated_by_repo={repo: False for repo in repos},
            branches_truncated_by_repo={repo: False for repo in repos},
        )

    monkeypatch.setattr(funnel, "_read_batched_pr_snapshots", read_batched)
    return reads


def test_the_pr_scan_asks_for_comment_tails_on_open_rows_only(monkeypatch):
    """#1985: the all-states page carried 100 comment bodies per PR.

    That request returned about 2 MB, and HTTP 502 or 504 on two attempts in
    three on the board of 2026-09-29, so the brief's 35 s budget tripped and
    every open ticket drew the unknown pip. Verdicts are read from open rows
    only, so the history page's tails had no reader.
    """
    reads = _split_pr_scan(monkeypatch, {})

    funnel.ticket_pr_facts([_item(11, parent=10)])

    assert [read["states"] for read in reads] == [
        ("OPEN",), ("CLOSED", "MERGED")
    ]
    by_states = {read["states"]: read for read in reads}
    assert by_states[("OPEN",)]["include_comments"] is True
    assert by_states[("CLOSED", "MERGED")]["include_comments"] is False
    # Branch refs ride the open read; asking twice would double the cost.
    assert by_states[("OPEN",)]["include_refs"] is True
    assert by_states[("CLOSED", "MERGED")]["include_refs"] is False


def test_the_split_pr_scan_keeps_the_newest_row_first(monkeypatch):
    """The merged snapshot stays in CREATED_AT descending order.

    ``ticket_pr_facts`` reports ``repo_rows[0]`` as the ticket's fact, so a
    split that appended the history after the open rows would report a stale
    merged PR for a branch that has an open one, or the reverse.
    """
    approved = [{
        "author": OWNER,
        "createdAt": "2026-09-02T01:00:00Z",
        "body": '{}\n```json\n{{"verdict": "approved", "head_sha": "sha-2"}}\n```'
                .format(funnel.REVIEW_MARKER),
    }]
    reads = _split_pr_scan(monkeypatch, {
        "OPEN": (_pr_row(2, state="OPEN", created_at="2026-09-02T00:00:00Z",
                         comments=approved),),
        "MERGED": (_pr_row(3, state="MERGED",
                           created_at="2026-09-03T00:00:00Z"),
                   _pr_row(1, state="MERGED",
                           created_at="2026-09-01T00:00:00Z")),
    })

    facts = funnel.ticket_pr_facts([_item(11, parent=10)])

    rows = facts.rows_by_ref["{}#11".format(REPO)]
    assert [row["number"] for row in rows] == [3, 2, 1]
    assert len(reads) == 2
    # The open row still derives its verdict from its own tail.
    open_row = next(row for row in rows if row["state"] == "OPEN")
    assert open_row["verdict"]["verdict"] == "approved"


def test_missing_created_at_ticket_fact_matches_single_all_states_read(monkeypatch):
    """A missing timestamp must not let the open-read row win the tie.

    The single all-states read reported merged PR #50 first for this branch;
    the split reads must report the same ticket fact when neither row has a
    ``createdAt`` field.
    """
    merged = _pr_row(50, state="MERGED", created_at=None)
    opened = _pr_row(49, state="OPEN", created_at=None)
    merged.pop("createdAt")
    opened.pop("createdAt")
    _split_pr_scan(monkeypatch, {"OPEN": (opened,), "MERGED": (merged,)})

    facts = funnel.ticket_pr_facts([_item(11, parent=10)])

    assert facts.rows_by_ref["{}#11".format(REPO)][0]["number"] == 50


def test_a_pr_merged_between_the_two_reads_is_kept_once_as_merged():
    """The split introduces a race the single all-states read could not have.

    The open read runs first; if a PR merges before the history read, both
    return it. One snapshot must carry it once, as the later observation saw
    it, or the merge gate reads a row that is already stale.
    """
    open_read = funnel.BatchedPRRead(
        rows_by_repo={REPO: (
            _pr_row(5, state="OPEN", created_at="2026-09-05T00:00:00Z"),
        )},
        branch_refs_by_repo={REPO: {"{}#5".format(REPO)}},
        pr_truncated_by_repo={REPO: False},
        branches_truncated_by_repo={REPO: False},
    )
    history = funnel.BatchedPRRead(
        rows_by_repo={REPO: (
            _pr_row(5, state="MERGED", created_at="2026-09-05T00:00:00Z"),
            _pr_row(4, state="MERGED", created_at="2026-09-04T00:00:00Z"),
        )},
        branch_refs_by_repo={},
        pr_truncated_by_repo={REPO: False},
        branches_truncated_by_repo={},
    )

    merged = funnel._merge_batched_pr_reads(open_read, history)

    rows = merged.rows_by_repo[REPO]
    assert [(row["number"], row["state"]) for row in rows] == [
        (5, "MERGED"), (4, "MERGED"),
    ]
    # Branch refs and their truncation flag come from the open read alone.
    assert merged.branch_refs_by_repo[REPO] == {"{}#5".format(REPO)}
    assert merged.branches_truncated_by_repo[REPO] is False


def test_either_read_hitting_its_bound_leaves_pr_absence_unestablished():
    """A truncated history must not read as a complete scan (#968)."""
    def read(truncated):
        return funnel.BatchedPRRead(
            rows_by_repo={REPO: ()},
            branch_refs_by_repo={REPO: set()},
            pr_truncated_by_repo={REPO: truncated},
            branches_truncated_by_repo={REPO: False},
        )

    assert funnel._merge_batched_pr_reads(
        read(False), read(True)).pr_truncated_by_repo[REPO] is True
    assert funnel._merge_batched_pr_reads(
        read(True), read(False)).pr_truncated_by_repo[REPO] is True
    assert funnel._merge_batched_pr_reads(
        read(False), read(False)).pr_truncated_by_repo[REPO] is False
