"""Premise probes are never review requirements (#1966).

A review weighs the ticket's own Do and Accept; the ticket's first
verification step checks the plan's premises (Nate, 2026-09-28). On
2026-09-29 the Muse lane listed a probe for every inferred premise of plan
#1769, judged each against an issue body the packet never carries, and
rejected CI-green PR #1941 on nothing else. These tests replay those
recorded requirement lists.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import review  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "review_premise_probes_1941.json"
REJECTED_1612 = ROOT / "tests" / "fixtures" / "review_rejected_1612.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def packet_1941(data):
    return {
        "ticket": {"ref": data["ticket_ref"], "number": 1937},
        "plan_premises": [{
            "parent_ref": data["parent_ref"],
            "ticket_refs": [data["ticket_ref"]],
            "available": True,
            "premises": data["premises"],
        }],
    }


@pytest.mark.parametrize("index", [0, 1])
def test_reproduction_1941_lists_keep_no_premise_probe(index):
    data = fixture()
    verdict = data["verdicts"][index]
    assert any(row.startswith("Probe inferred premise")
               for row in verdict["requirements"])

    result = review.normalize_plan_premise_requirements(
        packet_1941(data), verdict["requirements"])

    assert result == verdict["expected_kept"]
    assert not any("premise" in row.casefold() for row in result)


def test_the_parent_plan_probe_wording_is_dropped_too():
    data = fixture()
    claim = data["premises"][1]["claim"]
    probe = (
        "Probe the parent plan #1769 premise labelled inferred against live "
        "evidence using its evidence pointer: '{}' (evidence pointer: {}); "
        "cite support or contradiction, and record unsure if unresolved."
    ).format(claim, data["premises"][1]["evidence"])
    do_line = "Add a test that kills the cut-at-last-provenance mutant"

    result = review.normalize_plan_premise_requirements(
        packet_1941(data), [probe, do_line])

    assert result == [do_line]


def test_rows_that_do_not_probe_a_premise_are_kept():
    data = fixture()
    claim = data["premises"][1]["claim"]
    restates_claim = "Change it so {} no longer holds".format(claim)
    names_premise_only = "Record the premise check's evidence in the PR body"

    result = review.normalize_plan_premise_requirements(
        packet_1941(data), [restates_claim, names_premise_only])

    assert result == [restates_claim, names_premise_only]


def test_a_packet_without_premises_keeps_every_row():
    rows = ["Probe inferred premise 'x' via its evidence pointer", "Do it"]

    assert review.normalize_plan_premise_requirements({}, rows) == rows


def test_a_verified_deferral_still_yields_its_canonical_row():
    source = json.loads(REJECTED_1612.read_text())
    packet = source["packet"]
    packet["plan_premises"][0]["premises"][0]["deferred_answer"] = (
        source["expected_deferred_answer"])
    do_line = "The framer answers within its time budget"

    result = review.normalize_plan_premise_requirements(
        packet, [source["rejected_requirement"], do_line])

    assert result == [
        do_line,
        "Defer the inferred premise 'The split framer may itself still go "
        "silent' to its evidence pointer 'ticket 4, #1600, checks' until "
        "ticket #1598 is complete.",
    ]


def test_the_lister_header_no_longer_asks_for_premise_probes():
    engine = (ROOT / "scripts" / "muse-review-engine").read_text()
    header = engine.split("lister_header() {", 1)[1].split("\nHEADER", 1)[0]

    assert "keeps the normal probe requirement" not in header
    assert "Never list a requirement\nthat probes a `plan_premises` entry" \
        in header
    assert "still follows the normal\nprobe rule" not in engine


def test_the_review_routine_no_longer_asks_for_premise_probes():
    routine = (ROOT / "routines" / "muse-review.md").read_text()
    prompt = routine.split("\n---\n", 1)[1]

    assert "Probe every `plan_premises` entry" not in prompt
    assert "do not probe premises" in prompt


def test_a_short_claim_matches_only_as_whole_words():
    packet = {"plan_premises": [{"available": True,
                                 "premises": [{"claim": "CI",
                                               "evidence": "x",
                                               "label": "inferred"}]}]}
    kept = "Record the premise check's decision in the PR body"
    probe = "Probe inferred premise 'CI' via its evidence pointer 'x'"

    assert review.normalize_plan_premise_requirements(
        packet, [kept, probe]) == [kept]


def test_probes_of_a_second_plan_are_dropped_too():
    # A PR can close tickets from two plans; each group's premises count.
    packet = {"plan_premises": [
        {"available": True, "premises": [
            {"claim": "the first plan's claim holds", "evidence": "a",
             "label": "inferred"}]},
        {"available": True, "premises": [
            {"claim": "the second plan's claim holds", "evidence": "b",
             "label": "inferred"}]},
    ]}
    probe = "Probe inferred premise 'the second plan's claim holds' via 'b'"

    assert review.normalize_plan_premise_requirements(
        packet, [probe, "Do it"]) == ["Do it"]
