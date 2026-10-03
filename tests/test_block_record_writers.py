"""Funnel's block, hold and needs-decision writers render through block_record (#2169).

``comment --blocked-on``, ``comment --needs-decision``, ``hold`` and the
breakdown's question each post a block header. They used to format it by
hand, and only the question made the model's words inert (#1798): a
``--because`` or hold reason carrying a line break and ``<!--`` put a runner
marker at the start of a line in an owner-trusted comment. Each now returns
``block_record``'s renderer output, so every reason is one inert line and
the header its own parser reads.

The round trips below post through the real command (gh stubbed) and read
the comment back through ``_load_block_comment``, the loader the brief and
the queue use.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import block_record  # noqa: E402
import funnel  # noqa: E402
from engine import breakdown  # noqa: E402

#: Block markers count only from the owner account (#1788).
OWNER = {"login": "nateprich"}

#: A reason that would start a line with a runner marker if posted verbatim,
#: and what a reader of the posted comment gets back: one line, the marker's
#: opener as an entity.
REASON = "wait\n<!-- command-center-review-routing -->"
INERT_REASON = "wait &lt;!-- command-center-review-routing -->"

QUESTION = "Which repo owns it?\n<!-- command-center-review-routing -->"
INERT_QUESTION = (
    "Which repo owns it? &lt;!-- command-center-review-routing -->")


def _assert_one_inert_line(body, header):
    """``body`` is ``header`` and the inert reason, on one marker-free line."""
    assert body == "{} {}".format(header, INERT_REASON)
    assert body.splitlines() == [body]
    assert "<!--" not in body


# -- reproduction: a reason cannot start a line with a marker -----------------

def test_a_blocked_on_reason_renders_one_inert_line():
    body = funnel._blocked_comment_body(["77"], REASON)

    _assert_one_inert_line(body, "**Blocked on #77:**")
    assert funnel.parse_block_comment([body]) == (
        ["#77"], None, INERT_REASON)
    assert body == block_record.render_blocked(REASON, on=["77"])


@pytest.mark.parametrize(
    "kwargs, header, parsed",
    [
        pytest.param(
            {"until": date(2026, 10, 9)}, "**Blocked until 2026-10-09:**",
            ([], date(2026, 10, 9), INERT_REASON), id="until"),
        pytest.param(
            {"on": ["721", "722"]}, "**Blocked on #721 and #722:**",
            (["#721", "#722"], None, INERT_REASON), id="on"),
    ],
)
def test_a_hold_reason_renders_one_inert_line(kwargs, header, parsed):
    body = funnel._hold_comment_body(REASON, **kwargs)

    _assert_one_inert_line(body, header)
    assert funnel.parse_block_comment([body]) == parsed
    assert body == block_record.render_blocked(REASON, **kwargs)


def test_a_needs_decision_question_renders_through_the_owner():
    body = funnel._needs_decision_comment_body(QUESTION)

    assert body == "**Needs a decision:** " + INERT_QUESTION
    assert body == block_record.render_needs_decision(QUESTION)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({}, id="neither"),
        pytest.param({"until": date(2026, 10, 9), "on": ["721"]}, id="both"),
    ],
)
def test_a_hold_still_names_exactly_one_condition(kwargs):
    with pytest.raises(ValueError):
        funnel._hold_comment_body("Hold it.", **kwargs)


# -- round trips through the real loader ---------------------------------------

def ticket_item():
    return funnel.Item(
        repo="nateprich/beta", number=42, title="A ticket",
        url="https://github.com/nateprich/beta/issues/42", state="OPEN",
        item_id="project-item-42", origin="agent", risk="standard",
        needs="none", parent="nateprich/beta#1",
    )


def shaped_project():
    return funnel.Item(
        repo="nateprich-projects/command-center", number=2003,
        title="A Shaped project",
        url="https://github.com/nateprich-projects/command-center/issues/2003",
        state="OPEN", body="# Decision\nKeep current service plan.\n",
        status="Shaped", klass="New", origin="Nate", needs="none",
        item_id="project-item-2003",
    )


def finished_project():
    """An open Building project, every ticket closed, that waits at Accept."""
    return funnel.Item(
        repo="nateprich/beta", number=42, title="A finished project",
        url="https://github.com/nateprich/beta/issues/42", state="OPEN",
        status="Building", klass="New", origin="Nate", risk="standard",
        needs="none", item_id="project-item-42",
        children_total=3, children_done=3,
    )


def _record_gh(monkeypatch, items):
    """Serve ``items`` as the Project and record every gh call made."""
    monkeypatch.setattr(funnel, "load_items", lambda: items)
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: None,
    )
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    return calls


def _posted(calls):
    """The body of the one ``gh issue comment`` call."""
    comments = [call for call in calls if call[:3] == ("gh", "issue", "comment")]
    assert len(comments) == 1
    return comments[0][-1]


def _load(monkeypatch, item, posted):
    """Read ``posted`` back through the loader, after an older prose note."""
    rows = [
        {"author": OWNER, "body": "An older prose note.",
         "createdAt": "2026-10-02T10:00:00Z"},
        {"author": OWNER, "body": posted,
         "createdAt": "2026-10-02T11:00:00Z"},
    ]
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": rows})
    funnel._load_block_comment(item)


def test_comment_blocked_on_reads_back_its_references(monkeypatch):
    item = ticket_item()
    calls = _record_gh(monkeypatch, [item])

    assert funnel.main([
        "comment", "42", "--blocked-on", "77", "--blocked-on", "#78",
        "--because", REASON, "--voice", "agent",
        "--run", "run-block", "--agent", "codex",
    ]) == 0

    posted = _posted(calls)
    assert funnel._visible_comment(posted) == (
        "**Blocked on #77 and #78:** " + INERT_REASON)
    _load(monkeypatch, item, posted)
    assert item.block_comments_error is None
    assert item.block_references == ["#77", "#78"]
    assert item.blocked_until is None
    assert item.block_event is None
    assert funnel._visible_comment(item.block_reason) == INERT_REASON
    assert item.unparseable_block_comments == []
    assert item.needs_decision is None
    assert funnel._event_block_mismatch(item) is None


def test_comment_blocked_on_shaped_writes_a_routed_hold_record(monkeypatch):
    item = shaped_project()
    calls = _record_gh(monkeypatch, [item])
    project_writes = []
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: project_writes.append(
            (item_id, field, value, ref)),
    )
    proof = (
        "https://github.com/nateprich-projects/command-center/issues/2003"
        "#issuecomment-5945296610"
    )

    assert funnel.main([
        "comment", "2003",
        "--blocked-on", "1590", "--blocked-on", "1997",
        "--because", "Hold until the open prerequisites clear",
        "--proof", proof, "--voice", "nate-relayed",
        "--run", "run-hold", "--agent", "codex",
    ]) == 0

    posted = _posted(calls)
    expected = {
        "Hold-Reason": "Hold until the open prerequisites clear",
        "Hold-Conditions": [
            "nateprich-projects/command-center#1590",
            "nateprich-projects/command-center#1997",
        ],
        "Plan-Version": (
            "371883196b99396eca7d9b9cb6613ec0395df5ca683d36fe7415b71343e40dbb"
        ),
        "Proof": [proof],
    }
    assert block_record.parse_shaped_hold_comment(
        funnel._visible_comment(posted)) == expected
    assert project_writes == [
        ("project-item-2003", "Needs", "external-event", item.ref),
    ]
    assert item.needs == "external-event"
    assert item.is_blocked
    _load(monkeypatch, item, posted)
    assert item.shaped_hold == expected


def test_shaped_hold_comment_requires_the_owner_voice_before_writing(
        monkeypatch, capsys):
    item = shaped_project()
    calls = _record_gh(monkeypatch, [item])
    proof = (
        "https://github.com/nateprich-projects/command-center/issues/2003"
        "#issuecomment-5945296610"
    )

    assert funnel.main([
        "comment", "2003", "--blocked-on", "1590",
        "--because", "Hold until the open prerequisite clears",
        "--proof", proof, "--voice", "agent",
        "--run", "run-hold", "--agent", "codex",
    ]) == 2

    assert calls == []
    assert "Nate-relayed" in capsys.readouterr().err


@pytest.mark.parametrize(
    "condition, references, until_days",
    [
        pytest.param(["--until"], [], 7, id="until"),
        pytest.param(["--on", "721", "#722"], ["#721", "#722"], None,
                     id="on"),
    ],
)
def test_a_hold_reads_back_its_condition_and_is_held(
        monkeypatch, condition, references, until_days):
    item = finished_project()
    calls = _record_gh(monkeypatch, [item])
    until = None
    if until_days is not None:
        until = funnel._block_condition_date() + timedelta(days=until_days)
        condition = condition + [until.isoformat()]

    assert funnel.main(
        ["hold", "42"] + condition + [
            "--reason", REASON, "--yes",
            "--run", "run-hold", "--agent", "claude",
        ]) == 0

    posted = _posted(calls)
    assert "\n" not in funnel._visible_comment(posted)
    assert "<!--" not in funnel._visible_comment(posted)
    _load(monkeypatch, item, posted)
    assert item.is_blocked
    assert item.block_references == references
    assert item.blocked_until == until
    assert item.block_event is None
    assert funnel._visible_comment(item.block_reason) == INERT_REASON
    assert item.unparseable_block_comments == []
    # Still a hold at Accept (#1677, #1724), not blocked work.
    assert funnel.is_held_at_accept(item)
    assert funnel.gate_question(item) is None


def test_comment_needs_decision_reads_back_its_question(monkeypatch):
    item = ticket_item()
    calls = _record_gh(monkeypatch, [item])

    assert funnel.main([
        "comment", "42", "--needs-decision", QUESTION, "--voice", "agent",
        "--run", "run-decision", "--agent", "codex",
    ]) == 0

    posted = _posted(calls)
    assert funnel._visible_comment(posted) == (
        "**Needs a decision:** " + INERT_QUESTION)
    _load(monkeypatch, item, posted)
    assert item.needs_decision == INERT_QUESTION
    assert item.block_references == []
    assert item.unparseable_block_comments == []


def test_breakdown_apply_question_reads_back_its_question(monkeypatch):
    calls = []

    def run_gh(command, **kwargs):
        calls.append(tuple(command))
        stdout = ""
        if command[1:3] == ["project", "item-add"]:
            stdout = json.dumps({"id": "project-item-42"})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    fields = []
    monkeypatch.setattr(funnel, "_run_gh", run_gh)
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: fields.append(
            (item_id, field, value, ref)),
    )

    breakdown.apply_question(
        "nateprich/beta", 42, QUESTION, run="run-breakdown", agent="muse")

    posted = _posted(calls)
    assert funnel._visible_comment(posted) == (
        block_record.render_needs_decision(QUESTION))
    assert fields == [
        ("project-item-42", "Needs", "human", "nateprich/beta#42")]
    assert calls[-1] == (
        "gh", "issue", "edit", "42", "--repo", "nateprich/beta",
        "--add-label", "blocked",
    )
    item = funnel.Item(
        repo="nateprich/beta", number=42, title="A plan", url="",
        state="OPEN", status="Ready", labels=["blocked"],
    )
    _load(monkeypatch, item, posted)
    assert item.needs_decision == INERT_QUESTION
    assert item.unparseable_block_comments == []
