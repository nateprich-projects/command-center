"""Shape answers built from small parts (#1598, plan #1581).

The framer, sibling checks, deciders and auditor each answer a slice of
the shape question; code merges their parts into exactly the answer
``shape-apply`` already validates. These fixtures pin the packet slices,
the exactly-once rule for every assigned item, and the merge rules.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import shape  # noqa: E402
from engine import shape_split  # noqa: E402
from engine.shape import ShapeError  # noqa: E402

REPO = "owner/repo"


def sibling(number, **kw):
    row = {
        "ref": "{}#{}".format(REPO, number),
        "number": number,
        "title": "Sibling plan {}".format(number),
        "status": "Shaped",
        "klass": "Improve",
        "body": "# Sibling {}\n\nA long plan body {}.\n".format(
            number, number),
    }
    row.update(kw)
    return row


def packet(siblings=(), **kw):
    built = shape.build_packet(
        repo=REPO,
        idea={"ref": REPO + "#42", "number": 42, "title": "An idea",
              "url": "https://github.com/owner/repo/issues/42",
              "body": "Captured note.", "labels": ["needs-shaping"],
              "status": "Ideas", "klass": "Improve"},
        origin_voice="agent",
        override_target=None,
        plan_md="# plan.md rules",
        plan_md_missing=False,
        agents_md="# AGENTS.md rules",
        agents_md_missing=False,
        siblings=[sibling(number) for number in siblings],
        collected_at="2026-09-26T00:00:00+00:00",
        issue_comments=[{"author": {"login": "nateprich"},
                         "createdAt": "2026-09-25T10:00:00Z",
                         "body": "Please keep it small."}],
    )
    built.update(kw)
    return built


def framer_answer(**kw):
    data = {
        "proposed_class": "Improve",
        "plan_markdown": "# Plan\n\nDo the thing.\n",
        "decision_points": ["Which file holds it", "Whether to log it",
                            "Who sees the output"],
        "depends_on": [],
    }
    data.update(kw)
    return data


def draft(**kw):
    return shape_split.parse_framer(framer_answer(**kw))


def sibling_entry(number, relation="independent", why="different code"):
    return {"ref": "{}#{}".format(REPO, number), "relation": relation,
            "why": why}


def decisions_for(points):
    kinds = [
        {"kind": "precedent", "claim": "it lives in engine/",
         "source": "AGENTS.md"},
        {"kind": "agent", "decision": "log to stderr",
         "alternative": "log to a file", "why": "no state files"},
        {"kind": "nate", "category": "exposure",
         "question": "May the output name private repositories?"},
    ]
    return [dict(kinds[index % len(kinds)], point=point)
            for index, point in enumerate(points)]


def audit_answer(**kw):
    data = {
        "premises": [{"claim": "the packet is fetched once",
                      "evidence": "engine/shape.py:1027",
                      "label": "measured"}],
        "escalated_risk": [],
    }
    data.update(kw)
    return data


def merged(framer=None, siblings=(), decisions=None, audit=None):
    framer = framer or draft()
    if decisions is None:
        decisions = shape_split.parse_decider(
            {"decisions": decisions_for(framer["decision_points"])},
            framer["decision_points"])
    return shape_split.merge_shape_answer(
        framer, list(siblings), decisions,
        shape_split.parse_auditor(audit or audit_answer()))


# -- packets -------------------------------------------------------------

def test_framer_packet_carries_no_sibling_bodies():
    source = packet(siblings=(5, 6))
    framed = shape_split.framer_packet(source)
    assert "sibling_plans" not in framed
    assert framed["sibling_index"] == [
        {"ref": REPO + "#5", "title": "Sibling plan 5", "status": "Shaped",
         "klass": "Improve"},
        {"ref": REPO + "#6", "title": "Sibling plan 6", "status": "Shaped",
         "klass": "Improve"},
    ]
    assert "A long plan body" not in json.dumps(framed)
    assert framed["plan_md"] == "# plan.md rules"
    assert framed["issue_thread"] == source["issue_thread"]
    # The shape packet itself is left whole for the other parts.
    assert len(source["sibling_plans"]) == 2


def test_seven_siblings_take_three_calls_of_at_most_three():
    source = packet(siblings=range(1, 8))
    parts = shape_split.sibling_packets(source, draft())
    assert [[row["ref"] for row in part["sibling_plans"]]
            for part in parts] == [
        [REPO + "#1", REPO + "#2", REPO + "#3"],
        [REPO + "#4", REPO + "#5", REPO + "#6"],
        [REPO + "#7"],
    ]


def test_sibling_packets_carry_their_own_bodies_and_no_rules():
    source = packet(siblings=range(1, 5))
    parts = shape_split.sibling_packets(source, draft())
    second = parts[1]
    assert second["sibling_plans"][0]["body"].startswith("# Sibling 4")
    assert "Sibling 1" not in json.dumps(second)
    for key in ("plan_md", "agents_md", "sibling_index"):
        assert key not in second
    assert second["idea"]["ref"] == REPO + "#42"
    assert second["origin"] == {"voice": "agent", "override_target": None}
    assert "Please keep it small." in second["issue_thread"]
    assert second["draft"]["plan_markdown"] == "# Plan\n\nDo the thing."


def test_no_siblings_means_no_sibling_calls():
    assert shape_split.sibling_packets(packet(), draft()) == []


def test_decider_packets_chunk_points_and_carry_rules_and_index():
    points = ["p{}".format(index) for index in range(1, 8)]
    source = packet(siblings=(5,))
    parts = shape_split.decider_packets(source, draft(), points)
    assert [part["decision_points"] for part in parts] == [
        ["p1", "p2", "p3"], ["p4", "p5", "p6"], ["p7"]]
    first = parts[0]
    assert first["plan_md"] == "# plan.md rules"
    assert first["agents_md"] == "# AGENTS.md rules"
    assert first["sibling_index"][0]["ref"] == REPO + "#5"
    assert "sibling_plans" not in first
    assert "A long plan body" not in json.dumps(first)
    assert first["output_review"] == source["output_review"]


def test_decider_packets_refuse_empty_decision_points():
    with pytest.raises(ShapeError, match="non-empty list"):
        shape_split.decider_packets(packet(), draft(), [])


def test_auditor_packet_carries_idea_thread_and_draft_only():
    part = shape_split.auditor_packet(packet(siblings=(5,)), draft())
    assert set(part) == {"repo", "idea", "origin", "collected_at",
                         "issue_thread", "draft", "output_review"}


# -- framer --------------------------------------------------------------

def test_framer_answer_validates_and_accepts_raw_json():
    parsed = shape_split.parse_framer(json.dumps(framer_answer(
        depends_on=[" owner/repo#9 "])))
    assert parsed["depends_on"] == ["owner/repo#9"]
    assert parsed["decision_points"][0] == "Which file holds it"


def test_framer_refuses_empty_decision_points():
    with pytest.raises(ShapeError, match="decision_points"):
        shape_split.parse_framer(framer_answer(decision_points=[]))


def test_framer_refuses_repeated_decision_points():
    with pytest.raises(ShapeError, match="repeats"):
        shape_split.parse_framer(framer_answer(
            decision_points=["same point", "same  point"]))


@pytest.mark.parametrize("change", [
    {"proposed_class": "Urgent"},
    {"plan_markdown": "  "},
    {"depends_on": ["#9"]},
    {"plan_markdown": "# Plan\n\n## Siblings checked\n\n- none"},
])
def test_framer_refuses_malformed_fields(change):
    with pytest.raises(ShapeError):
        shape_split.parse_framer(framer_answer(**change))


def test_framer_refuses_missing_and_extra_keys():
    data = framer_answer()
    del data["depends_on"]
    with pytest.raises(ShapeError, match="missing depends_on"):
        shape_split.parse_framer(data)
    with pytest.raises(ShapeError, match="unknown fields"):
        shape_split.parse_framer(framer_answer(needs_nate={}))


def test_framer_refuses_invalid_json():
    with pytest.raises(ShapeError, match="not valid JSON"):
        shape_split.parse_framer("{not json")


# -- sibling checks ------------------------------------------------------

def test_sibling_check_returns_entries_in_assigned_order():
    refs = [REPO + "#1", REPO + "#2"]
    parsed = shape_split.parse_siblings(
        {"siblings": [sibling_entry(2, "overlaps", "same file"),
                      sibling_entry(1)]}, refs)
    assert [entry["ref"] for entry in parsed] == refs


@pytest.mark.parametrize("entries, message", [
    ([sibling_entry(1)], "does not answer"),
    ([sibling_entry(1), sibling_entry(1), sibling_entry(2)], "exactly once"),
    ([sibling_entry(1), sibling_entry(2), sibling_entry(3)], "unassigned"),
])
def test_sibling_check_answers_each_assigned_ref_exactly_once(
        entries, message):
    with pytest.raises(ShapeError, match=message):
        shape_split.parse_siblings({"siblings": entries},
                                   [REPO + "#1", REPO + "#2"])


def test_sibling_check_refuses_an_unknown_relation():
    with pytest.raises(ShapeError, match="relation"):
        shape_split.parse_siblings(
            {"siblings": [sibling_entry(1, "blocks")]}, [REPO + "#1"])


def test_sibling_check_refuses_more_than_three_assigned():
    with pytest.raises(ShapeError, match="at most 3"):
        shape_split.parse_siblings(
            {"siblings": []}, [REPO + "#{}".format(n) for n in range(1, 5)])


# -- deciders ------------------------------------------------------------

def test_decider_returns_entries_in_assigned_order():
    points = ["a", "b", "c"]
    entries = decisions_for(points)
    parsed = shape_split.parse_decider(
        {"decisions": list(reversed(entries))}, points)
    assert [entry["point"] for entry in parsed] == points
    assert [entry["kind"] for entry in parsed] == [
        "precedent", "agent", "nate"]


@pytest.mark.parametrize("points_answered, message", [
    (["a"], "does not answer"),
    (["a", "b", "b"], "exactly once"),
    (["a", "b", "z"], "unassigned"),
])
def test_decider_answers_each_assigned_point_exactly_once(
        points_answered, message):
    with pytest.raises(ShapeError, match=message):
        shape_split.parse_decider(
            {"decisions": decisions_for(points_answered)}, ["a", "b"])


@pytest.mark.parametrize("entry", [
    {"point": "a", "kind": "guess", "claim": "x", "source": "y"},
    {"point": "a", "kind": [], "claim": "x", "source": "y"},
    {"point": "a", "kind": "precedent", "claim": "x"},
    {"point": "a", "kind": "agent", "decision": "x", "alternative": "y",
     "why": "z", "claim": "extra"},
    {"point": "a", "kind": "nate", "category": "urgency", "question": "q?"},
    {"point": "a", "kind": "nate", "category": "scope", "question": " "},
])
def test_decider_refuses_malformed_entries(entry):
    with pytest.raises(ShapeError):
        shape_split.parse_decider({"decisions": [entry]}, ["a"])


def test_decider_refuses_no_assigned_points():
    with pytest.raises(ShapeError, match="non-empty list"):
        shape_split.parse_decider({"decisions": []}, [])


# -- auditor -------------------------------------------------------------

def test_auditor_reuses_the_premise_and_risk_validators():
    assert shape_split.parse_auditor(audit_answer())["premises"][0][
        "label"] == "measured"
    with pytest.raises(ShapeError, match="label"):
        shape_split.parse_auditor(audit_answer(premises=[
            {"claim": "c", "evidence": "e", "label": "guessed"}]))
    with pytest.raises(ShapeError, match="escalation reason"):
        shape_split.parse_auditor(audit_answer(escalated_risk=[
            {"reason": "spooky", "why": "w"}]))
    with pytest.raises(ShapeError, match="unknown fields"):
        shape_split.parse_auditor(audit_answer(depends_on=[]))


# -- merge ---------------------------------------------------------------

def test_merged_answer_passes_shape_validation_with_exactly_answer_keys():
    siblings = [sibling_entry(5), sibling_entry(6, "depends_on", "needs 6")]
    answer = merged(siblings=siblings)
    assert set(answer) == shape.ANSWER_KEYS
    assert shape.validate_answer(copy.deepcopy(answer)) == answer
    assert answer["decided_from_precedent"] == [
        {"claim": "it lives in engine/", "source": "AGENTS.md"}]
    assert answer["decided_by_agent"] == [
        {"decision": "log to stderr", "alternative": "log to a file",
         "why": "no state files"}]
    assert answer["needs_nate"] == {
        "exposure": ["May the output name private repositories?"],
        "gates": None, "scope": None, "preference": None}
    assert shape.render_plan(answer)


def test_merge_orders_decisions_by_framer_points_whatever_the_call_order():
    framer = draft(decision_points=["p1", "p2", "p3", "p4"])
    decisions = [
        {"point": "p3", "kind": "agent", "decision": "d3",
         "alternative": "a3", "why": "w3"},
        {"point": "p4", "kind": "agent", "decision": "d4",
         "alternative": "a4", "why": "w4"},
        {"point": "p1", "kind": "agent", "decision": "d1",
         "alternative": "a1", "why": "w1"},
        {"point": "p2", "kind": "nate", "category": "scope",
         "question": "q2?"},
    ]
    answer = merged(framer=framer, decisions=decisions)
    assert [entry["decision"] for entry in answer["decided_by_agent"]] == [
        "d1", "d3", "d4"]
    assert answer == merged(framer=framer,
                            decisions=list(reversed(decisions)))


def test_merge_refuses_a_lost_or_doubled_decision():
    framer = draft()
    decisions = shape_split.parse_decider(
        {"decisions": decisions_for(framer["decision_points"])},
        framer["decision_points"])
    with pytest.raises(ShapeError, match="does not answer"):
        merged(framer=framer, decisions=decisions[:2])
    with pytest.raises(ShapeError, match="exactly once"):
        merged(framer=framer, decisions=decisions + decisions[:1])


def test_merge_refuses_a_doubled_sibling():
    with pytest.raises(ShapeError, match="more than once"):
        merged(siblings=[sibling_entry(5), sibling_entry(5)])


def test_an_overlapping_sibling_becomes_one_scope_question_naming_it():
    answer = merged(siblings=[
        sibling_entry(5),
        sibling_entry(6, "overlaps", "both rewrite the packet builder."),
    ])
    scope = answer["needs_nate"]["scope"]
    assert len(scope) == 1
    assert REPO + "#6" in scope[0]
    assert "both rewrite the packet builder" in scope[0]
    assert REPO + "#5" not in scope[0]
    # A concrete question: the agent-origin output review keeps it open.
    reviewed, rejected = shape.review_agent_shape_output(
        answer, klass="Improve", origin_voice="agent", repo=REPO)
    assert reviewed["needs_nate"]["scope"] == scope
    assert rejected == []


def test_overlap_questions_follow_decider_scope_questions():
    framer = draft(decision_points=["p1"])
    decisions = [{"point": "p1", "kind": "nate", "category": "scope",
                  "question": "Should the report include archived repos?"}]
    answer = merged(framer=framer, decisions=decisions,
                    siblings=[sibling_entry(6, "overlaps", "same file")])
    scope = answer["needs_nate"]["scope"]
    assert scope[0] == "Should the report include archived repos?"
    assert REPO + "#6" in scope[1]


def test_empty_needs_categories_are_null():
    framer = draft(decision_points=["p1"])
    decisions = [{"point": "p1", "kind": "agent", "decision": "d",
                  "alternative": "a", "why": "w"}]
    answer = merged(framer=framer, decisions=decisions)
    assert answer["needs_nate"] == {
        "exposure": None, "gates": None, "scope": None, "preference": None}
    assert not shape.needs_nate_open(answer)


def test_depends_on_unions_framer_and_siblings_deduplicated_stably():
    framer = draft(depends_on=[REPO + "#9", REPO + "#6"])
    answer = merged(framer=framer, siblings=[
        sibling_entry(7, "depends_on", "needs 7"),
        sibling_entry(6, "depends_on", "needs 6"),
        sibling_entry(5),
    ])
    assert answer["depends_on"] == [REPO + "#9", REPO + "#6", REPO + "#7"]


def test_siblings_checked_section_is_appended_by_code():
    answer = merged(siblings=[
        sibling_entry(5, "independent", "different code"),
        sibling_entry(6, "depends_on", "needs its parser"),
    ])
    assert answer["plan_markdown"].startswith("# Plan\n\nDo the thing.")
    assert answer["plan_markdown"].endswith(
        "## Siblings checked\n\n"
        "- owner/repo#5 (independent): different code\n"
        "- owner/repo#6 (depends_on): needs its parser")


def test_no_siblings_says_no_open_sibling_plans():
    answer = merged(siblings=[])
    assert answer["plan_markdown"].endswith(
        "## Siblings checked\n\nNo open sibling plans.")
    assert "No open sibling plans." in shape.render_plan(answer)


def test_merge_carries_the_audit_through():
    risk = [{"reason": "credentials",
             "why": "the plan stores an access token"}]
    answer = merged(audit=audit_answer(escalated_risk=risk))
    assert answer["escalated_risk"] == risk
    assert answer["premises"][0]["evidence"] == "engine/shape.py:1027"
