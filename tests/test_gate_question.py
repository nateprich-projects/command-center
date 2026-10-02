"""gate_question never asks Unblock? about a block it could not read (#2134).

When ``_load_block_comment`` cannot read a blocked item's comments (the
shared GraphQL window running dry, #1417), the item's block conditions stay
empty. A dated, event-conditioned or issue-conditioned hold then read as a
silent block and asked "Unblock?" or "Unblock or park?", which
``watch_owns_gate`` handed to the funnel watch as a block with nothing named
that could lift it. An unread block now asks its own question, routed the
way the Unblock questions are, and every question comes from one map.

The table test replays the earlier gate-wording fixes (#1166, #1261, #1393,
#1401, #1403) so the single source cannot quietly change what they settled.
Expected strings are written out here, not read from the map under test.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
import funnel_render  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
REPO = "nateprich/beta"
UNREAD = "could not read comments"

EVENT = {
    "agent": "codex",
    "job": "command-center-tickets-hourly",
    "outcome": "errored",
    "after": "2026-09-22T15:00:00Z",
}


@pytest.fixture(autouse=True)
def offline_brief(monkeypatch):
    """Keep ``cmd_brief`` on the fixture items: no GitHub, no heartbeat."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", lambda items, now: None
    )
    monkeypatch.setattr(
        funnel, "decline_routing_metric",
        lambda items, now: {"status": "available", "declines": 0},
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})
    yield
    funnel.reset_api_usage()


def project(number=30, **fields):
    values = dict(
        repo=REPO, number=number, title="Project {}".format(number),
        url="https://example.invalid/{}".format(number), state="OPEN",
        status="Building", klass="Broken", origin="agent", risk="standard",
        needs="none", children_total=2, children_done=1,
        status_since=NOW - timedelta(days=3),
    )
    values.update(fields)
    return funnel.Item(**values)


def ticket(number=31, parent=30, **fields):
    values = dict(
        repo=REPO, number=number, title="Ticket {}".format(number),
        url="https://example.invalid/{}".format(number), state="OPEN",
        parent="{}#{}".format(REPO, parent), risk="standard", needs="none",
        labels=["blocked"], blocked_since=NOW - timedelta(days=1),
    )
    values.update(fields)
    return funnel.Item(**values)


def routed(item, *others):
    by_ref = {i.ref: i for i in (item,) + others}
    return funnel.watch_owns_gate(item, funnel.gate_question(item), by_ref)


# -- reproduction: an unread block is not a silent block ------------------


def test_an_unread_blocked_ticket_and_project_ask_the_unread_question():
    """Current main asked "Unblock?" and "Unblock or park?" here."""
    unread_ticket = ticket(block_comments_error=UNREAD)
    unread_project = project(
        32, labels=["blocked"], block_comments_error=UNREAD,
    )

    assert funnel.gate_question(unread_ticket) == "Block unread — recheck?"
    assert funnel.gate_question(unread_project) == "Block unread — recheck?"


def test_the_brief_hands_unread_blocks_to_the_watch_and_names_them_missing(
    capsys,
):
    parent = project()
    unread_ticket = ticket(block_comments_error=UNREAD)
    unread_project = project(
        32, labels=["blocked"], block_comments_error=UNREAD,
        blocked_since=NOW - timedelta(days=2),
    )

    assert funnel.cmd_brief([parent, unread_ticket, unread_project], NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["items"] == []
    assert brief["total_needing_nate"] == 0
    assert {
        row["ref"]: row["question"] for row in brief["watch_gates"]
    } == {
        unread_ticket.ref: "Block unread — recheck?",
        unread_project.ref: "Block unread — recheck?",
    }
    # The brief's existing record of the failed read still names both.
    assert [row for row in brief["missing"] if row["section"] == "blocked"] == [{
        "section": "blocked",
        "error": "{}: {}; {}: {}".format(
            unread_ticket.ref, UNREAD, unread_project.ref, UNREAD,
        ),
    }]


def test_an_unread_block_is_routed_exactly_as_an_unblock_question():
    """Watch-owned unless it waits on Nate's hands; a decline is the
    watch's even with Needs ``human`` (#1891, #1902)."""
    parent = project()
    for needs in ("none", "external-event", "claude-code-environment"):
        unread = ticket(needs=needs, block_comments_error=UNREAD)
        silent = ticket(needs=needs)
        assert funnel.gate_question(silent) == "Unblock?"
        assert routed(unread, parent) is routed(silent, parent) is True

    unread_human = ticket(needs="human", block_comments_error=UNREAD)
    silent_human = ticket(needs="human")
    assert funnel.gate_question(unread_human) == "Block unread — recheck?"
    assert routed(unread_human, parent) is routed(silent_human, parent) is False

    unread_project = project(32, labels=["blocked"], block_comments_error=UNREAD)
    assert routed(unread_project) is True


def test_an_unread_ordinary_agent_owned_block_stays_quiet_as_before():
    """Needs ``agent`` without a read decline asked nothing before and still
    asks nothing; the brief's ``missing`` entry is what names it."""
    unread = ticket(needs="agent", block_comments_error=UNREAD)

    assert funnel.gate_question(unread) is None


def test_an_unread_item_that_is_not_blocked_asks_nothing():
    """Needs ``agent`` and ``external-event`` rows have their comments read
    without being blocked; a failed read there is not a block question."""
    for needs in ("agent", "external-event"):
        unread = ticket(needs=needs, labels=[], block_comments_error=UNREAD)
        assert funnel.gate_question(unread) is None
        assert funnel.block_condition(unread) is None


# -- block_condition: one test for what can lift a block ------------------


@pytest.mark.parametrize("fields, expected", [
    ({"block_comments_error": UNREAD}, "unread"),
    ({"block_event": EVENT, "needs": "external-event"}, "event"),
    ({"blocked_until": date(2026, 10, 9)}, "date"),
    ({"blocked_until": "2026-10-09"}, "date"),
    ({"blocked_until": "not a date"}, None),
    ({"block_references": ["#70"]}, "reference"),
    ({"blocked_until": date(2026, 10, 9), "block_references": ["#70"]}, "date"),
    ({}, None),
    ({"labels": [], "block_references": ["#70"]}, None),
])
def test_block_condition_names_what_can_lift_the_block(fields, expected):
    assert funnel.block_condition(ticket(**fields)) == expected


def test_a_condition_is_unread_when_the_comments_were_not_read():
    """The conditions come from the comment thread, so an unread thread
    leaves them unknown, not absent."""
    unread = ticket(block_comments_error=UNREAD, block_references=["#70"])

    assert funnel.block_condition(unread) == "unread"


def test_held_at_accept_reads_the_shared_block_condition():
    """#1725's hold is a dated or issue-conditioned block on a finished
    project; an event wait and an unread block are not holds."""
    def finished(**fields):
        return project(
            50, klass="New", origin="Nate", children_total=2,
            children_done=2, labels=["blocked"], **fields,
        )

    assert funnel.is_held_at_accept(finished(blocked_until=date(2026, 10, 9)))
    assert funnel.is_held_at_accept(finished(block_references=["#70"]))
    assert not funnel.is_held_at_accept(finished(block_event=EVENT))
    assert not funnel.is_held_at_accept(finished())
    unread = finished(block_comments_error=UNREAD)
    assert not funnel.is_held_at_accept(unread)
    assert funnel.gate_question(unread) == "Block unread — recheck?"


# -- the earlier gate-wording fixes, replayed -----------------------------


GATES_ANSWER_BODY = (
    "# Reach the funnel from general chat\n\n## Needs Nate\n\n"
    "- Gates: answered — see the marker below.\n\n{marker}\n\n"
    "```json\n{payload}\n```\n"
).format(
    marker=funnel.GATES_ANSWER_MARKER,
    payload=json.dumps({
        "answer": "No. The connector never answers a gate; it relays mine.",
        "at": "2026-09-21T21:53:02Z",
        "decider": "nate",
    }),
)

ESCALATION_SOUNDING_PLAN = (
    "## What\n\nFix the parser.\n\n## Rejected\n\n"
    "- A data migration that backfills every row, because it authorizes "
    "a transaction nobody asked for.\n"
)


def _replay(case):
    if case == "1166 closed shaped plan":
        return project(status="Shaped", origin="Nate", state="CLOSED")
    if case == "1166 closed blocked ticket":
        return ticket(state="CLOSED")
    if case == "1166 closed finished project":
        return project(
            klass="New", children_done=2, state="CLOSED",
        )
    if case == "1261 answered gates marker":
        return project(
            status="Ready", labels=["blocked"], body=GATES_ANSWER_BODY,
            needs_decision="Should the connector answer gates for Nate?",
        )
    if case == "1261 unanswered breakdown question":
        return project(
            status="Ready", labels=["blocked"], body="# No marker\n",
            needs_decision="Should the connector answer gates for Nate?",
        )
    if case == "1393 decline with needs agent":
        return ticket(
            needs="agent", decline_reason="the prerequisite has not landed",
        )
    if case == "1393 ordinary agent-owned block":
        return ticket(needs="agent")
    if case == "1401 standard agent plan":
        return project(
            status="Shaped", children_total=0, children_done=0,
            body=ESCALATION_SOUNDING_PLAN,
        )
    if case == "1401 escalated agent plan":
        return project(
            status="Shaped", risk="escalated", children_total=0,
            children_done=0, body=ESCALATION_SOUNDING_PLAN,
        )
    if case == "1401 nate-origin plan":
        return project(
            status="Shaped", origin="Nate", children_total=0,
            children_done=0, body=ESCALATION_SOUNDING_PLAN,
        )
    if case == "1403 well-formed event wait":
        return ticket(needs="external-event", block_event=EVENT)
    if case == "1403 external-event without a spec":
        return ticket(needs="external-event")
    raise AssertionError(case)


@pytest.mark.parametrize("case, expected", [
    ("1166 closed shaped plan", None),
    ("1166 closed blocked ticket", None),
    ("1166 closed finished project", None),
    ("1261 answered gates marker", None),
    ("1261 unanswered breakdown question",
     "Answer the breakdown's question?"),
    ("1393 decline with needs agent", "Unblock?"),
    ("1393 ordinary agent-owned block", None),
    ("1401 standard agent plan", None),
    ("1401 escalated agent plan", "Is the plan good?"),
    ("1401 nate-origin plan", "Is the plan good?"),
    ("1403 well-formed event wait", None),
    ("1403 external-event without a spec", "Unblock?"),
])
def test_gate_question_replays_the_earlier_gate_wording_fixes(case, expected):
    assert funnel.gate_question(_replay(case)) == expected


# -- one gate-question source ---------------------------------------------


def _string_constants(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_every_gate_question_literal_appears_once_in_funnel():
    constants = _string_constants(ROOT / "funnel.py")
    for question in (
        "Is the plan good?", "Accept it?", "Answer the breakdown's question?",
        "Unblock?", "Unblock or park?", "Block unread — recheck?",
    ):
        assert constants.count(question) == 1, question
        assert question in funnel.GATE_QUESTIONS.values()


def test_the_stage_gates_and_watch_questions_read_the_one_map():
    questions = set(funnel.GATE_QUESTIONS.values())

    assert set(funnel.GATES.values()) <= questions
    assert funnel.WATCH_UNBLOCK_QUESTIONS == {
        "Unblock?", "Unblock or park?", "Block unread — recheck?",
    }


def test_the_render_template_names_the_watch_questions_from_the_map():
    """The watch-gates sentence is built from the map, so a reworded
    question cannot leave the template naming the old literal."""
    template = funnel_render.render_template()
    section = template.split("Then `watch_gates`", 1)[1].split("\n\n", 1)[0]
    for question in sorted(funnel.WATCH_UNBLOCK_QUESTIONS) + [
        funnel.GATES["Shaped"],
    ]:
        assert "`{}`".format(question) in section, question
    for constant in _string_constants(ROOT / "funnel_render.py"):
        for question in funnel.WATCH_UNBLOCK_QUESTIONS:
            assert question not in constant, question
