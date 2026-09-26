"""The filtered begin load, ``load_items(scope="begin")`` (#1591, ticket 2).

Every test answers GraphQL with a fake that pages each alias on its own, so
nothing here reaches GitHub.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"
OTHER_REPO = "owner/not-a-member"

# The design's filters, written out so a change to any of them is deliberate.
EXPECTED_FILTERS = {
    "open": "is:open",
    "claims": "is:closed has:in-motion-since",
    "parked": "is:closed status:Parked no:parent-issue",
    "doneDrift": "is:closed no:parent-issue reason:completed -status:Done",
    "parkDrift":
        'is:closed no:parent-issue reason:"not planned" -status:Parked',
    "shaping": "is:closed label:needs-shaping",
    "regress": '"Regression from PR"',
}
ALIASES = list(EXPECTED_FILTERS)


def _node(
    number, *, state="OPEN", reason=None, status="Building", parent=None,
    labels=(), lock=None, title=None, repo=REPO,
):
    return {
        "id": "item-{}-{}".format(repo, number),
        "lock": {"text": lock} if lock else None,
        "status": {"name": status} if status else None,
        "class": {"name": "New"},
        "content": {
            "number": number,
            "title": title or "issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(repo, number),
            "state": state,
            "stateReason": reason,
            "closedAt": None if state == "OPEN" else "2026-09-20T00:00:00Z",
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": label} for label in labels]},
            "assignees": {"nodes": []},
            "parent": (
                {"number": parent, "repository": {"nameWithOwner": repo}}
                if parent is not None
                else None
            ),
            "subIssuesSummary": {"total": 0, "completed": 0},
            "blockedBy": {"nodes": []},
        },
    }


def _page(nodes, cursor=None):
    return {
        "nodes": list(nodes),
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
    }


class FakeBoard:
    """Answer the aliased begin query one alias at a time.

    ``pages[alias]`` is a list of ``(nodes, next_cursor)``; the page served
    is the one after the cursor the request carries for that alias.
    """

    def __init__(self, pages):
        self.pages = {alias: list(pages.get(alias, [([], None)]))
                      for alias in ALIASES}
        self.calls = []

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        aliases = re.findall(r"(\w+): items\(", query)
        project = {}
        for alias in aliases:
            cursor = variables.get(alias + "Cursor")
            index = 0
            if cursor is not None:
                served = [next_cursor for _nodes, next_cursor
                          in self.pages[alias]]
                index = served.index(cursor) + 1
            nodes, next_cursor = self.pages[alias][index]
            project[alias] = _page(nodes, next_cursor)
        return {
            "rateLimit": {"cost": 3, "remaining": 4000, "resetAt": "later"},
            "user": {"projectV2": project},
        }


def _load(monkeypatch, board, **kwargs):
    monkeypatch.setattr(funnel, "gh_graphql", board)
    kwargs.setdefault("include_details", False)
    kwargs.setdefault("member_repo_names", [REPO])
    return funnel.load_items(scope="begin", **kwargs)


def test_query_document_carries_each_filter_string_exactly():
    assert dict(funnel.BEGIN_ITEM_CONNECTIONS) == EXPECTED_FILTERS

    query = funnel._begin_item_query(ALIASES)
    literals = dict(re.findall(
        r'(\w+): items\(first: 100, after: \$\1Cursor, '
        r'query: ("(?:[^"\\]|\\.)*")\)',
        query,
    ))
    assert {alias: json.loads(literal)
            for alias, literal in literals.items()} == EXPECTED_FILTERS
    for alias, text in EXPECTED_FILTERS.items():
        assert query.count("query: {})".format(json.dumps(text))) == 1
        assert "${}Cursor: String".format(alias) in query
    # Every connection reads the full load's node selection, nothing less.
    assert query.count("nodes { ...BeginItem }") == len(ALIASES)
    assert funnel.ITEM_NODE_FIELDS in query
    assert funnel.ITEM_NODE_FIELDS in funnel.ITEM_QUERY


def test_second_page_is_requested_for_the_paging_alias_only(monkeypatch):
    funnel.reset_api_usage()
    board = FakeBoard({
        "open": [([_node(1)], "open-1"), ([_node(2)], None)],
        "claims": [([_node(3, state="CLOSED", reason="COMPLETED",
                           status="Done", parent=1,
                           lock="2026-09-20T00:00:00Z")], None)],
    })

    items = _load(monkeypatch, board)

    assert sorted(item.number for item in items) == [1, 2, 3]
    assert len(board.calls) == 2
    first_query, first_variables = board.calls[0]
    assert re.findall(r"(\w+): items\(", first_query) == ALIASES
    assert first_variables == {
        "login": funnel.PROJECT_OWNER, "number": funnel.PROJECT_NUMBER,
    }
    second_query, second_variables = board.calls[1]
    assert re.findall(r"(\w+): items\(", second_query) == ["open"]
    assert "$claimsCursor" not in second_query
    assert second_variables == {
        "login": funnel.PROJECT_OWNER, "number": funnel.PROJECT_NUMBER,
        "openCursor": "open-1",
    }
    assert funnel.project_item_load_measurement() == (2, 3)


@pytest.mark.parametrize("breakage", [
    "missing",
    "nodes-not-a-list",
    "node-not-a-dict",
    "no-page-info",
    "has-next-not-bool",
    "next-page-without-cursor",
    "row-without-repository",
])
def test_a_malformed_connection_fails_the_load(monkeypatch, breakage):
    board = FakeBoard({"open": [([_node(1)], None)]})

    def broken(query, **variables):
        response = board(query, **variables)
        connection = response["user"]["projectV2"]["parked"]
        if breakage == "missing":
            del response["user"]["projectV2"]["parked"]
        elif breakage == "nodes-not-a-list":
            connection["nodes"] = None
        elif breakage == "node-not-a-dict":
            connection["nodes"] = ["item"]
        elif breakage == "no-page-info":
            del connection["pageInfo"]
        elif breakage == "has-next-not-bool":
            connection["pageInfo"]["hasNextPage"] = "false"
        elif breakage == "next-page-without-cursor":
            connection["pageInfo"] = {"hasNextPage": True, "endCursor": None}
        else:
            row = _node(5, state="CLOSED", status="Parked")
            del row["content"]["repository"]
            connection["nodes"] = [row]
        return response

    with pytest.raises(funnel.GitHubError, match="parked"):
        _load(monkeypatch, broken)


def test_a_missing_project_fails_the_load(monkeypatch):
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: {"user": {"projectV2": None}},
    )
    with pytest.raises(funnel.GitHubError, match="not found or not visible"):
        funnel.load_items(
            include_details=False, member_repo_names=[REPO], scope="begin",
        )


def test_a_stale_closed_row_in_open_is_dropped_not_fatal(monkeypatch):
    """On 2026-09-26 the live `is:open` filter still returned #1466 a day
    after it closed. The row's own state is live, so a closed row the
    filter mislabels is judged by the closed-set predicates like any other:
    here it matches none and is dropped, and the load goes on."""
    board = FakeBoard({
        "open": [([_node(1), _node(2, state="CLOSED", reason="COMPLETED",
                                   status="Done")], None)],
    })

    assert [item.number for item in _load(monkeypatch, board)] == [1]


def test_a_stale_closed_row_in_open_is_kept_when_a_predicate_holds(
    monkeypatch,
):
    board = FakeBoard({
        "open": [([_node(1)], "open-1"),
                 ([_node(2, state="CLOSED", reason="NOT_PLANNED",
                         status="Parked")], None)],
    })

    assert [item.number for item in _load(monkeypatch, board)] == [1, 2]


def test_an_open_row_from_a_closed_set_connection_is_kept(monkeypatch):
    """A reopened issue can still be indexed as closed; its live state says
    open, so begin sees it whichever connection returned it."""
    board = FakeBoard({
        "open": [([_node(1)], None)],
        "parked": [([_node(7, status="Parked")], None)],
    })

    assert [item.number for item in _load(monkeypatch, board)] == [1, 7]


def test_an_empty_open_connection_fails_the_load(monkeypatch):
    board = FakeBoard({
        "open": [([], None)],
        "claims": [([_node(3, state="CLOSED", lock="2026-09-20T00:00:00Z")],
                    None)],
    })

    with pytest.raises(funnel.GitHubError, match="open returned no items"):
        _load(monkeypatch, board)


def test_open_holding_only_drafts_counts_as_empty(monkeypatch):
    draft = {"id": "draft-1", "content": {}}
    board = FakeBoard({"open": [([draft], None)]})

    with pytest.raises(funnel.GitHubError, match="open returned no items"):
        _load(monkeypatch, board)


def test_a_cursor_that_does_not_advance_fails_the_load(monkeypatch):
    calls = []

    def stuck(query, **variables):
        calls.append(variables)
        assert len(calls) < 5, "the loader kept paging a stuck cursor"
        project = {
            alias: _page([], None)
            for alias in re.findall(r"(\w+): items\(", query)
        }
        project["open"] = _page([_node(1)], "open-1")
        return {"user": {"projectV2": project}}

    with pytest.raises(funnel.GitHubError, match="open did not advance"):
        _load(monkeypatch, stuck)
    assert len(calls) == 2
    assert calls[1]["openCursor"] == "open-1"


def test_rows_are_kept_once_across_connections(monkeypatch):
    regression = _node(
        4, title=funnel.REGRESSION_PREFIX + "12: something broke"
    )
    closed_claim = _node(
        5, state="CLOSED", reason="NOT_PLANNED", status="Parked",
        lock="2026-09-20T00:00:00Z",
    )
    board = FakeBoard({
        "open": [([_node(1), regression], "open-1"),
                 ([_node(1)], None)],
        "claims": [([closed_claim], None)],
        "parked": [([closed_claim], None)],
        "regress": [([regression], None)],
    })

    items = _load(monkeypatch, board)

    assert [item.number for item in items] == [1, 4, 5]
    assert len({item.item_id for item in items}) == 3


def test_a_row_the_filter_wrongly_returned_is_dropped(monkeypatch):
    lock = "2026-09-20T00:00:00Z"
    board = FakeBoard({
        "open": [([_node(1)], None)],
        # No claim on it after all.
        "claims": [([_node(10, state="CLOSED", reason="COMPLETED",
                            status="Done", parent=1)], None)],
        # A child ticket, which the parked wake never reads.
        "parked": [([_node(11, state="CLOSED", reason="NOT_PLANNED",
                            status="Parked", parent=1)], None)],
        # Already Done, so there is no drift to repair.
        "doneDrift": [([_node(12, state="CLOSED", reason="COMPLETED",
                               status="Done")], None)],
        # Completed, not "not planned".
        "parkDrift": [([_node(13, state="CLOSED", reason="COMPLETED",
                               status="Building")], None)],
        # Open, so it is not the closed shaping repair's.
        "shaping": [([_node(14, labels=("needs-shaping",))], None)],
        # Mentions the phrase but does not carry the prefix.
        "regress": [([_node(15, state="CLOSED",
                             title="Notes on Regression from PR review")],
                     None)],
    })
    # And each connection's genuine row survives beside the dropped one.
    board.pages["claims"][0][0].append(
        _node(20, state="CLOSED", reason="COMPLETED", status="Done",
              parent=1, lock=lock))
    board.pages["parked"][0][0].append(
        _node(21, state="CLOSED", reason="NOT_PLANNED", status="Parked"))
    board.pages["doneDrift"][0][0].append(
        _node(22, state="CLOSED", reason="COMPLETED", status=None))
    board.pages["parkDrift"][0][0].append(
        _node(23, state="CLOSED", reason="NOT_PLANNED", status="Ideas"))
    board.pages["shaping"][0][0].append(
        _node(24, state="CLOSED", reason="NOT_PLANNED", status="Parked",
              labels=("needs-shaping",)))
    board.pages["regress"][0][0].append(
        _node(25, state="CLOSED", reason="COMPLETED", status="Done",
              title=funnel.REGRESSION_PREFIX + "9: broke it"))

    items = _load(monkeypatch, board)

    # 13 is a genuine done-drift row (completed, Status Building) and 14
    # is open, so both are kept whichever connection returned them.
    assert [item.number for item in items] == [1, 20, 21, 22, 13, 23, 14, 24, 25]


def test_a_row_refused_by_one_connection_is_kept_by_another(monkeypatch):
    row = _node(6, state="CLOSED", reason="NOT_PLANNED", status="Parked")
    board = FakeBoard({
        "open": [([_node(1)], None)],
        "claims": [([row], None)],  # no lock: refused here
        "parked": [([row], None)],  # but a parentless closed Parked item
    })

    items = _load(monkeypatch, board)

    assert [item.number for item in items] == [1, 6]


def test_members_and_block_comments_are_handled_as_in_the_full_load(
    monkeypatch,
):
    comment_reads = []

    def gh_json(*args):
        comment_reads.append(args)
        return {"comments": [
            {"body": "**Blocked on #84:** Wait for the decision."}
        ]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    board = FakeBoard({
        "open": [([
            _node(1),
            _node(2, parent=1, labels=("blocked",)),
            _node(3, repo=OTHER_REPO, labels=("blocked",)),
        ], None)],
        "claims": [([_node(7, state="CLOSED", labels=("blocked",),
                           lock="2026-09-20T00:00:00Z")], None)],
    })
    timings = {}

    items = _load(monkeypatch, board, timings=timings)

    assert [item.ref for item in items] == [
        REPO + "#1", REPO + "#2", REPO + "#7",
    ]
    assert [args[3] for args in comment_reads] == ["2"]
    assert items[1].block_references == ["#84"]
    assert set(timings) == {
        "begin_load.project_items", "begin_load.block_comments",
    }


def test_timings_record_the_same_phases_as_the_full_load(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "gh_graphql",
                        FakeBoard({"open": [([_node(1)], None)]}))
    hydrated = []
    monkeypatch.setattr(
        funnel, "hydrate_item_details",
        lambda items, candidates=None: hydrated.append(list(items)),
    )
    timings = {}

    items = funnel.load_items(timings=timings, scope="begin")

    assert [item.number for item in items] == [1]
    assert hydrated == [items]
    assert set(timings) == {
        "begin_load.member_repos",
        "begin_load.project_items",
        "begin_load.block_comments",
        "begin_load.item_details",
    }


def test_a_failed_load_still_records_its_timing(monkeypatch):
    timings = {}
    with pytest.raises(funnel.GitHubError):
        _load(monkeypatch, FakeBoard({}), timings=timings)
    assert set(timings) == {
        "begin_load.project_items", "begin_load.block_comments",
    }


def test_the_default_scope_reads_the_full_board_unchanged(monkeypatch):
    nodes = [
        _node(1),
        _node(2, state="CLOSED", reason="COMPLETED", status="Done",
              parent=1),
        _node(3, repo=OTHER_REPO),
    ]
    results = {}
    for scope in ("default", None, "full"):
        calls = []

        def graphql(query, **variables):
            calls.append((query, variables))
            return {"user": {"projectV2": {"items": _page(nodes)}}}

        monkeypatch.setattr(funnel, "gh_graphql", graphql)
        kwargs = {} if scope == "default" else {"scope": scope}
        items = funnel.load_items(
            include_details=False, member_repo_names=[REPO], **kwargs
        )
        assert calls == [(funnel.ITEM_QUERY, {
            "login": funnel.PROJECT_OWNER, "number": funnel.PROJECT_NUMBER,
        })]
        results[scope] = [(item.ref, item.state) for item in items]

    assert results["default"] == [
        (REPO + "#1", "OPEN"), (REPO + "#2", "CLOSED"),
    ]
    assert results[None] == results["default"] == results["full"]


def test_an_unknown_scope_or_a_shape_issue_is_refused(monkeypatch):
    def no_calls(*args, **kwargs):
        raise AssertionError("the load must refuse before reading GitHub")

    monkeypatch.setattr(funnel, "gh_graphql", no_calls)
    monkeypatch.setattr(funnel, "member_repos", no_calls)
    with pytest.raises(ValueError, match="scope"):
        funnel.load_items(scope="brief")
    with pytest.raises(ValueError, match="shape issue"):
        funnel.load_items(scope="begin", shape_issue=(REPO, 42))
