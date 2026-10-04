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

import block_record  # noqa: E402
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
    assert calls[0][3:6] == (str(item.number), "--repo", item.repo)
    assert calls[1] == (
        "gh", "issue", "edit", str(item.number), "--repo", item.repo,
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


# -- Shaped hold write reproduction ------------------------------------------

SHAPED_PLAN_BODY = "# Decision\nKeep current service plan.\n"
SHAPED_PLAN_VERSION = (
    "371883196b99396eca7d9b9cb6613ec0395df5ca683d36fe7415b71343e40dbb"
)
SHAPED_CONDITIONS = ("1590", "1997", "1998", "1999", "2000")
SHAPED_PROOF = (
    "https://github.com/nateprich-projects/command-center/issues/2003"
    "#issuecomment-5945296610"
)


def shaped_project(**overrides):
    fields = dict(
        repo="nateprich-projects/command-center", number=2003,
        title="A Shaped project",
        url="https://github.com/nateprich-projects/command-center/issues/2003",
        state="OPEN",
        body=SHAPED_PLAN_BODY, status="Shaped", klass="New", origin="Nate",
        needs="none", item_id="project-item-2003",
    )
    fields.update(overrides)
    return funnel.Item(**fields)


def test_reproduction_owner_authorized_shaped_hold_writes_block_and_header(
        monkeypatch):
    item = shaped_project()
    calls = _record_github(monkeypatch, [item])
    project_writes = []
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: project_writes.append(
            (item_id, field, value, ref)),
    )
    args = ["hold", "2003", "--on"] + list(SHAPED_CONDITIONS)
    args.extend([
        "--reason", "Hold until the open prerequisites clear",
        "--proof", SHAPED_PROOF,
        "--yes", "--run", "run-hold", "--agent", "claude",
        "--instruction", "Nate asked to hold this Shaped plan",
    ])

    assert funnel.main(args) == 0

    posted = _assert_hold_written(calls, item)
    visible = funnel._visible_comment(posted)
    # The #2003 fixture keeps #1590 and #1997-#2000; stale closed #2120 is
    # not a live condition.
    expected_hold = {
        "Hold-Reason": "Hold until the open prerequisites clear",
        "Hold-Conditions": [
            "nateprich-projects/command-center#1590",
            "nateprich-projects/command-center#1997",
            "nateprich-projects/command-center#1998",
            "nateprich-projects/command-center#1999",
            "nateprich-projects/command-center#2000",
        ],
        "Plan-Version": SHAPED_PLAN_VERSION,
        "Proof": [SHAPED_PROOF],
    }
    assert "nateprich-projects/command-center#2120" not in (
        expected_hold["Hold-Conditions"])
    assert block_record.parse_shaped_hold_comment(visible) == expected_hold
    assert project_writes == [
        ("project-item-2003", "Needs", "external-event", item.ref),
    ]
    assert item.needs == "external-event"
    assert item.status == "Shaped"
    assert item.klass == "New"
    assert "approval" not in visible.lower()
    _read_back(monkeypatch, item, posted)
    assert item.shaped_hold == expected_hold
    assert item.block_references == []
    assert item.block_reason == expected_hold["Hold-Reason"]


def test_shaped_hold_refusal_allows_open_owner_project():
    item = shaped_project()

    assert funnel._hold_refusal(item) is None


def _load_live_shaped_hold(monkeypatch, item, *, condition_states=None,
                           proof_author="nateprich", extra_comments=()):
    record = {
        "Hold-Reason": "Wait for the named prerequisites",
        "Hold-Conditions": [
            "{}#{}".format(item.repo, number)
            for number in SHAPED_CONDITIONS
        ],
        "Plan-Version": SHAPED_PLAN_VERSION,
        "Proof": [SHAPED_PROOF],
    }
    hold_body = block_record.render_shaped_hold(
        record["Hold-Reason"], record["Hold-Conditions"],
        record["Plan-Version"], record["Proof"],
    )
    comments = [
        {
            "author": OWNER,
            "createdAt": "2026-10-01T00:00:00Z",
            "body": hold_body,
        },
        *extra_comments,
    ]
    live_states = {item.number: "OPEN"}
    live_states.update({
        int(number): state
        for number, state in (condition_states or {}).items()
    })

    def gh_json(*args):
        command = list(args)
        if "--json" in command:
            requested = command[command.index("--json") + 1]
            if requested == "comments":
                return {"comments": list(comments)}
            if requested == "state":
                issue_number = int(command[command.index("view") + 1])
                return {"state": live_states.get(issue_number)}
            if requested == "body":
                return {"body": SHAPED_PLAN_BODY}
            if requested == "state,body,comments,labels":
                return {
                    "state": "OPEN",
                    "body": SHAPED_PLAN_BODY,
                    "comments": list(comments),
                    "labels": [{"name": label} for label in item.labels],
                }
        return None

    monkeypatch.setattr(funnel, "_gh_json", gh_json)
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint, **kwargs: {
            "id": int(SHAPED_PROOF.rsplit("issuecomment-", 1)[1]),
            "html_url": SHAPED_PROOF,
            "user": {"login": proof_author},
        },
    )
    funnel._load_block_comment(item)
    item.labels = ["blocked"]
    item.needs = "external-event"
    return record, comments, live_states


def test_live_satisfied_shaped_hold_clears_once_and_reasks_plan_gate(
        monkeypatch):
    item = shaped_project(labels=["blocked"], needs="external-event")
    all_closed = {number: "CLOSED" for number in SHAPED_CONDITIONS}
    record, _comments, _states = _load_live_shaped_hold(
        monkeypatch, item, condition_states=all_closed)
    calls = []
    project_writes = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: project_writes.append(
            (item_id, field, value, ref)),
    )

    cleared = funnel.clear_satisfied_blocks(
        [item], datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
        run="run-clear", agent="codex",
    )

    assert [row["kind"] for row in cleared] == ["shaped-hold"]
    assert cleared[0]["conditions"] == record["Hold-Conditions"]
    assert cleared[0]["proof"] == [SHAPED_PROOF]
    assert not item.is_blocked
    assert item.needs == "none"
    assert project_writes == [
        (item.item_id, "Needs", "none", item.ref),
    ]
    assert [call[2] for call in calls] == ["comment", "edit"]
    visible = funnel._visible_comment(calls[0][-1])
    assert visible.startswith(block_record.SHAPED_HOLD_CLEAR_PREFIX)
    assert SHAPED_PROOF in visible
    assert visible.count(SHAPED_PLAN_VERSION) == 1
    assert funnel.parse_provenance(calls[0][-1])["run"] == "run-clear"
    assert funnel.gate_question(item) == "Is the plan good?"


def test_stale_closed_issue_does_not_clear_open_current_shaped_conditions(
        monkeypatch):
    item = shaped_project(labels=["blocked"], needs="external-event")
    states = {2120: "CLOSED"}
    states.update({number: "OPEN" for number in SHAPED_CONDITIONS})
    _record, _comments, _live_states = _load_live_shaped_hold(
        monkeypatch, item, condition_states=states)
    calls = _record_github(monkeypatch, [item])
    monkeypatch.setattr(
        funnel, "live_issue_state",
        lambda issue: states.get(issue.number),
    )
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: item.body)
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint, **kwargs: {
            "id": 5945296610, "html_url": SHAPED_PROOF,
            "user": OWNER,
        },
    )

    assert funnel.clear_satisfied_blocks(
        [item], datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
    ) == []
    assert calls == []
    assert item.is_blocked
    assert item.needs == "external-event"


def test_recorded_shaped_clear_recovers_without_posting_it_twice(monkeypatch):
    item = shaped_project(labels=["blocked"], needs="external-event")
    all_closed = {int(number): "CLOSED" for number in SHAPED_CONDITIONS}
    _record, comments, _states = _load_live_shaped_hold(
        monkeypatch, item, condition_states=all_closed)
    calls = []
    fail_label_write = [True]
    project_writes = []

    def run(args, capture_output, text=True):
        call = tuple(args)
        calls.append(call)
        if call[2] == "comment":
            comments.append({
                "author": OWNER,
                "createdAt": "2026-10-01T00:01:00Z",
                "body": call[-1],
            })
        if call[2] == "edit" and "--remove-label" in call and fail_label_write[0]:
            fail_label_write[0] = False
            return SimpleNamespace(
                returncode=1, stdout="", stderr="temporary label-write failure",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: project_writes.append(
            (item_id, field, value, ref)),
    )
    now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(funnel.GitHubError, match="could not remove its blocked label"):
        funnel.clear_satisfied_blocks([item], now, run="run-clear", agent="codex")

    assert item.is_blocked
    assert item.needs == "none"
    assert sum(call[2] == "comment" for call in calls) == 1
    funnel._load_block_comment(item)
    cleared = funnel.clear_satisfied_blocks([item], now, run="run-retry", agent="codex")

    assert len(cleared) == 1
    assert cleared[0]["clear"] == "conditioned"
    assert not item.is_blocked
    assert sum(call[2] == "comment" for call in calls) == 1
    assert project_writes == [(item.item_id, "Needs", "none", item.ref)]


def test_closed_conditions_do_not_clear_without_owner_authored_live_proof(
        monkeypatch):
    item = shaped_project(labels=["blocked"], needs="external-event")
    all_closed = {int(number): "CLOSED" for number in SHAPED_CONDITIONS}
    _load_live_shaped_hold(
        monkeypatch, item, condition_states=all_closed,
        proof_author="mallory",
    )
    calls = _record_github(monkeypatch, [item])
    monkeypatch.setattr(
        funnel, "live_issue_state",
        lambda issue: "CLOSED" if issue.number in all_closed else "OPEN",
    )
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: item.body)
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint, **kwargs: {
            "id": 5945296610, "html_url": SHAPED_PROOF,
            "user": {"login": "mallory"},
        },
    )

    assert funnel.clear_satisfied_blocks(
        [item], datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
    ) == []
    assert calls == []
    assert item.is_blocked


def test_explicit_release_ignores_conditions_and_proof_but_reasks_plan_gate(
        monkeypatch):
    item = shaped_project(labels=["blocked"], needs="external-event")
    _record, _comments, _states = _load_live_shaped_hold(
        monkeypatch, item,
        condition_states={int(number): "OPEN" for number in SHAPED_CONDITIONS},
    )
    calls = _record_github(monkeypatch, [item])
    project_writes = []
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint, **kwargs: pytest.fail(
            "explicit release does not recheck proof URLs"),
    )
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: project_writes.append(
            (item_id, field, value, ref)),
    )
    instruction = "Release the hold now and show me the current plan again."

    assert funnel.main([
        "hold", "2003", "--release", "--yes", "--instruction", instruction,
        "--run", "run-release", "--agent", "codex",
    ], _items=[item]) == 0

    visible = funnel._visible_comment(calls[0][-1])
    assert visible.startswith(block_record.SHAPED_HOLD_RELEASE_PREFIX)
    assert "does not approve the plan" in visible
    assert visible.count(SHAPED_PLAN_VERSION) == 1
    provenance = funnel.parse_provenance(calls[0][-1])
    assert provenance["voice"] == "nate-relayed"
    assert provenance["instruction"] == instruction
    assert [call[2] for call in calls] == ["comment", "edit"]
    assert project_writes == [(item.item_id, "Needs", "none", item.ref)]
    assert not item.is_blocked
    assert item.status == "Shaped"
    assert item.klass == "New"
    assert funnel.gate_question(item) == "Is the plan good?"


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
