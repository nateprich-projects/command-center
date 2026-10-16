"""`answer-gates` on current-format plans: one `## Needs Nate` question.

#2594: the shaper renders only the categories with open questions, so a
plan whose question is not a Gates question carries no `- Gates:` line and
`answered_gates_body` refused it with `PlanWriteRefused`. The legacy
`- Gates:` write is unchanged (a thin adapter); a body with no Gates line
but one `## Needs Nate` question is answered in that section instead.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


AT = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)
ANSWER = "Yes, both repos."
DECIDER = "Nate"


#: A current-format Shaped body: the only open question is Scope, so the
#: shaper rendered no `- Gates:` line at all.
CURRENT = """# Rollout plan

## What it is

Roll out the worker to both repos.

## Needs Nate

- Scope and priority: Should the rollout cover both repos?

<!-- command-center-provenance -->

```json
{
  "agent": "muse",
  "at": "2026-10-13T11:00:00.000000+00:00",
  "run": "2547run0000001",
  "voice": "agent"
}
```
"""

#: A legacy-format body: the question sits on the `- Gates:` line.
LEGACY = """# Rollout plan

## What it is

Roll out the worker to both repos.

## Needs Nate

- Exposure: nothing outstanding. No new credentials or reachable surface.
- Gates: Should the rollout cover both repos?
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.
"""


def written_current(body=CURRENT, answer=ANSWER):
    return funnel.answered_gates_body(
        body, answer, DECIDER, at=AT, run="answer-run", agent="muse")


# --- the accept clause: the current format answers with no hand-edit ---


def test_current_format_single_question_is_answered_in_section():
    body = written_current()

    assert "- Scope and priority: answered " in body
    assert ANSWER in body
    assert "## Needs Nate" in body


def test_current_format_marker_is_one_the_reader_accepts():
    record = funnel.parse_gates_answer(written_current())

    assert record is not None
    assert record["answer"] == ANSWER
    assert record["decider"] == DECIDER


def test_current_format_answer_is_verbatim():
    quote = "Yes, but only the worker repo; never the dashboard."

    assert quote in written_current(answer=quote)
    assert funnel.parse_gates_answer(
        written_current(answer=quote))["answer"] == quote


def test_current_format_question_reads_as_settled():
    assert funnel.plan_needs_nate(written_current()) is False
    assert funnel.open_needs_nate_categories(written_current()) == frozenset()


def test_current_format_leaves_every_other_byte_alone():
    before = CURRENT.splitlines()
    after = written_current().splitlines()
    moved = [line for line in before if line not in after]

    assert moved == ["- Scope and priority: Should the rollout cover both repos?"]


def test_current_format_second_answer_replaces_the_first():
    second = funnel.answered_gates_body(
        written_current(), "Changed my mind: worker only.", DECIDER, at=AT)

    assert second.count(funnel.GATES_ANSWER_MARKER) == 1
    assert funnel.parse_gates_answer(second)["answer"] == \
        "Changed my mind: worker only."
    assert len([line for line in second.splitlines()
                if "Scope and priority: answered" in line]) == 1


def test_current_format_indented_slot_keeps_its_indent():
    body = CURRENT.replace(
        "- Scope and priority: Should", "  - Scope and priority: Should")

    assert "  - Scope and priority: answered " in written_current(body=body)


def test_current_format_short_scope_spelling_answers():
    body = CURRENT.replace(
        "- Scope and priority: Should", "- Scope: Should")

    assert "- Scope: answered " in written_current(body=body)
    assert funnel.plan_needs_nate(written_current(body=body)) is False


# --- the legacy path still answers (thin adapter, unchanged) ---


def test_legacy_format_still_answers():
    body = funnel.answered_gates_body(
        LEGACY, ANSWER, DECIDER, at=AT, run="answer-run", agent="muse")
    record = funnel.parse_gates_answer(body)

    assert record is not None
    assert record["answer"] == ANSWER
    assert "- Gates: answered " in body
    assert funnel.plan_needs_nate(body) is False


def test_legacy_multi_gates_lines_still_refused():
    quoting = LEGACY.replace(
        "Roll out the worker to both repos.",
        "Roll out the worker, as in `- Gates: we decided offline`:\n"
        "- Gates: we decided offline",
    )

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(quoting, ANSWER, DECIDER, at=AT)


# --- neither format is still refused ---


def test_body_with_no_needs_section_is_refused():
    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(
            "# A plan with no Needs section\n", ANSWER, DECIDER, at=AT)


def test_section_with_no_open_question_is_refused():
    settled = CURRENT.replace(
        "- Scope and priority: Should the rollout cover both repos?",
        "- Scope and priority: nothing outstanding. The scope is settled.",
    )

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(settled, ANSWER, DECIDER, at=AT)


def test_unreadable_section_is_refused():
    prose = CURRENT.replace(
        "- Scope and priority: Should the rollout cover both repos?",
        "Someone should decide the rollout scope.",
    )

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(prose, ANSWER, DECIDER, at=AT)


# --- the guard: more than one question never guesses ---


def test_two_open_questions_are_refused_rather_than_guessed_between():
    two = CURRENT.replace(
        "## Needs Nate\n",
        "## Needs Nate\n\n- Exposure: Does this need a new credential?\n",
    )

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(two, ANSWER, DECIDER, at=AT)


def test_open_plus_answered_is_refused_as_ambiguous():
    both = written_current().replace(
        "## Needs Nate\n",
        "## Needs Nate\n\n- Exposure: Does this need a new credential?\n",
    )

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(both, ANSWER, DECIDER, at=AT)


def test_two_needs_headings_are_refused():
    quoted = CURRENT + "\n## Needs Nate\n\nQuoted from an older plan.\n"

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.answered_gates_body(quoted, ANSWER, DECIDER, at=AT)


def test_the_confinement_check_refuses_a_widened_section_write():
    widened = written_current().replace(
        "Roll out the worker to both repos.",
        "Roll out the worker to every repo.",
    )
    slot = funnel._needs_nate_slot(
        CURRENT.split("## Needs Nate")[1].split("<!--")[0])
    line = [entry for entry in written_current().splitlines()
            if "Scope and priority: answered" in entry][0]

    with pytest.raises(funnel.PlanWriteRefused):
        funnel._refuse_unconfined_needs_write(CURRENT, widened, slot, line)


# --- the command, which is what a session actually calls ---


def _item(body=CURRENT, state="OPEN", status="Shaped"):
    return funnel.Item(
        repo="nateprich-projects/command-center", number=2547,
        title="Run-keeper git maintenance settings",
        url="https://github.com/nateprich-projects/command-center/issues/2547",
        state=state, status=status, labels=["blocked"],
        needs_decision="Should the rollout cover both repos?",
        body=body,
    )


def _capture_edits(monkeypatch):
    calls = []

    def run_gh(args, **kwargs):
        calls.append(args)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(funnel, "_run_gh", run_gh)
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: None,
    )
    return calls


def test_the_command_answers_a_current_format_body_in_one_edit(monkeypatch):
    calls = _capture_edits(monkeypatch)
    item = _item()
    item.item_id = "project-item-2547"

    assert funnel.cmd_answer_gates(
        [item], AT, item.ref, ANSWER, DECIDER,
        run="answer-run", agent="muse") == 0

    edits = [call for call in calls if call[:3] == ["gh", "issue", "edit"]]
    assert len(edits) == 1
    assert "- Scope and priority: answered " in edits[0][-1]
    assert funnel.parse_gates_answer(edits[0][-1])["answer"] == ANSWER
    assert item.needs == "none"


def test_the_command_still_refuses_a_body_with_neither_format(monkeypatch):
    calls = _capture_edits(monkeypatch)
    item = _item(body="# No Needs section here\n")
    item.item_id = "project-item-2547"

    with pytest.raises(funnel.PlanWriteRefused):
        funnel.cmd_answer_gates(
            [item], AT, item.ref, ANSWER, DECIDER)

    assert calls == []
