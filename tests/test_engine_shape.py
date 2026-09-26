"""shape-packet and shape-apply with the plan schema (#810, Phase 2 of #794).

The shape runner shows the model a packet and nothing else; the model's
structured answer is validated, rendered, and decided mechanically. Each
field and each rule term gets a fixture test here so the cutover can lean
on this module without re-deriving what every piece means.
"""

from __future__ import annotations

import io
import json
import pathlib
import stat
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INVESTIGATE_SHAPE_FIXTURES = json.loads(
    (ROOT / "tests/fixtures/investigate_shape_excerpts.json").read_text(
        encoding="utf-8"))
ISSUE_THREAD_1195 = json.loads(
    (ROOT / "tests/fixtures/shape_issue_thread_1195.json").read_text(
        encoding="utf-8"))

import funnel  # noqa: E402
from engine import shape  # noqa: E402
from funnel import Item  # noqa: E402

REPO = "owner/repo"
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def canonical_field_writes(monkeypatch):
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: None,
    )


def answer(**kw):
    data = {
        "decided_from_precedent": [
            {"claim": "reviews stay human-gated",
             "source": "plan.md ladder"},
        ],
        "decided_by_agent": [
            {"decision": "validate strictly",
             "alternative": "accept unknown fields",
             "why": "unknown fields signal a confused model"},
        ],
        "needs_nate": {
            "exposure": None,
            "gates": None,
            "scope": None,
            "preference": None,
        },
        "proposed_class": "Improve",
        "plan_markdown": "# Plan\n\nDo the thing.\n",
        "escalated_risk": [],
        "depends_on": [],
        "premises": [
            {"claim": "reviews stay human-gated",
             "evidence": "plan.md:42", "label": "documented"},
        ],
    }
    data.update(kw)
    return data


def idea(number=42, **kw):
    body = kw.pop("body", None)
    if body is None:
        body = ("Captured note.\n\n" + funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="muse"))
    data = {
        "repo": REPO,
        "number": number,
        "title": "An idea",
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "state": "OPEN",
        "status": "Ideas",
        "klass": "Improve",
        "origin": "agent",
        "risk": "standard",
        "needs": "none",
        "item_id": "project-item-{}".format(number),
        "blocked_by_refs": [],
        "body": body,
        "labels": ["needs-shaping"],
    }
    data.update(kw)
    return Item(**data)


def stub_gh(monkeypatch, item):
    """Stub the GitHub reads and writes apply_shape performs."""
    calls = []

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        if funnel.CLASS_FIELD_ID in query:
            names = list(funnel.LADDER)
        else:
            names = ["Shaped", "Ready"]
        return {"node": {"options": [
            {"id": "opt-{}".format(name), "name": name}
            for name in names
        ]}}

    def run(args, capture_output=False, text=True, **kwargs):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    return calls


def gh_calls(calls, *prefix):
    return [call for call in calls
            if call[0] == "run" and call[1][:len(prefix)] == prefix]


def blocked_by_payload(refs):
    nodes = []
    for ref in refs:
        repo, number = ref.rsplit("#", 1)
        nodes.append({
            "number": int(number),
            "repository": {"nameWithOwner": repo},
        })
    return {"repository": {"issue": {"blockedBy": {
        "totalCount": len(nodes), "nodes": nodes,
    }}}}


# -- answer validation ---------------------------------------------------

def test_a_well_formed_answer_validates():
    found = shape.validate_answer(answer())
    assert found["proposed_class"] == "Improve"
    assert found["needs_nate"] == {
        "exposure": None, "gates": None, "scope": None,
        "preference": None}
    assert found["decided_from_precedent"] == [
        {"claim": "reviews stay human-gated",
         "source": "plan.md ladder"}]
    assert found["premises"] == [
        {"claim": "reviews stay human-gated",
         "evidence": "plan.md:42", "label": "documented"}]


def test_validation_strips_surrounding_whitespace():
    found = shape.validate_answer(answer(
        proposed_class="  Improve  ",
        plan_markdown="\n# Plan\n",
        needs_nate={"exposure": None, "gates": ["  Who decides?  "],
                    "scope": None, "preference": None},
        depends_on=["  owner/repo#165  "],
        premises=[{"claim": "  source is verified\n here ",
                   "evidence": "  command: check\noutput: passed  ",
                   "label": " measured "}]))
    assert found["proposed_class"] == "Improve"
    assert found["plan_markdown"] == "# Plan"
    assert found["needs_nate"]["gates"] == ["Who decides?"]
    assert found["depends_on"] == ["owner/repo#165"]
    assert found["premises"] == [{
        "claim": "source is verified here",
        "evidence": "command: check output: passed",
        "label": "measured",
    }]


def test_the_answer_must_be_an_object():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer([])
    with pytest.raises(shape.ShapeError):
        shape.validate_answer("plan")


def test_the_answer_needs_every_key_and_no_extras():
    missing = answer()
    del missing["needs_nate"]
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(missing)
    missing = answer()
    del missing["premises"]
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(missing)
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(risk="standard"))


def test_precedent_entries_need_a_claim_and_a_source():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_from_precedent="plan.md"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(
            decided_from_precedent=[{"claim": "no source"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_from_precedent=[
            {"claim": "c", "source": "s", "extra": "x"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(
            decided_from_precedent=[{"claim": "  ", "source": "s"}]))


def test_an_empty_precedent_list_is_honest():
    found = shape.validate_answer(answer(decided_from_precedent=[]))
    assert found["decided_from_precedent"] == []


def test_agent_decisions_keep_reasoning_and_rejected_alternative():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_by_agent="strict"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_by_agent=[
            {"decision": "d", "alternative": "a"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_by_agent=[
            {"decision": "d", "alternative": "", "why": "w"}]))
    found = shape.validate_answer(answer(decided_by_agent=[]))
    assert found["decided_by_agent"] == []


def test_needs_nate_needs_all_four_fields():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate=[]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": None, "scope": None}))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": None, "scope": None,
            "preference": None, "extra": None}))


@pytest.mark.parametrize("bad", ["", "   ", 0, False, [], {}])
def test_needs_nate_rejects_anything_but_null_or_a_question(bad):
    needs = {"exposure": None, "gates": None, "scope": None,
             "preference": None}
    needs["scope"] = bad
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate=needs))


def test_proposed_class_must_be_a_ladder_class():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(proposed_class="Broken2"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(proposed_class="  "))


@pytest.mark.parametrize(
    "fixture", INVESTIGATE_SHAPE_FIXTURES["non_defect_shapes"],
    ids=lambda fixture: fixture["source"],
)
def test_recorded_non_defect_shapes_cannot_propose_investigate(fixture):
    proposed_class = fixture["proposed_class_line"].split(": ", 1)[1]
    with pytest.raises(shape.ShapeError, match="Possible defect"):
        shape.validate_answer(answer(
            proposed_class=proposed_class,
            plan_markdown=fixture["body_excerpt"]))


def test_recorded_genuine_defect_shape_accepts_one_possible_defect_line():
    fixture = INVESTIGATE_SHAPE_FIXTURES["genuine_defect_shape"]
    proposed_class = fixture["proposed_class_line"].split(": ", 1)[1]
    candidate = shape.validate_answer(answer(
        proposed_class=proposed_class,
        plan_markdown="{}\n\n{}".format(
            fixture["body_excerpt"], fixture["possible_defect_line"])))

    assert candidate["proposed_class"] == "Investigate"
    assert fixture["possible_defect_line"] in candidate["plan_markdown"]


@pytest.mark.parametrize("malformed", [
    "Possible defect:",
    "Possible defect:   ",
    "Possible defect is present but has no colon: statement",
    "Possible defect:statement has no separator",
])
def test_investigate_rejects_blank_or_malformed_possible_defect_lines(
        malformed):
    with pytest.raises(shape.ShapeError, match="Possible defect"):
        shape.validate_answer(answer(
            proposed_class="Investigate",
            plan_markdown="# Plan\n\n{}\n".format(malformed)))


def test_investigate_rejects_multiple_possible_defect_lines():
    with pytest.raises(shape.ShapeError, match="Possible defect"):
        shape.validate_answer(answer(
            proposed_class="Investigate",
            plan_markdown=(
                "# Plan\n\n"
                "Possible defect: the first candidate.\n"
                "Possible defect: the second candidate.\n")))


def test_non_investigate_proposal_is_untouched_by_possible_defect_check():
    candidate = shape.validate_answer(answer(
        proposed_class="Improve",
        plan_markdown=(
            "# Plan\n\nPossible defect:\n"
            "Possible defect: another line.\n")))

    assert candidate["proposed_class"] == "Improve"


def test_plan_markdown_must_be_non_empty():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(plan_markdown="  \n "))


def test_an_answer_missing_escalated_risk_is_malformed():
    # #1034: the declaration is required, even when the plan carries no
    # risk — an empty list says so honestly.
    bad = answer()
    del bad["escalated_risk"]
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(bad)


def test_escalated_risk_must_be_a_list_of_reason_and_why():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(escalated_risk="data-migration"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "data-migration"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "data-migration", "why": "  "}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "data-migration", "why": "backfills",
             "extra": "x"}]))


def test_escalated_risk_reasons_come_from_the_scan_vocabulary():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "standard", "why": "nothing risky"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "Data-Migration", "why": "backfills"}]))
    found = shape.validate_answer(answer(escalated_risk=[
        {"reason": "  data-migration  ",
         "why": "backfills\nthe ledger  table"}]))
    assert found["escalated_risk"] == [
        {"reason": "data-migration",
         "why": "backfills the ledger table"}]


def test_an_empty_escalated_risk_list_is_honest():
    found = shape.validate_answer(answer(escalated_risk=[]))
    assert found["escalated_risk"] == []


def test_premises_are_a_list_of_claim_evidence_and_label():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(premises="a factual claim"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(premises=[{"claim": "a claim",
                                               "label": "measured"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(premises=[{
            "claim": "a claim", "evidence": "  ", "label": "measured"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(premises=[{
            "claim": "a claim", "evidence": "plan.md:42",
            "label": "measured", "extra": "not in the schema"}]))


def test_premise_labels_use_the_learnings_scale():
    for invalid in ("unknown", "Measured", ""):
        with pytest.raises(shape.ShapeError):
            shape.validate_answer(answer(premises=[{
                "claim": "a claim", "evidence": "plan.md:42",
                "label": invalid}]))
    found = shape.validate_answer(answer(premises=[
        {"claim": "seen directly", "evidence": "run output",
         "label": "measured"},
        {"claim": "vendor says so", "evidence": "vendor guide:12",
         "label": "documented"},
        {"claim": "might be true", "evidence": "rollout abc123",
         "label": "inferred"},
    ]))
    assert [entry["label"] for entry in found["premises"]] == [
        "measured", "documented", "inferred"]


def test_an_answer_missing_depends_on_is_malformed():
    # #1053: the sequencing list is required, even when the plan waits
    # on nothing — an empty list says so honestly.
    bad = answer()
    del bad["depends_on"]
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(bad)


def test_depends_on_must_be_a_list_of_owner_repo_refs():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(depends_on="owner/repo#165"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(depends_on=[165]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(depends_on=["#165"]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(depends_on=["owner/repo#0"]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(depends_on=["not a ref"]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(depends_on=[""]))
    found = shape.validate_answer(answer(depends_on=[
        "owner/repo#165", "other/repo#7"]))
    assert found["depends_on"] == ["owner/repo#165", "other/repo#7"]


def test_needs_nate_accepts_atomic_lists():
    # #1053: each category carries null or a small list of single
    # questions; inner whitespace collapses per question.
    found = shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": None,
        "scope": ["Should we build it at all?",
                  "Which  milestone\nowns the work?"],
        "preference": None}))
    assert found["needs_nate"]["scope"] == [
        "Should we build it at all?",
        "Which milestone owns the work?"]


def test_needs_nate_rejects_a_bare_string():
    # #1053: the old single-question shape is malformed now that each
    # category carries a list — the runner retries once with this error
    # fed back, and the model answers again with a list.
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": "Who may write Ready?",
            "scope": None, "preference": None}))


@pytest.mark.parametrize("bad", [[""], ["  "], [123], [None], ["ok", ""],
                                 [{"question": "q"}]])
def test_needs_nate_rejects_bad_list_entries(bad):
    needs = {"exposure": None, "gates": None, "scope": None,
             "preference": None}
    needs["scope"] = bad
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate=needs))


# -- rendering -------------------------------------------------------------

def test_render_carries_the_plan_and_every_field():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert body.startswith("# Plan\n\nDo the thing.\n")
    assert "## Premises" in body
    assert ("- reviews stay human-gated "
            "(label: documented; evidence: plan.md:42)") in body
    assert "## Decided from precedent" in body
    assert "- reviews stay human-gated (source: plan.md ladder)" in body
    assert "## Decided by the agent" in body
    assert ("- validate strictly (rejected: accept unknown fields; "
            "unknown fields signal a confused model)") in body
    assert "## Needs Nate" not in body
    assert ("Do the thing.\n\n## Premises\n\n"
            "- reviews stay human-gated (label: documented; "
            "evidence: plan.md:42)\n\nProposed class: Improve\n\n"
            "## Decided from precedent") in body


def test_render_omits_all_clear_needs_boilerplate():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert "nothing outstanding" not in body
    assert "## Needs Nate" not in body


def test_render_records_open_questions_verbatim():
    body = shape.render_plan(shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": ["Who may write Ready?"],
        "scope": None, "preference": None})))
    assert "- Gates: Who may write Ready?" in body
    assert "Exposure:" not in body


def test_render_marks_empty_decision_lists():
    body = shape.render_plan(shape.validate_answer(answer(
        decided_from_precedent=[], decided_by_agent=[], premises=[])))
    assert body.count("None recorded.") == 3


def test_render_marks_an_empty_premises_list():
    body = shape.render_plan(shape.validate_answer(answer(premises=[])))
    assert "## Premises\n\nNone recorded." in body


def test_the_all_clear_render_carries_no_authority_signals():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert funnel.needs_nate_signals(body) == []


def test_render_joins_atomic_questions_on_one_category_line():
    body = shape.render_plan(shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": None,
        "scope": ["Should we build it at all?",
                  "Which milestone owns the work?"],
        "preference": None})))
    assert ("- Scope and priority: Should we build it at all?; "
            "Which milestone owns the work?") in body


def test_render_records_sequencing_dependencies():
    body = shape.render_plan(shape.validate_answer(answer(
        depends_on=["owner/repo#165", "other/repo#7"])))
    assert "## Sequencing" in body
    assert "Depends on: owner/repo#165, other/repo#7" in body
    assert body.rstrip().endswith("Depends on: owner/repo#165, other/repo#7")


def test_render_omits_sequencing_when_nothing_waits():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert "## Sequencing" not in body
    assert "Depends on" not in body


# -- the mechanical rule -----------------------------------------------------

def test_an_all_clear_agent_plan_is_ready():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="agent")
    assert status == "Ready"
    assert reason == ("needs_nate all null; class Improve self-approvable; "
                      "origin agent")


def test_one_open_need_holds_at_shaped():
    status, reason = shape.decide(
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": ["Who may write Ready?"],
            "scope": None, "preference": None})),
        klass="Improve", origin_voice="agent")
    assert status == "Shaped"
    assert reason == "open question under Gates"


def test_every_open_need_is_named():
    status, reason = shape.decide(
        shape.validate_answer(answer(needs_nate={
            "exposure": ["New surface?"], "gates": None,
            "scope": None, "preference": ["Which default?"]})),
        klass="Improve", origin_voice="agent")
    assert status == "Shaped"
    assert reason == ("open question under Exposure; "
                      "open question under Preference")


def test_a_non_self_approvable_class_holds():
    validated = shape.validate_answer(answer())
    status, reason = shape.decide(
        validated, klass="New", origin_voice="agent")
    assert (status, reason) == (
        "Shaped", "class New is not self-approvable")
    status, reason = shape.decide(
        validated, klass=None, origin_voice="agent")
    assert (status, reason) == (
        "Shaped", "class unset is not self-approvable")


def test_nates_origin_holds():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="nate-relayed")
    assert (status, reason) == ("Shaped", "origin is Nate's")


def test_a_missing_origin_holds():
    status, _ = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice=None)
    assert status == "Shaped"


def test_an_override_to_agents_restores_eligibility():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="nate-relayed",
        override_target="agents")
    assert status == "Ready"
    assert reason.endswith("origin override to agents")


def test_escalated_risk_holds():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="agent",
        escalation_reasons=["credentials"])
    assert (status, reason) == (
        "Shaped", "escalated risk (credentials)")


def test_a_scan_match_line_is_in_the_escalation_explanation():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="agent",
        escalation_reasons=["credentials"],
        escalation_matches=[{
            "reason": "credentials",
            "line": "Read credentials from the environment.",
        }])
    assert (status, reason) == (
        "Shaped",
        "escalated risk (credentials: Read credentials from the environment.)")


def test_a_declared_risk_holds_with_a_clean_scan():
    # #1034: the model judges the plan, not its wording — a declared
    # data-migration with no scan hit still holds.
    status, reason = shape.decide(
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "data-migration",
             "why": "backfills the ledger table"}])),
        klass="Improve", origin_voice="agent",
        escalation_reasons=[])
    assert (status, reason) == (
        "Shaped", "escalated risk (data-migration)")


def test_a_scan_hit_holds_with_an_empty_declaration():
    # #1034: there is no standard override — an empty declaration never
    # clears what the wording scan found.
    status, reason = shape.decide(
        shape.validate_answer(answer(escalated_risk=[])),
        klass="Improve", origin_voice="agent",
        escalation_reasons=["credentials"])
    assert (status, reason) == (
        "Shaped", "escalated risk (credentials)")


def test_a_clear_declaration_with_a_clear_scan_is_ready():
    status, reason = shape.decide(
        shape.validate_answer(answer(escalated_risk=[])),
        klass="Improve", origin_voice="agent",
        escalation_reasons=[])
    assert status == "Ready"
    assert reason == ("needs_nate all null; class Improve self-approvable; "
                      "origin agent")


def test_declaration_and_scan_union_without_duplicates():
    status, reason = shape.decide(
        shape.validate_answer(answer(escalated_risk=[
            {"reason": "credentials", "why": "rotates the api-key"},
            {"reason": "data-migration",
             "why": "backfills the ledger"}])),
        klass="Improve", origin_voice="agent",
        escalation_reasons=["credentials"])
    assert (status, reason) == (
        "Shaped", "escalated risk (credentials, data-migration)")


def test_a_sequencing_only_plan_self_approves():
    # #1053: "wait for #165 or start now" is a dependency, not a
    # question — with needs_nate all null the plan is Ready.
    status, reason = shape.decide(
        shape.validate_answer(answer(depends_on=["owner/repo#165"])),
        klass="Improve", origin_voice="agent")
    assert status == "Ready"
    assert reason == ("needs_nate all null; class Improve self-approvable; "
                      "origin agent")


def test_a_compound_scope_splits_so_only_the_kind_a_half_stays_open():
    # #1053: the sequencing half moves to depends_on; the Kind A half —
    # whether to build the capability at all — stays the one open
    # question, and the reason names only its category.
    status, reason = shape.decide(
        shape.validate_answer(answer(
            needs_nate={"exposure": None, "gates": None,
                        "scope": ["Should we build the capability at all?"],
                        "preference": None},
            depends_on=["owner/repo#165"])),
        klass="Improve", origin_voice="agent")
    assert status == "Shaped"
    assert reason == "open question under Scope and priority"


def test_career_toolset_198_replay_drops_false_scope_and_risk():
    # The plan routes the CSV through the guarded reader and skips a cloud
    # write on cached input. A generic permission question and a hypothetical
    # bug in the no-write path are not stakeholder scope or proposed actions.
    candidate = shape.validate_answer(answer(
        proposed_class="Broken",
        plan_markdown=(
            "Route job-log.csv through the guarded workspace reader. "
            "On cached content, skip write_log and report a degraded run."),
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should this Broken fix be built?"],
                    "preference": None},
        escalated_risk=[{
            "reason": "destructive",
            "why": ("A hypothetical bug in the no-write path could "
                    "overwrite newer cloud rows."),
        }]))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Broken", origin_voice="agent")

    assert reviewed["needs_nate"]["scope"] is None
    assert reviewed["escalated_risk"] == []
    assert "generic Scope permission" in rejected[0]
    assert "hypothetical implementation bug" in rejected[1]


def test_command_center_1268_replay_keeps_its_gates_question():
    candidate = shape.validate_answer(answer(
        proposed_class="Broken",
        plan_markdown=(
            "Exclude paired, one-line inline code spans from the scan; "
            "keep ordinary prose and unclosed spans searchable."),
        needs_nate={"exposure": None,
                    "gates": ["May paired inline spans be excluded?"],
                    "scope": None, "preference": None},
        escalated_risk=[]))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Broken", origin_voice="agent")
    status, reason = shape.decide(
        reviewed, klass="Broken", origin_voice="agent")

    assert rejected == []
    assert reviewed["needs_nate"]["gates"] == [
        "May paired inline spans be excluded?"]
    assert (status, reason) == (
        "Shaped", "open question under Gates")


def test_agent_review_keeps_real_scope_and_proposed_action_risk():
    candidate = shape.validate_answer(answer(
        proposed_class="Broken",
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should the repair also cover archived rows?"],
                    "preference": None},
        escalated_risk=[{
            "reason": "destructive",
            "why": "The proposed repair deletes archived rows."}]))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Broken", origin_voice="agent")

    assert rejected == []
    assert reviewed["needs_nate"]["scope"] == [
        "Should the repair also cover archived rows?"]
    assert reviewed["escalated_risk"] == candidate["escalated_risk"]


@pytest.mark.parametrize("klass", sorted(funnel.SELF_APPROVABLE_CLASSES))
def test_all_agent_self_approvable_classes_strip_generic_permission(klass):
    candidate = shape.validate_answer(answer(
        proposed_class=klass,
        plan_markdown=(
            "# Plan\n\nDo the thing.\n\n"
            "Possible defect: a defect exists."
            if klass == "Investigate" else "# Plan\n\nDo the thing."),
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should we fix this?"],
                    "preference": None},
        escalated_risk=[{
            "reason": "destructive",
            "why": "A hypothetical implementation bug could lose data.",
        }]))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass=klass, origin_voice="agent")

    assert reviewed["needs_nate"]["scope"] is None
    assert "generic Scope permission" in rejected[0]
    assert reviewed["escalated_risk"] == []
    assert any("hypothetical implementation bug" in signal
               for signal in rejected)


def test_investigate_is_explicitly_covered_by_agent_output_review():
    assert "Investigate" in funnel.SELF_APPROVABLE_CLASSES

    repo = "nateprich-projects/command-center"
    item = idea(
        1448, repo=repo, klass="Investigate",
        body=funnel.origin_block("agent", at=NOW, run="shape-run",
                                 agent="muse"))
    candidate = shape.validate_answer(answer(
        proposed_class="Investigate",
        plan_markdown=(
            "# Plan\n\nDo the thing.\n\n"
            "Possible defect: the behavior may be defective."),
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should we fix this?",
                              "Should this happen now?"],
                    "preference": None}))

    reviewed, rejected = shape.review_shape_output_for_item(
        [item], item, candidate)
    status, reason = shape.decide(
        reviewed, klass="Investigate", origin_voice="agent")

    assert reviewed["needs_nate"]["scope"] is None
    assert any("generic Scope permission" in signal for signal in rejected)
    assert any("scheduling" in signal for signal in rejected)
    assert "Use the funnel's computed order" in shape.render_plan(reviewed)
    assert status == "Ready"
    assert reason == (
        "needs_nate all null; class Investigate self-approvable; "
        "origin agent")


def test_improve_is_explicitly_covered_by_agent_output_review_and_close_policy():
    assert "Improve" in funnel.SELF_APPROVABLE_CLASSES

    item = idea(
        1448, klass="Improve",
        body=funnel.origin_block("agent", at=NOW, run="shape-run",
                                 agent="muse"))
    candidate = shape.validate_answer(answer(
        proposed_class="Improve",
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should we fix this?",
                              "Should this happen now?"],
                    "preference": None}))

    reviewed, rejected = shape.review_shape_output_for_item(
        [item], item, candidate)
    status, reason = shape.decide(
        reviewed, klass="Improve", origin_voice="agent")

    assert reviewed["needs_nate"]["scope"] is None
    assert any("generic Scope permission" in signal for signal in rejected)
    assert any("scheduling" in signal for signal in rejected)
    assert status == "Ready"
    assert reason == "needs_nate all null; class Improve self-approvable; origin agent"

    item.status = "Building"
    item.children_total = 1
    item.children_done = 1
    assert funnel._auto_closeable_project(item)
    assert funnel.gate_question(item) is None


@pytest.mark.parametrize("klass", ["Broken", "Investigate", "Maintenance"])
@pytest.mark.parametrize("origin", ["agent", "nate-direct", "nate-relayed"])
def test_self_approvable_upkeep_classes_close_after_all_tickets(klass, origin):
    item = idea(
        1448, klass=klass, origin=origin,
        body=(funnel.origin_block(
            origin, at=NOW, run="shape-run",
            agent="muse" if origin == "agent" else "nate")
              if origin in funnel.ORIGIN_VOICES else ""),
        status="Building", children_total=2, children_done=2)

    assert klass in funnel.SELF_APPROVABLE_CLASSES
    assert funnel._auto_closeable_project(item)
    assert funnel.gate_question(item) is None


@pytest.mark.parametrize(
    ("origin", "can_close"),
    [("agent", True), ("nate-direct", False), ("nate-relayed", False)],
)
def test_improve_auto_close_requires_agent_origin(origin, can_close):
    item = idea(
        1448, klass="Improve", origin=origin,
        body=(funnel.origin_block(
            origin, at=NOW, run="shape-run",
            agent="muse" if origin == "agent" else "nate")
              if origin in funnel.ORIGIN_VOICES else ""),
        status="Building", children_total=2, children_done=2)

    assert "Improve" in funnel.SELF_APPROVABLE_CLASSES
    assert funnel._auto_closeable_project(item) is can_close
    assert funnel.gate_question(item) == (None if can_close else "Accept it?")


@pytest.mark.parametrize("klass", funnel.LADDER)
def test_analysis_projects_still_wait_at_accept_for_every_class(klass):
    body = "\n\n".join((
        funnel.origin_block("agent", at=NOW, run="shape-run", agent="muse"),
        funnel.ANALYSIS_MARKER + '\n```json\n{"analysis": true}\n```',
    ))
    item = idea(
        1448, klass=klass, origin="agent", body=body,
        status="Building", children_total=2, children_done=2)

    assert not funnel._auto_closeable_project(item)
    assert funnel.gate_question(item) == "Accept it?"


def test_recorded_the_league_258_timing_decision_advances_without_dependency():
    repo = "nateprich-projects/The-League"
    item = idea(
        258, repo=repo, klass="Maintenance",
        body=funnel.origin_block("agent", at=NOW, run="shape-run",
                                 agent="muse"))
    candidate = shape.validate_answer(answer(
        proposed_class="Maintenance",
        decided_by_agent=[{
            "decision": ("Land now with no ticket dependency; #165 and "
                         "#174 keep the gate green through rebase."),
            "alternative": "Wait for #165 and #174 to finish.",
            "why": ("No hard technical ordering exists, and landing now "
                    "guards the in-flight edits."),
        }],
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should this land now while #165 and #174 "
                              "are in flight, or wait?"],
                    "preference": None}))

    reviewed, rejected = shape.review_shape_output_for_item(
        [item], item, candidate)
    status, reason = shape.decide(
        reviewed, klass="Maintenance", origin_voice="agent")

    assert reviewed["needs_nate"]["scope"] is None
    assert reviewed["depends_on"] == []
    assert "Land now with no ticket dependency" in shape.render_plan(reviewed)
    assert status == "Ready"
    assert reason == ("needs_nate all null; class Maintenance self-approvable; "
                      "origin agent")
    assert any("scheduling" in signal for signal in rejected)


@pytest.mark.parametrize("question", [
    "Should this wait until #165 is complete?",
    "Should this happen after #165 closes?",
    ("Should this wait for "
     "https://github.com/nateprich-projects/The-League/issues/165?"),
])
def test_a_clear_wait_for_ticket_question_becomes_a_dependency(question):
    repo = "nateprich-projects/The-League"
    item = idea(
        1448, repo=repo, klass="Maintenance",
        body=funnel.origin_block("agent", at=NOW, run="shape-run",
                                 agent="muse"))
    candidate = shape.validate_answer(answer(
        proposed_class="Maintenance",
        needs_nate={"exposure": None, "gates": None,
                    "scope": [question],
                    "preference": None}))

    reviewed, _ = shape.review_shape_output_for_item(
        [item], item, candidate)
    status, reason = shape.decide(
        reviewed, klass="Maintenance", origin_voice="agent")
    body = shape.render_plan(reviewed)

    assert reviewed["needs_nate"]["scope"] is None
    assert reviewed["depends_on"] == [repo + "#165"]
    assert "Wait for {}#165 before proceeding".format(repo) \
        in body
    assert "Depends on: {}#165".format(repo) in body
    assert status == "Ready"
    assert "all null; class Maintenance self-approvable" in reason


def test_concrete_scope_tradeoff_with_timing_words_stays_open():
    candidate = shape.validate_answer(answer(
        proposed_class="Maintenance",
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should the repair also cover archived rows "
                              "in this release, or defer them?"],
                    "preference": None}))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Maintenance", origin_voice="agent",
        repo="owner/repo")

    assert rejected == []
    assert reviewed["needs_nate"]["scope"] == candidate["needs_nate"]["scope"]


@pytest.mark.parametrize(("field", "question"), [
    ("exposure", "Does this expose a new reachable surface?"),
    ("gates", "Who may write Ready?"),
    ("preference", "Would you prefer the compact output?"),
])
def test_other_needs_nate_categories_stay_open(field, question):
    needs = {"exposure": None, "gates": None,
             "scope": None, "preference": None}
    needs[field] = [question]
    candidate = shape.validate_answer(answer(
        proposed_class="Maintenance", needs_nate=needs))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Maintenance", origin_voice="agent",
        repo="owner/repo")
    status, reason = shape.decide(
        reviewed, klass="Maintenance", origin_voice="agent")

    assert rejected == []
    assert reviewed["needs_nate"][field] == [question]
    assert status == "Shaped"
    assert reason.startswith("open question under")


def test_unknown_scope_question_stays_open():
    question = "Is this the appropriate approach?"
    candidate = shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": None, "scope": [question],
        "preference": None}))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Maintenance", origin_voice="agent",
        repo="owner/repo")

    assert rejected == []
    assert reviewed["needs_nate"]["scope"] == [question]


def test_priority_question_uses_funnel_order_without_dependency():
    question = "Should this task take priority over #165?"
    candidate = shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": None, "scope": [question],
        "preference": None}))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Maintenance", origin_voice="agent",
        repo="owner/repo")

    assert reviewed["needs_nate"]["scope"] is None
    assert reviewed["depends_on"] == []
    assert "Use the funnel's computed order" in shape.render_plan(reviewed)
    assert any("scheduling" in signal for signal in rejected)


def test_unclassified_implementation_detail_is_kept():
    question = "Should encryption be applied before upload?"
    candidate = shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": None, "scope": [question],
        "preference": None}))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass="Maintenance", origin_voice="agent",
        repo="owner/repo")

    assert rejected == []
    assert reviewed["needs_nate"]["scope"] == [question]


@pytest.mark.parametrize("klass", ["New", "Replace"])
def test_non_self_approvable_classes_are_untouched(klass):
    candidate = shape.validate_answer(answer(
        proposed_class=klass,
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should we fix this?"],
                    "preference": None},
        escalated_risk=[{
            "reason": "destructive",
            "why": "A hypothetical implementation bug could lose data.",
        }]))

    reviewed, rejected = shape.review_agent_shape_output(
        candidate, klass=klass, origin_voice="agent")

    assert rejected == []
    assert reviewed == candidate


def test_nate_origin_plan_is_untouched_by_agent_output_review():
    candidate = shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": None,
        "scope": ["Should this land now while #165 is in flight?"],
        "preference": None}))
    item = idea(
        258, repo="nateprich-projects/The-League", klass="Maintenance",
        origin="nate-relayed", body=funnel.origin_block(
            "nate-relayed", at=NOW, run="shape-run", agent="nate"))

    reviewed, rejected = shape.review_shape_output_for_item(
        [item], item, candidate)

    assert rejected == []
    assert reviewed == candidate


def test_a_documented_runtime_root_keeps_a_path_question_out_of_needs_nate():
    # #1053: a machine-local path under the repo's documented runtime
    # root is precedent (or a recorded reversible default), never a
    # Needs Nate entry — with the record all null the plan is Ready.
    status, _ = shape.decide(
        shape.validate_answer(answer(decided_from_precedent=[
            {"claim": "machine-local state lives under the documented "
                      "runtime root; the exact path stays out of Git",
             "source": "AGENTS.md runtime root"}])),
        klass="Improve", origin_voice="agent")
    assert status == "Ready"


def test_exposure_still_holds_alongside_a_recorded_dependency():
    # #1053: Kind A questions stay his — a sequencing edge never
    # releases an exposure hold.
    status, reason = shape.decide(
        shape.validate_answer(answer(
            needs_nate={"exposure": ["Does this open a new reachable "
                                     "surface?"],
                        "gates": None, "scope": None,
                        "preference": None},
            depends_on=["owner/repo#165"])),
        klass="Improve", origin_voice="agent")
    assert status == "Shaped"
    assert reason == "open question under Exposure"


def test_an_authorisation_risk_still_holds_alongside_a_recorded_dependency():
    # #1053: same for the escalated hold — the dependency is recorded
    # and the risk still holds the plan at Shaped.
    status, reason = shape.decide(
        shape.validate_answer(answer(
            escalated_risk=[{"reason": "authorisation",
                             "why": "grants a new permission model"}],
            depends_on=["owner/repo#165"])),
        klass="Improve", origin_voice="agent",
        escalation_reasons=[])
    assert (status, reason) == (
        "Shaped", "escalated risk (authorisation)")


# -- the assembled packet ----------------------------------------------------

def idea_dict(**kw):
    data = {
        "ref": REPO + "#42",
        "number": 42,
        "title": "An idea",
        "url": "https://github.com/{}/issues/42".format(REPO),
        "body": "Captured note.",
        "labels": ["needs-shaping"],
        "status": "Ideas",
        "klass": None,
    }
    data.update(kw)
    return data


def sibling_dict(number, **kw):
    data = {
        "ref": "{}#{}".format(REPO, number),
        "number": number,
        "title": "Sibling plan",
        "status": "Shaped",
        "klass": "Improve",
        "body": "# Plan\n\nA sibling plan.\n",
    }
    data.update(kw)
    return data


def packet(**kw):
    args = {
        "repo": REPO,
        "idea": idea_dict(),
        "origin_voice": "agent",
        "override_target": None,
        "plan_md": "# design record",
        "plan_md_missing": False,
        "agents_md": "# rule book",
        "agents_md_missing": False,
        "siblings": [sibling_dict(89)],
        "collected_at": "2026-09-14T00:00:00+00:00",
    }
    args.update(kw)
    return shape.build_packet(**args)


def test_packet_carries_every_field():
    found = packet()
    assert found["repo"] == REPO
    assert found["idea"]["ref"] == REPO + "#42"
    assert found["idea"]["body"] == "Captured note."
    assert found["idea"]["labels"] == ["needs-shaping"]
    assert found["idea"]["status"] == "Ideas"
    assert found["origin"] == {"voice": "agent",
                               "override_target": None}
    assert found["plan_md"] == "# design record"
    assert found["plan_md_missing"] is False
    assert found["agents_md"] == "# rule book"
    assert found["agents_md_missing"] is False
    assert found["sibling_plans"] == [sibling_dict(89)]
    assert found["collected_at"] == "2026-09-14T00:00:00+00:00"
    json.dumps(found)  # the packet is JSON by contract


@pytest.mark.parametrize("klass", sorted(funnel.SELF_APPROVABLE_CLASSES))
def test_agent_self_approvable_packet_carries_its_output_review(klass):
    found = packet(idea=idea_dict(klass=klass))
    assert found["output_review"] == \
        shape.AGENT_SELF_APPROVABLE_OUTPUT_REVIEW
    assert "concrete unresolved stakeholder tradeoff" \
        in found["output_review"]["scope"]
    assert "Timing, priority, and sequencing" \
        in found["output_review"]["scheduling"]
    assert "hypothetical implementation bug" \
        in found["output_review"]["escalated_risk"]


def test_other_origins_and_classes_do_not_get_agent_broken_review():
    assert "output_review" not in packet()
    assert "output_review" not in packet(
        idea=idea_dict(klass="Broken"), origin_voice="nate-relayed")
    for klass in ("New", "Replace"):
        assert "output_review" not in packet(
            idea=idea_dict(klass=klass))


def test_packet_marks_missing_instruction_files():
    found = packet(plan_md="", plan_md_missing=True,
                   agents_md="", agents_md_missing=True)
    assert found["plan_md"] == "" and found["plan_md_missing"] is True
    assert found["agents_md"] == "" and found["agents_md_missing"] is True


def test_packet_reports_an_absent_origin_as_null():
    found = packet(origin_voice=None)
    assert found["origin"] == {"voice": None, "override_target": None}


def test_siblings_are_same_repo_open_plans_sorted_by_ref():
    current = idea(42)
    kept_ready = idea(89, status="Ready", klass="New",
                      body="A ready plan.")
    kept_building = idea(91, status="Building", klass="Broken",
                         body="A building plan.")
    dropped_idea = idea(93, status="Ideas", body="Not a plan yet.")
    dropped_closed = idea(95, status="Shaped", state="CLOSED",
                          body="A closed plan.")
    dropped_ticket = idea(97, status="Shaped", parent=REPO + "#89",
                          body="A child ticket, not a plan.")
    dropped_other_repo = idea(89, repo="other/repo", status="Shaped",
                              body="Another repo's plan.")
    items = [current, dropped_other_repo, kept_building, dropped_ticket,
             dropped_closed, dropped_idea, kept_ready]
    found = shape.sibling_plan_items(items, current)
    assert [row.ref for row in found] == [REPO + "#89", REPO + "#91"]


def test_collect_reads_the_idea_and_its_siblings(monkeypatch):
    current = idea(42, klass=None)
    sibling = idea(89, status="Shaped", body="A sibling plan.")
    other_repo = idea(7, repo="other/repo", status="Shaped",
                      body="Another repo's plan.")
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    found = shape.collect(
        REPO, 42, items_loader=lambda: [current, sibling, other_repo],
        now=NOW)
    assert found["repo"] == REPO
    assert found["idea"]["ref"] == REPO + "#42"
    assert found["idea"]["klass"] is None
    assert found["origin"] == {"voice": "agent",
                               "override_target": None}
    assert found["plan_md"] == "plan.md text"
    assert found["agents_md"] == "AGENTS.md text"
    assert "issue_thread" not in found
    assert [row["ref"] for row in found["sibling_plans"]] == \
        [REPO + "#89"]
    assert found["collected_at"] == NOW.isoformat()


def test_collect_renders_both_recorded_1195_falsification_comments(monkeypatch):
    current = idea(1195, issue_comments=ISSUE_THREAD_1195)
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))

    found = shape.collect(
        REPO, 1195, items_loader=lambda: [current], now=NOW)

    thread = found["issue_thread"]
    assert thread.startswith("## Issue thread\n\n")
    positions = []
    for comment in ISSUE_THREAD_1195:
        entry = "### @{} — {}\n\n{}".format(
            comment["author"], comment["created_at"], comment["body"]
        )
        positions.append(thread.index(entry))
        assert comment["body"] in thread
    assert positions == sorted(positions)


def test_collect_omits_issue_thread_for_a_successful_empty_read(monkeypatch):
    current = idea(42, issue_comments=[])
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))

    found = shape.collect(
        REPO, 42, items_loader=lambda: [current], now=NOW)

    assert "issue_thread" not in found


def test_collect_requests_the_thread_in_its_project_item_load(monkeypatch):
    current = idea(42, issue_comments=[])
    calls = []

    def load_items(**kwargs):
        calls.append(kwargs)
        return [current]

    monkeypatch.setattr(funnel, "load_items", load_items)
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))

    shape.collect(REPO, 42, now=NOW)

    assert calls == [{"issue_comments_for": (REPO, 42)}]


def test_packet_cli_records_an_unreadable_issue_thread(monkeypatch, capsys):
    def unreadable(**kwargs):
        assert kwargs == {"issue_comments_for": (REPO, 42)}
        raise funnel.GitHubError("could not read comments for owner/repo#42")

    monkeypatch.setattr(funnel, "load_items", unreadable)

    assert shape.packet_main(["42", "--repo", REPO]) == 1
    assert "could not read comments for owner/repo#42" \
        in capsys.readouterr().err


def test_collect_rejects_an_unknown_idea():
    with pytest.raises(funnel.GitHubError):
        shape.collect(REPO, 42, items_loader=lambda: [idea(43)])


# -- applying an answer ------------------------------------------------------

def test_apply_advances_an_all_clear_agent_plan_to_ready(
        monkeypatch, capsys):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"
    assert "needs-shaping" not in item.labels

    edits = gh_calls(calls, "gh", "issue", "edit")
    assert len(edits) == 2  # the body write, then the label removal
    written = edits[0][1][-1]
    assert written.startswith("# Plan\n\nDo the thing.\n")
    assert "## Decided from precedent" in written
    assert "## Needs Nate" not in written
    assert "Proposed class: Improve" in written
    assert funnel.parse_provenance(written) == {
        "agent": "muse", "at": NOW.isoformat(), "run": "shape-run",
        "voice": "agent"}
    assert edits[1][1][-2:] == ("--remove-label", "needs-shaping")

    comments = gh_calls(calls, "gh", "issue", "comment")
    assert len(comments) == 1
    assert funnel.parse_self_approval(comments[0][1][-1]) == (
        "needs_nate all null; class Improve self-approvable; "
        "origin agent; no escalated risk")

    output = capsys.readouterr().out
    assert "owner/repo#42 → Ready" in output
    assert "advanced to Ready: needs_nate all null" in output


def test_apply_reviews_false_holds_on_an_agent_broken_replay(
        monkeypatch, capsys):
    item = idea(198, klass="Broken")
    calls = stub_gh(monkeypatch, item)
    candidate = answer(
        proposed_class="Broken",
        plan_markdown=(
            "# Guard job-log.csv read\n\n"
            "Use the guarded reader and skip the cloud write on cached input."),
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should this Broken fix be built?"],
                    "preference": None},
        escalated_risk=[{
            "reason": "destructive",
            "why": ("A hypothetical bug in the no-write path could "
                    "overwrite newer cloud rows."),
        }])

    assert shape.apply_shape(
        [item], NOW, item.ref, candidate,
        run="shape-run", agent="muse") == 0

    assert item.status == "Ready"
    body_write = gh_calls(calls, "gh", "issue", "edit")[0][1][-1]
    assert "## Needs Nate" not in body_write
    assert "Risk: escalated" not in body_write
    diagnostics = capsys.readouterr().err
    assert "generic Scope permission" in diagnostics
    assert "hypothetical implementation bug" in diagnostics


def test_apply_holds_a_plan_with_one_open_need_at_shaped(
        monkeypatch, capsys):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(needs_nate={
            "exposure": None, "gates": ["Who may write Ready?"],
            "scope": None, "preference": None}),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"
    assert gh_calls(calls, "gh", "issue", "comment") == []
    written = gh_calls(calls, "gh", "issue", "edit")[0][1][-1]
    assert "- Gates: Who may write Ready?" in written
    output = capsys.readouterr().out
    assert "held at Shaped: open question under Gates" in output
    assert "It now waits on you: is the plan good?" in output


def test_an_open_section_in_prose_does_not_hold_an_all_clear_answer(
        monkeypatch):
    # The fields rule, not the prose: a stale Needs section inside the
    # narrative cannot hold a plan whose record is all null.
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(plan_markdown=(
            "# Plan\n\nDo the thing.\n\n## Needs you\n\n"
            "- Gates: Who decides?\n")),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"


def test_a_clear_section_in_prose_does_not_release_an_open_answer(
        monkeypatch):
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(
            plan_markdown="# Plan\n\n## Needs you\n\nNothing.\n",
            needs_nate={"exposure": None, "gates": ["Who decides?"],
                        "scope": None, "preference": None}),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"


def test_apply_rejects_a_malformed_answer_before_any_write(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no write may precede validation")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    item = idea(42)
    bad = answer()
    del bad["needs_nate"]
    with pytest.raises(shape.ShapeError):
        shape.apply_shape([item], NOW, item.ref, bad)
    assert item.status == "Ideas" and item.body.startswith("Captured")


def test_apply_keeps_unset_class_untouched_when_investigate_is_rejected(
        monkeypatch):
    item = idea(42, klass=None)
    calls = stub_gh(monkeypatch, item)
    bad = answer(proposed_class="Investigate")

    with pytest.raises(shape.ShapeError, match="Possible defect"):
        shape.apply_shape([item], NOW, item.ref, bad)

    assert item.klass is None
    assert item.status == "Ideas"
    assert calls == []


def test_apply_writes_the_class_for_an_unclassed_agent_idea(monkeypatch):
    item = idea(42, klass=None)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    writes = [call for call in calls
              if call[0] == "graphql" and call[1] == funnel.SET_FIELD]
    class_writes = [call for call in writes
                    if call[2].get("field") == funnel.CLASS_FIELD_ID]
    assert len(class_writes) == 1
    assert class_writes[0][2]["option"] == "opt-Improve"
    assert item.status == "Ready"


def test_apply_does_not_rewrite_an_existing_class_for_investigate(
        monkeypatch):
    item = idea(42, klass="Investigate")
    calls = stub_gh(monkeypatch, item)
    plan_markdown = (
        "# Plan\n\n"
        "Possible defect: the route uses a different quota counter.\n")

    assert shape.apply_shape(
        [item], NOW, item.ref, answer(
            proposed_class="Investigate", plan_markdown=plan_markdown),
        run="shape-run", agent="muse") == 0

    class_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
        and call[2].get("field") == funnel.CLASS_FIELD_ID
    ]
    assert class_writes == []
    assert item.klass == "Investigate"


def test_apply_honours_and_carries_an_override_to_agents(
        monkeypatch, capsys):
    override_block = (
        funnel.ORIGIN_OVERRIDE_MARKER
        + '\n\n```json\n{"target": "agents"}\n```')
    body = (
        "Captured note.\n\n"
        + funnel.origin_block(
            "nate-relayed", at=NOW, run="capture-run", agent="muse")
        + "\n\n"
        + funnel.provenance_block(
            "nate-relayed", at=NOW, run="shape-run", agent="muse")
        + "\n\n" + override_block)
    assert funnel.parse_origin_override(body)["target"] == "agents"
    item = idea(42, body=body, origin="Nate")
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"
    written = gh_calls(calls, "gh", "issue", "edit")[0][1][-1]
    assert override_block in written
    assert "origin override to agents" in capsys.readouterr().out


def test_apply_reports_an_unconfirmed_status_without_marking(monkeypatch):
    item = idea(42)

    def graphql(query, **variables):
        raise funnel.GitHubError("boom")

    def run(args, capture_output=False, text=True, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    watched = []
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: watched.append(args) or run(*args,
                                                             **kwargs))
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 1
    assert item.status == "Ideas"
    assert "needs-shaping" in item.labels
    verbs = [tuple(args[0][:3]) for args in watched]
    assert ("gh", "issue", "edit") in verbs  # the body write happened
    assert ("gh", "issue", "comment") not in verbs
    assert all("--remove-label" not in args[0] for args in watched)


def test_apply_holds_an_escalated_plan_at_shaped(monkeypatch, capsys):
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(plan_markdown="# Plan\n\nRotate the api-key monthly.\n"),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"
    assert "escalated risk (credentials: Rotate the api-key monthly.)" \
        in capsys.readouterr().out


def test_apply_holds_a_declared_risk_with_a_clean_scan(
        monkeypatch, capsys):
    # #1034: a clean-worded plan the model flags stays at Shaped, and the
    # declared risk is canonical in the Project field; its explanation stays.
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(escalated_risk=[
            {"reason": "data-migration",
             "why": "backfills the ledger table"}]),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"
    assert item.risk == "escalated"
    assert "- data-migration: backfills the ledger table" in item.body
    assert "Risk: escalated" not in item.body
    assert not funnel.plan_is_escalated(item.body)
    assert "escalated risk (data-migration)" in capsys.readouterr().out


def test_apply_records_sequencing_edges_with_the_body_write(
        monkeypatch, capsys):
    # #1053: the "wait for #165" plan self-approves and its dependency
    # lands as a native blocked-by edge on the same gh issue edit that
    # writes the plan — one mutation, never a plan without its edge.
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(depends_on=["owner/repo#165"]),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"
    edits = gh_calls(calls, "gh", "issue", "edit")
    assert len(edits) == 2
    body_write = edits[0][1]
    assert "--add-blocked-by" in body_write
    assert body_write[body_write.index("--add-blocked-by") + 1] == "165"
    written = body_write[body_write.index("--body") + 1]
    assert "Depends on: owner/repo#165" in written
    assert "advanced to Ready: needs_nate all null" in \
        capsys.readouterr().out


def test_apply_skips_a_dependency_already_on_the_loaded_item(monkeypatch):
    # Recorded #1195 shape: #1435 is already blocked-by before re-shaping.
    item = idea(1195, blocked_by_refs=["owner/repo#1435"])
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(depends_on=["owner/repo#1435"]),
        run="shape-run", agent="muse") == 0
    edits = gh_calls(calls, "gh", "issue", "edit")
    assert edits
    assert all("--add-blocked-by" not in call[1] for call in edits)
    assert item.status == "Ready"


def test_apply_accepts_a_raced_edge_only_after_re_read(monkeypatch):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    original_graphql = funnel.gh_graphql
    rereads = []

    def graphql(query, **variables):
        if query == shape.BLOCKED_BY_QUERY:
            rereads.append(variables)
            calls.append(("graphql", query, variables))
            return blocked_by_payload(["owner/repo#165"])
        return original_graphql(query, **variables)

    def run(args, capture_output=False, text=True, **kwargs):
        calls.append(("run", tuple(args)))
        if args[:3] == ["gh", "issue", "edit"] and "--add-blocked-by" in args:
            return SimpleNamespace(
                returncode=1, stdout="",
                stderr="Target issue has already been taken",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(depends_on=["owner/repo#165"]),
        run="shape-run", agent="muse") == 0
    attempts = [call for call in gh_calls(calls, "gh", "issue", "edit")
                if "--add-blocked-by" in call[1]]
    assert len(attempts) == 1
    assert len(rereads) == 1
    assert item.blocked_by_refs == ["owner/repo#165"]
    assert item.status == "Ready"


def test_apply_keeps_a_refused_edge_failure_when_re_read_shows_no_edge(
        monkeypatch):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    original_graphql = funnel.gh_graphql
    original_run = funnel.subprocess.run

    def graphql(query, **variables):
        if query == shape.BLOCKED_BY_QUERY:
            return blocked_by_payload([])
        return original_graphql(query, **variables)

    def run(args, capture_output=False, text=True, **kwargs):
        if args[:3] == ["gh", "issue", "edit"] and "--add-blocked-by" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="refused")
        return original_run(args, capture_output=capture_output, text=text,
                            **kwargs)

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    with pytest.raises(funnel.GitHubError, match="owner/repo#165"):
        shape.apply_shape(
            [item], NOW, item.ref,
            answer(depends_on=["owner/repo#165"]),
            run="shape-run", agent="muse")
    assert item.status == "Ideas"
    assert not any(call[0] == "graphql" and call[1] != shape.BLOCKED_BY_QUERY
                   for call in calls)


def test_apply_fails_closed_when_edge_re_read_is_incomplete(monkeypatch):
    item = idea(42)
    stub_gh(monkeypatch, item)
    original_graphql = funnel.gh_graphql
    original_run = funnel.subprocess.run

    def graphql(query, **variables):
        if query == shape.BLOCKED_BY_QUERY:
            return {"repository": {"issue": {"blockedBy": {
                "totalCount": 1, "nodes": [],
            }}}}
        return original_graphql(query, **variables)

    def run(args, capture_output=False, text=True, **kwargs):
        if args[:3] == ["gh", "issue", "edit"] and "--add-blocked-by" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="refused")
        return original_run(args, capture_output=capture_output, text=text,
                            **kwargs)

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    with pytest.raises(funnel.GitHubError, match="cannot read a complete"):
        shape.apply_shape(
            [item], NOW, item.ref,
            answer(depends_on=["owner/repo#165"]),
            run="shape-run", agent="muse")
    assert item.status == "Ideas"


def test_apply_fails_before_writing_when_initial_edges_are_unavailable(
        monkeypatch):
    item = idea(42, blocked_by_refs=None)
    calls = stub_gh(monkeypatch, item)
    with pytest.raises(funnel.GitHubError, match="existing blocked-by edges"):
        shape.apply_shape(
            [item], NOW, item.ref,
            answer(depends_on=["owner/repo#165"]),
            run="shape-run", agent="muse")
    assert gh_calls(calls, "gh") == []


def test_apply_renders_cross_repo_dependencies_as_urls(monkeypatch):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(depends_on=["other/repo#7"]),
        run="shape-run", agent="muse") == 0
    body_write = gh_calls(calls, "gh", "issue", "edit")[0][1]
    assert body_write[body_write.index("--add-blocked-by") + 1] == \
        "https://github.com/other/repo/issues/7"


def test_apply_writes_no_edge_flag_when_nothing_waits(monkeypatch):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    for edit in gh_calls(calls, "gh", "issue", "edit"):
        assert "--add-blocked-by" not in edit[1]


def test_blocked_by_values_render_numbers_and_urls():
    assert shape.blocked_by_values([], "owner/repo") == []
    assert shape.blocked_by_values(["owner/repo#165"], "owner/repo") == \
        ["165"]
    assert shape.blocked_by_values(
        ["owner/repo#165", "other/repo#7"], "owner/repo") == [
            "165", "https://github.com/other/repo/issues/7"]
    with pytest.raises(shape.ShapeError):
        shape.blocked_by_values(["#165"], "owner/repo")


# -- structural guarantees --------------------------------------------------

def test_engine_imports_from_funnel_and_never_the_reverse():
    engine_source = (ROOT / "engine" / "shape.py").read_text()
    assert "import funnel" in engine_source
    funnel_source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in funnel_source
    assert "from engine" not in funnel_source


def test_shape_packet_makes_no_writes():
    # shape-apply owns the shaping writes; the packet path must not
    # perform any. If a mutating verb appears in the packet entry point,
    # the read-only contract is broken.
    mutating = ("pr merge", "pr comment", "pr close", "pr review",
                "pr edit", "pr create", "issue close", "issue create",
                "issue comment", "issue edit", "item-add", "item-edit",
                "item-delete", "--yes", "delete-branch")
    source = (ROOT / "shape-packet").read_text()
    offenders = [verb for verb in mutating if verb in source]
    assert offenders == []
    assert "packet_main" in source


def test_collect_performs_no_subprocess_call(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the packet path must not call out")

    monkeypatch.setattr(funnel, "_run_gh", fail)
    monkeypatch.setattr(funnel, "_gh_json", fail)
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    found = shape.collect(
        REPO, 42, items_loader=lambda: [idea(42)], now=NOW)
    assert found["idea"]["ref"] == REPO + "#42"


def test_the_entry_points_are_executable():
    for name in ("shape-packet", "shape-apply"):
        entry = ROOT / name
        assert entry.exists()
        assert entry.stat().st_mode & stat.S_IXUSR
    assert "apply_main" in (ROOT / "shape-apply").read_text()


# -- the CLIs ---------------------------------------------------------------

def test_packet_cli_prints_valid_json_with_every_field(
        monkeypatch, capsys):
    current = idea(42, klass=None, issue_comments=[])
    sibling = idea(89, status="Shaped", body="A sibling plan.")
    monkeypatch.setattr(
        funnel, "load_items", lambda **kwargs: [current, sibling])
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    assert shape.packet_main(["42", "--repo", REPO]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["repo"] == REPO
    assert found["idea"]["ref"] == REPO + "#42"
    assert found["origin"] == {"voice": "agent",
                               "override_target": None}
    assert found["plan_md"] == "plan.md text"
    assert found["agents_md"] == "AGENTS.md text"
    assert [row["ref"] for row in found["sibling_plans"]] == \
        [REPO + "#89"]


def test_packet_cli_reports_an_unknown_idea(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda **kwargs: [idea(43)])
    assert shape.packet_main(["42", "--repo", REPO]) == 1
    assert "shape-packet:" in capsys.readouterr().err


def test_apply_cli_reads_the_answer_from_stdin(
        monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(answer())))
    stub_gh(monkeypatch, item)
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--run", "shape-run", "--agent", "muse"]) == 0
    assert item.status == "Ready"
    assert "→ Ready" in capsys.readouterr().out


def test_apply_cli_rejects_invalid_json_before_any_read(
        monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("no GitHub read may precede parsing")

    monkeypatch.setattr(funnel, "load_items", fail)
    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_apply_cli_rejects_a_malformed_answer_without_writing(
        monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def fail(*args, **kwargs):
        raise AssertionError("no write may precede validation")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    bad = answer()
    del bad["plan_markdown"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad)))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-"]) == 1
    assert "plan_markdown" in capsys.readouterr().err
    assert item.status == "Ideas"


# -- the runner protocol: --attempt and --validate-only ----------------------

def test_validation_exit_maps_attempts_to_retry_then_final():
    """#811: without --attempt the single-shot exit 1 stands; with it, a
    malformed answer is retryable below attempt 2 and final at it."""
    assert shape.validation_exit(None) == 1
    assert shape.validation_exit(1) == 3
    assert shape.validation_exit(2) == 1
    assert shape.validation_exit(3) == 1


def test_apply_cli_with_attempt_1_asks_for_a_retry(monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def fail(*args, **kwargs):
        raise AssertionError("no write may precede validation")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    bad = answer()
    del bad["plan_markdown"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad)))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--attempt", "1"]) == 3
    assert "plan_markdown" in capsys.readouterr().err
    assert item.status == "Ideas"


def test_apply_cli_with_attempt_2_is_final(monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def fail(*args, **kwargs):
        raise AssertionError("no write may precede validation")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    bad = answer()
    del bad["plan_markdown"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad)))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--attempt", "2"]) == 1
    assert "plan_markdown" in capsys.readouterr().err
    assert item.status == "Ideas"


def test_apply_cli_with_attempt_rejects_invalid_json(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("no GitHub read may precede parsing")

    monkeypatch.setattr(funnel, "load_items", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--attempt", "1"]) == 3
    assert "not valid JSON" in capsys.readouterr().err
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--attempt", "2"]) == 1


def test_apply_cli_validate_only_reports_the_decision_without_writing(
        monkeypatch, capsys):
    """Decide from the same inputs as the live path and report, without
    touching the idea; the runner uses this to validate a retried answer."""
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def fail(*args, **kwargs):
        raise AssertionError("validate-only must not reach GitHub")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(answer())))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--validate-only"]) == 0
    found = json.loads(capsys.readouterr().out)
    # The same answer advances the live path to Ready
    # (test_apply_cli_reads_the_answer_from_stdin): the preview must agree.
    assert found["status"] == "Ready"
    assert "needs_nate all null" in found["reason"]
    assert found["answer"]["proposed_class"] == "Improve"
    assert item.status == "Ideas"


def test_apply_cli_validate_only_previews_a_shaped_decision(
        monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def fail(*args, **kwargs):
        raise AssertionError("validate-only must not reach GitHub")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    held = answer(needs_nate={"exposure": ["May this ship in September?"],
                              "gates": None, "scope": None,
                              "preference": None})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(held)))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--validate-only"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["status"] == "Shaped"
    assert "open question under Exposure" in found["reason"]
    assert item.status == "Ideas"


def test_apply_cli_validate_only_rejects_a_malformed_answer(
        monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    bad = answer()
    del bad["plan_markdown"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad)))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-", "--validate-only",
         "--attempt", "1"]) == 3
    assert "plan_markdown" in capsys.readouterr().err


def test_apply_main_rejects_an_attempt_below_1(monkeypatch):
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(answer())))
    with pytest.raises(SystemExit) as caught:
        shape.apply_main(["42", "--repo", REPO, "--answer", "-",
                          "--attempt", "0"])
    assert caught.value.code == 2


# -- a closed issue is never self-approvable (#1206, #1208) -----------------


def test_a_closed_issue_is_never_self_approvable():
    """Advancing one to Ready puts it in the startable queue with nothing
    able to close it again. The state is a term of the one predicate, not a
    second gate beside it."""
    assert funnel.self_approval_eligible(
        "Broken", "agent", None, needs_nate=False, escalated=False,
        state="CLOSED",
    ) is False
    assert funnel.self_approval_eligible(
        "Broken", "agent", None, needs_nate=False, escalated=False,
        state="OPEN",
    ) is True


def test_an_absent_state_leaves_the_rule_exactly_as_it_was():
    assert funnel.self_approval_eligible(
        "Broken", "agent", None, needs_nate=False, escalated=False,
    ) is True
    assert funnel.self_approval_eligible(
        "New", "agent", None, needs_nate=False, escalated=False,
    ) is False


def test_decide_holds_a_closed_issue_and_says_why():
    validated = shape.validate_answer(answer())

    status, reason = shape.decide(
        validated, klass="Broken", origin_voice="agent", state="CLOSED")

    assert status == "Shaped"
    assert "the issue is CLOSED on GitHub" in reason


def test_decide_on_an_open_issue_is_unchanged():
    validated = shape.validate_answer(answer())

    assert shape.decide(
        validated, klass="Broken", origin_voice="agent", state="OPEN"
    ) == shape.decide(validated, klass="Broken", origin_voice="agent")
