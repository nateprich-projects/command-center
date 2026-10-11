"""The atomic ticket-decision answer command (#2484, ticket #2495).

On 2026-10-10 Fantasy-GM#154 kept its blocked label and its Needs-a-decision
block after a decision-review session answered in prose and enabled the
requested change: prose is not a record any reader understands, so the
question stayed open until the block was noticed and hand-edited. There was
no funnel command answering a ticket block — ``cmd_answer_gates`` answers a
plan Gates question and refuses tickets — so prose plus hand label edits was
the only path, and it left the pair behind.

Verify-first (base origin/main 3623c8acf2811e49f81d984d0d2200af3e27799b,
checked before implementing; implementation was contingent on both):
- funnel.py has no ticket answer path on base: ``git grep -n
  "answer-ticket|answer_ticket|cmd_answer_ticket|parse_ticket_answer|render_ticket_answer|TICKET_ANSWER"
  <base> --`` returns no lines.
- ``cmd_answer_gates`` is plan-Gates-only at funnel.py:18998 on base: it
  refuses tickets via ``if item.parent is not None`` ("the Gates question
  belongs to its project"), so a Needs-a-decision block on a sub-issue has
  no answer path.
- The brief and the gates already parse the three fixed replacement
  prefixes through the ordinary block reader: block_record.py:31
  (``BLOCK_COMMENT_PREFIX = "**Blocked"``), block_record.py:71
  (``BLOCK_EVENT_COMMENT_PREFIX = "**Blocked until event:**"``),
  funnel.py:1566 ``block_condition`` mapping date/reference/event, and
  funnel.py:1743 ``gate_question``. Observed on this checkout:
  ``_parse_block_comment_details(["**Blocked until 2026-10-12:** wait."])``
  reads ``([], 2026-10-12, 'wait.', None)``; ``(["**Blocked on #12:**
  wait."])`` reads ``(['#12'], None, 'wait.', None)``; the ``**Blocked
  until event:**`` JSON-spec form reads ``([], None, 'wait.', {agent, job,
  outcome, after})``; ``block_condition`` maps them to date/reference/event.
  Fixture-shaped evidence on base: tests/test_block.py:48 (``**Blocked on
  #84 and #90:**``), tests/test_block.py:75-area (``**Blocked until
  YYYY-MM-DD:**``), tests/test_block.py:33 (``FF_WEEKLY_EVENT_COMMENT``
  ``**Blocked until event:**``).

``answer-ticket`` is the sole ticket path now: one comment quotes Nate's
verbatim answer and supersedes the active question, then a lift removes the
blocked label while a replace keeps it under exactly one canonical
replacement condition. Expected values below are written out from the
ticket, not read from the code under test.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

#: Comment markers count only from the owner account (#1788).
OWNER = {"login": "nateprich"}
REPO = "nateprich-projects/Fantasy-GM"
REF = REPO + "#154"
NOW = datetime(2026, 10, 11, 14, 0, tzinfo=timezone.utc)

#: Fantasy-GM#154's shape: the funnel watch's Needs-a-decision post, then a
#: prose answer that enabled the change without lifting anything.
QUESTION = "Should bidding be enabled for week 7?"
NEEDS_DECISION = "**Needs a decision:** " + QUESTION
PROSE_ANSWER = "Sounds good — bidding is enabled."
ANSWER = "Yes, enable bidding for week 7."

EVENT_CONDITION = (
    'Blocked until event {"agent": "codex", '
    '"job": "command-center-tickets-hourly", '
    '"outcome": "errored", "after": "2026-10-12T00:00:00Z"}'
)


def row(body, at=None, author=OWNER):
    comment = {"author": author, "body": body}
    if at is not None:
        comment["createdAt"] = at
    return comment


def ticket_154():
    return funnel.Item(
        repo=REPO, number=154, title="Bidding stays disabled",
        url="https://github.com/{}/issues/154".format(REPO),
        state="OPEN", parent=REPO + "#100", risk="standard", needs="human",
        labels=["blocked"], item_id="project-item-154",
    )


def load(monkeypatch, item, comments):
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"comments": comments})
    funnel._load_block_comment(item)
    return item


def blocked_ticket(monkeypatch):
    """A Fantasy-GM#154-shaped ticket: label plus active question."""
    return load(monkeypatch, ticket_154(), [
        row(NEEDS_DECISION, "2026-10-10T11:55:00Z"),
        row(PROSE_ANSWER, "2026-10-10T15:24:00Z"),
    ])


def record_gh(monkeypatch, fail=None):
    """Capture gh calls; answer-ticket writes no Project field."""
    calls = []

    def run_gh(args, **kwargs):
        calls.append(list(args))
        if fail is not None and fail(list(args)):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="boom")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def no_project_write(*args):
        pytest.fail(
            "answer-ticket writes no Project field, got {!r}".format(args))

    monkeypatch.setattr(funnel, "_run_gh", run_gh)
    monkeypatch.setattr(funnel, "write_project_select", no_project_write)
    return calls


def posted_body(calls):
    comments = [call for call in calls if call[:3] == ["gh", "issue", "comment"]]
    assert len(comments) == 1
    return comments[0][comments[0].index("--body") + 1]


def reload_with_answer(monkeypatch, item, body):
    load(monkeypatch, item, [
        row(NEEDS_DECISION, "2026-10-10T11:55:00Z"),
        row(PROSE_ANSWER, "2026-10-10T15:24:00Z"),
        row(body, "2026-10-11T14:00:00Z"),
    ])
    return item


# -- the observed failure: prose answers nothing ----------------------------

def test_prose_answer_leaves_the_block_in_place(monkeypatch):
    """The Fantasy-GM#154 mechanism: a prose answer is not a record, so the
    question and the label stay until something else moves them."""
    item = blocked_ticket(monkeypatch)

    assert item.needs_decision == QUESTION
    assert item.is_blocked
    assert funnel.gate_question(item) == QUESTION


# -- lift: one comment plus the label removal, nothing else ------------------

def test_lift_leaves_no_stale_block_or_label(monkeypatch, capsys):
    """Accept clause 1: answering through the command leaves no stale
    Needs-a-decision block and no stale blocked marker."""
    item = blocked_ticket(monkeypatch)
    calls = record_gh(monkeypatch)

    assert funnel.cmd_answer_ticket(
        [item], NOW, REF, ANSWER, "lift", None,
        run="run-2495", agent="muse") == 0

    comments = [call for call in calls if call[:3] == ["gh", "issue", "comment"]]
    edits = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
    assert len(comments) == 1
    assert len(edits) == 1
    assert len(calls) == 2
    assert edits[0][edits[0].index("--remove-label") + 1] == "blocked"

    body = posted_body(calls)
    assert body.startswith("> " + ANSWER.splitlines()[0])
    assert ANSWER in body
    assert "**Answered:**" in body
    assert "**Blocked" not in body

    assert "blocked" not in item.labels
    assert item.needs_decision is None
    assert REF in capsys.readouterr().out

    reloaded = reload_with_answer(monkeypatch, blocked_ticket(monkeypatch), body)
    reloaded.labels = [label for label in reloaded.labels if label != "blocked"]
    assert reloaded.needs_decision is None
    assert funnel.block_condition(reloaded) is None
    assert funnel.gate_question(reloaded) is None


def test_lift_writes_label_and_block_together(monkeypatch):
    """Accept clause 2: the label and the block change in the same command
    run — the command never performs one half without the other."""
    item = blocked_ticket(monkeypatch)
    calls = record_gh(monkeypatch)

    funnel.cmd_answer_ticket([item], NOW, REF, ANSWER, "lift", None)

    kinds = [tuple(call[:3]) for call in calls]
    assert kinds == [
        ("gh", "issue", "comment"),
        ("gh", "issue", "edit"),
    ]


def test_a_refused_post_changes_nothing(monkeypatch):
    """The comment goes first, so a refused post never reaches the label."""
    item = blocked_ticket(monkeypatch)
    calls = record_gh(
        monkeypatch, fail=lambda args: args[:3] == ["gh", "issue", "comment"])

    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket([item], NOW, REF, ANSWER, "lift", None)

    assert len(calls) == 1
    assert item.is_blocked
    assert item.needs_decision == QUESTION


def test_a_refused_label_edit_reports_the_recorded_answer(monkeypatch):
    item = blocked_ticket(monkeypatch)
    calls = record_gh(
        monkeypatch, fail=lambda args: args[:3] == ["gh", "issue", "edit"])

    with pytest.raises(funnel.GitHubError) as failed:
        funnel.cmd_answer_ticket([item], NOW, REF, ANSWER, "lift", None)

    assert "recorded the answer" in str(failed.value)
    assert len(calls) == 2


# -- replace: the same answer plus one canonical condition -------------------

REPLACEMENTS = [
    pytest.param(
        "Blocked until 2026-10-12", "**Blocked until 2026-10-12:**", "date",
        id="until-date",
    ),
    pytest.param(
        "Blocked on #12", "**Blocked on #12:**", "reference",
        id="on-issue",
    ),
    pytest.param(
        EVENT_CONDITION, "**Blocked until event:**", "event",
        id="on-event",
    ),
]


@pytest.mark.parametrize("condition,header,expected", REPLACEMENTS)
def test_replace_keeps_the_label_under_one_canonical_condition(
    monkeypatch, condition, header, expected,
):
    """A replace supersedes the question and establishes exactly one
    condition the brief and the gates parse."""
    item = blocked_ticket(monkeypatch)
    calls = record_gh(monkeypatch)

    assert funnel.cmd_answer_ticket(
        [item], NOW, REF, ANSWER, "replace",
        funnel._ticket_answer_condition(condition)) == 0

    body = posted_body(calls)
    assert body.startswith("> " + ANSWER.splitlines()[0])
    assert "**Answered:**" in body
    assert header in body
    assert body.count("**Blocked") == 1

    edits = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
    assert len(calls) == 2
    assert "--add-label" in edits[0]
    assert item.is_blocked
    assert item.needs_decision is None

    reloaded = reload_with_answer(monkeypatch, blocked_ticket(monkeypatch), body)
    assert reloaded.needs_decision is None
    assert funnel.block_condition(reloaded) == expected
    assert funnel.gate_question(reloaded) is None


# -- non-canonical replacements never reach GitHub ----------------------------

NON_CANONICAL = [
    "next Friday",
    "until 2026-10-12",
    "Blocked when ready",
    "Blocked on event",
    "Blocked on #0",
    "Blocked on #abc",
    "Blocked on #12 and #abc",
    "Blocked until 2026-13-01",
    "Blocked until 2026-02-30",
    "Blocked until event {}",
    "Blocked until event not-json",
    "Blocked until event {}".format('{"agent": "codex"}'),
]


@pytest.mark.parametrize("condition", NON_CANONICAL)
def test_non_canonical_replacements_are_rejected_before_github(condition):
    """Accept clause 3: fixed prefixes only — prose, bare dates, unknown
    prefixes and malformed specs are refused during parsing."""
    with pytest.raises(argparse.ArgumentTypeError):
        funnel._ticket_answer_condition(condition)


def test_replace_without_a_condition_never_reaches_github(monkeypatch):
    item = blocked_ticket(monkeypatch)
    calls = record_gh(monkeypatch)

    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket([item], NOW, REF, ANSWER, "replace", None)

    assert calls == []


def test_lift_with_a_condition_never_reaches_github(monkeypatch):
    item = blocked_ticket(monkeypatch)
    calls = record_gh(monkeypatch)

    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket(
            [item], NOW, REF, ANSWER, "lift",
            funnel._ticket_answer_condition("Blocked until 2026-10-12"))

    assert calls == []


# -- the command refuses anything outside its one blocked state --------------

def _refusal_cases():
    base = ticket_154()
    closed = ticket_154()
    closed.state = "CLOSED"
    project = funnel.Item(
        repo=REPO, number=100, title="Project", url="",
        state="OPEN", labels=["blocked"], item_id="project-item-100",
        needs_decision=QUESTION,
    )
    no_project_row = ticket_154()
    no_project_row.item_id = None
    unblocked = ticket_154()
    unblocked.labels = []
    unblocked.needs_decision = QUESTION
    return closed, project, no_project_row, unblocked


def test_answer_ticket_refusals_change_nothing(monkeypatch):
    closed, project, no_project_row, unblocked = _refusal_cases()
    calls = record_gh(monkeypatch)

    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket(
            [closed], NOW, REPO + "#154", ANSWER, "lift", None)
    with pytest.raises(funnel.GitHubError) as project_refusal:
        funnel.cmd_answer_ticket(
            [project], NOW, REPO + "#100", ANSWER, "lift", None)
    assert "answer-gates" in str(project_refusal.value)
    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket(
            [no_project_row], NOW, REPO + "#154", ANSWER, "lift", None)
    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket(
            [unblocked], NOW, REPO + "#154", ANSWER, "lift", None)
    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket([ticket_154()], NOW, REF, ANSWER, "lift", None)
    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket(
            [blocked_ticket(monkeypatch)], NOW, REF, "   ", "lift", None)
    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_ticket(
            [blocked_ticket(monkeypatch)], NOW, REF, ANSWER, "drop", None)

    assert calls == []


# -- the quote cannot smuggle in a second record -------------------------------

HEADER_SHAPED_ANSWER = "**Blocked on #99:** wrong.\n<!-- command-center-x -->"


def test_a_header_shaped_answer_stays_a_quote(monkeypatch):
    """Nate's verbatim may itself name a block: blockquoted, it parses as
    text, so a lift still lifts and establishes no condition."""
    item = blocked_ticket(monkeypatch)
    calls = record_gh(monkeypatch)

    funnel.cmd_answer_ticket(
        [item], NOW, REF, HEADER_SHAPED_ANSWER, "lift", None)

    body = posted_body(calls)
    assert "> **Blocked on #99:** wrong." in body
    assert "&lt;!--" in body
    assert funnel.parse_ticket_answer(body) == ([], None, None, None)

    reloaded = reload_with_answer(monkeypatch, blocked_ticket(monkeypatch), body)
    reloaded.labels = [label for label in reloaded.labels if label != "blocked"]
    assert reloaded.needs_decision is None
    assert reloaded.block_references == []
    assert funnel.block_condition(reloaded) is None


# -- verify-first premises (base origin/main; pre-existing code) ------------

def test_verify_no_ticket_answer_path_and_gates_refuses_tickets(monkeypatch):
    """Verify-first premise 1 (funnel.py:18998 on base): no ticket answer
    path existed and ``cmd_answer_gates`` answers plan Gates only — it
    refuses a ticket, so a Needs-a-decision block on a sub-issue had no
    command to answer it. (The no-ticket-path half is a base-tree grep fact
    recorded in the module docstring, not assertable from this branch.)"""
    item = blocked_ticket(monkeypatch)
    with pytest.raises(funnel.GitHubError) as refused:
        funnel.cmd_answer_gates([item], NOW, REF, ANSWER, "nate")
    assert "ticket" in str(refused.value).lower()
    assert "project" in str(refused.value).lower()


def test_verify_brief_and_gates_parse_the_three_fixed_prefixes():
    """Verify-first premise 2 (block_record.py:31, block_record.py:71,
    funnel.py:1566 on base): the ordinary block reader parses each fixed
    replacement prefix, so the brief and the gates read back what a replace
    writes."""
    from block_record import _parse_block_comment_details as parse_details

    refs, until, _reason, event = parse_details(
        ["**Blocked until 2026-10-12:** wait."])
    assert refs == [] and str(until) == "2026-10-12" and event is None

    refs, until, _reason, event = parse_details(
        ["**Blocked on #12:** wait."])
    assert refs == ["#12"] and until is None and event is None

    refs, until, _reason, event = parse_details(
        ['**Blocked until event:**\n```json\n{"agent": "codex", '
         '"job": "command-center-tickets-hourly", "outcome": "errored", '
         '"after": "2026-10-12T00:00:00Z"}\n```\nwait.'])
    assert refs == [] and until is None and event is not None
    assert event["outcome"] == "errored"
