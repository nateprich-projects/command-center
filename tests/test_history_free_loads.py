"""Shape and finish-ticket loads fetch only the Project items they use (#1623).

``shape-packet`` remains a history-free board reader. ``shape-apply`` reads
its idea and parent by ref; ``release_claim`` reads its ticket by ref. Each
changed caller is compared with the old full-board behavior against one
fixture board served through a fake ``gh_graphql``. Output and writes must
match, and the new path must make no ``nodes(ids:`` history request. A
by-ref miss must fall back to the full board without history.
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import funnel  # noqa: E402
from engine import implement, shape  # noqa: E402

REPO = "owner/repo"
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
IDEA = 42
TICKET = 11


def _node(number, *, status, klass=None, origin=None, parent=None,
          state="OPEN", labels=(), children=(0, 0), body="", lock=None):
    """One compact Project row: no timelineItems, no subIssues."""
    return {
        "id": "PVTI_{}".format(number),
        "lock": {"text": lock} if lock else None,
        "status": {"name": status},
        "class": {"name": klass} if klass else None,
        "origin": {"name": origin} if origin else None,
        "risk": {"name": "standard"},
        "pinned": None,
        "needs": {"name": "none"},
        "content": {
            "number": number,
            "title": "issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(REPO, number),
            "body": body,
            "state": state,
            "stateReason": "COMPLETED" if state == "CLOSED" else None,
            "createdAt": "2026-09-01T00:00:00Z",
            "closedAt": "2026-09-20T00:00:00Z" if state == "CLOSED" else None,
            "repository": {"nameWithOwner": REPO},
            "labels": {"nodes": [{"name": label} for label in labels]},
            "assignees": {"nodes": []},
            "parent": (
                {"number": parent, "repository": {"nameWithOwner": REPO}}
                if parent is not None else None
            ),
            "subIssuesSummary": {
                "total": children[0], "completed": children[1],
            },
            "blockedBy": {"totalCount": 0, "nodes": []},
        },
    }


IDEA_BODY = "Captured note.\n\n" + funnel.origin_block(
    "agent", at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    run="capture-run", agent="muse")

# Two pages, so the fake exercises pagination and the shape-comment field
# that rides only the first page.
PAGES = [
    [
        _node(10, status="Building", klass="Improve", origin="nate",
              children=(2, 1), body="# Plan\n\nThe parent plan."),
        _node(TICKET, status="Building", parent=10,
              lock="2026-09-26T11:00:00Z", body="Ticket body."),
        _node(12, status="Done", parent=10, state="CLOSED"),
    ],
    [
        _node(IDEA, status="Ideas", klass="Improve", origin="agent",
              labels=("needs-shaping",), body=IDEA_BODY),
        _node(43, status="Shaped", klass="Maintain", origin="agent",
              body="# Plan\n\nA sibling plan."),
    ],
]

SHAPE_COMMENTS = [{
    "author": {"login": "nateprich"},
    "body": "The old premise is false.",
    "createdAt": "2026-09-25T01:00:00Z",
}]


def _status_event(at, previous, status):
    return {
        "__typename": "ProjectV2ItemStatusChangedEvent",
        "createdAt": at, "previousStatus": previous, "status": status,
        "project": {"number": funnel.PROJECT_NUMBER},
    }


# Real, non-empty history for every row: a caller that read any of it would
# see different values between the two loads.
HISTORY = {
    "PVTI_10": [_status_event("2026-09-10T00:00:00Z", "Ready", "Building")],
    "PVTI_11": [
        _status_event("2026-09-24T00:00:00Z", "Ready", "Building"),
        {"__typename": "LabeledEvent", "createdAt": "2026-09-24T01:00:00Z",
         "label": {"name": "blocked"}},
        {"__typename": "UnlabeledEvent", "createdAt": "2026-09-24T02:00:00Z",
         "label": {"name": "blocked"}},
    ],
    "PVTI_12": [_status_event("2026-09-20T00:00:00Z", "Building", "Done")],
    "PVTI_42": [_status_event("2026-09-25T00:00:00Z", None, "Ideas")],
    "PVTI_43": [_status_event("2026-09-22T00:00:00Z", "Ideas", "Shaped")],
}
CHILDREN = {
    "PVTI_10": [
        {"createdAt": "2026-09-11T00:00:00Z", "closedAt": None},
        {"createdAt": "2026-09-12T00:00:00Z",
         "closedAt": "2026-09-20T00:00:00Z"},
    ],
}

ALL_OPTIONS = sorted(set(funnel.LADDER) | {
    "Ideas", "Shaped", "Ready", "Building", "Done", "Parked",
})


class Board:
    """A fake GitHub: Project pages, history batch, and recorded writes."""

    def __init__(self):
        self.pages = copy.deepcopy(PAGES)
        self.queries = []
        self.ref_queries = []
        self.missing_refs = set()
        self.writes = []
        self.argv = []

    def graphql(self, query, **variables):
        self.queries.append(query)
        compact = " ".join(query.split())
        if "nodes(ids:" in compact:
            return self._details(variables)
        if re.search(r"r\d+: items\(first: \d+, query:", compact):
            return self._refs_page(query)
        aliases = re.findall(r"(\w+): items\(", query)
        if aliases:
            return self._begin_page(query, aliases, variables)
        if "items(first:" in compact:
            return self._page(query, variables)
        if query in (funnel.SET_FIELD, funnel.SET_LOCK):
            self.writes.append(
                ("SET_FIELD" if query == funnel.SET_FIELD else "SET_LOCK",
                 sorted(variables.items())))
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": variables["item"]},
            }}
        if "ProjectV2SingleSelectField" in compact:
            return {"node": {"options": [
                {"id": "opt-{}".format(name), "name": name}
                for name in ALL_OPTIONS
            ]}}
        raise AssertionError("unexpected GraphQL request: {}".format(
            compact[:120]))

    def _refs_page(self, query):
        compact = " ".join(query.split())
        matches = re.findall(
            r'r(\d+): items\(first: \d+, query: "repo:([^ ]+) #(\d+)"\)',
            compact,
        )
        refs = ["{}#{}".format(repo, number) for _, repo, number in matches]
        self.ref_queries.append(refs)
        rows = [node for page in self.pages for node in page]
        project = {}
        for alias, repo, number in matches:
            ref = "{}#{}".format(repo, number)
            row = next((node for node in rows
                        if node["content"]["repository"]["nameWithOwner"]
                        == repo
                        and str(node["content"]["number"]) == number), None)
            nodes = [] if ref in self.missing_refs or row is None else [
                copy.deepcopy(row)]
            project["r{}".format(alias)] = {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        return {"user": {"projectV2": project}}

    def _page(self, query, variables):
        index = int(variables.get("cursor") or 0)
        response = {"user": {"projectV2": {"items": {
            "nodes": copy.deepcopy(self.pages[index]),
            "pageInfo": {
                "hasNextPage": index + 1 < len(self.pages),
                "endCursor": (str(index + 1)
                              if index + 1 < len(self.pages) else None),
            },
        }}}}
        if "shapeIssue:" in query:
            assert index == 0
            response["shapeIssue"] = {"issue": {"comments": {
                "nodes": copy.deepcopy(SHAPE_COMMENTS),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}
        return response

    def _begin_page(self, query, aliases, variables):
        """The aliased begin document (#1625): ``open`` pages the open rows
        of each full page in turn; no closed row here matches a closed-set
        filter (#12 is a Done child), so the other connections are empty."""
        project = {}
        for alias in aliases:
            if alias != "open":
                project[alias] = {"nodes": [], "pageInfo": {
                    "hasNextPage": False, "endCursor": None}}
                continue
            index = int(variables.get("openCursor") or 0)
            more = index + 1 < len(self.pages)
            project[alias] = {
                "nodes": [copy.deepcopy(node) for node in self.pages[index]
                          if node["content"]["state"] == "OPEN"],
                "pageInfo": {"hasNextPage": more,
                             "endCursor": str(index + 1) if more else None},
            }
        response = {"user": {"projectV2": project}}
        if "shapeIssue:" in query:
            assert "openCursor" not in variables
            response["shapeIssue"] = {"issue": {"comments": {
                "nodes": copy.deepcopy(SHAPE_COMMENTS),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}
        return response

    def _details(self, variables):
        history = [
            {"id": item_id, "content": {"timelineItems": {
                "nodes": copy.deepcopy(HISTORY.get(item_id, [])),
            }}}
            for item_id in variables["ids"]
        ]
        if "childIds" not in variables:
            return {"nodes": history}
        children = [
            {"id": item_id, "content": {"subIssues": {
                "nodes": copy.deepcopy(CHILDREN.get(item_id, [])),
            }}}
            for item_id in variables["childIds"]
        ]
        return {"history": history, "children": children}

    def run(self, args, capture_output=False, text=True, **kwargs):
        args = tuple(args)
        self.argv.append(args)
        if args[:3] == ("gh", "issue", "view") and args[-2:] == (
                "--json", "state"):
            return SimpleNamespace(
                returncode=0, stdout='{"state": "OPEN"}', stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def detail_requests(self):
        return [q for q in self.queries if "nodes(ids:" in " ".join(q.split())]


REAL_LOAD_ITEMS = funnel.load_items
REAL_LOAD_PROJECT_ITEMS_BY_REFS = funnel.load_project_items_by_refs


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


def _serve(monkeypatch, *, full, missing_refs=()):
    """Install a fresh board; ``full`` forces the old full-board path."""
    board = Board()
    board.missing_refs.update(missing_refs)
    monkeypatch.setattr(funnel, "member_repos", lambda: [REPO])
    monkeypatch.setattr(funnel, "gh_graphql", board.graphql)
    monkeypatch.setattr(funnel.subprocess, "run", board.run)
    monkeypatch.setattr(shape, "datetime", FixedDatetime)
    monkeypatch.setattr(implement, "datetime", FixedDatetime)
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: board.writes.append(
            ("select", item_id, field, value, ref)))
    loaded = []
    real = REAL_LOAD_ITEMS

    def load_items(**kwargs):
        if full:
            kwargs["include_details"] = True
        items = real(**kwargs)
        # Snapshot history as loaded: a confirmed Status write later sets
        # status_since on the written item, which is not a read.
        loaded.append([
            (item.status_since, list(item.status_events), item.blocked_since,
             item.blocked_cleared_at, item.first_child_created_at,
             item.last_child_closed_at)
            for item in items
        ])
        return items

    def load_project_items_by_refs(refs, **kwargs):
        if full:
            return None
        return REAL_LOAD_PROJECT_ITEMS_BY_REFS(refs, **kwargs)

    monkeypatch.setattr(funnel, "load_items", load_items)
    monkeypatch.setattr(funnel, "load_project_items_by_refs",
                        load_project_items_by_refs)
    board.loaded = loaded
    return board


def _assert_parity_shape(old, new):
    """The baseline had history; the by-ref path asked for none."""
    assert old.detail_requests, "fixture must exercise the history batch"
    empty = (None, [], None, None, None, None)
    assert all(row != empty for row in old.loaded[0]), (
        "every row of the old load must carry history for the parity to "
        "mean anything")
    assert new.detail_requests == []
    assert all(
        row == empty
        for loaded_rows in new.loaded
        for row in loaded_rows
    )


def _run_both(monkeypatch, call):
    results = []
    for full in (True, False):
        with monkeypatch.context() as patch:
            board = _serve(patch, full=full)
            results.append((board, call()))
    (old, old_out), (new, new_out) = results
    _assert_parity_shape(old, new)
    return old, old_out, new, new_out


def test_shape_packet_is_identical_without_history(monkeypatch):
    old, old_out, new, new_out = _run_both(
        monkeypatch,
        lambda: json.dumps(shape.collect(REPO, IDEA, now=NOW),
                           indent=2, sort_keys=True),
    )
    assert new_out == old_out
    packet = json.loads(new_out)
    assert packet["idea"]["ref"] == "{}#{}".format(REPO, IDEA)
    assert [row["ref"] for row in packet["sibling_plans"]] == [
        REPO + "#10", REPO + "#43"]
    assert "The old premise is false." in packet["issue_thread"]


def _answer_file(tmp_path, **overrides):
    data = {
        "decided_from_precedent": [
            {"claim": "reviews stay human-gated", "source": "plan.md"},
        ],
        "decided_by_agent": [
            {"decision": "validate strictly",
             "alternative": "accept unknown fields",
             "why": "unknown fields signal a confused model"},
        ],
        "needs_nate": {"exposure": None, "gates": None, "scope": None,
                       "preference": None},
        "proposed_class": "Improve",
        "plan_markdown": "# Plan\n\nDo the thing.\n",
        "escalated_risk": [],
        "depends_on": [],
        "premises": [
            {"claim": "reviews stay human-gated",
             "evidence": "plan.md:42", "label": "documented"},
        ],
    }
    data.update(overrides)
    path = tmp_path / "answer.json"
    path.write_text(json.dumps(data))
    return str(path)


def _apply(capsys, argv):
    capsys.readouterr()
    code = shape.apply_main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("needs_nate", [
    None,
    {"exposure": ["May this ship in September?"], "gates": None,
     "scope": None, "preference": None},
])
def test_shape_apply_validate_only_is_identical_without_history(
        monkeypatch, capsys, tmp_path, needs_nate):
    extra = {} if needs_nate is None else {"needs_nate": needs_nate}
    answer = _answer_file(tmp_path, **extra)
    argv = [str(IDEA), "--repo", REPO, "--answer", answer, "--validate-only"]
    old, old_out, new, new_out = _run_both(
        monkeypatch, lambda: _apply(capsys, argv))
    assert new_out == old_out
    assert new_out[0] == 0
    assert json.loads(new_out[1])["status"] == (
        "Ready" if needs_nate is None else "Shaped")
    assert old.writes == new.writes == []
    assert old.argv == new.argv == []
    assert new.ref_queries == [[REPO + "#{}".format(IDEA)]]


@pytest.mark.parametrize("needs_nate", [
    None,
    {"exposure": ["May this ship in September?"], "gates": None,
     "scope": None, "preference": None},
])
def test_shape_apply_live_writes_are_identical_without_history(
        monkeypatch, capsys, tmp_path, needs_nate):
    extra = {} if needs_nate is None else {"needs_nate": needs_nate}
    answer = _answer_file(tmp_path, **extra)
    argv = [str(IDEA), "--repo", REPO, "--answer", answer,
            "--run", "shape-run", "--agent", "muse"]
    old, old_out, new, new_out = _run_both(
        monkeypatch, lambda: _apply(capsys, argv))
    assert new_out == old_out
    assert new_out[0] == 0
    assert new.argv == old.argv
    assert new.writes == old.writes
    expected = "Ready" if needs_nate is None else "Shaped"
    assert ("SET_FIELD", sorted({
        "project": funnel.PROJECT_ID, "item": "PVTI_{}".format(IDEA),
        "field": funnel.STATUS_FIELD_ID,
        "option": "opt-{}".format(expected),
    }.items())) in new.writes
    assert any(args[:4] == ("gh", "issue", "edit", str(IDEA))
               for args in new.argv)
    assert new.ref_queries == [[REPO + "#{}".format(IDEA)]]


def test_shape_apply_reads_its_parent_by_ref(monkeypatch, capsys, tmp_path):
    answer = _answer_file(tmp_path)
    argv = [str(IDEA), "--repo", REPO, "--answer", answer, "--validate-only"]
    results = []
    for full in (True, False):
        with monkeypatch.context() as patch:
            board = _serve(patch, full=full)
            # Effective Class comes from the parent when the idea is a child.
            board.pages[0][0]["class"] = {"name": "New"}
            board.pages[1][0]["content"]["parent"] = {
                "number": 10, "repository": {"nameWithOwner": REPO},
            }
            results.append((board, _apply(capsys, argv)))
    (old, old_out), (new, new_out) = results
    _assert_parity_shape(old, new)
    assert new_out == old_out
    assert new_out[0] == 0
    assert json.loads(new_out[1])["status"] == "Shaped"
    assert new.ref_queries == [[REPO + "#{}".format(IDEA)], [REPO + "#10"]]


def test_shape_apply_falls_back_when_parent_by_ref_misses(
        monkeypatch, capsys, tmp_path):
    answer = _answer_file(tmp_path)
    argv = [str(IDEA), "--repo", REPO, "--answer", answer, "--validate-only"]
    with monkeypatch.context() as patch:
        board = _serve(patch, full=False, missing_refs=[REPO + "#10"])
        board.pages[1][0]["content"]["parent"] = {
            "number": 10, "repository": {"nameWithOwner": REPO},
        }
        code, out, _err = _apply(capsys, argv)
    assert code == 0
    assert json.loads(out)["status"] == "Ready"
    assert board.ref_queries == [[REPO + "#{}".format(IDEA)], [REPO + "#10"]]
    assert len(board.loaded) == 1
    empty = (None, [], None, None, None, None)
    assert all(row == empty for row in board.loaded[0])
    assert board.detail_requests == []
    assert funnel.ITEM_QUERY in board.queries


def test_release_claim_releases_the_same_item_without_history(monkeypatch):
    ref = "{}#{}".format(REPO, TICKET)
    bound = {
        "run": "run-42", "phase": "bind",
        "ts": int(datetime(2026, 9, 26, 11, 1,
                            tzinfo=timezone.utc).timestamp()),
        "do": "ticket", "work": ref,
    }
    monkeypatch.setattr(
        heartbeat, "read_github_strict",
        lambda agent: [bound] if agent == "codex" else [],
    )
    old, _, new, _ = _run_both(
        monkeypatch, lambda: implement.release_claim(ref, run="run-42"))
    assert new.writes == old.writes
    assert new.writes == [("SET_LOCK", sorted({
        "project": funnel.PROJECT_ID, "item": "PVTI_{}".format(TICKET),
        "field": funnel.LOCK_FIELD_ID, "value": "",
    }.items()))]
    assert new.ref_queries == [[ref]]


def test_release_claim_falls_back_to_compact_board_on_by_ref_miss(
        monkeypatch):
    ref = "{}#{}".format(REPO, TICKET)
    bound = {
        "run": "run-42", "phase": "bind",
        "ts": int(datetime(2026, 9, 26, 11, 1,
                            tzinfo=timezone.utc).timestamp()),
        "do": "ticket", "work": ref,
    }
    monkeypatch.setattr(
        heartbeat, "read_github_strict",
        lambda agent: [bound] if agent == "codex" else [],
    )
    with monkeypatch.context() as patch:
        board = _serve(patch, full=False, missing_refs=[ref])
        implement.release_claim(ref, run="run-42")
    assert board.ref_queries == [[ref]]
    assert len(board.loaded) == 1
    empty = (None, [], None, None, None, None)
    assert all(row == empty for row in board.loaded[0])
    assert board.detail_requests == []
    assert funnel.ITEM_QUERY in board.queries
    assert board.writes == [("SET_LOCK", sorted({
        "project": funnel.PROJECT_ID, "item": "PVTI_{}".format(TICKET),
        "field": funnel.LOCK_FIELD_ID, "value": "",
    }.items()))]
