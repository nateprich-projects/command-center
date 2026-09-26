"""The Needs field is the only capability signal (#826)."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def test_human_and_machine_local_items_read_the_needs_field():
    machine_local = funnel.Item(
        repo="nateprich/beta", number=20, title="Run Claude locally",
        url="https://example.invalid/20", state="OPEN",
        parent="nateprich/beta#19",
        needs="claude-code-environment",
    )
    human = funnel.Item(
        repo="nateprich/beta", number=21, title="Create the account",
        url="https://example.invalid/21", state="OPEN",
        parent="nateprich/beta#19",
        needs="human",
    )
    ordinary = funnel.Item(
        repo="nateprich/beta", number=22, title="Implement the change",
        url="https://example.invalid/22", state="OPEN",
        parent="nateprich/beta#19",
        needs="none",
    )
    unset = funnel.Item(
        repo="nateprich/beta", number=23, title="Legacy ticket",
        url="https://example.invalid/23", state="OPEN",
        parent="nateprich/beta#19",
    )

    assert funnel.human_step_items(
        [machine_local, human, ordinary, unset]) == [human]
    assert funnel.machine_local_step_items(
        [machine_local, human, ordinary, unset]) == [machine_local]


def test_body_text_naming_a_human_step_is_not_read():
    """Field-only: prose mentioning the old marker changes nothing (#826)."""
    prose = funnel.Item(
        repo="nateprich/beta", number=24, title="Describe the marker",
        url="https://example.invalid/24", state="OPEN",
        parent="nateprich/beta#19",
        body="Human step: entering a credential",
        needs="none",
    )

    assert funnel.human_step_items([prose]) == []
    assert funnel.machine_local_step_items([prose]) == []


@pytest.mark.parametrize(
    ("needs", "actionable", "blocked"),
    [
        (
            "human",
            funnel.human_step_items,
            funnel.blocked_human_step_items,
        ),
        (
            "claude-code-environment",
            funnel.machine_local_step_items,
            funnel.blocked_machine_local_step_items,
        ),
    ],
)
def test_needs_steps_split_by_native_and_parent_blockers(
    needs, actionable, blocked
):
    parent = funnel.Item(
        repo="nateprich/beta", number=30, title="Project",
        url="https://example.invalid/30", state="OPEN", status="Building",
        klass="New",
    )
    blocked_parent = funnel.Item(
        repo="nateprich/beta", number=31, title="Blocked project",
        url="https://example.invalid/31", state="OPEN", status="Building",
        klass="New", labels=["blocked"],
    )
    native_blocker = funnel.Item(
        repo="nateprich/beta", number=32, title="Prerequisite",
        url="https://example.invalid/32", state="OPEN",
    )
    open_edge = funnel.Item(
        repo="nateprich/beta", number=33, title="Open native edge",
        url="https://example.invalid/33", state="OPEN",
        parent=parent.ref,
        needs=needs,
        open_blockers=[native_blocker.ref],
    )
    own_marker = funnel.Item(
        repo="nateprich/beta", number=34, title="Own blocked marker",
        url="https://example.invalid/34", state="OPEN",
        parent=parent.ref,
        needs=needs,
        labels=["blocked"], block_references=["#88"],
    )
    parent_marker = funnel.Item(
        repo="nateprich/beta", number=35, title="Blocked parent",
        url="https://example.invalid/35", state="OPEN",
        parent=blocked_parent.ref,
        needs=needs,
    )
    control = funnel.Item(
        repo="nateprich/beta", number=36, title="Actionable control",
        url="https://example.invalid/36", state="OPEN",
        parent=parent.ref,
        needs=needs,
    )
    items = [
        control, parent_marker, own_marker, open_edge,
        native_blocker, blocked_parent, parent,
    ]

    by_ref = {item.ref: item for item in items}
    assert funnel.blocked_step_reason(open_edge, by_ref) == \
        "open native blockers"
    assert funnel.blocked_step_reason(own_marker, by_ref) == \
        "ticket carries blocked marker"
    assert funnel.blocked_step_reason(parent_marker, by_ref) == \
        "parent carries blocked marker"
    assert funnel.blocked_step_reason(control, by_ref) is None

    assert [item.number for item in actionable(items)] == [36]
    assert [item.number for item in blocked(items)] == [33, 34, 35]
