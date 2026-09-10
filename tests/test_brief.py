"""The brief's parked section and its queue exclusions."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "project_items.json"


@pytest.fixture(autouse=True)
def no_resend_network(monkeypatch):
    """Brief fixture tests should not read the live heartbeat branch."""
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})


def fixture_items():
    return [
        item for item in (funnel._from_node(node) for node in json.loads(FIXTURE.read_text()))
        if item
    ]


def test_brief_removes_a_branchless_takeover_from_in_motion(
    monkeypatch, capsys
):
    project = funnel.Item(
        repo="nateprich/beta", number=1, title="Project",
        url="https://example.invalid/1", state="OPEN", status="Building",
        klass="Improve", children_total=2,
    )
    ghost = funnel.Item(
        repo=project.repo, number=2, title="Ghost claim",
        url="https://example.invalid/2", state="OPEN", parent=project.ref,
        in_motion_since=NOW - timedelta(minutes=31),
    )
    live = funnel.Item(
        repo=project.repo, number=3, title="Pushed work",
        url="https://example.invalid/3", state="OPEN", parent=project.ref,
        in_motion_since=NOW - timedelta(minutes=31),
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    assert funnel.cmd_brief(
        [project, ghost, live],
        NOW,
        pr_facts={ghost.ref: None, live.ref: {"branch_exists": True}},
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["in_motion"] == [live.ref]
    assert brief["stale_locks_taken_over"] == [ghost.ref]


def _dependency_item(number, *, body=None, state="OPEN", open_blockers=()):
    return funnel.Item(
        repo="nateprich/beta",
        number=number,
        title="Issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state=state,
        parent="nateprich/beta#1",
        body=body,
        open_blockers=list(open_blockers),
    )


def _approval_item(number, at, *, previous="Shaped"):
    return funnel.Item(
        repo="nateprich/beta",
        number=number,
        title="Approval {}".format(number),
        url="https://example.invalid/{}".format(number),
        state="OPEN",
        status="Ready",
        status_since=at,
        status_events=[{
            "previous_status": previous,
            "status": "Ready",
            "at": at,
        }],
    )


def test_prose_dependencies_reports_open_named_issues_without_native_edges():
    blocker = _dependency_item(7)
    missing_edge = _dependency_item(8, body="Depends on #7")
    native_edge = _dependency_item(
        9, body="Blocked on #7", open_blockers=[blocker.ref]
    )
    closed_blocker = _dependency_item(10, state="CLOSED")
    closed_target = _dependency_item(11, body="until #10")
    unrecognised = _dependency_item(12, body="See #7 when ready")

    assert funnel.prose_dependencies([
        unrecognised,
        closed_target,
        closed_blocker,
        native_edge,
        missing_edge,
        blocker,
    ]) == [{
        "ref": missing_edge.ref,
        "names": [blocker.ref],
        "sentence": "Depends on #7",
    }]


def test_prose_dependencies_recognises_each_supported_sentence_shape():
    blocker_numbers = [7, 8, 9, 10, 11]
    blockers = [_dependency_item(number) for number in blocker_numbers]
    ticket = _dependency_item(
        20,
        body=(
            "Depends on #7.\n"
            "Blocked on #8.\n"
            "Work starts after #9 lands.\n"
            "Wait until #10.\n"
            "This requires #11.\n"
        ),
    )

    assert funnel.prose_dependencies([ticket, *blockers]) == [
        {
            "ref": ticket.ref,
            "names": ["nateprich/beta#7"],
            "sentence": "Depends on #7.",
        },
        {
            "ref": ticket.ref,
            "names": ["nateprich/beta#8"],
            "sentence": "Blocked on #8.",
        },
        {
            "ref": ticket.ref,
            "names": ["nateprich/beta#9"],
            "sentence": "Work starts after #9 lands.",
        },
        {
            "ref": ticket.ref,
            "names": ["nateprich/beta#10"],
            "sentence": "Wait until #10.",
        },
        {
            "ref": ticket.ref,
            "names": ["nateprich/beta#11"],
            "sentence": "This requires #11.",
        },
    ]


def test_brief_includes_prose_dependencies_without_counting_them(monkeypatch, capsys):
    blocker = _dependency_item(7)
    ticket = _dependency_item(8, body="Depends on #7")
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([ticket, blocker], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["prose_dependencies"] == [{
        "ref": ticket.ref,
        "names": [blocker.ref],
        "sentence": "Depends on #7",
    }]
    assert brief["total_needing_nate"] == 0


def test_brief_surfaces_unclassed_captures_with_origin_without_counting_them(
    monkeypatch, capsys
):
    agent_origin = funnel.Item(
        repo="nateprich/beta", number=13, title="Observed idea",
        url="https://example.invalid/13", state="OPEN", status="Ideas",
        status_since=NOW - timedelta(hours=3),
        body=funnel.origin_block(
            "agent", at=NOW, run="agent-run", agent="codex"
        ),
    )
    classed = funnel.Item(
        repo="nateprich/beta", number=14, title="Already classed",
        url="https://example.invalid/14", state="OPEN", status="Ideas",
        klass="Improve", status_since=NOW - timedelta(hours=2),
        body=funnel.origin_block(
            "agent", at=NOW, run="classed-run", agent="codex"
        ),
    )
    nate_origin = funnel.Item(
        repo="nateprich/beta", number=15, title="Nate's idea",
        url="https://example.invalid/15", state="OPEN", status="Ideas",
        status_since=NOW - timedelta(hours=1),
        body=funnel.origin_block(
            "nate-relayed", at=NOW, run="nate-run", agent="claude"
        ),
    )
    legacy = funnel.Item(
        repo="nateprich/beta", number=16, title="Legacy idea",
        url="https://example.invalid/16", state="OPEN", status="Ideas",
        status_since=NOW - timedelta(minutes=30), body="Old capture.",
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(
        [legacy, nate_origin, classed, agent_origin], NOW
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["unclassed_captures"] == [
        {
            "ref": agent_origin.ref,
            "title": agent_origin.title,
            "url": agent_origin.url,
            "origin": "agent",
        },
        {
            "ref": nate_origin.ref,
            "title": nate_origin.title,
            "url": nate_origin.url,
            "origin": "nate-relayed",
        },
        {
            "ref": legacy.ref,
            "title": legacy.title,
            "url": legacy.url,
            "origin": "unknown",
        },
    ]
    assert classed.ref not in {
        row["ref"] for row in brief["unclassed_captures"]
    }
    assert "Ideas" not in brief["counts_by_gate"]
    assert brief["total_needing_nate"] == 0


def test_brief_surfaces_recent_self_approvals_but_not_nate_or_old_ones(
    monkeypatch, capsys
):
    self_approved = _approval_item(
        80, NOW - timedelta(hours=1), previous="Ideas"
    )
    nate_approved = _approval_item(81, NOW - timedelta(hours=2))
    old = _approval_item(
        82, NOW - funnel.MAINTENANCE_WINDOW - timedelta(seconds=1)
    )
    basis = (
        "plan declares nothing open; no escalated risk; authority signals: "
        "gate authority, policy authority"
    )
    comments = {
        80: [{"body": funnel.SELF_APPROVED_PREFIX + basis}],
        81: [{"body": "Approved at the Shaped gate — Ready."}],
        82: [{"body": funnel.SELF_APPROVED_PREFIX + "old basis"}],
    }
    calls = []

    def gh_json(*args):
        calls.append(args)
        return {"comments": comments[int(args[3])]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(
        [old, nate_approved, self_approved], NOW
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["unattended_approvals"] == [{
        "ref": self_approved.ref,
        "title": self_approved.title,
        "url": self_approved.url,
        "at": (NOW - timedelta(hours=1)).isoformat(),
        "basis": basis,
    }]
    assert "authority signals: gate authority, policy authority" in \
        brief["unattended_approvals"][0]["basis"]
    assert [call[3] for call in calls] == ["81", "80"]


def test_brief_surfaces_funnel_closed_projects_newest_first_and_with_drift(
    monkeypatch, capsys
):
    def closed(number, title, at):
        return funnel.Item(
            repo="nateprich/beta", number=number, title=title,
            url="https://example.invalid/{}".format(number), state="CLOSED",
            state_reason="COMPLETED", status="Done", closed_at=at,
        )

    newest = closed(70, "Newest upkeep", NOW - timedelta(hours=1))
    accepted = closed(71, "Nate accepted", NOW - timedelta(days=1))
    older = closed(72, "Older upkeep", NOW - timedelta(days=2))
    outside_window = closed(
        73, "Too old", NOW - funnel.CLOSED_ITSELF_WINDOW - timedelta(minutes=1)
    )
    comments = {
        newest.number: funnel.closed_itself_comment(
            [], [funnel.DRIFT_PLAN_EDIT, funnel.DRIFT_LATE_TICKET]
        ),
        accepted.number: "Nate accepted this project at the gate.",
        older.number: funnel.closed_itself_comment([], []),
        outside_window.number: funnel.closed_itself_comment([], []),
    }
    calls = []

    def gh_json(*args):
        calls.append(args)
        return {"comments": [{"body": comments[int(args[3])]}]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(
        [accepted, outside_window, older, newest], NOW
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["closed_itself"] == [
        {
            "ref": newest.ref,
            "title": newest.title,
            "url": newest.url,
            "closed_at": newest.closed_at.isoformat(),
            "drift": [funnel.DRIFT_PLAN_EDIT, funnel.DRIFT_LATE_TICKET],
        },
        {
            "ref": older.ref,
            "title": older.title,
            "url": older.url,
            "closed_at": older.closed_at.isoformat(),
            "drift": [],
        },
    ]
    assert brief["total_needing_nate"] == 0
    assert [call[3] for call in calls] == ["70", "71", "72"]


def test_brief_surfaces_parked_items_with_their_reason(monkeypatch, capsys):
    nodes = json.loads(FIXTURE.read_text())
    items = fixture_items()
    parked_node = next(node for node in nodes if node.get("park_comment"))
    calls = []

    def gh_json(*args):
        calls.append(args)
        if args[3] == "14":
            return {"comments": []}
        assert args == (
            "gh", "issue", "view", "15", "--repo", "nateprich/beta",
            "--json", "comments",
        )
        return {"comments": [
            {"body": "An unrelated comment."},
            {"body": parked_node["park_comment"]},
        ]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(items, NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["parked"] == [{
        "ref": "nateprich/beta#15",
        "title": "Rewrite everything in Rust",
        "url": "https://github.com/nateprich/beta/issues/15",
        "parked_at": "2026-09-03T00:00:00+00:00",
        "reason": "The rewrite no longer earns its maintenance cost.",
    }]
    assert calls == [
        (
            "gh", "issue", "view", "15", "--repo", "nateprich/beta",
            "--json", "comments",
        ),
        (
            "gh", "issue", "view", "14", "--repo", "nateprich/beta",
            "--json", "comments",
        ),
    ]


def test_brief_does_not_treat_ready_as_a_human_decision(monkeypatch, capsys):
    ready = funnel.Item(
        repo="nateprich/beta", number=22, title="Ready with breakdown delay",
        url="https://example.invalid/22", state="OPEN", status="Ready",
        status_since=NOW - timedelta(hours=13), children_total=1,
        first_child_created_at=NOW - timedelta(hours=1),
    )
    no_gap = funnel.Item(
        repo="nateprich/beta", number=23, title="Ready without breakdown delay",
        url="https://example.invalid/23", state="OPEN", status="Ready",
        status_since=NOW - timedelta(hours=1), children_total=1,
        first_child_created_at=NOW - timedelta(hours=1),
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([ready, no_gap], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["items"] == []
    assert brief["counts_by_gate"]["Ready"] == 2
    assert funnel.breakdown_latency(ready) == timedelta(hours=12)
    assert funnel.breakdown_latency(no_gap) is None


def test_brief_shows_pinned_items_without_marking_unpinned_items(
    monkeypatch, capsys
):
    pinned = funnel.Item(
        repo="nateprich/beta", number=24, title="Pinned project",
        url="https://example.invalid/24", state="OPEN", status="Building",
        klass="New", pinned=True, status_since=NOW - timedelta(days=2),
        children_total=1, children_done=1,
    )
    unpinned = funnel.Item(
        repo="nateprich/beta", number=25, title="Unpinned project",
        url="https://example.invalid/25", state="OPEN", status="Building",
        klass="New", status_since=NOW - timedelta(days=1),
        children_total=1, children_done=1,
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([pinned, unpinned], NOW) == 0
    brief = json.loads(capsys.readouterr().out)
    rows = {row["ref"]: row for row in brief["items"]}

    assert rows[pinned.ref]["pinned"] is True
    assert "pinned" not in rows[unpinned.ref]


def test_parked_items_are_newest_first_and_missing_reason_is_null(monkeypatch):
    older = funnel.Item(
        repo="nateprich/beta", number=20, title="Older", url="https://example.invalid/20",
        state="CLOSED", status="Parked",
        status_since=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    newer = funnel.Item(
        repo="nateprich/beta", number=21, title="Newer", url="https://example.invalid/21",
        state="CLOSED", status="Parked",
        status_since=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )

    def gh_json(*args):
        if args[3] == "21":
            return {"comments": [{"body": "No marker here."}]}
        return {"comments": [{"body": funnel.PARK_COMMENT_PREFIX + "Older reason"}]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    assert funnel.parked_json([older, newer]) == [
        {
            "ref": "nateprich/beta#21",
            "title": "Newer",
            "url": "https://example.invalid/21",
            "parked_at": "2026-09-04T00:00:00+00:00",
            "reason": None,
        },
        {
            "ref": "nateprich/beta#20",
            "title": "Older",
            "url": "https://example.invalid/20",
            "parked_at": "2026-09-01T00:00:00+00:00",
            "reason": "Older reason",
        },
    ]


def test_brief_does_not_fetch_comments_for_unparked_items(monkeypatch, capsys):
    # Closed Done projects are candidates for the closed_itself marker and
    # therefore intentionally do need a comment lookup.
    items = [
        item for item in fixture_items()
        if item.status != "Parked" and item.state != "CLOSED"
    ]
    calls = []

    def gh_json(*args):
        calls.append(args)
        raise AssertionError("brief fetched comments for an unparked item")

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(items, NOW) == 0
    json.loads(capsys.readouterr().out)
    assert calls == []


def test_parked_items_stay_out_of_gate_counts_and_maintenance_load(monkeypatch, capsys):
    items = fixture_items()
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(items, NOW) == 0
    brief = json.loads(capsys.readouterr().out)
    without_parked = [item for item in items if item.status != "Parked"]

    assert brief["counts_by_gate"]["Parked"] == 0
    assert brief["total_needing_nate"] == len(funnel.awaiting_decision(without_parked))
    assert all(item["ref"] != "nateprich/beta#15" for item in brief["items"])
    assert brief["maintenance_load"] == funnel.maintenance_load(without_parked, NOW)


def test_brief_surfaces_blocked_projects_and_tickets_oldest_first(
    monkeypatch, capsys
):
    named_project = funnel.Item(
        repo="nateprich/beta", number=30, title="Named project",
        url="https://example.invalid/30", state="OPEN", status="Ready",
        status_since=datetime(2026, 9, 1, tzinfo=timezone.utc),
        labels=["blocked"], block_references=["#84", "#90"],
        block_reason="Wait for both decisions.",
    )
    silent_project = funnel.Item(
        repo="nateprich/beta", number=31, title="Silent project",
        url="https://example.invalid/31", state="OPEN", status="Ready",
        status_since=datetime(2026, 9, 3, tzinfo=timezone.utc),
        labels=["blocked"], block_reason="Nate needs to decide.",
    )
    named_ticket = funnel.Item(
        repo="nateprich/beta", number=32, title="Named ticket",
        url="https://example.invalid/32", state="OPEN",
        status_since=datetime(2026, 9, 2, tzinfo=timezone.utc),
        labels=["blocked"], block_references=["#84"],
        block_reason="Wait for the parent decision.",
        parent="nateprich/beta#29",
    )
    ordinary = funnel.Item(
        repo="nateprich/beta", number=33, title="Ordinary issue",
        url="https://example.invalid/33", state="OPEN", status="Ready",
        status_since=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    items = [ordinary, silent_project, named_ticket, named_project]

    calls = []

    def gh_json(*args):
        calls.append(args)
        raise AssertionError("brief fetched a block comment twice")

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(items, NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert [row["ref"] for row in brief["blocked"]] == [
        "nateprich/beta#30", "nateprich/beta#32", "nateprich/beta#31",
    ]
    assert brief["blocked"] == [
        {
            "ref": "nateprich/beta#30",
            "title": "Named project",
            "url": "https://example.invalid/30",
            "reason": "Wait for both decisions.",
            "conditions": ["#84", "#90"],
            "blocked_at": "2026-09-01T00:00:00+00:00",
        },
        {
            "ref": "nateprich/beta#32",
            "title": "Named ticket",
            "url": "https://example.invalid/32",
            "reason": "Wait for the parent decision.",
            "conditions": ["#84"],
            "blocked_at": "2026-09-02T00:00:00+00:00",
        },
        {
            "ref": "nateprich/beta#31",
            "title": "Silent project",
            "url": "https://example.invalid/31",
            "reason": "Nate needs to decide.",
            "conditions": [],
            "blocked_at": "2026-09-03T00:00:00+00:00",
        },
    ]
    assert [row["ref"] for row in brief["items"]] == ["nateprich/beta#31"]
    assert all(row["ref"] != "nateprich/beta#33" for row in brief["blocked"])
    assert calls == []


def test_brief_carries_breakdown_question_on_decision_and_blocked_rows(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=36, title="Needs an answer",
        url="https://example.invalid/36", state="OPEN", status="Ready",
        status_since=NOW - timedelta(days=1), labels=["blocked"],
        needs_decision="Where should this connector live?",
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["items"][0]["needs_decision"] == (
        "Where should this connector live?"
    )
    assert brief["blocked"][0]["needs_decision"] == (
        "Where should this connector live?"
    )


def test_brief_surfaces_suspected_human_steps_separately(
    monkeypatch, capsys
):
    suspected = funnel.Item(
        repo="nateprich/beta", number=34, title="Provision the token",
        url="https://example.invalid/34", state="OPEN",
        labels=["blocked"], parent="nateprich/beta#29",
        block_reason="Human step: entering a credential",
    )
    named = funnel.Item(
        repo="nateprich/beta", number=35, title="Wait for the token",
        url="https://example.invalid/35", state="OPEN",
        labels=["blocked"], parent="nateprich/beta#29",
        block_references=["#77"],
        block_reason="Human step: entering a credential",
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([named, suspected], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["suspected_human_steps"] == [{
        "ref": "nateprich/beta#34",
        "title": "Provision the token",
        "url": "https://example.invalid/34",
        "reason": "entering a credential",
    }]


def test_brief_surfaces_open_human_steps_outside_the_decision_queue(
    monkeypatch, capsys
):
    human_step = funnel.Item(
        repo="nateprich/beta", number=40, title="Create the account",
        url="https://example.invalid/40", state="OPEN",
        parent="nateprich/beta#39",
        body="Part of the deployment.\n\nHuman step: an account or billing setting\n",
    )
    ordinary_ticket = funnel.Item(
        repo="nateprich/beta", number=41, title="Deploy the service",
        url="https://example.invalid/41", state="OPEN",
        parent="nateprich/beta#39",
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([ordinary_ticket, human_step], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["human_steps"] == [{
        "ref": "nateprich/beta#40",
        "title": "Create the account",
        "url": "https://example.invalid/40",
        "reason": "an account or billing setting",
    }]
    assert brief["items"] == []
    assert funnel.awaiting_decision([human_step]) == []


def test_brief_flags_completed_access_plan_without_any_human_ticket(
    monkeypatch, capsys
):
    missed = funnel.Item(
        repo="nateprich/beta", number=50, title="Reach the funnel",
        url="https://example.invalid/50", state="CLOSED",
        state_reason="COMPLETED", status="Done",
        closed_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        body="Deploy behind a Cloudflare Tunnel using a fine-grained token.",
    )
    carried = funnel.Item(
        repo="nateprich/beta", number=51, title="A covered project",
        url="https://example.invalid/51", state="CLOSED",
        state_reason="COMPLETED", status="Done",
        closed_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        body="Register the connector in the account.",
    )
    closed_human_step = funnel.Item(
        repo="nateprich/beta", number=52, title="Register the connector",
        url="https://example.invalid/52", state="CLOSED",
        parent=carried.ref,
        body="Human step: an app UI with no API",
    )
    quiet = funnel.Item(
        repo="nateprich/beta", number=53, title="A quiet project",
        url="https://example.invalid/53", state="CLOSED",
        state_reason="COMPLETED", status="Done",
        body="Add fixtures and run the test suite.",
    )
    parked = funnel.Item(
        repo="nateprich/beta", number=54, title="A parked project",
        url="https://example.invalid/54", state="CLOSED",
        state_reason="NOT_PLANNED", status="Parked",
        body="Use an account and token if this is resumed.",
    )
    items = [quiet, closed_human_step, parked, carried, missed]

    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(items, NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["closed_with_access_vocabulary"] == [{
        "ref": "nateprich/beta#50",
        "title": "Reach the funnel",
        "url": "https://example.invalid/50",
        "access_signals": ["token", "tunnel"],
    }]
    assert brief["human_steps"] == []


def test_brief_surfaces_agent_health_without_counting_it_as_a_decision(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=60, title="A plan with an open question",
        url="https://example.invalid/60", state="OPEN", status="Shaped",
        klass="Improve", status_since=NOW,
        body="## Needs you\n\nChoose a direction.\n",
    )
    health = [{
        "agent": "codex",
        "condition": "`codex` errored 3 times this week. Most recent: reserve",
    }]
    monkeypatch.setattr(funnel, "agent_health", lambda now: health)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["agent_health"] == health
    assert brief["total_needing_nate"] == 1


def test_brief_emits_elapsed_seconds_for_each_section(monkeypatch, capsys):
    item = funnel.Item(
        repo="nateprich/beta", number=93, title="Timed project",
        url="https://example.invalid/93", state="OPEN", status="Ready",
        status_since=NOW,
    )
    clock = [0.0]

    def fake_perf_counter():
        clock[0] += 0.1
        return clock[0]

    monkeypatch.setattr(funnel.time, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["timings"]["items"] == pytest.approx(0.1)
    assert brief["timings"]["rejected_merges"] == pytest.approx(0.1)
    assert brief["timings"]["unattended_merges"] == pytest.approx(0.1)
    assert all(value >= 0 for value in brief["timings"].values())


def test_brief_marks_an_over_budget_informational_section_degraded(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=94, title="Degraded project",
        url="https://example.invalid/94", state="OPEN", status="Ready",
        status_since=NOW,
    )
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS,
                        "working_tree_touched", 0.0)
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["working_tree_touched"] == []
    assert any(
        row["section"] == "working_tree_touched"
        for row in brief["degraded"]
    )
    assert brief["timings"]["working_tree_touched"] == 0.0


def test_brief_fails_closed_without_partial_json_when_gate_section_is_slow(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=95, title="Gate project",
        url="https://example.invalid/95", state="OPEN", status="Ready",
        status_since=NOW,
    )
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS,
                        "rejected_merges", 0.0)
    monkeypatch.setattr(funnel, "rejected_merges", lambda items, now: {})

    assert funnel.cmd_brief([item], NOW) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "brief failed closed" in captured.err
    assert "rejected_merges" in captured.err
    assert "no partial JSON emitted" in captured.err
