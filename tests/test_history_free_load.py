"""Commands that read no item history load the Project without it (#1622).

``load_items(include_details=True)`` adds a batched timeline and child read on
top of the paged Project list -- about fourteen pages and most of a minute on
the 2026-09 board. ``PROJECT_LOAD_READS_HISTORY`` names the commands that can
skip it. These tests pin three things: the loader argument each command gets,
that an unlisted command still gets the full load, and that every command
switched to the compact load prints and writes exactly what it did on the
full load, on a fixture board whose history is real and non-empty.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"

#: The decision table as verified on 2026-09-26. A change to the table must
#: change this too, with the verification that justifies it.
EXPECTED = {
    "claim": False,
    "release": False,
    "pin": False,
    "unpin": False,
    "park": False,
    "answer-gates": False,
    "comment": False,
    "capture": False,
    "next-review": False,
    "review": False,
    "doctor": False,
    "reject": True,
    "approve": True,
    "accept": True,
    "show": True,
    "ideas": True,
    "merge": True,
    "next": True,
    "queue": True,
    "brief": True,
}

#: One parseable argv per command that goes through ``main``'s shared load.
ARGV = {
    "claim": ["claim", "2"],
    "release": ["release", "2"],
    "pin": ["pin", "4"],
    "unpin": ["unpin", "4"],
    "park": ["park", "4", "--reason", "no longer earns its place"],
    "answer-gates": ["answer-gates", "4", "--answer", "No."],
    "comment": ["comment", "2", "--voice", "agent", "--body", "note"],
    "capture": ["capture", "an idea", "--origin", "nate-relayed"],
    "next-review": ["next-review"],
    "review": ["review", "9", "--verdict", "approved", "--ci", "green"],
    "reject": ["reject", "9"],
    "approve": ["approve", "4"],
    "accept": ["accept", "1"],
    "show": ["show", "1"],
    "ideas": ["ideas"],
    "merge": ["merge", "9"],
    "next": ["next"],
    "queue": ["queue"],
    "brief": ["brief"],
}


def test_the_table_is_the_verified_one():
    assert funnel.PROJECT_LOAD_READS_HISTORY == EXPECTED
    assert set(ARGV) | {"doctor"} == set(EXPECTED)


@pytest.mark.parametrize("command", sorted(ARGV))
def test_main_passes_each_command_its_loader_argument(monkeypatch, command):
    received = []

    def load_items(include_details=True):
        received.append(include_details)
        raise funnel.GitHubError("stop after the load")

    monkeypatch.setattr(funnel, "load_items", load_items)

    funnel.main(ARGV[command])

    assert received == [EXPECTED[command]]


def test_doctor_passes_its_loader_argument(monkeypatch):
    received = []

    def load_items(include_details=True):
        received.append(include_details)
        return []

    monkeypatch.setattr(funnel, "load_items", load_items)
    monkeypatch.setattr(funnel, "merged_pr_facts", lambda items: None)
    monkeypatch.setattr(
        funnel, "doctor_checks",
        lambda items=None, merged_pr_facts=None: [
            funnel.Check("local", True, "ok", "")
        ],
    )

    funnel.main(["doctor"])

    assert received == [False]


@pytest.mark.parametrize("command", [None, "", "a-command-added-later"])
def test_an_unknown_command_gets_the_full_load(command):
    assert funnel.project_load_reads_history(command) is True


def test_a_command_missing_from_the_table_gets_the_full_load(monkeypatch):
    """The fail-safe path through ``main`` itself, not only the helper."""
    received = []
    monkeypatch.delitem(funnel.PROJECT_LOAD_READS_HISTORY, "claim")

    def load_items(include_details=True):
        received.append(include_details)
        raise funnel.GitHubError("stop after the load")

    monkeypatch.setattr(funnel, "load_items", load_items)

    funnel.main(ARGV["claim"])

    assert received == [True]


@pytest.mark.parametrize("command", ["claim", "show"])
def test_a_session_loader_gets_the_same_argument(monkeypatch, command):
    received = []

    def loader(include_details=True):
        received.append(include_details)
        raise funnel.GitHubError("stop after the load")

    funnel.main(ARGV[command], _items_loader=loader)

    assert received == [EXPECTED[command]]


# --------------------------------------------------------------------------
# A fixture board served by a fake gh_graphql
# --------------------------------------------------------------------------

#: Old enough that a timestamp from this history can never be mistaken for
#: one the command stamped with its own clock.
HISTORY_AT = "2020-01-0{}T00:00:00Z"

PLAN = """# A plan

## What it is

Something worth doing.

## Needs Nate

- Exposure: nothing outstanding. No new surface.
- Gates: Should the connector answer gates on Nate's behalf?
- Scope and priority: nothing outstanding. Scoped.
- Preference: nothing outstanding. No choice remains.
"""


def _node(number, status, *, parent=None, labels=(), lock=None,
          children=(0, 0), body="", klass="New", pinned=False):
    return {
        "id": "pi-{}".format(number),
        "lock": {"text": lock} if lock else None,
        "status": {"name": status},
        "class": {"name": klass},
        "origin": {"name": "Nate"},
        "risk": {"name": "standard"},
        "pinned": {"name": "Pinned"} if pinned else None,
        "needs": {"name": "none"},
        "content": {
            "number": number,
            "title": "issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(REPO, number),
            "body": body,
            "state": "OPEN",
            "stateReason": None,
            "createdAt": HISTORY_AT.format(1),
            "closedAt": None,
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


def _board():
    return [
        # A Ready project with two tickets: claiming one writes its parent.
        _node(1, "Ready", children=(2, 1), body=PLAN),
        _node(2, "Ready", parent=1),
        _node(3, "Building", parent=1, labels=("blocked",)),
        # A Shaped plan carrying an open Gates question.
        _node(4, "Shaped", body=PLAN, pinned=True),
        _node(5, "Ideas", klass="Improve"),
    ]


def _history(item_id):
    number = int(item_id.split("-")[1])
    status = next(
        node["status"]["name"] for node in _board()
        if node["id"] == item_id
    )
    events = [{
        "__typename": "ProjectV2ItemStatusChangedEvent",
        "createdAt": HISTORY_AT.format(2 + number % 5),
        "previousStatus": "Ideas",
        "status": status,
        "project": {"number": funnel.PROJECT_NUMBER},
    }, {
        "__typename": "LabeledEvent",
        "createdAt": HISTORY_AT.format(3),
        "label": {"name": "blocked"},
    }]
    return {"id": item_id, "content": {"timelineItems": {"nodes": events}}}


def _children(item_id):
    return {"id": item_id, "content": {"subIssues": {"nodes": [
        {"createdAt": HISTORY_AT.format(4), "closedAt": HISTORY_AT.format(5)},
        {"createdAt": HISTORY_AT.format(4), "closedAt": None},
    ]}}}


OPTIONS = [
    {"id": "opt-{}".format(name), "name": name}
    for name in (
        "Ideas", "Shaped", "Ready", "Building", "Done", "Parked",
        "Pinned", "human", "none", "Nate", "agent", "standard", "escalated",
        "Broken", "Improve", "New", "Replace",
    )
]


class World:
    """Fake GitHub. Reads answer from the fixture; writes are recorded."""

    def __init__(self):
        self.reads = []
        self.writes = []

    def gh_graphql(self, query, **variables):
        if query.lstrip().startswith("mutation"):
            self.writes.append(("graphql", " ".join(query.split()), variables))
            item = variables.get("item") or "pi-new"
            return {
                "updateProjectV2ItemFieldValue": {"projectV2Item": {"id": item}},
                "clearProjectV2ItemFieldValue": {"projectV2Item": {"id": item}},
            }
        if "history: nodes(ids:" in query:
            self.reads.append("details")
            return {
                "history": [_history(i) for i in variables["ids"]],
                "children": [_children(i) for i in variables["childIds"]],
            }
        if "nodes(ids: $ids)" in query:
            self.reads.append("details")
            return {"nodes": [_history(i) for i in variables["ids"]]}
        if "items(first:" in query:
            self.reads.append("project")
            return {"user": {"projectV2": {"items": {
                "nodes": _board(),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        if "ProjectV2SingleSelectField{options" in query:
            return {"node": {"options": OPTIONS}}
        if "fields(" in query:
            return {"user": {"projectV2": {"fields": {"nodes": [
                {"id": "field-{}".format(name), "name": name,
                 "options": OPTIONS}
                for name in ("Status", "Class", "Origin", "Risk", "Pinned",
                             "Needs")
            ]}}}}
        if "pullRequests(first:" in query:
            return {"repo0": {
                "pullRequests": {"nodes": [], "pageInfo": {
                    "hasNextPage": False, "endCursor": None}},
                "refs": {"nodes": [], "pageInfo": {"hasNextPage": False}},
            }}
        self.reads.append("other")
        return {}

    def run_gh(self, args, **kwargs):
        self.writes.append(("gh", list(args)))
        stdout = ""
        if args[:3] == ["gh", "issue", "create"]:
            stdout = "https://github.com/{}/issues/99\n".format(REPO)
        if args[:3] == ["gh", "project", "item-add"]:
            stdout = json.dumps({"id": "pi-99"})
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def gh_json(self, *args):
        self.writes.append(("gh-json", list(args)))
        if args[:3] == ("gh", "issue", "view") and "state" in args:
            return {"state": "OPEN"}
        if args[:3] == ("gh", "pr", "view"):
            return {"headRefOid": "abc123", "state": "OPEN"}
        if args[:3] == ("gh", "issue", "view"):
            return {"comments": []}
        return []


def _normalise(text, started):
    """Blank timestamps the command stamped from its own clock.

    History timestamps in the fixture are from 2020, so a difference between
    the full and compact runs that came from history would survive this.
    """
    def replace(match):
        try:
            at = datetime.fromisoformat(match.group(0).replace("Z", "+00:00"))
        except ValueError:
            return match.group(0)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if abs(at - started) < timedelta(hours=1):
            return "<now>"
        return match.group(0)

    return re.sub(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?",
        replace, text,
    )


def _run(monkeypatch, capsys, argv, *, full):
    world = World()
    with monkeypatch.context() as patch:
        patch.setattr(funnel, "member_repos", lambda *a, **k: [REPO])
        patch.setattr(funnel, "gh_graphql", world.gh_graphql)
        patch.setattr(funnel, "_run_gh", world.run_gh)
        patch.setattr(funnel, "_gh_json", world.gh_json)
        patch.setattr(funnel, "_PROJECT_SELECT_CACHE", None)
        if full:
            patch.setattr(
                funnel, "project_load_reads_history", lambda command: True
            )
        started = datetime.now(timezone.utc)
        code = funnel.main(list(argv))
    out, err = capsys.readouterr()
    return {
        "code": code,
        "out": _normalise(out, started),
        "err": _normalise(err, started),
        "writes": _normalise(json.dumps(world.writes, default=str), started),
        "reads": world.reads,
    }


#: Every command switched to the compact load, with the exit code that shows
#: it did its real work rather than failing the same way twice.
PARITY = [
    (["claim", "2"], 0),
    (["release", "3"], 0),
    (["pin", "4"], 1),
    (["pin", "4", "--yes"], 0),
    (["unpin", "4", "--yes"], 0),
    (["park", "4", "--reason", "no longer earns its place"], 0),
    (["park", "4", "--reason", "later", "--wake-date", "2099-01-01"], 0),
    (["answer-gates", "4", "--answer", "No. It relays mine."], 0),
    (["comment", "3", "--voice", "agent", "--body", "a note"], 0),
    (["comment", "2", "--voice", "agent", "--needs-decision",
      "Which one?"], 0),
    (["comment", "2", "--voice", "agent", "--blocked-on", "3",
      "--because", "it needs the other first"], 0),
    (["capture", "an idea", "--origin", "nate-relayed", "--repo", REPO], 0),
    (["next-review"], 1),
    (["review", "9", "--repo", REPO, "--verdict", "approved",
      "--ci", "green"], 0),
]


@pytest.mark.parametrize(
    "argv,expected_code", PARITY, ids=[" ".join(a[:2]) for a, _ in PARITY]
)
def test_compact_load_prints_and_writes_what_the_full_load_did(
    monkeypatch, capsys, argv, expected_code,
):
    assert funnel.project_load_reads_history(argv[0]) is False

    full = _run(monkeypatch, capsys, argv, full=True)
    compact = _run(monkeypatch, capsys, argv, full=False)

    # The two runs really differ in what they read ...
    assert "details" in full["reads"]
    assert "details" not in compact["reads"]
    # ... and in nothing the command does.
    assert full["code"] == expected_code, full
    for key in ("code", "out", "err", "writes"):
        assert compact[key] == full[key], key


def test_doctor_prints_the_same_checks_without_history(monkeypatch, capsys):
    """Doctor loads through ``cmd_doctor``; its item checks see no history."""
    monkeypatch.setattr(
        funnel, "doctor_checks",
        lambda items=None, merged_pr_facts=None: [
            funnel.Check("item consistency", True,
                         json.dumps(funnel.status_state_mismatches(items or [])),
                         ""),
        ],
    )
    monkeypatch.setattr(funnel, "merged_pr_facts", lambda items: None)
    monkeypatch.setattr(funnel, "check_api_usage",
                        lambda: funnel.Check("api", True, "", ""))
    monkeypatch.setattr(funnel, "check_project_pagination",
                        lambda *a: funnel.Check("pages", True, "", ""))

    full = _run(monkeypatch, capsys, ["doctor"], full=True)
    compact = _run(monkeypatch, capsys, ["doctor"], full=False)

    assert "details" in full["reads"]
    assert "details" not in compact["reads"]
    for key in ("code", "out", "err", "writes"):
        assert compact[key] == full[key], key


def test_the_fixture_history_is_visible_to_a_command_that_reads_it(
    monkeypatch, capsys,
):
    """Guard the parity tests: on this board, history changes a command.

    If the fixture's history stopped reaching the items, every parity test
    above would pass vacuously. ``show`` prints the gate age, so its output
    must differ between the two loads.
    """
    monkeypatch.setitem(funnel.PROJECT_LOAD_READS_HISTORY, "show", False)

    full = _run(monkeypatch, capsys, ["show", "4"], full=True)
    compact = _run(monkeypatch, capsys, ["show", "4"], full=False)

    assert full["code"] == 0, full
    assert full["out"] != compact["out"]


# --------------------------------------------------------------------------
# The session view
# --------------------------------------------------------------------------


def _session_loader(record):
    def loader(include_details=True, member_repo_names=None, timings=None):
        record.append(("load", include_details))
        return [
            funnel.Item(repo=REPO, number=1, title="t", url="u",
                        state="OPEN", item_id="pi-1", status="Ready"),
        ]
    return loader


def _dispatch_recording(monkeypatch, record):
    def main(argv, *, _items=None, _items_loader=None, _reset_api_usage=True):
        if _items is None:
            _items = funnel._call_with_optional_keywords(
                _items_loader,
                include_details=funnel.project_load_reads_history(argv[0]),
            )
        record.append(("run", argv[0], _items[0].status_since))
        return 0

    monkeypatch.setattr(funnel, "main", main)
    monkeypatch.setattr(funnel, "report_api_cost", lambda **kw: None)
    monkeypatch.setattr(funnel, "report_graphql_spend", lambda: None)

    def hydrate(items, candidates=None):
        record.append(("hydrate", len(items)))
        for item in items:
            item.status_since = datetime(2020, 1, 2, tzinfo=timezone.utc)

    monkeypatch.setattr(funnel, "hydrate_item_details", hydrate)


def test_a_session_hydrates_a_compact_view_once_when_history_is_needed(
    monkeypatch,
):
    record = []
    _dispatch_recording(monkeypatch, record)
    session = funnel.FunnelSession(loader=_session_loader(record))

    session.dispatch(["claim", "1"])
    session.dispatch(["comment", "1"])
    session.dispatch(["show", "1"])
    session.dispatch(["queue"])

    since = datetime(2020, 1, 2, tzinfo=timezone.utc)
    assert record == [
        ("load", False),
        ("run", "claim", None),
        ("run", "comment", None),
        ("hydrate", 1),
        ("run", "show", since),
        ("run", "queue", since),
    ]


def test_a_session_that_starts_on_history_loads_it_once(monkeypatch):
    record = []
    _dispatch_recording(monkeypatch, record)
    session = funnel.FunnelSession(loader=_session_loader(record))

    session.dispatch(["show", "1"])
    session.dispatch(["claim", "1"])
    session.dispatch(["brief"])

    assert [entry[0] for entry in record] == ["load", "run", "run", "run"]
    assert record[0] == ("load", True)


def test_a_begin_view_keeps_its_candidate_only_contract(monkeypatch):
    """Begin's compact view is not re-hydrated by later session commands.

    That is the contract before #1622; changing it would add a full history
    read to every merge a Muse run makes after begin.
    """
    record = []
    session = funnel.FunnelSession(loader=_session_loader(record))
    monkeypatch.setattr(funnel, "hydrate_item_details",
                        lambda *a, **k: record.append("hydrate"))

    session._command = "begin"
    session._load_items(include_details=False)
    session._command = "show"
    session._load_items(include_details=True)

    assert record == [("load", False)]
