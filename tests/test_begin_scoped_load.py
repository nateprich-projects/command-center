"""The filtered begin load, ``load_items(scope="begin")`` (#1591, tickets 2-3).

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

_REAL_HEARTBEAT_BOUND_REFS = funnel._heartbeat_bound_refs


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
    labels=(), lock=None, title=None, repo=REPO, closed_at=None, needs=None,
    parent_repo=None,
):
    return {
        "id": "item-{}-{}".format(repo, number),
        "lock": {"text": lock} if lock else None,
        "status": {"name": status} if status else None,
        "class": {"name": "New"},
        "needs": {"name": needs} if needs else None,
        "content": {
            "number": number,
            "title": title or "issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(repo, number),
            "state": state,
            "stateReason": reason,
            "closedAt": (
                None if state == "OPEN"
                else closed_at or "2026-09-20T00:00:00Z"
            ),
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": label} for label in labels]},
            "assignees": {"nodes": []},
            "parent": (
                {"number": parent,
                 "repository": {"nameWithOwner": parent_repo or repo}}
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


ANCHOR_CONNECTION = re.compile(
    r'(r\d+): items\(first: 5, query: ("(?:[^"\\]|\\.)*")\)'
)


def _anchor_filters(query):
    """The ``{alias: filter}`` of an anchor query, in alias order."""
    return {alias: json.loads(literal)
            for alias, literal in ANCHOR_CONNECTION.findall(query)}


class FakeBoard:
    """Answer the aliased begin query one alias at a time.

    ``pages[alias]`` is a list of ``(nodes, next_cursor)``; the page served
    is the one after the cursor the request carries for that alias.
    ``anchors[filter]`` is the rows an anchor connection with that Project
    filter returns; any other filter returns none.
    """

    def __init__(self, pages, anchors=None):
        self.pages = {alias: list(pages.get(alias, [([], None)]))
                      for alias in ALIASES}
        self.anchors = dict(anchors or {})
        self.calls = []

    @property
    def anchor_calls(self):
        return [(query, variables) for query, variables in self.calls
                if _anchor_filters(query)]

    def __call__(self, query, **variables):
        self.calls.append((query, variables))
        anchors = _anchor_filters(query)
        if anchors:
            return {
                "rateLimit": {"cost": 3, "remaining": 4000,
                              "resetAt": "later"},
                "user": {"projectV2": {
                    alias: _page(self.anchors.get(text, []))
                    for alias, text in anchors.items()
                }},
            }
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


@pytest.fixture(autouse=True)
def no_heartbeat(monkeypatch):
    """Keep the heartbeat anchor source off GitHub; a test may override it."""
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs", lambda: [])


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
        "begin_load.anchor_items",
    }
    # The missing blocker was asked for, and it is not in the Project.
    assert [list(_anchor_filters(query).values())
            for query, _variables in board.anchor_calls] == [
        ["repo:owner/repo #84"],
    ]


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
        "begin_load.anchor_items",
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


# Anchors (#1591, ticket 3): refs a begin consumer reads that the filtered
# connections leave out are fetched by ref, one filtered connection each.

OTHER_MEMBER = "owner/second"
NOW = funnel.datetime(2026, 9, 26, 18, 0, tzinfo=funnel.timezone.utc)


def _blocked_on(monkeypatch, body):
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"comments": [{"body": body}]}
    )


def test_an_old_closed_blocker_is_fetched_and_its_block_clears(monkeypatch):
    _blocked_on(monkeypatch, "**Blocked on #84:** Wait for the decision.")
    blocker = _node(84, state="CLOSED", reason="COMPLETED", status="Done",
                    parent=1, closed_at="2026-07-28T00:00:00Z")
    board = FakeBoard(
        {"open": [([_node(1), _node(2, parent=1, labels=("blocked",))],
                   None)]},
        anchors={"repo:owner/repo #84": [blocker]},
    )

    items = _load(monkeypatch, board)
    by_ref = {item.ref: item for item in items}

    assert [item.ref for item in items] == [
        REPO + "#1", REPO + "#2", REPO + "#84",
    ]
    assert by_ref[REPO + "#84"].state == "CLOSED"
    assert funnel.satisfied_block_refs(by_ref[REPO + "#2"], by_ref, NOW) == [
        REPO + "#84",
    ]


def test_the_closed_freeze_owner_is_fetched_so_the_freeze_stays_inert(
    monkeypatch,
):
    owner_repo, owner_number = funnel.FREEZE_OWNER_REF.split("#")
    owner = _node(int(owner_number), state="CLOSED", reason="COMPLETED",
                  status="Done", repo=owner_repo)
    board = FakeBoard(
        {"open": [([_node(1)], None)]},
        anchors={"repo:{} #{}".format(owner_repo, owner_number): [owner]},
    )

    items = _load(monkeypatch, board,
                  member_repo_names=[REPO, owner_repo])
    by_ref = {item.ref: item for item in items}

    assert by_ref[funnel.FREEZE_OWNER_REF].state == "CLOSED"
    assert funnel._freeze_governing(by_ref) is None
    # Without it the freeze would read as governing again.
    del by_ref[funnel.FREEZE_OWNER_REF]
    assert funnel._freeze_governing(by_ref) is not None


def test_a_closed_parent_is_fetched_so_its_ticket_stays_startable(
    monkeypatch,
):
    parent = _node(3, state="CLOSED", reason="COMPLETED", status="Building")
    board = FakeBoard(
        {"open": [([_node(1), _node(5, parent=3, needs="agent")], None)]},
        anchors={"repo:owner/repo #3": [parent]},
    )

    items = _load(monkeypatch, board)
    by_ref = {item.ref: item for item in items}

    assert REPO + "#3" in by_ref
    assert funnel._startable_without_repo_readiness(
        by_ref[REPO + "#5"], by_ref, set()
    )
    del by_ref[REPO + "#3"]
    assert not funnel._startable_without_repo_readiness(
        by_ref[REPO + "#5"], by_ref, set()
    )


def test_a_heartbeat_bound_closed_ticket_is_fetched(monkeypatch):
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs",
                        lambda: [REPO + "#40"])
    ticket = _node(40, state="CLOSED", reason="COMPLETED", status="Done",
                   parent=1)
    board = FakeBoard(
        {"open": [([_node(1)], None)]},
        anchors={"repo:owner/repo #40": [ticket]},
    )

    items = _load(monkeypatch, board)

    assert [item.ref for item in items] == [REPO + "#1", REPO + "#40"]


def test_heartbeat_refs_come_from_open_ticket_starts_of_live_agents(
    monkeypatch,
):
    import heartbeat

    monkeypatch.setattr(heartbeat, "PROVIDERS",
                        {"alpha": "a", "beta": "b", "gone": "g"})
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset({"gone"}))
    spools = {
        "alpha": [
            {"run": "a1", "phase": "start", "ts": 1},
            {"run": "a1", "phase": "bind", "ts": 2, "do": "ticket",
             "work": REPO + "#40"},
            {"run": "a2", "phase": "start", "ts": 3},
            {"run": "a2", "phase": "bind", "ts": 4, "do": "review",
             "work": "12", "repo": REPO},
            {"run": "a3", "phase": "start", "ts": 5},
            {"run": "a3", "phase": "bind", "ts": 6, "do": "ticket",
             "work": REPO + "#41"},
            {"run": "a3", "phase": "finish", "ts": 7, "outcome": "done"},
        ],
        "gone": [
            {"run": "g1", "phase": "start", "ts": 1},
            {"run": "g1", "phase": "bind", "ts": 2, "do": "ticket",
             "work": REPO + "#42"},
        ],
    }

    def rows(agent):
        if agent == "beta":
            raise RuntimeError("heartbeat branch unreadable")
        return spools[agent]

    monkeypatch.setattr(funnel, "_brief_heartbeat_rows", rows)
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs",
                        _REAL_HEARTBEAT_BOUND_REFS)

    assert funnel._heartbeat_bound_refs() == [REPO + "#40"]


def test_an_issue_outside_the_project_stays_absent(monkeypatch):
    _blocked_on(monkeypatch, "**Blocked on #84 and #85:** Wait.")
    board = FakeBoard(
        {"open": [([
            _node(1),
            _node(2, parent=1, labels=("blocked",)),
            _node(6, parent=9, parent_repo=OTHER_MEMBER),
            _node(7, parent=3, parent_repo=OTHER_REPO),
        ], None)]},
        # The filter narrows; Python decides. Near matches are not the ref.
        anchors={"repo:owner/repo #84": [
            _node(841, state="CLOSED"),
            _node(84, state="CLOSED", repo=OTHER_MEMBER),
        ]},
    )

    items = _load(monkeypatch, board,
                  member_repo_names=[REPO, OTHER_MEMBER])

    assert [item.ref for item in items] == [
        REPO + "#1", REPO + "#2", REPO + "#6", REPO + "#7",
    ]
    # A non-member repo is never asked for, as the full load never keeps it.
    assert [list(_anchor_filters(query).values())
            for query, _variables in board.anchor_calls] == [[
        "repo:owner/repo #84", "repo:owner/repo #85", "repo:owner/second #9",
    ]]


def test_a_failed_anchor_fetch_raises(monkeypatch):
    board = FakeBoard({"open": [([_node(1), _node(5, parent=3)], None)]})

    def failing(query, **variables):
        if _anchor_filters(query):
            raise funnel.GitHubError("GraphQL: something went wrong")
        return board(query, **variables)

    with pytest.raises(funnel.GitHubError, match="something went wrong"):
        _load(monkeypatch, failing)


@pytest.mark.parametrize("breakage", [
    "missing-connection", "nodes-not-a-list", "row-without-repository",
    "next-page-without-the-issue",
])
def test_a_malformed_anchor_response_raises(monkeypatch, breakage):
    board = FakeBoard({"open": [([_node(1), _node(5, parent=3)], None)]})

    def broken(query, **variables):
        response = board(query, **variables)
        if not _anchor_filters(query):
            return response
        project = response["user"]["projectV2"]
        if breakage == "missing-connection":
            del project["r0"]
        elif breakage == "nodes-not-a-list":
            project["r0"]["nodes"] = None
        elif breakage == "row-without-repository":
            row = _node(3, state="CLOSED")
            del row["content"]["repository"]
            project["r0"]["nodes"] = [row]
        else:
            project["r0"] = _page([_node(30, state="CLOSED")], "more")
        return response

    with pytest.raises(funnel.GitHubError, match=r"r0|owner/repo#3"):
        _load(monkeypatch, broken)


def test_nothing_missing_sends_no_anchor_query(monkeypatch):
    _blocked_on(monkeypatch, "**Blocked on #3:** Wait.")
    board = FakeBoard({
        "open": [([_node(1), _node(2, parent=1, labels=("blocked",))],
                   None)],
        "claims": [([_node(3, state="CLOSED", reason="COMPLETED",
                           status="Done", parent=1,
                           lock="2026-09-20T00:00:00Z")], None)],
    })

    _load(monkeypatch, board)

    assert len(board.calls) == 1
    assert board.anchor_calls == []


def test_missing_refs_across_two_repos_share_one_request(monkeypatch):
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs",
                        lambda: [OTHER_MEMBER + "#7", REPO + "#3"])
    board = FakeBoard(
        {"open": [([
            _node(1),
            _node(5, parent=3),
            _node(6, parent=3),
            _node(8, parent=4, parent_repo=OTHER_MEMBER),
        ], None)]},
        anchors={
            "repo:owner/repo #3": [_node(3, state="CLOSED")],
            "repo:owner/second #4": [
                _node(4, state="CLOSED", repo=OTHER_MEMBER)],
            "repo:owner/second #7": [
                _node(7, state="CLOSED", repo=OTHER_MEMBER, parent=4)],
        },
    )
    funnel.reset_api_usage()

    items = _load(monkeypatch, board,
                  member_repo_names=[REPO, OTHER_MEMBER])

    assert len(board.anchor_calls) == 1
    query, variables = board.anchor_calls[0]
    assert _anchor_filters(query) == {
        "r0": "repo:owner/repo #3",
        "r1": "repo:owner/second #4",
        "r2": "repo:owner/second #7",
    }
    assert variables == {
        "login": funnel.PROJECT_OWNER, "number": funnel.PROJECT_NUMBER,
    }
    assert funnel.ITEM_NODE_FIELDS in query
    assert [item.ref for item in items][-3:] == [
        REPO + "#3", OTHER_MEMBER + "#4", OTHER_MEMBER + "#7",
    ]
    assert funnel.project_item_load_measurement() == (2, 7)


def test_more_than_twenty_missing_refs_take_another_request(monkeypatch):
    refs = [REPO + "#{}".format(100 + index) for index in range(25)]
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs", lambda: refs)
    board = FakeBoard({"open": [([_node(1)], None)]})

    _load(monkeypatch, board)

    assert [len(_anchor_filters(query))
            for query, _variables in board.anchor_calls] == [20, 5]
