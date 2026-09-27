"""Pure classification rules for recorded Codex implementation declines."""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple


_DECLINED_ISSUE_REF = (
    r"(?P<ref>(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?"
    r"#[1-9][0-9]*)(?![A-Za-z0-9_])"
)
_DECLINED_PREREQUISITE_PATTERNS = (
    re.compile(
        r"\bprerequisite(?:\s+(?:ticket|issue))?\s+"
        + _DECLINED_ISSUE_REF,
        re.IGNORECASE,
    ),
    re.compile(
        _DECLINED_ISSUE_REF + r"\s+as\s+(?:(?:a|an)\s+)?prerequisite\b",
        re.IGNORECASE,
    ),
)

_DEFER_NOTE_REFERENCE = re.compile(r"\bdefer(?:red)?[- ]notes?\b", re.IGNORECASE)
_DEFER_NOTE_PERMISSION = re.compile(
    r"\b(?:explicit(?:ly)?|allow(?:s|ed)?|permit(?:s|ted)?|"
    r"accept(?:s|ed|able)?|valid|counts as)\b",
    re.IGNORECASE,
)
_DEFER_NOTE_PROOF = re.compile(r"\bproof\b", re.IGNORECASE)
_DEFER_NOTE_DENIALS = (
    re.compile(
        r"\bdefer(?:red)?[- ]notes?\b.{0,50}"
        r"\b(?:is|are|was|were)?\s*(?:not|never|cannot|can't)\b.{0,50}"
        r"\b(?:proof|allowed|permitted|accepted|acceptable|valid)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(?:not|never|cannot|can't|does\s+not|doesn't)\b.{0,50}"
        r"\b(?:allow|permit|accept)\b.{0,40}"
        r"\bdefer(?:red)?[- ]notes?\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\bnot\b.{0,50}"
        r"\b(?:allowed|permitted|accepted|acceptable|valid|proof)\b.{0,40}"
        r"\bdefer(?:red)?[- ]notes?\b",
        re.IGNORECASE | re.DOTALL,
    ),
)
_DEFER_NOTE_REASON = (
    re.compile(r"\bdefer(?:red)?\b", re.IGNORECASE),
    re.compile(r"\bseparate idea\b", re.IGNORECASE),
    re.compile(r"\bclose note\b", re.IGNORECASE),
    re.compile(
        r"\bno code\b.{0,100}\b(?:changed|written|made|added)\b",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _accepts_defer_note_proof(ticket_body: str) -> bool:
    """Require explicit permission in an Accept or Proof paragraph."""
    for match in re.finditer(
            r"\b(accept|proof)\s*:\s*(.*?)(?=\n\s*\n|\Z)",
            ticket_body or "", re.IGNORECASE | re.DOTALL):
        heading, paragraph = match.group(1).lower(), match.group(2)
        if not (_DEFER_NOTE_REFERENCE.search(paragraph)
                and _DEFER_NOTE_PERMISSION.search(paragraph)):
            continue
        if any(pattern.search(paragraph) for pattern in _DEFER_NOTE_DENIALS):
            continue
        if heading == "proof" or _DEFER_NOTE_PROOF.search(paragraph):
            return True
    return False


def _is_defer_note_proof_reason(reason: str) -> bool:
    """Recognize the written defer-note shape, including unchanged-code proof."""
    return all(pattern.search(reason or "") for pattern in _DEFER_NOTE_REASON)


DECLINE_REVIEW_ROUTING_MARKER = "<!-- command-center-review-routing -->"
_DECLINED_CONFLICT_POINTER = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:md|rst|py|yml|yaml|toml|json|txt))"
    r"(?P<anchor>(?:#(?:L[1-9][0-9]*|[A-Za-z0-9][A-Za-z0-9-]*))"
    r"|(?::L?[1-9][0-9]*))"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_DECLINED_CONFLICT_WORDS = (
    r"conflict(?:s|ed|ing)?|contradict(?:s|ed|ion|ory)?|"
    r"inconsisten(?:t|cy)|stale|outdated|superseded"
)
_DECLINED_ACCEPT_BODY_WORDS = (
    r"accept(?:ance)?|body|ticket|issue|plan|repo(?:sitory)?|rule(?:s)?"
)
_DECLINED_ACCEPT_BODY_CONFLICT = re.compile(
    r"(?:\b(?:" + _DECLINED_ACCEPT_BODY_WORDS + r")\b.{0,180}\b(?:"
    + _DECLINED_CONFLICT_WORDS + r")\b|\b(?:"
    + _DECLINED_CONFLICT_WORDS + r")\b.{0,180}\b(?:"
    + _DECLINED_ACCEPT_BODY_WORDS + r")\b)",
    re.IGNORECASE | re.DOTALL,
)
_DECLINED_STALE_ROUTINE_FREEZE = re.compile(
    r"(?P<issue>#[1-9][0-9]*)\s+routine\s+freeze"
    r"(?=.{0,240}\bverdict\s+confirms\s+the\s+conflict\b)",
    re.IGNORECASE | re.DOTALL,
)
_DECLINED_ACCEPTANCE_IMPOSSIBLE = re.compile(
    r"\b(?:accept|acceptance)(?:\s+(?:condition|criteria|requirement))?\b"
    r".{0,240}\b(?:cannot|can't|can not|impossible|unsatisfiable|"
    r"unachievable)\b.{0,80}\b(?:meet|met|satisfy|satisfied|clear|"
    r"fulfil|fulfilled|fulfill)\b",
    re.IGNORECASE | re.DOTALL,
)
_DECLINED_NO_CHANGE_CAN_CLEAR = re.compile(
    r"\bno\s+(?:in[- ]scope\s+)?change\s+can\s+(?:ever\s+)?"
    r"make\b.{0,100}\b(?:clear|meet|satisfy)\b",
    re.IGNORECASE | re.DOTALL,
)
_DECLINED_ACCEPTANCE_TERM = re.compile(
    r"\b(?:accept|acceptance)(?:\s+(?:condition|criteria|requirement))?\b",
    re.IGNORECASE,
)
_DECLINED_PENDING_GATE_ANSWER = re.compile(
    r"(?:\b(?:gate|question)\b.{0,100}\b(?:answer|response)\b"
    r".{0,120}\b(?:pending|awaiting|unanswered|not\s+yet\s+answered)\b"
    r"|\b(?:pending|awaiting|unanswered)\b.{0,120}\b(?:gate|question)\b"
    r".{0,100}\b(?:answer|response)\b)",
    re.IGNORECASE | re.DOTALL,
)
_DECLINED_GATE_ANSWER_ISSUE = re.compile(
    r"https?://github\.com/"
    r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/"
    r"(?P<number>[1-9][0-9]*)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _declined_conflict_pointer(reason: str) -> Optional[str]:
    """Return one specific repository text pointer from a body conflict."""
    text = reason or ""
    if _DECLINED_ACCEPT_BODY_CONFLICT.search(text):
        pointers = set()
        for match in _DECLINED_CONFLICT_POINTER.finditer(text):
            path = match.group("path")
            if path.startswith("/") or any(
                    part in (".", "..") for part in path.split("/")):
                continue
            pointers.add(path + match.group("anchor"))
        if len(pointers) == 1:
            return next(iter(pointers))

    # Preserve the known old routine-freeze shape without routing every plan
    # disagreement as a conflict.
    legacy = _DECLINED_STALE_ROUTINE_FREEZE.search(text)
    if legacy is not None:
        number = legacy.group("issue").lstrip("#")
        return "plan.md#routine-freeze-while-{}-lands".format(number)
    return None


def declined_review_routing_comment(reason: str, pointer: str) -> str:
    """Render the stable machine-readable handoff for an Accept conflict."""
    excerpt = (reason or "").strip()
    if len(excerpt) > 1200:
        excerpt = excerpt[:1197].rstrip() + "..."
    record = {
        "type": "accept-body-conflict",
        "decline_excerpt": excerpt,
        "conflict_pointer": pointer,
    }
    return "\n".join((
        "**Review routing: Accept/body conflict**",
        "",
        DECLINE_REVIEW_ROUTING_MARKER,
        "```json",
        json.dumps(record, ensure_ascii=False, indent=2),
        "```",
    ))


def declined_unsatisfiable_acceptance_comment(
        reason: str, acceptance_digest: str) -> str:
    """Render a durable route back to shaping until the acceptance changes."""
    excerpt = (reason or "").strip()
    if len(excerpt) > 1200:
        excerpt = excerpt[:1197].rstrip() + "..."
    record = {
        "type": "unsatisfiable-acceptance",
        "decline_excerpt": excerpt,
        "acceptance_digest": acceptance_digest,
    }
    return "\n".join((
        "**Review routing: Unsatisfiable acceptance**",
        "",
        DECLINE_REVIEW_ROUTING_MARKER,
        "```json",
        json.dumps(record, ensure_ascii=False, indent=2),
        "```",
    ))


def declined_pending_gate_answer_comment(reason: str, gate_ref: str) -> str:
    """Render a durable route that waits for one named gate answer."""
    excerpt = (reason or "").strip()
    if len(excerpt) > 1200:
        excerpt = excerpt[:1197].rstrip() + "..."
    record = {
        "type": "pending-gate-answer",
        "decline_excerpt": excerpt,
        "gate_ref": gate_ref,
    }
    return "\n".join((
        "**Review routing: Pending gate answer**",
        "",
        DECLINE_REVIEW_ROUTING_MARKER,
        "```json",
        json.dumps(record, ensure_ascii=False, indent=2),
        "```",
    ))


def _declined_gate_answer_ref(reason: str) -> Optional[str]:
    """Return one unambiguous issue named by a pending-gate decline."""
    refs = {
        "{}#{}".format(match.group("repo"), match.group("number"))
        for match in _DECLINED_GATE_ANSWER_ISSUE.finditer(reason or "")
    }
    return next(iter(refs)) if len(refs) == 1 else None


def classify_decline_reason(reason: str, ticket_repo: str,
                            ticket_body: str = ""
                            ) -> Tuple[str, Optional[str]]:
    """Classify machine-actionable declines without guessing from mentions."""
    found = {
        match.group("ref")
        for pattern in _DECLINED_PREREQUISITE_PATTERNS
        for match in pattern.finditer(reason or "")
    }
    if found:
        if len(found) != 1:
            pointer = _declined_conflict_pointer(reason)
            return (("accept-body-conflict", pointer)
                    if pointer else ("unknown", None))
        raw_ref = next(iter(found))
        match = re.fullmatch(
            r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?"
            r"#(?P<number>[1-9][0-9]*)",
            raw_ref,
        )
        if match is None:
            pointer = _declined_conflict_pointer(reason)
            return (("accept-body-conflict", pointer)
                    if pointer else ("unknown", None))
        repo = match.group("repo") or ticket_repo
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
            pointer = _declined_conflict_pointer(reason)
            return (("accept-body-conflict", pointer)
                    if pointer else ("unknown", None))
        ref = "{}#{}".format(repo, match.group("number"))
        if re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", ref
        ) is None:
            pointer = _declined_conflict_pointer(reason)
            return (("accept-body-conflict", pointer)
                    if pointer else ("unknown", None))
        return "prerequisite-ticket", ref

    pointer = _declined_conflict_pointer(reason)
    if pointer is not None:
        return "accept-body-conflict", pointer
    if _DECLINED_PENDING_GATE_ANSWER.search(reason or ""):
        gate_ref = _declined_gate_answer_ref(reason)
        if gate_ref is not None:
            return "pending-gate-answer", gate_ref
        return "unknown", None
    if (_DECLINED_ACCEPTANCE_IMPOSSIBLE.search(reason or "")
            or (_DECLINED_ACCEPTANCE_TERM.search(reason or "")
                and _DECLINED_NO_CHANGE_CAN_CLEAR.search(reason or ""))):
        return "unsatisfiable-acceptance", None
    if (_is_defer_note_proof_reason(reason)
            and _accepts_defer_note_proof(ticket_body)):
        return "defer-note-proof", None
    return "unknown", None
