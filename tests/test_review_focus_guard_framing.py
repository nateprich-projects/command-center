"""Guard-framed Review-focus Accept copy reads as must-not (#2570).

Parent plan #2567: a Review-focus failure-mode bullet copied verbatim into
ticket Accept was listed as a build requirement, so PR #2556 — a correct
withhold for the #2485 closed-but-unmerged-prerequisite focus line copied
into #2497 Accept — was rejected as doing the opposite. The fix frames each
copied line at breakdown time as ``Guard (must not happen):`` plus the
bullet verbatim, with no lister change and no sweep of already-copied
tickets (including the #2497 guards rewritten by funnel-watch push dfb98af).

Premise evidence (ticket gate — both hold, so the fix proceeds):
- engine/breakdown.py defines REVIEW_FOCUS_ACCEPT_RULE as
  "For each `## Review focus` bullet, put one `Accept` test in the ticket
  that owns that behavior. Prefix the copied line as `Guard (must not
  happen):` plus the bullet's text verbatim, exactly once."
- routines/muse-breakdown.md:29 carries the same copy rule:
  "For each `## Review focus` bullet, put one `Accept` test in its owning
  ticket; prefix it `Guard (must not happen):` plus the text verbatim, once."
- tests/test_decline_prerequisite_lift.py:100 defines
  test_closed_but_unmerged_pr_still_withheld, asserting withhold on a
  closed-but-unmerged PR via
  "assert funnel._decline_route_withholds_startability(item, by_ref) is True"
  with the fake API returning
  {"state": "closed", "merged": False, "merged_at": None}.

Fixture scope is the observed #2485 to #2497 pair only: no extra synthetic
variants. Every GitHub read here goes through fakes; nothing reaches GitHub.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import breakdown, review  # noqa: E402


#: The observed #2485 Review-focus failure-mode bullet, about a
#: closed-but-unmerged prerequisite, as copied into #2497 Accept.
OBSERVED_FOCUS_BULLET = (
    "A closed-but-unmerged prerequisite PR still withholds the ticket."
)

#: The breakdown-time framing this ticket mandates: the marker plus the
#: original bullet verbatim.
OBSERVED_GUARD_LINE = (
    "Guard (must not happen): " + OBSERVED_FOCUS_BULLET
)

REPO = "owner/repo"


def _parent():
    return funnel.Item(
        repo=REPO, number=1, title="parent", url="", state="OPEN",
        status="Ready", klass="Improve", needs="none", children_total=1,
    )


def _ticket(body, excerpt):
    route = {"type": "unsatisfiable-acceptance",
             "acceptance_digest": hashlib.sha256(
                 body.encode("utf-8")).hexdigest()}
    if excerpt is not None:
        route["decline_excerpt"] = excerpt
    parent = _parent()
    item = funnel.Item(
        repo=REPO, number=2, title="ticket", url="", state="OPEN",
        body=body, parent=parent.ref, needs="agent", risk="standard",
        decline_route=route,
    )
    return parent, item


def test_breakdown_rule_mandates_guard_prefix():
    """The breakdown copy path frames Review-focus lines as must-not."""
    assert "Guard (must not happen):" in breakdown.REVIEW_FOCUS_ACCEPT_RULE
    assert "verbatim" in breakdown.REVIEW_FOCUS_ACCEPT_RULE
    assert "exactly once" in breakdown.REVIEW_FOCUS_ACCEPT_RULE


def test_packet_sizing_carries_guard_rule_with_a_review_focus():
    """The #2485 to #2497 path: a plan Review focus adds the Guard rule."""
    body = ("# Plan\n\nDo the thing.\n\n## Review focus\n\n- {}\n"
            .format(OBSERVED_FOCUS_BULLET))
    packet = breakdown.build_packet(
        repo=REPO, number=1,
        plan={"title": "plan", "url": "", "body": body, "state": "OPEN"},
        siblings=[], sizing="sizing\n", collected_at="2026-10-14T00:00:00+00:00")
    assert "Guard (must not happen):" in packet["sizing_standard"]

    plain = breakdown.build_packet(
        repo=REPO, number=1,
        plan={"title": "plan", "url": "", "body": "# Plan\n\nDo it.\n",
              "state": "OPEN"},
        siblings=[], sizing="sizing\n", collected_at="2026-10-14T00:00:00+00:00")
    assert plain["sizing_standard"] == "sizing\n"


def test_observed_guard_line_carries_must_not_signal_and_stays_listed():
    """The framed #2497 Accept bullet is listed intact, as a must-not."""
    assert OBSERVED_GUARD_LINE.startswith("Guard (must not happen):")
    assert OBSERVED_GUARD_LINE.endswith(OBSERVED_FOCUS_BULLET)

    body = ("What: lift the decline when the prerequisite lands.\n\n"
            "Acceptance:\n"
            "- {}\n".format(OBSERVED_GUARD_LINE))
    lines = review._ticket_acceptance_lines(body)
    assert lines == [OBSERVED_GUARD_LINE]


def test_closed_but_unmerged_pr_still_withheld_for_observed_pair(monkeypatch):
    """The judge-verdict half: the #2497 shape withholds, not builds."""
    parent, item = _ticket(
        "What: do the thing.\n\nAcceptance: {}.".format(OBSERVED_GUARD_LINE),
        "Acceptance needed #9.")
    by_ref = {row.ref: row for row in (parent, item)}

    def fake_api(endpoint, **kwargs):
        assert endpoint == "repos/{}/pulls/9".format(REPO)
        return {"state": "closed", "merged": False, "merged_at": None}

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_existing_accept_requirements_still_list_as_build_requirements():
    """Unrelated Accept lines pass through with no marker rewrite."""
    line = "The decline lifts without a body edit when the prerequisite lands."
    body = "What: do the thing.\n\nAcceptance:\n- {}\n".format(line)
    assert review._ticket_acceptance_lines(body) == [line]
    assert not line.startswith("Guard (must not happen):")


def test_routine_and_rule_agree_on_the_guard_marker():
    """engine/breakdown.py and routines/muse-breakdown.md:29 name one marker."""
    routine = (ROOT / "routines" / "muse-breakdown.md").read_text()
    assert routine.count("Guard (must not happen):") == 1
    assert breakdown.REVIEW_FOCUS_ACCEPT_RULE.count(
        "Guard (must not happen):") == 1
