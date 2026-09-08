"""Pure plan checks used by the unattended shaping rule."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import plan_is_escalated, plan_needs_nate  # noqa: E402


def test_a_question_in_the_needs_nate_section_requires_nate():
    plan = """
    ## Decided from precedent
    Use the existing command shape.

    ## Needs Nate
    Should the command also update the Project field?

    ## Rejected
    Add a second source of truth.
    """
    assert plan_needs_nate(plan) is True


def test_needs_nate_accepts_both_heading_spellings():
    for heading in ("Needs Nate", "Needs you"):
        assert plan_needs_nate("## {}\n\nNothing.\n\n## Rejected\nNo-op."
                               .format(heading)) is False


def test_a_bare_nothing_with_whitespace_is_empty():
    assert plan_needs_nate("## Needs Nate\n\n  Nothing  \n\n## Rejected\nNo-op.") is False
    assert plan_needs_nate("## Needs Nate\n\n   \n\n## Rejected\nNo-op.") is False


def test_a_missing_needs_section_fails_closed():
    assert plan_needs_nate("## Decided\nEverything follows precedent.") is True


def test_a_lower_level_heading_stays_inside_the_needs_section():
    plan = """
    ## Needs Nate
    ### Exposure
    Nothing.
    ## Rejected
    No-op.
    """
    assert plan_needs_nate(plan) is True


def test_plan_escalation_scans_the_whole_body_and_returns_reasons():
    plan = """
    ## Decided from precedent
    Take the single-in-motion lock and keep the gate vocabulary ordinary.

    ## Needs Nate
    Nothing.

    ## Rejected
    Allow destructive or irreversible operations unattended.
    """
    assert plan_is_escalated(plan) == ["destructive"]


def test_lock_and_gate_subject_matter_stays_standard():
    plan = """
    ## Decided from precedent
    The funnel takes the lock, checks the gate, and can park an issue.
    It does not change permissions or perform a migration.

    ## Needs Nate
    Nothing.
    """
    assert plan_is_escalated(plan) == []


def test_plan_escalation_preserves_the_shared_risk_marker_rule():
    plan = """
    Risk: standard
    ## Rejected
    Guard against a race condition in the spool.
    """
    assert plan_is_escalated(plan) == []
