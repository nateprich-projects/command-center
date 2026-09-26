#!/usr/bin/env python3
"""Shape one idea from a structured answer (#810, Phase 2 of #794).

The shape runner shows the model a packet and nothing else: the idea, its
origin, plan.md, AGENTS.md, and the sibling plans in the same repo. The
model returns one structured answer; this module validates that answer
against the plan schema, renders the issue body from the fields, and
applies the self-approval rule mechanically.

The decision never parses a Needs section: ``needs_nate``'s four fields
are the open-question record, and ``decide`` feeds them to the shared
``self_approval_eligible`` predicate. The rendered body includes only open
questions; the Project field is the durable routing record.

Two entry points share this module: ``shape-packet`` is read-only, while
``shape-apply`` performs the shaping writes.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402


class ShapeError(Exception):
    """A shape answer failed validation, or shaping cannot proceed."""


#: Malformed answer on a runner-protocol attempt below the final one: the
#: runner feeds the parse error back to the model and calls again.
RETRY_EXIT = 3

#: Attempts past the first are final: a malformed answer then exits 1
#: with nothing recorded, and the runner finishes the run errored. The
#: idea keeps its needs-shaping label, so the next run asks again.
FINAL_ATTEMPT = 2


def validation_exit(attempt: Optional[int]) -> int:
    """The exit for a malformed answer, recording nothing either way.

    Without ``--attempt`` the single-shot legacy exit 1 stands. With an
    explicit attempt the runner protocol applies: retryable 3 below the
    final attempt, 1 at it.
    """
    if attempt is None:
        return 1
    return RETRY_EXIT if attempt < FINAL_ATTEMPT else 1


#: The answer keys shape-apply accepts — exactly these, no extras. From
#: the Shape row of #794, plus the model-declared escalated-risk list
#: (#1034) that the decision unions with the wording scan, plus the
#: sequencing-dependency list (#1053) that sequencing questions become
#: instead of Needs Nate entries, plus the evidence-backed premises list
#: recorded in the plan body (#1422).
ANSWER_KEYS = frozenset({
    "decided_from_precedent",
    "decided_by_agent",
    "needs_nate",
    "proposed_class",
    "plan_markdown",
    "escalated_risk",
    "depends_on",
    "premises",
})

#: The confidence vocabulary shared with LEARNINGS.md (#1422).
PREMISE_LABELS = ("measured", "documented", "inferred")

#: A sequencing dependency: owner/repo#n, the only shape accepted
#: (#1053). The packet's sibling plans carry full refs, so the model
#: copies them verbatim; a bare #n is a confused-model shape and fails.
REF_RE = re.compile(
    r"\A(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"#(?P<number>[1-9][0-9]*)\Z"
)

#: The needs_nate fields, each mapped to the Needs-section category it
#: renders as. The category spellings are the stable headings the skill
#: and the old parser agree on.
NEEDS_FIELDS = (
    ("exposure", "Exposure"),
    ("gates", "Gates"),
    ("scope", "Scope and priority"),
    ("preference", "Preference"),
)

#: The runner's review policy for agent-origin self-approvable plans.
#: It rides in the packet so the shaping model sees the rule at the point
#: where it chooses signals; ``review_agent_shape_output`` enforces the
#: false-hold cases before they are recorded.
AGENT_SELF_APPROVABLE_OUTPUT_REVIEW = {
    "scope": (
        "For agent-origin Investigate, Broken, Maintenance, and Improve "
        "work, ask Scope and priority only for a concrete unresolved "
        "stakeholder tradeoff. Do not ask generic permission to implement "
        "the work."),
    "scheduling": (
        "Timing, priority, and sequencing are project-manager decisions, "
        "not Needs Nate questions. Record the agent-owned ordering "
        "decision. Turn a clear wait-for named ticket into a depends_on "
        "reference; leave other named tickets as context when the recorded "
        "decision says there is no dependency."),
    "escalated_risk": (
        "Declare risk only from actions the proposed plan actually takes. "
        "A hypothetical implementation bug or its possible consequences "
        "are test concerns, not plan risks."),
    "preserve": (
        "Keep genuine Exposure and Gates questions, concrete Scope "
        "tradeoffs, and risks from proposed escalated actions."),
}

_GENERIC_FIX_PERMISSION_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE)
                                        for pattern in (
    r"\Ashould\s+(?:we|i)\s+(?:build|fix|repair|implement|ship|work\s+on)"
    r"\s+(?:this|it|the\s+(?:fix|repair|work|project|idea))"
    r"\s*[?.!]*\Z",
    r"\Ashould\s+(?:this|the)\s+"
    r"(?:(?:agent[- ]origin|broken)\s+){0,2}"
    r"(?:fix|repair|issue|idea|project|plan)\s+be\s+"
    r"(?:built|fixed|repaired|implemented|shipped)\s*[?.!]*\Z",
))

_HYPOTHETICAL_IMPLEMENTATION_RISK = re.compile(
    r"\b(?:hypothetical(?:ly)?\s+(?:implementation[- ]?)?bug|"
    r"(?:a|the|any)\s+bug\s+in\s+(?:the\s+)?(?:implementation|code|"
    r"fix|repair|path)|buggy\s+implementation|misimplement\w*|"
    r"implementation[- ](?:bug|error|failure|defect)|"
    r"arbitrary\s+implementation[- ]bug)\b",
    re.IGNORECASE,
)

_CONCRETE_SCOPE_PATTERN = re.compile(
    r"\b(?:include|exclude|cover|support|handle|add|remove|preserve|"
    r"change|retain|drop|feature|capability|functionality|customer|"
    r"user|behavior|behaviour|coverage|format|output|data|report)\b",
    re.IGNORECASE)

_SCHEDULING_QUESTION_PATTERN = re.compile(
    r"\b(?:timing|schedule|scheduled|now|later|today|tomorrow|this week|"
    r"next week|early|late|first|next|priority|priorit(?:y|ize|ise)|rank|"
    r"before|after|wait|delay|defer|land|start|ship|proceed|hold|in flight|"
    r"sequenc(?:e|ing))\b", re.IGNORECASE)

_SCHEDULING_WORK_OBJECT_PATTERN = re.compile(
    r"\b(?:this|it|the work|the plan|the ticket|the issue|the project|"
    r"the task|the repair|the fix|the change|the patch|these tasks|"
    r"those tasks)\b", re.IGNORECASE)

_PRIORITY_QUESTION_PATTERN = re.compile(
    r"\b(?:priority|priorit(?:y|ize|ise)|rank)\b", re.IGNORECASE)

_ORDER_CHOICE_PATTERN = re.compile(
    r"\b(?:which|what)\b.{0,50}\b(?:first|next|before|after)\b",
    re.IGNORECASE)

# Only unambiguous waits become a dependency on the named ticket. A question
# such as the #258 "land now while #165 and #174 are in flight, or wait?"
# is a timing decision; its recorded "land now, no dependency" decision must
# not turn contextual ticket references into blockers.
_TICKET_TARGET_PATTERN = (
    r"(?:(?:https?://)?github\.com/[A-Za-z0-9_.-]+/"
    r"[A-Za-z0-9_.-]+/issues/[1-9][0-9]*|"
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[1-9][0-9]*)")
_WAIT_FOR_TICKET_PATTERN = re.compile(
    r"\b(?:wait(?:ing)?\s+(?:for|until)|depend(?:s|ing)?\s+on|"
    r"blocked\s+by|(?:not|only)\s+until)\s+"
    r"(?:the\s+(?:completion|close|closure)\s+of\s+)?"
    + _TICKET_TARGET_PATTERN
    + r"|\b(?:after|once|when)\s+(?:the\s+)?"
    + _TICKET_TARGET_PATTERN,
    re.IGNORECASE)

_TICKET_REFERENCE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+))?#(?P<number>[1-9][0-9]*)\b")
_TICKET_URL_PATTERN = re.compile(
    r"github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"/issues/(?P<number>[1-9][0-9]*)", re.IGNORECASE)


def _require_text(value: object, where: str) -> str:
    """Return stripped text, or raise when it is missing or blank."""
    if not isinstance(value, str) or not value.strip():
        raise ShapeError("{} must be a non-empty string".format(where))
    return value.strip()


def _require_line(value: object, where: str) -> str:
    """Return one line of text, or raise when it is missing or blank.

    Decision fields render as Markdown list items, so inner newlines are
    collapsed: a multi-line claim would otherwise break the list form.
    """
    return re.sub(r"\s+", " ", _require_text(value, where))


def _validate_investigate_possible_defect(plan_markdown: str) -> None:
    """Require one explicit possible-defect statement for Investigate.

    Only a whole line in the plan narrative counts. A malformed line that
    starts with the marker also fails closed, even if another valid line is
    present, so the answer cannot carry conflicting defect statements.
    """
    matching_lines = []
    malformed_line = False
    for raw_line in plan_markdown.splitlines():
        line = raw_line.strip()
        if not line.startswith("Possible defect"):
            continue
        match = re.fullmatch(r"Possible defect:[ ]+(.+)", line)
        if not match or not match.group(1).strip():
            malformed_line = True
            continue
        if line.count("Possible defect:") != 1:
            malformed_line = True
            continue
        matching_lines.append(line)

    if malformed_line or len(matching_lines) != 1:
        raise ShapeError(
            "proposed_class Investigate requires exactly one non-empty "
            "whole line of the form 'Possible defect: <statement>' "
            "in plan_markdown")


def _check_keys(entry: object, keys: Sequence[str], where: str) -> None:
    """Reject a mapping that is missing keys or carries unknown ones."""
    if not isinstance(entry, dict):
        raise ShapeError("{} must be an object".format(where))
    missing = sorted(set(keys) - set(entry))
    extra = sorted(set(entry) - set(keys))
    if missing:
        raise ShapeError("{} is missing {}".format(
            where, ", ".join(missing)))
    if extra:
        raise ShapeError("{} has unknown fields: {}".format(
            where, ", ".join(extra)))


def _validate_precedent(entries: object) -> List[Dict[str, str]]:
    """Validate the decided-from-precedent list. It may be empty: a
    genuinely novel idea cites nothing, and an empty list says so
    honestly rather than inventing a source."""
    if not isinstance(entries, list):
        raise ShapeError("decided_from_precedent must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "decided_from_precedent[{}]".format(index)
        _check_keys(entry, ("claim", "source"), where)
        validated.append({
            "claim": _require_line(entry["claim"], where + ".claim"),
            "source": _require_line(entry["source"], where + ".source"),
        })
    return validated


def _validate_agent_decisions(entries: object) -> List[Dict[str, str]]:
    """Validate the decided-by-agent list. Each entry keeps its
    reasoning and its rejected alternative, in the same shape as the
    plan's Rejected section."""
    if not isinstance(entries, list):
        raise ShapeError("decided_by_agent must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "decided_by_agent[{}]".format(index)
        _check_keys(entry, ("decision", "alternative", "why"), where)
        validated.append({
            "decision": _require_line(
                entry["decision"], where + ".decision"),
            "alternative": _require_line(
                entry["alternative"], where + ".alternative"),
            "why": _require_line(entry["why"], where + ".why"),
        })
    return validated


def _validate_needs_nate(needs: object) -> Dict[str, Optional[List[str]]]:
    """Validate the open-question record: the four fields, each null or
    a non-empty list of single questions (#1053).

    Atomic questions per category: a compound splits before it is asked,
    so a settled half cannot drag its genuine half to Nate. A bare
    string is the old single-question shape and fails — the runner
    retries once with the error fed back, and the model answers again
    with a list.
    """
    fields = [field for field, _ in NEEDS_FIELDS]
    _check_keys(needs, fields, "needs_nate")
    assert isinstance(needs, dict)
    validated: Dict[str, Optional[List[str]]] = {}
    for field, _ in NEEDS_FIELDS:
        value = needs[field]
        if value is None:
            validated[field] = None
        elif not isinstance(value, list) or not value:
            raise ShapeError(
                "needs_nate.{} must be null or a non-empty list of "
                "questions".format(field))
        else:
            questions = []
            for index, entry in enumerate(value):
                where = "needs_nate.{}.{}".format(field, index)
                questions.append(_require_line(entry, where))
            validated[field] = questions
    return validated


def _validate_depends_on(entries: object) -> List[str]:
    """Validate the sequencing-dependency list (#1053).

    Sequencing is never a Needs Nate question: waiting on a named
    sibling, priority against named tickets, or a separate pin or
    activation is recorded here as owner/repo#n refs, and the apply
    step writes the native blocked-by edges. Empty when the plan waits
    on nothing — an empty list says so honestly.
    """
    if not isinstance(entries, list):
        raise ShapeError("depends_on must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "depends_on[{}]".format(index)
        if not isinstance(entry, str) or not REF_RE.match(entry.strip()):
            raise ShapeError(
                "{} must be an owner/repo#n ref".format(where))
        validated.append(entry.strip())
    return validated


def _validate_premises(entries: object) -> List[Dict[str, str]]:
    """Validate the factual premises recorded with a shaped plan (#1422).

    Each premise carries one claim, its evidence pointer, and an honest
    confidence label. Evidence is kept as a compact line in the plan body;
    its supported forms are described in skills/shape.
    """
    if not isinstance(entries, list):
        raise ShapeError("premises must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "premises[{}]".format(index)
        _check_keys(entry, ("claim", "evidence", "label"), where)
        claim = _require_line(entry["claim"], where + ".claim")
        evidence = _require_line(entry["evidence"], where + ".evidence")
        label = _require_line(entry["label"], where + ".label")
        if label not in PREMISE_LABELS:
            raise ShapeError(
                "{}.label must be one of {}".format(
                    where, ", ".join(PREMISE_LABELS)))
        validated.append({
            "claim": claim,
            "evidence": evidence,
            "label": label,
        })
    return validated


def _validate_escalated_risk(entries: object) -> List[Dict[str, str]]:
    """Validate the model-declared escalated-risk list (#1034).

    A list of ``{"reason", "why"}`` objects, possibly empty. Each reason
    names one of ``funnel.ESCALATION_PATTERNS`` exactly; each why is one
    non-empty line saying what in the plan carries that risk. The model
    judges the plan itself here, not its wording: the decision holds on
    this list or the wording scan, whichever fires, so the declaration
    can only hold, never release.
    """
    if not isinstance(entries, list):
        raise ShapeError("escalated_risk must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "escalated_risk[{}]".format(index)
        _check_keys(entry, ("reason", "why"), where)
        reason = _require_line(entry["reason"], where + ".reason")
        if reason not in funnel.ESCALATION_PATTERNS:
            raise ShapeError(
                "{} is not an escalation reason; choose one of "
                "{}".format(where + ".reason",
                             ", ".join(sorted(
                                 funnel.ESCALATION_PATTERNS))))
        validated.append({
            "reason": reason,
            "why": _require_line(entry["why"], where + ".why"),
        })
    return validated


def validate_answer(data: object) -> Dict:
    """Validate a shape answer against the plan schema.

    Returns a normalized copy: surrounding whitespace stripped, inner
    newlines in one-line fields collapsed. Anything malformed raises
    ``ShapeError`` before any write, so a confused model cannot leave a
    half-shaped idea behind.
    """
    _check_keys(data, sorted(ANSWER_KEYS), "the answer")
    assert isinstance(data, dict)
    proposed = _require_line(data["proposed_class"], "proposed_class")
    if proposed not in funnel.LADDER:
        raise ShapeError(
            "proposed_class {!r} is not a ladder class; choose one of "
            "{}".format(proposed, ", ".join(funnel.LADDER)))
    decided_from_precedent = _validate_precedent(
        data["decided_from_precedent"])
    decided_by_agent = _validate_agent_decisions(
        data["decided_by_agent"])
    needs_nate = _validate_needs_nate(data["needs_nate"])
    plan_markdown = _require_text(data["plan_markdown"], "plan_markdown")
    if proposed == "Investigate":
        _validate_investigate_possible_defect(plan_markdown)
    return {
        "decided_from_precedent": decided_from_precedent,
        "decided_by_agent": decided_by_agent,
        "needs_nate": needs_nate,
        "proposed_class": proposed,
        "plan_markdown": plan_markdown,
        "escalated_risk": _validate_escalated_risk(
            data["escalated_risk"]),
        "depends_on": _validate_depends_on(data["depends_on"]),
        "premises": _validate_premises(data["premises"]),
    }


def render_plan(answer: Dict) -> str:
    """Render the issue body from validated answer fields.

    The plan narrative is followed by its evidence-backed premises and
    proposed class, risk rationale when the model declared a risk, then the
    runner-owned decision record: what precedent settled, what the
    agent decided itself, the sequencing dependencies where any wait
    (#1053), and only the Needs Nate categories with open questions.
    Canonical Risk and Needs values live in Project fields. Takes a
    validated answer; ``apply_shape`` validates before calling.
    """
    lines = [answer["plan_markdown"].rstrip(), "",
             "## Premises", ""]
    premises = answer["premises"]
    if premises:
        for entry in premises:
            lines.append("- {} (label: {}; evidence: {})".format(
                entry["claim"], entry["label"], entry["evidence"]))
    else:
        lines.append("None recorded.")
    lines.extend(["", "Proposed class: {}".format(
        answer["proposed_class"]), ""])
    if answer["escalated_risk"]:
        lines.extend(["## Risk rationale", ""])
        lines.extend(
            "- {}: {}".format(entry["reason"], entry["why"])
            for entry in answer["escalated_risk"]
        )
        lines.append("")
    lines.extend(["## Decided from precedent", ""])
    precedent = answer["decided_from_precedent"]
    if precedent:
        for entry in precedent:
            lines.append("- {} (source: {})".format(
                entry["claim"], entry["source"]))
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Decided by the agent", ""])
    by_agent = answer["decided_by_agent"]
    if by_agent:
        for entry in by_agent:
            lines.append("- {} (rejected: {}; {})".format(
                entry["decision"], entry["alternative"], entry["why"]))
    else:
        lines.append("None recorded.")
    depends = answer.get("depends_on", [])
    if depends:
        lines.extend(["", "## Sequencing", "",
                      "Depends on: {}".format(", ".join(depends))])
    needs = answer["needs_nate"]
    open_questions = [
        (category, needs[field]) for field, category in NEEDS_FIELDS
        if needs[field] is not None
    ]
    if open_questions:
        lines.extend(["", "## Needs Nate", ""])
        for category, questions in open_questions:
            lines.append("- {}: {}".format(category, "; ".join(questions)))
    lines.append("")
    return "\n".join(lines)


def open_need_categories(answer: Dict) -> List[str]:
    """The Needs Nate categories holding a question, in field order."""
    return [category for field, category in NEEDS_FIELDS
            if answer["needs_nate"][field] is not None]


def needs_nate_open(answer: Dict) -> bool:
    """Whether the answer's open-question record asks Nate anything.

    This boolean is what the old Needs-section parser used to supply.
    One non-null field holds the plan at Shaped.
    """
    return bool(open_need_categories(answer))


def _ticket_refs_in_question(question: str, repo: Optional[str]
                             ) -> Tuple[List[str], bool]:
    """Return canonical ticket refs in question order and whether a local
    ``#n`` could not be resolved to a repository.
    """
    found = []
    unresolved_local = False
    matches = []
    matches.extend((match.start(), match.end(), "url", match)
                   for match in _TICKET_URL_PATTERN.finditer(question))
    matches.extend((match.start(), match.end(), "ref", match)
                   for match in _TICKET_REFERENCE_PATTERN.finditer(question))
    for _, _, kind, match in sorted(matches, key=lambda entry: entry[0]):
        if kind == "url":
            ref = "{}/{}#{}".format(
                match.group("owner"), match.group("repo"),
                match.group("number"))
        elif match.group("owner"):
            ref = "{}/{}#{}".format(
                match.group("owner"), match.group("repo"),
                match.group("number"))
        elif repo:
            ref = "{}#{}".format(repo, match.group("number"))
        else:
            unresolved_local = True
            continue
        if ref not in found:
            found.append(ref)
    return found, unresolved_local


def _scope_question_kind(question: str, repo: Optional[str]
                         ) -> Tuple[Optional[str], List[str]]:
    """Classify only categories the validator can judge confidently.

    Ambiguous scope questions stay open. A generic permission, timing, or
    priority question is owned by the project manager; a clear wait-for
    question naming tickets is represented as a dependency instead.
    """
    if any(pattern.fullmatch(question)
           for pattern in _GENERIC_FIX_PERMISSION_PATTERNS):
        return "permission", []
    if _CONCRETE_SCOPE_PATTERN.search(question):
        return None, []
    refs, unresolved_local = _ticket_refs_in_question(question, repo)
    if _WAIT_FOR_TICKET_PATTERN.search(question):
        if refs and not unresolved_local:
            return "sequencing", refs
        if unresolved_local:
            return None, []
    if _SCHEDULING_QUESTION_PATTERN.search(question):
        if (_SCHEDULING_WORK_OBJECT_PATTERN.search(question)
                or (_PRIORITY_QUESTION_PATTERN.search(question) and refs)
                or _ORDER_CHOICE_PATTERN.search(question)
                or (len(refs) > 1 and re.search(
                    r"\b(?:first|next|before|after)\b", question,
                    re.IGNORECASE))):
            return "scheduling", []
    return None, []


def _has_recorded_ordering_decision(answer: Dict, refs: Sequence[str]
                                    ) -> bool:
    """Whether the rendered agent-decision list already records this call."""
    markers = re.compile(
        r"\b(?:timing|schedule|sequenc|priorit|order|land|wait|"
        r"dependenc|now|later)\w*\b", re.IGNORECASE)
    ref_numbers = [ref.rsplit("#", 1)[-1] for ref in refs]
    for entry in answer.get("decided_by_agent", []):
        text = " ".join(str(entry.get(key, ""))
                         for key in ("decision", "alternative", "why"))
        if refs:
            if any(ref in text or re.search(
                    r"(?<!\d)#{}(?!\d)".format(re.escape(number)), text)
                   for ref, number in zip(refs, ref_numbers)):
                return True
        elif markers.search(text):
            return True
    return False


def _record_ordering_decision(answer: Dict, refs: Sequence[str]) -> None:
    """Make the agent-owned scheduling choice explicit in the plan body."""
    if _has_recorded_ordering_decision(answer, refs):
        return
    if refs:
        names = ", ".join(refs)
        entry = {
            "decision": "Wait for {} before proceeding".format(names),
            "alternative": "Proceed concurrently with the named work",
            "why": ("A clear sequencing question is recorded as a "
                    "dependency; the agent owns this ordering decision."),
        }
    else:
        entry = {
            "decision": "Use the funnel's computed order; add no timing hold",
            "alternative": "Ask Nate to choose when the work runs",
            "why": ("Scheduling belongs to the project manager, and the "
                    "funnel computes work order."),
        }
    answer["decided_by_agent"].append(entry)


def review_agent_shape_output(
        answer: Dict, *, klass: Optional[str],
        origin_voice: Optional[str], repo: Optional[str] = None
        ) -> Tuple[Dict, List[str]]:
    """Review false holds for agent-origin self-approvable plans.

    Generic implementation permission and clearly categorized scheduling
    questions do not become stakeholder tradeoffs by being placed under
    Scope and priority. Clear waits on named tickets move to ``depends_on``;
    concrete scope questions and questions whose category is unclear remain
    open. Hypothetical implementation bugs are not risks in the proposed
    plan. The shared self-approval predicate remains the sole gate.
    """
    reviewed = copy.deepcopy(answer)
    rejected = []
    if (klass not in funnel.SELF_APPROVABLE_CLASSES
            or origin_voice != "agent"):
        return reviewed, rejected

    scope_questions = reviewed["needs_nate"]["scope"]
    kept = []
    removed_kinds = []
    added_dependencies = []
    sequencing_refs = []
    if scope_questions is not None:
        for question in scope_questions:
            kind, refs = _scope_question_kind(question, repo)
            if kind is None:
                kept.append(question)
                continue
            removed_kinds.append(kind)
            if kind == "sequencing":
                for ref in refs:
                    if ref not in sequencing_refs:
                        sequencing_refs.append(ref)
                    if (ref not in reviewed["depends_on"]
                            and ref not in added_dependencies):
                        added_dependencies.append(ref)
        if removed_kinds:
            reviewed["needs_nate"]["scope"] = kept or None

    if added_dependencies:
        reviewed["depends_on"].extend(added_dependencies)
        rejected.append(
            "agent-owned sequencing question recorded as depends_on: "
            + ", ".join(added_dependencies))
    scheduling_kinds = {"scheduling", "sequencing"}
    if any(kind in scheduling_kinds for kind in removed_kinds):
        _record_ordering_decision(reviewed, sequencing_refs)
        rejected.append(
            "agent-owned scheduling question (timing, priority, or "
            "sequencing)")
    if "permission" in removed_kinds:
        rejected.append(
            "generic Scope permission to implement agent-origin work")

    risks = reviewed["escalated_risk"]
    kept_risks = [entry for entry in risks
                  if not _HYPOTHETICAL_IMPLEMENTATION_RISK.search(
                      entry["why"])]
    if len(kept_risks) != len(risks):
        reviewed["escalated_risk"] = kept_risks
        rejected.append(
            "escalated risk based only on a hypothetical implementation bug")
    return reviewed, rejected


def decide(answer: Dict, *,
           klass: Optional[str],
           origin_voice: Optional[str],
           override_target: Optional[str] = None,
           escalation_reasons: Sequence[str] = (),
           escalation_matches: Sequence[Dict[str, Optional[str]]] = (),
           state: Optional[str] = None) -> Tuple[str, str]:
    """Apply the self-approval rule to validated fields. Pure: no IO.

    Returns the status and its reason: ``Ready`` only when the four
    needs_nate fields are all null, the class is self-approvable, the
    effective shaper is agents, and the plan carries no escalated risk.
    The rule itself is the shared ``self_approval_eligible`` predicate,
    so the packet path cannot drift from the shaping path; only the
    needs_nate input comes from the fields instead of the parser.

    Escalated risk is the union of the model's ``escalated_risk``
    declaration and the wording scan passed as ``escalation_reasons``
    (#1034): either one holding is enough, so a plan-worded risk the
    scan misses still holds, and a scan hit the model omitted still
    holds. Matching lines from ``escalation_matches`` are included in
    the explanation for scan hits. There is no standard override: an
    empty declaration never clears a scan hit.

    Sequencing dependencies never hold (#1053): a plan that waits on
    a named sibling but asks Nate nothing self-approves, and the
    dependency is recorded as a native edge, not a question.
    """
    declared = [entry["reason"] for entry in answer.get("escalated_risk", [])
                if isinstance(entry, dict)
                and isinstance(entry.get("reason"), str)]
    reasons = sorted(set(escalation_reasons or ()) | set(declared))
    matched_lines = {
        entry.get("reason"): entry.get("line")
        for entry in escalation_matches or ()
        if isinstance(entry, dict)
        and isinstance(entry.get("reason"), str)
        and isinstance(entry.get("line"), str)
        and entry.get("line").strip()
    }
    open_categories = open_need_categories(answer)
    if funnel.self_approval_eligible(
            klass, origin_voice, override_target,
            needs_nate=bool(open_categories),
            escalated=bool(reasons),
            state=state):
        if origin_voice == "agent":
            owner_basis = "origin agent"
        else:
            owner_basis = "origin override to agents"
        return ("Ready",
                "needs_nate all null; class {} self-approvable; "
                "{}".format(klass, owner_basis))
    failed = []
    if state is not None and str(state).upper() != "OPEN":
        failed.append("the issue is {} on GitHub".format(
            str(state).upper()))
    if klass not in funnel.SELF_APPROVABLE_CLASSES:
        failed.append("class {} is not self-approvable".format(
            klass or "unset"))
    if funnel.effective_shape_owner(
            origin_voice, override_target) != "agents":
        failed.append("origin is Nate's")
    failed.extend("open question under {}".format(category)
                  for category in open_categories)
    if reasons:
        described = [
            "{}: {}".format(reason, matched_lines[reason])
            if reason in matched_lines else reason
            for reason in reasons
        ]
        failed.append("escalated risk ({})".format(", ".join(described)))
    return "Shaped", "; ".join(failed)


def preview_decision(items: list, item, answer: Dict) -> Tuple[str, str]:
    """The status one validated answer would record, without writing.

    The same inputs the live path decides from — the effective class
    with the class-missing recovery, the origin voice and override, the
    model's escalated-risk declaration inside the answer, and the
    escalated-risk scan of the rendered plan — so ``--validate-only``
    reports the status the live path would write. Pure apart from its
    arguments; takes a validated answer.
    """
    original_body = item.body or ""
    origin_voice = item.origin
    override = funnel.parse_origin_override(original_body)
    override_target = override["target"] if override is not None else None
    by_ref = {candidate.ref: candidate for candidate in items}
    effective_klass = funnel.effective_class(item, by_ref)
    if item.klass not in funnel.LADDER and origin_voice == "agent":
        effective_klass = answer["proposed_class"]
    scan_answer = dict(answer)
    # `decide` consumes the typed declaration separately. Keep it out of the
    # wording scan here so the durable Risk line added by `render_plan` does
    # not report the same declaration twice.
    scan_answer["escalated_risk"] = []
    matches = funnel.plan_escalation_matches(render_plan(scan_answer))
    return decide(
        answer,
        klass=effective_klass,
        origin_voice=origin_voice,
        override_target=override_target,
        escalation_reasons=[entry["reason"] for entry in matches
                            if isinstance(entry.get("reason"), str)],
        escalation_matches=matches,
        state=item.state,
    )


def review_shape_output_for_item(items: list, item, answer: Dict
                                 ) -> Tuple[Dict, List[str]]:
    """Apply the output review using the same effective class as shaping."""
    origin_voice = item.origin
    by_ref = {candidate.ref: candidate for candidate in items}
    effective_klass = funnel.effective_class(item, by_ref)
    if item.klass not in funnel.LADDER and origin_voice == "agent":
        effective_klass = answer["proposed_class"]
    return review_agent_shape_output(
        answer, klass=effective_klass, origin_voice=origin_voice,
        repo=item.repo)


def report_output_review(rejected: Sequence[str]) -> None:
    """Make any runner-side signal corrections visible to the caller."""
    if rejected:
        print("shape-apply output review rejected: {}".format(
            "; ".join(rejected)), file=sys.stderr)


def issue_url(ref: str) -> str:
    """Render an owner/repo#n ref as the issue URL `gh` takes for edges."""
    match = REF_RE.match(ref.strip())
    if not match:
        raise ShapeError("not an owner/repo#n ref: {!r}".format(ref))
    return "https://github.com/{}/{}/issues/{}".format(
        match.group("owner"), match.group("repo"), match.group("number"))


def blocked_by_values(depends_on: Sequence[str], repo: str) -> List[str]:
    """Render sequencing deps as `gh --add-blocked-by` values (#1053).

    Same-repo refs become bare numbers; cross-repo ones become issue
    URLs, since a bare number would point at the wrong repository.
    Answer order is preserved. Takes validated refs; anything else
    fails closed rather than guessing an edge target.
    """
    values = []
    for ref in depends_on or []:
        match = REF_RE.match(ref.strip())
        if not match:
            raise ShapeError(
                "not an owner/repo#n ref: {!r}".format(ref))
        if "{}/{}".format(match.group("owner"),
                          match.group("repo")) == repo:
            values.append(match.group("number"))
        else:
            values.append(issue_url(ref))
    return values


BLOCKED_BY_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      blockedBy(first: 100) {
        totalCount
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}
"""


def read_blocked_by_refs(item) -> List[str]:
    """Re-read complete native blocker refs after a refused edge write."""
    owner, separator, name = item.repo.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise funnel.GitHubError(
            "cannot read blocked-by edges for {}: invalid repository ref".format(
                item.ref
            )
        )
    try:
        payload = funnel.gh_graphql(
            BLOCKED_BY_QUERY, owner=owner, name=name, number=item.number
        )
    except funnel.GitHubError as exc:
        raise funnel.GitHubError(
            "cannot read blocked-by edges for {}: {}".format(item.ref, exc)
        ) from exc
    repository = payload.get("repository") if isinstance(payload, dict) else None
    issue = repository.get("issue") if isinstance(repository, dict) else None
    connection = issue.get("blockedBy") if isinstance(issue, dict) else None
    refs = funnel._blocked_by_refs_from_connection(connection)
    if refs is None:
        raise funnel.GitHubError(
            "cannot read a complete blocked-by edge list for {}".format(
                item.ref
            )
        )
    return refs


def fetch_repo_text(repo: str, path: str) -> Tuple[str, bool]:
    """One text file at the repo's default branch, or ("", True).

    Missing is a fact about the repo, not a fetch failure: the packet
    stays valid and says so, because the shape question is still
    answerable without it.
    """
    proc = funnel._run_gh(
        ["gh", "api", "repos/{}/contents/{}".format(repo, path),
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        if "404" in (proc.stderr or ""):
            return "", True
        raise funnel.GitHubError(
            "could not read {} in {}: {}".format(
                path, repo, (proc.stderr or "").strip()))
    return proc.stdout or "", False


def sibling_plan_items(items: Sequence, idea) -> list:
    """The other open project plans in the idea's repo, sorted by ref.

    Open plans are parentless, open, Shaped/Ready/Building items —
    narrowed to the idea's own repo, which is what the shape packet
    promises.
    """
    return sorted(
        (row for row in items
         if row.ref != idea.ref
         and row.repo == idea.repo
         and row.parent is None
         and row.state == "OPEN"
         and row.status in funnel.SHAPING_PLAN_STATUSES),
        key=lambda row: row.ref)


def _idea_packet(item) -> Dict:
    """The idea's identity, captured note, labels, and Project fields."""
    return {
        "ref": item.ref,
        "number": item.number,
        "title": item.title,
        "url": item.url,
        "body": item.body,
        "labels": sorted(item.labels or []),
        "status": item.status,
        "klass": item.klass,
    }


def _sibling_packet(item) -> Dict:
    """One sibling plan: identity, stage, class, and the plan itself."""
    return {
        "ref": item.ref,
        "number": item.number,
        "title": item.title,
        "status": item.status,
        "klass": item.klass,
        "body": item.body,
    }


def issue_thread_section(comments: Sequence[Dict]) -> Optional[str]:
    """Render every issue comment chronologically, preserving each body."""
    if not isinstance(comments, (list, tuple)):
        raise funnel.GitHubError("could not read a complete issue thread")
    if not comments:
        return None
    ordered = []
    for index, row in enumerate(comments):
        if not isinstance(row, dict) or not isinstance(row.get("body"), str):
            raise funnel.GitHubError("could not read a complete issue thread")
        author = row.get("author")
        if isinstance(author, dict):
            author = author.get("login")
        if not isinstance(author, str) or not author.strip():
            author = "unknown author"
        created_at = row.get("createdAt") or row.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise funnel.GitHubError("could not read a complete issue thread")
        try:
            timestamp = datetime.fromisoformat(
                created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise funnel.GitHubError(
                "could not read a complete issue thread") from exc
        if timestamp.tzinfo is None:
            raise funnel.GitHubError("could not read a complete issue thread")
        ordered.append((timestamp, index, created_at, author.strip(), row["body"]))
    ordered.sort(key=lambda row: (row[0], row[1]))
    blocks = [
        "### @{} — {}\n\n{}".format(author, created_at, body)
        for _, _, created_at, author, body in ordered
    ]
    return "## Issue thread\n\n" + "\n\n".join(blocks)


def build_packet(*, repo: str, idea: Dict,
                 origin_voice: Optional[str],
                 override_target: Optional[str],
                 plan_md: str, plan_md_missing: bool,
                 agents_md: str, agents_md_missing: bool,
                 siblings: Sequence[Dict],
                 collected_at: str,
                 issue_comments: Optional[Sequence[Dict]] = None) -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO.

    Everything the shape question needs in one JSON-serialisable dict:
    the idea, its origin, the repo's plan.md and AGENTS.md, and the
    sibling plans the model cites as precedent.
    """
    packet = {
        "repo": repo,
        "idea": dict(idea),
        "origin": {
            "voice": origin_voice,
            "override_target": override_target,
        },
        "plan_md": plan_md,
        "plan_md_missing": plan_md_missing,
        "agents_md": agents_md,
        "agents_md_missing": agents_md_missing,
        "sibling_plans": [dict(row) for row in siblings],
        "collected_at": collected_at,
    }
    issue_thread = issue_thread_section(
        issue_comments if issue_comments is not None else [])
    if issue_thread is not None:
        packet["issue_thread"] = issue_thread
    if (origin_voice == "agent"
            and idea.get("klass") in funnel.SELF_APPROVABLE_CLASSES):
        packet["output_review"] = dict(
            AGENT_SELF_APPROVABLE_OUTPUT_REVIEW)
    return packet


def collect(repo: Optional[str], idea_number: int, *,
            items_loader: Optional[Callable[[], list]] = None,
            now: Optional[datetime] = None) -> Dict:
    """Fetch every piece and build the packet. Reads only, no writes."""
    resolved = funnel.resolve_repo(repo)
    if items_loader is None:
        # No packet field reads Project history (status_since, status
        # events, blocked times, child timestamps), so skip the per-item
        # detail batch: it is most of this load's time and points (#1620).
        items = funnel.load_items(
            include_details=False, shape_issue=(resolved, idea_number))
    else:
        items = items_loader()
    idea_item = funnel.find(items, "{}#{}".format(resolved, idea_number))
    issue_comments = getattr(idea_item, "issue_comments", None)
    if issue_comments is None and items_loader is not None:
        # Pure packet fixtures and injected readers can omit the optional
        # thread; production reads always request it with the Project query.
        issue_comments = []
    if not isinstance(issue_comments, list):
        raise funnel.GitHubError(
            "could not read comments for {}#{}".format(resolved, idea_number)
        )
    override = funnel.parse_origin_override(idea_item.body or "")
    plan_md, plan_md_missing = fetch_repo_text(resolved, "plan.md")
    agents_md, agents_md_missing = fetch_repo_text(resolved, "AGENTS.md")
    return build_packet(
        repo=resolved,
        idea=_idea_packet(idea_item),
        origin_voice=idea_item.origin,
        override_target=(override["target"]
                         if override is not None else None),
        plan_md=plan_md,
        plan_md_missing=plan_md_missing,
        agents_md=agents_md,
        agents_md_missing=agents_md_missing,
        siblings=[_sibling_packet(row)
                  for row in sibling_plan_items(items, idea_item)],
        collected_at=(now or datetime.now(timezone.utc)).isoformat(),
        issue_comments=issue_comments,
    )


def packet_main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the shape packet for one idea as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only shape packet for an idea")
    parser.add_argument("idea", type=int, help="idea issue number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    args = parser.parse_args(argv)
    try:
        packet = collect(args.repo, args.idea)
    except funnel.GitHubError as exc:
        print("shape-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


def apply_shape(items: list, now: datetime, ref: str,
                answer_data: object,
                run: Optional[str] = None,
                agent: Optional[str] = None) -> int:
    """Validate one shape answer and record the plan it carries.

    Renders the issue body from the answer fields, applies the
    self-approval rule mechanically, writes Shaped or Ready, and posts
    the self-approval record as a code-owned comment on a Ready
    transition. Validation runs before the first write, so a malformed
    answer leaves the idea untouched.

    The open-question record comes from the needs_nate fields rather than a
    Needs-section parse. A manually placed origin-override block is carried
    over verbatim; Origin itself lives in the Project field.

    Sequencing dependencies ride the body write as native blocked-by
    edges (#1053): one ``gh issue edit`` carries the rendered plan and
    the edges together, so a ref GitHub cannot resolve fails the whole
    write instead of recording a plan whose dependency is missing.
    """
    item = funnel.find(items, ref)
    answer = validate_answer(answer_data)
    answer, rejected_signals = review_shape_output_for_item(
        items, item, answer)
    report_output_review(rejected_signals)

    original_body = item.body or ""
    origin_voice = item.origin
    carried_blocks = []
    for marker in (funnel.ORIGIN_OVERRIDE_MARKER,):
        block = funnel._marked_json_block(original_body, marker)
        if block is not None:
            carried_blocks.append(block)
    class_missing = item.klass not in funnel.LADDER

    rendered = render_plan(answer)
    # Decided before the first write from the same inputs as the shadow
    # path: the decision is pure, and previewing it early changes nothing
    # the writes below can observe.
    status, reason = preview_decision(items, item, answer)
    authority_signals = funnel.needs_nate_signals(rendered)
    body = funnel.append_provenance(
        rendered, "agent", at=now, run=run, agent=agent)
    for block in carried_blocks:
        body = "{}\n\n{}".format(body, block)

    depends_on = answer.get("depends_on", [])
    current_blockers = getattr(item, "blocked_by_refs", None)
    if depends_on and current_blockers is None:
        raise funnel.GitHubError(
            "cannot write dependencies for {}: existing blocked-by edges "
            "are unavailable".format(item.ref)
        )
    current_blocker_set = set(current_blockers or [])
    pending_dependencies = [
        ref for ref in depends_on if ref not in current_blocker_set
    ]
    command = ["gh", "issue", "edit", str(item.number), "--repo",
               item.repo, "--body", body]
    blocked_by = blocked_by_values(pending_dependencies, item.repo)
    if blocked_by:
        command += ["--add-blocked-by", ",".join(blocked_by)]
    out = funnel._run_gh(command, capture_output=True, text=True)
    if out.returncode != 0:
        write_error = out.stderr.strip() or "gh issue edit failed"
        if pending_dependencies:
            try:
                confirmed_blockers = read_blocked_by_refs(item)
            except funnel.GitHubError as exc:
                raise funnel.GitHubError(
                    "{}; could not confirm blocked-by edge(s) {} for {}: {}".format(
                        write_error, ", ".join(pending_dependencies), item.ref, exc
                    )
                ) from exc
            missing_dependencies = [
                ref for ref in pending_dependencies
                if ref not in set(confirmed_blockers)
            ]
            if missing_dependencies:
                raise funnel.GitHubError(
                    "{}; blocked-by edge(s) still missing for {}: {}".format(
                        write_error, item.ref,
                        ", ".join(missing_dependencies),
                    )
                )
            # The edge may have been added by a concurrent writer after the
            # Project snapshot. Confirmed state makes it satisfied; do not
            # retry the refused issue edit.
            item.blocked_by_refs = confirmed_blockers
        else:
            raise funnel.GitHubError(write_error)
    elif pending_dependencies:
        item.blocked_by_refs = list(dict.fromkeys(
            list(current_blockers or []) + pending_dependencies
        ))
    # The session keeps this object after the issue-body write. Keep its
    # body aligned with GitHub before a same-session reader evaluates it.
    item.body = body

    if not item.item_id:
        raise funnel.GitHubError("{} is not in the Project".format(item.ref))
    if class_missing and origin_voice == "agent":
        # This recovery write is the only place shaping may assign a
        # Class. Keep it immediately before the Status mutation so the
        # latter never makes an unclassed idea look like it advanced
        # cleanly.
        funnel.gh_graphql(
            funnel.SET_FIELD, project=funnel.PROJECT_ID,
            item=item.item_id, field=funnel.CLASS_FIELD_ID,
            option=funnel._option_id(funnel.CLASS_FIELD_ID,
                                     answer["proposed_class"]))
    risk = "escalated" if (
        answer["escalated_risk"]
        or funnel.plan_escalation_matches(answer["plan_markdown"])
    ) else "standard"
    needs = "human" if status == "Shaped" else "none"
    funnel.write_project_select(item.item_id, "Risk", risk, item.ref)
    funnel.write_project_select(item.item_id, "Needs", needs, item.ref)
    item.risk = risk
    item.needs = needs
    status_error = funnel._write_status(item, status, now)
    if status_error is not None:
        # The body is durable, but the stage is not confirmed. Do not
        # clear the shaping label or post a self-approval marker, both
        # of which would make the item look further along than its known
        # Project state.
        persisted_status = item.status or "unknown"
        print("{} → {}\n{}".format(item.ref, persisted_status, item.url))
        print(
            "could not confirm requested Status {} for {}; retaining the "
            "previously observed stage {}: {}".format(
                status, item.ref, persisted_status, status_error
            ),
            file=sys.stderr,
        )
        return 1

    label = funnel._run_gh(
        ["gh", "issue", "edit", str(item.number), "--repo", item.repo,
         "--remove-label", "needs-shaping"],
        capture_output=True, text=True,
    )
    if label.returncode == 0:
        item.labels = [
            value for value in item.labels if value != "needs-shaping"
        ]
    if status == "Ready":
        basis = "{}; no escalated risk".format(reason)
        if authority_signals:
            basis += "; authority signals: {}".format(
                ", ".join(authority_signals)
            )
        comment = funnel._run_gh(
            ["gh", "issue", "comment", str(item.number),
             "--repo", item.repo,
             "--body", funnel.self_approval_comment(
                 basis, at=now, run=run, agent=agent
             )],
            capture_output=True, text=True,
        )
        if comment.returncode != 0:
            raise funnel.GitHubError(comment.stderr.strip())
    print("{} → {}\n{}".format(item.ref, status, item.url))
    if status == "Ready":
        print("advanced to Ready: {}".format(reason))
    else:
        print("held at Shaped: {}".format(reason))
    if authority_signals:
        print("\n--- self-approval advisory ---")
        print("Authority signals are recorded in the Self-approved basis:")
        descriptions = {
            "policy authority": (
                "cites plan.md or AGENTS.md on a gate, membership, "
                "or who may write"
            ),
            "unattended authority": "changes what an agent may do unattended",
            "gate authority": "changes a gate's question, answer, or owner",
            "field authority": (
                "changes who may set a field that other rules act on"
            ),
        }
        for signal in authority_signals:
            print("  {}: {}".format(
                signal, descriptions.get(signal, signal)
            ))
    if status != "Ready":
        print("\nIt now waits on you: is the plan good? "
              "Answer by moving it to Ready.")
    return 0


def apply_main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate a shape answer from a file or stdin and record it."""
    parser = argparse.ArgumentParser(
        description="validate one shape answer and record the plan")
    parser.add_argument("idea", type=int, help="idea issue number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    parser.add_argument("--answer", required=True,
                        help="answer JSON file, or - for stdin")
    parser.add_argument("--run", default=None,
                        help="run id recorded in provenance blocks")
    parser.add_argument("--agent", default=None,
                        help="agent name recorded in provenance blocks")
    parser.add_argument("--attempt", type=int, default=None,
                        help="attempt number in the runner protocol: a "
                             "malformed answer exits 3 below attempt 2 "
                             "(the runner retries once) and 1 at 2 or "
                             "later. Without --attempt, exit 1.")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate and decide without writing anything; "
                             "print the status, reason, and answer as JSON")
    args = parser.parse_args(argv)
    if args.attempt is not None and args.attempt < 1:
        parser.error("--attempt must be at least 1")
    try:
        if args.answer == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = pathlib.Path(args.answer).read_text()
            except OSError as exc:
                raise ShapeError(
                    "cannot read {}: {}".format(args.answer, exc))
    except (ShapeError, funnel.GitHubError) as exc:
        # An unreadable answer is a local failure, not a malformed one:
        # retrying the same read could not help.
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return 1
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print("shape-apply: the answer is not valid JSON: {}".format(exc),
              file=sys.stderr)
        return validation_exit(args.attempt)
    try:
        answer = validate_answer(data)
    except ShapeError as exc:
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return validation_exit(args.attempt)
    try:
        resolved = funnel.resolve_repo(args.repo)
        # Neither the decision, the output review, nor any write guard
        # (including the closed-issue Status refusal) reads Project
        # history, so the detail batch is skipped (#1620). `_write_status`
        # only assigns status_since/status_events after a confirmed write.
        items = funnel.load_items(include_details=False)
        ref = "{}#{}".format(resolved, args.idea)
        if args.validate_only:
            item = funnel.find(items, ref)
            answer, rejected_signals = review_shape_output_for_item(
                items, item, answer)
            report_output_review(rejected_signals)
            status, reason = preview_decision(items, item, answer)
            print(json.dumps({"status": status, "reason": reason,
                              "answer": answer},
                             indent=2, sort_keys=True))
            return 0
        return apply_shape(
            items, datetime.now(timezone.utc), ref, data,
            run=args.run, agent=args.agent)
    except (ShapeError, funnel.GitHubError) as exc:
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return 1
