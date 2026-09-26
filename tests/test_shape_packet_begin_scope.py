"""The shape packet reads the open board, not the full board (#1625).

``shape.collect`` loads ``scope="begin"``: every open Project item plus the
closed sets begin carries, with the idea's comment thread riding the first
aliased request. Its siblings are open, parentless, same-repo plans, so
nothing the packet reads lives only in the full board.

One fixture board is served two ways by a fake ``gh_graphql``: as the full
``ITEM_QUERY`` pages, and as the aliased begin document, each connection
narrowed by a stand-in for GitHub's search filter. The packet must be
identical either way.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import shape  # noqa: E402

REPO = "owner/repo"
SISTER = "owner/sister"        # a member repo, not the idea's
OUTSIDER = "owner/outsider"    # on the Project, not a member
MEMBERS = [REPO, SISTER]
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
IDEA = 42

IDEA_BODY = "Captured note.\n\n" + funnel.origin_block(
    "agent", at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    run="capture-run", agent="muse")

THREAD = [
    {"author": {"login": "nate"}, "body": "The old premise is false.",
     "createdAt": "2026-09-25T01:00:00Z"},
    {"author": {"login": "muse"}, "body": "Noted; re-reading the plan.",
     "createdAt": "2026-09-25T02:00:00Z"},
]


def _node(number, *, status, repo=REPO, state="OPEN", reason=None,
          klass="Improve", origin="agent", parent=None, labels=(),
          lock=None, title=None, body=None):
    if state == "CLOSED" and reason is None:
        reason = "COMPLETED"
    return {
        "id": "PVTI_{}_{}".format(repo, number),
        "lock": {"text": lock} if lock else None,
        "status": {"name": status} if status else None,
        "class": {"name": klass} if klass else None,
        "origin": {"name": origin} if origin else None,
        "risk": {"name": "standard"},
        "pinned": None,
        "needs": {"name": "none"},
        "content": {
            "number": number,
            "title": title or "issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(repo, number),
            "body": body if body is not None else "# Plan {}".format(number),
            "state": state,
            "stateReason": reason,
            "createdAt": "2026-09-01T00:00:00Z",
            "closedAt": "2026-09-20T00:00:00Z" if state == "CLOSED" else None,
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": label} for label in labels]},
            "assignees": {"nodes": []},
            "parent": (
                {"number": parent, "repository": {"nameWithOwner": repo}}
                if parent is not None else None
            ),
            "subIssuesSummary": {"total": 0, "completed": 0},
            "blockedBy": {"totalCount": 0, "nodes": []},
        },
    }


def _board_rows():
    """Every kind of row a sibling rule could trip over."""
    return [
        # The idea itself, with its captured note.
        _node(IDEA, status="Ideas", labels=("needs-shaping",),
              body=IDEA_BODY),
        # Siblings: open, parentless, same repo, Shaped/Ready/Building.
        _node(10, status="Building", origin="nate"),
        _node(43, status="Shaped"),
        _node(44, status="Ready", klass="Maintain"),
        # A parent plan's children: open and closed, never siblings.
        _node(11, status="Building", parent=10,
              lock="2026-09-26T11:00:00Z"),
        _node(12, status="Done", parent=10, state="CLOSED"),
        # Parked, open and closed: not a shaping status.
        _node(50, status="Parked"),
        _node(51, status="Parked", state="CLOSED", reason="NOT_PLANNED"),
        # Closed plans: finished, and one whose Status never caught up.
        _node(60, status="Done", state="CLOSED"),
        _node(61, status="Shaped", state="CLOSED"),
        # Another idea waiting its turn.
        _node(45, status="Ideas", labels=("needs-shaping",)),
        # Plans in other repos: a member and a non-member.
        _node(70, status="Shaped", repo=SISTER),
        _node(71, status="Ready", repo=SISTER),
        _node(80, status="Building", repo=OUTSIDER),
        # A draft issue has no content number.
        {"id": "PVTI_draft", "content": {}},
    ]


def _open(node):
    return node["content"].get("state") == "OPEN"


def _closed(node):
    return node["content"].get("state") == "CLOSED"


def _parentless(node):
    return node["content"].get("parent") is None


def _status(node):
    return (node.get("status") or {}).get("name")


def _reason(node):
    return node["content"].get("stateReason")


def _labels(node):
    return {row["name"] for row in node["content"]["labels"]["nodes"]}


# A stand-in for GitHub's search filter behind each begin connection.
SEARCH = {
    "open": _open,
    "claims": lambda n: _closed(n) and bool(n.get("lock")),
    "parked": lambda n: (_closed(n) and _status(n) == "Parked"
                         and _parentless(n)),
    "doneDrift": lambda n: (_closed(n) and _parentless(n)
                            and _reason(n) == "COMPLETED"
                            and _status(n) != "Done"),
    "parkDrift": lambda n: (_closed(n) and _parentless(n)
                            and _reason(n) == "NOT_PLANNED"
                            and _status(n) != "Parked"),
    "shaping": lambda n: _closed(n) and "needs-shaping" in _labels(n),
    "regress": lambda n: "Regression from PR" in n["content"]["title"],
}


class Board:
    """Serve one row list as full pages or as the aliased begin document."""

    def __init__(self, rows, *, comments=THREAD, page_size=4,
                 stale_open=(), drop_comments=False):
        self.rows = rows
        self.comments = comments
        self.page_size = page_size
        # Rows the (stale) `is:open` index still returns though closed.
        self.stale_open = list(stale_open)
        self.drop_comments = drop_comments
        self.queries = []

    def graphql(self, query, **variables):
        self.queries.append(query)
        aliases = re.findall(r"(\w+): items\(", query)
        if aliases:
            response = self._begin(aliases, variables)
        elif "items(first:" in query:
            response = self._full(variables)
        elif query == funnel.SHAPE_ISSUE_COMMENTS_PAGE_QUERY:
            return self._comment_page(variables["cursor"])
        else:
            raise AssertionError("unexpected GraphQL request: {}".format(
                " ".join(query.split())[:120]))
        if "shapeIssue:" in query and not self.drop_comments:
            response["shapeIssue"] = self._comment_page(None)["repository"]
        return response

    def _comment_page(self, cursor):
        index = 0 if cursor is None else int(cursor)
        nodes = copy.deepcopy(self.comments[index:index + 1]) \
            if len(self.comments) > 1 else copy.deepcopy(self.comments)
        more = len(self.comments) > 1 and index + 1 < len(self.comments)
        return {"repository": {"issue": {"comments": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": more,
                         "endCursor": str(index + 1) if more else None},
        }}}}

    def _slice(self, rows, cursor):
        start = int(cursor or 0)
        end = start + self.page_size
        more = end < len(rows)
        return {
            "nodes": copy.deepcopy(rows[start:end]),
            "pageInfo": {"hasNextPage": more,
                         "endCursor": str(end) if more else None},
        }

    def _full(self, variables):
        return {"user": {"projectV2": {
            "items": self._slice(self.rows, variables.get("cursor")),
        }}}

    def _begin(self, aliases, variables):
        project = {}
        for alias in aliases:
            rows = [row for row in self.rows
                    if row.get("content", {}).get("number")
                    and SEARCH[alias](row)]
            if alias == "open":
                rows = rows + self.stale_open
            project[alias] = self._slice(
                rows, variables.get(alias + "Cursor"))
        return {"user": {"projectV2": project}}

    @property
    def begin_requests(self):
        return [q for q in self.queries if re.search(r"\w+: items\(", q)]

    @property
    def full_requests(self):
        return [q for q in self.queries
                if "items(first:" in q and not re.search(r"\w+: items\(", q)]


REAL_LOAD_ITEMS = funnel.load_items


def _serve(monkeypatch, board, *, full):
    """Install ``board``; ``full`` forces the shipped call back to the full
    load while keeping every other argument it passes."""
    monkeypatch.setattr(funnel, "member_repos", lambda: list(MEMBERS))
    monkeypatch.setattr(funnel, "gh_graphql", board.graphql)
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    received = []

    def load_items(**kwargs):
        received.append(dict(kwargs))
        if full:
            kwargs.pop("scope", None)
        return REAL_LOAD_ITEMS(**kwargs)

    monkeypatch.setattr(funnel, "load_items", load_items)
    return received


def _packet(monkeypatch, board, *, full, idea=IDEA):
    with monkeypatch.context() as patch:
        received = _serve(patch, board, full=full)
        packet = shape.collect(REPO, idea, now=NOW)
    return received, json.dumps(packet, indent=2, sort_keys=True)


def _both(monkeypatch, make_board, idea=IDEA):
    full_board, begin_board = make_board(), make_board()
    _full_args, full_out = _packet(monkeypatch, full_board, full=True,
                                   idea=idea)
    begin_args, begin_out = _packet(monkeypatch, begin_board, full=False,
                                    idea=idea)
    # Each run read the board the way it claims to, and only that way.
    assert full_board.full_requests and not full_board.begin_requests
    assert begin_board.begin_requests and not begin_board.full_requests
    assert begin_args == [{"include_details": False,
                           "shape_issue": (REPO, idea), "scope": "begin"}]
    return full_board, full_out, begin_board, begin_out


def test_packet_is_identical_between_the_full_and_begin_loads(monkeypatch):
    full, full_out, begin, begin_out = _both(
        monkeypatch, lambda: Board(_board_rows()))

    assert begin_out == full_out
    packet = json.loads(begin_out)
    assert packet["idea"]["ref"] == "{}#{}".format(REPO, IDEA)
    assert packet["idea"]["body"] == IDEA_BODY
    assert packet["origin"]["voice"] == "agent"
    assert [row["ref"] for row in packet["sibling_plans"]] == [
        REPO + "#10", REPO + "#43", REPO + "#44"]
    for comment in THREAD:
        assert comment["body"] in packet["issue_thread"]
    # Both loads paged, so the thread riding page one only is exercised.
    assert len(full.full_requests) > 1
    assert len(begin.begin_requests) > 1
    # The thread rides the first request of each load and no other.
    for board in (full, begin):
        carrying = [q for q in board.queries if "shapeIssue:" in q]
        assert carrying == [board.queries[0]]


def test_packet_parity_holds_for_a_long_thread(monkeypatch):
    """A thread past its first page pages through the follow-up query in
    either load, and the whole thread lands in the packet."""
    long_thread = [
        {"author": {"login": "nate"}, "body": "comment {}".format(n),
         "createdAt": "2026-09-25T0{}:00:00Z".format(n)}
        for n in range(1, 4)
    ]
    full, full_out, begin, begin_out = _both(
        monkeypatch, lambda: Board(_board_rows(), comments=long_thread))

    assert begin_out == full_out
    for comment in long_thread:
        assert comment["body"] in json.loads(begin_out)["issue_thread"]
    assert begin.queries.count(funnel.SHAPE_ISSUE_COMMENTS_PAGE_QUERY) == 2


def test_stale_closed_rows_in_open_follow_the_live_state_rule(monkeypatch):
    """`is:open` has returned closed rows (#1466, 2026-09-26). The live
    state decides: a closed Shaped plan a predicate holds for is kept, one
    none holds for is dropped, and neither is ever a sibling."""
    stale = [
        _node(62, status="Ready", state="CLOSED"),     # doneDrift holds
        _node(63, status="Done", state="CLOSED"),      # nothing holds
    ]

    def make():
        # The full board has them too; only the begin index mislabels them.
        return Board(_board_rows() + copy.deepcopy(stale),
                     stale_open=copy.deepcopy(stale))

    _full, full_out, _begin, begin_out = _both(monkeypatch, make)

    assert begin_out == full_out
    refs = [row["ref"] for row in json.loads(begin_out)["sibling_plans"]]
    assert REPO + "#62" not in refs and REPO + "#63" not in refs

    monkeypatch.setattr(funnel, "gh_graphql", make().graphql)
    items = REAL_LOAD_ITEMS(include_details=False, member_repo_names=MEMBERS,
                            scope="begin", shape_issue=(REPO, IDEA))
    numbers = {item.number for item in items if item.repo == REPO}
    assert 62 in numbers and 63 not in numbers


def test_a_closed_idea_begin_carries_gives_the_same_packet(monkeypatch):
    """A closed idea still labelled needs-shaping sits in the `shaping`
    connection, so the begin view finds it as the full board does."""
    def make():
        rows = _board_rows()
        rows[0] = _node(IDEA, status="Ideas", labels=("needs-shaping",),
                        state="CLOSED", reason="NOT_PLANNED", body=IDEA_BODY)
        return Board(rows)

    _full, full_out, _begin, begin_out = _both(monkeypatch, make)
    assert begin_out == full_out


def test_an_idea_missing_from_the_begin_view_fails_closed(monkeypatch):
    """A closed idea no begin connection carries (completed, Done, no
    needs-shaping label) is absent from the view.
    The packet refuses rather than guess; the full load would have found
    it, so this is not silently a different packet."""
    rows = _board_rows()
    rows[0] = _node(IDEA, status="Done", state="CLOSED", body=IDEA_BODY)
    board = Board(rows)

    with monkeypatch.context() as patch:
        _serve(patch, board, full=False)
        with pytest.raises(funnel.GitHubError,
                           match="not in the open board view"):
            shape.collect(REPO, IDEA, now=NOW)


def test_missing_comments_fail_the_begin_packet_closed(monkeypatch):
    board = Board(_board_rows(), drop_comments=True)

    with monkeypatch.context() as patch:
        _serve(patch, board, full=False)
        with pytest.raises(funnel.GitHubError,
                           match="could not read comments for owner/repo#42"):
            shape.collect(REPO, IDEA, now=NOW)
    assert board.begin_requests and not board.full_requests


@pytest.mark.parametrize("broken", [
    None,
    {"issue": None},
    {"issue": {"comments": {"nodes": None,
                            "pageInfo": {"hasNextPage": False}}}},
    {"issue": {"comments": {"nodes": [],
                            "pageInfo": {"hasNextPage": True,
                                         "endCursor": None}}}},
])
def test_an_incomplete_thread_fails_the_begin_load(monkeypatch, broken):
    board = Board(_board_rows())

    def graphql(query, **variables):
        response = board.graphql(query, **variables)
        if "shapeIssue" in response:
            response["shapeIssue"] = broken
        return response

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    with pytest.raises(funnel.GitHubError, match="comments"):
        REAL_LOAD_ITEMS(include_details=False, member_repo_names=MEMBERS,
                        scope="begin", shape_issue=(REPO, IDEA))


def test_the_begin_document_carries_the_thread_beside_the_project():
    query = funnel._begin_item_query(
        ["open", "claims"], funnel._shape_issue_comments_field(REPO, IDEA))

    assert query.count("shapeIssue:") == 1
    assert 'shapeIssue: repository(owner: "owner", name: "repo")' in query
    assert "issue(number: 42)" in query
    # The field is a top-level selection: after `user { ... }`, before the
    # fragment, and the document's braces still balance.
    head, fragment = query.split("\nfragment BeginItem", 1)
    assert "shapeIssue:" in head and "shapeIssue:" not in fragment
    assert head.index("user(login:") < head.index("shapeIssue:")
    assert head.count("{") == head.count("}")
    # Without a shape issue the document is unchanged.
    assert funnel._begin_item_query(["open"]) == funnel._begin_item_query(
        ["open"], "")


def test_the_full_path_still_reads_the_thread_as_before(monkeypatch):
    board = Board(_board_rows())
    monkeypatch.setattr(funnel, "gh_graphql", board.graphql)

    items = REAL_LOAD_ITEMS(include_details=False, member_repo_names=MEMBERS,
                            shape_issue=(REPO, IDEA))

    idea = funnel.find(items, "{}#{}".format(REPO, IDEA))
    assert idea.issue_comments == THREAD
    assert board.full_requests and not board.begin_requests
    assert board.queries[0] == funnel._item_query_with_shape_comments(
        REPO, IDEA)
