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
from dataclasses import dataclass
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

#: Marker consumed by the shape runner for a clean stale-apply refusal.
SKIPPED_STALE_SHAPE_OUTCOME = "run outcome: skipped-stale-shape"


def validation_exit(attempt: Optional[int]) -> int:
    """The exit for a malformed answer, recording nothing either way.

    Without ``--attempt`` the single-shot legacy exit 1 stands. With an
    explicit attempt the runner protocol applies: retryable 3 below the
    final attempt, 1 at it.
    """
    if attempt is None:
        return 1
    return RETRY_EXIT if attempt < FINAL_ATTEMPT else 1


def _read_fresh_shape_facts(item) -> Tuple[str, Optional[str], int]:
    """Read the target's current issue state, Project Status and complete
    child count: the inputs of ``funnel.unshapeable_reason`` (#2139)."""
    fresh_items = funnel.load_project_items_by_refs([item.ref])
    if (not isinstance(fresh_items, list) or len(fresh_items) != 1
            or getattr(fresh_items[0], "ref", None) != item.ref):
        raise funnel.GitHubError(
            "could not re-read {} from the Project before shape apply".format(
                item.ref
            )
        )

    fresh = fresh_items[0]
    state = getattr(fresh, "state", None)
    if not isinstance(state, str):
        raise funnel.GitHubError(
            "could not read current issue state for {} before shape "
            "apply".format(item.ref)
        )
    status = getattr(fresh, "status", None)
    if status is not None and not isinstance(status, str):
        raise funnel.GitHubError(
            "could not read current Status for {} before shape apply".format(
                item.ref
            )
        )
    children_total = getattr(fresh, "children_total", None)
    if (not isinstance(children_total, int)
            or isinstance(children_total, bool) or children_total < 0):
        raise funnel.GitHubError(
            "could not read current sub-issue count for {} before shape "
            "apply".format(item.ref)
        )
    return state, status, children_total


#: The answer keys shape-apply accepts — exactly these, no extras. From
#: the Shape row of #794, plus the model-declared escalated-risk list
#: (#1034), the only risk that holds a plan since #1721, plus the
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
    "failure_modes",
    "hotspot_targets",
    "redesign_remainder",
})

#: Optional answer fields. Omission is normalized before exact-key validation.
OPTIONAL_ANSWER_KEYS = frozenset({
    "failure_modes", "hotspot_targets", "redesign_remainder",
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
#: false-hold cases before they are recorded. ``shape_inputs`` decides
#: both, so an agent-origin idea whose Class the answer will set gets it
#: too (#2136).
AGENT_SELF_APPROVABLE_OUTPUT_REVIEW = {
    "scope": (
        "For agent-origin Investigate, Broken, Maintenance, Improve, and "
        "Bug work, ask Scope and priority only for a concrete unresolved "
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


#: The five decision-first phases (#2407). Shape names them as strings so
#: phase proposals validate before the Class-option migration lands; only
#: Implement carries a gate here.
PHASE_CLASS_NAMES = (
    "Curate", "Describe", "Hypothesize", "Test", "Implement",
)

#: An observed-symptom marker: "Observed symptom" with a space or a hyphen,
#: in any case. The canonical line is Title Case with a space.
_OBSERVED_SYMPTOM_MARKER_RE = re.compile(
    r"observed[\s-]+symptom", re.IGNORECASE)
_OBSERVED_SYMPTOM_LINE_RE = re.compile(
    r"observed[\s-]+symptom:[ ]+(.+)", re.IGNORECASE)


def _validate_investigate_observed_symptom(plan_markdown: str) -> None:
    """Require one explicit observed-symptom statement for Investigate.

    Mirrors ``_validate_investigate_possible_defect``: only a whole line in
    the plan narrative counts, and a malformed line carrying the marker
    fails closed, even if another valid line is present.
    """
    matching_lines = []
    malformed_line = False
    for raw_line in plan_markdown.splitlines():
        line = raw_line.strip()
        if not _OBSERVED_SYMPTOM_MARKER_RE.match(line):
            continue
        match = _OBSERVED_SYMPTOM_LINE_RE.fullmatch(line)
        if not match or not match.group(1).strip():
            malformed_line = True
            continue
        if len(_OBSERVED_SYMPTOM_MARKER_RE.findall(line)) != 1:
            malformed_line = True
            continue
        matching_lines.append(line)

    if malformed_line or len(matching_lines) != 1:
        raise ShapeError(
            "proposed_class Investigate requires exactly one non-empty "
            "whole line of the form 'Observed symptom: <statement>' "
            "in plan_markdown")


_TESTED_FINDING_RE = re.compile(
    r"\btest(?:ed)?[\s-]+finding\b\s*:\s*\S", re.IGNORECASE)
_EXPLICIT_ASK_RE = re.compile(
    r"\bexplicit[\s-]+ask\b\s*:\s*\S", re.IGNORECASE)
_VERIFY_LINE_RE = re.compile(r"\bverify\s*:\s*\S", re.IGNORECASE)


def _validate_implement_basis(plan_markdown: str) -> None:
    """Require an Implement plan to cite its basis (#2407).

    A tested finding or Nate's explicit ask, each as one non-empty line of
    the form 'Tested finding: <finding>' or 'Explicit ask: <ask>'. A cheap,
    reversible change may instead ship with its test: the plan says it is
    cheap and reversible and carries a 'Verify: <check>' line naming the
    check that judges it. At least one basis passes; a blank citation
    cites nothing.
    """
    lines = plan_markdown.splitlines()
    if any(_TESTED_FINDING_RE.search(line) for line in lines):
        return
    if any(_EXPLICIT_ASK_RE.search(line) for line in lines):
        return
    lowered = plan_markdown.lower()
    cheap_reversible = (
        "cheap" in lowered and "reversib" in lowered
        and "irreversib" not in lowered)
    if cheap_reversible and any(
            _VERIFY_LINE_RE.search(line) for line in lines):
        return
    raise ShapeError(
        "proposed_class Implement requires a tested finding or an "
        "explicit ask: one non-empty whole line of the form "
        "'Tested finding: <finding>' or 'Explicit ask: <ask>'; a cheap, "
        "reversible change may instead ship with its test as a "
        "'Verify: <check>' line")


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
    judges the plan itself here, not its wording: this list holds the
    plan at Shaped, while a wording-scan hit it omits only raises the
    review tier (#1721). An empty list never lowers the Risk field.
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


def _validate_failure_modes(entries: object) -> List[str]:
    """Validate the bounded Review focus list (#1839, #2022)."""
    if not isinstance(entries, list):
        raise ShapeError("failure_modes must be a list")
    if len(entries) > 3:
        raise ShapeError("failure_modes must contain at most 3 items")
    return [
        _require_line(entry, "failure_modes[{}]".format(index))
        for index, entry in enumerate(entries)
    ]


def _validate_packet_hotspots(hotspots: object) -> List[Dict[str, object]]:
    """Validate the runner-owned hotspot list the answer may select from."""
    if not isinstance(hotspots, (list, tuple)):
        raise ShapeError("the packet's hotspots must be a list")
    validated = []
    for index, row in enumerate(hotspots):
        where = "packet hotspots[{}]".format(index)
        if not isinstance(row, dict) or set(row) != {
                "repo_path", "function", "broken_fix_count", "window_days"}:
            raise ShapeError(
                "{} must contain repo_path, function, broken_fix_count, "
                "and window_days".format(where))
        path = _require_line(row["repo_path"], where + ".repo_path")
        function = _require_line(row["function"], where + ".function")
        count = row["broken_fix_count"]
        window_days = row["window_days"]
        if (not isinstance(count, int) or isinstance(count, bool)
                or count < 3):
            raise ShapeError(
                "{}.broken_fix_count must be at least 3".format(where))
        if (not isinstance(window_days, int) or isinstance(window_days, bool)
                or window_days != 7):
            raise ShapeError("{}.window_days must be 7".format(where))
        validated.append({
            "repo_path": path,
            "function": function,
            "broken_fix_count": count,
            "window_days": window_days,
        })
    return validated


def _validate_hotspot_routing(
        entries: object, remainder: object,
        hotspots: Sequence[Dict[str, object]], proposed_class: str
        ) -> Tuple[List[Dict[str, str]], str]:
    """Keep planned targets inside the packet's measured hotspot list."""
    if not isinstance(entries, list):
        raise ShapeError("hotspot_targets must be a list")
    allowed = {
        (row["repo_path"], row["function"]) for row in hotspots
    }
    targets = []
    seen = set()
    for index, entry in enumerate(entries):
        where = "hotspot_targets[{}]".format(index)
        if (not isinstance(entry, dict)
                or set(entry) != {"repo_path", "function"}):
            raise ShapeError(
                "{} must contain only repo_path and function".format(where))
        target = {
            "repo_path": _require_line(
                entry["repo_path"], where + ".repo_path"),
            "function": _require_line(
                entry["function"], where + ".function"),
        }
        key = (target["repo_path"], target["function"])
        if key not in allowed:
            raise ShapeError(
                "{} is not in the packet's hotspot list".format(
                    target["repo_path"] + ":" + target["function"]))
        if key in seen:
            raise ShapeError("{} duplicates a hotspot target".format(where))
        seen.add(key)
        targets.append(target)

    if targets:
        if proposed_class != "Broken":
            raise ShapeError(
                "hotspot routing requires proposed_class Broken")
        return targets, _require_text(remainder, "redesign_remainder")
    if not isinstance(remainder, str) or remainder.strip():
        raise ShapeError(
            "redesign_remainder requires at least one hotspot target")
    return [], ""


def validate_answer(
        data: object, *, include_failure_modes: bool = True,
        hotspots: Optional[Sequence[Dict[str, object]]] = None,
        include_hotspot_routing: bool = True) -> Dict:
    """Validate a shape answer against the plan schema.

    Returns a normalized copy: surrounding whitespace stripped, inner
    newlines in one-line fields collapsed. Anything malformed raises
    ``ShapeError`` before any write, so a confused model cannot leave a
    half-shaped idea behind. ``failure_modes`` is optional and omission is
    equivalent to an empty list. The runner can omit it for a safe fallback.
    ``hotspot_targets`` is restricted to the packet's measured list, and a
    non-empty target list requires a ``redesign_remainder``.
    """
    if isinstance(data, dict):
        data = dict(data)
        if not include_failure_modes:
            data.pop("failure_modes", None)
        if not include_hotspot_routing:
            data.pop("hotspot_targets", None)
            data.pop("redesign_remainder", None)
        for key in OPTIONAL_ANSWER_KEYS:
            data.setdefault(
                key, "" if key == "redesign_remainder" else [])
    _check_keys(data, sorted(ANSWER_KEYS), "the answer")
    assert isinstance(data, dict)
    proposed = _require_line(data["proposed_class"], "proposed_class")
    if proposed not in funnel.LADDER and proposed not in PHASE_CLASS_NAMES:
        raise ShapeError(
            "proposed_class {!r} is not a ladder class; choose one of "
            "{}".format(proposed, ", ".join(funnel.LADDER)))
    allowed_hotspots = _validate_packet_hotspots(
        hotspots if include_hotspot_routing and hotspots is not None else [])
    hotspot_targets, redesign_remainder = _validate_hotspot_routing(
        data["hotspot_targets"], data["redesign_remainder"],
        allowed_hotspots, proposed)
    decided_from_precedent = _validate_precedent(
        data["decided_from_precedent"])
    decided_by_agent = _validate_agent_decisions(
        data["decided_by_agent"])
    needs_nate = _validate_needs_nate(data["needs_nate"])
    plan_markdown = _require_text(data["plan_markdown"], "plan_markdown")
    if proposed == "Investigate":
        _validate_investigate_possible_defect(plan_markdown)
        _validate_investigate_observed_symptom(plan_markdown)
    if proposed == "Implement":
        _validate_implement_basis(plan_markdown)
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
        "failure_modes": _validate_failure_modes(data["failure_modes"]),
        "hotspot_targets": hotspot_targets,
        "redesign_remainder": redesign_remainder,
    }


def _without_proposed_class_lines(plan_markdown: str) -> str:
    """The model's narrative minus any whole-line ``Proposed class:`` line.

    The runner owns that line and appends it from ``proposed_class``. A
    model that also wrote one left the body with two, which
    ``funnel.proposed_class_for_approval`` reads as ambiguous, so approval
    could not adopt the Class (#1697, #1723). Matching with the funnel's
    own pattern drops exactly the lines approval would count, fuzzy values
    included, and leaves prose that merely mentions the phrase.
    """
    return "".join(
        line for line in plan_markdown.splitlines(keepends=True)
        if funnel.PROPOSED_CLASS_LINE_RE.fullmatch(line) is None
    )


def render_plan(answer: Dict, *,
                hotspot_routes: Sequence[Dict[str, str]] = ()) -> str:
    """Render the issue body from validated answer fields.

    The plan narrative is followed by its evidence-backed premises and
    proposed class, risk rationale when the model declared a risk, then the
    runner-owned decision record: what precedent settled, what the
    agent decided itself, the sequencing dependencies where any wait
    (#1053), and only the Needs Nate categories with open questions.
    Canonical Risk and Needs values live in Project fields. Takes a
    validated answer; ``apply_shape`` validates before calling. The body
    carries exactly one ``Proposed class:`` line, the runner's (#1723).

    No origin override is rendered (#2138). The runner carries the one the
    decision honoured after this body, so an override marker anywhere in
    the model's fields, the narrative or a one-line field, is made inert:
    the model never moves an idea toward agents or toward Nate.
    """
    lines = [_without_proposed_class_lines(answer["plan_markdown"]).rstrip()]
    failure_modes = answer.get("failure_modes", [])
    if failure_modes:
        lines.extend(["", "## Review focus", ""])
        lines.extend("- {}".format(mode) for mode in failure_modes)
    lines.extend(["", "## Premises", ""])
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
    if hotspot_routes:
        lines.extend(["", "## Hotspot routing", ""])
        for route in hotspot_routes:
            target = "{}:{}".format(
                route["repo_path"], route["function"])
            lines.append("- `{}` → [{}]({})".format(
                target, route["ref"], route["url"]))
        lines.extend(["", "Containment: one ticket"])
    lines.append("")
    # Only model text can hold the marker: no runner line above writes it.
    # ``&lt;!--`` renders as ``<!--`` on GitHub, as in
    # ``funnel.inert_comment_text``, but no reader takes it for the marker.
    marker = funnel.ORIGIN_OVERRIDE_MARKER
    return "\n".join(lines).replace(
        marker, marker.replace("<!--", "&lt;!--", 1))


def open_need_categories(answer: Dict) -> List[str]:
    """The Needs Nate categories holding a question, in field order."""
    return [category for field, category in NEEDS_FIELDS
            if answer["needs_nate"][field] is not None]


def _plan_escalation_scan_body(answer: Dict) -> str:
    """Render the complete plan body without echoing typed risk declarations.

    Declared risks are evaluated separately from the wording scan. Every
    other rendered section must be in the scan so preview and the persisted
    Risk field read the same plan; ``funnel.plan_scan_text`` then decides
    which regions of it the scan reads (#2180).
    """
    scan_answer = dict(answer)
    scan_answer["escalated_risk"] = []
    return render_plan(scan_answer)


def _plan_escalation_matches(answer: Dict) -> List[Dict[str, Optional[str]]]:
    """Scan the same rendered plan body used by preview and Risk writes."""
    return funnel.plan_escalation_matches(
        _plan_escalation_scan_body(answer))


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


def output_review_applies(klass: Optional[str],
                          origin_voice: Optional[str]) -> bool:
    """Whether the agent-origin output review judges an answer (#2136).

    The one copy of the predicate: ``review_agent_shape_output`` gates on it
    and ``shape_inputs`` builds the packet's flag from it. Agent origin only;
    an origin override to agents does not widen it.
    """
    return (origin_voice == "agent"
            and klass in funnel.SELF_APPROVABLE_CLASSES)


@dataclass(frozen=True)
class ShapeInputs:
    """One reading of an idea's shaping inputs (#2136, #1746).

    The packet, the output review, preview and apply each read origin,
    override and Class themselves until #2136, and the copies drifted: #1369
    and #1448 each patched one, and the packet then omitted the guidance the
    review enforced on an agent-origin idea with no Class.

    ``class_adopted``: the idea is agent-origin with no ladder Class, so the
    answer's ``proposed_class`` is its Class and apply writes it. ``klass``
    is the effective Class the decision uses: the adopted one, else the
    parent's Class or the idea's own. It is None for an adopted Class read
    without an answer, which only the answer can supply. ``output_review``
    says whether the agent-origin output review applies: the effective Class
    is self-approvable, or, before the answer, the Class is still to be
    adopted from it. ``state`` is the issue's GitHub state.
    """

    origin_voice: Optional[str]
    override_target: Optional[str]
    class_adopted: bool
    klass: Optional[str]
    output_review: bool
    state: Optional[str]


def shape_inputs(items: Sequence, item,
                 answer: Optional[Dict] = None) -> ShapeInputs:
    """Read the inputs every shaping step decides from (#2136).

    ``collect`` reads without an answer; the output review, preview and
    apply pass the validated answer. Pure apart from its arguments.
    """
    origin_voice = item.origin
    override = funnel.parse_origin_override(item.body or "")
    override_target = override["target"] if override is not None else None
    class_adopted = item.klass not in funnel.LADDER and origin_voice == "agent"
    if class_adopted:
        klass = answer["proposed_class"] if answer is not None else None
    else:
        by_ref = {candidate.ref: candidate for candidate in items}
        klass = funnel.effective_class(item, by_ref)
    return ShapeInputs(
        origin_voice=origin_voice,
        override_target=override_target,
        class_adopted=class_adopted,
        klass=klass,
        output_review=(output_review_applies(klass, origin_voice)
                       or (class_adopted and answer is None)),
        state=item.state,
    )


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
    plan, unless the wording scan finds a risky action in it (#1721). The
    shared self-approval predicate remains the sole gate.
    """
    reviewed = copy.deepcopy(answer)
    rejected = []
    if not output_review_applies(klass, origin_voice):
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
    hypothetical = [entry for entry in risks
                    if _HYPOTHETICAL_IMPLEMENTATION_RISK.search(
                        entry["why"])]
    # A hypothetical-bug why is dropped only when the plan's wording proposes
    # no risky action at all. Until #1721 the scan's own hold caught such a
    # plan whatever the reasons; now that the scan holds nothing, dropping
    # the shaper's declaration there would release a plan it called risky.
    # The scan body leaves the typed whys out, so a why cannot vouch for
    # itself.
    if hypothetical and _plan_escalation_matches(reviewed):
        hypothetical = []
    kept_risks = [entry for entry in risks if entry not in hypothetical]
    if len(kept_risks) != len(risks):
        reviewed["escalated_risk"] = kept_risks
        rejected.append(
            "escalated risk based only on a hypothetical implementation bug")
    return reviewed, rejected


def declared_risks(answer: Dict) -> List[str]:
    """The risks the shaper declared: the one predicate for a hold (#1721).

    The typed ``escalated_risk`` reasons, then whatever
    ``funnel.plan_declared_risks`` finds in the rendered scan body: a
    ``Risk rationale`` section or a ``Risk: escalated`` line the shaper
    wrote into the plan. The Shaped sweep calls the same reader on the
    stored body, where the typed list is rendered as that section, so the
    first write and the sweep cannot disagree about what holds.
    """
    declared = []
    for entry in answer.get("escalated_risk", []):
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if isinstance(reason, str) and reason not in declared:
            declared.append(reason)
    for reason in funnel.plan_declared_risks(
            _plan_escalation_scan_body(answer)):
        if reason not in declared:
            declared.append(reason)
    return declared


def scan_only_matches(answer: Dict,
                      matches: Sequence[Dict[str, Optional[str]]]
                      ) -> List[Dict[str, Optional[str]]]:
    """The wording-scan matches that raise the tier but do not hold (#1721).

    Empty when the shaper declared a risk (``declared_risks``): the
    declaration holds the plan and carries its own explanation.
    """
    if declared_risks(answer):
        return []
    return [entry for entry in matches
            if isinstance(entry.get("reason"), str)]


def scan_escalation_comment(matches: Sequence[Dict[str, Optional[str]]],
                            status: str = "Ready",
                            at: Optional[datetime] = None,
                            run: Optional[str] = None,
                            agent: Optional[str] = None) -> str:
    """The one comment recording a scan-only escalation's evidence (#1721).

    Each reason is listed with the plan line it matched, quoted, so the
    breakdown and the reviewer can see what raised the tier and judge it.
    The heading says the plan was not held only when it advanced: an open
    question or the class can still hold it at Shaped. The quoted line is
    the model's plan text, so it is made inert (#1798).
    """
    if status == "Ready":
        heading = "Escalation scan: review tier raised, plan not held."
    else:
        heading = ("Escalation scan: review tier raised; the plan waits at "
                   "Shaped for another reason.")
    lines = [
        "**{}** The shaper declared no risk, and the wording scan matched "
        "the lines below, so Risk is `escalated` and the escalated reviewer "
        "judges the implementation. The scan alone never holds a plan at "
        "Shaped (#1721).".format(heading),
        "",
    ]
    for entry in matches:
        lines.append("- `{}`".format(entry["reason"]))
        line = entry.get("line")
        if isinstance(line, str) and line.strip():
            lines.append("  > {}".format(funnel.inert_comment_text(line)))
    return funnel.append_provenance(
        "\n".join(lines), "agent", at=at, run=run, agent=agent)


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
    effective shaper is agents, and the plan declares no escalated risk.
    The rule itself is the shared ``self_approval_eligible`` predicate,
    so the packet path cannot drift from the shaping path; only the
    needs_nate input comes from the fields instead of the parser.

    Only a declared risk holds (#1721, reversing #1034's union), as
    ``declared_risks`` reads it from the answer: the typed
    ``escalated_risk``, or a ``Risk rationale`` section or
    ``Risk: escalated`` line in the plan. A wording-scan hit alone, passed
    as ``escalation_reasons``, no longer holds: the
    plan advances as its other fields allow, and the Ready reason names the
    scan's reasons, because the Risk write still records it as escalated so
    the escalated reviewer judges the implementation. Five predicate patches
    since #1034 each moved a false hold rather than ending it, and each one
    spent a decision of Nate's where the scan's own comment prices a false
    positive at one escalated review. Matching lines from
    ``escalation_matches`` are included in the explanation of a hold.

    Sequencing dependencies never hold (#1053): a plan that waits on
    a named sibling but asks Nate nothing self-approves, and the
    dependency is recorded as a native edge, not a question.
    """
    scanned = sorted({reason for reason in escalation_reasons or ()
                      if isinstance(reason, str)})
    reasons = sorted(declared_risks(answer))
    scan_only = [] if reasons else scanned
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
        reason = ("needs_nate all null; class {} self-approvable; "
                  "{}".format(klass, owner_basis))
        if scan_only:
            reason += "; " + funnel.scan_only_escalation_note(scan_only)
        return "Ready", reason
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


@dataclass(frozen=True)
class ShapeDecision:
    """One answer's decision record: everything shaping writes (#2137).

    Preview, ``--validate-only`` and ``apply_shape`` each decided, scanned
    and derived the Risk write themselves until #2137, and preview and the
    Risk write drifted apart twice (#1538 in #1644, #1679 in #1721).
    ``decision_record`` now builds this once per answer and the writes read
    only from it.

    ``answer`` is the validated answer the record was built from, reviewed
    when ``shape_decision`` built it. ``rendered`` is ``render_plan``'s
    body and ``matches`` the wording scan of it, typed declarations left
    out; ``declared`` is what holds the plan (``declared_risks``).
    ``status`` and ``reason`` are ``decide``'s. ``risk`` is escalated
    exactly when a risk is declared or scanned, and ``needs`` is human
    exactly when the status is Shaped. ``scan_only`` holds the matches that
    raise the review tier without holding, ``authority_signals`` the
    advisory signals in the rendered plan, and ``risk_record`` the runner's
    shape-risk block for the body.
    """

    answer: Dict
    inputs: ShapeInputs
    rendered: str
    matches: List[Dict[str, Optional[str]]]
    declared: List[str]
    status: str
    reason: str
    risk: str
    needs: str
    scan_only: List[Dict[str, Optional[str]]]
    authority_signals: List[str]
    risk_record: str


def decision_record(
        items: Sequence, item, answer: Dict, *,
        hotspot_routes: Sequence[Dict[str, str]] = ()) -> ShapeDecision:
    """Build one validated answer's decision record (#2137).

    The inputs come from ``shape_inputs`` (#2136): the effective class with
    the class-missing recovery, the origin voice and override, and the
    issue state. The model's escalated-risk declaration inside the answer
    and the escalated-risk scan of the rendered plan complete it. Pure
    apart from its arguments; it does not review the answer.
    """
    inputs = shape_inputs(items, item, answer)
    # `decide` consumes the typed declaration separately. Keep it out of the
    # wording scan so the durable Risk rationale does not report the same
    # declaration twice.
    matches = _plan_escalation_matches(answer)
    scan = [entry["reason"] for entry in matches
            if isinstance(entry.get("reason"), str)]
    declared = declared_risks(answer)
    status, reason = decide(
        answer,
        klass=inputs.klass,
        origin_voice=inputs.origin_voice,
        override_target=inputs.override_target,
        escalation_reasons=scan,
        escalation_matches=matches,
        state=inputs.state,
    )
    rendered = render_plan(answer, hotspot_routes=hotspot_routes)
    return ShapeDecision(
        answer=answer,
        inputs=inputs,
        rendered=rendered,
        matches=matches,
        declared=declared,
        status=status,
        reason=reason,
        # A declaration the shaper wrote into the plan holds it just as the
        # typed list does, so it must also write Risk escalated: the sweep
        # reads a standard Risk as no hold at all.
        risk="escalated" if (declared or matches) else "standard",
        needs="human" if status == "Shaped" else "none",
        scan_only=scan_only_matches(answer, matches),
        authority_signals=funnel.needs_nate_signals(rendered),
        # The runner's record of what the decision held on (#1721). The
        # Shaped sweep releases an escalated Risk only on this record, never
        # on prose alone, which a malformed fence or quote in the narrative
        # can blank.
        risk_record=funnel.shape_risk_block(declared, scan),
    )


def preview_decision(items: list, item, answer: Dict) -> Tuple[str, str]:
    """The status one validated answer would record, without writing.

    The status and reason of ``decision_record``, the record the live path
    writes from (#2137). Pure apart from its arguments; takes a validated
    answer and does not review it.
    """
    record = decision_record(items, item, answer)
    return record.status, record.reason


def review_shape_output_for_item(items: list, item, answer: Dict
                                 ) -> Tuple[Dict, List[str]]:
    """Apply the output review with the Class ``shape_inputs`` reads."""
    inputs = shape_inputs(items, item, answer)
    return review_agent_shape_output(
        answer, klass=inputs.klass, origin_voice=inputs.origin_voice,
        repo=item.repo)


def report_output_review(rejected: Sequence[str]) -> None:
    """Make any runner-side signal corrections visible to the caller."""
    if rejected:
        print("shape-apply output review rejected: {}".format(
            "; ".join(rejected)), file=sys.stderr)


def shape_decision(
        items: Sequence, item, answer_data: object, *,
        hotspots: Optional[Sequence[Dict[str, object]]] = None,
        hotspot_routes: Sequence[Dict[str, str]] = (),
        include_failure_modes: bool = True,
        include_hotspot_routing: bool = True) -> ShapeDecision:
    """Validate, review and record one answer (#2137).

    The steps ``apply_main --validate-only`` and the live ``apply_shape``
    share: each runs this once, so the status the preview prints is the one
    the live path writes. Raises ``ShapeError`` on a malformed answer
    before anything is reported.
    """
    answer = validate_answer(
        answer_data, hotspots=hotspots,
        include_failure_modes=include_failure_modes,
        include_hotspot_routing=include_hotspot_routing)
    reviewed, rejected_signals = review_shape_output_for_item(
        items, item, answer)
    report_output_review(rejected_signals)
    return decision_record(items, item, reviewed,
                           hotspot_routes=hotspot_routes)


def carried_override_blocks(original_body: str,
                            override_target: Optional[str]) -> List[str]:
    """The idea's blocks that carry the override the decision used (#2138).

    ``funnel.parse_origin_override`` honours an override to agents only
    while the body's newest provenance, in body order, is a Nate voice, and
    the plan body ends with the shaper's own provenance. So an override to
    agents is carried with the provenance block that authorised it, the
    one that reader checked on the idea's body, verbatim and after the
    shaper's: the runner never writes a Nate voice of its own. An override
    toward Nate needs no voice. An override the decision did not honour is
    not carried, so a nate-relayed shaping cannot lend it the voice it
    lacked. Each block is the newest parseable one, the one the reader read.
    """
    if override_target is None:
        return []
    markers = [funnel.ORIGIN_OVERRIDE_MARKER]
    if override_target == "agents":
        markers.append(funnel.PROVENANCE_MARKER)
    blocks = []
    for marker in markers:
        block = funnel._marked_json_block(original_body, marker)
        if block is not None:
            blocks.append(block)
    return blocks


def read_back_mismatches(body: str, decision: ShapeDecision) -> List[str]:
    """Where funnel's stored-body readers disagree with the decision (#2138).

    Every later reader, the Shaped sweep above all, reads the stored plan
    body, never the idea's body the decision read. Each entry names one
    reader that would see something other than what the decision used: the
    origin override, the runner's risk record, or the proposed Class.
    """
    mismatches = []
    override = funnel.parse_origin_override(body)
    target = override["target"] if override is not None else None
    if target != decision.inputs.override_target:
        mismatches.append(
            "the origin override reads {} where the decision used {}".format(
                target or "none", decision.inputs.override_target or "none"))
    runner_record = funnel._marked_json(
        decision.risk_record, funnel.SHAPE_RISK_MARKER)
    if funnel.parse_shape_risk_record(body) != runner_record:
        mismatches.append(
            "the shape-risk record read is not the runner's")
    proposed = funnel.proposed_class_for_approval(body)
    if proposed is None or proposed[0] != decision.answer["proposed_class"]:
        mismatches.append("the proposed Class does not read {}".format(
            decision.answer["proposed_class"]))
    return mismatches


def written_body(decision: ShapeDecision, original_body: str, *,
                 voice: str, now: datetime,
                 run: Optional[str] = None,
                 agent: Optional[str] = None) -> str:
    """The plan body ``apply_shape`` writes for ``decision`` (#2138).

    The rendered plan, the runner's risk record, the shaper's provenance,
    then the carried override blocks. The runner's record and provenance
    come before the carried blocks, so a later copied marker must not read
    as newer (#1937). Raises ``ShapeError``, before anything is written,
    when the body would not read back to the decision. In practice that is
    a carried Nate-voiced block dated at or after ``now``: it would become
    the provenance ``parse_shape_risk_record`` reads the runner's record
    against, and that record would read as unreadable.
    """
    body = funnel.append_provenance(
        "{}\n\n{}\n".format(decision.rendered.rstrip("\n"),
                            decision.risk_record),
        voice, at=now, run=run, agent=agent)
    for block in carried_override_blocks(
            original_body, decision.inputs.override_target):
        body = "{}\n\n{}".format(body, block)
    mismatches = read_back_mismatches(body, decision)
    if mismatches:
        raise ShapeError(
            "refusing to write the plan: its body would not read back to "
            "the decision it was written from: {}".format(
                "; ".join(mismatches)))
    return body


def issue_url(ref: str) -> str:
    """Render an owner/repo#n ref as the issue URL `gh` takes for edges."""
    match = REF_RE.match(ref.strip())
    if not match:
        raise ShapeError("not an owner/repo#n ref: {!r}".format(ref))
    return "https://github.com/{}/{}/issues/{}".format(
        match.group("owner"), match.group("repo"), match.group("number"))


def hotspot_redesign_candidate(items: Sequence, repo: str,
                               target: Dict[str, str]):
    """Find the most specific open Improve plan already owning a hotspot."""
    marker = "Hotspot: {}:{}".format(
        target["repo_path"], target["function"])
    prefix = "Redesign {}:{}".format(
        target["repo_path"], target["function"])
    candidates = []
    for item in items:
        if (item.repo != repo or str(item.state).upper() != "OPEN"
                or item.klass != "Improve"):
            continue
        body_match = marker in (item.body or "").splitlines()
        title_match = (isinstance(item.title, str)
                       and item.title.startswith(prefix))
        if body_match or title_match:
            candidates.append((not body_match, item.number, item))
    return min(candidates, default=(None, None, None),
               key=lambda row: (row[0], row[1]))[2]


def prepare_hotspot_routes(items: list, repo: str,
                           targets: Sequence[Dict[str, str]],
                           hotspots: Sequence[Dict[str, object]],
                           now: datetime) -> List[Dict[str, str]]:
    """Reuse or capture one open Improve project for each selected hotspot."""
    measured = {
        (row["repo_path"], row["function"]): row
        for row in hotspots
    }
    routes = []
    for target in targets:
        row = measured[(target["repo_path"], target["function"])]
        item = hotspot_redesign_candidate(items, repo, target)
        if item is None:
            title = "Redesign {}:{}: {} Broken fixes in seven days".format(
                target["repo_path"], target["function"],
                row["broken_fix_count"])
            note = (
                "Redesign this measured recurring defect hotspot.\n\n"
                "Hotspot: {}:{}"
            ).format(target["repo_path"], target["function"])
            urls: List[str] = []
            funnel.cmd_capture(
                items, now, title, note, repo=repo, origin="agent",
                klass="Improve", voice="agent", created_urls=urls,
                quiet=True)
            if len(urls) != 1:
                raise funnel.GitHubError(
                    "captured hotspot redesign has no confirmed issue URL")
            match = re.search(r"/issues/(\d+)$", urls[0])
            if match is None:
                raise funnel.GitHubError(
                    "captured hotspot redesign has an invalid issue URL")
            ref = "{}#{}".format(repo, match.group(1))
            url = urls[0]
        else:
            ref = item.ref
            url = item.url or issue_url(ref)
        routes.append({
            "repo_path": target["repo_path"],
            "function": target["function"],
            "ref": ref,
            "url": url,
        })
    return routes


def post_hotspot_remainders(routes: Sequence[Dict[str, str]],
                            remainder: str, broken_item, now: datetime, *,
                            run: Optional[str],
                            agent: Optional[str]) -> None:
    """Post the same scoped remainder once per redesign, linked to its plan."""
    grouped: Dict[str, List[str]] = {}
    for route in routes:
        grouped.setdefault(route["ref"], []).append(
            "{}:{}".format(route["repo_path"], route["function"]))
    for ref, targets in grouped.items():
        match = REF_RE.match(ref)
        if match is None:
            raise ShapeError("not an owner/repo#n ref: {!r}".format(ref))
        text = (
            "Hotspot(s): {}\n\n{}\n\nBroken plan: [{}]({})"
        ).format(
            ", ".join("`{}`".format(value) for value in targets),
            remainder.strip(), broken_item.ref, broken_item.url)
        body = funnel.append_provenance(
            text, "agent", at=now, run=run, agent=agent)
        result = funnel._run_gh(
            ["gh", "issue", "comment", match.group("number"),
             "--repo", "{}/{}".format(
                 match.group("owner"), match.group("repo")),
             "--body", body], capture_output=True, text=True)
        if result.returncode != 0:
            raise funnel.GitHubError(
                result.stderr.strip() or
                "could not post the redesign remainder for {}".format(ref))


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
    """Render every issue comment chronologically, preserving each body.

    Only the owner account's bodies are preserved (#1788). The shaper and
    breakdown read this thread as send-back reasoning and corrections that
    override a plan's premises, and anyone can comment on a public issue, so
    any other author's comment keeps its place as a one-line placeholder
    naming who posted it and when.
    """
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
        body = row["body"]
        if not funnel.trusted_comment(row):
            body = funnel.untrusted_comment_placeholder(row, created_at)
        ordered.append((timestamp, index, created_at, author.strip(), body))
    ordered.sort(key=lambda row: (row[0], row[1]))
    blocks = [
        "### @{} — {}\n\n{}".format(author, created_at, body)
        for _, _, created_at, author, body in ordered
    ]
    return "## Issue thread\n\n" + "\n\n".join(blocks)


def build_packet(*, repo: str, idea: Dict,
                 origin_voice: Optional[str],
                 override_target: Optional[str],
                 output_review: bool,
                 plan_md: str, plan_md_missing: bool,
                 agents_md: str, agents_md_missing: bool,
                 siblings: Sequence[Dict],
                 collected_at: str,
                 hotspots: Optional[Sequence[Dict[str, object]]] = None,
                 issue_comments: Optional[Sequence[Dict]] = None) -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO.

    Everything the shape question needs in one JSON-serialisable dict:
    the idea, its origin, the repo's plan.md and AGENTS.md, and the
    sibling plans the model cites as precedent. ``output_review`` is
    ``shape_inputs``' flag, so the packet carries the review policy exactly
    when the review will judge the answer (#2136).
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
    if hotspots is not None:
        packet["hotspots"] = [dict(row) for row in hotspots]
    issue_thread = issue_thread_section(
        issue_comments if issue_comments is not None else [])
    if issue_thread is not None:
        packet["issue_thread"] = issue_thread
    if output_review:
        packet["output_review"] = dict(
            AGENT_SELF_APPROVABLE_OUTPUT_REVIEW)
    return packet


def load_packet_bodies(items: list, idea_item) -> None:
    """Read the idea's and its included siblings' unloaded bodies (#2149).

    The begin view lists open rows without ``Issue.body`` (#1775), so the
    packet published the idea and every sibling plan with ``body: null``
    and the origin override read an empty body. Only the idea and the
    sibling plans the packet carries are read, and only when unloaded,
    through the body-only read #2067 added; no other row is. A body still
    unloaded after the read fails the packet rather than ship it empty.
    """
    wanted = [row for row in [idea_item]
              + sibling_plan_items(items, idea_item)
              if not getattr(row, "body_loaded", True)]
    if not wanted:
        return
    funnel.hydrate_item_details(items, wanted, body_only=True)
    for row in wanted:
        if not getattr(row, "body_loaded", True):
            raise funnel.GitHubError(
                "could not read the body of {}".format(row.ref))


def measure_shape_hotspots(now: datetime,
                           items_loader: Optional[Callable[[], list]] = None
                           ) -> List[Dict[str, object]]:
    """Measure runner-owned Broken-fix hotspots for the shape packet."""
    import fix_recurrence

    try:
        items = (items_loader() if items_loader is not None
                 else funnel.load_items(include_details=False))
        snapshot = {
            "recorded_cause_regressions":
                funnel.recorded_cause_regressions(items, now)
        }
        projects = fix_recurrence.fix_projects_from_snapshot(snapshot)
        measured = fix_recurrence.measure(
            pathlib.Path(__file__).resolve().parent.parent, projects, now)
    except (funnel.GitHubError, fix_recurrence.RecurrenceError,
            OSError, ValueError) as exc:
        raise funnel.GitHubError(
            "shape hotspots could not be measured: {}".format(exc)) from exc

    if (measured.get("window_days") != 7
            or measured.get("hotspot_threshold") != 3
            or not isinstance(measured.get("hotspots"), list)):
        raise funnel.GitHubError(
            "shape hotspots returned an unexpected measurement")
    rows = measured["hotspots"]
    hotspots = []
    for index, row in enumerate(rows):
        if (not isinstance(row, dict)
                or not isinstance(row.get("path"), str)
                or not row["path"].strip()
                or not isinstance(row.get("function"), str)
                or not row["function"].strip()
                or not isinstance(row.get("count"), int)
                or isinstance(row.get("count"), bool)
                or row["count"] < 2):
            raise funnel.GitHubError(
                "shape hotspot row {} is malformed".format(index))
        if row["count"] >= 3:
            hotspots.append({
                "repo_path": row["path"], "function": row["function"],
                "broken_fix_count": row["count"], "window_days": 7,
            })
    hotspots.sort(key=lambda row: row["broken_fix_count"], reverse=True)
    return hotspots


def collect(repo: Optional[str], idea_number: int, *,
            items_loader: Optional[Callable[[], list]] = None,
            include_hotspot_routing: bool = True,
            now: Optional[datetime] = None) -> Dict:
    """Fetch every piece and build the packet. Reads only, no writes."""
    resolved = funnel.resolve_repo(repo)
    idea_ref = "{}#{}".format(resolved, idea_number)
    if items_loader is None:
        # No packet field reads Project history (status_since, status
        # events, blocked times, child timestamps), so skip the per-item
        # detail batch: it is most of this load's time and points (#1620).
        # Nor does it read a closed item other than, at most, the idea:
        # siblings are open plans. So the begin view serves it, and the
        # idea's thread rides that load's first request (#1625).
        items = funnel.load_items(
            include_details=False, shape_issue=(resolved, idea_number),
            scope="begin")
        if not any(row.ref == idea_ref for row in items):
            # The begin view holds every open item and only some closed
            # ones, so a missing idea is closed or off the board. Refuse
            # rather than guess: the full load would have found a closed
            # one, and shaping a closed idea is not a job begin offers.
            raise funnel.GitHubError(
                "{} is not in the open board view; shape-packet reads "
                "only open ideas and the closed items begin "
                "carries".format(idea_ref))
    else:
        items = items_loader()
    idea_item = funnel.find(items, idea_ref)
    load_packet_bodies(items, idea_item)
    issue_comments = getattr(idea_item, "issue_comments", None)
    if issue_comments is None and items_loader is not None:
        # Pure packet fixtures and injected readers can omit the optional
        # thread; production reads always request it with the Project query.
        issue_comments = []
    if not isinstance(issue_comments, list):
        raise funnel.GitHubError(
            "could not read comments for {}#{}".format(resolved, idea_number)
        )
    inputs = shape_inputs(items, idea_item)
    plan_md, plan_md_missing = fetch_repo_text(resolved, "plan.md")
    agents_md, agents_md_missing = fetch_repo_text(resolved, "AGENTS.md")
    measured_at = now or datetime.now(timezone.utc)
    collected_at = measured_at.isoformat()
    if include_hotspot_routing and resolved == funnel.REPO:
        hotspots = measure_shape_hotspots(
            measured_at,
            items_loader=(lambda: items) if items_loader is not None else None)
    elif include_hotspot_routing:
        # The recorded Broken-fix join and its git history belong to this
        # repository; carrying those names into another repo would misroute.
        hotspots = []
    else:
        hotspots = None
    return build_packet(
        repo=resolved,
        idea=_idea_packet(idea_item),
        origin_voice=inputs.origin_voice,
        override_target=inputs.override_target,
        output_review=inputs.output_review,
        plan_md=plan_md,
        plan_md_missing=plan_md_missing,
        agents_md=agents_md,
        agents_md_missing=agents_md_missing,
        siblings=[_sibling_packet(row)
                  for row in sibling_plan_items(items, idea_item)],
        collected_at=collected_at,
        hotspots=hotspots,
        issue_comments=issue_comments,
    )


def packet_main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the shape packet for one idea as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only shape packet for an idea")
    parser.add_argument("idea", type=int, help="idea issue number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    parser.add_argument("--no-hotspot-routing", action="store_true",
                        help="omit runner-owned hotspot routing data")
    args = parser.parse_args(argv)
    try:
        packet = collect(args.repo, args.idea,
                         include_hotspot_routing=
                         not args.no_hotspot_routing)
    except funnel.GitHubError as exc:
        print("shape-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


def apply_shape(items: list, now: datetime, ref: str,
                answer_data: object,
                run: Optional[str] = None,
                agent: Optional[str] = None,
                voice: str = "agent", *,
                hotspots: Optional[Sequence[Dict[str, object]]] = None,
                include_failure_modes: bool = True,
                include_hotspot_routing: bool = True) -> int:
    """Validate one shape answer and record the plan it carries.

    Renders the issue body from the answer fields, applies the
    self-approval rule mechanically, writes Shaped or Ready, and posts
    the self-approval record as a code-owned comment on a Ready
    transition. Validation runs before the first write, and a fresh
    Project read refuses a stale apply before it can overwrite the issue.

    The open-question record comes from the needs_nate fields rather than a
    Needs-section parse. The origin override the decision honoured is
    carried over verbatim, with the Nate-voiced provenance that authorised
    an override to agents (``written_body``, #2138); Origin itself lives in
    the Project field. The written body, and the in-session item, read back
    to the decision through funnel's stored-body readers.

    Sequencing dependencies ride the body write as native blocked-by
    edges (#1053): one ``gh issue edit`` carries the rendered plan and
    the edges together, so a ref GitHub cannot resolve fails the whole
    write instead of recording a plan whose dependency is missing.
    """
    if voice not in ("agent", "nate-relayed"):
        raise ShapeError("shape provenance voice must be agent or nate-relayed")
    item = funnel.find(items, ref)
    # Validated, reviewed and decided once, before the body write below
    # replaces item.body (#2136), by the steps --validate-only runs; every
    # write below reads this record and nothing else decides (#2137).
    decision = shape_decision(
        items, item, answer_data, hotspots=hotspots,
        include_failure_modes=include_failure_modes,
        include_hotspot_routing=include_hotspot_routing)
    answer = decision.answer
    hotspot_routes: List[Dict[str, str]] = []
    if answer["hotspot_targets"]:
        # Routing may capture a missing Improve plan, so confirm the source
        # idea is still shapeable before the first route write.
        fresh_state, fresh_status, fresh_children = _read_fresh_shape_facts(item)
        stale_reason = funnel.unshapeable_reason(
            fresh_state, fresh_status, fresh_children)
        if stale_reason is not None:
            status_label = fresh_status if fresh_status is not None else "missing"
            print("{} ref={} reason={} fresh Status={} children={} "
                  "state={}".format(
                      SKIPPED_STALE_SHAPE_OUTCOME, item.ref, stale_reason,
                      status_label, fresh_children, fresh_state))
            return 0
        route_items = funnel.load_items(include_details=False)
        hotspot_routes = prepare_hotspot_routes(
            route_items, item.repo, answer["hotspot_targets"],
            _validate_packet_hotspots(hotspots or []), now)
        # Rebuild the rendered record with runner-resolved issue references.
        decision = decision_record(
            items, item, answer, hotspot_routes=hotspot_routes)
    status, reason = decision.status, decision.reason
    scan_only = decision.scan_only
    authority_signals = decision.authority_signals

    # The body reads back to this decision through funnel's stored-body
    # readers, or nothing is written (#2138).
    body = written_body(decision, item.body or "", voice=voice, now=now,
                        run=run, agent=agent)

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

    # The packet and decision may be minutes old. Re-read immediately before
    # the first issue mutation; a closed issue, a moved stage or a newly added
    # child makes this answer stale, by the picker's own predicate (#2139).
    # Body-only edits deliberately do not block the apply.
    fresh_state, fresh_status, fresh_children = _read_fresh_shape_facts(item)
    stale_reason = funnel.unshapeable_reason(
        fresh_state, fresh_status, fresh_children)
    if stale_reason is not None:
        status_label = fresh_status if fresh_status is not None else "missing"
        print("{} ref={} reason={} fresh Status={} children={} "
              "state={}".format(
                  SKIPPED_STALE_SHAPE_OUTCOME, item.ref, stale_reason,
                  status_label, fresh_children, fresh_state,
              ))
        return 0

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

    if hotspot_routes:
        post_hotspot_remainders(
            hotspot_routes, answer["redesign_remainder"], item, now,
            run=run, agent=agent)

    if not item.item_id:
        raise funnel.GitHubError("{} is not in the Project".format(item.ref))
    if decision.inputs.class_adopted:
        # This recovery write is the only place shaping may assign a
        # Class. Keep it immediately before the Status mutation so the
        # latter never makes an unclassed idea look like it advanced
        # cleanly.
        funnel.gh_graphql(
            funnel.SET_FIELD, project=funnel.PROJECT_ID,
            item=item.item_id, field=funnel.CLASS_FIELD_ID,
            option=funnel._option_id(funnel.CLASS_FIELD_ID,
                                     decision.inputs.klass))
        # A same-session reader, the Shaped sweep included, reads the Class
        # from this object, as it reads the body above (#2138).
        item.klass = decision.inputs.klass
    funnel.write_project_select(item.item_id, "Risk", decision.risk, item.ref)
    funnel.write_project_select(
        item.item_id, "Needs", decision.needs, item.ref)
    item.risk = decision.risk
    item.needs = decision.needs
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
        basis = "{}; {}".format(
            reason, "no declared risk" if scan_only else "no escalated risk")
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
    if scan_only:
        # Whatever the status, the scan's evidence is recorded where the
        # breakdown and review read it, since it no longer holds the plan.
        scan_comment = funnel._run_gh(
            ["gh", "issue", "comment", str(item.number),
             "--repo", item.repo,
             "--body", scan_escalation_comment(
                 scan_only, status, at=now, run=run, agent=agent)],
            capture_output=True, text=True,
        )
        if scan_comment.returncode != 0:
            raise funnel.GitHubError(scan_comment.stderr.strip())
    print("{} → {}\n{}".format(item.ref, status, item.url))
    if status == "Ready":
        print("advanced to Ready: {}".format(reason))
    else:
        print("held at Shaped: {}".format(reason))
    if scan_only:
        described = "; ".join(
            "{}: {}".format(entry["reason"], entry["line"])
            if entry.get("line") else entry["reason"]
            for entry in scan_only)
        print("escalation scan raised the review tier (the scan alone "
              "holds nothing): {}".format(described))
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
    parser.add_argument("--packet", default=None,
                        help="the shape packet used to validate hotspot targets")
    parser.add_argument("--run", default=None,
                        help="run id recorded in provenance blocks")
    parser.add_argument("--agent", default=None,
                        help="agent name recorded in provenance blocks")
    parser.add_argument("--voice", choices=("agent", "nate-relayed"),
                        default="agent",
                        help="provenance voice for the plan body")
    parser.add_argument("--attempt", type=int, default=None,
                        help="attempt number in the runner protocol: a "
                             "malformed answer exits 3 below attempt 2 "
                             "(the runner retries once) and 1 at 2 or "
                             "later. Without --attempt, exit 1.")
    parser.add_argument("--omit-failure-modes", action="store_true",
                        help="omit Review focus from the rendered plan")
    parser.add_argument("--no-hotspot-routing", action="store_true",
                        help="ignore hotspot fields and omit routing")
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
    packet_hotspots: Sequence[Dict[str, object]] = []
    if args.packet and not args.no_hotspot_routing:
        try:
            packet_data = json.loads(pathlib.Path(args.packet).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print("shape-apply: cannot read the shape packet: {}".format(exc),
                  file=sys.stderr)
            return 1
        if not isinstance(packet_data, dict):
            print("shape-apply: the shape packet must be a JSON object",
                  file=sys.stderr)
            return 1
        packet_hotspots = packet_data.get("hotspots", [])
        if not isinstance(packet_hotspots, list):
            print("shape-apply: packet hotspots must be a list",
                  file=sys.stderr)
            return 1
    try:
        # Only the exit code is decided here, before any read: a malformed
        # answer is retryable on the runner protocol. Both paths then run
        # ``shape_decision`` on the same data (#2137).
        data = validate_answer(
            data, include_failure_modes=not args.omit_failure_modes,
            hotspots=packet_hotspots,
            include_hotspot_routing=not args.no_hotspot_routing)
    except ShapeError as exc:
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return validation_exit(args.attempt)
    try:
        resolved = funnel.capture_repo(args.repo, args.run, args.agent)
        ref = "{}#{}".format(resolved, args.idea)
        # Shape validation reads the idea and its parent for effective Class.
        # Fetch those Project rows by ref; if either filter misses, keep the
        # history-free full-board behavior so the decision inputs stay exact.
        members = funnel.member_repos()
        items = funnel.load_project_items_by_refs(
            [ref], member_repo_names=members)
        if items is None or len(items) != 1 or items[0].ref != ref:
            items = funnel.load_items(include_details=False)
        else:
            parent_ref = items[0].parent
            if parent_ref and parent_ref not in {item.ref for item in items}:
                parent_items = funnel.load_project_items_by_refs(
                    [parent_ref], member_repo_names=members)
                if (parent_items is None or len(parent_items) != 1
                        or parent_items[0].ref != parent_ref):
                    items = funnel.load_items(include_details=False)
                else:
                    items.extend(parent_items)
        if args.validate_only:
            # The live path's own steps on the same answer data (#2137).
            decision = shape_decision(
                items, funnel.find(items, ref), data,
                hotspots=packet_hotspots,
                include_failure_modes=not args.omit_failure_modes,
                include_hotspot_routing=not args.no_hotspot_routing)
            print(json.dumps({"status": decision.status,
                              "reason": decision.reason,
                              "answer": decision.answer},
                             indent=2, sort_keys=True))
            return 0
        return apply_shape(
            items, datetime.now(timezone.utc), ref, data,
            run=args.run, agent=args.agent, voice=args.voice,
            hotspots=packet_hotspots,
            include_failure_modes=not args.omit_failure_modes,
            include_hotspot_routing=not args.no_hotspot_routing)
    except (ShapeError, funnel.GitHubError) as exc:
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return 1
