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
