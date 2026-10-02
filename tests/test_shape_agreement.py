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

import io
import json
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
    stub_project_ref_load,
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
    # A CLOSED idea may be refused before any write (#2139); it then gets
    # no Class write either.
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


# -- one decision record per answer (#2137) ---------------------------------
#
# Preview, ``apply_main --validate-only`` and the live ``apply_shape`` each
# decided, scanned and derived the Risk write themselves until #2137, and
# preview and the Risk write drifted apart twice (#1538 in #1644, #1679 in
# #1721). ``shape.shape_decision`` now validates, reviews and records one
# answer, both paths run it once, and every write reads the record.

GENERIC_SCOPE = {"exposure": None, "gates": None,
                 "scope": ["Should we fix this?"], "preference": None}
GATES_QUESTION = {"exposure": None, "gates": ["Who may write Ready?"],
                  "scope": None, "preference": None}

#: Raw model answers. ``generic-permission`` is the matrix's answer; the
#: others are the cases #2137 adds, each differing from ``clean`` in one way.
ANSWERS = {
    "generic-permission": lambda: answer(
        proposed_class="Broken", needs_nate=GENERIC_SCOPE),
    "clean": lambda: answer(proposed_class="Broken"),
    "typed-risk": lambda: answer(
        proposed_class="Broken",
        escalated_risk=[{"reason": "data-migration",
                         "why": "backfills the ledger table"}]),
    "scan-only": lambda: answer(
        proposed_class="Broken",
        plan_markdown=("# Plan\n\n"
                       "We will rotate the deploy api-key monthly.\n")),
    "prose-rationale": lambda: answer(
        proposed_class="Broken",
        plan_markdown=("# Plan\n\nDisplay source freshness.\n\n"
                       "## Risk rationale\n\n"
                       "- destructive: drops the retired table\n")),
    "gates-question": lambda: answer(
        proposed_class="Broken", needs_nate=GATES_QUESTION),
    "generic-permission-new": lambda: answer(
        proposed_class="New", needs_nate=GENERIC_SCOPE),
}


@dataclass(frozen=True)
class DecisionCase:
    """A matrix idea, maybe under a parent with a Class, and an answer."""

    case: ShapeCase
    answer: str
    parent: Optional[str] = None

    @property
    def id(self):
        parts = [self.case.origin, str(self.case.klass), self.case.state,
                 self.answer]
        if self.parent is not None:
            parts.append("under-" + self.parent)
        return "-".join(parts)


AGENT_BROKEN_OPEN = ShapeCase("agent", "Broken", "OPEN")
AGENT_BROKEN_CLOSED = ShapeCase("agent", "Broken", "CLOSED")
_AGENT_READY = ("needs_nate all null; class Broken self-approvable; "
                "origin agent")
_CLOSED = "the issue is CLOSED on GitHub"

#: (status, reason, Risk, Needs, declared, scan) for every case beyond the
#: matrix's generic-permission rows, written out from the ticket. The parent
#: rows and the unclassed agent idea proposing New answer the #2136 review.
EXPECTED_RECORDS = {
    DecisionCase(AGENT_BROKEN_OPEN, "typed-risk"): (
        "Shaped", "escalated risk (data-migration)",
        "escalated", "human", ["data-migration"], []),
    DecisionCase(AGENT_BROKEN_CLOSED, "typed-risk"): (
        "Shaped", _CLOSED + "; escalated risk (data-migration)",
        "escalated", "human", ["data-migration"], []),
    DecisionCase(AGENT_BROKEN_OPEN, "scan-only"): (
        "Ready", _AGENT_READY
        + "; scan-only escalation (credentials) raises the review tier",
        "escalated", "none", [], ["credentials"]),
    DecisionCase(AGENT_BROKEN_CLOSED, "scan-only"): (
        "Shaped", _CLOSED, "escalated", "human", [], ["credentials"]),
    DecisionCase(AGENT_BROKEN_OPEN, "prose-rationale"): (
        "Shaped", "escalated risk (destructive)",
        "escalated", "human", ["destructive"], []),
    DecisionCase(AGENT_BROKEN_CLOSED, "prose-rationale"): (
        "Shaped", _CLOSED + "; escalated risk (destructive)",
        "escalated", "human", ["destructive"], []),
    DecisionCase(AGENT_BROKEN_OPEN, "gates-question"): (
        "Shaped", "open question under Gates", "standard", "human", [], []),
    DecisionCase(AGENT_BROKEN_CLOSED, "gates-question"): (
        "Shaped", _CLOSED + "; open question under Gates",
        "standard", "human", [], []),
    # The parent's Class is the idea's Class, whatever the idea carries.
    DecisionCase(ShapeCase("agent", "New", "OPEN"), "generic-permission",
                 parent="Broken"): (
        "Ready", _AGENT_READY, "standard", "none", [], []),
    DecisionCase(ShapeCase("agent", "Broken", "OPEN"), "generic-permission",
                 parent="New"): (
        "Shaped", "class New is not self-approvable; " + _SCOPE_OPEN,
        "standard", "human", [], []),
    DecisionCase(ShapeCase("nate-override-to-agents", None, "OPEN"), "clean",
                 parent="Broken"): (
        "Ready", "needs_nate all null; class Broken self-approvable; "
        "origin override to agents", "standard", "none", [], []),
    # The adopted Class is not self-approvable, so the output review does not
    # judge the answer and its generic question stays open.
    DecisionCase(ShapeCase("agent", None, "OPEN"),
                 "generic-permission-new"): (
        "Shaped", "class New is not self-approvable; " + _SCOPE_OPEN,
        "standard", "human", [], []),
}

DECISION_CASES = ([DecisionCase(case, "generic-permission") for case in MATRIX]
                  + list(EXPECTED_RECORDS))

#: Needs is human exactly when the status is Shaped (#2137).
_NEEDS_FOR = {"Ready": "none", "Shaped": "human"}


def expected_record(case: DecisionCase):
    """The matrix rows decide as #2136's table says, with nothing at risk."""
    if case in EXPECTED_RECORDS:
        return EXPECTED_RECORDS[case]
    status, reason = expected_decision(case.case)
    return status, reason, "standard", _NEEDS_FOR[status], [], []


PARENT_NUMBER = 7


def decision_items(case: DecisionCase):
    """The idea and the Project rows shaping reads, its parent included."""
    item = matrix_item(case.case)
    if case.parent is None:
        return item, [item]
    parent = idea(PARENT_NUMBER, klass=case.parent, status="Building",
                  origin="Nate", labels=[])
    item.parent = parent.ref
    return item, [item, parent]


def run_shape_apply(monkeypatch, capsys, data, *flags):
    """Run the shape-apply CLI on idea 42 with ``data`` on stdin."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(data)))
    capsys.readouterr()
    code = shape.apply_main(["42", "--repo", REPO, "--answer", "-", *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _bodies(calls, *prefix):
    return [args[args.index("--body") + 1]
            for _, args in gh_calls(calls, *prefix) if "--body" in args]


@pytest.mark.parametrize("case", DECISION_CASES, ids=lambda case: case.id)
def test_the_record_holds_the_decision(case):
    item, items = decision_items(case)
    status, reason, risk, needs, declared, scan = expected_record(case)

    record = shape.shape_decision(items, item, ANSWERS[case.answer]())

    assert (record.status, record.reason) == (status, reason)
    assert (record.risk, record.needs) == (risk, needs)
    assert record.declared == declared
    assert [entry["reason"] for entry in record.matches] == scan
    # The ticket's two rules, read off the record itself.
    assert (record.risk == "escalated") is bool(
        record.declared or record.matches)
    assert (record.needs == "human") is (record.status == "Shaped")
    assert shape.preview_decision(items, item, record.answer) == (
        status, reason)


@pytest.mark.parametrize("case", DECISION_CASES, ids=lambda case: case.id)
def test_validate_only_prints_what_the_live_apply_writes(
        case, monkeypatch, capsys):
    item, items = decision_items(case)
    stub_project_ref_load(monkeypatch, *items)
    status, reason, risk, needs, declared, scan = expected_record(case)
    data = ANSWERS[case.answer]()

    def offline(*args, **kwargs):
        raise AssertionError("validate-only must not reach GitHub")

    monkeypatch.setattr(funnel, "gh_graphql", offline)
    monkeypatch.setattr(funnel.subprocess, "run", offline)
    code, out, _ = run_shape_apply(
        monkeypatch, capsys, data, "--validate-only")
    assert code == 0
    previewed = json.loads(out)
    assert (previewed["status"], previewed["reason"]) == (status, reason)

    calls = stub_gh(monkeypatch, item)
    field_writes = []
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: field_writes.append((field, value)))
    code, out, err = run_shape_apply(
        monkeypatch, capsys, data, "--run", "shape-run", "--agent", "muse")

    body_writes = _bodies(calls, "gh", "issue", "edit")
    comments = _bodies(calls, "gh", "issue", "comment")
    approvals = [comment for comment in comments
                 if comment.startswith(funnel.SELF_APPROVED_PREFIX)]
    if not body_writes:
        # #2139 refuses an idea the fresh read shows CLOSED before any write,
        # so nothing is written that could disagree with the preview.
        assert case.case.state == "CLOSED"
        assert (code, field_writes, comments) == (0, [], [])
        return

    (body,) = body_writes
    assert funnel.parse_shape_risk_record(body) == {
        "declared": declared, "scan": scan}
    assert field_writes == [("Risk", risk), ("Needs", needs)]
    class_writes = [
        call[2]["option"] for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
        and call[2].get("field") == funnel.CLASS_FIELD_ID]
    if case.case.origin == "agent" and case.case.klass is None:
        assert class_writes == [
            "opt-{}".format(data["proposed_class"])]
    else:
        assert class_writes == []
    if case.case.state == "CLOSED":
        # The closed-issue guard refuses the Status write (#1206); what was
        # asked for is the record's status, and nothing claims approval.
        assert code == 1
        assert item.status == "Ideas"
        assert "could not confirm requested Status {} for {}".format(
            status, item.ref) in err
        assert approvals == []
        return

    assert code == 0
    assert item.status == status
    verb = "advanced to Ready" if status == "Ready" else "held at Shaped"
    assert "{} → {}".format(item.ref, status) in out
    assert "{}: {}".format(verb, reason) in out
    if status == "Ready":
        (approval,) = approvals
        assert approval.startswith(
            funnel.SELF_APPROVED_PREFIX + reason + "; no ")
    else:
        assert approvals == []
    scan_comments = [comment for comment in comments
                     if comment.startswith("**Escalation scan")]
    assert len(scan_comments) == (1 if scan and not declared else 0)


def test_both_paths_validate_review_and_record_once(monkeypatch, capsys):
    """Each path runs the shared steps once and the wording scan once."""
    item = matrix_item(AGENT_BROKEN_OPEN)
    stub_project_ref_load(monkeypatch, item)
    data = ANSWERS["scan-only"]()
    steps = []
    real_steps = getattr(shape, "shape_decision", None)

    def spy_steps(items, target, answer_data):
        steps.append(target.ref)
        return real_steps(items, target, answer_data)

    monkeypatch.setattr(shape, "shape_decision", spy_steps, raising=False)
    scans = []
    real_scan = funnel.plan_escalation_matches

    def spy_scan(body):
        scans.append(body)
        return real_scan(body)

    monkeypatch.setattr(funnel, "plan_escalation_matches", spy_scan)

    assert run_shape_apply(
        monkeypatch, capsys, data, "--validate-only")[0] == 0
    preview = (len(scans), list(steps))
    scans.clear()
    steps.clear()
    stub_gh(monkeypatch, item)
    assert run_shape_apply(
        monkeypatch, capsys, data, "--run", "shape-run", "--agent", "muse"
    )[0] == 0

    assert (preview[0], len(scans)) == (1, 1)
    assert (preview[1], steps) == ([item.ref], [item.ref])
