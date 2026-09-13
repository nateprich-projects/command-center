"""The human-step ticket marker and its fail-closed parser."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


@pytest.mark.parametrize("reason", funnel.HUMAN_STEP_REASONS)
def test_each_access_reason_parses(reason):
    assert funnel.parse_human_step(
        "{}{}".format(funnel.HUMAN_STEP_PREFIX, reason)
    ) == reason


def test_machine_local_reason_parses_as_the_middle_capability_outcome():
    assert funnel.parse_human_step(
        funnel.HUMAN_STEP_PREFIX + funnel.MACHINE_LOCAL_REASON
    ) == funnel.MACHINE_LOCAL_REASON


def test_unmarked_and_marked_bodies_cover_all_three_capability_outcomes():
    assert funnel.parse_human_step("Implement the bounded change.") is None
    assert funnel.parse_human_step(
        funnel.HUMAN_STEP_PREFIX + funnel.MACHINE_LOCAL_REASON
    ) == funnel.MACHINE_LOCAL_REASON
    assert funnel.parse_human_step(
        funnel.HUMAN_STEP_PREFIX + funnel.HUMAN_STEP_REASONS[0]
    ) == funnel.HUMAN_STEP_REASONS[0]


def test_human_and_machine_local_items_use_separate_allowlists():
    machine_local = funnel.Item(
        repo="nateprich/beta", number=20, title="Run Claude locally",
        url="https://example.invalid/20", state="OPEN",
        parent="nateprich/beta#19",
        body=funnel.HUMAN_STEP_PREFIX + funnel.MACHINE_LOCAL_REASON.upper(),
    )
    human = funnel.Item(
        repo="nateprich/beta", number=21, title="Create the account",
        url="https://example.invalid/21", state="OPEN",
        parent="nateprich/beta#19",
        body=funnel.HUMAN_STEP_PREFIX + funnel.HUMAN_STEP_REASONS[0],
    )

    assert funnel.human_step_items([machine_local, human]) == [human]
    assert funnel.machine_local_step_items([machine_local, human]) == [
        machine_local
    ]


@pytest.mark.parametrize(
    ("reason", "actionable", "blocked"),
    [
        (
            funnel.HUMAN_STEP_REASONS[0],
            funnel.human_step_items,
            funnel.blocked_human_step_items,
        ),
        (
            funnel.MACHINE_LOCAL_REASON,
            funnel.machine_local_step_items,
            funnel.blocked_machine_local_step_items,
        ),
    ],
)
def test_marked_steps_split_by_native_and_parent_blockers(
    reason, actionable, blocked
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
        body=funnel.HUMAN_STEP_PREFIX + reason,
        open_blockers=[native_blocker.ref],
    )
    own_marker = funnel.Item(
        repo="nateprich/beta", number=34, title="Own blocked marker",
        url="https://example.invalid/34", state="OPEN",
        parent=parent.ref,
        body=funnel.HUMAN_STEP_PREFIX + reason,
        labels=["blocked"], block_references=["#88"],
    )
    parent_marker = funnel.Item(
        repo="nateprich/beta", number=35, title="Blocked parent",
        url="https://example.invalid/35", state="OPEN",
        parent=blocked_parent.ref,
        body=funnel.HUMAN_STEP_PREFIX + reason,
    )
    control = funnel.Item(
        repo="nateprich/beta", number=36, title="Actionable control",
        url="https://example.invalid/36", state="OPEN",
        parent=parent.ref,
        body=funnel.HUMAN_STEP_PREFIX + reason,
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


def test_difficulty_is_not_a_human_step_reason():
    assert funnel.parse_human_step(
        funnel.HUMAN_STEP_PREFIX + "this is hard"
    ) is None


def test_a_body_without_the_marker_returns_none():
    assert funnel.parse_human_step("Implement the bounded change.\nRisk: standard") \
        is None


def test_an_embedded_marker_does_not_match():
    body = "The ticket says {}{} in the middle of a sentence.".format(
        funnel.HUMAN_STEP_PREFIX, funnel.HUMAN_STEP_REASONS[0]
    )
    assert funnel.parse_human_step(body) is None


@pytest.mark.parametrize("reason", funnel.HUMAN_STEP_REASONS)
def test_a_block_reason_can_match_the_allowlist(reason):
    assert funnel.matching_human_step_reason(
        "**Blocked:** Human step: {}".format(reason)
    ) == reason


def test_suspected_human_step_requires_a_blocked_child_without_references():
    suspected = funnel.Item(
        repo="nateprich/beta", number=10, title="Provision the token",
        url="https://example.invalid/10", state="OPEN",
        labels=["blocked"], parent="nateprich/beta#9",
        block_reason="Human step: entering a credential",
    )
    named = funnel.Item(
        repo="nateprich/beta", number=11, title="Wait for the token",
        url="https://example.invalid/11", state="OPEN",
        labels=["blocked"], parent="nateprich/beta#9",
        block_references=["#77"],
        block_reason="Human step: entering a credential",
    )

    assert funnel.suspected_human_step_reason(suspected) == \
        "entering a credential"
    assert funnel.suspected_human_step_reason(named) is None


def test_an_unparseable_block_line_can_surface_a_human_step():
    item = funnel.Item(
        repo="nateprich/beta", number=12, title="Configure the account",
        url="https://example.invalid/12", state="OPEN",
        labels=["blocked"], parent="nateprich/beta#9",
        unparseable_block_comments=[
            "**Blocked on #77, 2026-09-07.** Human step: an account or billing setting"
        ],
    )

    assert funnel.suspected_human_step_reason(item) == \
        "an account or billing setting"


def test_closed_human_step_marks_its_project_as_ever_carried():
    project = funnel.Item(
        repo="nateprich/beta", number=1, title="Project",
        url="https://example.invalid/1", state="OPEN", status="Building",
        klass="Broken",
    )
    closed_human_step = funnel.Item(
        repo="nateprich/beta", number=2, title="Configure the account",
        url="https://example.invalid/2", state="CLOSED", parent=project.ref,
        body="Human step: an account or billing setting",
    )
    ordinary_project = funnel.Item(
        repo="nateprich/beta", number=3, title="Other project",
        url="https://example.invalid/3", state="OPEN", status="Building",
        klass="Broken",
    )

    funnel.mark_projects_that_carried_human_steps(
        [project, closed_human_step, ordinary_project]
    )

    assert project.carried_human_step
    assert not ordinary_project.carried_human_step
