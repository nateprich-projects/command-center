"""Judge restraint for description-named protected seams (#2515, ticket #2517).

Pre-hand-edit #2489 worded its seam by description: the ticket asked for
breakdown-guidance work without spelling ``skills/breakdown/SKILL.md``.
``precheck_protected`` (explicit-path-only ``ticket_asks``) then rejected any
diff touching the file, while the model judge demanded the guidance work as
``requirement unmet: Breakdown guidance states...``, so no diff could pass.

The agreement is restored on the judge side: demands on protected ground
the ticket does not admit are excluded from the requirements, while
``precheck_protected`` and ``ticket_asks`` stay unchanged.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import review  # noqa: E402

SEAM = "skills/breakdown/SKILL.md"

# Pre-hand-edit #2489 wording: names the seam by description only. Neither
# the path nor its ``skills/`` rule appears, so ``ticket_asks`` stays False.
DESCRIPTION_TICKET = {
    "title": "Breakdown sizing misfires on single-file tickets",
    "body": ("Parent: #2515.\n\nWhat: fix the breakdown sizing so small "
             "tickets get a right-sized plan.\n\nThe breakdown guidance "
             "states sizing rules the writer ignores; make the writer "
             "follow them.\n\nRisk: standard"),
}

# Post-hand-edit wording: the same seam spelled out, so ``ticket_asks`` holds.
EXPLICIT_TICKET = {
    "title": "Breakdown sizing misfires on single-file tickets",
    "body": ("Parent: #2515.\n\nWhat: fix the breakdown sizing so small "
             "tickets get a right-sized plan.\n\nThe breakdown guidance "
             "states sizing rules the writer ignores; record the sizing "
             "rule in skills/breakdown/SKILL.md and make the writer follow "
             "it.\n\nRisk: standard"),
}

# A lister demand phrased like the observed judge rejection: it wants the
# protected guidance work without spelling the path.
GUIDANCE_DEMAND = ("Breakdown guidance states the sizing rule for small "
                   "tickets; the writer follows it")
ORDINARY_DEMAND = ("The writer sizes single-file tickets to a right-sized "
                   "plan in engine/breakdown.py")


def test_ticket_asks_admits_only_a_spelled_out_path():
    """engine/review.py ticket_asks is explicit-path-only.

    Predicate (engine/review.py ``ticket_asks``): ``if path in text`` on the
    title/body text, else the touched path's protected rule in the text.
    Description prose never admits.
    """
    assert review.ticket_asks(DESCRIPTION_TICKET, SEAM) is False
    assert review.ticket_asks(EXPLICIT_TICKET, SEAM) is True


def test_precheck_still_blocks_an_unasked_protected_touch():
    """Negative 1: vague prose never authorizes a protected edit."""
    reasons = review.precheck_protected({
        "ticket": DESCRIPTION_TICKET,
        "protected": {"touched": [SEAM]},
    })
    assert reasons == [
        "protected: {} touched but the ticket does not ask for it".format(
            SEAM)]


def test_precheck_passes_a_diff_that_leaves_the_seam_alone():
    packet = {
        "ticket": DESCRIPTION_TICKET,
        "protected": {"touched": []},
    }
    assert review.precheck_protected(packet) == []


def test_restraint_drops_the_unadmitted_guidance_demand():
    kept = review.restrain_protected_demands(
        [GUIDANCE_DEMAND, ORDINARY_DEMAND], DESCRIPTION_TICKET)
    assert kept == [ORDINARY_DEMAND]


def test_restraint_keeps_a_demand_the_ticket_admits():
    """Negative 2: the judge still requires guidance the ticket did intend."""
    kept = review.restrain_protected_demands(
        [GUIDANCE_DEMAND, ORDINARY_DEMAND], EXPLICIT_TICKET)
    assert kept == [GUIDANCE_DEMAND, ORDINARY_DEMAND]


def test_restraint_keeps_literal_demands_the_ticket_admits():
    demand = "Record the sizing rule in skills/breakdown/SKILL.md"
    assert review.restrain_protected_demands(
        [demand], EXPLICIT_TICKET) == [demand]
    assert review.restrain_protected_demands(
        [demand], DESCRIPTION_TICKET) == []


def test_restraint_never_drops_does_not_break_rows():
    row = "Does not break: keep skills/breakdown/SKILL.md sizing stable"
    assert review.restrain_protected_demands(
        [row], DESCRIPTION_TICKET) == [row]


def test_one_diff_passes_both_gates_after_restraint():
    """The wedged pair, replayed: a diff outside the seam passes precheck,
    and the restrained demands leave the judge nothing unmeetable."""
    assert review.precheck_protected({
        "ticket": DESCRIPTION_TICKET,
        "protected": {"touched": []},
    }) == []
    kept = review.restrain_protected_demands(
        [GUIDANCE_DEMAND, ORDINARY_DEMAND], DESCRIPTION_TICKET)
    assert GUIDANCE_DEMAND not in kept
    assert kept == [ORDINARY_DEMAND]
