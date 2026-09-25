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
    """Brief fixture tests should not read live heartbeat or outcome branches."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(funnel, "_read_portfolio_metrics", lambda items, now: None)
    monkeypatch.setattr(
        funnel, "decline_routing_metric",
        lambda items, now: {"status": "available", "declines": 0},
    )
    yield
    funnel.reset_api_usage()


def fixture_items():
    return [
        item for item in (funnel._from_node(node) for node in json.loads(FIXTURE.read_text()))
        if item
    ]


def maintenance_fixture_items():
    def closed(number, klass, parent=None, status="Done"):
        return funnel.Item(
            repo="nateprich/beta",
            number=number,
            title="Closed {}".format(number),
            url="https://example.invalid/{}".format(number),
            state="CLOSED",
            state_reason="COMPLETED",
            status=status,
            klass=klass,
            parent=parent,
            closed_at=NOW - timedelta(days=1),
        )

    projects = [
        closed(number, klass)
        for number, klass in enumerate(
            ("Broken", "Maintenance", "Broken", "New", "New"), start=1
        )
    ]
    tickets = [
        closed(
            100 + number,
            None,
            parent=projects[number % len(projects)].ref,
            status=None,
        )
        for number in range(20)
    ]
    return projects + tickets


def test_maintenance_load_counts_closed_projects_not_tickets():
    load = funnel.maintenance_load(maintenance_fixture_items(), NOW)

    assert load["window_days"] == 30
    assert load["closed_in_window"] == 5
    assert load["upkeep_share"] == round(3 / 5, 3)


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


def test_brief_surfaces_unclassed_captures_with_origin_without_counting_them(
    monkeypatch, capsys
):
    agent_origin = funnel.Item(
        repo="nateprich/beta", number=13, title="Observed idea",
        url="https://example.invalid/13", state="OPEN", status="Ideas",
            status_since=NOW - timedelta(hours=3),
            origin="agent", risk="standard", needs="none",
        body=funnel.origin_block(
            "agent", at=NOW, run="agent-run", agent="codex"
        ),
    )
    classed = funnel.Item(
        repo="nateprich/beta", number=14, title="Already classed",
        url="https://example.invalid/14", state="OPEN", status="Ideas",
            klass="Improve", status_since=NOW - timedelta(hours=2),
            origin="agent", risk="standard", needs="none",
        body=funnel.origin_block(
            "agent", at=NOW, run="classed-run", agent="codex"
        ),
    )
    nate_origin = funnel.Item(
        repo="nateprich/beta", number=15, title="Nate's idea",
        url="https://example.invalid/15", state="OPEN", status="Ideas",
            status_since=NOW - timedelta(hours=1),
            origin="Nate", risk="standard", needs="none",
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
                "origin": "Nate",
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


def test_brief_carries_named_outcome_signals_without_recomputing_them(
    monkeypatch, capsys
):
    signals = {
        "schema_version": 1,
        "source": "outcomes",
        "signals": {
            "cost_per_merged_pr": {"status": "insufficient_data"},
            "rework_rate": {"status": "available", "value": 0.5},
            "intervention_rate": {"status": "available", "value": 0.25},
        },
    }
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([], NOW, outcome_signals=signals) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["outcome_signals"] == signals


def test_brief_carries_portfolio_metrics_without_recomputing_them(capsys):
    metrics = {
        "recorded_cause_regressions": {
            "broken_projects": 3,
            "with_recorded_cause": 2,
        },
        "command_center_ticket_pr_share": {
            "merged_prs": 10,
            "ticket_merged_prs": 4,
            "share": 0.4,
        },
    }

    assert funnel.cmd_brief([], NOW, portfolio_metrics=metrics) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["recorded_cause_regressions"] == metrics[
        "recorded_cause_regressions"
    ]
    assert brief["command_center_ticket_pr_share"] == metrics[
        "command_center_ticket_pr_share"
    ]


def test_brief_carries_decline_routing_section_without_recomputing_them(capsys):
    metric = {
        "window_days": 30,
        "declines": 4,
        "became_edge": 1,
        "closed_as_proven_defer": 1,
        "routed_to_review": 1,
        "stayed_blocked": 1,
    }

    assert funnel.cmd_brief([], NOW, decline_routing=metric) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["decline_routing"] == metric


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
        80: [{
            "body": funnel.SELF_APPROVED_PREFIX + basis,
            "createdAt": "2026-09-05T11:00:00Z",
        }],
        81: [{
            "body": "Approved at the Shaped gate — Ready.",
            "createdAt": "2026-09-05T10:00:00Z",
        }],
        82: [{
            "body": funnel.SELF_APPROVED_PREFIX + "old basis",
            "createdAt": "2026-08-29T12:00:00Z",
        }],
    }
    calls = []

    def gh_graphql(query, **variables):
        calls.append(query)
        _, aliases = funnel._closed_itself_comment_query(
            [nate_approved, self_approved]
        )
        response = {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"}
        }
        for ref, (repo_alias, issue_alias) in aliases.items():
            number = int(ref.rsplit("#", 1)[1])
            response.setdefault(repo_alias, {})[issue_alias] = {
                "comments": {"nodes": comments[number]}
            }
        return response

    monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
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
    assert len(calls) == 1
    assert "rateLimit { cost remaining resetAt }" in calls[0]
    assert "comments(last: {})".format(
        funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE
    ) in calls[0]
    assert "nodes { body createdAt }" in calls[0]


def test_unattended_approvals_batch_is_cached_for_one_run(monkeypatch):
    items = [
        _approval_item(83, NOW - timedelta(hours=1), previous="Ideas"),
        _approval_item(84, NOW - timedelta(hours=2)),
    ]
    comments = {
        item.number: [{"body": funnel.SELF_APPROVED_PREFIX + "basis"}]
        for item in items
    }
    calls = []

    def gh_graphql(query, **variables):
        calls.append(query)
        _, aliases = funnel._closed_itself_comment_query(items)
        response = {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"}
        }
        for ref, (repo_alias, issue_alias) in aliases.items():
            number = int(ref.rsplit("#", 1)[1])
            response.setdefault(repo_alias, {})[issue_alias] = {
                "comments": {"nodes": comments[number]}
            }
        return response

    monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
    cache = funnel.BriefCache()

    first = funnel.unattended_approvals(items, NOW, brief_cache=cache)
    second = funnel.unattended_approvals(items, NOW, brief_cache=cache)

    assert [row["ref"] for row in first] == [items[0].ref, items[1].ref]
    assert second == first
    assert len(calls) == 1


def test_brief_comment_tail_cache_is_shared_between_sections(monkeypatch):
    item = funnel.Item(
        repo="nateprich/beta", number=85, title="Both markers",
        url="https://example.invalid/85", state="CLOSED", status="Done",
            body=funnel.origin_block("agent", at=NOW, run="brief-run", agent="codex"),
            klass="Improve", origin="agent", risk="standard", needs="none",
            children_total=1, children_done=1,
        closed_at=NOW - timedelta(hours=1),
        status_events=[{
            "previous_status": "Ideas", "status": "Ready",
            "at": NOW - timedelta(hours=2),
        }],
    )
    comments = [{
        "body": funnel.closed_itself_comment([], []),
    }, {
        "body": funnel.SELF_APPROVED_PREFIX + "basis",
    }]
    calls = []

    def gh_graphql(query, **variables):
        calls.append(query)
        _, aliases = funnel._closed_itself_comment_query([item])
        repo_alias, issue_alias = aliases[item.ref]
        return {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
            repo_alias: {
                issue_alias: {"comments": {"nodes": comments}}
            },
        }

    monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
    cache = funnel.BriefCache()

    assert funnel.closed_itself_json([item], NOW, brief_cache=cache)
    assert funnel.unattended_approvals([item], NOW, brief_cache=cache)
    assert len(calls) == 1


def test_brief_surfaces_funnel_closed_projects_newest_first_and_with_drift(
    monkeypatch, capsys
):
    def closed(number, title, at):
        return funnel.Item(
            repo="nateprich/beta", number=number, title=title,
            url="https://example.invalid/{}".format(number), state="CLOSED",
                state_reason="COMPLETED", status="Done", klass="Improve",
                origin="agent", risk="standard", needs="none",
                body=funnel.origin_block("agent", at=NOW, run="brief-run", agent="codex"),
            children_total=1, children_done=1, closed_at=at,
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

    def gh_graphql(query, **variables):
        calls.append(query)
        return {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
            "repo0": {
                "issue0": {"comments": {"nodes": [
                    {"body": comments[70]}
                ]}},
                "issue1": {"comments": {"nodes": [
                    {"body": comments[71]}
                ]}},
                "issue2": {"comments": {"nodes": [
                    {"body": comments[72]}
                ]}},
            },
        }

    monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
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
    assert len(calls) == 1
    assert "rateLimit { cost remaining resetAt }" in calls[0]
    assert "comments(last: {})".format(
        funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE
    ) in calls[0]


def test_closed_itself_batch_is_bounded_and_cached_for_one_run(monkeypatch):
    def closed(number, at, repo="nateprich/beta"):
        return funnel.Item(
            repo=repo, number=number, title="Upkeep {}".format(number),
            url="https://example.invalid/{}".format(number), state="CLOSED",
                state_reason="COMPLETED", status="Done", klass="Improve",
                origin="agent", risk="standard", needs="none",
                body=funnel.origin_block("agent", at=NOW, run="brief-run", agent="codex"),
            children_total=1, children_done=1, closed_at=at,
        )

    newest = closed(101, NOW - timedelta(hours=1))
    older = closed(102, NOW - timedelta(hours=2), repo="other/repo")
    items = [newest, older]
    calls = []

    def gh_graphql(query, **variables):
        calls.append(query)
        _, aliases = funnel._closed_itself_comment_query(items)
        response = {
            "rateLimit": {"cost": 2, "remaining": 98, "resetAt": "later"}
        }
        for ref, (repo_alias, issue_alias) in aliases.items():
            body = funnel.closed_itself_comment([], [])
            response.setdefault(repo_alias, {})[issue_alias] = {
                "comments": {"nodes": [{"body": body}]}
            }
        return response

    monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
    cache = funnel.BriefCache()

    first = funnel.closed_itself_json(items, NOW, brief_cache=cache)
    second = funnel.closed_itself_json(items, NOW, brief_cache=cache)

    assert [row["ref"] for row in first] == [newest.ref, older.ref]
    assert second == first
    assert len(calls) == 1
    assert all(
        "comments(last: {})".format(funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE)
        in query
        for query in calls
    )
    assert all("rateLimit { cost remaining resetAt }" in query for query in calls)

    cache.clear()
    funnel.closed_itself_json(items, NOW, brief_cache=cache)
    assert len(calls) == 2


def test_closed_itself_candidates_follow_auto_close_eligibility_signal():
    def closed(number, **kwargs):
        return funnel.Item(
            repo="nateprich/beta", number=number, title="Project {}".format(number),
            url="https://example.invalid/{}".format(number), state="CLOSED",
            state_reason="COMPLETED", status="Done",
            closed_at=NOW - timedelta(hours=1), **kwargs
        )

    plausible = closed(
        110,
            klass="Improve",
            origin="agent", risk="standard", needs="none",
            body=funnel.origin_block("agent", at=NOW, run="brief-run", agent="codex"),
        children_total=1,
        children_done=1,
    )
    no_children = closed(
        111, klass="Improve", children_total=0, children_done=0
    )
    wrong_class = closed(
        112, klass="New", children_total=1, children_done=1
    )
    carried_human_step = closed(
        113, klass="Improve", children_total=1, children_done=1,
            body=funnel.origin_block("agent", at=NOW, run="brief-run", agent="codex"),
            origin="agent", risk="standard", needs="none",
            carried_human_step=True,
    )

    assert funnel.closed_itself_items(
        [no_children, wrong_class, carried_human_step, plausible], NOW
    ) == [plausible, carried_human_step]


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
    assert brief["pending_wakes"] == []
    assert len(calls) == 1
    assert calls[0][3] == "15"


def test_brief_reports_wakes_for_parked_items_until_they_resume(
    monkeypatch, capsys
):
    future = funnel.Item(
        repo="nateprich/beta", number=31, title="Resume the study",
        url="https://example.invalid/31", state="CLOSED", status="Parked",
        status_since=NOW,
    )
    no_wake = funnel.Item(
        repo="nateprich/beta", number=32, title="Keep this parked",
        url="https://example.invalid/32", state="CLOSED", status="Parked",
        status_since=NOW - timedelta(days=1),
    )
    resumed = funnel.Item(
        repo="nateprich/beta", number=33, title="Already resumed",
        url="https://example.invalid/33", state="OPEN", status="Ready",
        status_since=NOW,
    )
    comment_bodies = {
        31: "{}date=2026-09-10 status=Building\n{}Resume the study".format(
            funnel.PARK_WAKE_PREFIX, funnel.PARK_COMMENT_PREFIX
        ),
        32: "{}Keep this parked".format(funnel.PARK_COMMENT_PREFIX),
        33: "{}date=2026-09-01 status=Ready\n{}Already resumed".format(
            funnel.PARK_WAKE_PREFIX, funnel.PARK_COMMENT_PREFIX
        ),
    }
    comment_reads = []

    def issue_comments(item):
        comment_reads.append(item.number)
        return [{"body": comment_bodies[item.number]}]

    monkeypatch.setattr(funnel, "_issue_comments", issue_comments)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([future, no_wake, resumed], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["pending_wakes"] == [{
        "ref": future.ref,
        "title": future.title,
        "url": future.url,
        "wake_date": "2026-09-10",
        "wake_status": "Building",
    }]
    assert comment_reads == [31, 32]


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


def test_brief_reports_disposal_next_to_maintenance_load(monkeypatch, capsys):
    report = {
        "window_days": 30,
        "done": 2,
        "parked": 1,
        "finished_vs_abandoned": 2.0,
        "net_open_growth": 3,
    }
    monkeypatch.setattr(funnel, "disposal", lambda items, now: report)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["maintenance_load"] == funnel.maintenance_load([], NOW)
    assert brief["disposal"] == report


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


def test_brief_renders_event_condition_and_elapsed_wait_from_after(
    monkeypatch, capsys
):
    after = (NOW - timedelta(days=2, hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "agent": "codex",
        "job": "command-center-tickets-hourly",
        "outcome": "errored",
        "after": after,
    }
    item = funnel.Item(
        repo="nateprich/beta", number=34, title="Daily failure wait",
        url="https://example.invalid/34", state="OPEN", status="Building",
        status_since=NOW - timedelta(days=8), labels=["blocked"],
        block_reason="Wait for the next daily failure.",
        block_event=event, parent="nateprich/beta#29",
        needs="external-event",
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    row = brief["blocked"][0]
    assert row["event_condition"] == event
    assert row["event_wait"] == "2 days"
    assert row["event_wait_seconds"] == 187200.0
    assert brief["items"] == []
    assert brief["event_block_inconsistencies"] == []


def test_brief_flags_both_event_spec_and_needs_mismatches(monkeypatch, capsys):
    event = {
        "agent": "codex",
        "job": "command-center-tickets-hourly",
        "outcome": "errored",
        "after": "2026-09-04T00:00:00Z",
    }
    spec_without_needs = funnel.Item(
        repo="nateprich/beta", number=40, title="Missing event routing",
        url="https://example.invalid/40", state="OPEN", status="Building",
        labels=["blocked"], block_event=event, parent="nateprich/beta#29",
        needs="none",
    )
    needs_without_spec = funnel.Item(
        repo="nateprich/beta", number=41, title="Missing event spec",
        url="https://example.invalid/41", state="OPEN", status="Building",
        labels=["blocked"], parent="nateprich/beta#29",
        needs="external-event",
    )
    ordinary_ticket = funnel.Item(
        repo="nateprich/beta", number=42, title="Ordinary block",
        url="https://example.invalid/42", state="OPEN", status="Building",
        labels=["blocked"], parent="nateprich/beta#29", needs="none",
    )
    project = funnel.Item(
        repo="nateprich/beta", number=43, title="External-event project",
        url="https://example.invalid/43", state="OPEN", status="Ready",
        labels=["blocked"], needs="external-event",
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(
        [spec_without_needs, needs_without_spec, ordinary_ticket, project], NOW
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert [row["ref"] for row in brief["event_block_inconsistencies"]] == [
        "nateprich/beta#40", "nateprich/beta#41",
    ]
    assert [row["mismatch"] for row in brief["event_block_inconsistencies"]] == [
        "well-formed event spec without Needs: external-event",
        "Needs: external-event without a well-formed event spec",
    ]


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


def test_brief_surfaces_open_human_steps_outside_the_decision_queue(
    monkeypatch, capsys
):
    human_step = funnel.Item(
        repo="nateprich/beta", number=40, title="Create the account",
        url="https://example.invalid/40", state="OPEN",
        parent="nateprich/beta#39",
        needs="human",
    )
    ordinary_ticket = funnel.Item(
        repo="nateprich/beta", number=41, title="Deploy the service",
        url="https://example.invalid/41", state="OPEN",
        parent="nateprich/beta#39",
        needs="none",
    )
    machine_local_step = funnel.Item(
        repo="nateprich/beta", number=42, title="Run the local setup",
        url="https://example.invalid/42", state="OPEN",
        parent="nateprich/beta#39",
        needs="claude-code-environment",
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief(
        [ordinary_ticket, human_step, machine_local_step], NOW
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["human_steps"] == [{
        "ref": "nateprich/beta#40",
        "title": "Create the account",
        "url": "https://example.invalid/40",
        "reason": "human",
        # Every row says how long it has waited, as the decision rows do; a
        # fixture with no creation time reads "unknown" rather than guessing.
        "waited": "unknown",
    }]
    assert brief["machine_local_steps"] == [{
        "ref": "nateprich/beta#42",
        "title": "Run the local setup",
        "url": "https://example.invalid/42",
        "reason": "claude-code-environment",
    }]
    assert brief["items"] == []
    assert brief["total_needing_nate"] == 0
    assert funnel.awaiting_decision([human_step]) == []


def test_brief_separates_blocked_human_and_machine_local_steps(
    monkeypatch, capsys
):
    parent = funnel.Item(
        repo="nateprich/beta", number=43, title="Project",
        url="https://example.invalid/43", state="OPEN", status="Building",
        klass="New",
    )
    blocked_parent = funnel.Item(
        repo="nateprich/beta", number=44, title="Blocked project",
        url="https://example.invalid/44", state="OPEN", status="Building",
        klass="New", labels=["blocked"], parent=parent.ref,
        block_references=["#88"], block_reason="Wait for the prerequisite.",
    )
    blocker = funnel.Item(
        repo="nateprich/beta", number=45, title="Prerequisite",
        url="https://example.invalid/45", state="OPEN",
    )
    blocked_human = funnel.Item(
        repo="nateprich/beta", number=46, title="Create the account",
        url="https://example.invalid/46", state="OPEN",
        parent=parent.ref,
        needs="human",
        open_blockers=[blocker.ref],
    )
    blocked_machine_local = funnel.Item(
        repo="nateprich/beta", number=47, title="Run local setup",
        url="https://example.invalid/47", state="OPEN",
        parent=blocked_parent.ref,
        needs="claude-code-environment",
    )
    actionable_human = funnel.Item(
        repo="nateprich/beta", number=48, title="Set the account option",
        url="https://example.invalid/48", state="OPEN",
        parent=parent.ref,
        needs="human",
    )
    actionable_machine_local = funnel.Item(
        repo="nateprich/beta", number=49, title="Run the local check",
        url="https://example.invalid/49", state="OPEN",
        parent=parent.ref,
        needs="claude-code-environment",
    )

    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.cmd_brief([
        blocked_machine_local,
        actionable_machine_local,
        blocked_human,
        actionable_human,
        blocker,
        blocked_parent,
        parent,
    ], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert [row["ref"] for row in brief["human_steps"]] == [
        actionable_human.ref
    ]
    assert [row["ref"] for row in brief["machine_local_steps"]] == [
        actionable_machine_local.ref
    ]
    assert brief["blocked_human_steps"] == [{
        "ref": blocked_human.ref,
        "title": "Create the account",
        "url": "https://example.invalid/46",
        "reason": "human",
        "blocked_reason": "open native blockers",
        "blockers": [blocker.ref],
    }]
    assert brief["blocked_machine_local_steps"] == [{
        "ref": blocked_machine_local.ref,
        "title": "Run local setup",
        "url": "https://example.invalid/47",
        "reason": "claude-code-environment",
        "blocked_reason": "parent carries blocked marker",
        "blockers": [blocked_parent.ref],
    }]
    assert brief["total_needing_nate"] == 0


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
        needs="human",
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
            origin="Nate", risk="standard", needs="human",
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


def test_brief_surfaces_run_summary_with_rebegins_outside_finishes(
    monkeypatch, capsys
):
    summary = [{
        "agent": "codex",
        "starts": 2,
        "finishes": 1,
        "re_begins": 1,
    }]
    monkeypatch.setattr(funnel, "agent_run_summary", lambda now: summary)
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    assert funnel.cmd_brief([], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["run_summary"] == summary


def test_brief_keeps_readable_sections_when_one_section_cannot_be_read(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=90, title="Readable project",
        url="https://example.invalid/90", state="OPEN", status="Ready",
        status_since=NOW,
    )

    def unreadable(_now):
        raise funnel.GitHubError("rate limit")

    monkeypatch.setattr(funnel, "unattended_merges", unreadable)

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["counts_by_gate"]["Ready"] == 1
    assert brief["unattended_merges"] is None
    assert brief["missing"] == [{
        "section": "unattended_merges",
        "error": "rate limit",
    }]


def test_brief_marks_an_unreadable_comment_section_instead_of_empty_result(
    monkeypatch, capsys
):
    parked = funnel.Item(
        repo="nateprich/beta", number=91, title="Parked project",
        url="https://example.invalid/91", state="CLOSED", status="Parked",
        status_since=NOW,
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)

    assert funnel.cmd_brief([parked], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["parked"] is None
    assert brief["missing"] == [{
        "section": "parked",
        "error": "could not read comments for nateprich/beta#91",
    }]


def test_brief_surfaces_blocked_comment_load_failures(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=93, title="Blocked ticket",
        url="https://example.invalid/93", state="OPEN", status="Building",
        labels=["blocked"], block_comments_error="could not read comments",
        parent="nateprich/beta#1",
    )

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["blocked"][0]["ref"] == item.ref
    assert brief["missing"] == [{
        "section": "blocked",
        "error": "nateprich/beta#93: could not read comments",
    }]


def test_main_brief_marks_sections_depending_on_unreadable_pr_facts(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=92, title="A ticket",
        url="https://example.invalid/92", state="OPEN",
        parent="nateprich/beta#1",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def unreadable(_items):
        raise funnel.GitHubError("PR scan offline")

    monkeypatch.setattr(funnel, "ticket_pr_facts", unreadable)

    assert funnel.main(["brief"]) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["stranded"] is None
    assert brief["in_motion"] is None
    assert brief["stale_locks_taken_over"] is None
    # Filtered to this test's subject: `main_ci` is a separate live read
    # (#1219) and reports its own unreadable row beside these.
    assert [
        row for row in brief["missing"]
        if row["section"] in funnel.BRIEF_PR_FACT_SECTIONS
    ] == [
        {
            "section": section,
            "error": "could not read ticket branch facts: PR scan offline",
        }
        for section in funnel.BRIEF_PR_FACT_SECTIONS
    ]


def test_main_brief_reports_an_unreadable_project_load(
    monkeypatch, capsys
):
    def unreadable():
        raise funnel.GitHubError("Project offline")

    monkeypatch.setattr(funnel, "load_items", unreadable)

    assert funnel.main(["brief"]) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["missing"] == [{
        "section": "items",
        "error": "Project offline",
    }]


def test_main_brief_carries_project_load_timing(monkeypatch):
    observed = {}

    monkeypatch.setattr(funnel, "load_items", lambda: [])

    def fake_cmd_brief(items, now, **kwargs):
        observed["timings"] = kwargs["timings"]
        return 0

    monkeypatch.setattr(funnel, "cmd_brief", fake_cmd_brief)

    assert funnel.main(["brief"]) == 0
    assert observed["timings"]["project_load"] >= 0


def test_main_brief_reads_named_outcome_signals(monkeypatch, capsys):
    signals = {
        "schema_version": 1,
        "source": "outcomes",
        "signals": {"rework_rate": {"status": "available", "value": 1.0}},
    }
    monkeypatch.setattr(funnel, "load_items", lambda: [])
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: signals)

    assert funnel.main(["brief"]) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["outcome_signals"] == signals
    assert "outcome_signals" in brief["timings"]
    assert brief["decline_routing"] == {
        "status": "available", "declines": 0
    }
    assert "decline_routing" in brief["timings"]


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
    assert brief["timings"]["brief_assembly"] == pytest.approx(0.1)
    assert all(value >= 0 for value in brief["timings"].values())


def test_brief_places_measured_api_cost_in_documented_timings_map(monkeypatch, capsys):
    item = funnel.Item(
        repo="nateprich/beta", number=94, title="API metrics project",
        url="https://example.invalid/94", state="OPEN", status="Ready",
        status_since=NOW,
    )
    monkeypatch.setattr(
        funnel, "api_cost",
        lambda: {"graphql_points": 23, "gh_calls": 7},
    )
    monkeypatch.setattr(
        funnel, "graphql_caller_spend",
        lambda: {
            "publisher": {"calls": 3, "points": 23, "remaining": 4100},
            "unattributed": {"calls": 1, "points": None, "remaining": 4000},
        },
    )

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["timings"]["api_cost.graphql_points"] == 23
    assert brief["timings"]["api_cost.gh_calls"] == 7
    assert brief["timings"][
        "api_cost.graphql_by_caller.publisher.points"
    ] == 23
    assert brief["timings"][
        "api_cost.graphql_by_caller.unattributed.points"
    ] is None
    assert "api_cost" not in brief


def test_brief_timings_identify_a_slow_stage_without_changing_payload(
    monkeypatch, capsys
):
    """A later slowdown must name its stage instead of only blocking the brief."""
    item = funnel.Item(
        repo="nateprich/beta", number=97, title="Regression project",
        url="https://example.invalid/97", state="OPEN", status="Ready",
        status_since=NOW,
    )
    clock = [0.0]

    monkeypatch.setattr(funnel.time, "perf_counter", lambda: clock[0])

    def slow_closed_itself(*args, **kwargs):
        clock[0] += 3.5
        return []

    monkeypatch.setattr(funnel, "closed_itself_json", slow_closed_itself)

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["items"] == []
    assert brief["total_needing_nate"] == 0
    assert brief["closed_itself"] == []
    assert brief["timings"]["closed_itself"] == pytest.approx(3.5)
    assert max(brief["timings"], key=brief["timings"].get) == "closed_itself"


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


@pytest.mark.parametrize(
    "section", ["closed_itself", "cleared_blocks", "unattended_approvals"]
)
def test_brief_record_sections_allow_the_observed_two_second_read(
    monkeypatch, section
):
    """The latency that triggered #613 is below each measured section cap."""
    clock = [0.0]

    monkeypatch.setattr(funnel.time, "perf_counter", lambda: clock[0])

    def reader():
        clock[0] += 2.01
        return []

    timings = {}
    degraded = []
    assert funnel._brief_timed(section, reader, timings, degraded) == []
    assert timings[section] == pytest.approx(2.01)
    assert degraded == []


@pytest.mark.parametrize(
    "section, reader",
    [
        ("closed_itself", "closed_itself_json"),
        ("cleared_blocks", "cleared_blocks_json"),
        ("unattended_approvals", "unattended_approvals"),
        ("rejected_merges", "rejected_merges"),
    ],
)
def test_brief_degrades_a_slow_section_without_losing_the_rest(
    monkeypatch, capsys, section, reader
):
    """No section fails the whole brief: a slow read degrades explicitly
    while every other section is still emitted (#824)."""
    item = funnel.Item(
        repo="nateprich/beta", number=95, title="Slow project",
        url="https://example.invalid/95", state="OPEN", status="Ready",
        status_since=NOW,
    )
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS, section, 0.0)
    monkeypatch.setattr(funnel, reader, lambda *args: {})

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    degraded = [
        row for row in brief["degraded"] if row["section"] == section
    ]
    assert len(degraded) == 1
    assert degraded[0]["budget_seconds"] == 0.0
    assert degraded[0]["reason"]
    assert brief["timings"][section] == 0.0
    # The rest of the brief is intact, not missing.
    assert brief["items"] == []
    assert brief["total_needing_nate"] == 0
    assert brief["counts_by_gate"]["Ready"] == 1
    named = [entry["section"] for entry in brief["missing"]]
    if section == "cleared_blocks":
        # This one reads as news when empty — "nothing was unblocked" — so an
        # unread section names itself rather than degrading to [] (#1211).
        assert named == [section]
        assert brief[section] is None
    else:
        assert section not in named


def test_closed_itself_degrades_explicitly_when_over_budget(
    monkeypatch, capsys
):
    item = funnel.Item(
        repo="nateprich/beta", number=96, title="Closed project",
        url="https://example.invalid/96", state="CLOSED", status="Done",
        klass="Improve", children_total=1, children_done=1, closed_at=NOW,
    )
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS, "closed_itself", 0.0)

    assert funnel.cmd_brief([item], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["closed_itself"] == []
    assert {
        "section": "closed_itself",
        "elapsed_seconds": 0.0,
        "budget_seconds": 0.0,
        "reason": "brief budget exhausted before the section started",
    } in brief["degraded"]
    assert brief["counts_by_gate"]["Ready"] == 0


def test_ticket_pr_facts_budget_covers_the_observed_max():
    """The 22.9 s observed max over 738 refs fits inside the budget (#1210)."""
    assert funnel.BRIEF_SECTION_BUDGETS["ticket_pr_facts"] == 35.0


def test_ticket_pr_facts_section_succeeds_under_the_raised_budget(
    monkeypatch,
):
    """A read taking the observed 22.9 s succeeds with no degraded entry."""
    clock = [0.0]

    monkeypatch.setattr(funnel.time, "perf_counter", lambda: clock[0])

    def reader():
        clock[0] += 22.9
        return {"facts": True}

    timings = {}
    degraded = []
    assert funnel._brief_timed(
        "ticket_pr_facts", reader, timings, degraded
    ) == {"facts": True}
    assert timings["ticket_pr_facts"] == pytest.approx(22.9)
    assert degraded == []


def test_main_brief_retries_a_pr_facts_timeout_once_within_deadline(
    monkeypatch, capsys
):
    """A first-attempt timeout is retried once sharing the section deadline;
    when the retry succeeds the dependent sections render instead of
    reporting missing (#1210)."""
    item = funnel.Item(
        repo="nateprich/beta", number=92, title="A ticket",
        url="https://example.invalid/92", state="OPEN",
        parent="nateprich/beta#1",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    calls = []

    def flaky(_items):
        calls.append(1)
        if len(calls) == 1:
            raise funnel.BriefSectionTimeout(
                "ticket_pr_facts", "section read timed out"
            )
        return {}

    monkeypatch.setattr(funnel, "ticket_pr_facts", flaky)

    assert funnel.main(["brief"]) == 0
    brief = json.loads(capsys.readouterr().out)

    assert len(calls) == 2
    assert brief["stranded"] is not None
    assert brief["in_motion"] is not None
    assert brief["stale_locks_taken_over"] is not None
    assert [
        entry for entry in brief["missing"]
        if entry["section"] in funnel.BRIEF_PR_FACT_SECTIONS
    ] == []


def test_main_brief_retry_exhausted_still_reports_degraded(
    monkeypatch, capsys
):
    """Two timeouts (first attempt plus the one shared-deadline retry) leave
    the dependent sections missing and record the degraded section (#1210)."""
    item = funnel.Item(
        repo="nateprich/beta", number=92, title="A ticket",
        url="https://example.invalid/92", state="OPEN",
        parent="nateprich/beta#1",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    calls = []

    def always_slow(_items):
        calls.append(1)
        raise funnel.BriefSectionTimeout(
            "ticket_pr_facts", "section read timed out"
        )

    monkeypatch.setattr(funnel, "ticket_pr_facts", always_slow)

    assert funnel.main(["brief"]) == 0
    brief = json.loads(capsys.readouterr().out)

    assert len(calls) == 2
    assert brief["stranded"] is None
    assert brief["in_motion"] is None
    assert brief["stale_locks_taken_over"] is None
    # Filtered to this test's subject, as above: `main_ci` is a separate live
    # read (#1219) and reports its own unreadable row beside these.
    assert [
        row for row in brief["missing"]
        if row["section"] in funnel.BRIEF_PR_FACT_SECTIONS
    ] == [
        {
            "section": section,
            "error": "could not read ticket branch facts: "
                     "brief section read timed out",
        }
        for section in funnel.BRIEF_PR_FACT_SECTIONS
    ]
    degraded = [
        row for row in brief["degraded"]
        if row["section"] == "ticket_pr_facts"
    ]
    assert len(degraded) == 1
    assert degraded[0]["budget_seconds"] == 35.0


def test_a_timed_out_parked_section_reads_as_unread_not_empty(
    monkeypatch, capsys
):
    """`[]` on this section says "nothing is parked", which is news, and the
    opposite of what a timeout means (#1211)."""
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS, "parked", 0.0)

    assert funnel.cmd_brief([], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["parked"] is None
    assert brief["pending_wakes"] is None
    assert [row["section"] for row in brief["missing"]] == ["parked"]
    assert "not an empty one" in brief["missing"][0]["error"]
    assert any(row["section"] == "parked" for row in brief["degraded"])


def test_a_timed_out_cleared_blocks_section_reads_as_unread_not_empty(
    monkeypatch, capsys
):
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS, "cleared_blocks", 0.0)

    assert funnel.cmd_brief([], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["cleared_blocks"] is None
    assert [row["section"] for row in brief["missing"]] == ["cleared_blocks"]


def test_both_sections_still_read_as_lists_when_they_succeed(capsys):
    assert funnel.cmd_brief([], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["parked"] == []
    assert brief["pending_wakes"] == []
    assert brief["cleared_blocks"] == []
    assert brief["missing"] == []


def test_the_two_budgets_clear_their_measured_reads():
    """Measured 2026-09-21 on 1091 items: parked 27.86/19.17/22.24 s,
    cleared_blocks 20.93/25.17/22.94 s. The ticket's 5 s and 12 s came from a
    738-ref board and would degrade every one of those reads."""
    assert funnel.BRIEF_SECTION_BUDGETS["parked"] >= 30.0
    assert funnel.BRIEF_SECTION_BUDGETS["cleared_blocks"] >= 30.0
