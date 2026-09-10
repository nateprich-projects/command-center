"""Fixtures for parsed block conditions and their satisfied state."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


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
    parsed = funnel.parse_block_comment([comment]) if comment is not None else None
    references, reason = parsed or ([], None)
    return funnel.Item(
        repo=REPO,
        number=number,
        title="issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state=state,
        block_references=references,
        block_reason=reason,
        open_blockers=list(open_blockers),
        dead_blockers=list(dead_blockers),
    )


@pytest.mark.parametrize(
    "body, expected",
    [
        pytest.param(
            "**Blocked on #77:** waiting for the scan.",
            (["#77"], "waiting for the scan."),
            id="single-reference",
        ),
        pytest.param(
            "**Blocked on #138 and #141:** waiting for both decisions.",
            (["#138", "#141"], "waiting for both decisions."),
            id="multiple-references",
        ),
        pytest.param(
            "**Blocked:** waiting for Nate to provision the token.",
            ([], "waiting for Nate to provision the token."),
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
        return {"comments": [{"body": comments[number]}]}

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
