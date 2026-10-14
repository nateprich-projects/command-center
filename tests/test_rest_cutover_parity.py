"""REST cutover parity: paired REST/GraphQL fixtures, one ordering (#2423).

``funnel brief`` and each lane's ``begin`` share their ordering and gate
outputs through pure helpers (``startable``, ``next_ticket``,
``split_decisions``, ``gate_question``). This file builds one board twice —
once as GraphQL Project rows through ``_from_node``, once as REST item rows
through ``rest_item_to_item`` — and requires identical ordering and gate
output from both. A row whose routing value is unreadable on either path
stays out of the work queue instead of joining it silently.

A second group pins the spend split the cutover exists for: a REST board
load costs no GraphQL call, and the time-at-gate read costs exactly the one
GraphQL query REST cannot express.

Every transport goes through fakes; nothing here reaches GitHub.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "nateprich/command-center"
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
AT = "2026-10-01T00:00:00Z"
LOCK_AT = "2026-10-02T00:00:00Z"


def reset():
    funnel.reset_api_usage()


def _content(number, *, state="OPEN", reason=None, parent=None,
             blocked_by=(), children=(0, 0), labels=(), title=None):
    total, completed = children
    return {
        "number": number,
        "title": title or "issue {}".format(number),
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "state": state,
        "stateReason": reason,
        "body": "body {}".format(number),
        "createdAt": AT,
        "closedAt": None,
        "repository": {"nameWithOwner": REPO},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "assignees": {"nodes": [{"login": "octo"}]},
        "parent": (
            {"number": parent,
             "repository": {"nameWithOwner": REPO}}
            if parent is not None else None
        ),
        "subIssuesSummary": {"total": total, "completed": completed},
        "blockedBy": {
            "totalCount": len(blocked_by),
            "nodes": [
                {
                    "number": ref,
                    "state": "OPEN",
                    "stateReason": None,
                    "repository": {"nameWithOwner": REPO},
                }
                for ref in blocked_by
            ],
        },
    }


def _graph_node(number, *, status, klass, origin="agent", risk="standard",
                needs="none", domain="command-center", pinned=None,
                lock=None, **content_kw):
    node = {
        "id": "PVTI_{}".format(number),
        "status": {"name": status, "updatedAt": AT},
        "class": {"name": klass},
        "origin": {"name": origin},
        "risk": {"name": risk},
        "gate": {"name": needs},
        "domain": {"name": domain},
        "content": _content(number, **content_kw),
    }
    if pinned is not None:
        node["pinned"] = {"name": pinned}
    if lock is not None:
        node["claim"] = {"text": lock}
    return node


def _rest_field(field_id, raw, **extra):
    entry = {"field_id": field_id,
             "value": {"name": {"raw": raw}}}
    entry.update(extra)
    return entry


def _rest_row(number, *, status=None, klass=None, origin="agent",
              risk="standard", needs="none", domain="command-center",
              pinned=None, lock=None, **content_kw):
    fields = [
        {"name": "Origin", "value": {"name": {"raw": origin}}},
        {"name": "Risk", "value": {"name": {"raw": risk}}},
        _rest_field(funnel.NEEDS_FIELD_ID, needs),
        {"name": "Domain", "value": {"name": {"raw": domain}}},
    ]
    if status is not None:
        fields.append(
            _rest_field(funnel.STATUS_FIELD_ID, status, updatedAt=AT))
    if klass is not None:
        fields.append(_rest_field(funnel.CLASS_FIELD_ID, klass))
    if pinned is not None:
        fields.append(_rest_field(funnel.PINNED_FIELD_ID, pinned))
    if lock is not None:
        fields.append({"field_id": funnel.LOCK_FIELD_ID,
                       "value": {"text": lock}})
    content = _content(number, **content_kw)
    content["labels"] = [
        node["name"] for node in content["labels"]["nodes"]
    ]
    content["assignees"] = [
        node["login"] for node in content["assignees"]["nodes"]
    ]
    return {"id": "PVTI_{}".format(number), "fields": fields,
            "content": content}


def _board_specs():
    """One board as paired keyword specs: (number, kwargs) for each row."""
    return [
        (1, {"status": "Ready", "klass": "Implement", "children": (2, 0)}),
        (2, {"status": None, "klass": None, "parent": 1, "lock": LOCK_AT}),
        (3, {"status": None, "klass": None, "parent": 1,
             "labels": ("blocked",), "blocked_by": (9,)}),
        (4, {"status": "Ready", "klass": "Broken", "children": (1, 0)}),
        (5, {"status": None, "klass": None, "parent": 4}),
        (6, {"status": "Done", "klass": "Broken", "state": "CLOSED",
             "reason": "COMPLETED"}),
        (7, {"status": "Parked", "klass": "Implement", "state": "CLOSED",
             "reason": "NOT_PLANNED"}),
        (8, {"status": "Shaped", "klass": "Broken", "origin": "Nate"}),
        (9, {"status": "Shaped", "klass": "Implement", "origin": "Nate"}),
    ]


def _graphql_board():
    rows = []
    for number, kw in _board_specs():
        kw = dict(kw)
        status = kw.pop("status")
        klass = kw.pop("klass")
        node = _graph_node(number, status=status, klass=klass, **kw)
        if status is None:
            del node["status"]
            node["needs"] = node.pop("gate")
        if klass is None:
            del node["class"]
        rows.append(funnel._from_node(node))
    return [row for row in rows if row is not None]


def _rest_board():
    rows = []
    for number, kw in _board_specs():
        rows.append(funnel.rest_item_to_item(_rest_row(number, **dict(kw))))
    return [row for row in rows if row is not None]


def _compared(item):
    return (
        item.repo, item.number, item.title, item.url, item.state,
        item.body, item.state_reason,
        tuple(item.labels), tuple(item.assignees), item.parent,
        item.children_total, item.children_done,
        tuple(item.blocked_by_refs or []),
        tuple(item.open_blockers), tuple(item.dead_blockers),
        item.item_id, item.in_motion_since, item.status_updated_at,
        item.created_at, item.closed_at, item.pinned,
    )


def _routing(item):
    return (item.status, item.klass, item.origin, item.risk, item.needs,
            item.domain)


def _routing_equal(left, right):
    """Blank GraphQL fields read None; the same REST row reads the explicit
    unknown marker (#2421). Both mean unroutable: no consumer assigns a
    queue, gate, or rank from either (see the fail-closed test below)."""
    pairs = zip(_routing(left), _routing(right))
    return all(
        graph == rest or (
            graph is None and rest == funnel.REST_UNKNOWN_VALUE
        )
        for graph, rest in pairs
    )


def test_paired_rows_convert_to_identical_items():
    """The same board reads the same through REST and GraphQL."""
    reset()
    graphql_items = _graphql_board()
    rest_items = _rest_board()

    assert len(graphql_items) == len(rest_items) == 9
    gaps = 0
    for left, right in zip(graphql_items, rest_items):
        assert _compared(left) == _compared(right), left.ref
        assert _routing_equal(left, right), (left.ref, _routing(left),
                                             _routing(right))
        gaps += sum(
            1 for graph, rest in zip(_routing(left), _routing(right))
            if graph != rest
        )
    # The only representation gap is the three tickets' blank Status/Class,
    # which they inherit from their parent rather than reading themselves.
    assert gaps == 6


def test_brief_and_begin_share_identical_ordering_and_gate_output():
    """Startable order, next pick, and gate payloads agree on both paths."""
    reset()
    graphql_items = _graphql_board()
    rest_items = _rest_board()

    graphql_by_ref = {item.ref: item for item in graphql_items}
    rest_by_ref = {item.ref: item for item in rest_items}

    assert [item.ref for item in funnel.startable(graphql_items)] == [
        item.ref for item in funnel.startable(rest_items)
    ]
    assert [item.ref for item in funnel.startable(graphql_items)]

    graphql_pick = funnel.next_ticket(graphql_items, NOW)
    rest_pick = funnel.next_ticket(rest_items, NOW)
    assert (graphql_pick.ref if graphql_pick else None) == (
        rest_pick.ref if rest_pick else None
    )

    graphql_decisions, graphql_watch = funnel.split_decisions(graphql_items)
    rest_decisions, rest_watch = funnel.split_decisions(rest_items)
    assert [item.ref for item in graphql_decisions] == [
        item.ref for item in rest_decisions
    ]
    assert [item.ref for item in graphql_watch] == [
        item.ref for item in rest_watch
    ]
    assert [
        funnel.gate_question(item, graphql_by_ref) for item in graphql_items
    ] == [
        funnel.gate_question(item, rest_by_ref) for item in rest_items
    ]
    assert [
        funnel.item_json(item, NOW, graphql_by_ref)
        for item in graphql_decisions
    ] == [
        funnel.item_json(item, NOW, rest_by_ref) for item in rest_decisions
    ]


def test_unreadable_routing_value_stays_out_of_the_queue_on_both():
    """An unknown Status never silently joins the work queue (review focus).

    The GraphQL row carries a name outside the known stages; the REST row
    reads it as the explicit unknown marker. Both rows stay out of
    ``startable`` and out of the next-ticket pick.
    """
    reset()
    graph_parent = funnel._from_node(
        _graph_node(11, status="Frobnicate", klass="Broken",
                    children=(1, 0)))
    graph_ticket = funnel._from_node(
        _graph_node(12, status="Ready", klass="Broken", parent=11))
    rest_parent = funnel.rest_item_to_item(
        _rest_row(11, status="Frobnicate", klass="Broken",
                  children=(1, 0)))
    rest_ticket = funnel.rest_item_to_item(
        _rest_row(12, status="Ready", klass="Broken", parent=11))

    assert rest_parent is not None and graph_parent is not None
    assert rest_parent.status == funnel.REST_UNKNOWN_VALUE
    assert graph_parent is not None and graph_parent.status == "Frobnicate"

    for board in ([graph_parent, graph_ticket],
                  [rest_parent, rest_ticket]):
        assert funnel.startable(board) == []
        assert funnel.next_ticket(board, NOW) is None


def test_rest_board_load_costs_no_graphql_call(monkeypatch):
    """Project list pages ride the core pool; GraphQL spend stays zero."""
    reset()
    pages = [
        {"items": [_rest_row(21, status="Ready", klass="Broken")],
         "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"}},
        {"items": [_rest_row(22, status="Building",
                             klass="Implement")]},
    ]

    def run(args, **kwargs):
        assert args[:2] == ["gh", "api"]
        assert args[2].startswith(funnel.REST_PROJECT_ITEMS_PATH)
        return SimpleNamespace(stdout=json.dumps(pages.pop(0)), stderr="",
                               returncode=0)

    monkeypatch.setattr(funnel.subprocess, "run", run)
    items = funnel.load_rest_project_items(list(funnel.REST_BOARD_FIELD_IDS))

    assert [(item.number, item.status) for item in items] == [
        (21, "Ready"), (22, "Building"),
    ]
    assert funnel.rest_spend() == {"calls": 2}
    assert funnel.graphql_spend()["calls"] == 0


def test_time_at_gate_costs_a_single_graphql_query(monkeypatch):
    """The one query REST cannot express: status-change timeline events."""
    reset()
    seen = []

    def run(args, **kwargs):
        seen.append(list(args))
        payload = {
            "data": {
                "rateLimit": {"cost": 1, "remaining": 4999,
                              "resetAt": "2026-10-15T00:00:00Z"},
                "repository": {
                    "issue": {"timelineItems": {"nodes": []}},
                },
            },
        }
        return SimpleNamespace(stdout=json.dumps(payload), stderr="",
                               returncode=0)

    monkeypatch.setattr(funnel, "_run_gh", run)
    item = funnel.Item(repo=REPO, number=7, title="t", url="",
                       state="OPEN", item_id="PVTI_7")

    assert funnel._project_status_times(item) == {
        "Ready": None, "Building": None,
    }
    assert len(seen) == 1
    assert any(
        "PROJECT_V2_ITEM_STATUS_CHANGED_EVENT" in part for part in seen[0]
    )
    assert funnel.graphql_spend()["calls"] == 1
    assert funnel.rest_spend() == {"calls": 0}
