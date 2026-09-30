"""Fixtures for parsed block conditions and their satisfied state."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


REPO = "owner/repo"
NOW = datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc)


def issue(
    number,
    *,
    comment=None,
    state="OPEN",
    open_blockers=(),
    dead_blockers=(),
):
    parsed = (
        funnel._parse_block_comment_details([comment])
        if comment is not None else None
    )
    references, blocked_until, reason, event = parsed or (
        [], None, None, None
    )
    return funnel.Item(
        repo=REPO,
        number=number,
        title="issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state=state,
        block_references=references,
        block_reason=reason,
        blocked_until=blocked_until,
        block_event=event,
        open_blockers=list(open_blockers),
        dead_blockers=list(dead_blockers),
    )


EVENT_COMMENT = '''**Blocked until event:**
```json
{
  "agent": "codex",
  "job": "command-center-tickets-hourly",
  "outcome": "errored",
  "after": "2026-09-09T15:00:00Z"
}
```
Wait for the scheduled run to fail.
'''


def event_finish(*, ts, agent="codex", job="command-center-tickets-hourly",
                 outcome="errored", run="run-event-1"):
    return {
        "agent": agent,
        "job": job,
        "phase": "finish",
        "ts": ts,
        "outcome": outcome,
        "run": run,
    }


def event_issue():
    waiting = issue(754, comment=EVENT_COMMENT)
    waiting.labels = ["blocked"]
    waiting.needs = "external-event"
    waiting.item_id = "project-item-754"
    return waiting


@pytest.mark.parametrize(
    "body, expected",
    [
        pytest.param(
            "**Blocked on #77:** waiting for the scan.",
            (["#77"], None, "waiting for the scan."),
            id="single-reference",
        ),
        pytest.param(
            "**Blocked on #138 and #141:** waiting for both decisions.",
            (["#138", "#141"], None, "waiting for both decisions."),
            id="multiple-references",
        ),
        pytest.param(
            "**Blocked:** waiting for Nate to provision the token.",
            ([], None, "waiting for Nate to provision the token."),
            id="no-reference",
        ),
    ],
)
def test_block_comment_fixtures_parse(body, expected):
    assert funnel.parse_block_comment([body]) == expected


@pytest.mark.parametrize(
    "number, blocker_number",
    [
        pytest.param(108, 77, id="108-blocked-on-77"),
        pytest.param(141, 138, id="141-blocked-on-138"),
        pytest.param(145, 138, id="145-blocked-on-138"),
        pytest.param(168, 161, id="168-blocked-on-161"),
    ],
)
def test_closed_block_fixtures_are_satisfied(number, blocker_number):
    waiting = issue(
        number,
        comment="**Blocked on #{}:** waiting for the prerequisite.".format(
            blocker_number
        ),
    )
    blocker = issue(blocker_number, state="CLOSED")

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, blocker.ref: blocker}
    ) == [blocker.ref]


def test_empty_reference_block_is_never_satisfied():
    human_step = issue(
        270,
        comment="**Blocked:** waiting for Nate to provision the token.",
    )

    assert human_step.block_references == []
    assert human_step.block_reason is not None
    assert funnel.satisfied_block_refs(human_step, {}) is None


def test_open_chain_block_is_not_satisfied():
    waiting = issue(
        109,
        comment="**Blocked on #108:** waiting for the earlier ticket to land.",
    )
    not_landed = issue(108)

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, not_landed.ref: not_landed}
    ) is None


def test_native_open_blocker_keeps_a_closed_named_block_unsatisfied():
    waiting = issue(
        108,
        comment="**Blocked on #77:** waiting for the scan.",
        open_blockers=["#77"],
    )
    blocker = issue(77, state="CLOSED")
    waiting.satisfied_block_record = funnel.parse_satisfied_block_comment(
        funnel.satisfied_block_comment(
            [blocker.ref], NOW, run="run-1874", agent="codex",
        )
    )
    assert waiting.satisfied_block_record is not None

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, blocker.ref: blocker}
    ) is None


@pytest.mark.parametrize(
    "number, blocker_number",
    [(108, 77), (141, 138), (145, 138), (168, 161)],
)
def test_fully_satisfied_blocks_are_recorded_then_cleared(
    monkeypatch, number, blocker_number
):
    waiting = issue(
        number,
        comment="**Blocked on #{}:** waiting for the prerequisite.".format(
            blocker_number
        ),
    )
    waiting.labels = ["blocked"]
    blocker = issue(blocker_number, state="CLOSED")
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    cleared = funnel.clear_satisfied_blocks(
        [waiting, blocker], NOW, run="run-305", agent="codex"
    )

    assert cleared == [{
        "ref": waiting.ref,
        "conditions": [blocker.ref],
        "cleared_at": NOW.isoformat(),
    }]
    assert calls[0][:6] == (
        "gh", "issue", "comment", str(number), "--repo", REPO,
    )
    record = funnel.parse_satisfied_block_comment(calls[0][-1])
    assert record == {
        "conditions": [blocker.ref],
        "found_closed_at": "2026-09-09T16:00:00Z",
    }
    assert funnel.parse_provenance(calls[0][-1]) == {
        "agent": "codex",
        "at": NOW.isoformat(),
        "run": "run-305",
        "voice": "agent",
    }
    assert calls[1] == (
        "gh", "issue", "edit", str(number), "--repo", REPO,
        "--remove-label", "blocked",
    )
    assert not waiting.is_blocked
    assert waiting.blocked_cleared_at == NOW


def test_newer_decline_does_not_reuse_satisfied_older_header(monkeypatch):
    repo = "nateprich-projects/command-center"
    waiting = issue(2005)
    waiting.repo = repo
    waiting.labels = ["blocked"]
    waiting.blocked_since = funnel.parse_time("2026-09-30T03:29:24Z")
    blocker = issue(2006, state="CLOSED")
    blocker.repo = repo
    header = "**Blocked on #2006:** Complete the human step before resuming."
    old_satisfied = funnel.satisfied_block_comment(
        [blocker.ref],
        funnel.parse_time("2026-09-30T02:41:23Z"),
        run="run-2005-satisfied",
        agent="codex",
    )
    comments = [
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T00:32:27Z",
            "body": header,
        },
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T02:42:49Z",
            "body": old_satisfied,
        },
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T03:21:03Z",
            "body": "**Declined:** The ticket prerequisite is false in this run.",
        },
    ]
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"comments": comments}
    )
    funnel._load_block_comment(waiting)
    now = datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc)
    by_ref = {waiting.ref: waiting, blocker.ref: blocker}
    assert waiting.satisfied_block_record == {
        "conditions": [blocker.ref],
        "found_closed_at": "2026-09-30T02:41:23Z",
    }
    assert funnel.satisfied_block_refs(waiting, by_ref, now=now) is None
    assert not funnel._record_covers_current_block(waiting, [blocker.ref])

    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    funnel.clear_satisfied_blocks(
        [waiting, blocker], now,
        run="run-2019",
        agent="codex",
    )

    assert calls == []
    assert waiting.is_blocked


def test_newer_header_after_decline_clears_and_records_satisfied_refs(
    monkeypatch,
):
    repo = "nateprich-projects/command-center"
    newer_blocker_ref = "nateprich-projects/command-center#2007"
    waiting = issue(2005)
    waiting.repo = repo
    waiting.labels = ["blocked"]
    waiting.blocked_since = funnel.parse_time("2026-09-30T03:29:24Z")
    older_blocker = issue(2006, state="CLOSED")
    older_blocker.repo = repo
    newer_blocker = issue(2007, state="CLOSED")
    newer_blocker.repo = repo
    old_satisfied = funnel.satisfied_block_comment(
        ["nateprich-projects/command-center#2006"],
        funnel.parse_time("2026-09-30T02:41:23Z"),
        run="run-2005-satisfied",
        agent="codex",
    )
    comments = [
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T03:40:00Z",
            "body": "**Blocked on #2007:** Wait for the tracked prerequisite.",
        },
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T00:32:27Z",
            "body": "**Blocked on #2006:** Complete the human step.",
        },
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T02:42:49Z",
            "body": old_satisfied,
        },
        {
            "author": {"login": "nateprich"},
            "createdAt": "2026-09-30T03:21:03Z",
            "body": "**Declined:** The ticket prerequisite is false in this run.",
        },
    ]
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"comments": comments}
    )
    funnel._load_block_comment(waiting)
    assert waiting.block_references == ["#2007"]

    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    cleared = funnel.clear_satisfied_blocks(
        [waiting, older_blocker, newer_blocker],
        datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc),
        run="run-2019",
        agent="codex",
    )

    assert cleared == [{
        "ref": "nateprich-projects/command-center#2005",
        "conditions": [newer_blocker_ref],
        "cleared_at": "2026-09-30T05:00:00+00:00",
    }]
    assert len(calls) == 2
    assert calls[0][:6] == (
        "gh", "issue", "comment", "2005", "--repo", repo,
    )
    assert funnel.parse_satisfied_block_comment(calls[0][-1]) == {
        "conditions": [newer_blocker_ref],
        "found_closed_at": "2026-09-30T05:00:00Z",
    }
    assert calls[1] == (
        "gh", "issue", "edit", "2005", "--repo", repo,
        "--remove-label", "blocked",
    )
    assert not waiting.is_blocked


def test_declined_unblock_clears_legacy_human_needs_before_label(
    monkeypatch,
):
    # #1902's post-decline shape: a declined, condition-backed Unblock? whose
    # Needs field still says human after the watch removes the blocked label.
    waiting = issue(
        1902, comment="**Blocked on #1899:** waiting for the evidence."
    )
    waiting.parent = REPO + "#1901"
    waiting.labels = ["blocked"]
    waiting.needs = "human"
    waiting.item_id = "project-item-1902"
    waiting.decline_reason = "The accepted evidence is not available."
    blocker = issue(1899, state="CLOSED")
    actions = []

    def run(args, capture_output, text=True):
        actions.append(("gh", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def write_project_select(item_id, field, value, ref):
        actions.append(("project", item_id, field, value, ref))

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "write_project_select", write_project_select)

    cleared = funnel.clear_satisfied_blocks(
        [waiting, blocker], NOW, run="run-1956", agent="codex"
    )

    assert cleared == [{
        "ref": waiting.ref,
        "conditions": [blocker.ref],
        "cleared_at": NOW.isoformat(),
    }]
    assert waiting.needs == "none"
    assert actions[1] == (
        "project", "project-item-1902", "Needs", "none", waiting.ref
    )
    assert actions[2] == (
        "gh", (
            "gh", "issue", "edit", "1902", "--repo", REPO,
            "--remove-label", "blocked",
        ),
    )


def test_satisfied_human_step_without_decline_keeps_needs_human(monkeypatch):
    waiting = issue(
        1903, comment="**Blocked on #1899:** complete Nate's step."
    )
    waiting.parent = REPO + "#1901"
    waiting.labels = ["blocked"]
    waiting.needs = "human"
    waiting.item_id = "project-item-1903"
    blocker = issue(1899, state="CLOSED")
    project_writes = []

    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda *args: project_writes.append(args),
    )

    funnel.clear_satisfied_blocks([waiting, blocker], NOW)

    assert waiting.needs == "human"
    assert project_writes == []


def test_satisfied_block_marker_must_start_a_runner_comment_line():
    record = funnel.satisfied_block_comment(
        ["owner/repo#77"], NOW, run="run-1874", agent="codex",
    )

    assert funnel.parse_satisfied_block_comment(record) == {
        "conditions": ["owner/repo#77"],
        "found_closed_at": "2026-09-09T16:00:00Z",
    }
    assert funnel.parse_satisfied_block_comment(
        "Cleaned breakdown question: " + record
    ) is None
    for prefix in (" ", "\t", "> "):
        assert funnel.parse_satisfied_block_comment(prefix + record) is None


def test_every_failed_conjunction_part_reports_but_never_clears(monkeypatch):
    empty = issue(
        270, comment="**Blocked:** waiting for Nate to provision the token."
    )
    unparseable = issue(
        271, comment="**Blocked on #77, since yesterday:** malformed."
    )
    unresolvable = issue(272)
    unresolvable.block_reason = "Malformed reference."
    unresolvable.block_references = ["77"]
    missing = issue(273, comment="**Blocked on #77:** missing from the Project.")
    chain = issue(109, comment="**Blocked on #108:** still waiting.")
    open_blocker = issue(108)
    for item in (empty, unparseable, unresolvable, missing, chain):
        item.labels = ["blocked"]

    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("an unsatisfied block was mutated"),
    )

    assert funnel.clear_satisfied_blocks(
        [empty, unparseable, unresolvable, missing, chain, open_blocker], NOW,
        run="run-305", agent="codex",
    ) == []


def test_retry_reuses_a_current_record_before_removing_the_label(monkeypatch):
    waiting = issue(108, comment="**Blocked on #77:** waiting.")
    waiting.labels = ["blocked"]
    waiting.blocked_since = NOW - timedelta(hours=1)
    waiting.satisfied_block_record = {
        "conditions": ["owner/repo#77"],
        "found_closed_at": "2026-09-09T16:00:00Z",
    }
    blocker = issue(77, state="CLOSED")
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    funnel.clear_satisfied_blocks(
        [waiting, blocker], NOW, run="run-305", agent="codex"
    )

    assert calls == [(
        "gh", "issue", "edit", "108", "--repo", REPO,
        "--remove-label", "blocked",
    )]


def test_label_failure_leaves_a_reusable_provenance_record(monkeypatch):
    waiting = issue(108, comment="**Blocked on #77:** waiting.")
    waiting.labels = ["blocked"]
    blocker = issue(77, state="CLOSED")
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[2] == "edit":
            return SimpleNamespace(returncode=1, stdout="", stderr="offline")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    with pytest.raises(funnel.GitHubError, match="recorded satisfied block"):
        funnel.clear_satisfied_blocks(
            [waiting, blocker], NOW, run="run-305", agent="codex"
        )

    assert funnel.parse_satisfied_block_comment(calls[0][-1]) is not None
    assert calls[1][2] == "edit"
    assert waiting.is_blocked


def test_matching_heartbeat_finish_names_record_then_clears_event_hold(
    monkeypatch,
):
    waiting = event_issue()
    finish = event_finish(ts=int(NOW.timestamp()), run="run-event-1451")
    calls = []
    project_writes = []

    monkeypatch.setattr(
        heartbeat, "read_github", lambda agent: [finish]
    )
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda *args: project_writes.append(args),
    )

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    cleared = funnel.clear_satisfied_blocks(
        [waiting], NOW, run="clear-run", agent="codex"
    )

    expected_condition = (
        "heartbeat finish agent=codex job=command-center-tickets-hourly "
        "outcome=errored run=run-event-1451 at=2026-09-09T16:00:00Z"
    )
    assert cleared == [{
        "ref": waiting.ref,
        "conditions": [expected_condition],
        "cleared_at": NOW.isoformat(),
    }]
    assert len([call for call in calls if call[2] == "comment"]) == 1
    assert expected_condition in calls[0][-1]
    assert project_writes == [(
        "project-item-754", "Needs", "none", waiting.ref
    )]
    assert calls[-1] == (
        "gh", "issue", "edit", "754", "--repo", REPO,
        "--remove-label", "blocked",
    )
    assert not waiting.is_blocked


def test_heartbeat_finish_before_event_threshold_does_not_clear(monkeypatch):
    waiting = event_issue()
    finish = event_finish(ts=int((NOW - timedelta(hours=2)).timestamp()))
    monkeypatch.setattr(heartbeat, "read_github", lambda agent: [finish])
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("an early event was mutated"),
    )

    assert funnel.clear_satisfied_blocks([waiting], NOW) == []
    assert waiting.is_blocked


def test_no_matching_heartbeat_finish_leaves_event_hold_untouched(monkeypatch):
    waiting = event_issue()
    finish = event_finish(
        ts=int(NOW.timestamp()), job="another-scheduled-job"
    )
    monkeypatch.setattr(heartbeat, "read_github", lambda agent: [finish])
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: pytest.fail(
            "a non-matching event was mutated"
        ),
    )

    assert funnel.clear_satisfied_blocks([waiting], NOW) == []
    assert waiting.is_blocked


def test_item_load_tracks_the_actual_blocked_label_removal_event():
    node = {
        "id": "item-108",
        "status": None,
        "class": None,
        "pinned": None,
        "lock": None,
        "content": {
            "number": 108,
            "title": "waiting",
            "url": "https://example.invalid/108",
            "body": "",
            "state": "OPEN",
            "stateReason": None,
            "closedAt": None,
            "repository": {"nameWithOwner": REPO},
            "labels": {"nodes": []},
            "assignees": {"nodes": []},
            "parent": {
                "number": 100,
                "repository": {"nameWithOwner": REPO},
            },
            "subIssuesSummary": {"total": 0, "completed": 0},
            "subIssues": {"nodes": []},
            "blockedBy": {"nodes": []},
            "timelineItems": {"nodes": [{
                "__typename": "UnlabeledEvent",
                "createdAt": "2026-09-09T15:59:00Z",
                "label": {"name": "blocked"},
            }]},
        },
    }

    item = funnel._from_node(node)

    assert item.blocked_cleared_at == datetime(
        2026, 9, 9, 15, 59, tzinfo=timezone.utc
    )


def test_recent_clears_surface_from_the_label_event_and_provenance(monkeypatch):
    newest = issue(108)
    newest.blocked_cleared_at = NOW - timedelta(hours=1)
    older = issue(141, state="CLOSED")
    older.blocked_cleared_at = NOW - timedelta(days=2)
    expired = issue(168)
    expired.blocked_cleared_at = NOW - timedelta(days=8)
    comments = {
        108: funnel.satisfied_block_comment(
            ["owner/repo#77"], NOW - timedelta(hours=1),
            run="run-108", agent="codex",
        ),
        141: funnel.satisfied_block_comment(
            ["owner/repo#138"], NOW - timedelta(days=2),
            run="run-141", agent="codex",
        ),
    }
    calls = []

    def gh_json(*args):
        number = int(args[3])
        calls.append(number)
        return {"comments": [{"author": {"login": "nateprich"},
                              "body": comments[number]}]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    assert funnel.cleared_blocks_json([older, expired, newest], NOW) == [
        {
            "ref": newest.ref,
            "title": newest.title,
            "url": newest.url,
            "conditions": ["owner/repo#77"],
            "cleared_at": newest.blocked_cleared_at.isoformat(),
        },
        {
            "ref": older.ref,
            "title": older.title,
            "url": older.url,
            "conditions": ["owner/repo#138"],
            "cleared_at": older.blocked_cleared_at.isoformat(),
        },
    ]
    assert calls == [108, 141]


def test_a_forged_satisfied_block_record_from_another_author_is_ignored(
        monkeypatch):
    """Only the owner account's record is an agent's clear (#1788)."""
    record = funnel.satisfied_block_comment(
        ["owner/repo#77"], NOW - timedelta(hours=1),
        run="run-108", agent="codex",
    )
    rows = {"owner": {"author": {"login": "nateprich"}, "body": record},
            "outsider": {"author": {"login": "mallory"}, "body": record}}
    posted = {"by": "outsider"}
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"comments": [rows[posted["by"]]]})

    cleared = issue(108)
    cleared.blocked_cleared_at = NOW - timedelta(hours=1)
    waiting = issue(109, comment="**Blocked on #77:** waiting.")
    waiting.labels = ["blocked"]

    assert funnel.cleared_blocks_json([cleared], NOW) == []
    funnel._load_block_comment(waiting)
    assert waiting.satisfied_block_record is None

    posted["by"] = "owner"
    assert [row["conditions"] for row in funnel.cleared_blocks_json(
        [cleared], NOW)] == [["owner/repo#77"]]
    funnel._load_block_comment(waiting)
    assert waiting.satisfied_block_record["conditions"] == ["owner/repo#77"]
