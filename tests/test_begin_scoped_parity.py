"""The filtered begin view decides exactly what the full board decides (#1591).

``load_items(scope="begin")`` reads every open item, the closed items one of
``BEGIN_ITEM_CONNECTIONS`` stands for, and the anchors ``_begin_anchor_refs``
names. This file runs ``cmd_begin`` twice over one fixture board: once with
every Project item, as the full load returns them, and once with the subset
the filtered loader would return, built from the loader's own predicates and
anchor function. Everything ``begin`` prints and every write it makes must be
identical.

The fixture board is written to reach each closed-item consumer the design
lists: closed tickets of open projects, a closed Parked item whose wake is
due, a closed ticket holding a claim, done and park drift, closed
needs-shaping items, a blocked ticket whose block names a long-closed issue,
#794 closed (``FREEZE_OWNER_REF``), a heartbeat-bound closed ticket, an open
ticket under a closed project, and the regression issues the merge gate
counts. ``test_each_closed_consumer_is_load_bearing`` removes each of those
from the filtered view in turn and requires the result to change, so a
parity pass cannot come from a board on which the closed items never
mattered.

Every GitHub read and write goes through fakes; nothing here reaches GitHub.
"""

from __future__ import annotations

import copy
import io
import json
import pathlib
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
import usage  # noqa: E402


CC = funnel.REPO
EX = "nateprich-projects/example"
MEMBERS = {CC, EX}

# `_heartbeat_bound_refs` measures its seven-day window against the wall
# clock, so the board is dated from it too.
NOW = datetime.fromtimestamp(int(time.time()), timezone.utc)


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(repo, number, *, state="OPEN", reason=None, status="Building",
          klass="Improve", parent=None, labels=(), title=None, body=None,
          risk="standard", origin="agent", needs="none", **kw):
    parent_ref = None
    if parent is not None:
        parent_ref = parent if "#" in str(parent) else "{}#{}".format(
            repo, parent)
    kw.setdefault("status_since", NOW - timedelta(days=2))
    kw.setdefault(
        "closed_at", NOW - timedelta(days=1) if state == "CLOSED" else None)
    return funnel.Item(
        repo=repo,
        number=number,
        title=title or "{} {}".format("ticket" if parent else "item", number),
        url="https://github.com/{}/issues/{}".format(repo, number),
        state=state,
        state_reason=reason,
        status=status,
        klass=klass,
        origin=origin,
        risk=risk,
        needs=needs,
        labels=list(labels),
        parent=parent_ref,
        body=body if body is not None else "What: work.\n\nRisk: {}".format(
            risk),
        item_id="item-{}-{}".format(repo, number),
        **kw,
    )


def _verdict_comment(verdict, head):
    payload = {"verdict": verdict, "ci": "green", "head_sha": head,
               "blocking": []}
    return {
        "body": funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(payload)
        + "\n```",
        "createdAt": _stamp(NOW - timedelta(hours=1)),
    }


def _pr(repo, number, ticket, *, state="OPEN", verdict=None, head=None,
        created=None):
    head = head or "sha-{}".format(number)
    created = created or NOW - timedelta(hours=3)
    row = {
        "number": number,
        "state": state,
        "headRefName": "ticket/{}".format(ticket),
        "headRefOid": head,
        "mergeable": "MERGEABLE",
        "title": "PR for ticket {}".format(ticket),
        "url": "https://github.com/{}/pull/{}".format(repo, number),
        "createdAt": _stamp(created),
        "statusCheckRollup": [
            {"name": "tests", "conclusion": "SUCCESS", "status": "COMPLETED"}
        ],
        "comments": [_verdict_comment(verdict, head)] if verdict else [],
    }
    if state == "MERGED":
        row["mergedAt"] = _stamp(NOW - timedelta(hours=20))
    return row


def _board(*, regressions=3, back_off_every_ticket=False, start_age_days=1):
    """One Project board: items, comments, PR rows, heartbeat, sub-issues."""
    frozen_body = (
        "Parent: #100.\n\nWhat: rewrite routines/muse.md.\n\n"
        "Accept: routines/muse.md reads clean.\n\nRisk: standard"
    )
    items = [
        # #794 closed: the freeze owner. Nothing else would load it.
        _item(CC, 794, state="CLOSED", reason="COMPLETED", status="Done",
              title="Freeze owner"),
        # An open project with two closed tickets and an open one that names
        # frozen ground, startable only while #794 reads as closed.
        _item(CC, 100, children_total=3, children_done=2),
        _item(CC, 101, state="CLOSED", reason="COMPLETED", status="Done",
              parent=100),
        _item(CC, 102, state="CLOSED", reason="COMPLETED", status="Done",
              parent=100),
        _item(CC, 103, parent=100, body=frozen_body),
        # A project whose ticket's block names a long-closed issue, plus a
        # closed ticket still holding a claim and a closed ticket that kept
        # its needs-shaping label.
        _item(EX, 10, children_total=3, children_done=2),
        _item(EX, 11, parent=10, labels=["blocked"],
              block_reason="waiting on #5", block_references=["#5"],
              blocked_since=NOW - timedelta(days=30)),
        _item(EX, 12, state="CLOSED", reason="COMPLETED", status="Done",
              parent=10, in_motion_since=NOW - timedelta(days=3)),
        _item(EX, 13, state="CLOSED", reason="COMPLETED", status="Done",
              parent=10, labels=["needs-shaping"]),
        _item(EX, 5, state="CLOSED", reason="COMPLETED", status="Done",
              title="The long-closed prerequisite",
              closed_at=NOW - timedelta(days=200)),
        # An open ticket claimed ten minutes ago, with its branch pushed: a
        # startable claim the implementing lane passes and reports as held.
        _item(EX, 20, klass="Broken"),
        _item(EX, 21, parent=20, in_motion_since=NOW - timedelta(minutes=10)),
        # A closed Parked item whose dated wake is due.
        _item(CC, 200, state="CLOSED", reason="NOT_PLANNED", status="Parked",
              title="Parked with a wake date"),
        # Done drift, park drift, and a closed idea that kept needs-shaping.
        _item(CC, 300, state="CLOSED", reason="COMPLETED", status="Building"),
        _item(CC, 301, state="CLOSED", reason="NOT_PLANNED", status="Ready"),
        _item(CC, 302, state="CLOSED", reason="COMPLETED", status="Ideas",
              labels=["needs-shaping"]),
        # A project whose last open ticket carries an approved PR, and whose
        # other ticket closed under a run the heartbeat never finished.
        _item(CC, 110, klass="Broken", children_total=2, children_done=1),
        _item(CC, 111, state="CLOSED", reason="COMPLETED", status="Done",
              parent=110),
        _item(CC, 113, parent=110, risk="escalated",
              body="What: fix.\n\nRisk: escalated"),
        # A project with an open PR waiting for review.
        _item(CC, 120),
        _item(CC, 112, parent=120, risk="escalated",
              body="What: review me.\n\nRisk: escalated"),
        # An open ticket left under a project that has closed. Its class
        # comes from that project, and Broken puts its PR first in review.
        _item(CC, 151, state="CLOSED", reason="COMPLETED", status="Done",
              klass="Broken", children_total=1, children_done=0),
        _item(CC, 152, parent=151, klass=None, risk="escalated",
              body="What: late fix.\n\nRisk: escalated"),
        # A finished upkeep project the reconcile closes by itself.
        _item(CC, 130, klass="Maintenance", children_total=1,
              children_done=1),
        _item(CC, 131, state="CLOSED", reason="COMPLETED", status="Done",
              parent=130),
        # A Ready project waiting for breakdown and an idea waiting to shape.
        _item(CC, 140, status="Ready", klass="New", risk="escalated"),
        _item(CC, 500, status="Ideas", labels=["needs-shaping"],
              risk="escalated", klass="New"),
        # A closed, long-accepted project nothing in begin reads.
        _item(CC, 600, state="CLOSED", reason="COMPLETED", status="Done",
              closed_at=NOW - timedelta(days=90)),
    ]
    for index in range(regressions):
        items.append(_item(
            CC, 400 + index, state="CLOSED", reason="COMPLETED",
            status="Done", status_since=NOW - timedelta(days=1 + index),
            title="{}{}: broke".format(funnel.REGRESSION_PREFIX, 900 + index),
        ))

    wake = (NOW - timedelta(days=1)).date().isoformat()
    comments = {
        "{}#200".format(CC): [{
            "body": "{}date={} status=Ready\n{}back later".format(
                funnel.PARK_WAKE_PREFIX, wake, funnel.PARK_COMMENT_PREFIX),
        }],
    }

    prs = {
        CC: [
            _pr(CC, 511, 111, state="MERGED"),
            _pr(CC, 512, 112),
            _pr(CC, 552, 152, created=NOW - timedelta(hours=1)),
            _pr(CC, 513, 113, verdict="approved"),
            _pr(CC, 501, 101, state="MERGED"),
        ],
        EX: [],
    }
    branches = {
        CC: {"{}#{}".format(CC, n) for n in (112, 113, 152)},
        EX: {"{}#21".format(EX)},
    }

    day = int((NOW - timedelta(days=start_age_days)).timestamp())
    records = {
        "codex": [
            {"run": "r-codex-111", "agent": "codex", "phase": "start",
             "ts": day},
            {"run": "r-codex-111", "agent": "codex", "phase": "bind",
             "ts": day, "do": "ticket", "work": "{}#111".format(CC)},
        ],
        "muse": [
            {"run": "r-muse-merge", "agent": "muse", "phase": "start",
             "ts": day + 60},
            {"run": "r-muse-merge", "agent": "muse", "phase": "finish",
             "ts": day + 120, "outcome": "done", "merged": 511},
        ],
    }
    if back_off_every_ticket:
        for ref in ("{}#103".format(CC), "{}#11".format(EX)):
            for attempt in range(funnel.BACKOFF_FAILURES):
                run = "r-fail-{}-{}".format(ref, attempt)
                stamp = int((NOW - timedelta(minutes=30 - attempt)).timestamp())
                records["codex"] += [
                    {"run": run, "agent": "codex", "phase": "start",
                     "ts": stamp},
                    {"run": run, "agent": "codex", "phase": "bind",
                     "ts": stamp, "do": "ticket", "work": ref},
                    {"run": run, "agent": "codex", "phase": "finish",
                     "ts": stamp + 1, "outcome": "errored"},
                ]

    return SimpleNamespace(
        items=items, comments=comments, prs=prs, branches=branches,
        records=records,
    )


def scoped_view(full, members=MEMBERS):
    """The subset ``load_items(scope="begin")`` returns for ``full``.

    Each connection's server filter is stood in for by the predicate the
    loader re-checks it against, so the rule is the loader's own: an open row
    is kept from any connection, a closed row only when some closed-set
    predicate holds. Rows keep the loader's order (connection by connection,
    Project order within each), then the anchors ``_begin_anchor_refs``
    names, fetched in its order.
    """
    kept = {}
    for alias, _filter in funnel.BEGIN_ITEM_CONNECTIONS:
        predicate = funnel.BEGIN_ITEM_PREDICATES[alias]
        for item in full:
            if not predicate(item):
                continue
            if item.state != "OPEN" and not any(
                check(item) for check in funnel.BEGIN_ITEM_PREDICATES.values()
            ):
                continue
            kept.setdefault(item.item_id, item)
    rows = [item for item in kept.values() if item.repo in members]
    by_ref = {item.ref: item for item in full}
    for ref in funnel._begin_anchor_refs(rows, members):
        if ref in by_ref:
            rows.append(by_ref[ref])
    return rows


class _Fakes:
    """Every GitHub read and write ``begin`` makes, answered from a board."""

    def __init__(self, board):
        self.board = board
        self.writes = []
        self.heartbeat_writes = []

    def install(self, monkeypatch):
        board = self.board
        funnel.reset_api_usage()
        funnel.reset_route_state()

        def gh_graphql(query, **variables):
            if query in (funnel.SET_FIELD, funnel.SET_LOCK):
                self.writes.append(("graphql", query.split("(")[0].strip(),
                                    sorted(variables.items())))
                return {"updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": variables.get("item")}}}
            if query == funnel.CLOSED_ITSELF_TICKETS:
                project = "{}/{}#{}".format(
                    variables["owner"], variables["name"],
                    variables["number"])
                children = [
                    item for item in board.items if item.parent == project
                ]
                return {"repository": {"issue": {"subIssues": {
                    "totalCount": len(children),
                    "nodes": [
                        {"number": child.number, "title": child.title,
                         "state": child.state, "url": child.url,
                         "repository": {"nameWithOwner": child.repo}}
                        for child in children
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }}}}
            raise AssertionError(
                "unexpected GraphQL in begin parity: {}".format(query[:80]))

        def run_gh(args, **kwargs):
            argv = [str(part) for part in args]
            if "--json" in argv:
                field = argv[argv.index("--json") + 1]
                number = argv[3]
                repo = argv[argv.index("--repo") + 1]
                ref = "{}#{}".format(repo, number)
                if field == "comments":
                    payload = {"comments": board.comments.get(ref, [])}
                elif field == "state":
                    payload = {"state": "OPEN"}
                else:
                    raise AssertionError("unexpected gh read {}".format(argv))
                self.writes.append(("read", tuple(argv)))
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload), stderr="")
            self.writes.append(("gh", tuple(argv)))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def read_batched(repos, **kwargs):
            wanted = sorted(set(repos))
            return funnel.BatchedPRRead(
                {repo: tuple(copy.deepcopy(board.prs.get(repo, [])))
                 for repo in wanted},
                {repo: set(board.branches.get(repo, set()))
                 for repo in wanted},
                {repo: False for repo in wanted},
                {repo: False for repo in wanted},
            )

        def heartbeat_append(agent, record):
            self.heartbeat_writes.append((agent, dict(record)))
            return "pushed"

        monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
        monkeypatch.setattr(funnel, "_run_gh", run_gh)
        monkeypatch.setattr(funnel, "_read_batched_pr_snapshots", read_batched)
        monkeypatch.setattr(funnel, "_option_id",
                            lambda field, name: "option-" + name)
        monkeypatch.setattr(
            funnel, "project_single_select",
            lambda name: {"id": "field-" + name,
                          "options": {value: "option-" + value
                                      for value in funnel.NEEDS_OPTIONS}})
        monkeypatch.setattr(funnel, "fetch_drift_facts",
                            lambda item: funnel.DriftFacts())
        monkeypatch.setattr(funnel, "read_lock",
                            lambda item: item.in_motion_since)
        monkeypatch.setattr(
            funnel, "implementation_packet",
            lambda repo, number, agent: {"repo": repo,
                                         "ticket": {"number": number}})
        bodies = {(item.repo, item.number): item.body for item in board.items}
        monkeypatch.setattr(
            funnel, "_ticket_body",
            lambda repo, number: bodies.get((repo, number)) or "")
        monkeypatch.setattr(
            heartbeat, "read",
            lambda agent, timeout=None: copy.deepcopy(
                board.records.get(agent, [])))
        monkeypatch.setattr(
            heartbeat, "read_github",
            lambda agent, timeout=None: copy.deepcopy(
                board.records.get(agent, [])))
        monkeypatch.setattr(heartbeat, "append", heartbeat_append)
        monkeypatch.setattr(
            heartbeat, "record_binding",
            lambda agent, run, do, work, repo=None:
            self.heartbeat_writes.append(
                (agent, {"bind": (run, do, work, repo)})) or "pushed")
        monkeypatch.setattr(
            heartbeat, "record_event",
            lambda agent, run, outcome, **fields:
            self.heartbeat_writes.append(
                (agent, {"event": (run, outcome, sorted(fields.items()))}))
            or "pushed")
        monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)


def _run_begin(monkeypatch, board, items, *, agent, tier, caller_role=None,
               breakdown=False):
    fakes = _Fakes(board)
    fakes.install(monkeypatch)
    out = io.StringIO()
    err = io.StringIO()
    preflight = ({"agent": agent, "run": "run-under-test", "gate": "ok"}, {})
    with redirect_stdout(out), redirect_stderr(err):
        code = funnel.cmd_begin(
            items, NOW, agent, tier, False, breakdown,
            caller_role=caller_role, _preflight=preflight,
        )
    assert code == 0, err.getvalue()
    return {
        "json": json.loads(out.getvalue()),
        "writes": fakes.writes,
        "heartbeat": fakes.heartbeat_writes,
    }


def _both(monkeypatch, *, board_kwargs=None, drop=(), **begin_kwargs):
    """Run begin over the full board and over the filtered view of a copy."""
    board_kwargs = board_kwargs or {}
    full_board = _board(**board_kwargs)
    full = _run_begin(monkeypatch, full_board, full_board.items,
                      **begin_kwargs)

    scoped_board = _board(**board_kwargs)
    view = [
        item for item in scoped_view(scoped_board.items)
        if item.ref not in drop
    ]
    scoped = _run_begin(monkeypatch, scoped_board, view, **begin_kwargs)
    return full, scoped, full_board, view


IMPLEMENT = {"agent": "codex", "tier": "standard"}
REVIEW = {"agent": "muse", "tier": "escalated", "caller_role": "review",
          "breakdown": True}

SCENARIOS = {
    "implement": ({}, IMPLEMENT),
    "implement-all-backed-off": ({"back_off_every_ticket": True}, IMPLEMENT),
    "review": ({}, REVIEW),
    "review-merge-closes-project": ({"regressions": 2}, REVIEW),
}


def test_the_filtered_view_leaves_out_what_begin_never_reads(monkeypatch):
    board = _board()
    _Fakes(board).install(monkeypatch)
    view = {item.ref for item in scoped_view(board.items)}
    full = {item.ref for item in board.items}
    assert full - view == {
        "{}#{}".format(CC, n) for n in (101, 102, 131, 600)
    }
    # Each closed consumer arrives by its own connection or anchor.
    for ref in ("#794", "#200", "#300", "#301", "#302", "#111", "#151",
                "#400"):
        assert CC + ref in view
    for ref in ("#5", "#12", "#13"):
        assert EX + ref in view


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_begin_over_the_filtered_view_matches_the_full_board(
    monkeypatch, scenario
):
    board_kwargs, begin_kwargs = SCENARIOS[scenario]
    full, scoped, _board_used, view = _both(
        monkeypatch, board_kwargs=board_kwargs, **begin_kwargs)
    assert len(view) < len(_board_used.items)
    assert scoped["json"] == full["json"]
    assert scoped["writes"] == full["writes"]
    assert scoped["heartbeat"] == full["heartbeat"]


#: Each closed item a begin consumer reads, and a lane whose result it
#: decides. Dropping it from the filtered view must change what begin does.
LOAD_BEARING = {
    CC + "#794": IMPLEMENT,    # the freeze would withhold #103
    EX + "#5": IMPLEMENT,      # #11's block would stay
    EX + "#12": IMPLEMENT,     # its claim would stay
    CC + "#200": IMPLEMENT,    # it would not wake
    CC + "#300": IMPLEMENT,    # done drift
    CC + "#301": IMPLEMENT,    # park drift
    CC + "#302": IMPLEMENT,    # closed idea keeping needs-shaping
    EX + "#13": IMPLEMENT,     # closed ticket keeping needs-shaping
    CC + "#111": IMPLEMENT,    # the orphaned start would stay open
    CC + "#400": REVIEW,       # two regressions would let #513 merge
    CC + "#151": REVIEW,       # #552 would lose its Broken class
}


@pytest.mark.parametrize("ref", sorted(LOAD_BEARING))
def test_each_closed_consumer_is_load_bearing(monkeypatch, ref):
    full, scoped, _board_used, view = _both(
        monkeypatch, drop={ref}, **LOAD_BEARING[ref])
    assert ref in {item.ref for item in scoped_view(_board().items)}
    assert (scoped["json"], scoped["writes"], scoped["heartbeat"]) != (
        full["json"], full["writes"], full["heartbeat"])


def test_an_orphaned_start_older_than_the_anchor_window_is_left_alone(
    monkeypatch
):
    """The one designed difference (#1608): the filtered view fetches only
    tickets bound to starts from the last ``HEARTBEAT_ANCHOR_WINDOW``, so an
    older orphaned start is left for the watchdog instead of being closed.
    Everything else begin prints and writes is unchanged."""
    age = funnel.HEARTBEAT_ANCHOR_WINDOW.days + 3
    full, scoped, _board_used, view = _both(
        monkeypatch, board_kwargs={"start_age_days": age}, **IMPLEMENT)
    assert CC + "#111" not in {item.ref for item in view}
    assert [row["ref"] for row in full["json"]["reconciled_starts"]] == [
        CC + "#111"]
    assert "reconciled_starts" not in scoped["json"]
    rest = dict(full["json"])
    rest.pop("reconciled_starts")
    assert scoped["json"] == rest
    assert scoped["writes"] == full["writes"]
    assert scoped["heartbeat"] == [
        entry for entry in full["heartbeat"]
        if entry[1].get("phase") != "finish"
    ]
