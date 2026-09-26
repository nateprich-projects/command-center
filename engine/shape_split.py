#!/usr/bin/env python3
"""Build one shape answer from small parts (#1598, plan #1581).

One max call over the whole shape packet goes silent on the hardest
ideas, and #1233 measured that difficulty, not size, drives the silence:
trimming plan.md alone completed none of three. Review answered that by
splitting the question, not the evidence (``engine/review.py``); this
module does the same for shaping. The packet is still fetched once; each
part sees only the slice its question needs:

1. The framer drafts the plan with sibling bodies replaced by an index,
   and lists the decision points it leaves open. It judges nothing about
   who decides.
2. Sibling checks read at most three full sibling plans each and say how
   each one relates to the draft.
3. Deciders take at most three decision points each and settle every one
   from precedent, as the agent, or as one question for Nate.
4. The auditor records the draft's premises and escalated risk.

The parts are merged here, in code, into exactly ``engine.shape``'s
``ANSWER_KEYS``, so ``shape-apply`` and its self-approval rule are
unchanged. Every parser reuses the shape validators and raises
``ShapeError``, the error the runner already feeds back to the model on
its one retry. The #1581 premise that the framer itself may still go silent
remains unsure until #1600 posts live run evidence; this pure module and its
tests do not establish runtime stream behavior. Pure: no IO, no subprocess,
no network.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402
from engine.shape import (  # noqa: E402
    ANSWER_KEYS,
    NEEDS_FIELDS,
    REF_RE,
    ShapeError,
    _check_keys,
    _require_line,
    _require_text,
    _validate_agent_decisions,
    _validate_depends_on,
    _validate_escalated_risk,
    _validate_investigate_possible_defect,
    _validate_precedent,
    _validate_premises,
    validate_answer,
)

#: Sibling plans one sibling-check call reads in full. Fourteen plans of
#: 15-18K characters were 60% of the #1195 packet; three keeps each call
#: in the size review's judges finish at (#1233).
MAX_SIBLINGS_PER_CALL = 3

#: Decision points one decider call settles, matching review's
#: ``MAX_JUDGE_REQUIREMENTS``.
MAX_DECISION_POINTS_PER_CALL = 3

#: How a sibling plan relates to the framer's draft. ``depends_on`` joins
#: the answer's ``depends_on``; ``overlaps`` becomes one Scope question.
SIBLING_RELATIONS = ("independent", "depends_on", "overlaps")

#: How a decider settles one decision point, and the fields each kind
#: carries beside ``point`` and ``kind``. The field sets are the shape
#: answer's own: a precedent entry, an agent decision, a Nate question.
DECISION_KINDS = {
    "precedent": ("claim", "source"),
    "agent": ("decision", "alternative", "why"),
    "nate": ("category", "question"),
}

#: The framer's answer keys — exactly these, no extras.
FRAMER_KEYS = ("proposed_class", "plan_markdown", "decision_points",
               "depends_on")

#: The auditor's answer keys — exactly these, no extras.
AUDITOR_KEYS = ("premises", "escalated_risk")

#: The heading code appends to the plan narrative. The framer never
#: writes it: the section is rendered from the sibling checks.
SIBLINGS_HEADING = "## Siblings checked"

_SIBLINGS_HEADING_RE = re.compile(
    r"^\s*##\s+Siblings checked\s*$", re.IGNORECASE | re.MULTILINE)

#: The packet fields every part may read about the idea itself.
_IDEA_FIELDS = ("repo", "idea", "origin", "collected_at")

#: The repository's rules, which only the framer and the deciders read.
_RULE_FIELDS = ("plan_md", "plan_md_missing", "agents_md",
                "agents_md_missing")


# -- chunking ------------------------------------------------------------

def _chunks(items: Sequence, size: int) -> List[list]:
    """Split items into consecutive groups of at most ``size``."""
    return [list(items[start:start + size])
            for start in range(0, len(items), size)]


def _decision_point_list(points: object, where: str) -> List[str]:
    """A non-empty list of distinct one-line decision points.

    Distinct because a decider answers each point exactly once: two
    identical points could not be told apart in its answer.
    """
    if not isinstance(points, list) or not points:
        raise ShapeError("{} must be a non-empty list".format(where))
    validated = []
    for index, point in enumerate(points):
        line = _require_line(point, "{}[{}]".format(where, index))
        if line in validated:
            raise ShapeError(
                "{}[{}] repeats the decision point {!r}".format(
                    where, index, line))
        validated.append(line)
    return validated


def _sibling_index(packet: Dict) -> List[Dict]:
    """The sibling plans without their bodies: ref, title, status, class."""
    return [{"ref": row.get("ref"), "title": row.get("title"),
             "status": row.get("status"), "klass": row.get("klass")}
            for row in packet.get("sibling_plans") or []]


def _base_packet(packet: Dict, draft: Optional[Dict] = None) -> Dict:
    """The idea, its origin and thread, and the framer draft when given."""
    part = {key: packet[key] for key in _IDEA_FIELDS if key in packet}
    if "issue_thread" in packet:
        part["issue_thread"] = packet["issue_thread"]
    if draft is not None:
        part["draft"] = dict(draft)
    return part


# -- packets -------------------------------------------------------------

def framer_packet(packet: Dict) -> Dict:
    """The framer's packet: the whole shape packet, sibling bodies out.

    Sibling plans become ``sibling_index``: their ref, title, status and
    class, enough to name a dependency without reading the plan. The
    sibling checks read the bodies instead.
    """
    framed = {key: value for key, value in packet.items()
              if key != "sibling_plans"}
    framed["sibling_index"] = _sibling_index(packet)
    return framed


def sibling_packets(packet: Dict, draft: Dict) -> List[Dict]:
    """One packet per sibling-check call, at most three siblings each.

    Each carries the idea, origin, thread and the framer draft, plus the
    full bodies of its own siblings only; the repository rules stay out,
    since relating two plans needs neither. No siblings, no calls.
    """
    rows = [dict(row) for row in packet.get("sibling_plans") or []]
    packets = []
    for chunk in _chunks(rows, MAX_SIBLINGS_PER_CALL):
        part = _base_packet(packet, draft)
        part["sibling_plans"] = chunk
        packets.append(part)
    return packets


def decider_packets(packet: Dict, draft: Dict,
                    points: Sequence[str]) -> List[Dict]:
    """One packet per decider call, at most three decision points each.

    Each carries the repository rules, the idea, origin, thread, the
    framer draft and the sibling index, plus its own decision points.
    The output-review policy rides along when the shape packet carries
    it, since the deciders are the ones who ask Nate. An empty point list
    is refused: a framer that leaves nothing open has failed.
    """
    canonical = _decision_point_list(list(points or []), "decision_points")
    packets = []
    for chunk in _chunks(canonical, MAX_DECISION_POINTS_PER_CALL):
        part = _base_packet(packet, draft)
        for key in _RULE_FIELDS:
            if key in packet:
                part[key] = packet[key]
        part["sibling_index"] = _sibling_index(packet)
        if "output_review" in packet:
            part["output_review"] = dict(packet["output_review"])
        part["decision_points"] = chunk
        packets.append(part)
    return packets


def auditor_packet(packet: Dict, draft: Dict) -> Dict:
    """The auditor's packet: the idea, origin, thread and the draft.

    The output-review policy rides along when present: its escalated-risk
    rule is the auditor's to follow.
    """
    part = _base_packet(packet, draft)
    if "output_review" in packet:
        part["output_review"] = dict(packet["output_review"])
    return part


# -- parsers -------------------------------------------------------------

def _decode(answer: object, where: str) -> Dict:
    """Decode a raw JSON answer, or take an already decoded object."""
    if isinstance(answer, (str, bytes)):
        if not answer.strip():
            raise ShapeError(
                "the {} answer is empty: expected a JSON object".format(where))
        try:
            answer = json.loads(answer)
        except ValueError as exc:
            raise ShapeError(
                "the {} answer is not valid JSON: {}".format(where, exc))
    if not isinstance(answer, dict):
        raise ShapeError("the {} answer must be a JSON object".format(where))
    return answer


def _assigned(values: Sequence[str], limit: int, where: str) -> List[str]:
    """Validate the items one call was assigned: 1..limit distinct lines."""
    canonical = _decision_point_list(list(values or []), where)
    if len(canonical) > limit:
        raise ShapeError("one call may be assigned at most {} {}".format(
            limit, where))
    return canonical


def _exactly_once(entries: List[Dict], key: str,
                  assigned: Sequence[str], where: str) -> List[Dict]:
    """Return entries in assigned order, each assigned value exactly once.

    Missing, duplicated or unassigned entries refuse the whole answer, as
    ``engine.review.parse_judge_answer`` does, so the runner retries the
    call once with the error fed back.
    """
    ordered = []
    for value in assigned:
        matches = [entry for entry in entries if entry[key] == value]
        if not matches:
            raise ShapeError("{} does not answer {!r}".format(where, value))
        if len(matches) > 1:
            raise ShapeError("{} answers {!r} {} times; answer it exactly "
                             "once".format(where, value, len(matches)))
        ordered.append(matches[0])
    extra = [entry[key] for entry in entries if entry[key] not in assigned]
    if extra:
        raise ShapeError("{} answers unassigned {}: {}".format(
            where, key, ", ".join(repr(value) for value in extra)))
    return ordered


def parse_framer(answer: object) -> Dict:
    """Validate the framer's draft.

    ``proposed_class`` is a ladder class, ``plan_markdown`` a non-empty
    narrative (an Investigate draft carries its one Possible defect line),
    ``decision_points`` a non-empty list of distinct one-line points, and
    ``depends_on`` owner/repo#n refs. The narrative may not write the
    Siblings checked section: code renders it from the sibling checks.
    """
    data = _decode(answer, "framer")
    _check_keys(data, FRAMER_KEYS, "the framer answer")
    proposed = _require_line(data["proposed_class"], "proposed_class")
    if proposed not in funnel.LADDER:
        raise ShapeError(
            "proposed_class {!r} is not a ladder class; choose one of "
            "{}".format(proposed, ", ".join(funnel.LADDER)))
    plan_markdown = _require_text(data["plan_markdown"], "plan_markdown")
    if proposed == "Investigate":
        _validate_investigate_possible_defect(plan_markdown)
    if _SIBLINGS_HEADING_RE.search(plan_markdown):
        raise ShapeError(
            "plan_markdown must not carry a '{}' section; the sibling "
            "checks render it".format(SIBLINGS_HEADING))
    return {
        "proposed_class": proposed,
        "plan_markdown": plan_markdown,
        "decision_points": _decision_point_list(
            data["decision_points"], "decision_points"),
        "depends_on": _validate_depends_on(data["depends_on"]),
    }


def parse_siblings(answer: object,
                   assigned_refs: Sequence[str]) -> List[Dict[str, str]]:
    """Validate one sibling check, returned in assigned-ref order.

    ``{"siblings": [{"ref", "relation", "why"}]}`` with exactly one entry
    per assigned ref, the relation from ``SIBLING_RELATIONS``, and the
    why one non-empty line.
    """
    assigned = _assigned(assigned_refs, MAX_SIBLINGS_PER_CALL, "siblings")
    data = _decode(answer, "sibling check")
    _check_keys(data, ("siblings",), "the sibling check answer")
    entries = data["siblings"]
    if not isinstance(entries, list):
        raise ShapeError("siblings must be a list")
    shaped = []
    for index, entry in enumerate(entries):
        where = "siblings[{}]".format(index)
        _check_keys(entry, ("ref", "relation", "why"), where)
        relation = _require_line(entry["relation"], where + ".relation")
        if relation not in SIBLING_RELATIONS:
            raise ShapeError("{}.relation must be one of {}".format(
                where, ", ".join(SIBLING_RELATIONS)))
        shaped.append({
            "ref": _require_line(entry["ref"], where + ".ref"),
            "relation": relation,
            "why": _require_line(entry["why"], where + ".why"),
        })
    return _exactly_once(shaped, "ref", assigned, "the sibling check")


def _parse_decision(entry: object, where: str) -> Dict[str, str]:
    """Validate one decided point through the shape answer's validators."""
    if not isinstance(entry, dict):
        raise ShapeError("{} must be an object".format(where))
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in DECISION_KINDS:
        raise ShapeError("{}.kind must be one of {}".format(
            where, ", ".join(DECISION_KINDS)))
    fields = DECISION_KINDS[kind]
    _check_keys(entry, ("point", "kind") + fields, where)
    decided = {"point": _require_line(entry["point"], where + ".point"),
               "kind": kind}
    body = {field: entry[field] for field in fields}
    if kind == "precedent":
        decided.update(_validate_precedent([body])[0])
    elif kind == "agent":
        decided.update(_validate_agent_decisions([body])[0])
    else:
        categories = [field for field, _ in NEEDS_FIELDS]
        category = _require_line(body["category"], where + ".category")
        if category not in categories:
            raise ShapeError("{}.category must be one of {}".format(
                where, ", ".join(categories)))
        decided["category"] = category
        decided["question"] = _require_line(
            body["question"], where + ".question")
    return decided


def parse_decider(answer: object,
                  assigned_points: Sequence[str]) -> List[Dict[str, str]]:
    """Validate one decider, returned in assigned-point order.

    ``{"decisions": [...]}`` with exactly one entry per assigned point.
    Each entry is ``{"point", "kind"}`` plus the kind's fields: a
    ``precedent`` carries ``claim`` and ``source``; an ``agent`` decision
    ``decision``, ``alternative`` and ``why``; a ``nate`` question a
    ``category`` (exposure, gates, scope, preference) and ``question``.
    """
    assigned = _assigned(assigned_points, MAX_DECISION_POINTS_PER_CALL,
                         "decision_points")
    data = _decode(answer, "decider")
    _check_keys(data, ("decisions",), "the decider answer")
    entries = data["decisions"]
    if not isinstance(entries, list):
        raise ShapeError("decisions must be a list")
    shaped = [_parse_decision(entry, "decisions[{}]".format(index))
              for index, entry in enumerate(entries)]
    return _exactly_once(shaped, "point", assigned, "the decider")


def parse_auditor(answer: object) -> Dict[str, List[Dict[str, str]]]:
    """Validate the auditor's premises and escalated-risk declaration."""
    data = _decode(answer, "auditor")
    _check_keys(data, AUDITOR_KEYS, "the auditor answer")
    return {
        "premises": _validate_premises(data["premises"]),
        "escalated_risk": _validate_escalated_risk(data["escalated_risk"]),
    }


# -- merge ---------------------------------------------------------------

def overlap_question(ref: str, why: str) -> str:
    """The one Scope question an overlapping sibling becomes.

    Concrete by construction: it names the sibling and asks whether this
    plan includes the shared work, so the agent-origin output review
    keeps it open rather than reading it as generic permission.
    """
    return ("{} overlaps this plan ({}): should this plan include the "
            "shared work, or leave it to {}?".format(
                ref, why.rstrip(" ."), ref))


def siblings_section(siblings: Sequence[Dict[str, str]]) -> str:
    """Render the Siblings checked section from the sibling checks."""
    lines = [SIBLINGS_HEADING, ""]
    if siblings:
        lines.extend("- {} ({}): {}".format(
            entry["ref"], entry["relation"], entry["why"])
            for entry in siblings)
    else:
        lines.append("No open sibling plans.")
    return "\n".join(lines)


def merge_shape_answer(framer: Dict,
                       siblings: Sequence[Dict[str, str]],
                       decisions: Sequence[Dict[str, str]],
                       audit: Dict) -> Dict:
    """Build the shape answer from the parsed parts. Pure: no IO.

    Takes ``parse_framer``'s draft, every sibling check's entries and
    every decider's entries concatenated in call order, and
    ``parse_auditor``'s record. Returns a dict with exactly
    ``engine.shape.ANSWER_KEYS``, validated by ``validate_answer``:

    - precedent decisions become ``decided_from_precedent`` and agent
      decisions ``decided_by_agent``, in decision-point order;
    - Nate questions land under their ``needs_nate`` category; a
      sibling marked ``overlaps`` adds one Scope question naming it;
      a category with no question is null;
    - ``depends_on`` is the framer's refs, then each sibling marked
      ``depends_on``, deduplicated in that order;
    - the Siblings checked section is appended to the narrative, saying
      "No open sibling plans" when there were none.

    The decisions must answer the framer's decision points exactly once
    each, and a sibling may be reported only once: a part lost or
    doubled between the calls and the merge fails closed.
    """
    points = framer["decision_points"]
    ordered = _exactly_once(list(decisions), "point", points,
                            "the merged decisions")
    seen = set()
    for entry in siblings:
        if entry["ref"] in seen:
            raise ShapeError(
                "sibling {} is reported more than once".format(entry["ref"]))
        seen.add(entry["ref"])

    precedent = []
    by_agent = []
    needs: Dict[str, List[str]] = {field: [] for field, _ in NEEDS_FIELDS}
    for entry in ordered:
        if entry["kind"] == "precedent":
            precedent.append({"claim": entry["claim"],
                              "source": entry["source"]})
        elif entry["kind"] == "agent":
            by_agent.append({"decision": entry["decision"],
                             "alternative": entry["alternative"],
                             "why": entry["why"]})
        else:
            needs[entry["category"]].append(entry["question"])
    needs["scope"].extend(overlap_question(entry["ref"], entry["why"])
                          for entry in siblings
                          if entry["relation"] == "overlaps")

    depends_on = list(framer["depends_on"])
    for entry in siblings:
        if entry["relation"] == "depends_on":
            if not REF_RE.match(entry["ref"]):
                raise ShapeError(
                    "sibling {!r} is not an owner/repo#n ref".format(
                        entry["ref"]))
            depends_on.append(entry["ref"])

    plan_markdown = "{}\n\n{}\n".format(
        framer["plan_markdown"].rstrip(), siblings_section(siblings))
    merged = {
        "decided_from_precedent": precedent,
        "decided_by_agent": by_agent,
        "needs_nate": {field: (questions or None)
                       for field, questions in needs.items()},
        "proposed_class": framer["proposed_class"],
        "plan_markdown": plan_markdown,
        "escalated_risk": list(audit["escalated_risk"]),
        "depends_on": list(dict.fromkeys(depends_on)),
        "premises": list(audit["premises"]),
    }
    assert set(merged) == ANSWER_KEYS
    return validate_answer(merged)
