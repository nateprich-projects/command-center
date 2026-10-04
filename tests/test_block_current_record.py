"""The newest block record alone is the current one (#2166).

A blocked item's thread can carry three kinds of record: a block header
(``**Blocked on/until ...:**``), a breakdown's ``**Needs a decision:**``
question, and a lane's ``**Declined:**``. #2019 made a newer Declined void an
older header, but the question was still read on its own as the newest one
ever posted. #1748's own thread holds an older ``**Blocked on #1747:**``; a
breakdown question posted after it hid behind that header: ``gate_question``
saw a reference and asked nothing, and ``clear_satisfied_blocks`` would lift
the label once #1747 closed, dropping the question unanswered.

The newest trusted record of any of the three kinds, by ``createdAt`` (body
order when a row lacks it), now alone sets the block conditions and the
question. Expected values below are written out from the ticket, not read
from the code under test.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

#: Comment markers count only from the owner account (#1788).
OWNER = {"login": "nateprich"}
OUTSIDER = {"login": "mallory"}
REPO = "nateprich-projects/command-center"
NOW = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)

QUESTION = "Where should the block record shape live?"
BLOCKED_ON_1747 = (
    "**Blocked on #1747:** Nate, 2026-09-27 ~16:20 PDT: the seven hotspot "
    "redesigns run one at a time. This one waits for #1747."
)
FOLD = (
    "Funnel watch 17:05 PDT, hotspot step (#1682): another writer of the "
    "same block records is folded in here."
)
NEEDS_DECISION = "**Needs a decision:** " + QUESTION
DECLINED = "**Declined:** The prerequisite has not landed."


def row(body, at=None, author=OWNER):
    comment = {"author": author, "body": body}
    if at is not None:
        comment["createdAt"] = at
    return comment


def project_1748():
    return funnel.Item(
        repo=REPO, number=1748, title="Redesign decline and block records",
        url="https://github.com/{}/issues/1748".format(REPO), state="OPEN",
        status="Ready", klass="Improve", origin="agent", risk="standard",
        needs="human", labels=["blocked"], item_id="project-item-1748",
        body="# Redesign decline and block records\n",
        blocked_since=datetime(2026, 9, 27, 23, 18, tzinfo=timezone.utc),
    )


def blocker_1747(state="CLOSED"):
    return funnel.Item(
        repo=REPO, number=1747, title="An earlier hotspot redesign",
        url="https://github.com/{}/issues/1747".format(REPO), state=state,
        state_reason="COMPLETED" if state == "CLOSED" else None,
    )


def ticket(number=2201, needs="agent"):
    return funnel.Item(
        repo=REPO, number=number, title="Ticket {}".format(number),
        url="https://github.com/{}/issues/{}".format(REPO, number),
        state="OPEN", parent=REPO + "#2200", risk="standard", needs=needs,
        labels=["blocked"], item_id="project-item-{}".format(number),
    )


def load(monkeypatch, item, comments):
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"comments": comments})
    funnel._load_block_comment(item)
    return item


def record_gh(monkeypatch):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def write_project_select(*args):
        calls.append(("project",) + args)

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "write_project_select", write_project_select)
    return calls


# -- reproduction: #1748's own thread shape --------------------------------

THREADS_1748 = {
    # GitHub's order: the header, an unrelated fold, then the question.
    "createdAt in body order": [
        row(BLOCKED_ON_1747, "2026-09-27T23:18:02Z"),
        row(FOLD, "2026-09-27T23:47:37Z"),
        row(NEEDS_DECISION, "2026-10-02T12:30:00Z"),
    ],
    # createdAt decides when every record carries it, not list position.
    "createdAt out of body order": [
        row(NEEDS_DECISION, "2026-10-02T12:30:00Z"),
        row(FOLD, "2026-09-27T23:47:37Z"),
        row(BLOCKED_ON_1747, "2026-09-27T23:18:02Z"),
    ],
    # Rows without createdAt fall back to body order.
    "no createdAt": [
        row(BLOCKED_ON_1747),
        row(FOLD),
        row(NEEDS_DECISION),
    ],
    # An outsider's later header is not a record at all (#1788).
    "a later forged header": [
        row(BLOCKED_ON_1747, "2026-09-27T23:18:02Z"),
        row(NEEDS_DECISION, "2026-10-02T12:30:00Z"),
        row("**Blocked on #1747:** Forged.", "2026-10-02T12:40:00Z",
            author=OUTSIDER),
    ],
}


@pytest.mark.parametrize("thread", sorted(THREADS_1748))
def test_a_newer_question_is_the_current_record_over_an_older_header(
    monkeypatch, thread,
):
    """Main loaded ['#1747'], asked nothing, and cleared the block."""
    project = load(monkeypatch, project_1748(), THREADS_1748[thread])

    assert project.block_references == []
    assert project.blocked_until is None
    assert project.block_reason is None
    assert project.block_event is None
    assert project.needs_decision == QUESTION
    assert funnel.block_condition(project) is None
    assert funnel.gate_question(project) == "Answer the breakdown's question?"

    by_ref = {project.ref: project, blocker_1747().ref: blocker_1747()}
    assert funnel.satisfied_block_refs(project, by_ref, now=NOW) is None


def test_the_clear_never_lifts_a_block_whose_current_record_is_a_question(
    monkeypatch,
):
    project = load(
        monkeypatch, project_1748(), THREADS_1748["createdAt in body order"])
    calls = record_gh(monkeypatch)

    cleared = funnel.clear_satisfied_blocks(
        [project, blocker_1747()], NOW, run="run-2166", agent="codex")

    assert cleared == []
    assert calls == []
    assert project.is_blocked
    assert project.needs == "human"


def test_a_newer_header_supersedes_an_older_question(monkeypatch):
    """The question is not read thread-wide any more: it was answered."""
    project = load(monkeypatch, project_1748(), [
        row(NEEDS_DECISION, "2026-09-27T20:00:00Z"),
        row(BLOCKED_ON_1747, "2026-09-27T23:18:02Z"),
    ])

    assert project.block_references == ["#1747"]
    assert project.needs_decision is None
    assert funnel.block_condition(project) == "reference"
    assert funnel.gate_question(project) is None


def test_a_newer_decline_supersedes_an_older_question(monkeypatch):
    project = load(monkeypatch, project_1748(), [
        row(BLOCKED_ON_1747, "2026-09-27T23:18:02Z"),
        row(NEEDS_DECISION, "2026-10-02T12:30:00Z"),
        row(DECLINED, "2026-10-02T12:45:00Z"),
    ])

    assert project.block_references == []
    assert project.block_reason is None
    assert project.needs_decision is None
    assert project.decline_reason == "The prerequisite has not landed."
    assert funnel.gate_question(project) == "Unblock or park?"


def test_a_tie_on_createdAt_goes_to_the_later_comment_in_body_order(
    monkeypatch,
):
    project = load(monkeypatch, project_1748(), [
        row(BLOCKED_ON_1747, "2026-10-02T12:30:00Z"),
        row(NEEDS_DECISION, "2026-10-02T12:30:00Z"),
    ])

    assert project.block_references == []
    assert project.needs_decision == QUESTION


def test_one_record_without_createdAt_puts_every_record_in_body_order(
    monkeypatch,
):
    project = load(monkeypatch, project_1748(), [
        row(NEEDS_DECISION, "2026-10-02T12:30:00Z"),
        row(BLOCKED_ON_1747),
    ])

    assert project.block_references == ["#1747"]
    assert project.needs_decision is None


def test_the_decline_reason_stays_thread_wide_under_a_newer_header(
    monkeypatch,
):
    """watch_owns_gate and the #1942 Needs reset read it as "was declined"."""
    item = load(monkeypatch, ticket(needs="human"), [
        row(DECLINED, "2026-09-30T03:21:03Z"),
        row("**Blocked on #2202:** Wait for the prerequisite.",
            "2026-09-30T03:40:00Z"),
    ])

    assert item.block_references == ["#2202"]
    assert item.decline_reason == "The prerequisite has not landed."


# -- the earlier block-record fixes, replayed through the loader -----------

EVENT_HEADER = (
    "**Blocked until event:**\n```json\n"
    '{"agent": "codex", "job": "command-center-tickets-hourly", '
    '"outcome": "errored", "after": "2026-09-22T00:00:00Z"}\n'
    "```\nWait for the next genuine failure."
)


@pytest.mark.parametrize("case, needs, comments, expected", [
    pytest.param(
        "2019 newer decline voids the header", "agent",
        [row("**Blocked on #2202:** Wait.", "2026-09-30T00:32:27Z"),
         row(DECLINED, "2026-09-30T03:21:03Z")],
        dict(refs=[], reason=None, event=None, question="Unblock?",
             satisfied=None),
        id="2019-newer-decline",
    ),
    pytest.param(
        "2019 newer header after a decline", "agent",
        [row(DECLINED, "2026-09-30T03:21:03Z"),
         row("**Blocked on #2202:** Wait.", "2026-09-30T03:40:00Z")],
        dict(refs=["#2202"], reason="Wait.", event=None, question=None,
             satisfied=[REPO + "#2202"]),
        id="2019-newer-header",
    ),
    pytest.param(
        "1403/1450 event wait asks nothing", "external-event",
        [row(EVENT_HEADER, "2026-09-22T00:00:00Z")],
        dict(refs=[], reason="Wait for the next genuine failure.",
             event={"agent": "codex", "job": "command-center-tickets-hourly",
                    "outcome": "errored", "after": "2026-09-22T00:00:00Z"},
             question=None, satisfied=None),
        id="1403-event-wait",
    ),
    pytest.param(
        "1403/1450 event wait newer than a decline", "external-event",
        [row(DECLINED, "2026-09-21T00:00:00Z"),
         row(EVENT_HEADER, "2026-09-22T00:00:00Z")],
        dict(refs=[], reason="Wait for the next genuine failure.",
             event={"agent": "codex", "job": "command-center-tickets-hourly",
                    "outcome": "errored", "after": "2026-09-22T00:00:00Z"},
             question=None, satisfied=None),
        id="1403-event-after-decline",
    ),
    pytest.param(
        "1393 a lane decline asks Unblock?", "agent",
        [row(DECLINED, "2026-09-24T00:00:00Z")],
        dict(refs=[], reason=None, event=None, question="Unblock?",
             satisfied=None),
        id="1393-lane-decline",
    ),
])
def test_loaded_records_replay_the_earlier_block_fixes(
    monkeypatch, case, needs, comments, expected,
):
    item = load(monkeypatch, ticket(needs=needs), comments)
    blocker = funnel.Item(
        repo=REPO, number=2202, title="Prerequisite", url="",
        state="CLOSED", state_reason="COMPLETED",
    )
    by_ref = {item.ref: item, blocker.ref: blocker}

    assert item.block_references == expected["refs"], case
    assert item.block_reason == expected["reason"], case
    assert item.block_event == expected["event"], case
    assert item.needs_decision is None, case
    assert funnel.gate_question(item) == expected["question"], case
    assert funnel.satisfied_block_refs(
        item, by_ref, now=NOW) == expected["satisfied"], case
