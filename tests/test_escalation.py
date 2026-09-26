"""Which engine may take a ticket — decided by the funnel, never by the model.

The cheap default engineer must not pick up auth, migrations, destructive work
or concurrency. `MIGRATION.md` P3a is explicit that the model must not decide
whether it deserves escalation, because a model asked "is this too hard for you"
answers from confidence rather than from risk.

The hard part is not the rule, it is the vocabulary. **This repository is about
locks, gates and destructive operations** — `funnel park` closes issues on
purpose and every routine takes a single-in-motion lock. Those ordinary terms
must stay standard, while the vocabulary plans use to describe genuinely risky
work must err toward escalation now that it is a safety boundary.

So: an explicit `Risk:` marker written at breakdown is authoritative, and the
pattern list is a safety boundary for tickets written before markers existed.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import (escalation_matches, escalation_reasons,
                    plan_is_escalated, required_tier)  # noqa: E402


# -- the marker is authoritative ----------------------------------------------

def test_a_declared_marker_escalates():
    reasons = escalation_reasons("Add funnel park",
                                 "Risk: escalated — destructive\nCloses issues.")
    assert reasons == ["declared: destructive"]
    assert required_tier("x", "Risk: escalated") == "escalated"


def test_a_declared_standard_beats_the_patterns():
    """The person who wrote the plan knew what the words meant. A regex did not."""
    body = "Risk: standard\nGuards against a race condition in the spool."
    assert escalation_reasons("Fix the heartbeat", body) == []
    assert required_tier("Fix the heartbeat", body) == "standard"


# -- the safety net, and what it must NOT catch -------------------------------

def test_the_net_catches_risk_in_this_repository_s_own_vocabulary():
    risky = [
        ("credentials", "Change the install",
         "Read credentials from the environment."),
        ("authorisation", "Change the unattended boundary",
         "Broaden an agent's permissions to write the gate."),
        ("data-migration", "Move the Project fields",
         "Migrate every existing item to the new options."),
        ("destructive", "Remove the command guard",
         "Allow destructive or irreversible operations unattended."),
        ("concurrency", "Repair heartbeat attribution",
         "Handle overlapping runs that write the same record."),
    ]
    for reason, title, body in risky:
        assert escalation_reasons(title, body) == [reason]


def test_shared_scan_keeps_rejected_text_but_plan_adapter_excludes_it():
    """Ticket scans keep their scope; plan headings belong in the adapter."""
    body = """## Decided
Use the existing environment.

## Rejected
Store credentials in the checkout instead.
"""
    assert escalation_reasons("Keep setup local", body) == ["credentials"]
    assert funnel.plan_escalation_matches(body) == []


def test_safe_exposure_and_subject_matter_prose_do_not_trip_the_net():
    """Category names and explicit absence are not descriptions of risky work."""
    ordinary = [
        ("No exposure change",
         "Exposure: nothing. No credential, nothing internet-reachable. "
         "This changes no credential and does not grant permissions."),
        ("Document the rollout",
         "Keep MIGRATION.md current and discuss migration as a topic."),
        ("Split the plan",
         "The tickets overlap in subject matter but touch different files."),
        ("Repair an executable bit",
         "Restore the file permissions in the checkout."),
        ("Report portfolio load",
         "There are 15 concurrent projects and one of 78 ever parked."),
    ]
    for title, body in ordinary:
        assert escalation_reasons(title, body) == []


def test_this_repository_s_own_vocabulary_does_not_escalate_everything():
    """The property the whole design turns on.

    Real ticket text from this funnel. If these escalate, the cheap engine never
    runs and the routing change achieves nothing while appearing to work.
    """
    ordinary = [
        ("funnel park <ref> --reason",
         "Sets Status to Parked and closes the issue with state_reason "
         "not_planned. Posts the reason as a comment behind PARK_COMMENT_PREFIX."),
        ("Surface parked items in funnel brief",
         "Add a parked list to cmd_brief's JSON. Fetch comments only for items "
         "already known to be Parked."),
        ("funnel doctor: check the GitHub wiring",
         "Check auth scope, Project fields and the topic."),
        ("Take the single-in-motion lock",
         "A Broken ticket may take the lock before the TTL expires."),
        ("Resolve a run's id from the records",
         "Refuse to guess when more than one run is open; delete the pointer."),
    ]
    for title, body in ordinary:
        assert required_tier(title, body) == "standard", \
            "{!r} would escalate on: {}".format(title, escalation_reasons(title, body))


# -- a failed attempt ---------------------------------------------------------

def test_a_prior_failure_escalates_even_a_standard_ticket():
    body = "Risk: standard\nplain bounded work"
    assert escalation_reasons("x", body, failed_before=True) == [
        "prior attempt failed"]
    assert required_tier("x", body, failed_before=True) == "escalated"


def test_a_failure_adds_to_declared_reasons_rather_than_replacing_them():
    reasons = escalation_reasons("x", "Risk: escalated — concurrency",
                                 failed_before=True)
    assert reasons == ["declared: concurrency", "prior attempt failed"]


def test_a_single_matching_sentence_returns_its_reason_and_line():
    sentence = "Migrate the ledger schema in place."
    assert escalation_matches("", sentence) == [
        {"reason": "data-migration", "line": sentence},
    ]


def test_escalation_matches_return_the_matching_line_per_reason():
    body = (
        "Read credentials from the environment.\n"
        "Migrate the ledger schema in place.\n"
    )
    assert escalation_matches("", body) == [
        {"reason": "credentials",
         "line": "Read credentials from the environment."},
        {"reason": "data-migration",
         "line": "Migrate the ledger schema in place."},
    ]


def test_an_explicit_risk_match_reports_the_marker_line():
    assert escalation_matches(
        "", "Risk: escalated — destructive\nRun the operation.\n"
    ) == [{
        "reason": "declared: destructive",
        "line": "Risk: escalated — destructive",
    }]


def test_an_unmarked_ordinary_ticket_is_standard():
    assert required_tier("Add a column to the brief", "Print Class in cmd_ideas.") \
        == "standard"


# -- plans use the same machinery with no separate title ----------------------

def test_plan_escalation_excludes_the_rejected_section():
    plan = (
        "## Decided from precedent\n"
        "Take the single-in-motion lock and keep the gate vocabulary ordinary.\n"
        "\n## Needs Nate\nNothing.\n"
        "\n## Rejected\n"
        "Allow destructive or irreversible operations unattended.\n"
    )
    assert funnel.plan_escalation_matches(plan) == []
    assert plan_is_escalated(plan) == []


def test_the_recorded_1503_shape_keeps_its_remaining_source_citation():
    """The Rejected backfill hit goes away; its precedent citation remains."""
    body = (FIXTURES / "escalation_plan_1503_recorded.md").read_text()

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": (
            "- The fix is forward-only and the 14 invalid-JSON lines and the "
            "lost escalated fire stand as the before-measurement. (source: "
            "sibling convention #1393 and #1182 no-backfill decisions)"
        ),
    }]


def test_a_genuine_backfill_proposal_outside_rejected_still_matches():
    body = "## Proposal\n\nBackfill the recent entries from the canonical source.\n"

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "Backfill the recent entries from the canonical source.",
    }]


def test_a_second_rejected_trigger_is_excluded():
    body = "## Rejected\n\nDelete the stale entries permanently.\n"

    assert funnel.plan_escalation_matches(body) == []


def test_a_mixed_plan_returns_only_its_genuine_trigger():
    body = (
        "## Proposal\n\nBackfill the recent entries from the canonical source.\n"
        "\n## Rejected\n\nDelete the stale entries permanently.\n"
    )

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "Backfill the recent entries from the canonical source.",
    }]


def test_rejected_section_includes_nested_headings_and_stops_at_higher_heading():
    body = (
        "## Rejected\n"
        "### Earlier approach\nDelete the stale entries permanently.\n"
        "#### Detail\nBackfill all historical rows.\n"
        "# Proposal\nBackfill the recent entries from the source.\n"
    )

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "Backfill the recent entries from the source.",
    }]


def test_a_malformed_rejected_heading_scans_the_whole_body():
    body = "##Rejected\n\nBackfill the recent entries from the source.\n"

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "Backfill the recent entries from the source.",
    }]


def test_a_tab_indented_rejected_heading_is_not_treated_as_a_section():
    body = "\t## Rejected\n\nBackfill the recent entries from the source.\n"

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "Backfill the recent entries from the source.",
    }]


def test_a_malformed_boundary_inside_rejected_fails_closed():
    body = (
        "## Rejected\n\nNothing will be backfilled.\n"
        "##Malformed\n\nBackfill the recent entries from the source.\n"
        "## Proposal\n\nKeep the original store.\n"
    )

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "Backfill the recent entries from the source.",
    }]


def test_agent_decision_rejected_clauses_do_not_create_scan_hits():
    body = (
        "## Decided by the agent\n\n"
        "- Use the existing records (rejected: backfill all historical rows; "
        "delete the old ledger permanently)\n"
    )

    assert funnel.plan_escalation_matches(body) == []


def test_agent_decision_scan_keeps_a_risk_in_the_chosen_decision():
    body = (
        "## Decided by the agent\n\n"
        "- Backfill recent entries (rejected: backfill all years; "
        "delete the old ledger permanently)\n"
    )

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": "- Backfill recent entries",
    }]


def test_rejected_wording_outside_agent_decisions_still_scans():
    body = (
        "## Notes\n\n"
        "- The rejected option was (rejected: backfill all historical rows; "
        "delete the old ledger permanently).\n"
    )

    assert funnel.plan_escalation_matches(body) == [{
        "reason": "data-migration",
        "line": (
            "- The rejected option was (rejected: backfill all historical "
            "rows; delete the old ledger permanently)."
        ),
    }]


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


# -- the scan reads asserted prose only (#1167, #1235) ----------------------
#
# Nate answered the gate question on 2026-09-21: skip quoted text, having been
# shown the cost — a plan that describes its real risk only inside a code
# fence would drop to the standard lane.

import re  # noqa: E402

import pytest  # noqa: E402

import funnel  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def scan_everything(text: str) -> list:
    """The pre-change scan: every pattern against the whole text.

    Kept here rather than in funnel.py so these fixtures can pin what the old
    behaviour was without leaving a second scanner in the shipped code.
    """
    return [name for name, pattern in sorted(funnel.ESCALATION_PATTERNS.items())
            if re.search(pattern, text, re.IGNORECASE)]


def test_the_recorded_1167_score_comes_entirely_from_quoted_evidence():
    """#1167 recorded that a report *about* the scan, with none of these
    properties, scored all four risks. This fixture reproduces that body's
    shape — pasted log lines and a rejected alternative in a block quote —
    because the issue itself has since been rewritten by shaping and GitHub
    exposes no body history, so the original text cannot be fetched."""
    body = (FIXTURES / "escalation_scan_1167_recorded.md").read_text()

    assert scan_everything(body) == [
        "authorisation", "concurrency", "data-migration", "destructive",
    ]
    assert funnel.escalation_reasons("", body) == []


def test_the_recorded_ff225_capture_scored_one_pasted_log_line():
    """FF-Weekly-Start-Sit#225 was captured with one line copied from an
    operator log and scored ['data-migration']; the plan written for the same
    work, without the pasted line, scored nothing. Same reconstruction: the
    capture body was replaced by the plan when the item was shaped."""
    body = (FIXTURES / "escalation_scan_ff225_capture.md").read_text()

    assert scan_everything(body) == ["data-migration"]
    assert funnel.escalation_reasons("", body) == []


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_a_fenced_block_is_not_scanned(fence):
    body = "A report.\n{}\nResource deadlock avoided\n{}\n".format(fence, fence)

    assert funnel.escalation_reasons("x", body) == []


def test_a_fence_with_an_info_string_still_opens_and_closes():
    assert funnel.escalation_reasons(
        "x", "A report.\n```text\nhard delete\n```\nNothing else.\n"
    ) == []


def test_a_tilde_fence_is_not_closed_by_backticks():
    assert funnel.escalation_reasons(
        "x", "~~~\nrewrite history\n```\nstill quoted\n~~~\n"
    ) == []


def test_a_block_quote_is_not_scanned():
    assert funnel.escalation_reasons(
        "x", "The plan says:\n\n> we will migrate the schema\n\nIt will not.\n"
    ) == []


def test_a_paired_inline_code_span_is_not_scanned():
    body = "The OS error was `Resource deadlock avoided`, not ours.\n"

    assert scan_everything(body) == ["concurrency"]
    assert funnel.escalation_reasons("x", body) == []
    assert funnel.required_tier("x", body) == "standard"
    assert funnel.plan_is_escalated(body) == []


def test_unclosed_or_multiline_inline_backticks_stay_searchable():
    unclosed = "The OS error was `Resource deadlock avoided, not ours.\n"
    multiline = "The OS error was `Resource deadlock\navoided`, not ours.\n"

    assert funnel.escalation_reasons("x", unclosed) == ["concurrency"]
    assert funnel.escalation_reasons("x", multiline) == ["concurrency"]


def test_real_prose_risk_outside_inline_code_still_matches():
    assert funnel.escalation_reasons(
        "x", "The worker can hit a race condition.\n"
    ) == ["concurrency"]


def test_everything_else_outside_the_three_regions_is_scanned_as_before():
    assert funnel.escalation_reasons(
        "x", "This step will migrate the schema in place.\n"
    ) == ["data-migration"]
    assert funnel.escalation_reasons(
        "x", "It runs destructive operations on the branch.\n"
    ) == ["destructive"]


def test_a_risk_sentence_after_a_fence_is_still_read():
    body = (
        "```\nResource deadlock avoided\n```\n\n"
        "This ticket will migrate the schema.\n"
    )

    assert funnel.escalation_reasons("x", body) == ["data-migration"]


def test_the_marker_still_outranks_the_regex_in_both_directions():
    assert funnel.escalation_reasons(
        "x", "Risk: standard\n\nThis will migrate the schema.\n"
    ) == []
    assert funnel.escalation_reasons(
        "x", "Risk: escalated — concurrency\n\nNothing risky in prose.\n"
    ) == ["declared: concurrency"]


def test_a_marker_inside_a_fence_is_not_a_marker():
    """It is being shown, like everything else in there."""
    assert funnel.escalation_reasons(
        "x", "```\nRisk: standard\n```\n\nThis will migrate the schema.\n"
    ) == ["data-migration"]


def test_lines_are_blanked_rather_than_deleted():
    """A marker must not be joined to the sentence above it."""
    assert funnel.asserted_text(
        "one\n```\ntwo\n```\nthree\n"
    ).splitlines() == ["one", "", "", "", "three"]


def test_an_unclosed_fence_quiets_the_rest_of_the_body():
    """Fail towards the cheap lane only where the author opened a fence and
    never closed it; the alternative is scanning a block they meant to quote."""
    assert funnel.escalation_reasons(
        "x", "```\nThis will migrate the schema.\n"
    ) == []


def test_a_negated_sentence_in_prose_still_matches_and_is_not_this_fix():
    """Two of the six recorded false positives are asserted prose that says
    the opposite — "authorises no lineup change", "performs no destructive
    operation". The negation lookaheads miss those forms, and narrowing the
    scanned region cannot help: the sentence is an assertion about the work.
    Pinned so the gap is recorded rather than assumed fixed."""
    assert funnel.escalation_reasons(
        "x", "This plan authorises no lineup change.\n"
    ) == ["authorisation"]
    assert funnel.escalation_reasons(
        "x", "It performs no destructive operation.\n"
    ) == ["destructive"]


def test_empty_and_missing_text_are_unchanged():
    assert funnel.asserted_text("") == ""
    assert funnel.escalation_reasons("", "") == []
