"""The shaping pipeline reads one idea's inputs once (#2136, #1746).

The packet, the output review, preview and apply each read an idea's origin,
its origin override and its effective Class. Each used to read them itself,
and the copies drifted: on ef9af026d the packet for an agent-origin idea with
no Class carried no output-review guidance, while the output review still
stripped a generic Scope question from that idea's answer.
``shape.shape_inputs`` is now the one reading, and these tests hold every
caller to it.

``MATRIX`` and the expectation tables below are the #1746 shared fixture
matrix. Later tickets in that redesign extend them here rather than building
their own. Expected values are written out from the ticket, not computed the
reader's way.
"""

from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from typing import Optional

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import shape  # noqa: E402
from test_engine_shape import (  # noqa: E402  (shared fixture harness)
    NOW,
    REPO,
    answer,
    gh_calls,
    idea,
    stub_gh,
)


@pytest.fixture(autouse=True)
def canonical_field_writes(monkeypatch):
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: None,
    )


@pytest.fixture(autouse=True)
def offline_instruction_files(monkeypatch):
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))


# -- the shared fixture matrix ----------------------------------------------

@dataclass(frozen=True)
class ShapeCase:
    """One idea in the matrix: who raised it, its Class, its GitHub state."""

    origin: str
    klass: Optional[str]
    state: str

    @property
    def key(self):
        return (self.origin, self.klass)


#: Who raised the idea. The third is a Nate-raised idea that a Nate-voiced
#: origin-override block hands to agents.
ORIGINS = ("agent", "nate", "nate-override-to-agents")
#: A self-approvable Class, another Class, and none.
CLASSES = ("Broken", "New", None)
STATES = ("OPEN", "CLOSED")

MATRIX = [ShapeCase(origin, klass, state)
          for origin in ORIGINS for klass in CLASSES for state in STATES]

OVERRIDE_TO_AGENTS = (
    funnel.ORIGIN_OVERRIDE_MARKER + '\n\n```json\n{"target": "agents"}\n```')


def matrix_item(case: ShapeCase, number: int = 42):
    """The Ideas row for one case, its body carrying the origin record."""
    if case.origin == "agent":
        body = "Captured note.\n\n" + funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="muse")
        return idea(number, klass=case.klass, state=case.state,
                    origin="agent", body=body)
    body = "Captured note.\n\n" + funnel.origin_block(
        "nate-relayed", at=NOW, run="capture-run", agent="muse")
    if case.origin == "nate-override-to-agents":
        body += "\n\n{}\n\n{}".format(
            funnel.provenance_block(
                "nate-relayed", at=NOW, run="override-run", agent="claude"),
            OVERRIDE_TO_AGENTS)
    return idea(number, klass=case.klass, state=case.state, origin="Nate",
                body=body)


def generic_permission_answer():
    """An answer proposing Broken that asks only generic permission."""
    return shape.validate_answer(answer(
        proposed_class="Broken",
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should we fix this?"], "preference": None}))


#: What the reader says for each origin and Class, read without an answer
#: (the packet) and with ``generic_permission_answer`` (review, preview,
#: apply). The Class is adopted from the answer only for an agent-raised idea
#: with none; the output review stays agent-origin only, and before the
#: answer an adopted Class still counts, because the answer may propose a
#: self-approvable one.
EXPECTED_INPUTS = {
    ("agent", "Broken"): dict(
        origin_voice="agent", override_target=None, class_adopted=False,
        klass_before="Broken", klass="Broken",
        review_before=True, review=True),
    ("agent", "New"): dict(
        origin_voice="agent", override_target=None, class_adopted=False,
        klass_before="New", klass="New",
        review_before=False, review=False),
    ("agent", None): dict(
        origin_voice="agent", override_target=None, class_adopted=True,
        klass_before=None, klass="Broken",
        review_before=True, review=True),
    ("nate", "Broken"): dict(
        origin_voice="Nate", override_target=None, class_adopted=False,
        klass_before="Broken", klass="Broken",
        review_before=False, review=False),
    ("nate", "New"): dict(
        origin_voice="Nate", override_target=None, class_adopted=False,
        klass_before="New", klass="New",
        review_before=False, review=False),
    ("nate", None): dict(
        origin_voice="Nate", override_target=None, class_adopted=False,
        klass_before=None, klass=None,
        review_before=False, review=False),
    ("nate-override-to-agents", "Broken"): dict(
        origin_voice="Nate", override_target="agents", class_adopted=False,
        klass_before="Broken", klass="Broken",
        review_before=False, review=False),
    ("nate-override-to-agents", "New"): dict(
        origin_voice="Nate", override_target="agents", class_adopted=False,
        klass_before="New", klass="New",
        review_before=False, review=False),
    ("nate-override-to-agents", None): dict(
        origin_voice="Nate", override_target="agents", class_adopted=False,
        klass_before=None, klass=None,
        review_before=False, review=False),
}

_SCOPE_OPEN = "open question under Scope and priority"

#: The status and reason for the reviewed ``generic_permission_answer`` on an
#: OPEN issue. Where the review applies it strips the generic question.
EXPECTED_OPEN_DECISION = {
    ("agent", "Broken"): (
        "Ready",
        "needs_nate all null; class Broken self-approvable; origin agent"),
    ("agent", "New"): (
        "Shaped", "class New is not self-approvable; " + _SCOPE_OPEN),
    ("agent", None): (
        "Ready",
        "needs_nate all null; class Broken self-approvable; origin agent"),
    ("nate", "Broken"): ("Shaped", "origin is Nate's; " + _SCOPE_OPEN),
    ("nate", "New"): (
        "Shaped",
        "class New is not self-approvable; origin is Nate's; " + _SCOPE_OPEN),
    ("nate", None): (
        "Shaped",
        "class unset is not self-approvable; origin is Nate's; "
        + _SCOPE_OPEN),
    ("nate-override-to-agents", "Broken"): ("Shaped", _SCOPE_OPEN),
    ("nate-override-to-agents", "New"): (
        "Shaped", "class New is not self-approvable; " + _SCOPE_OPEN),
    ("nate-override-to-agents", None): (
        "Shaped", "class unset is not self-approvable; " + _SCOPE_OPEN),
}


def expected_decision(case: ShapeCase):
    """A CLOSED issue never self-approves, and says so first (#1206)."""
    status, reason = EXPECTED_OPEN_DECISION[case.key]
    if case.state == "OPEN":
        return status, reason
    closed = "the issue is CLOSED on GitHub"
    if status == "Ready":
        return "Shaped", closed
    return "Shaped", "{}; {}".format(closed, reason)


def expected_inputs(case: ShapeCase, *, with_answer: bool):
    row = EXPECTED_INPUTS[case.key]
    return shape.ShapeInputs(
        origin_voice=row["origin_voice"],
        override_target=row["override_target"],
        class_adopted=row["class_adopted"],
        klass=row["klass"] if with_answer else row["klass_before"],
        output_review=row["review"] if with_answer else row["review_before"],
        state=case.state,
    )


def test_the_matrix_covers_every_origin_class_and_state():
    assert len(MATRIX) == len(ORIGINS) * len(CLASSES) * len(STATES) == 18
    assert set(EXPECTED_INPUTS) == set(EXPECTED_OPEN_DECISION) == {
        case.key for case in MATRIX}
    for case in MATRIX:
        item = matrix_item(case)
        override = funnel.parse_origin_override(item.body)
        assert (override["target"] if override else None) == \
            EXPECTED_INPUTS[case.key]["override_target"]


# -- reproduction -----------------------------------------------------------

def test_reproduction_packet_and_review_agree_on_an_unclassed_agent_idea():
    """On ef9af026d the packet omitted the guidance the review enforced."""
    item = idea(42, klass=None, origin="agent")

    packet = shape.collect(REPO, 42, items_loader=lambda: [item], now=NOW)
    reviewed, rejected = shape.review_shape_output_for_item(
        [item], item, generic_permission_answer())

    assert reviewed["needs_nate"]["scope"] is None
    assert any("generic Scope permission" in signal for signal in rejected)
    assert packet.get("output_review") == \
        shape.AGENT_SELF_APPROVABLE_OUTPUT_REVIEW


# -- every caller matches the reader over the matrix ------------------------

@pytest.mark.parametrize("case", MATRIX, ids=lambda case: "-".join(
    str(part) for part in (case.origin, case.klass, case.state)))
def test_the_reader_reads_each_case(case):
    item = matrix_item(case)

    assert shape.shape_inputs([item], item) == \
        expected_inputs(case, with_answer=False)
    assert shape.shape_inputs([item], item, generic_permission_answer()) == \
        expected_inputs(case, with_answer=True)


@pytest.mark.parametrize("case", MATRIX, ids=lambda case: "-".join(
    str(part) for part in (case.origin, case.klass, case.state)))
def test_the_packet_carries_the_review_exactly_when_the_reader_says(case):
    item = matrix_item(case)
    inputs = shape.shape_inputs([item], item)

    packet = shape.collect(REPO, 42, items_loader=lambda: [item], now=NOW)

    assert ("output_review" in packet) is inputs.output_review
    assert packet["origin"] == {"voice": inputs.origin_voice,
                                "override_target": inputs.override_target}


@pytest.mark.parametrize("case", MATRIX, ids=lambda case: "-".join(
    str(part) for part in (case.origin, case.klass, case.state)))
def test_the_review_judges_with_the_readers_class(case, monkeypatch):
    item = matrix_item(case)
    candidate = generic_permission_answer()
    inputs = shape.shape_inputs([item], item, candidate)
    seen = []
    real_review = shape.review_agent_shape_output

    def spy(answer_fields, **kwargs):
        seen.append(kwargs)
        return real_review(answer_fields, **kwargs)

    monkeypatch.setattr(shape, "review_agent_shape_output", spy)

    reviewed, _ = shape.review_shape_output_for_item([item], item, candidate)

    assert [(call["klass"], call["origin_voice"]) for call in seen] == [
        (inputs.klass, inputs.origin_voice)]
    assert (reviewed["needs_nate"]["scope"] is None) is inputs.output_review


@pytest.mark.parametrize("case", MATRIX, ids=lambda case: "-".join(
    str(part) for part in (case.origin, case.klass, case.state)))
def test_preview_decides_from_the_reader(case):
    item = matrix_item(case)
    reviewed, _ = shape.review_shape_output_for_item(
        [item], item, generic_permission_answer())
    inputs = shape.shape_inputs([item], item, reviewed)

    found = shape.preview_decision([item], item, reviewed)

    assert found == expected_decision(case)
    assert found == shape.decide(
        reviewed, klass=inputs.klass, origin_voice=inputs.origin_voice,
        override_target=inputs.override_target, state=inputs.state)


@pytest.mark.parametrize("case", MATRIX, ids=lambda case: "-".join(
    str(part) for part in (case.origin, case.klass, case.state)))
def test_apply_writes_the_class_the_reader_adopts(case, monkeypatch, capsys):
    item = matrix_item(case)
    inputs = shape.shape_inputs([item], item, generic_permission_answer())
    calls = stub_gh(monkeypatch, item)

    shape.apply_shape([item], NOW, item.ref, answer(
        proposed_class="Broken",
        needs_nate={"exposure": None, "gates": None,
                    "scope": ["Should we fix this?"], "preference": None}),
        run="shape-run", agent="muse")

    class_writes = [
        call[2]["option"] for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
        and call[2].get("field") == funnel.CLASS_FIELD_ID
    ]
    body_written = bool(gh_calls(calls, "gh", "issue", "edit"))
    if case.state == "OPEN":
        assert body_written
    if body_written and inputs.class_adopted:
        assert class_writes == ["opt-{}".format(inputs.klass)]
    else:
        assert class_writes == []
    if case.state == "OPEN":
        status, reason = expected_decision(case)
        verb = "advanced to Ready" if status == "Ready" else "held at Shaped"
        assert "{}: {}".format(verb, reason) in capsys.readouterr().out
