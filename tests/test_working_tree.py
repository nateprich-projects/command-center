"""Detection where prevention is not available.

No routine has business writing Nate's own checkout: the engineers work in their
own clones and the reviewers are read-only. Codex is actually stopped, by its
sandbox writable roots. zcode has no equivalent setting — and its permission
modes offer no allowlist, so the only mode that can run unattended is the
permissive one. Recording the tree's state at both ends of a run cannot prevent a
violation, but it stops one being invisible: on 2026-09-06 a run added worktrees
and ran `git pull` in his checkout, and nothing recorded it except the transcript.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402

NOW = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)


def rec(phase, run, repo, hours_ago=1.0):
    return {"phase": phase, "run": run, "repo": repo,
            "ts": int((NOW - timedelta(hours=hours_ago)).timestamp())}


def wire(monkeypatch, rows):
    monkeypatch.setattr(heartbeat, "read",
                        lambda agent: rows if agent == "zcode" else [])
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"zcode": "zai"})


def wire_agents(monkeypatch, rows_by_agent):
    monkeypatch.setattr(heartbeat, "read",
                        lambda agent: rows_by_agent.get(agent, []))
    monkeypatch.setattr(
        heartbeat, "PROVIDERS",
        {agent: agent for agent in rows_by_agent},
    )


def test_an_unchanged_tree_reports_nothing(monkeypatch):
    state = {"head": "aaaaaaaaaaaa", "dirty": 0}
    wire(monkeypatch, [rec("start", "r1", state), rec("finish", "r1", state)])
    assert funnel.working_tree_touched(NOW) == []


def test_a_moved_head_is_reported(monkeypatch):
    """The 2026-09-06 case: a run ran `git pull` in his checkout."""
    wire(monkeypatch, [rec("start", "r1", {"head": "aaaaaaaaaaaa", "dirty": 0}),
                       rec("finish", "r1", {"head": "bbbbbbbbbbbb", "dirty": 0})])
    found = funnel.working_tree_touched(NOW)
    assert len(found) == 1
    assert found[0]["observers"] == [{"agent": "zcode", "run": "r1"}]


def test_repeated_head_transitions_collapse_and_carry_observers(monkeypatch):
    transitions = [
        ("aaaaaaaaaaaa", "bbbbbbbbbbbb", ["r1", "r2"]),
        ("bbbbbbbbbbbb", "cccccccccccc", ["r3", "r4", "r5"]),
        ("cccccccccccc", "dddddddddddd", ["r6", "r7", "r8", "r9"]),
        ("dddddddddddd", "eeeeeeeeeeee", ["r10"]),
    ]
    rows = []
    for index, (before, after, runs) in enumerate(transitions):
        for offset, run in enumerate(runs):
            hours_ago = index * 2 + offset / 10
            rows.extend([
                rec("start", run, {"head": before, "dirty": 0}, hours_ago),
                rec("finish", run, {"head": after, "dirty": 0}, hours_ago),
            ])
    wire(monkeypatch, rows)

    found = funnel.working_tree_touched(NOW)

    assert len(found) == 4
    grouped = {
        (row["before"]["head"], row["after"]["head"]): row
        for row in found
    }
    for before, after, runs in transitions:
        assert sorted(grouped[(before, after)]["observers"],
                      key=lambda observer: observer["run"]) == [
            {"agent": "zcode", "run": run} for run in runs
        ]


def test_dirty_only_changes_stay_one_row_per_run(monkeypatch):
    rows = []
    for run, dirty_before, dirty_after in (
        ("r1", 0, 1),
        ("r2", 1, 3),
    ):
        rows.extend([
            rec("start", run, {"head": "aaaaaaaaaaaa", "dirty": dirty_before}),
            rec("finish", run, {"head": "aaaaaaaaaaaa", "dirty": dirty_after}),
        ])
    wire(monkeypatch, rows)

    found = funnel.working_tree_touched(NOW)

    assert [row["run"] for row in found] == ["r1", "r2"]
    assert all("observers" not in row for row in found)


def test_a_single_observer_is_still_explicit(monkeypatch):
    rows_by_agent = {
        "zcode": [
            rec("start", "z1", {"head": "aaaaaaaaaaaa", "dirty": 0}),
            rec("finish", "z1", {"head": "bbbbbbbbbbbb", "dirty": 0}),
        ],
        "codex": [
            rec("start", "c1", {"head": "cccccccccccc", "dirty": 0}),
            rec("finish", "c1", {"head": "dddddddddddd", "dirty": 0}),
        ],
    }
    wire_agents(monkeypatch, rows_by_agent)

    found = funnel.working_tree_touched(NOW)

    assert len(found) == 2
    assert all(len(row["observers"]) == 1 for row in found)
    assert {
        (row["observers"][0]["agent"], row["observers"][0]["run"])
        for row in found
    } == {("zcode", "z1"), ("codex", "c1")}


def test_new_uncommitted_files_are_reported(monkeypatch):
    wire(monkeypatch, [rec("start", "r1", {"head": "aaaaaaaaaaaa", "dirty": 0}),
                       rec("finish", "r1", {"head": "aaaaaaaaaaaa", "dirty": 3})])
    assert len(funnel.working_tree_touched(NOW)) == 1


def test_a_run_with_no_reading_is_not_an_accusation(monkeypatch):
    """repo_state is best effort. Absent is unknown, not a violation."""
    wire(monkeypatch, [rec("start", "r1", None),
                       rec("finish", "r1", {"head": "bbbbbbbbbbbb", "dirty": 0})])
    assert funnel.working_tree_touched(NOW) == []


def test_old_runs_fall_out_of_the_window(monkeypatch):
    wire(monkeypatch, [rec("start", "r1", {"head": "a", "dirty": 0}, hours_ago=24 * 60),
                       rec("finish", "r1", {"head": "b", "dirty": 0}, hours_ago=24 * 60)])
    assert funnel.working_tree_touched(NOW) == []


def test_repo_state_is_never_fatal(monkeypatch):
    """Instrumentation that can stop a run is worse than instrumentation absent."""
    monkeypatch.setattr(heartbeat, "CANONICAL_REPO", "/nowhere/at/all")
    assert heartbeat.repo_state() is None
