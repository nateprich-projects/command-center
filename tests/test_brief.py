"""The brief's parked section and its queue exclusions."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

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
