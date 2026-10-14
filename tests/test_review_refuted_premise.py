"""Route refuted base-premise lane findings to the ticket (#2549).

When the lane judge finds a ticket verify-first or reproduction premise
false about base behavior (observed shape: #2500 on reuse, #2492 on
claude-code-environment), the review ends routed to the ticket for
amend-or-send-back instead of a rejected verdict the implementer must fix,
so the next head is not re-rejected on the same carried premise. A prior
premise-refuted finding is excluded from the Prior blocking item addressed
carry-forward.

First verification, confirmed in this checkout and recorded here (the
runner posts the PR from the run summary, which carries the same refs):
(a) base reused via PATCH: pre-fix `.github/scripts/watchdog.py` `main()`
PATCH-updated the matching open alert body in place (`gh api -X PATCH ...
-f body=`, "Update in place rather than filing a second issue";
`git show d70e614^:.github/scripts/watchdog.py`), recorded in the
amended-ticket comment at tests/test_watchdog.py:1301-1309 ("the old
main() PATCH-reused a matching open issue in place"); fix commit d70e614
(#2500, #2553) replaced it with open-new plus close-superseded, and the
current `.github/scripts/watchdog.py` `close_alert` still PATCH-closes
(`-X PATCH ... -f state=closed`). So the #2500 verify-first premise
("never reuses") was false about base: confirmed. Routing still fires
only on a per-head judge observation with its own code/record ref.
(b) base never wrote claude-code-environment at the decline seam for the
#2492 plain calendar-wait shape: with the calendar patterns neutralized
the reason classifies "unknown"
(tests/test_engine_implement.py:4945-4960,
`test_classify_decline_reason_read_unknown_without_calendar_rule`:
"which finish_declined routed to blocked plus Needs agent, never to the
session lane. The decline seam therefore never wrote
claude-code-environment"), and the unknown branch in
engine/implement.py `finish_declined` (the `not ... calendar_wait_routed`
branch ending at the `declined_needs_effect` + `block_effect` calls)
defaults to `write_declined_agent_needs` (engine/implement.py:2659-2664,
`write_needs(item_id, "agent", ...)`); the session-lane writer
`write_declined_session_needs` (engine/implement.py:2678-2690) fires only
for genuine-Mac waits matching STILL_CLAUDE_RES, which plain
calendar-wait wording does not. Recorded likewise in
tests/test_engine_implement.py:4981-4989 ("the old decline route never
wrote claude-code-environment"). So the #2492 reproduction premise (base
writes claude-code-environment at that seam) was false about base:
confirmed.
(c) is established in code: `build_scoped_rereview` carries prior
blocking items (engine/review.py), the scoped lister asks one
`Prior blocking item addressed (<kind>): <finding>` per carried item
(scripts/muse-review-engine), and `derive_judge_answer` records an unmet
one as `requirement unmet: ...` rejected — the carry-forward this ticket
breaks for routed premises.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import review  # noqa: E402

REPO = "owner/repo"

TICKET = {"ref": REPO + "#2500", "number": 2500,
          "title": "Reuse open claims",
          "url": "https://github.com/{}/issues/2500".format(REPO)}

#: The #2500 PATCH-reuse shape: a Reproduction requirement asserting base
#: never reuses, refuted by observed PATCH reuse on base (pre-fix
#: `.github/scripts/watchdog.py` `main()` PATCH-updated the matching open
#: alert body in place; fix commit d70e614).
REUSE_REQUIREMENT = (
    "Reproduction: base never reuses an open claim; verify on base first")
REUSE_OBSERVATION = (
    "premise refuted (base): base PATCH-updates the matching open alert "
    "in place in .github/scripts/watchdog.py main "
    "(gh api -X PATCH -f body=)")


def _result(requirement, status="unmet", evidence="nope"):
    return {"requirement": requirement, "status": status,
            "evidence": evidence}


def test_refuted_patch_reuse_premise_routes_to_ticket():
    requirements = [REUSE_REQUIREMENT, "Add the claim-reuse guard"]
    results = [_result(REUSE_REQUIREMENT, "unmet", REUSE_OBSERVATION),
               _result("Add the claim-reuse guard", "met", "guard added")]

    routing = review.route_lane_verdict(TICKET, requirements, results)

    assert routing["route"] == "ticket"
    assert routing["kind"] == "amend-or-send-back"
    assert routing["ticket_ref"] == REPO + "#2500"
    assert routing["ticket_findings"] == [
        {"requirement": REUSE_REQUIREMENT, "evidence": REUSE_OBSERVATION}]
    # No implementer-directed rejection for the premise.
    assert routing["implementer_blocking"] == []
    assert not any(REUSE_REQUIREMENT in line
                   and not line.startswith(
                       review.PREMISE_REFUTED_ROUTING_PREFIX)
                   for line in routing["blocking"])
    assert routing["blocking"] == [
        "{} {} -- {}".format(review.PREMISE_REFUTED_ROUTING_PREFIX,
                             REUSE_REQUIREMENT, REUSE_OBSERVATION)]


def test_verify_first_environment_premise_routes_to_ticket():
    requirement = ("Verify first on base: base writes the "
                   "claude-code-environment record before implement runs")
    evidence = ("premise refuted (base): the decline seam never writes "
                "that Needs value for plain calendar-wait wording; "
                "an unknown decline routes to blocked plus Needs agent "
                "(engine/implement.py finish_declined, "
                "write_declined_agent_needs)")
    routing = review.route_lane_verdict(
        {"ref": REPO + "#2492"},
        [requirement],
        [_result(requirement, "unmet", evidence)])

    assert routing["route"] == "ticket"
    assert routing["kind"] == "amend-or-send-back"
    assert routing["verdict"] == "rejected"
    assert routing["implementer_blocking"] == []
    assert len(routing["blocking"]) == 1
    assert review.is_premise_refuted_routing(routing["blocking"][0])


def test_genuine_miss_without_opposite_base_behavior_stays_rejected():
    requirements = ["Add the claim-reuse guard", REUSE_REQUIREMENT]
    results = [_result("Add the claim-reuse guard", "unmet",
                       "no guard in the diff"),
               _result(REUSE_REQUIREMENT, "met", "reproduced on base")]

    routing = review.route_lane_verdict(TICKET, requirements, results)
    derived = review.derive_judge_answer(requirements, results)

    assert routing["route"] == "implementer"
    assert routing["kind"] is None
    assert routing["verdict"] == "rejected"
    assert routing["ticket_findings"] == []
    assert routing["blocking"] == derived["blocking"]
    assert routing["blocking"] == [
        "requirement unmet: Add the claim-reuse guard -- no guard in the diff"]


def test_marker_on_a_do_obligation_is_ignored():
    requirement = "Add retry coverage for the claim path"
    routing = review.route_lane_verdict(
        TICKET, [requirement],
        [_result(requirement, "unmet",
                 "premise refuted (base): base retries in foo.py:12")])

    assert routing["route"] == "implementer"
    assert routing["verdict"] == "rejected"
    assert routing["blocking"] == [
        "requirement unmet: {} -- premise refuted (base): "
        "base retries in foo.py:12".format(requirement)]


def test_unsure_observation_never_routes():
    routing = review.route_lane_verdict(
        TICKET, [REUSE_REQUIREMENT],
        [_result(REUSE_REQUIREMENT, "unsure", REUSE_OBSERVATION)])

    assert routing["route"] == "implementer"
    assert routing["verdict"] == "rejected"


def test_bare_marker_without_observation_routes_nothing():
    routing = review.route_lane_verdict(
        TICKET, [REUSE_REQUIREMENT],
        [_result(REUSE_REQUIREMENT, "unmet", "premise refuted (base):  ")])

    assert routing["route"] == "implementer"
    assert routing["verdict"] == "rejected"


def test_prior_wrapped_legacy_premise_routes_on_fresh_observation():
    wrapped = (
        "Prior blocking item addressed (missing_requirement_or_accept_test): "
        "requirement unmet: {} -- base showed no reuse at that head".format(
            REUSE_REQUIREMENT))
    routing = review.route_lane_verdict(
        TICKET, [wrapped], [_result(wrapped, "unmet", REUSE_OBSERVATION)])

    assert routing["route"] == "ticket"
    assert routing["implementer_blocking"] == []
    assert routing["ticket_findings"][0]["requirement"] == wrapped


def test_mixed_refuted_and_genuine_miss_keeps_genuine_blocker():
    requirements = [REUSE_REQUIREMENT, "Add the claim-reuse guard"]
    results = [_result(REUSE_REQUIREMENT, "unmet", REUSE_OBSERVATION),
               _result("Add the claim-reuse guard", "unmet",
                       "no guard in the diff")]

    routing = review.route_lane_verdict(TICKET, requirements, results)

    assert routing["route"] == "ticket"
    assert routing["verdict"] == "rejected"
    assert routing["implementer_blocking"] == [
        "requirement unmet: Add the claim-reuse guard -- no guard in the diff"]
    assert len(routing["blocking"]) == 2


def test_all_met_stays_approved_without_routing():
    requirements = [REUSE_REQUIREMENT]
    results = [_result(REUSE_REQUIREMENT, "met", "reproduced on base")]

    routing = review.route_lane_verdict(TICKET, requirements, results)

    assert routing["route"] == "implementer"
    assert routing["verdict"] == "approved"
    assert routing["blocking"] == []


def test_implementer_route_matches_derive_byte_for_byte():
    requirements = ["Add the claim-reuse guard", REUSE_REQUIREMENT,
                    "Does not break: keep old retries"]
    results = [_result("Add the claim-reuse guard", "unsure", "hmm"),
               _result(REUSE_REQUIREMENT, "met", "reproduced"),
               _result("Does not break: keep old retries", "unmet", "broke")]

    routing = review.route_lane_verdict(TICKET, requirements, results)

    assert routing["blocking"] == review.derive_judge_answer(
        requirements, results)["blocking"]


def _rejected_comments(head, blocking, created_at="2026-10-13T05:00:00Z"):
    import funnel
    record = {"verdict": "rejected", "head_sha": head,
              "blocking": blocking, "ci": "green"}
    body = (funnel.REVIEW_MARKER + "\n\n" + chr(96) * 3 + "json\n"
            + json.dumps(record) + "\n" + chr(96) * 3 + "\n")
    return {"status": "available", "comments": [
        {"kind": "issue", "author": "nateprich",
         "created_at": created_at, "body": body}]}


def test_carry_forward_excludes_routed_premise_finding(monkeypatch):
    prior_head = "2" * 40
    current_head = "3" * 40
    routed = ("{} {} -- {}".format(
        review.PREMISE_REFUTED_ROUTING_PREFIX,
        REUSE_REQUIREMENT, REUSE_OBSERVATION))
    comments = _rejected_comments(
        prior_head, [routed, "Code defect: retry loop"])
    monkeypatch.setattr(
        review, "fetch_branch_head", lambda repo, branch: "9" * 40)
    monkeypatch.setattr(
        review, "fetch_scope",
        lambda repo, base_sha, head: (
            ["f.py"], "same scoped diff", "merge-base"))
    monkeypatch.setattr(
        review, "_diff_line_count",
        lambda diff, interdiff=False: 0 if interdiff else 100)

    result, _ = review.build_scoped_rereview(
        REPO, 7, "main", current_head, comments)

    assert result["active"] is True
    assert result["prior_blocking_items"] == [
        {"kind": "code_defect", "finding": "Code defect: retry loop"}]
    assert result["premise_refuted_items"] == [routed]
    assert result["full_review_fallback"] is False
    assert not any("Prior blocking item addressed" in item["finding"]
                   and REUSE_REQUIREMENT in item["finding"]
                   for item in result["prior_blocking_items"])


def test_only_routed_premise_leaves_no_prior_item_to_reject(monkeypatch):
    prior_head = "2" * 40
    current_head = "3" * 40
    routed = ("{} {} -- {}".format(
        review.PREMISE_REFUTED_ROUTING_PREFIX,
        REUSE_REQUIREMENT, REUSE_OBSERVATION))
    comments = _rejected_comments(prior_head, [routed])
    monkeypatch.setattr(
        review, "fetch_branch_head", lambda repo, branch: "9" * 40)
    monkeypatch.setattr(
        review, "fetch_scope",
        lambda repo, base_sha, head: (
            ["f.py"], "same scoped diff", "merge-base"))
    monkeypatch.setattr(
        review, "_diff_line_count",
        lambda diff, interdiff=False: 0 if interdiff else 100)

    result, _ = review.build_scoped_rereview(
        REPO, 7, "main", current_head, comments)

    assert result["prior_blocking_items"] == []
    assert result["premise_refuted_items"] == [routed]
    assert result["full_review_fallback"] is False


def test_stopping_rule_kind_never_admits_a_routed_premise():
    routed = ("{} {} -- {}".format(
        review.PREMISE_REFUTED_ROUTING_PREFIX,
        REUSE_REQUIREMENT, REUSE_OBSERVATION))
    assert review.is_premise_refuted_routing(routed)
    assert review._stopping_rule_kind(routed) is None
    assert not review.is_premise_refuted_routing(
        "requirement unmet: {} -- {}".format(
            REUSE_REQUIREMENT, "no reuse seen"))


@pytest.mark.parametrize("requirement,expected", [
    (REUSE_REQUIREMENT, True),
    ("Verify first on base: base writes the record", True),
    ("The fix passes on base before merge", True),
    ("Reproduction on base shows the old retry", True),
    ("Add retry coverage for the claim path", False),
    ("Add a reproduction test for the retry path", False),
    ("Does not break: reproduction passes on base", False),
    ("No new blocking defect: the interdiff adds no code defect, "
     "missing requirement or Accept test, or merged-main suite failure.",
     False),
    ("", False),
    (None, False),
])
def test_base_premise_requirement_shapes(requirement, expected):
    assert review.is_base_premise_requirement(requirement) is expected


def test_judge_prompt_marks_refuted_premises_for_routing():
    engine = (ROOT / "scripts" / "muse-review-engine").read_text()

    assert "premise refuted (base): " in engine
    assert "the ticket premise is refuted, not the diff" in engine
    assert "stays a plain unmet and\nnever carries that marker" in engine
    assert "route_lane_verdict(packet.get(\"ticket\")" in engine
