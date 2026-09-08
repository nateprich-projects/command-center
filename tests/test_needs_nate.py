"""Authority signals that make an all-clear Needs section untrustworthy.

The fixtures are trimmed from real plans in #82, #83 and #84. The scan must
notice authority claims rather than every mention of a gate, lock or agent.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import needs_nate_signals  # noqa: E402


# -- trimmed real-plan fixtures ----------------------------------------------

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

PLAN_84 = """
Condition 1 is self-certification. #77 builds plan_needs_nate() to read the
plan's own sections and body. The agent writes the section; a function then
reads it to decide whether the agent may skip Nate.

That is #31's failure shape at a gate: an agent's unverifiable claim about its
own work, treated as fact by the thing that acts on it.

The check refuses a self-approval and does not add a decision he must answer
on the happy path. The plan also records why unattended approvals are visible.
"""


def test_issue_83_is_caught_for_citing_policy_on_membership_gate():
    assert needs_nate_signals(PLAN_83) == ["policy authority"]


def test_issue_82_gate_subject_matter_is_not_an_authority_claim():
    assert needs_nate_signals(PLAN_82) == []


def test_issue_84_is_caught_for_unattended_and_gate_authority():
    assert needs_nate_signals(PLAN_84) == [
        "gate authority",
        "unattended authority",
    ]


def test_each_signal_needs_authority_shaped_context():
    cases = [
        ("This changes the gate's question and who answers it.",
         ["gate authority"]),
        ("The agent may approve the item unattended.",
         ["unattended authority"]),
        ("Only Nate may set the Status field used by the gate.",
         ["field authority"]),
        ("plan.md:417 makes membership itself a gate.",
         ["policy authority"]),
    ]
    for body, expected in cases:
        assert needs_nate_signals(body) == expected


def test_document_mentions_and_gate_subject_matter_stay_clear():
    body = """
    Read plan.md for context. AGENTS.md contains the repository instructions.
    The plan discusses the gate and the Status field as subject matter, but it
    changes neither and grants no additional authority to an agent.
    """
    assert needs_nate_signals(body) == []


def test_empty_or_missing_body_has_no_signals():
    assert needs_nate_signals("") == []
    assert needs_nate_signals(None) == []
