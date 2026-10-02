"""block_record.py — the one owner of block, decision and decline headers.

A blocked ticket or project explains itself in an issue comment whose first
line is one of four fixed headers: ``**Blocked[ until DATE][ on #N]:**``,
``**Blocked until event:**`` with a fenced JSON spec, ``**Needs a
decision:**`` and ``**Declined:**``. This module owns those shapes, the
parsers that read one comment body (or a list of bodies) and one renderer
per kind, so a writer and the reader cannot drift apart (#1748, #2165).

It imports neither funnel nor engine. funnel.py must never import engine
(``engine/__init__.py``), and the engine already imports funnel, so the
shape lives here and both import it, as they do ``decline_classifier.py``.
funnel.py re-exports every name under its old spelling.

funnel.py keeps the row-level work: which comment rows are trusted, which
one is current by ``createdAt`` (``_current_block_comment_details``) and the
provenance-trailer strip. Comments already on GitHub are read as before;
nothing here rewrites one.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

#: A blocked comment names an optional, knowable condition after this marker.
#: The parser below owns the rest of the fixed header shape.
BLOCK_COMMENT_PREFIX = "**Blocked"
BLOCK_COMMENT_RE = re.compile(
    r"\A" + re.escape(BLOCK_COMMENT_PREFIX)
    + r"(?: until (?P<blocked_until>[0-9]{4}-[0-9]{2}-[0-9]{2}))?"
      r"(?: on (?P<references>#[0-9]+(?: and #[0-9]+)*))?:\*\*"
)

#: A blocked ticket may wait on the one event kind the queue understands.
#: The body after this header must be a strict fenced JSON spec.
BLOCK_EVENT_COMMENT_PREFIX = "**Blocked until event:**"
BLOCK_EVENT_COMMENT_RE = re.compile(
    r"\A" + re.escape(BLOCK_EVENT_COMMENT_PREFIX)
    + r"[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*```json[ \t]*\r?\n"
      r"(?P<event_spec>.*?)\r?\n[ \t]*```[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)
BLOCK_EVENT_KIND_HEADER_RE = re.compile(
    r"\A\*\*Blocked until (?P<kind>[^:\r\n]+):\*\*"
)
BLOCK_FENCED_PAYLOAD_RE = re.compile(
    r"\A[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*```[^\r\n]*\r?\n"
    r".*?\r?\n[ \t]*```[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)

#: A breakdown can leave a project waiting on Nate's answer. The header is
#: deliberately strict and anchored just like the ordinary block header so a
#: quoted or embedded sentence cannot become a gate question by accident.
NEEDS_DECISION_PREFIX = "**Needs a decision:**"
NEEDS_DECISION_RE = re.compile(
    r"\A" + re.escape(NEEDS_DECISION_PREFIX)
    + r"[ \t]+(?P<question>.+)", flags=re.DOTALL
)

#: The implement runner's decline record (``finish_declined``). It names a
#: reason in prose, not a condition the funnel can clear, so readers show it
#: and ``stranded_items`` flags the block until a parseable one replaces it.
DECLINED_PREFIX = "**Declined:**"

#: The event spec's ``after`` field takes GitHub's own timestamp shape, the
#: one ``funnel.parse_time`` reads. It is spelled here because this module
#: may not import funnel; a test holds the two to the same answers.
_EVENT_AFTER_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

#: Every line break ``str.splitlines`` honours, with the blanks around it.
_COMMENT_LINE_BREAK_RE = re.compile(
    r"\s*[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]\s*")


def inert_comment_text(text: object) -> str:
    """Model text made safe to write outside a JSON block in a comment (#1798).

    A runner comment is the owner's own, so the author filter (#1787, #1788)
    trusts all of it, the model's words it echoes included: a blocking or
    unsure item, a decline reason, a question. A blocking reason once carried
    a line-leading ``<!-- command-center-review -->`` and a JSON block, and
    the owner's rejection read as an approval (#1797). Line breaks collapse to
    one space, so the text never begins a line or opens a fence, and ``<!--``
    becomes ``&lt;!--``, so it holds no marker even mid-line. GitHub renders
    the entity as ``<!--``, so a reader still sees every word. One pass is
    enough: neither replacement can put a ``<!--`` together.

    Text inside a JSON block needs none of this: ``json.dumps`` escapes the
    line breaks, and a marker quoted there is content (#1688).
    """
    cleaned = _COMMENT_LINE_BREAK_RE.sub(
        " ", "" if text is None else str(text)).strip()
    return cleaned.replace("<!--", "&lt;!--")


def _unique_json_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    """Reject ambiguous JSON objects instead of silently taking the last key."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _event_after_time(value: str) -> Optional[datetime]:
    """``funnel.parse_time`` for the event spec's ``after`` field."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), _EVENT_AFTER_FORMAT)
    except ValueError:
        return None


def _parse_block_event_spec(raw: str) -> Optional[Dict[str, str]]:
    """Return the one supported event spec, or None for malformed input."""
    try:
        spec = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        return None
    if not isinstance(spec, dict) or set(spec) != {
        "agent", "job", "outcome", "after"
    }:
        return None
    if (
        not isinstance(spec["agent"], str)
        or not spec["agent"].strip()
        or not isinstance(spec["job"], str)
        or not spec["job"].strip()
        or spec["outcome"] != "errored"
        or not isinstance(spec["after"], str)
        or _event_after_time(spec["after"]) is None
    ):
        return None
    return spec


def _unconditioned_event_reason(body: str) -> Optional[str]:
    """Keep malformed or unknown event forms as unconditioned block reasons."""
    header = BLOCK_EVENT_KIND_HEADER_RE.match(body)
    if header is None:
        return None
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", header.group("kind")):
        return None
    suffix = body[header.end():]
    payload = BLOCK_FENCED_PAYLOAD_RE.match(suffix)
    if payload is not None:
        suffix = suffix[payload.end():]
    return suffix.strip()


def _parse_block_comment_header(
    body: str,
) -> Optional[Tuple[re.Match, Optional[date], Optional[Dict[str, str]]]]:
    """Return a block header and its validated date or event condition."""
    event_match = BLOCK_EVENT_COMMENT_RE.match(body)
    if event_match is not None:
        event = _parse_block_event_spec(event_match.group("event_spec"))
        if event is None:
            return None
        return event_match, None, event

    match = BLOCK_COMMENT_RE.match(body)
    if match is None:
        return None
    raw_date = match.group("blocked_until")
    if raw_date is None:
        return match, None, None
    try:
        blocked_until = date.fromisoformat(raw_date)
    except ValueError:
        # The regex establishes the shape; the date parser establishes that
        # the calendar date actually exists (for example, no February 30).
        return None
    return match, blocked_until, None


def _parse_block_comment_details(
    bodies: Iterable[str],
) -> Optional[Tuple[List[str], Optional[date], str, Optional[Dict[str, str]]]]:
    """Return the newest parseable block's refs, date, reason and event."""
    for body in reversed(list(bodies)):
        if not isinstance(body, str):
            continue
        parsed_header = _parse_block_comment_header(body)
        if parsed_header is None:
            reason = _unconditioned_event_reason(body)
            if reason is not None:
                return [], None, reason, None
            continue
        match, blocked_until, event = parsed_header
        references = match.groupdict().get("references")
        return (
            references.split(" and ") if references else [],
            blocked_until,
            body[match.end():].strip(),
            event,
        )
    return None


def parse_block_comment(
    bodies: Iterable[str],
) -> Optional[Tuple[List[str], Optional[date], str]]:
    """Return the newest parseable block comment's refs, date and reason.

    The header is deliberately strict and anchored at the start of the body so
    an old or embedded mention cannot accidentally become a condition.
    """
    parsed = _parse_block_comment_details(bodies)
    return parsed[:3] if parsed is not None else None


def parse_decline_comment(bodies: Iterable[str]) -> Optional[str]:
    """Return the newest decline record's first line of reason, if any."""
    for body in reversed(list(bodies)):
        if not isinstance(body, str) or not body.startswith(DECLINED_PREFIX):
            continue
        reason = body[len(DECLINED_PREFIX):].strip()
        return reason.splitlines()[0].strip() if reason else ""
    return None


def unparseable_block_comment_lines(bodies: Iterable[str]) -> List[str]:
    """Return first lines that look like block comments but fail the parser."""
    findings: List[str] = []
    for body in bodies:
        if not isinstance(body, str):
            continue
        if not body.lstrip().startswith(BLOCK_COMMENT_PREFIX):
            continue
        if _parse_block_comment_header(body) is not None:
            continue
        lines = body.splitlines()
        findings.append(lines[0] if lines else body)
    return findings


# --------------------------------------------------------------------------
# Renderers. One per header kind; each passes the model's words through
# ``inert_comment_text`` and refuses input its own parser would not read
# back, so a posted record always round-trips (#2165).
# --------------------------------------------------------------------------

def _issue_reference(value: object) -> str:
    """One ``#N`` for the block header; ``N`` or ``#N``, a positive number."""
    text = "" if isinstance(value, bool) else str(value).strip()
    if text.startswith("#"):
        text = text[1:]
    if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
        raise ValueError(
            "a block names positive issue numbers, not {!r}".format(value))
    return "#" + text


def render_blocked(reason: object, on: Sequence[object] = (),
                   until: Optional[date] = None) -> str:
    """``**Blocked[ until DATE][ on #N and #M]:** reason``.

    ``on`` lists issue numbers; the block lifts once all of them close and
    ``until`` (a calendar date) has arrived. With neither, only a person
    lifts it.
    """
    if isinstance(on, (str, bytes, int)):
        raise TypeError("on takes a sequence of issue numbers")
    references = [_issue_reference(value) for value in on]
    if until is not None and (
        not isinstance(until, date) or isinstance(until, datetime)
    ):
        raise TypeError("until takes a calendar date")
    header = BLOCK_COMMENT_PREFIX
    if until is not None:
        header += " until " + until.isoformat()
    if references:
        header += " on " + " and ".join(references)
    return "{}:** {}".format(header, inert_comment_text(reason))


def render_blocked_until_event(spec: Mapping[str, object],
                               reason: object) -> str:
    """``**Blocked until event:**``, the fenced JSON spec, then the reason.

    The spec must be one the parser accepts; anything else would post a
    block nothing can lift, so it is refused here instead.
    """
    raw = json.dumps(dict(spec), indent=2)
    if _parse_block_event_spec(raw) is None:
        raise ValueError(
            "an event block needs exactly agent, job, outcome \"errored\" "
            "and an after timestamp")
    body = "{}\n```json\n{}\n```".format(BLOCK_EVENT_COMMENT_PREFIX, raw)
    words = inert_comment_text(reason)
    return "{}\n{}".format(body, words) if words else body


def render_needs_decision(question: object) -> str:
    """``**Needs a decision:** question``; a blank question is refused."""
    words = inert_comment_text(question)
    if not words:
        raise ValueError("a needs-decision record must ask a question")
    return "{} {}".format(NEEDS_DECISION_PREFIX, words)


def render_declined(reason: object) -> str:
    """``**Declined:** reason``, the implement runner's decline record."""
    return "{} {}".format(DECLINED_PREFIX, inert_comment_text(reason))
