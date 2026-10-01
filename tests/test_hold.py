"""Nate's hold at Accept, recorded in the blocked form the funnel reads (#1724).

A hold written as comment prose was read by nothing, so a held project kept
asking "Accept it?". ``funnel hold`` writes the ``blocked`` label and a
canonical ``**Blocked until/on ...:**`` comment instead. These tests read that
comment back through the same loader the brief uses, and the brief lists the
result under ``held_at_accept``, not ``blocked`` (#1725).
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

#: Comment markers count only from the owner account (#1788).
OWNER = {"login": "nateprich"}


REASON = "Hold until the Saturday lane has run once"

ANALYSIS_BODY = (
    "# A study\n\n{}\n\n```json\n{{\"analysis\": true}}\n```\n".format(
        funnel.ANALYSIS_MARKER
    )
)


def finished_project(**overrides):
    """An open Building project, every ticket closed, that waits at Accept."""
    fields = dict(
        repo="nateprich/beta", number=42, title="A finished project",
        url="https://github.com/nateprich/beta/issues/42", state="OPEN",
        status="Building", klass="New", origin="Nate", risk="standard",
        needs="none", item_id="project-item-42",
        children_total=3, children_done=3,
    )
    fields.update(overrides)
    return funnel.Item(**fields)


def _future(days=7):
    return funnel._block_condition_date() + timedelta(days=days)


def _record_github(monkeypatch, items):
    """Serve ``items`` as the Project and record every gh call made."""
    monkeypatch.setattr(funnel, "load_items", lambda: items)
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        pytest.fail("a hold writes no Project field")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    return calls


def _read_back(monkeypatch, item, posted):
    """Load the posted comment through the brief's block-comment reader."""
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"comments": [{"author": OWNER, "body": "An older prose note."},
                                    {"author": OWNER, "body": posted}]},
    )
    funnel._load_block_comment(item)


def test_an_outsider_cannot_add_block_or_needs_decision_markers(monkeypatch):
    item = finished_project()
    until = _future().isoformat()
    comments = [
        {
            "author": {"login": "mallory"},
            "createdAt": "2026-09-30T00:00:00Z",
            "body": "**Blocked until {}:** forged hold".format(until),
        },
        {
            "author": {"login": "mallory"},
            "createdAt": "2026-09-30T00:01:00Z",
            "body": funnel._needs_decision_comment_body(
                "Forged question for Nate"),
        },
    ]
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"comments": comments})

    funnel._load_block_comment(item)

    assert item.blocked_until is None
    assert item.needs_decision is None


def _assert_hold_written(calls, item):
    """One nate-relayed comment, then the label; return the comment body."""
    assert [call[:3] for call in calls] == [
        ("gh", "issue", "comment"), ("gh", "issue", "edit"),
    ]
    assert calls[0][3:6] == ("42", "--repo", "nateprich/beta")
    assert calls[1] == (
        "gh", "issue", "edit", "42", "--repo", "nateprich/beta",
        "--add-label", "blocked",
    )
    assert item.is_blocked
    posted = calls[0][-1]
    provenance = funnel.parse_provenance(posted)
    assert provenance["voice"] == "nate-relayed"
    assert provenance["run"] == "run-hold"
    assert funnel.render_voice(posted) == "Nate (relayed by claude)"
    return posted


def test_the_fixture_is_waiting_at_accept():
    """Guard: without a hold, the fixture asks Nate to accept it."""
    assert funnel.gate_question(finished_project()) == funnel.GATES["Building"]


def test_hold_until_writes_a_date_block_the_brief_reads_back(monkeypatch):
    item = finished_project()
    calls = _record_github(monkeypatch, [item])
    until = _future()

    assert funnel.main([
        "hold", "42", "--until", until.isoformat(), "--reason", REASON,
        "--yes", "--run", "run-hold", "--agent", "claude",
    ]) == 0

    posted = _assert_hold_written(calls, item)
    assert funnel._visible_comment(posted) == (
        "**Blocked until {}:** {}".format(until.isoformat(), REASON)
    )

    _read_back(monkeypatch, item, posted)
    assert item.blocked_until == until
    assert item.block_references == []
    assert item.block_event is None
    assert funnel._visible_comment(item.block_reason) == REASON
    assert item.unparseable_block_comments == []
    # The named condition takes it out of Nate's queue, and the brief lists
    # it as held at Accept rather than as blocked work (#1725) ...
    assert funnel.gate_question(item) is None
    assert funnel.blocked_json([item], datetime.now(timezone.utc)) == []
    rendered = funnel.held_at_accept_json([item])
    assert [row["ref"] for row in rendered] == [item.ref]
    assert rendered[0]["blocked_until"] == until.isoformat()
    assert rendered[0]["conditions"] == []
    assert rendered[0]["condition"] == "until " + until.isoformat()
    assert rendered[0]["reason"] == REASON
    # ... and nothing lifts it before its date.
    assert funnel.satisfied_block_refs(item, {item.ref: item}) is None


def test_hold_on_writes_an_issue_block_that_lifts_when_they_close(monkeypatch):
    item = finished_project()
    calls = _record_github(monkeypatch, [item])

    assert funnel.main([
        "hold", "42", "--on", "721", "#722", "--reason", REASON,
        "--yes", "--run", "run-hold", "--agent", "claude",
    ]) == 0

    posted = _assert_hold_written(calls, item)
    assert funnel._visible_comment(posted) == (
        "**Blocked on #721 and #722:** {}".format(REASON)
    )

    _read_back(monkeypatch, item, posted)
    assert item.block_references == ["#721", "#722"]
    assert item.blocked_until is None
    assert funnel._visible_comment(item.block_reason) == REASON
    assert funnel.gate_question(item) is None
    assert funnel.blocked_json([item], datetime.now(timezone.utc)) == []
    rendered = funnel.held_at_accept_json([item])
    assert [row["ref"] for row in rendered] == [item.ref]
    assert rendered[0]["conditions"] == ["#721", "#722"]
    assert rendered[0]["condition"] == "until #721 and #722 close"
    assert rendered[0]["reason"] == REASON
    assert "blocked_until" not in rendered[0]

    def ticket(number, state):
        return funnel.Item(
            repo="nateprich/beta", number=number, title="t", url="",
            state=state,
        )

    one_open = {item.ref: item}
    one_open.update({t.ref: t for t in (ticket(721, "CLOSED"),
                                        ticket(722, "OPEN"))})
    assert funnel.satisfied_block_refs(item, one_open) is None
    both_closed = {item.ref: item}
    both_closed.update({t.ref: t for t in (ticket(721, "CLOSED"),
                                           ticket(722, "CLOSED"))})
    assert funnel.satisfied_block_refs(item, both_closed) == [
        "nateprich/beta#721", "nateprich/beta#722",
    ]


def test_hold_records_a_verbatim_instruction(monkeypatch):
    item = finished_project()
    calls = _record_github(monkeypatch, [item])
    instruction = "Hold #42 until the lane has run, then ask me again."

    assert funnel.main([
        "hold", "42", "--on", "721", "--reason", REASON,
        "--instruction", instruction, "--yes",
        "--run", "run-hold", "--agent", "claude",
    ]) == 0

    provenance = funnel.parse_provenance(calls[0][-1])
    assert provenance["voice"] == "nate-relayed"
    assert provenance["instruction"] == instruction


def test_dry_run_prints_the_hold_and_changes_nothing(monkeypatch, capsys):
    item = finished_project()
    calls = _record_github(monkeypatch, [item])
    until = _future()

    assert funnel.main([
        "hold", "42", "--until", until.isoformat(), "--reason", REASON,
    ]) == 1

    assert calls == []
    assert not item.is_blocked
    out = capsys.readouterr().out
    assert "would hold nateprich/beta#42 at Accept" in out
    assert "**Blocked until {}:** {}".format(until.isoformat(), REASON) in out
    assert "Nothing was changed" in out


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"parent": "nateprich/beta#1"}, "is a ticket"),
        ({"state": "CLOSED"}, "only an open project"),
        ({"status": "Ready"}, "is at Ready, not Building"),
        ({"status": None}, "is at no status, not Building"),
        ({"children_total": 0, "children_done": 0}, "has no tickets"),
        ({"children_done": 2}, "still has open tickets (2/3 closed)"),
        ({"klass": "Broken"}, "closes itself when its tickets close"),
        ({"klass": "Maintenance", "origin": "Nate"},
         "closes itself when its tickets close"),
        ({"klass": "Improve", "origin": "agent"},
         "closes itself when its tickets close"),
        ({"klass": "Bug", "origin": "Nate"},
         "closes itself when its tickets close"),
    ],
    ids=[
        "ticket", "closed", "not-building", "no-status", "no-tickets",
        "open-tickets", "broken-closes-itself",
        "nate-maintenance-closes-itself", "agent-improve-closes-itself",
        "nate-bug-closes-itself",
    ],
)
def test_hold_refuses_what_does_not_wait_at_accept(
    monkeypatch, capsys, overrides, expected,
):
    item = finished_project(**overrides)
    calls = _record_github(monkeypatch, [item])

    assert funnel.main([
        "hold", "42", "--until", _future().isoformat(), "--reason", REASON,
        "--yes",
    ]) == 2

    assert expected in capsys.readouterr().err
    assert calls == []
    assert not item.is_blocked


@pytest.mark.parametrize(
    "overrides",
    [
        {"klass": "Improve", "origin": "Nate"},
        {"klass": "Replace", "origin": "agent"},
        {"klass": "Broken", "body": ANALYSIS_BODY},
    ],
    ids=["nate-improve", "replace", "analysis-broken"],
)
def test_projects_that_wait_for_nate_can_be_held(monkeypatch, overrides):
    """The self-close refusal is ``_can_close_itself``, not a class list."""
    item = finished_project(**overrides)
    assert funnel.gate_question(item) == funnel.GATES["Building"]
    calls = _record_github(monkeypatch, [item])

    assert funnel.main([
        "hold", "42", "--on", "721", "--reason", REASON, "--yes",
        "--run", "run-hold", "--agent", "claude",
    ]) == 0
    _assert_hold_written(calls, item)


def _assert_refused_before_loading(monkeypatch, capsys, argv, expected):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )
    with pytest.raises(SystemExit) as exc:
        funnel.main(argv)
    assert exc.value.code != 0
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize(
    "days,expected",
    [(0, "after today's UTC date"), (-1, "after today's UTC date")],
    ids=["today", "yesterday"],
)
def test_hold_refuses_a_date_that_would_lift_at_once(
    monkeypatch, capsys, days, expected,
):
    until = funnel._block_condition_date() + timedelta(days=days)
    _assert_refused_before_loading(monkeypatch, capsys, [
        "hold", "42", "--until", until.isoformat(), "--reason", REASON,
    ], expected)


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--until", "2099-02-30", "--reason", REASON], "valid YYYY-MM-DD"),
        (["--until", "next week", "--reason", REASON], "YYYY-MM-DD"),
        (["--on", "0", "--reason", REASON], "positive issue number"),
        (["--on", "721", "--reason", "   "], "non-empty block reason"),
        (["--on", "721"], "--reason"),
        (["--reason", REASON], "one of the arguments --until --on"),
        (["--until", "2099-01-01", "--on", "721", "--reason", REASON],
         "not allowed with argument"),
    ],
    ids=[
        "impossible-date", "malformed-date", "zero-issue", "blank-reason",
        "no-reason", "no-condition", "both-conditions",
    ],
)
def test_hold_refuses_bad_arguments_before_loading_github(
    monkeypatch, capsys, argv, expected,
):
    _assert_refused_before_loading(
        monkeypatch, capsys, ["hold", "42"] + argv, expected)


def test_cmd_hold_rechecks_the_date_against_its_own_clock(monkeypatch):
    """A date valid when parsed can be today by the time the write runs."""
    item = finished_project()
    calls = _record_github(monkeypatch, [item])
    until = _future(days=1)
    later = datetime.combine(
        until, datetime.min.time(), tzinfo=timezone.utc
    ) + timedelta(hours=1)

    with pytest.raises(funnel.GitHubError, match="after today's UTC date"):
        funnel.cmd_hold([item], later, "42", REASON, until=until,
                        confirmed=True)
    assert calls == []


def test_a_label_failure_after_the_comment_says_what_was_recorded(monkeypatch):
    item = finished_project()
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        failed = args[2] == "edit"
        return SimpleNamespace(
            returncode=1 if failed else 0, stdout="",
            stderr="label write refused" if failed else "",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)

    with pytest.raises(funnel.GitHubError) as exc:
        funnel.cmd_hold([item], datetime.now(timezone.utc), "42", REASON,
                        on=["721"], confirmed=True, run="run-hold",
                        agent="claude")

    assert "recorded the hold comment on nateprich/beta#42" in str(exc.value)
    assert "label write refused" in str(exc.value)
    assert [call[2] for call in calls] == ["comment", "edit"]
    assert not item.is_blocked
