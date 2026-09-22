"""The sanctioned post-Ready write that records an answered Gates question.

#1273 gave the marker a reader and #1274 taught the breakdown lane to
consult it. This is the third half: the write that puts a record there,
and the refusal that keeps it from becoming a way to hand-edit a plan
after Ready — which is the drift the whole plan replaces.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


#: #1167's shape, as the accept clause names it: a plan held at a breakdown
#: question, with prose around the Needs section that the write must leave
#: untouched byte for byte.
PLAN = """# The escalation scan reads quoted evidence as if the work declared it

## What it is

Pasting a log line into an issue changes that item's tier, because the scan
does not distinguish quoted evidence from a declaration.

## Rejected

- A label for quoted text: the label set is closed at two.

## Needs Nate

- Exposure: nothing outstanding. No new credentials or reachable surface.
- Gates: Should the connector answer gates on Nate's behalf?
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.

<!-- command-center-provenance -->

```json
{
  "agent": "muse",
  "at": "2026-09-21T20:50:02.105030+00:00",
  "run": "5f7714320d3b",
  "voice": "agent"
}
```
"""

ANSWER = "No. The connector never answers a gate; it relays mine."
AT = datetime(2026, 9, 21, 21, 53, 2, tzinfo=timezone.utc)


def written(body=PLAN, answer=ANSWER, decider="Nate", at=AT):
    return funnel.answered_gates_body(
        body, answer, decider, at=at, run="5f7714320d3b", agent="claude")


def gates_lines(body):
    return [line for line in body.splitlines()
            if line.lstrip().startswith("- Gates:")]


# --- the accept clause: the reader accepts it, and the line agrees ---


def test_the_marker_this_writes_is_one_the_reader_accepts():
    """The binding test. `parse_gates_answer` validates `at` through
    `parse_time`, which takes only `%Y-%m-%dT%H:%M:%SZ` — narrower than the
    `isoformat()` every other marker in funnel.py writes. A writer built to
    the house style emits a record its own reader fails closed on, and the
    gate then stays open while the body says it is answered."""
    record = funnel.parse_gates_answer(written())

    assert record is not None
    assert record["answer"] == ANSWER
    assert record["decider"] == "Nate"
    assert record["at"] == "2026-09-21T21:53:02Z"


@pytest.mark.parametrize("stamp", [
    "2026-09-21T21:53:02.105030+00:00",
    "2026-09-21T21:53:02+00:00",
])
def test_the_house_isoformat_would_not_have_been_read(stamp):
    """The negative control for the case above: these are what
    `isoformat()` produces, and the reader rejects both."""
    assert funnel.parse_time(stamp) is None


def test_the_breakdown_lane_sees_the_question_as_settled():
    item = funnel.Item(
        repo="nateprich-projects/command-center", number=1167,
        title="The escalation scan reads quoted evidence", url="",
        state="OPEN", status="Ready", labels=["blocked"],
        needs_decision="Should the connector answer gates on Nate's behalf?",
        body=written(),
    )

    assert funnel.gate_question(item) is None
    assert funnel.awaiting_decision([item]) == []


def test_the_human_line_carries_the_same_answer_the_marker_does():
    body = written()
    record = funnel.parse_gates_answer(body)

    assert gates_lines(body) == [
        "- Gates: answered {} by {}. {}".format(
            record["at"], record["decider"], record["answer"])
    ]


def test_the_answer_is_verbatim_rather_than_summarised():
    """A record saying only that something was answered is the state #1167
    was already in: the block had cleared and nobody could see on what."""
    quote = "Yes, but only for repos I own; never Brandon's."

    assert quote in written(answer=quote)
    assert funnel.parse_gates_answer(written(answer=quote))["answer"] == quote


# --- every other byte unchanged ---


def test_nothing_outside_the_gates_line_and_the_marker_moves():
    before = PLAN.splitlines()
    after = written().splitlines()
    moved = [line for line in before if line not in after]

    assert moved == ["- Gates: Should the connector answer gates on Nate's behalf?"]


def test_the_other_three_need_lines_are_untouched():
    after = written()

    for category in ("Exposure", "Scope and priority", "Preference"):
        assert "- {}: nothing outstanding.".format(category) in after


def test_the_existing_provenance_block_survives():
    """It is a marker block too, and a writer that rebuilt the body from
    parsed parts rather than editing it would drop it."""
    assert funnel.parse_provenance(written())["run"] == "5f7714320d3b"
    assert "## Rejected" in written()


# --- the refusal ---


def test_the_write_offers_no_way_to_supply_prose():
    """The first line of defence, and the weaker one: the only inputs are
    an answer and a decider, so today's code cannot be asked to edit a
    plan. This is an argument about today's code, which is why the guard
    below exists as well."""
    accepted = inspect.signature(funnel.answered_gates_body).parameters

    assert set(accepted) == {"body", "answer", "decider", "at", "run", "agent"}


def test_a_widened_write_that_touched_prose_would_be_refused():
    """The guard is a check on the result, so it holds against a change
    nobody has made yet. This stands in for that change: a body where one
    sentence of the Rejected section moved as well."""
    widened = funnel.answered_gates_body(PLAN, ANSWER, "Nate", at=AT).replace(
        "the label set is closed at two", "we can add a third")

    with pytest.raises(funnel.PlanWriteRefused) as refusal:
        funnel._refuse_unconfined_write(PLAN, widened)

    assert "after Ready" in str(refusal.value)


def test_the_guard_is_what_refuses_it_and_not_something_else():
    """Negative control. Without the confinement check the same widened
    body is indistinguishable from a legitimate write, so the test above
    binds to the guard rather than to the fixture."""
    widened = funnel.answered_gates_body(PLAN, ANSWER, "Nate", at=AT).replace(
        "the label set is closed at two", "we can add a third")

    assert funnel.parse_gates_answer(widened) is not None
    assert gates_lines(widened) == gates_lines(written())


def test_a_body_with_no_gates_line_is_refused():
    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(
            "# A plan with no Needs section\n", ANSWER, "Nate", at=AT)


def test_an_empty_body_is_refused():
    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body("   ", ANSWER, "Nate", at=AT)


@pytest.mark.parametrize("answer,decider", [
    ("", "Nate"),
    ("   ", "Nate"),
    (ANSWER, ""),
    (ANSWER, "   "),
])
def test_an_incomplete_record_is_never_written(answer, decider):
    """The reader fails closed on these, so writing one would produce a
    body claiming an answer that no lane can read."""
    with pytest.raises(ValueError):
        funnel.gates_answer_block(answer, decider, at=AT)


# --- a second answer ---


def test_a_second_answer_replaces_the_first_rather_than_stacking():
    second = funnel.answered_gates_body(
        written(), "Changed my mind: relay it.", "Nate", at=AT)

    assert second.count(funnel.GATES_ANSWER_MARKER) == 1
    assert funnel.parse_gates_answer(second)["answer"] == \
        "Changed my mind: relay it."
    assert len(gates_lines(second)) == 1


def test_a_second_answer_leaves_the_prose_alone_too():
    second = funnel.answered_gates_body(
        written(), "Changed my mind: relay it.", "Nate", at=AT)

    assert "## Rejected" in second
    assert "- Exposure: nothing outstanding." in second


# --- shape details that would otherwise be guesswork ---


def test_an_indented_gates_line_keeps_its_indent():
    body = PLAN.replace(
        "- Gates: Should", "  - Gates: Should")

    assert gates_lines(written(body=body))[0].startswith("  - Gates: answered")


def test_the_payload_names_the_session_that_heard_it():
    payload = json.loads(
        written().split("```json\n")[-1].rsplit("\n```", 1)[0])

    assert payload["agent"] == "claude"
    assert payload["run"] == "5f7714320d3b"


# --- the command, which is what a session actually calls ---


def _item(body=PLAN, state="OPEN", parent=None, status="Ready"):
    return funnel.Item(
        repo="nateprich-projects/command-center", number=1167,
        title="The escalation scan reads quoted evidence",
        url="https://github.com/nateprich-projects/command-center/issues/1167",
        state=state, status=status, labels=["blocked"], parent=parent,
        needs_decision="Should the connector answer gates on Nate's behalf?",
        body=body,
    )


def _capture_edits(monkeypatch):
    calls = []

    def run_gh(args, **kwargs):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(funnel, "_run_gh", run_gh)
    return calls


def test_the_command_writes_the_body_in_exactly_one_edit(monkeypatch, capsys):
    """One `gh issue edit` carries both halves, so no reader can catch the
    body with a marker and a Gates line that disagree."""
    calls = _capture_edits(monkeypatch)
    item = _item()

    assert funnel.cmd_answer_gates(
        [item], AT, "nateprich-projects/command-center#1167",
        ANSWER, "Nate", run="5f7714320d3b", agent="claude") == 0

    edits = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
    assert len(edits) == 1
    assert len(calls) == 1
    assert ANSWER in edits[0][-1]
    assert funnel.parse_gates_answer(edits[0][-1])["decider"] == "Nate"
    assert ANSWER in capsys.readouterr().out


def test_the_command_leaves_the_in_session_item_matching_github(monkeypatch):
    """A same-session reader evaluating this item after the write must not
    see the body GitHub no longer holds."""
    _capture_edits(monkeypatch)
    item = _item()

    funnel.cmd_answer_gates(
        [item], AT, "nateprich-projects/command-center#1167", ANSWER, "Nate")

    assert funnel.parse_gates_answer(item.body) is not None
    assert funnel.gate_question(item) is None


def test_a_closed_project_takes_no_answer(monkeypatch):
    calls = _capture_edits(monkeypatch)

    with pytest.raises(funnel.GitHubError):
        funnel.cmd_answer_gates(
            [_item(state="CLOSED")], AT,
            "nateprich-projects/command-center#1167", ANSWER, "Nate")

    assert calls == []


def test_a_ticket_is_refused_because_the_question_is_its_projects(monkeypatch):
    calls = _capture_edits(monkeypatch)
    parent = "nateprich-projects/command-center#1100"

    with pytest.raises(funnel.GitHubError) as refusal:
        funnel.cmd_answer_gates(
            [_item(parent=parent)], AT,
            "nateprich-projects/command-center#1167", ANSWER, "Nate")

    assert parent in str(refusal.value)
    assert calls == []


def test_a_body_without_a_gates_line_never_reaches_github(monkeypatch):
    """The refusal happens before the write, so a plan this cannot record
    an answer in is left exactly as it was."""
    calls = _capture_edits(monkeypatch)

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.cmd_answer_gates(
            [_item(body="# No Needs section here\n")], AT,
            "nateprich-projects/command-center#1167", ANSWER, "Nate")

    assert calls == []


def test_two_gates_lines_are_refused_rather_than_guessed_between():
    """A plan may quote the Needs form while rejecting an alternative —
    `skills/shape` documents these lines verbatim. Rewriting the quotation
    would leave the real question open, and the confinement check cannot
    tell: it strips every Gates line from both sides, so the wrong one
    moving looks exactly like the right one moving."""
    quoting = PLAN.replace(
        "- A label for quoted text: the label set is closed at two.",
        "- Answering in the plan text, as in `- Gates: we decided offline`:\n"
        "  invisible to every lane.\n"
        "- Gates: we decided offline",
    )

    with pytest.raises(funnel.PlanWriteRefused) as refusal:
        funnel.answered_gates_body(quoting, ANSWER, "Nate", at=AT)

    assert "2 `- Gates:` lines" in str(refusal.value)


def test_the_single_line_case_is_unaffected():
    """The guard above must not have narrowed the ordinary path."""
    assert funnel.parse_gates_answer(written()) is not None
    assert len(gates_lines(written())) == 1
