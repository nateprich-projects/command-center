"""Finished projects close themselves from the begin view too (#2147).

Since #1773/#1775 the begin view lists open items through
``STARTABLE_ITEM_NODE_FIELDS``, which carried no ``Origin`` and carries no
``Issue.body``. ``_can_close_itself`` reads both, so in that view every
Improve project read as Nate-origin and never closed: #1744 sat at Building
with 5/5 tickets through a begin on 2026-10-02. The same unloaded body let an
analysis-marked upkeep project close itself there (latent). Begin's
``reconcile_auto_closeable_projects`` and merge's ``_auto_close_parent`` both
run on that view.

The fake Project answers each listing with exactly the fields its
``StartableItem`` fragment asks for, so a listing without ``Origin`` gets no
origin back, as GitHub would. Every read and write goes through fakes.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


REPO = "owner/repo"
NOW = datetime(2026, 10, 2, 9, 0, 0, tzinfo=timezone.utc)

ANALYSIS = funnel.ANALYSIS_MARKER + '\n```json\n{"analysis": true}\n```'


def _marked(marker, **fields):
    return marker + "\n\n```json\n" + json.dumps(fields) + "\n```"


def _override(target, voice):
    """An origin override with the provenance that authorises it."""
    return "\n\n".join((
        "# Plan\n\nRedesign the thing.",
        _marked(funnel.ORIGIN_OVERRIDE_MARKER, target=target),
        _marked(
            funnel.PROVENANCE_MARKER, voice=voice, agent="claude",
            run="run-2147", at="2026-10-01T16:00:00+00:00",
        ),
    ))


class Row:
    """One Project item on the fake board."""

    def __init__(self, number, body="# Plan", *, klass=None, origin="agent",
                 status="Building", parent=None, children=0, done=0,
                 needs="none", risk="standard"):
        self.number = number
        self.body = body
        self.klass = klass
        self.origin = origin
        self.status = status
        self.parent = parent
        self.children = children
        self.done = done
        self.needs = needs
        self.risk = risk

    @property
    def item_id(self):
        return "item-{}".format(self.number)

    @property
    def ref(self):
        return "{}#{}".format(REPO, self.number)

    def listed(self, fragment):
        """This row as the listing's ``StartableItem`` fragment selects it."""
        content = {
            "number": self.number,
            "title": "issue {}".format(self.number),
            "url": "https://github.com/{}/issues/{}".format(
                REPO, self.number),
            "state": "OPEN",
            "stateReason": None,
            "createdAt": "2026-09-01T00:00:00Z",
            "closedAt": None,
            "repository": {"nameWithOwner": REPO},
            "labels": {"nodes": []},
            "parent": (
                {"number": self.parent,
                 "repository": {"nameWithOwner": REPO}}
                if self.parent is not None else None
            ),
            "subIssuesSummary": {
                "total": self.children, "completed": self.done,
            },
            "blockedBy": {"totalCount": 0, "nodes": []},
        }
        if re.search(r"\bbody\b", fragment):
            content["body"] = self.body
        node = {
            "id": self.item_id,
            "claim": None,
            "status": {"name": self.status,
                       "updatedAt": "2026-09-20T00:00:00Z"},
            "class": {"name": self.klass} if self.klass else None,
            "gate": {"name": self.needs},
            "risk": {"name": self.risk},
            "pinned": None,
            "startable": content,
        }
        if 'fieldValueByName(name: "Origin")' in fragment:
            node["origin"] = {"name": self.origin} if self.origin else None
        return node


class Board:
    """Answer the begin listing, body reads, the close's reads and writes.

    ``body_reads`` is the spy: the item ids of every detail read that asked
    for bodies, one list per request. ``status_writes`` and ``gh`` record the
    close's Status write and its ``gh issue`` calls.
    """

    def __init__(self, rows, closed_children=()):
        self.rows = list(rows)
        self.by_id = {row.item_id: row for row in self.rows}
        self.closed_children = list(closed_children)
        self.body_reads = []
        self.status_writes = []
        self.gh = []

    def __call__(self, query, **variables):
        limit = {"cost": 1, "remaining": 4000, "resetAt": "later"}
        if "nodes(ids: $ids)" in query:
            ids = list(variables["ids"])
            assert "timelineItems" not in query, "only a body-only read"
            self.body_reads.append(ids)
            return {"rateLimit": limit, "nodes": [
                {"id": item_id, "content": {"body": self.by_id[item_id].body}}
                for item_id in ids
            ]}
        if query == funnel.CLOSED_ITSELF_TICKETS:
            parent = int(variables["number"])
            nodes = [
                {"number": number, "title": "ticket {}".format(number),
                 "state": "CLOSED",
                 "url": "https://github.com/{}/issues/{}".format(
                     REPO, number),
                 "repository": {"nameWithOwner": REPO}}
                for owner, number in self.closed_children if owner == parent
            ]
            return {"rateLimit": limit, "repository": {"issue": {
                "subIssues": {
                    "totalCount": len(nodes), "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }}}
        if query == funnel.SET_FIELD:
            self.status_writes.append(variables["item"])
            return {}
        aliases = re.findall(r"(\w+): items\(", query)
        assert aliases, "unexpected GraphQL document"
        assert not any(alias.startswith("r") and alias[1:].isdigit()
                       for alias in aliases), "no anchor read expected"
        fragment = query.split("fragment StartableItem on ProjectV2Item", 1)
        assert len(fragment) == 2, "the begin view lists open rows minimally"
        return {"rateLimit": limit, "user": {"projectV2": {
            alias: {
                "nodes": (
                    [row.listed(fragment[1]) for row in self.rows]
                    if alias == "open" else []
                ),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            for alias in aliases
        }}}

    def closed(self):
        """The issue numbers the close wrote ``gh issue close`` for."""
        return [int(argv[3]) for argv in self.gh
                if argv[:3] == ["gh", "issue", "close"]]


def _board(monkeypatch, rows, closed_children=()):
    board = Board(rows, closed_children)
    monkeypatch.setattr(funnel, "gh_graphql", board)
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs", lambda: [])
    monkeypatch.setattr(funnel, "_load_block_comment", lambda item: None)
    monkeypatch.setattr(funnel, "drift_since_approval", lambda item: [])
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "done-option")

    def run(argv, **kwargs):
        board.gh.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    return board


def _begin_view():
    """What ``main`` hands ``cmd_begin``: the scoped, startable load."""
    view = funnel.load_items(
        include_details=False,
        member_repo_names=[REPO],
        scope="begin",
        include_startable=True,
        startable_agent="codex",
    )
    assert funnel.items_scope(view) == "begin"
    return view


def _finished(number, body="# Plan", **kwargs):
    kwargs.setdefault("klass", "Improve")
    return Row(number, body, children=2, done=2, **kwargs)


def _children(*numbers):
    return [(number, 900 + number) for number in numbers]


def test_begin_view_closes_a_finished_agent_origin_improve_project(
    monkeypatch,
):
    project = _finished(1744)
    board = _board(monkeypatch, [project], _children(1744))
    view = _begin_view()
    loaded = {item.ref: item for item in view}[project.ref]

    assert funnel.reconcile_auto_closeable_projects(view) == [project.ref]
    assert board.closed() == [1744]
    assert board.status_writes == [project.item_id]
    assert loaded.state == "CLOSED"
    assert loaded.status == "Done"
    # Decided on the listed Origin and the body read on demand.
    assert loaded.origin == "agent"
    assert board.body_reads == [[project.item_id]]


def test_an_analysis_marked_upkeep_project_waits_in_the_begin_view(
    monkeypatch,
):
    project = _finished(51, "# Findings\n\n" + ANALYSIS, klass="Broken")
    board = _board(monkeypatch, [project], _children(51))
    view = _begin_view()

    assert funnel.reconcile_auto_closeable_projects(view) == []
    assert board.body_reads == [[project.item_id]]
    assert board.closed() == []
    assert board.status_writes == []
    loaded = {item.ref: item for item in view}[project.ref]
    assert loaded.body_loaded and loaded.state == "OPEN"


def test_an_origin_override_to_agents_closes_a_nate_origin_improve_project(
    monkeypatch,
):
    project = _finished(
        61, _override("agents", "nate-relayed"), origin="nate-relayed",
    )
    board = _board(monkeypatch, [project], _children(61))
    view = _begin_view()

    assert funnel.reconcile_auto_closeable_projects(view) == [project.ref]
    assert board.body_reads == [[project.item_id]]
    assert board.closed() == [61]


def test_an_origin_override_to_nate_keeps_an_agent_improve_project_waiting(
    monkeypatch,
):
    project = _finished(62, _override("nate", "agent"))
    board = _board(monkeypatch, [project], _children(62))
    view = _begin_view()

    assert funnel.reconcile_auto_closeable_projects(view) == []
    assert board.body_reads == [[project.item_id]]
    assert board.closed() == []


def test_bodies_are_read_only_for_close_candidates(monkeypatch):
    candidates = [
        _finished(10),                                   # Improve, agent
        _finished(11, ANALYSIS, klass="Maintenance"),    # upkeep, marked
        _finished(12, origin="nate-relayed"),            # Improve, Nate's
        _finished(13, klass="Bug"),
    ]
    others = [
        Row(20, klass="Improve", children=2, done=1),    # a ticket is open
        Row(21, parent=20, klass=None, origin=None, status=None),
        _finished(22, klass="New"),                      # waits at Accept
        _finished(23, klass="Replace"),
        _finished(24, klass=None),                       # no class yet
        _finished(25, status="Ready"),                   # not Building
        Row(26, klass="Improve", status="Shaped"),
        Row(27, klass="Broken", status="Ideas"),
    ]
    board = _board(
        monkeypatch, candidates + others, _children(10, 12, 13),
    )
    view = _begin_view()

    closed = funnel.reconcile_auto_closeable_projects(view)

    assert board.body_reads == [[row.item_id for row in candidates]]
    loaded = {item.ref: item for item in view}
    assert {ref for ref, item in loaded.items() if item.body_loaded} == {
        row.ref for row in candidates
    }
    # Nate's Improve without an override and the analysis project wait.
    assert closed == [REPO + "#10", REPO + "#13"]
    assert board.closed() == [10, 13]


def test_a_full_board_close_reads_no_body(monkeypatch):
    project = _finished(1744)
    board = _board(monkeypatch, [project], _children(1744))
    item = funnel._from_node({
        "id": project.item_id,
        "status": {"name": "Building", "updatedAt": "2026-09-20T00:00:00Z"},
        "class": {"name": "Improve"},
        "origin": {"name": "agent"},
        "content": dict(
            project.listed("")["startable"], body=project.body,
        ),
    })
    assert item.body_loaded

    assert funnel.reconcile_auto_closeable_projects([item]) == [project.ref]
    assert board.body_reads == []


@pytest.mark.parametrize("klass", funnel.LADDER + [None])
def test_the_body_read_covers_every_class_that_can_close(klass):
    """A class the close rule could let through always has its body read."""
    item = funnel.Item(
        repo=REPO, number=1, title="p", url="u", state="OPEN",
        status="Building", klass=klass, origin="agent",
        children_total=2, children_done=2, body=None,
    )
    item.body_loaded = False

    assert funnel._finished_close_candidate(item) is funnel._can_close_itself(
        item
    )


# --------------------------------------------------------------------------
# merge: ``_auto_close_parent`` runs on the same view (BEGIN_VIEW_COMMANDS).


def _last_ticket(parent):
    """The parent's one open ticket, as merge sees it before GitHub closes it."""
    parent.children, parent.done = 2, 1
    return Row(parent.number + 1, parent=parent.number, klass=None,
               origin=None, status=None)


def test_merge_on_the_begin_view_closes_a_finished_agent_improve_parent(
    monkeypatch,
):
    parent = Row(1744, klass="Improve")
    ticket = _last_ticket(parent)
    board = _board(monkeypatch, [parent, ticket], _children(1744))
    view = _begin_view()
    merged = {item.ref: item for item in view}[ticket.ref]

    assert funnel._auto_close_parent(view, merged) is True
    assert board.body_reads == [[parent.item_id]]
    assert board.closed() == [1744]


def test_merge_on_the_begin_view_leaves_an_analysis_parent_at_accept(
    monkeypatch,
):
    parent = Row(51, "# Findings\n\n" + ANALYSIS, klass="Broken")
    ticket = _last_ticket(parent)
    board = _board(monkeypatch, [parent, ticket], _children(51))
    view = _begin_view()
    merged = {item.ref: item for item in view}[ticket.ref]

    assert funnel._auto_close_parent(view, merged) is False
    assert board.body_reads == [[parent.item_id]]
    assert board.closed() == []
    assert board.status_writes == []


def test_merge_reads_no_body_for_a_parent_that_cannot_close(monkeypatch):
    parent = Row(70, klass="New")
    ticket = _last_ticket(parent)
    board = _board(monkeypatch, [parent, ticket])
    view = _begin_view()
    merged = {item.ref: item for item in view}[ticket.ref]

    assert funnel._auto_close_parent(view, merged) is False
    assert board.body_reads == []


# --------------------------------------------------------------------------
# The begin envelope (#1776) pays for the read, as #2067's routed read does.


def _one_body():
    return (
        funnel.BEGIN_DETAIL_BODY_FIELDS_PER_ITEM
        + funnel.BEGIN_DETAIL_QUERY_OVERHEAD
    )


@pytest.mark.parametrize("spare, refuses", [(0, True), (1, False)])
def test_the_close_read_is_charged_and_never_spends_the_claim_reserve(
    monkeypatch, spare, refuses,
):
    project = _finished(1744)
    board = _board(monkeypatch, [project], _children(1744))
    # The listing's row, the read and its request beside the full reserve,
    # less one unit when it must refuse.
    envelope = funnel.BeginWorkEnvelope(
        limit=funnel.BEGIN_DETAIL_WORK_RESERVE + 1 + _one_body() + spare
    )
    monkeypatch.setattr(funnel, "_ACTIVE_BEGIN_ENVELOPE", envelope)
    view = _begin_view()
    assert envelope.listed_items == 1

    if refuses:
        with pytest.raises(funnel.BeginCannotComplete):
            funnel.reconcile_auto_closeable_projects(view)
        assert board.body_reads == []
        assert envelope.hydrated_fields == 0
        assert board.closed() == []
    else:
        # Stop before the close's own gh calls spend the envelope.
        monkeypatch.setattr(
            funnel, "_close_auto_closeable_project",
            lambda project, **kwargs: False,
        )
        funnel.reconcile_auto_closeable_projects(view)
        assert board.body_reads == [[project.item_id]]
        assert envelope.hydrated_fields == _one_body()
        assert envelope.remaining == funnel.BEGIN_DETAIL_WORK_RESERVE + 1


# --------------------------------------------------------------------------
# Listing Origin must not let the Shaped sweep decide on an unread body.


def test_the_shaped_sweep_does_not_advance_a_plan_whose_body_was_not_read(
    monkeypatch,
):
    """An override toward Nate lives in the body the begin view leaves out."""
    plan = Row(80, _override("nate", "agent"), klass="Broken",
               status="Shaped")
    board = _board(monkeypatch, [plan])
    writes = []
    monkeypatch.setattr(
        funnel, "_write_status",
        lambda item, status, now: writes.append((item.ref, status)),
    )
    view = _begin_view()
    loaded = {item.ref: item for item in view}[plan.ref]
    assert loaded.origin == "agent" and loaded.body_loaded is False

    advanced, errors = funnel.sweep_shaped_self_approvals(view, NOW)

    assert (advanced, errors, writes) == ([], [], [])
    assert board.gh == []
