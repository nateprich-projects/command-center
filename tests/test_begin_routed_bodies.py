"""Decline routes are decided before ranking, not after claim (#2067).

Since #1775 the shared queue/begin listing leaves ``Issue.body`` unloaded, and
``_decline_route_withholds_startability`` defers the two routes that read a
body. On 2026-09-30 that ranked a Bug whose ``unsatisfiable-acceptance`` route
still matched its body as startable: every begin claimed it, found it withheld
after the post-claim refresh, released it and stopped, while other work was
waiting. The fix reads only the routed rows' bodies before ranking, through
the same detail fetch begin uses after claim.

Every GitHub read and write here goes through fakes; nothing reaches GitHub.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


REPO = "owner/repo"
NOW = datetime(2026, 9, 30, 18, 0, 0, tzinfo=timezone.utc)
CLAIM = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")

ORIGINAL = (
    "What: fix the thing.\n\n"
    "Accept: an agent can never meet this condition.\n\nRisk: standard"
)
DIGEST = hashlib.sha256(ORIGINAL.encode("utf-8")).hexdigest()
UNSATISFIABLE = {
    "type": "unsatisfiable-acceptance",
    "acceptance_digest": DIGEST,
}
WITHHELD = "ticket's decline route still withholds work"


def _content(number, *, parent=None, children=0, body=None):
    content = {
        "number": number,
        "title": "issue {}".format(number),
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "state": "OPEN",
        "stateReason": None,
        "createdAt": "2026-09-01T00:00:00Z",
        "closedAt": None,
        "repository": {"nameWithOwner": REPO},
        "labels": {"nodes": []},
        "parent": (
            {"number": parent, "repository": {"nameWithOwner": REPO}}
            if parent is not None else None
        ),
        "subIssuesSummary": {"total": children, "completed": 0},
        "blockedBy": {"totalCount": 0, "nodes": []},
    }
    if body is not None:
        content["body"] = body
        content["assignees"] = {"nodes": []}
    return content


class Row:
    """One Project item, rendered as either Project projection."""

    def __init__(self, number, body, *, klass=None, parent=None,
                 needs="none", children=0, updated="2026-09-20T00:00:00Z",
                 status="Building"):
        self.number = number
        self.body = body
        self.klass = klass
        self.parent = parent
        self.needs = needs
        self.children = children
        self.updated = updated
        self.status = status

    @property
    def item_id(self):
        return "item-{}".format(self.number)

    @property
    def ref(self):
        return "{}#{}".format(REPO, self.number)

    def _fields(self):
        return {
            "id": self.item_id,
            "status": {"name": self.status, "updatedAt": self.updated},
            "class": {"name": self.klass} if self.klass else None,
            "risk": {"name": "standard"},
            "pinned": None,
        }

    def minimal(self):
        """The shared startable listing's row: no body (#1773, #1775)."""
        node = self._fields()
        node.update(
            claim=None,
            gate={"name": self.needs},
            startable=_content(
                self.number, parent=self.parent, children=self.children,
            ),
        )
        return node

    def full(self):
        """The full board's row, which carries the body."""
        node = self._fields()
        node.update(
            lock=None,
            origin={"name": "agent"},
            needs={"name": self.needs},
            content=_content(
                self.number, parent=self.parent, children=self.children,
                body=self.body,
            ),
        )
        return node


class Board:
    """Answer every GraphQL document the listing, queue and begin send.

    ``events`` records each detail read that asked for bodies, as
    ``("body", [item ids])``, in order with the claim writes the begin
    fixture adds. ``body_reads`` is the same reads alone: the spy.
    """

    def __init__(self, rows, events):
        self.rows = list(rows)
        self.by_id = {row.item_id: row for row in self.rows}
        self.events = events
        self.body_reads = []

    def __call__(self, query, **variables):
        limit = {"cost": 1, "remaining": 4000, "resetAt": "later"}
        if "nodes(ids: $ids)" in query:
            ids = list(variables["ids"])
            with_body = "body" in query
            if with_body:
                self.body_reads.append(ids)
                self.events.append(("body", ids))
            nodes = []
            for item_id in ids:
                content = {"timelineItems": {"nodes": []}}
                if with_body:
                    content["body"] = self.by_id[item_id].body
                nodes.append({"id": item_id, "content": content})
            if "children: nodes(ids: $childIds)" in query:
                return {
                    "rateLimit": limit,
                    "history": nodes,
                    "children": [
                        {"id": item_id,
                         "content": {"subIssues": {"nodes": []}}}
                        for item_id in variables.get("childIds", [])
                    ],
                }
            return {"rateLimit": limit, "nodes": nodes}
        if query == funnel.ITEM_QUERY:
            return {"rateLimit": limit, "user": {"projectV2": {"items": {
                "nodes": [row.full() for row in self.rows],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        aliases = re.findall(r"(\w+): items\(", query)
        assert aliases, "unexpected GraphQL document"
        assert not any(alias.startswith("r") and alias[1:].isdigit()
                       for alias in aliases), "no anchor read expected"
        return {"rateLimit": limit, "user": {"projectV2": {
            alias: {
                "nodes": (
                    [row.minimal() for row in self.rows]
                    if alias == "open" else []
                ),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
            for alias in aliases
        }}}


def _board(monkeypatch, rows, routes):
    """Install the fake Project; ``routes`` maps a ref to its decline route."""
    events = []
    board = Board(rows, events)
    monkeypatch.setattr(funnel, "gh_graphql", board)
    monkeypatch.setattr(funnel, "_heartbeat_bound_refs", lambda: [])

    def block_comment(item):
        item.decline_route = routes.get(item.ref)

    monkeypatch.setattr(funnel, "_load_block_comment", block_comment)
    return board


def _bug_and_improve(bug_body):
    """The oldest Bug carries the route; a newer Improve ticket waits."""
    return [
        Row(1, "Gates: none", klass="Bug", children=1),
        Row(2, bug_body, parent=1, needs="agent",
            updated="2026-09-10T00:00:00Z"),
        Row(3, "Gates: none", klass="Improve", children=1),
        Row(4, "What: ordinary work.\n\nRisk: standard", parent=3,
            updated="2026-09-25T00:00:00Z"),
    ]


def _begin_view():
    """What ``main`` hands ``cmd_begin``: the scoped, startable load."""
    return funnel.load_items(
        include_details=False,
        member_repo_names=[REPO],
        scope="begin",
        include_startable=True,
        startable_agent="codex",
    )


def _queue_view():
    """What ``main`` hands ``cmd_queue``: the full board plus the listing."""
    return funnel.load_items(
        include_details=funnel.project_load_reads_history("queue"),
        member_repo_names=[REPO],
        include_startable=True,
        startable_agent="codex",
    )


def _stub_begin(monkeypatch, events, current_bodies=None):
    """Stub every begin side effect but the claim writes it records."""
    for name in (
        "reconcile_approved_merges", "reconcile_auto_closeable_projects",
        "reconcile_closed_items", "reconcile_parked_wakes",
        "reconcile_closed_claims", "reconcile_orphaned_starts",
        "reconcile_abandoned_claims", "clear_satisfied_blocks",
    ):
        monkeypatch.setattr(funnel, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "awaiting_review",
                        lambda *args, **kwargs: set())
    monkeypatch.setattr(funnel, "finished_by_comments", lambda rows: set())
    monkeypatch.setattr(funnel, "_backoff_rows", lambda: [])
    monkeypatch.setattr(funnel, "read_lock", lambda item: None)
    monkeypatch.setattr(funnel, "ensure_ticket_branch",
                        lambda repo, number: "fixture-sha")
    monkeypatch.setattr(
        funnel, "implementation_packet",
        lambda repo, number, agent: {"repo": repo,
                                     "ticket": {"number": number}},
    )
    monkeypatch.setattr(heartbeat, "record_binding",
                        lambda *args, **kwargs: "pushed")
    writes = []

    def write_lock(item, value):
        writes.append((item.ref, value))
        events.append(("claim", item.ref, value))

    monkeypatch.setattr(funnel, "write_lock", write_lock)
    return writes


def _run_begin(monkeypatch, capsys, view, detail_loader):
    assert funnel.cmd_begin(
        view, NOW, "codex", "standard", False,
        _detail_loader=detail_loader,
        _preflight=({"agent": "codex", "run": "run-id", "gate": "ok"},
                    {"windows": {}}),
        _pr_facts={},
    ) == 0
    return json.loads(capsys.readouterr().out)


def _hydrating(view):
    """Begin's post-claim refresh, exactly as ``main`` wires it."""
    def load(candidates, include_body=False, include_history=False):
        funnel.hydrate_item_details(
            view, candidates,
            include_body=include_body, include_history=include_history,
        )
    return load


def _run_queue(monkeypatch, capsys, view):
    monkeypatch.setattr(funnel, "_backoff_rows", lambda: [])
    monkeypatch.setattr(funnel, "finished_by_comments_runs",
                        lambda rows: {})
    assert funnel.cmd_queue(view, NOW, pr_facts={}) == 0
    output = capsys.readouterr().out
    section = output.split("Startable by Codex", 1)[1]
    return output, section.split("\n\n", 1)[0]


def test_a_withheld_bug_is_dropped_before_ranking_so_begin_takes_the_next(
    monkeypatch, capsys,
):
    rows = _bug_and_improve(ORIGINAL)
    bug, other = rows[1], rows[3]
    board = _board(monkeypatch, rows, {bug.ref: UNSATISFIABLE})
    writes = _stub_begin(monkeypatch, board.events)

    view = _begin_view()

    assert [item.ref for item in view.startable_candidates] == [other.ref]
    result = _run_begin(monkeypatch, capsys, view, _hydrating(view))

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == other.ref
    assert writes == [(other.ref, CLAIM)]
    # The spy: only the routed Bug's body is read before ranking, and only
    # the selected ticket's after its claim. No parent, and not the
    # unrouted ticket before it was chosen.
    assert board.events == [
        ("body", [bug.item_id]),
        ("claim", other.ref, CLAIM),
        ("body", [other.item_id]),
    ]

    board.body_reads.clear()
    output, startable = _run_queue(monkeypatch, capsys, _queue_view())

    assert startable.startswith(" (1)")
    assert other.ref in startable
    assert bug.ref not in startable
    assert board.body_reads == [[bug.item_id]]


def test_an_edited_acceptance_makes_the_routed_bug_startable_again(
    monkeypatch, capsys,
):
    rows = _bug_and_improve(
        ORIGINAL + "\n\nAccept revised: the check now runs offline."
    )
    bug, other = rows[1], rows[3]
    board = _board(monkeypatch, rows, {bug.ref: UNSATISFIABLE})
    writes = _stub_begin(monkeypatch, board.events)

    view = _begin_view()

    # Decided on the loaded body, not deferred past the claim.
    assert board.body_reads == [[bug.item_id]]
    assert bug.ref in [item.ref for item in view.startable_candidates]
    result = _run_begin(monkeypatch, capsys, view, _hydrating(view))

    # Nothing has started yet, so the Bugs' turn takes the Bug.
    assert result["do"] == "ticket"
    assert result["work"]["ref"] == bug.ref
    assert writes == [(bug.ref, CLAIM)]
    assert board.events == [
        ("body", [bug.item_id]),
        ("claim", bug.ref, CLAIM),
        ("body", [bug.item_id]),
    ]

    board.body_reads.clear()
    output, startable = _run_queue(monkeypatch, capsys, _queue_view())

    assert startable.startswith(" (2)")
    assert bug.ref in startable
    assert other.ref in startable
    assert board.body_reads == [[bug.item_id]]
