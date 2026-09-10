"""Pure plan checks used by the unattended shaping rule."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import (  # noqa: E402
    plan_is_escalated,
    plan_needs_nate,
    shaped_plan_status,
)


PLAN_82 = """
The defect is that capture, review and merge silently default to one repo.
With a second member repo, those become wrong writes: an idea lands here, a
verdict is recorded against the wrong PR, and merge points at the wrong
repository. Nothing in the output says which repo was acted on.

Why this is separate: #18's plan was approved at a gate on 2026-09-06.
Widening an approved plan by editing its body bypasses that gate, which is the
drift #57 is being built to detect.
"""

PLAN_83 = """
Everything required to onboard a repo exists only as prose in a closed issue.
Membership is itself a gate: in the funnel is a commitment; out of it means
still deciding what to do with it.

The questions shaping would need to answer include whether an incompletely
onboarded repo may be refused entry. Per plan.md:417, membership itself is a
gate, and plan.md:740 keeps applying topics and transferring issues with him.
"""


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


def test_a_body_authority_signal_is_advisory_with_an_all_clear_section():
    plan = """
## Context
Per plan.md:417, membership itself is a gate.

## Needs you
Nothing.
    """
    assert shaped_plan_status(plan) == ("Ready", "plan declares nothing open")
    assert plan_needs_nate(plan) is False
    assert plan_needs_nate(plan.replace("Per plan.md:417, ", "")) is False


def test_real_plan_fixtures_are_checked_after_an_all_clear_section():
    assert plan_needs_nate(PLAN_83 + "\n## Needs you\nNothing.\n") is False
    assert plan_needs_nate(PLAN_82 + "\n## Needs you\nNothing.\n") is False


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


def _needs_plan(lines, heading="## Needs you"):
    return "{}\n\n{}\n".format(heading, "\n".join(lines))


@pytest.mark.parametrize(
    ("issue", "plan"),
    (
        (
            131,
            _needs_plan(
                [
                    "- Exposure: nothing outstanding. read-only inspection.",
                    "- Gates: nothing outstanding. no approval changes.",
                    "- Scope and priority: nothing outstanding. one run.",
                    "- Preference: nothing outstanding. no user-facing surface.",
                ],
                heading="### Needs Nate",
            ),
        ),
        (
            239,
            _needs_plan(
                [
                    "- Exposure. Nothing outstanding. existing credential only.",
                    "- Gates. Nothing outstanding. usage gate is untouched.",
                    "- Scope and priority. Nothing outstanding. one measurement.",
                    "- Preference. Nothing outstanding. use response headers.",
                ],
            ),
        ),
        (
            429,
            _needs_plan(
                [
                    "Exposure. Nothing outstanding. no new surface.",
                    "Gates. Nothing outstanding; leave the usage gate alone.",
                    "Scope and priority. None — both halves are filed.",
                    "Preference. nothing - fail fast.",
                ],
            ),
        ),
        (
            460,
            _needs_plan(
                [
                    "**Preference:** none. no naming decision.",
                    "Scope and priority: nothing outstanding. documented-command fix.",
                    "**Gates:** nothing outstanding. no approval change.",
                    "Exposure: NOTHING OUTSTANDING — no new surface.",
                ],
            ),
        ),
    ),
)
def test_shaped_status_accepts_per_category_all_clear_fixtures(issue, plan):
    assert shaped_plan_status(plan) == ("Ready", "plan declares nothing open")


@pytest.mark.parametrize(
    ("issue", "answer", "reason"),
    (
        (462, "one recorded item. name the guarded edit.", "open question under Gates"),
        (463, "undecided — the scope answer controls exposure.", "open question under Exposure"),
        (
            245,
            "nothing outstanding beyond this gate itself — no gate definitions change.",
            "open question under Gates",
        ),
    ),
)
def test_shaped_status_names_the_open_category(issue, answer, reason):
    lines = [
        "Exposure: nothing outstanding. no new surface.",
        "Gates: nothing outstanding. no gate change.",
        "Scope and priority: nothing outstanding. filed scope.",
        "Preference: nothing outstanding. no taste decision.",
    ]
    category = {
        462: "Gates",
        463: "Exposure",
        245: "Gates",
    }[issue]
    category_index = {
        "Exposure": 0,
        "Gates": 1,
        "Scope and priority": 2,
        "Preference": 3,
    }
    lines[category_index[category]] = (
        "{}: {}".format(category, answer)
    )
    assert shaped_plan_status(_needs_plan(lines)) == ("Shaped", reason)


def test_shaped_status_names_a_missing_category():
    plan = _needs_plan(
        [
            "Exposure: nothing outstanding.",
            "Gates: nothing outstanding.",
            "Preference: nothing outstanding.",
        ]
    )

    assert shaped_plan_status(plan) == (
        "Shaped",
        "open question under Scope and priority",
    )


def test_shaped_status_rejects_extra_prose_inside_a_category_section():
    plan = _needs_plan(
        [
            "Exposure: nothing outstanding.",
            "Gates: nothing outstanding.",
            "This is an unlabelled note.",
            "Scope and priority: nothing outstanding.",
            "Preference: nothing outstanding.",
        ]
    )

    assert shaped_plan_status(plan) == ("Shaped", "plan has an open question")


# -- a wrapped elaboration is still one answer (#524) -------------------------

WRAPPED_514 = """\
# Plan

## Needs you

- Exposure: nothing outstanding. No new credentials or reachable surface; the
  z.ai key stays in its existing keychain entry.
- Gates: nothing outstanding. No gate ownership changes; the snapshot stays
  documentary and never fatal, and the pace gate itself is untouched.
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.
"""


def test_a_wrapped_elaboration_is_still_clear():
    """#514's section verbatim: Muse wraps at eighty columns."""
    assert shaped_plan_status(WRAPPED_514) == ("Ready", "plan declares nothing open")


def test_a_wrapped_open_question_stays_open():
    plan = WRAPPED_514.replace(
        "- Gates: nothing outstanding. No gate ownership changes; the snapshot stays\n"
        "  documentary and never fatal, and the pace gate itself is untouched.\n",
        "- Gates: who may write Ready for\n  an all-clear plan?\n",
    )
    assert shaped_plan_status(plan) == ("Shaped", "open question under Gates")


def test_an_unindented_extra_line_stays_open():
    plan = WRAPPED_514 + "Also: the reviewer should decide the wording.\n"
    assert shaped_plan_status(plan) == ("Shaped", "plan has an open question")


def test_a_nested_bullet_is_not_a_continuation():
    plan = WRAPPED_514.replace(
        "- Preference: nothing outstanding. No user-facing choice remains.\n",
        "- Preference: nothing outstanding.\n  - except the label colour, which is open\n",
    )
    assert shaped_plan_status(plan) == ("Shaped", "plan has an open question")
