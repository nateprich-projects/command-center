"""The unattended shaping rule combines class, origin and plan checks."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LEGACY_BACKLOG = json.loads(
    (ROOT / "tests" / "fixtures" / "pre_marker_backlog.json").read_text()
)

from funnel import (  # noqa: E402
    effective_shape_owner,
    needs_nate_signals,
    self_approval_eligible,
    shaped_self_approvable,
)


def eligible(klass, origin, override=None, needs_nate=False, escalated=False):
    return self_approval_eligible(
        klass,
        origin,
        override,
        needs_nate=needs_nate,
        escalated=escalated,
    )


def all_clear_plan():
    return (
        "# Plan\n\n"
        "## Needs you\n"
        "Exposure: nothing outstanding. no new surface.\n"
        "Gates: nothing outstanding. no gate change.\n"
        "Scope and priority: nothing outstanding. bounded.\n"
        "Preference: nothing outstanding. no user-facing choice.\n"
    )


def shaped_eligible(body, origin, klass, risk=None):
    return shaped_self_approvable(
        body,
        {"voice": origin} if isinstance(origin, str) else origin,
        klass,
        [] if risk is None else risk,
    )


@pytest.mark.parametrize("origin", ["nate-direct", "nate-relayed"])
@pytest.mark.parametrize("klass", ["Broken", "Maintenance", "Improve"])
def test_nate_raised_existing_work_still_requires_nate(klass, origin):
    assert eligible(klass, origin) is False


@pytest.mark.parametrize(
    ("klass", "expected"),
    [
        ("Broken", True),
        ("Maintenance", True),
        ("Improve", True),
        ("New", False),
        ("Replace", False),
        (None, False),
    ],
)
def test_agent_observed_work_follows_the_existing_class_rule(klass, expected):
    assert eligible(klass, "agent") is expected


def test_authorised_override_changes_the_effective_origin_both_directions():
    assert eligible("Improve", "nate-relayed", "agents") is True
    assert eligible("Improve", None, "agents") is True
    assert eligible("Improve", "agent", "nate") is False


@pytest.mark.parametrize("origin", [None, "", "unknown", "agents"])
def test_absent_or_malformed_origin_is_not_agent_shapeable(origin):
    assert effective_shape_owner(origin) == "nate"
    assert eligible("Improve", origin) is False


def test_malformed_override_fails_closed_instead_of_using_agent_origin():
    assert eligible("Improve", "agent", "everybody") is False


@pytest.mark.parametrize("idea", LEGACY_BACKLOG, ids=lambda idea: idea["ref"])
def test_existing_pre_marker_backlog_resolves_to_nate(idea):
    assert effective_shape_owner(idea["origin"]) == "nate"
    assert eligible("Improve", idea["origin"]) is False


def test_origin_is_one_term_in_the_combined_self_approval_condition():
    authority_plan = "Only Nate may set the Status field used by the gate."
    authority_signals = needs_nate_signals(authority_plan)
    assert authority_signals == ["field authority"]

    assert eligible("Improve", "agent", needs_nate=bool(authority_signals)) is False
    assert eligible("Improve", "agent", escalated=True) is False
    assert eligible("Improve", "nate-relayed") is False
    assert eligible("Improve", "agent") is True


def test_shaped_predicate_matches_the_shared_ready_decision():
    assert shaped_eligible(all_clear_plan(), "agent", "Improve") is True
    assert shaped_eligible(all_clear_plan(), "nate-relayed", "Improve") is False
    assert shaped_eligible(all_clear_plan(), "agent", "New") is False
    assert shaped_eligible(
        all_clear_plan(), "agent", "Broken", ["declared: destructive"]
    ) is False


@pytest.mark.parametrize(
    ("body", "origin", "klass", "risk"),
    [
        (None, "agent", "Improve", []),
        (all_clear_plan(), None, "Improve", []),
        (all_clear_plan(), {"voice": "unknown"}, "Improve", []),
        (all_clear_plan(), "agent", None, []),
        (all_clear_plan(), "agent", "Improve", None),
        (all_clear_plan(), "agent", "Improve", ["bad", 1]),
    ],
)
def test_shaped_predicate_fails_closed_on_missing_or_malformed_input(
    body, origin, klass, risk
):
    assert shaped_self_approvable(body, origin, klass, risk) is False
