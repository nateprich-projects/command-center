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

from funnel import escalation_reasons, required_tier  # noqa: E402


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


def test_rejected_risky_alternatives_still_escalate_a_plan():
    """A false escalation is safer than silently approving a risky plan."""
    body = """## Decided
Use the existing environment.

## Rejected
Store credentials in the checkout instead.
"""
    assert escalation_reasons("Keep setup local", body) == ["credentials"]


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


def test_an_unmarked_ordinary_ticket_is_standard():
    assert required_tier("Add a column to the brief", "Print Class in cmd_ideas.") \
        == "standard"
