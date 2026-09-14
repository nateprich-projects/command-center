"""The block-comment convention and its fail-closed parser."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def comment_item():
    return funnel.Item(
        repo="nateprich/beta", number=42, title="A ticket",
        url="https://github.com/nateprich/beta/issues/42", state="OPEN",
    )


def test_named_block_comment_returns_references_and_reason():
    assert funnel.parse_block_comment([
        "**Blocked on #84 and #90:** Wait for both decisions.",
    ]) == (["#84", "#90"], None, "Wait for both decisions.")


def test_block_comment_without_a_condition_returns_empty_references():
    assert funnel.parse_block_comment([
        "**Blocked:** Nate needs to decide whether this still matters.",
    ]) == ([], None, "Nate needs to decide whether this still matters.")


def test_latest_matching_block_comment_wins():
    assert funnel.parse_block_comment([
        "**Blocked on #84:** Older condition.",
        "An unrelated comment.",
        "**Blocked:** Newer decision needed.",
    ]) == ([], None, "Newer decision needed.")


def test_date_condition_returns_a_real_date_and_supports_issue_references():
    assert funnel.parse_block_comment([
        "**Blocked until 2026-09-19 on #84 and #90:** Wait for both.",
    ]) == (
        ["#84", "#90"], date(2026, 9, 19), "Wait for both."
    )


def test_invalid_calendar_date_is_not_a_parseable_block_header():
    body = "**Blocked until 2026-02-29:** The date is not real."

    assert funnel.parse_block_comment([body]) is None
    assert funnel.unparseable_block_comment_lines([body]) == [body]


def test_future_date_block_is_quiet_and_carries_date_in_the_brief(
    monkeypatch, capsys
):
    blocked_until = date.today() + timedelta(days=1)
    item = funnel.Item(
        repo="owner/repo", number=754, title="Future block", url="",
        state="OPEN", status="Ready", labels=["blocked"],
        block_reason="Wait for the release date.",
        blocked_until=blocked_until,
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])

    assert funnel.gate_question(item) is None
    assert funnel.cmd_brief(
        [item], datetime.now(timezone.utc)
    ) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["blocked"][0]["blocked_until"] == blocked_until.isoformat()
    assert brief["blocked"][0]["conditions"] == []


def test_passed_date_is_a_satisfied_condition_and_can_be_cleared(monkeypatch):
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    item = funnel.Item(
        repo="owner/repo", number=754, title="Passed block", url="",
        state="OPEN", labels=["blocked"],
        block_reason="Wait for the release date.",
        blocked_until=now.date(),
    )

    result = funnel.check_block_conditions([item], now=now)
    assert not result.ok
    assert result.found == (
        "owner/repo#754: satisfied block conditions: until 2026-09-14"
    )

    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    assert funnel.clear_satisfied_blocks(
        [item], now, run="run-date", agent="codex"
    ) == [{
        "ref": item.ref,
        "conditions": ["until 2026-09-14"],
        "cleared_at": now.isoformat(),
    }]
    assert calls[1][-2:] == ("--remove-label", "blocked")


def test_combined_date_and_issue_block_requires_both_conditions():
    blocked_until = date.today() - timedelta(days=1)
    parsed = funnel.parse_block_comment([
        "**Blocked until {} on #77:** Wait for both.".format(
            blocked_until.isoformat()
        ),
    ])
    references, parsed_until, reason = parsed
    waiting = funnel.Item(
        repo="owner/repo", number=754, title="Combined block", url="",
        state="OPEN", labels=["blocked"], block_references=references,
        block_reason=reason, blocked_until=parsed_until,
    )
    open_blocker = funnel.Item(
        repo="owner/repo", number=77, title="Open blocker", url="",
        state="OPEN",
    )
    closed_blocker = funnel.Item(
        repo="owner/repo", number=77, title="Closed blocker", url="",
        state="CLOSED",
    )

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, open_blocker.ref: open_blocker}
    ) is None
    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, closed_blocker.ref: closed_blocker}
    ) == ["until {}".format(blocked_until), "owner/repo#77"]


def test_malformed_date_block_remains_a_human_question():
    item = funnel.Item(
        repo="owner/repo", number=754, title="Malformed block", url="",
        state="OPEN", labels=["blocked"],
    )

    assert funnel.parse_block_comment([
        "**Blocked until 2026-02-29:** The date is not real.",
    ]) is None
    assert funnel.gate_question(item) == "Unblock or park?"


def test_unparseable_block_comment_returns_no_condition():
    assert funnel.parse_block_comment([
        "**Blocked on #84, 2026-09-07.** Legacy format.",
    ]) is None


def test_embedded_block_prefix_does_not_match():
    assert funnel.parse_block_comment([
        "A sentence before **Blocked on #84:** is not a header.",
    ]) is None


def test_needs_decision_comment_returns_the_latest_question():
    assert funnel.parse_needs_decision_comment([
        "**Needs a decision:** Older question.",
        "A regular follow-up.",
        "**Needs a decision:** Where should this connector live?",
    ]) == "Where should this connector live?"


def test_embedded_needs_decision_prefix_does_not_match():
    assert funnel.parse_needs_decision_comment([
        "A sentence before **Needs a decision:** is not a header.",
    ]) is None


def test_unparseable_block_comment_reports_its_first_line(monkeypatch):
    item = funnel.Item(
        repo="owner/repo", number=7, title="Broken comment", url="", state="OPEN",
        labels=["blocked"],
    )

    def gh_json(*args):
        return {"comments": [
            {"body": "**Blocked on #77, 2026-09-07.** Legacy format.\nMore detail."},
            {"body": "A regular follow-up."},
        ]}

    # The loader is the only remote seam; the doctor check remains pure over
    # the state it records on the Item.
    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    funnel._load_block_comment(item)

    assert funnel.check_block_comments([item]) == funnel.Check(
        "block comments", False,
        "owner/repo#7: **Blocked on #77, 2026-09-07.** Legacy format.",
        "",
    )


def test_canonical_block_comment_is_quiet_in_doctor():
    item = funnel.Item(
        repo="owner/repo", number=7, title="Good comment", url="", state="OPEN",
        labels=["blocked"],
        unparseable_block_comments=[],
    )

    assert funnel.check_block_comments([item]) == funnel.Check(
        "block comments", True, "", ""
    )


def test_comment_fetch_failure_is_reported_by_doctor(monkeypatch):
    item = funnel.Item(
        repo="owner/repo", number=7, title="Unreadable comments", url="", state="OPEN",
        labels=["blocked"],
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)

    funnel._load_block_comment(item)

    assert funnel.check_block_comments([item]) == funnel.Check(
        "block comments", False,
        "owner/repo#7: could not read comments", "",
    )


def test_comment_posts_a_canonical_single_block_header(monkeypatch):
    monkeypatch.setattr(funnel, "load_items", lambda: [comment_item()])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--blocked-on", "77", "--because", "waiting on X",
        "--voice", "agent", "--run", "run-block", "--agent", "codex",
    ]) == 0

    posted = calls[0][-1]
    assert posted.startswith("**Blocked on #77:** waiting on X\n\n")
    assert funnel.parse_block_comment([posted])[0] == ["#77"]


def test_comment_joins_multiple_block_references(monkeypatch):
    monkeypatch.setattr(funnel, "load_items", lambda: [comment_item()])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--blocked-on", "77", "--blocked-on", "78",
        "--because", "waiting on both", "--voice", "agent",
        "--run", "run-block", "--agent", "codex",
    ]) == 0

    posted = calls[0][-1]
    assert posted.startswith("**Blocked on #77 and #78:** waiting on both\n\n")
    assert funnel.parse_block_comment([posted])[0] == ["#77", "#78"]


def test_comment_posts_needs_decision_and_applies_blocked_label(monkeypatch):
    item = comment_item()
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--needs-decision", "Where should this live?",
        "--voice", "agent", "--run", "run-decision", "--agent", "codex",
    ]) == 0

    assert calls[0][:6] == (
        "gh", "issue", "comment", "42", "--repo", "nateprich/beta",
    )
    posted = calls[0][-1]
    assert posted.startswith(
        "**Needs a decision:** Where should this live?\n\n"
    )
    assert funnel.parse_needs_decision_comment([posted]) == "Where should this live?"
    assert calls[1] == (
        "gh", "issue", "edit", "42", "--repo", "nateprich/beta",
        "--add-label", "blocked",
    )
    assert item.is_blocked


@pytest.mark.parametrize(
    "extra",
    [
        ("--body", "another comment"),
        ("--blocked-on", "77"),
    ],
)
def test_needs_decision_is_mutually_exclusive_with_other_comment_forms(
    monkeypatch, extra
):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main([
            "comment", "42", "--needs-decision", "Where should this live?",
            extra[0], extra[1], "--voice", "agent",
        ])

    assert exc.value.code != 0


def test_blocked_comment_requires_because_before_loading_github(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(funnel, "load_items", lambda: called.append("loaded"))

    with pytest.raises(SystemExit) as exc:
        funnel.main([
            "comment", "42", "--blocked-on", "77", "--voice", "agent",
        ])

    assert exc.value.code != 0
    assert called == []
    assert "--because" in capsys.readouterr().err


def test_ordinary_body_comments_are_not_validated_as_block_comments(monkeypatch):
    monkeypatch.setattr(funnel, "load_items", lambda: [comment_item()])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--body", "**Blocked on #77, 2026-09-07.** legacy",
        "--voice", "agent", "--run", "run-body", "--agent", "codex",
    ]) == 0

    assert calls[0][-1].startswith(
        "**Blocked on #77, 2026-09-07.** legacy\n\n"
    )


def test_load_items_fetches_comments_only_for_open_blocked_items(monkeypatch):
    def node(number, repo="owner/repo", state="OPEN", labels=None):
        return {
            "status": {"name": "Ready"},
            "class": {"name": "New"},
            "content": {
                "number": number,
                "title": "issue {}".format(number),
                "url": "https://github.com/{}/issues/{}".format(repo, number),
                "state": state,
                "stateReason": None,
                "closedAt": None,
                "repository": {"nameWithOwner": repo},
                "labels": {"nodes": [{"name": label} for label in labels or []]},
                "assignees": {"nodes": []},
                "parent": None,
                "subIssuesSummary": {"total": 0, "completed": 0},
                "timelineItems": {"nodes": []},
            },
        }

    nodes = [
        node(1, labels=["blocked"]),
        node(2),
        node(3, state="CLOSED", labels=["blocked"]),
        node(4, repo="outside/repo", labels=["blocked"]),
    ]
    monkeypatch.setattr(funnel, "member_repos", lambda: ["owner/repo"])
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: {
            "user": {"projectV2": {"items": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}
        },
    )
    calls = []

    def gh_json(*args):
        calls.append(args)
        return {"comments": [
            {"body": "**Blocked on #84:** Wait for the decision."},
        ]}

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    items = funnel.load_items()

    assert [item.number for item in items] == [1, 2, 3]
    assert calls == [(
        "gh", "issue", "view", "1", "--repo", "owner/repo",
        "--json", "comments",
    )]
    blocked = items[0]
    assert blocked.block_references == ["#84"]
    assert blocked.block_reason == "Wait for the decision."
    assert blocked.needs_decision is None
    assert blocked.unparseable_block_comments == []
    assert blocked.block_comments_error is None
