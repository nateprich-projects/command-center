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
