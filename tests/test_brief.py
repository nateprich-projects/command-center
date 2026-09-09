"""The brief's parked section and its queue exclusions."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "project_items.json"


def fixture_items():
    return [
        item for item in (funnel._from_node(node) for node in json.loads(FIXTURE.read_text()))
        if item
    ]


def test_brief_surfaces_parked_items_with_their_reason(monkeypatch, capsys):
    nodes = json.loads(FIXTURE.read_text())
    items = fixture_items()
    parked_node = next(node for node in nodes if node.get("park_comment"))
    calls = []

    def gh_json(*args):
        calls.append(args)
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
    assert calls == [(
        "gh", "issue", "view", "15", "--repo", "nateprich/beta",
        "--json", "comments",
    )]


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
    items = [item for item in fixture_items() if item.status != "Parked"]
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
