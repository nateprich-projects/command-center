"""Pure fixtures for the bounded max-review judges (#1242)."""

import json

import pytest

from engine.review import (
    ReviewJudgeError,
    chunk_requirements,
    derive_judge_answer,
    parse_judge_answer,
    uncertain_judge_results,
)


@pytest.mark.parametrize(
    "count, sizes",
    [(1, [1]), (3, [3]), (4, [3, 1]), (7, [3, 3, 1])],
)
def test_chunks_hold_at_most_three_requirements_and_drop_none(count, sizes):
    requirements = ["requirement {}".format(i) for i in range(count)]

    chunks = chunk_requirements(requirements)

    assert [len(chunk) for chunk in chunks] == sizes
    assert all(len(chunk) <= 3 for chunk in chunks)
    assert [requirement for chunk in chunks for requirement in chunk] == \
        requirements
    assert [sum(chunk.count(requirement) for chunk in chunks)
            for requirement in requirements] == [1] * count


def test_a_judge_answer_is_scoped_to_its_chunk_and_reordered_canonically():
    expected = ["first", "second"]
    raw = json.dumps({"requirements": [
        {"requirement": "second", "status": "met", "evidence": "line 2"},
        {"requirement": "first", "status": "met", "evidence": "line 1"},
    ]})

    assert parse_judge_answer(raw, expected) == [
        {"requirement": "first", "status": "met", "evidence": "line 1"},
        {"requirement": "second", "status": "met", "evidence": "line 2"},
    ]


def test_a_judge_parser_rejects_more_than_three_assigned_requirements():
    with pytest.raises(ReviewJudgeError, match="at most 3"):
        parse_judge_answer("{}", ["one", "two", "three", "four"])


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"verdict": "approved", "requirements": []}),
    json.dumps({"requirements": []}),
    json.dumps({"requirements": [
        {"requirement": "first", "status": "met", "evidence": "line 1"},
    ]}),
    json.dumps({"requirements": [
        {"requirement": "first", "status": "met", "evidence": "line 1"},
        {"requirement": "first", "status": "met", "evidence": "line 1"},
    ]}),
    json.dumps({"requirements": [
        {"requirement": "first", "status": "unknown", "evidence": "line 1"},
        {"requirement": "second", "status": "met", "evidence": "line 2"},
    ]}),
])
def test_malformed_or_unassigned_judge_results_are_rejected(raw):
    with pytest.raises(ReviewJudgeError):
        parse_judge_answer(raw, ["first", "second"])


def test_all_met_results_derive_approval_in_code():
    requirements = ["first", "second"]
    results = [
        {"requirement": item, "status": "met", "evidence": "diff line"}
        for item in requirements
    ]

    answer = derive_judge_answer(requirements, results)

    assert answer["verdict"] == "approved"
    assert answer["blocking"] == []
    assert [entry["requirement"] for entry in answer["requirements"]] == \
        requirements


def test_one_unmet_result_derives_rejection_with_that_requirement():
    answer = derive_judge_answer(
        ["first", "second"],
        [
            {"requirement": "first", "status": "met", "evidence": "line 1"},
            {"requirement": "second", "status": "unmet",
             "evidence": "missing from the diff"},
        ],
    )

    assert answer["verdict"] == "rejected"
    assert answer["blocking"] == [
        "requirement unmet: second -- missing from the diff"
    ]


def test_one_unsure_result_derives_rejection():
    answer = derive_judge_answer(
        ["first"],
        [{"requirement": "first", "status": "unsure",
          "evidence": "the diff does not resolve this"}],
    )

    assert answer["verdict"] == "rejected"
    assert answer["blocking"] == [
        "requirement unsure: first -- the diff does not resolve this"
    ]


@pytest.mark.parametrize("reason", [
    "judge call failed (exit 1): provider failure",
    "judge call timed out after 180 seconds",
])
def test_failed_or_timed_out_judge_call_rejects_every_requirement_in_its_chunk(
        reason):
    requirements = ["first", "second"]
    answer = derive_judge_answer(
        requirements, uncertain_judge_results(requirements, reason))

    assert answer["verdict"] == "rejected"
    assert answer["requirements"] == [
        {"requirement": item, "status": "unsure", "evidence": reason}
        for item in requirements
    ]
    assert len(answer["blocking"]) == len(requirements)


def test_missing_result_fails_closed_for_the_missing_requirement():
    answer = derive_judge_answer(
        ["first", "second"],
        [{"requirement": "first", "status": "met", "evidence": "line 1"}],
    )

    assert answer["verdict"] == "rejected"
    assert answer["requirements"][1] == {
        "requirement": "second",
        "status": "unsure",
        "evidence": "No valid judge result was recorded for this requirement",
    }
