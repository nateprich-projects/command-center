#!/usr/bin/env python3
"""Function-grouped scope evidence for the per-PR unrequested count.

Ticket #2027, plan #1840 phase 4: the runner groups the PR diff's hunks
by containing function within each file and emits one scope row per
group as review evidence. Rows render as ``- `` fact lines, the shape the
#1783 evidence path reads (``engine.review.packet_evidence``), which makes
scope the first real consumer of that path.

The judge maps each group to a requirement ref or ``unrequested``. The
mapping is completeness-checked exactly once per group, the way
requirement rows are in ``engine.review.parse_judge_answer``: every group
mapped exactly once, no dropped or invented groups. A malformed mapping
raises ``ScopeMappingError``. That error fails the evidence check, never
the review: the runner skips mapping for the PR and records a deferred
outcomes entry with the reason instead of blocking.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from engine.review import DIFF_LIMIT_BYTES, DIFF_TOO_LARGE_REASON
from engine import review


#: The judge's target for a scope group no requirement asked for. Matched
#: exactly, the way ``parse_judge_answer`` matches requirement names.
UNREQUESTED = "unrequested"

#: Rendered function for hunks outside any function. All such hunks in one
#: file form a single group, so the completeness check stays bounded.
FILE_SCOPE_LABEL = "<file scope>"


class ScopeMappingError(ValueError):
    """A judge scope mapping could not be tied safely to its groups."""


_SCOPE_DEF_RE = re.compile(
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"[ \t]*\(")
_SCOPE_CLASS_RE = re.compile(
    r"^[ \t]*class[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:[ \t]*[(:]|$)")
_SCOPE_HEADING_DEF_RE = re.compile(
    r"(?:async[ \t]+)?def[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\(")
_SCOPE_HEADING_CLASS_RE = re.compile(
    r"class[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def _hunk_headings(diff: str) -> List[str]:
    """The ``@@`` section heading of every hunk, in diff order.

    Runs alongside ``engine.review._diff_sections``: both passes key on
    the same two line prefixes in a single order, so the nth heading here
    belongs to the nth hunk there. The structure (paths, hunk bounds)
    stays owned by that parser; this pass only recovers the heading text
    it discards.
    """
    headings: List[str] = []
    in_file = False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            in_file = True
            continue
        if in_file and line.startswith("@@"):
            parts = line.split("@@", 2)
            headings.append(parts[2].strip() if len(parts) > 2 else "")
    return headings


def _defined_name(added: Sequence[str],
                  removed: Sequence[str]) -> Optional[str]:
    """The first function or class a hunk defines, added side first.

    A hunk that introduces a function belongs to it even when git's
    ``@@`` heading still names the preceding function, so definitions
    outrank headings. Added lines come first because a rename's new name
    is the group's post-image identity.
    """
    for line in list(added) + list(removed):
        match = _SCOPE_DEF_RE.match(line)
        if match is not None:
            return match.group("name")
        match = _SCOPE_CLASS_RE.match(line)
        if match is not None:
            return match.group("name")
    return None


def _heading_function(heading: str) -> Optional[str]:
    """The containing function git's ``@@`` heading names, if any.

    ``def``/``class`` headings normalize to the bare name so hunks stay
    grouped when a signature changes mid-diff. Any other heading is kept
    verbatim: it still bounds the judge's output for languages whose
    headings carry no Python definition.
    """
    text = (heading or "").strip()
    if not text:
        return None
    match = _SCOPE_HEADING_DEF_RE.search(text)
    if match is not None:
        return match.group("name")
    match = _SCOPE_HEADING_CLASS_RE.search(text)
    if match is not None:
        return match.group("name")
    return text


def scope_group_id(file: str, function: Optional[str]) -> str:
    """The stable identity of one scope group within a diff."""
    return "{}::{}".format(file, function or FILE_SCOPE_LABEL)


def scope_groups(diff: str) -> List[Dict[str, Any]]:
    """Group every diff hunk under exactly one scope group.

    One group per (file, function) pair in first-appearance order. A hunk
    defining a function groups under it; otherwise git's ``@@`` heading
    names the containing function; hunks outside any function form one
    file-scope group per file. Each row carries its stable identity plus
    the hunk span, so the judge's mapping answer is completeness-checked
    the way requirement rows are. ``hunks`` holds ``[section, hunk]``
    pairs into ``_diff_sections`` order, JSON-safe by construction.
    """
    sections = review._diff_sections(diff)
    headings = _hunk_headings(diff or "")
    hunk_count = sum(len(section["hunks"]) for section in sections)
    if len(headings) != hunk_count:
        raise ValueError(
            "scope pass counted {} headings for {} hunks".format(
                len(headings), hunk_count))
    groups: List[Dict[str, Any]] = []
    index: Dict[Tuple[str, Optional[str]], int] = {}
    position = 0
    for section_index, section in enumerate(sections):
        path = section.get("new_path") or section.get("old_path") or ""
        for hunk_index, hunk in enumerate(section["hunks"]):
            heading = headings[position]
            position += 1
            function = _defined_name(hunk["added"], hunk["removed"])
            if function is None:
                function = _heading_function(heading)
            key = (path, function)
            slot = index.get(key)
            if slot is None:
                slot = len(groups)
                index[key] = slot
                groups.append({
                    "id": scope_group_id(path, function),
                    "file": path,
                    "function": function,
                    "hunks": [],
                })
            groups[slot]["hunks"].append([section_index, hunk_index])
    return groups


def render_scope_row(group: Mapping[str, Any]) -> str:
    """One scope row as a ``- `` fact line for the #1783 evidence shape."""
    spans = ", ".join("{}.{}".format(section, hunk)
                      for section, hunk in group["hunks"])
    return "- scope: {} (hunks {})".format(group["id"], spans)


def render_scope_rows(groups: Sequence[Mapping[str, Any]]) -> List[str]:
    """Every scope group rendered as review evidence, in group order."""
    return [render_scope_row(group) for group in groups]


def _canonical_group_ids(
        expected_groups: Sequence[Any]) -> List[str]:
    """The group ids a mapping answer must cover exactly once."""
    if (isinstance(expected_groups, (str, bytes))
            or not isinstance(expected_groups, (list, tuple))):
        raise ScopeMappingError("expected groups must be a list")
    ids = []
    for index, group in enumerate(expected_groups):
        if isinstance(group, Mapping):
            value = group.get("id")
        elif isinstance(group, str):
            value = group
        else:
            value = None
        if not isinstance(value, str) or not value.strip():
            raise ScopeMappingError(
                "expected group {} must carry a non-empty id".format(index))
        ids.append(value.strip())
    if len(set(ids)) != len(ids):
        raise ScopeMappingError("expected groups must have distinct ids")
    return ids


def parse_scope_mapping(raw: str,
                        expected_groups: Sequence[Any]
                        ) -> List[Dict[str, str]]:
    """Validate one judge's scope mapping and return it in group order.

    The answer sends each scope group to a requirement ref or
    ``unrequested``. A group mapped zero times or twice, an invented
    group, or a malformed entry makes the whole answer unusable: the
    caller records a deferred outcomes entry, never a review block.
    """
    expected = _canonical_group_ids(expected_groups)
    if not expected:
        raise ScopeMappingError("scope mapping needs at least one group")
    if not (raw or "").strip():
        raise ScopeMappingError("empty answer: expected a JSON object")
    try:
        answer = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ScopeMappingError("invalid JSON: {}".format(exc))
    if not isinstance(answer, dict):
        raise ScopeMappingError("answer must be a JSON object")
    if set(answer) != {"scope"}:
        raise ScopeMappingError("answer must contain only the 'scope' key")
    entries = answer["scope"]
    if not isinstance(entries, list):
        raise ScopeMappingError("'scope' must be a list")
    shaped = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {
                "group", "requirement", "evidence"}:
            raise ScopeMappingError(
                "'scope'[{}] must contain group, requirement, and "
                "evidence only".format(index))
        group = entry["group"]
        requirement = entry["requirement"]
        evidence = entry["evidence"]
        if not isinstance(group, str) or not group.strip():
            raise ScopeMappingError(
                "'scope'[{}].group must be a non-empty string".format(index))
        if not isinstance(requirement, str) or not requirement.strip():
            raise ScopeMappingError(
                "'scope'[{}].requirement must be a non-empty string".format(
                    index))
        if not isinstance(evidence, str) or not evidence.strip():
            raise ScopeMappingError(
                "'scope'[{}].evidence must be a non-empty string".format(
                    index))
        shaped.append({"group": group.strip(),
                       "requirement": requirement.strip(),
                       "evidence": evidence.strip()})

    if len(shaped) != len(expected):
        got = [entry["group"] for entry in shaped]
        raise ScopeMappingError(
            "judge mapped {} scope group(s) for {} assigned: "
            "missing {}; unassigned {}".format(
                len(shaped), len(expected),
                sorted(set(expected) - set(got)),
                sorted(set(got) - set(expected))))
    remaining = list(shaped)
    ordered = []
    for group_id in expected:
        matches = [entry for entry in remaining
                   if entry["group"] == group_id]
        if len(matches) != 1:
            raise ScopeMappingError(
                "scope group must be mapped exactly once: {!r}".format(
                    group_id))
        entry = matches[0]
        remaining.remove(entry)
        ordered.append(entry)
    if remaining:
        raise ScopeMappingError(
            "judge mapped an unassigned scope group: {!r}".format(
                remaining[0]["group"]))
    return ordered


def count_unrequested(entries: Sequence[Mapping[str, Any]]) -> int:
    """How many mapped scope groups no requirement asked for.

    One per group mapped to ``unrequested``: the judge answers one
    mapping per scope group and completeness is checked per group, so
    per-group keeps evidence, check, and outcomes aligned.
    """
    return sum(1 for entry in entries
               if isinstance(entry, Mapping)
               and entry.get("requirement") == UNREQUESTED)


def scope_skip_reason(diff: object) -> Optional[str]:
    """Why scope mapping must be skipped for this diff, or None to map it.

    An oversized diff reuses the review's settled size cap: the judge
    cannot map what does not fit, and a partial count would corrupt the
    per-PR measurement. A diff with no mappable hunks (empty, unreadable,
    or naming files without showing any) defers the same way. Either case
    records a deferred outcomes entry with the reason and never blocks.
    """
    if not isinstance(diff, str) or not diff.strip():
        return "diff unavailable: nothing to map"
    if len(diff.encode("utf-8")) > DIFF_LIMIT_BYTES:
        return DIFF_TOO_LARGE_REASON
    if not scope_groups(diff):
        return "diff has no mappable hunks"
    return None
