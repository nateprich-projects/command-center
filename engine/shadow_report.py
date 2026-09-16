#!/usr/bin/env python3
"""Summarise review, breakdown, and shape shadow periods from heartbeats.

The review engine and the older reviewer currently write to the same ``muse``
heartbeat stream.  Shadow finishes carry ``shadow review`` in their note, while
the live reviewer carries ``reviewed PR``.  The report also accepts two
different heartbeat streams, which keeps the reader useful if the schedules
are separated later.

The optional ``breakdown-shape`` mode reads the corresponding issue jobs from
the same streams and reports the three-part outcome agreement defined by the
breakdown/shape cutover plan.  Breakdown jobs compare ticket count and
``needs_decision``; live shape outcomes are read from the Project Status field
at report time and accepted only when the status transition falls inside the
live shape job, so an old read cannot become false agreement.

Only completed review jobs inside the requested window count.  A missing or
ambiguous verdict is not silently treated as a rejection: it is excluded from
the agreement denominator and is surfaced through the malformed-output count
when the record says that parsing failed.  This makes the cutover threshold
fail closed when the telemetry is incomplete.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import heartbeat


# The review cutover requires at least 48 hours or 50 jobs.  The caller can
# choose the actual observation window; 48 hours is the useful default while
# the shadow plist is running.
DEFAULT_WINDOW_SECONDS = 48 * 60 * 60
DEFAULT_AGENT = "muse"

SHADOW_NOTE_RE = re.compile(
    r"(?:command-center-shadow-review|shadow\s+review)", re.IGNORECASE
)
REVIEW_NOTE_RE = re.compile(
    r"(?:reviewed\s+PR|review\s+of\s+PR|shadow\s+review|pull\s+request\s+#)",
    re.IGNORECASE,
)
PR_RE = re.compile(
    r"\b(?:PR|pull\s+request)\s*#?\s*(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
DECISION_RE = re.compile(
    r"(?:decision\s*:\s*\**|recorded\s+|reviewed\s+[^:]*:\s*|"
    r"shadow\s+review[^:]*:\s*|verdict\s*[:=]\s*[\"']?)"
    r"(?P<decision>approved|rejected)\b",
    re.IGNORECASE,
)
LOOSE_DECISION_RE = re.compile(
    r"\breviewed\s+(?:PR|pull\s+request)\s+#?\s*[1-9][0-9]*\b"
    r"[^\n]{0,160}?\b(?P<decision>approved|rejected)\b",
    re.IGNORECASE,
)
MALFORMED_RE = re.compile(
    r"malformed|unparseable|could not be parsed|parse error|no verdict",
    re.IGNORECASE,
)
REVIEW_HEAD_RE = re.compile(
    r"\bat\s+(?P<head>[0-9a-f]{7,64})(?=[^0-9a-f]|$)",
    re.IGNORECASE,
)
REVIEW_REPO_RE = re.compile(
    r"\bin\s+(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b",
    re.IGNORECASE,
)
ISSUE_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
    r"\s*#\s*(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
ISSUE_URL_RE = re.compile(
    r"github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/"
    r"(?P<number>[1-9][0-9]*)\b",
    re.IGNORECASE,
)
ISSUE_SHADOW_RE = re.compile(
    r"(?:command-center-shadow-(?:breakdown|shape)|shadow\s+(?:breakdown|shape))",
    re.IGNORECASE,
)
ISSUE_BREAKDOWN_NOTE_RE = re.compile(
    r"(?:command-center-shadow-breakdown|shadow\s+breakdown|broke\s+down)\b",
    re.IGNORECASE,
)
ISSUE_SHAPE_NOTE_RE = re.compile(
    r"(?:command-center-shadow-shape|shadow\s+shape|\bshaped\s+"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\s*#\s*[1-9][0-9]*)",
    re.IGNORECASE,
)
ISSUE_KINDS = {"breakdown", "shape"}
REPORT_MODES = {"review", "breakdown-shape"}
SHAPE_STATUS_RE = re.compile(r"\b(?P<status>shaped|ready)\b", re.IGNORECASE)
SHADOW_SHAPE_STATUS_RE = re.compile(
    r"\bshadow\s+shape\b[^:\n]*:\s*(?P<status>shaped|ready)\b",
    re.IGNORECASE,
)
ANSWER_MARKER_RE = re.compile(r"\banswer\s*:\s*", re.IGNORECASE)
LIVE_TICKET_COUNT_RE = re.compile(
    r"\b(?:created|opened|added|made|with|into)\s+"
    r"(?P<count>[0-9]+)\s+(?:sub[- ]?issues?|tickets?)\b",
    re.IGNORECASE,
)
LIVE_NEEDS_DECISION_RE = re.compile(
    r"\bneeds[_ -]?decision(?:\s+question)?\s*[:=]\s*"
    r"(?P<question>[^;\n]+)",
    re.IGNORECASE,
)


def _timestamp(value: object) -> Optional[float]:
    """Return an epoch timestamp from heartbeat's numeric or ISO shapes."""
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _truthy(value: object) -> bool:
    """Recognise explicit boolean-like shadow markers without guessing."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "shadow"}
    return False


def _note(row: Dict) -> str:
    value = row.get("note")
    return value if isinstance(value, str) else ""


def decision(row: Dict) -> Optional[str]:
    """Return a review verdict from a structured field or finish note.

    Older heartbeat finishes predate ``--review-result`` and carry the verdict
    in prose.  The structured field always wins; the note parser is deliberately
    narrow so words such as ``approved`` in an unrelated explanation do not
    become a review result.
    """
    for key in ("review_result", "verdict", "decision"):
        value = row.get(key)
        if isinstance(value, str) and value.lower() in {"approved", "rejected"}:
            return value.lower()
    note = _note(row)
    matches = list(DECISION_RE.finditer(note))
    matches += list(LOOSE_DECISION_RE.finditer(note))
    if matches:
        return matches[-1].group("decision").lower()
    return None


def is_malformed(row: Dict) -> bool:
    """Whether a finish explicitly records malformed model output."""
    for key in ("malformed", "malformed_output", "parse_error"):
        if _truthy(row.get(key)):
            return True
    note = _note(row)
    return bool(note and MALFORMED_RE.search(note))


def is_shadow(row: Dict) -> bool:
    """Whether a heartbeat row carries an explicit shadow marker."""
    if any(_truthy(row.get(key)) for key in ("shadow", "shadow_run")):
        return True
    for key in ("mode", "kind", "role"):
        value = row.get(key)
        if isinstance(value, str) and value.strip().lower() == "shadow":
            return True
    agent = row.get("agent")
    if isinstance(agent, str) and "shadow" in agent.lower():
        return True
    return bool(SHADOW_NOTE_RE.search(_note(row)))


def _run_kinds(records: Iterable[Dict]) -> Dict[str, str]:
    """Collect explicit per-run shadow/live markers from any record phase."""
    found: Dict[str, str] = {}
    for row in records:
        if not isinstance(row, dict) or not row.get("run"):
            continue
        if is_shadow(row):
            found[str(row["run"])] = "shadow"
        elif str(row.get("mode") or row.get("kind") or "").lower() == "live":
            found.setdefault(str(row["run"]), "live")
    return found


def partition_records(records: Sequence[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split one heartbeat stream into shadow and live run records.

    The split is by run id, not by individual JSONL row, so the start and bind
    rows stay attached to the finish that identifies the mode.
    """
    rows = [row for row in records if isinstance(row, dict)]
    kinds = _run_kinds(rows)
    shadow_runs = {run for run, kind in kinds.items() if kind == "shadow"}
    shadow = [row for row in rows if str(row.get("run")) in shadow_runs]
    live = [row for row in rows if str(row.get("run")) not in shadow_runs]
    return shadow, live


def _latest_by_run(records: Iterable[Dict], phase: str) -> Dict[str, Dict]:
    """Return the latest timestamped row for each run and phase."""
    found: Dict[str, Dict] = {}
    for row in records:
        if not isinstance(row, dict) or row.get("phase") != phase:
            continue
        run = row.get("run")
        timestamp = _timestamp(row.get("ts"))
        if not run or timestamp is None:
            continue
        key = str(run)
        current = found.get(key)
        if current is None or timestamp >= _timestamp(current.get("ts")):
            found[key] = row
    return found


def _earliest_by_run(records: Iterable[Dict], phase: str) -> Dict[str, Dict]:
    """Return the earliest timestamped row for each run and phase."""
    found: Dict[str, Dict] = {}
    for row in records:
        if not isinstance(row, dict) or row.get("phase") != phase:
            continue
        run = row.get("run")
        timestamp = _timestamp(row.get("ts"))
        if not run or timestamp is None:
            continue
        key = str(run)
        current = found.get(key)
        if current is None or timestamp < _timestamp(current.get("ts")):
            found[key] = row
    return found


def _bindings(records: Iterable[Dict]) -> Dict[str, Dict]:
    """Return the latest binding row for each run."""
    return _latest_by_run(records, "bind")


def _review_target(note: str) -> Optional[str]:
    """Extract a stable PR key from a review note when one is present."""
    match = PR_RE.search(note or "")
    if not match:
        return None
    # Shadow notes include ``in owner/repo`` while older live notes often do
    # not.  The PR number is the common durable key across those note shapes;
    # duplicate numbers are paired in chronological order below.
    return "pr#{}".format(match.group("number"))


def _positive_pr_number(value: object) -> Optional[int]:
    """Read a PR number from a bare number or an issue-style reference."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isdigit():
        number = int(text)
        return number if number > 0 else None
    match = re.search(r"#([1-9][0-9]*)$", text)
    return int(match.group(1)) if match else None


def _issue_ref(value: object, repo: object = None) -> Optional[str]:
    """Return a canonical ``owner/repo#number`` from a reference value."""
    if isinstance(value, dict):
        for key in ("ref", "work", "ticket", "issue"):
            found = _issue_ref(value.get(key), value.get("repo") or repo)
            if found:
                return found
        number = _positive_pr_number(value.get("number"))
        value_repo = value.get("repo") or repo
        if number and isinstance(value_repo, str) and "/" in value_repo:
            return "{}#{}".format(value_repo.strip().lower(), number)
        return None

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value if value > 0 else None
        if number and isinstance(repo, str) and "/" in repo:
            return "{}#{}".format(repo.strip().lower(), number)
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    match = ISSUE_REF_RE.search(text) or ISSUE_URL_RE.search(text)
    if match:
        return "{}#{}".format(
            match.group("repo").lower(), match.group("number")
        )
    number = _positive_pr_number(text)
    if number and isinstance(repo, str) and "/" in repo:
        return "{}#{}".format(repo.strip().lower(), number)
    return None


def _issue_target(binding: Optional[Dict], finish: Dict) -> Optional[str]:
    """Return the issue target from binding data, then finish-note fallback."""
    for row in (binding or {}, finish):
        if not isinstance(row, dict):
            continue
        for key in ("ref", "work", "ticket", "issue", "number"):
            found = _issue_ref(row.get(key), row.get("repo"))
            if found:
                return found
    note = _note(finish)
    match = ISSUE_REF_RE.search(note) or ISSUE_URL_RE.search(note)
    if match:
        return "{}#{}".format(
            match.group("repo").lower(), match.group("number")
        )
    return None


def _issue_kind(binding: Optional[Dict], finish: Dict) -> Optional[str]:
    """Identify a breakdown or shape run, preferring its run binding."""
    if isinstance(binding, dict):
        bound_do = str(binding.get("do") or "").strip().lower()
        if bound_do:
            return bound_do if bound_do in ISSUE_KINDS else None

    for row in (finish,):
        for key in ("issue_kind", "job_kind"):
            value = row.get(key)
            if isinstance(value, str) and value.strip().lower() in ISSUE_KINDS:
                return value.strip().lower()

    note = _note(finish)
    if ISSUE_BREAKDOWN_NOTE_RE.search(note):
        return "breakdown"
    if ISSUE_SHAPE_NOTE_RE.search(note):
        return "shape"
    return None


def _issue_is_shadow(row: Optional[Dict]) -> bool:
    """Recognise issue-job shadow markers without changing review parsing."""
    if not isinstance(row, dict):
        return False
    return is_shadow(row) or bool(ISSUE_SHADOW_RE.search(_note(row)))


def _issue_is_malformed(row: Dict) -> bool:
    """Detect malformed issue output without scanning answer prose.

    Shape plans can legitimately discuss malformed output in their JSON
    rationale.  Only the finish metadata and the prose before ``answer:``
    describe the run's own parse result.
    """
    for key in ("malformed", "malformed_output", "parse_error"):
        if _truthy(row.get(key)):
            return True
    note = _note(row)
    marker = ANSWER_MARKER_RE.search(note)
    prefix = note[:marker.start()] if marker else note
    return bool(prefix and MALFORMED_RE.search(prefix))


def _review_number(binding: Optional[Dict], finish: Dict) -> Optional[int]:
    """Return the PR number, preferring the explicit number in the note."""
    target = _review_target(_note(finish))
    if target:
        return _positive_pr_number(target)
    for row in (finish, binding or {}):
        for key in ("pr", "work", "job", "ticket"):
            number = _positive_pr_number(row.get(key))
            if number is not None:
                return number
    return None


def _review_repo(binding: Optional[Dict], finish: Dict) -> Optional[str]:
    """Return the repository stored on the run binding or review note."""
    for row in (finish, binding or {}):
        value = row.get("repo")
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = REVIEW_REPO_RE.search(_note(finish))
    return match.group("repo") if match else None


def _review_head(row: Optional[Dict]) -> Optional[str]:
    """Return a full or abbreviated head recorded by a review finish."""
    if not isinstance(row, dict):
        return None
    for key in ("head_sha", "head_oid", "reviewed_head"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = REVIEW_HEAD_RE.search(_note(row))
    return match.group("head") if match else None


def _heads_match(actual: object, expected: object) -> bool:
    """Compare full and abbreviated commit ids without treating blanks as equal."""
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    actual = actual.strip().lower()
    expected = expected.strip().lower()
    if not actual or not expected:
        return False
    return actual == expected or actual.startswith(expected) or expected.startswith(actual)


def _work_key(binding: Optional[Dict], finish: Dict) -> Optional[str]:
    """Use the PR target first, then the runner's bound work value."""
    target = _review_target(_note(finish))
    if target:
        return target
    for row in (finish, binding or {}):
        for key in ("work", "job", "pr", "ticket"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
    return None


def _is_review_job(finish: Dict, binding: Optional[Dict]) -> bool:
    """Identify review jobs without counting no-op or breakdown heartbeats."""
    if isinstance(binding, dict) and str(binding.get("do") or "").lower() == "review":
        return True
    for key in ("review_result", "verdict", "decision"):
        value = finish.get(key)
        if isinstance(value, str) and value.lower() in {"approved", "rejected"}:
            return True
    note = _note(finish)
    return bool(REVIEW_NOTE_RE.search(note) or decision(finish) is not None)


def jobs_from_records(
    records: Sequence[Dict],
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> List[Dict]:
    """Build completed review jobs from heartbeat rows.

    A job is represented by a finish with a matching start.  The finish time
    determines window membership, matching the watchdog's completed-duration
    rule and allowing a job that started just before the window to be counted.
    """
    rows = [row for row in records if isinstance(row, dict)]
    starts = _earliest_by_run(rows, "start")
    finishes = _latest_by_run(rows, "finish")
    bindings = _bindings(rows)
    jobs: List[Dict] = []
    for run, finish in finishes.items():
        finished_at = _timestamp(finish.get("ts"))
        if finished_at is None:
            continue
        if since is not None and finished_at < since:
            continue
        if until is not None and finished_at > until:
            continue
        binding = bindings.get(run)
        if heartbeat.is_rebegin_finish(finish) or not _is_review_job(finish, binding):
            continue
        started = starts.get(run)
        started_at = _timestamp(started.get("ts")) if started else None
        duration = None
        if started_at is not None and finished_at >= started_at:
            duration = finished_at - started_at
        jobs.append({
            "run": run,
            "key": _work_key(binding, finish) or "run#{}".format(run),
            "repo": _review_repo(binding, finish),
            "pr": _review_number(binding, finish),
            "head_sha": _review_head(finish) or _review_head(binding),
            "finished_at": finished_at,
            "decision": decision(finish),
            "malformed": is_malformed(finish),
            "duration_seconds": duration,
            "finish": finish,
        })
    return sorted(jobs, key=lambda job: (job["finished_at"], job["run"]))


def partition_issue_records(
    records: Sequence[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """Split a shared heartbeat stream into shadow and live issue runs."""
    rows = [row for row in records if isinstance(row, dict)]
    shadow_runs = {
        str(row["run"])
        for row in rows
        if row.get("run") and _issue_is_shadow(row)
    }
    shadow = [row for row in rows if str(row.get("run")) in shadow_runs]
    live = [row for row in rows if str(row.get("run")) not in shadow_runs]
    return shadow, live


def issue_jobs_from_records(
    records: Sequence[Dict],
    *,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> List[Dict]:
    """Build completed breakdown or shape jobs from heartbeat rows."""
    rows = [row for row in records if isinstance(row, dict)]
    starts = _earliest_by_run(rows, "start")
    finishes = _latest_by_run(rows, "finish")
    bindings = _bindings(rows)
    jobs: List[Dict] = []
    for run, finish in finishes.items():
        finished_at = _timestamp(finish.get("ts"))
        if finished_at is None:
            continue
        if since is not None and finished_at < since:
            continue
        if until is not None and finished_at > until:
            continue
        binding = bindings.get(run)
        kind = _issue_kind(binding, finish)
        if heartbeat.is_rebegin_finish(finish) or kind is None:
            continue
        started = starts.get(run)
        started_at = _timestamp(started.get("ts")) if started else None
        duration = None
        if started_at is not None and finished_at >= started_at:
            duration = finished_at - started_at
        jobs.append({
            "run": run,
            "kind": kind,
            "key": _issue_target(binding, finish),
            "started_at": started_at,
            "finished_at": finished_at,
            "malformed": _issue_is_malformed(finish),
            "duration_seconds": duration,
            "finish": finish,
        })
    return sorted(jobs, key=lambda job: (job["finished_at"], job["run"]))


def _percentile(values: Sequence[float], percentile: float) -> Optional[float]:
    """Linearly interpolated percentile, with an explicit empty result."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (
        position - lower
    )


def _time_stats(jobs: Sequence[Dict]) -> Dict[str, object]:
    durations = [
        float(job["duration_seconds"])
        for job in jobs
        if isinstance(job.get("duration_seconds"), (int, float))
        and not isinstance(job.get("duration_seconds"), bool)
    ]
    return {
        "jobs": len(jobs),
        "measured": len(durations),
        "median_seconds": statistics.median(durations) if durations else None,
        "p90_seconds": _percentile(durations, 90),
    }


def _pair_jobs(shadow: Sequence[Dict], live: Sequence[Dict]) -> List[Tuple[Dict, Dict]]:
    """Pair same-target jobs in chronological order, preserving duplicates."""
    shadow_by_key: Dict[str, List[Dict]] = {}
    live_by_key: Dict[str, List[Dict]] = {}
    for job in shadow:
        shadow_by_key.setdefault(str(job["key"]), []).append(job)
    for job in live:
        live_by_key.setdefault(str(job["key"]), []).append(job)
    pairs: List[Tuple[Dict, Dict]] = []
    for key in sorted(set(shadow_by_key) & set(live_by_key)):
        left = sorted(shadow_by_key[key], key=lambda job: job["finished_at"])
        right = sorted(live_by_key[key], key=lambda job: job["finished_at"])
        pairs.extend(zip(left, right))
    return pairs


def _pair_issue_jobs(
    shadow: Sequence[Dict], live: Sequence[Dict]
) -> List[Tuple[Dict, Dict]]:
    """Pair same-kind issue jobs by target and chronological occurrence."""
    shadow_by_key: Dict[Tuple[str, str], List[Dict]] = {}
    live_by_key: Dict[Tuple[str, str], List[Dict]] = {}
    for job in shadow:
        key = job.get("key")
        if key is not None:
            shadow_by_key.setdefault((str(job.get("kind")), str(key)), []).append(job)
    for job in live:
        key = job.get("key")
        if key is not None:
            live_by_key.setdefault((str(job.get("kind")), str(key)), []).append(job)
    pairs: List[Tuple[Dict, Dict]] = []
    for key in sorted(set(shadow_by_key) & set(live_by_key)):
        left = sorted(shadow_by_key[key], key=lambda job: job["finished_at"])
        right = sorted(live_by_key[key], key=lambda job: job["finished_at"])
        pairs.extend(zip(left, right))
    return pairs


def _answer_payload(row: Dict) -> object:
    """Read a JSON answer carried by a finish row or its note."""
    for key in ("answer", "payload"):
        value = row.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed

    note = _note(row)
    marker = ANSWER_MARKER_RE.search(note)
    if marker:
        try:
            parsed, _ = json.JSONDecoder().raw_decode(note[marker.end():].lstrip())
        except (TypeError, ValueError):
            return _MISSING
        return parsed if isinstance(parsed, dict) else _MISSING
    return _MISSING


def _shape_status(value: object) -> Optional[str]:
    """Normalise one of the two Project outcomes used by the shape gate."""
    if not isinstance(value, str):
        return None
    match = SHAPE_STATUS_RE.fullmatch(value.strip())
    return match.group("status").title() if match else None


def _shadow_shape_status(finish: Dict) -> Optional[str]:
    """Read the shadow shape result, with structured fields taking priority."""
    for key in (
        "shape_status", "shape_result", "shape_outcome", "project_status",
        "status",
    ):
        status = _shape_status(finish.get(key))
        if status:
            return status

    payload = _answer_payload(finish)
    if isinstance(payload, dict):
        for key in ("shape_status", "shape_result", "shape_outcome", "status"):
            status = _shape_status(payload.get(key))
            if status:
                return status

    match = SHADOW_SHAPE_STATUS_RE.search(_note(finish))
    return _shape_status(match.group("status")) if match else None


def _normalized_question(value: object) -> Optional[str]:
    """Normalise a needs-decision question exactly as the plan specifies."""
    if value is None:
        return None
    if not isinstance(value, str):
        return _MISSING  # type: ignore[return-value]
    normalized = value.strip().casefold()
    return normalized if normalized else _MISSING  # type: ignore[return-value]


def _breakdown_observation(finish: Dict) -> Optional[Tuple[int, Optional[str]]]:
    """Read ticket count and needs-decision from a shadow breakdown answer."""
    payload = _answer_payload(finish)
    if payload is _MISSING and "tickets" in finish and "needs_decision" in finish:
        payload = finish
    if not isinstance(payload, dict):
        return None
    tickets = payload.get("tickets")
    if not isinstance(tickets, list) or "needs_decision" not in payload:
        return None
    question = _normalized_question(payload.get("needs_decision"))
    if question is _MISSING:
        return None
    return len(tickets), question


def _live_ticket_count(note: str) -> Optional[int]:
    """Extract the created sub-issue count from a live breakdown note."""
    match = LIVE_TICKET_COUNT_RE.search(note)
    if match:
        return int(match.group("count"))

    lowered = note.lower()
    if re.search(r"\b(?:one|a|an)\s+(?:sub[- ]?issues?|tickets?)\b", lowered):
        return 1

    marker = re.search(r"\b(?:into|created|opened|added)\b(?P<tail>.*)", note,
                       re.IGNORECASE)
    if not marker:
        return None
    # A breakdown note names the new issues with #N.  Count distinct refs so a
    # dependency mentioned later in the prose cannot double-count a ticket.
    refs = set(re.findall(r"(?<![A-Za-z0-9])#([1-9][0-9]*)\b", marker.group("tail")))
    return len(refs) if refs else None


def _live_breakdown_observation(finish: Dict) -> Optional[Tuple[int, Optional[str]]]:
    """Read the live side of a breakdown from structured fields or its note."""
    count: Optional[int] = None
    for key in ("ticket_count", "created_ticket_count", "sub_issue_count"):
        value = finish.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            count = value
            break
    if count is None:
        for key in ("tickets", "created_tickets", "sub_issues"):
            value = finish.get(key)
            if isinstance(value, list):
                count = len(value)
                break

    note = _note(finish)
    if count is None:
        count = _live_ticket_count(note)

    question_value: object = _MISSING
    for key in ("needs_decision", "needsDecision"):
        if key in finish:
            question_value = finish.get(key)
            break
    if question_value is _MISSING:
        match = LIVE_NEEDS_DECISION_RE.search(note)
        question_value = match.group("question") if match else None
    question = _normalized_question(question_value)
    if count is None or question is _MISSING:
        return None
    return count, question


def _issue_key_parts(key: object) -> Optional[Tuple[str, int]]:
    if not isinstance(key, str):
        return None
    match = re.fullmatch(
        r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)",
        key.strip(), re.IGNORECASE,
    )
    if not match:
        return None
    return match.group("repo"), int(match.group("number"))


def _shape_status_observation(value: object) -> Dict[str, object]:
    """Make callback and Project-item status reads share one shape."""
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return {
            "status": _shape_status(value[0]),
            "status_since": value[1],
        }
    if isinstance(value, dict):
        status_value = value.get("status")
        if status_value is None:
            status_value = value.get("name")
        status_at = None
        for key in ("status_since", "status_at", "changed_at", "updated_at", "ts"):
            if key in value:
                status_at = value.get(key)
                break
        return {"status": _shape_status(status_value), "status_since": status_at}

    status_value = getattr(value, "status", None)
    status_at = getattr(value, "status_since", None)
    if status_value is not None or status_at is not None:
        return {"status": _shape_status(status_value), "status_since": status_at}
    return {"status": _shape_status(value), "status_since": None}


def _fresh_shape_status(job: Dict, value: object) -> Optional[str]:
    """Accept a status only when its transition belongs to this live job."""
    observation = _shape_status_observation(value)
    status = observation.get("status")
    changed_at = _timestamp(observation.get("status_since"))
    started_at = _timestamp(job.get("started_at"))
    finished_at = _timestamp(job.get("finished_at"))
    if (
        status not in {"Shaped", "Ready"}
        or changed_at is None
        or started_at is None
        or finished_at is None
        or changed_at < started_at
        or changed_at > finished_at
    ):
        return None
    return status


def _lookup_live_shape_status(
    live_shape_statuses: Mapping[object, object], job: Dict,
) -> object:
    candidates = [job.get("run"), job.get("key")]
    seen = set()
    for candidate in candidates:
        try:
            marker = repr(candidate)
            if marker in seen or candidate not in live_shape_statuses:
                continue
            seen.add(marker)
            return live_shape_statuses[candidate]
        except (TypeError, AttributeError):
            continue
    return _MISSING


def _apply_live_shape_statuses(
    live: Sequence[Dict],
    live_shape_statuses: Optional[Mapping[object, object]],
) -> None:
    """Overlay only current, transition-matched live Project statuses."""
    if not live_shape_statuses:
        return
    for job in live:
        if job.get("kind") != "shape":
            continue
        candidate = _lookup_live_shape_status(live_shape_statuses, job)
        if candidate is _MISSING:
            continue
        status = _fresh_shape_status(job, candidate)
        if status:
            job["shape_status"] = status


def _read_current_issue_statuses() -> Dict[str, Dict[str, object]]:
    """Read current Project statuses once for the matched live shape jobs."""
    import funnel

    try:
        items = funnel.load_items()
    except Exception:
        # Reporting is diagnostic. A status read failure must remove shape
        # observations from the denominator, not stop review telemetry.
        return {}
    found: Dict[str, Dict[str, object]] = {}
    for item in items:
        key = getattr(item, "ref", None)
        if not isinstance(key, str):
            continue
        found[key.lower()] = {
            "status": getattr(item, "status", None),
            "status_since": getattr(item, "status_since", None),
        }
    return found


def fetch_live_shape_statuses(
    live_jobs: Sequence[Dict],
    *,
    current_status: Optional[Callable[[str, int], object]] = None,
) -> Dict[str, Dict[str, object]]:
    """Fetch live shape statuses and keep only transition-fresh observations.

    The default reader makes one Project read for all jobs. Tests and other
    callers can inject a per-issue reader returning ``status`` plus
    ``status_since``. A status without a transition timestamp is deliberately
    unusable: a report must never turn an old Project read into agreement.
    """
    shape_jobs = [job for job in live_jobs if job.get("kind") == "shape"]
    if not shape_jobs:
        return {}
    by_key = _read_current_issue_statuses() if current_status is None else None
    found: Dict[str, Dict[str, object]] = {}
    for job in shape_jobs:
        candidate: object = _MISSING
        if current_status is not None:
            parts = _issue_key_parts(job.get("key"))
            if parts is not None:
                try:
                    candidate = current_status(parts[0], parts[1])
                except Exception:
                    candidate = _MISSING
        else:
            key = job.get("key")
            if isinstance(key, str):
                candidate = by_key.get(key.lower(), _MISSING)
        if candidate is _MISSING:
            continue
        if _fresh_shape_status(job, candidate):
            found[str(job.get("run"))] = _shape_status_observation(candidate)
    return found


def _comparison_pairs(
    pairs: Sequence[Tuple[Dict, Dict]],
) -> List[Tuple[Tuple[Dict, Dict], Tuple[Dict, Dict]]]:
    """Join breakdown and shape pairs by target and occurrence."""
    breakdown: Dict[str, List[Tuple[Dict, Dict]]] = {}
    shape: Dict[str, List[Tuple[Dict, Dict]]] = {}
    for left, right in pairs:
        key = left.get("key")
        if key is None or key != right.get("key"):
            continue
        destination = breakdown if left.get("kind") == "breakdown" else shape
        destination.setdefault(str(key), []).append((left, right))
    joined: List[Tuple[Tuple[Dict, Dict], Tuple[Dict, Dict]]] = []
    for key in sorted(set(breakdown) & set(shape)):
        left_pairs = sorted(
            breakdown[key], key=lambda pair: pair[0]["finished_at"]
        )
        right_pairs = sorted(
            shape[key], key=lambda pair: pair[0]["finished_at"]
        )
        joined.extend(zip(left_pairs, right_pairs))
    return joined


_MISSING = object()
_COMPARISON_UNREADABLE = object()
_UNKNOWN_QUESTION = object()

_ISSUE_SOURCE_FIELDS = (
    "tickets", "created", "created_tickets", "created_issues",
    "sub_issues", "subissues", "issues", "ticket_numbers",
    "created_count", "ticket_count", "sub_issue_count", "count",
    "needs_decision", "needs_decision_question", "question",
)
_LIVE_CREATED_COUNT_RE = re.compile(
    r"\b(?:created|into)\s+(?P<count>[0-9]+)\s+"
    r"(?:tickets?|sub[- ]issues?)\b", re.IGNORECASE
)
_LIVE_CREATED_ZERO_RE = re.compile(
    r"\b(?:created|into)\s+(?:no|zero)\s+"
    r"(?:tickets?|sub[- ]issues?)\b", re.IGNORECASE
)
_LIVE_CREATED_ONE_RE = re.compile(
    r"\b(?:created|into)\s+(?:one|a|an)\s+"
    r"(?:ticket|sub[- ]issue)\b", re.IGNORECASE
)
_LIVE_TICKET_REF_RE = re.compile(
    r"(?<![A-Za-z0-9_])#[1-9][0-9]*\b"
)
_NEEDS_QUESTION_VALUE_RE = re.compile(
    r"\bneeds[_ -]?decision(?:\s+question)?\s*[:=]\s*"
    r"(?P<question>.+?)\s*$", re.IGNORECASE
)
_NEEDS_QUESTION_MARKER_RE = re.compile(
    r"\basked\s+(?:a\s+)?needs[_ -]?decision\s+question\b",
    re.IGNORECASE,
)


def _note_answer(note: str) -> object:
    """Read the JSON answer appended to a shadow issue finish note."""
    if not isinstance(note, str) or not note.strip():
        return _MISSING
    matches = list(re.finditer(
        r"(?:^|;\s*)answer\s*:\s*", note, re.IGNORECASE
    ))
    if not matches:
        matches = list(re.finditer(r"\banswer\s*:\s*", note,
                                   re.IGNORECASE))
    if not matches:
        return _MISSING
    encoded = note[matches[0].end():].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(encoded)
    except (TypeError, ValueError):
        return _COMPARISON_UNREADABLE
    return dict(value) if isinstance(value, Mapping) else _COMPARISON_UNREADABLE


def _structured_answer(row: object) -> object:
    """Read an explicitly structured breakdown result, if a row has one."""
    if not isinstance(row, Mapping):
        return _COMPARISON_UNREADABLE
    for key in ("breakdown_answer", "answer", "result", "payload"):
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return _COMPARISON_UNREADABLE
            return (dict(decoded) if isinstance(decoded, Mapping)
                    else _COMPARISON_UNREADABLE)
        return _COMPARISON_UNREADABLE
    return _MISSING


def _structured_issue_source(row: object) -> object:
    """Combine structured result fields without scraping their prose."""
    if not isinstance(row, Mapping):
        return _COMPARISON_UNREADABLE
    answer = _structured_answer(row)
    if answer is _COMPARISON_UNREADABLE:
        return answer
    source: Dict[str, object] = {}
    if answer is not _MISSING:
        source.update(answer)
    for key in _ISSUE_SOURCE_FIELDS:
        if key in row:
            source[key] = row.get(key)
    return source if source else _MISSING


def _lookup_live_issue_data(
    live_issue_data: Optional[Mapping[object, object]], job: Dict,
) -> object:
    """Find pre-read live issue data by run, target, or kind/target key."""
    if live_issue_data is None:
        return _MISSING
    if not isinstance(live_issue_data, Mapping):
        return _COMPARISON_UNREADABLE
    key = job.get("key")
    candidates = [job.get("run"), key]
    if key is not None:
        candidates.append((job.get("kind"), key))
    seen = set()
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            marker = repr(candidate)
            if marker in seen or candidate not in live_issue_data:
                continue
            seen.add(marker)
            return live_issue_data[candidate]
        except (TypeError, AttributeError):
            continue
    return _MISSING


def _question_value(source: Mapping[str, object], *, required: bool = False) -> object:
    """Read a structured needs-decision question or explicit null."""
    for key in ("needs_decision", "needs_decision_question", "question"):
        if key not in source:
            continue
        value = source.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return _COMPARISON_UNREADABLE
    return _COMPARISON_UNREADABLE if required else _MISSING


def _question_from_note(note: str) -> object:
    """Read a live finish's question, retaining unknown-present state."""
    if not isinstance(note, str) or not note.strip():
        return _MISSING
    match = _NEEDS_QUESTION_VALUE_RE.search(note)
    if match:
        question = match.group("question").strip()
        return question or _UNKNOWN_QUESTION
    if _NEEDS_QUESTION_MARKER_RE.search(note):
        return _UNKNOWN_QUESTION
    return _MISSING


def _collection_count(value: object) -> object:
    """Count an already-read collection or an explicit non-negative count."""
    if isinstance(value, bool):
        return _COMPARISON_UNREADABLE
    if isinstance(value, int):
        return value if value >= 0 else _COMPARISON_UNREADABLE
    if isinstance(value, (list, tuple, set)):
        return len(value)
    return _COMPARISON_UNREADABLE


def _source_ticket_count(source: Mapping[str, object]) -> object:
    """Read a live created-sub-issue count from structured data."""
    for key in (
        "created", "created_tickets", "created_issues", "sub_issues",
        "subissues", "issues", "ticket_numbers", "tickets",
    ):
        if key in source:
            return _collection_count(source.get(key))
    for key in ("created_count", "ticket_count", "sub_issue_count", "count"):
        if key in source:
            return _collection_count(source.get(key))
    # A validated needs-decision answer creates no tickets.  This also lets a
    # pre-read map carry only the question for that branch.
    if any(key in source for key in (
        "needs_decision", "needs_decision_question", "question",
    )):
        return 0
    return _MISSING


def _live_ticket_count_from_note(note: str) -> object:
    """Read the created-sub-issue count from the live finish note."""
    if not isinstance(note, str) or not note.strip():
        return _MISSING
    if _NEEDS_QUESTION_MARKER_RE.search(note):
        return 0
    match = _LIVE_CREATED_COUNT_RE.search(note)
    if match:
        return int(match.group("count"))
    if _LIVE_CREATED_ZERO_RE.search(note):
        return 0
    if _LIVE_CREATED_ONE_RE.search(note):
        return 1
    for marker in re.finditer(r"\b(?:created|into)\b", note,
                              re.IGNORECASE):
        tail = note[marker.end():].splitlines()[0]
        if not re.search(r"\b(?:tickets?|sub[- ]issues?)\b", tail,
                         re.IGNORECASE):
            continue
        refs = _LIVE_TICKET_REF_RE.findall(tail)
        return len(refs) if refs else _COMPARISON_UNREADABLE
    return _MISSING


def _shadow_breakdown_observation(job: Dict) -> object:
    """Read the validated shadow breakdown payload for one job."""
    if job.get("malformed"):
        return _COMPARISON_UNREADABLE
    finish = job.get("finish")
    source = _structured_issue_source(finish)
    if source is _MISSING:
        source = _note_answer(_note(finish) if isinstance(finish, dict) else "")
    if not isinstance(source, Mapping):
        return _COMPARISON_UNREADABLE
    tickets = source.get("tickets")
    if not isinstance(tickets, list):
        return _COMPARISON_UNREADABLE
    if "needs_decision" not in source:
        return _COMPARISON_UNREADABLE
    question = _question_value(source, required=True)
    if question is _COMPARISON_UNREADABLE:
        return _COMPARISON_UNREADABLE
    return {"ticket_count": len(tickets), "needs_decision": question}


def _live_breakdown_observation(
    job: Dict, live_issue_data: Optional[Mapping[object, object]] = None,
) -> object:
    """Read a live breakdown's created set and needs-decision outcome."""
    if job.get("malformed"):
        return _COMPARISON_UNREADABLE
    finish = job.get("finish")
    note = _note(finish) if isinstance(finish, dict) else ""
    mapped = _lookup_live_issue_data(live_issue_data, job)
    data_supplied = live_issue_data is not None
    if mapped is _COMPARISON_UNREADABLE:
        return _COMPARISON_UNREADABLE
    if data_supplied and mapped is _MISSING:
        # A supplied pre-read map is authoritative.  Its omission means that
        # the live issue set could not be read, not that it was empty.
        return _COMPARISON_UNREADABLE

    note_source = _note_answer(note)
    structured = _structured_issue_source(finish)
    if structured is _COMPARISON_UNREADABLE:
        return _COMPARISON_UNREADABLE

    source: Dict[str, object] = {}
    if mapped is not _MISSING:
        if isinstance(mapped, Mapping):
            mapped_source = _structured_issue_source(mapped)
        elif isinstance(mapped, (list, tuple, set)):
            mapped_source = {"created": mapped}
        elif isinstance(mapped, int) and not isinstance(mapped, bool):
            mapped_source = {"created_count": mapped}
        else:
            mapped_source = _COMPARISON_UNREADABLE
        if not isinstance(mapped_source, Mapping):
            return _COMPARISON_UNREADABLE
        source.update(mapped_source)
    elif note_source is not _COMPARISON_UNREADABLE:
        if isinstance(note_source, Mapping):
            source.update(note_source)
    if isinstance(structured, Mapping):
        source.update(structured)

    count = _source_ticket_count(source)
    if count is _MISSING:
        if data_supplied:
            return _COMPARISON_UNREADABLE
        count = _live_ticket_count_from_note(note)
    if count in (_MISSING, _COMPARISON_UNREADABLE):
        return _COMPARISON_UNREADABLE

    question = _question_value(source)
    if question is _COMPARISON_UNREADABLE:
        return _COMPARISON_UNREADABLE
    if question is _MISSING:
        question = _question_from_note(note)
        if question is _MISSING:
            question = None
        elif question is _COMPARISON_UNREADABLE:
            return _COMPARISON_UNREADABLE
    return {"ticket_count": count, "needs_decision": question}


def _breakdown_pair_agrees(left: object, right: object) -> object:
    """Compare the two #813 breakdown predicates, fail-closed."""
    if (not isinstance(left, Mapping) or not isinstance(right, Mapping)
            or left.get("ticket_count") is _COMPARISON_UNREADABLE
            or right.get("ticket_count") is _COMPARISON_UNREADABLE):
        return _COMPARISON_UNREADABLE
    left_count = left.get("ticket_count")
    right_count = right.get("ticket_count")
    if (isinstance(left_count, bool) or not isinstance(left_count, int)
            or isinstance(right_count, bool)
            or not isinstance(right_count, int)):
        return _COMPARISON_UNREADABLE
    left_question = left.get("needs_decision")
    right_question = right.get("needs_decision")
    if left_question is _UNKNOWN_QUESTION or right_question is _UNKNOWN_QUESTION:
        if ((left_question is _UNKNOWN_QUESTION and right_question is None)
                or (right_question is _UNKNOWN_QUESTION and left_question is None)):
            question_agrees = False
        else:
            return _COMPARISON_UNREADABLE
    elif left_question is None or right_question is None:
        question_agrees = left_question is None and right_question is None
    elif isinstance(left_question, str) and isinstance(right_question, str):
        question_agrees = (
            left_question.strip().casefold()
            == right_question.strip().casefold()
        )
    else:
        return _COMPARISON_UNREADABLE
    return abs(left_count - right_count) <= 1 and question_agrees


def _issue_malformed_count(
    jobs: Sequence[Dict], observations: Mapping[int, object], *, shadow: bool,
) -> int:
    """Count explicit or comparison-unreadable issue jobs once each."""
    return sum(
        bool(job.get("malformed"))
        or (
            job.get("kind") == "breakdown"
            and observations.get(id(job)) is _COMPARISON_UNREADABLE
        )
        or (
            shadow
            and job.get("kind") == "shape"
            and observations.get(id(job)) is None
        )
        for job in jobs
    )


def _unreadable_issue_job(job: Dict, *, shadow: bool) -> bool:
    """Whether a job's model output cannot participate in a comparison."""
    if bool(job.get("malformed")):
        return True
    if job.get("kind") == "breakdown":
        if shadow:
            return _shadow_breakdown_observation(job) is _COMPARISON_UNREADABLE
        return _live_breakdown_observation(job) is _COMPARISON_UNREADABLE
    if shadow and job.get("kind") == "shape":
        return _shadow_shape_status(job.get("finish") or {}) is None
    return False


def _lookup_live_verdict(
    live_verdicts: Mapping[object, object], job: Dict,
) -> object:
    """Find one caller-supplied verdict using stable job identity variants."""
    repo = job.get("repo")
    pr = job.get("pr")
    candidates = [job.get("run"), job.get("key")]
    if repo and pr is not None:
        candidates.extend([(repo, pr), "{}#{}".format(repo, pr)])
    if pr is not None:
        candidates.extend(["pr#{}".format(pr), pr, str(pr)])
    seen = set()
    for candidate in candidates:
        try:
            marker = repr(candidate)
            if marker in seen or candidate not in live_verdicts:
                continue
            seen.add(marker)
            return live_verdicts[candidate]
        except (TypeError, AttributeError):
            continue
    return _MISSING


def _verdict_value(value: object) -> Optional[str]:
    """Read an approved/rejected word from a caller-supplied verdict."""
    if isinstance(value, str):
        found = value.lower()
        return found if found in {"approved", "rejected"} else None
    if isinstance(value, dict):
        return decision(value)
    return None


def _apply_live_verdicts(
    live: Sequence[Dict], live_verdicts: Optional[Mapping[object, object]],
) -> None:
    """Overlay validated recorded verdicts while retaining note fallbacks."""
    if not live_verdicts:
        return
    for job in live:
        candidate = _lookup_live_verdict(live_verdicts, job)
        if candidate is _MISSING:
            continue
        value = _verdict_value(candidate)
        if value is None:
            continue
        if isinstance(candidate, dict) and job.get("head_sha"):
            if not _heads_match(candidate.get("head_sha"), job.get("head_sha")):
                continue
        job["decision"] = value


def _rate(count: int, total: int) -> Optional[float]:
    return count / total if total else None


def window_bounds(
    *,
    now: Optional[float] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Tuple[float, float]:
    """Resolve an inclusive observation window and reject inverted bounds."""
    end = float(time.time() if now is None else now) if until is None else float(until)
    if since is None:
        start = end - float(window_seconds)
    else:
        start = float(since)
    if start > end:
        raise ValueError("the report window starts after it ends")
    return start, end


def _oldest_timestamp(*record_sets: Optional[Sequence[Dict]]) -> Optional[float]:
    """Return the oldest parseable heartbeat timestamp in the input streams."""
    timestamps = []
    for records in record_sets:
        if records is None:
            continue
        for row in records:
            if not isinstance(row, dict):
                continue
            timestamp = _timestamp(row.get("ts"))
            if timestamp is not None:
                timestamps.append(timestamp)
    return min(timestamps) if timestamps else None


def build_breakdown_shape_report(
    shadow_records: Sequence[Dict],
    live_records: Optional[Sequence[Dict]] = None,
    *,
    live_issue_data: Optional[Mapping[object, object]] = None,
    live_shape_statuses: Optional[Mapping[object, object]] = None,
    now: Optional[float] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Dict[str, object]:
    """Build the breakdown/shape report from already-read observations.

    The builder is fixture-pure.  ``live_issue_data`` and
    ``live_shape_statuses`` are optional caller-supplied reads for the live
    breakdown issue set and Project status.  A supplied map is authoritative:
    missing data is unreadable rather than an empty result.  Shape statuses
    are accepted only when their transition falls between the live shape
    job's start and finish.
    """
    start, end = window_bounds(
        now=now, window_seconds=window_seconds, since=since, until=until
    )
    if live_records is None or live_records is shadow_records:
        shadow_rows, live_rows = partition_issue_records(shadow_records)
    else:
        shadow_rows, live_rows = list(shadow_records), list(live_records)
    shadow = issue_jobs_from_records(shadow_rows, since=start, until=end)
    live = issue_jobs_from_records(live_rows, since=start, until=end)
    _apply_live_shape_statuses(live, live_shape_statuses)
    data_since = _oldest_timestamp(shadow_records, live_records)
    pairs = _pair_issue_jobs(shadow, live)

    shadow_observations = {
        id(job): _shadow_breakdown_observation(job)
        if job.get("kind") == "breakdown"
        else (_shadow_shape_status(job.get("finish") or {})
              if job.get("kind") == "shape" else None)
        for job in shadow
    }
    live_observations = {
        id(job): _live_breakdown_observation(job, live_issue_data)
        if job.get("kind") == "breakdown"
        else (job.get("shape_status")
              if job.get("kind") == "shape" else None)
        for job in live
    }

    comparison_pairs = _comparison_pairs(pairs)
    comparable = []
    for breakdown_pair, shape_pair in comparison_pairs:
        shadow_breakdown, live_breakdown = breakdown_pair
        shadow_shape, live_shape = shape_pair
        breakdown_agrees = _breakdown_pair_agrees(
            shadow_observations.get(id(shadow_breakdown)),
            live_observations.get(id(live_breakdown)),
        )
        shadow_shape_status = shadow_observations.get(id(shadow_shape))
        live_shape_status = live_observations.get(id(live_shape))
        if (
            any(
                bool(job.get("malformed"))
                for job in (
                    shadow_breakdown, live_breakdown,
                    shadow_shape, live_shape,
                )
            )
            or
            breakdown_agrees is _COMPARISON_UNREADABLE
            or shadow_shape_status not in {"Shaped", "Ready"}
            or live_shape_status not in {"Shaped", "Ready"}
        ):
            continue
        comparable.append(
            bool(breakdown_agrees and shadow_shape_status == live_shape_status)
        )

    agree = sum(comparable)
    compared = len(comparable)
    shadow_malformed = _issue_malformed_count(
        shadow, shadow_observations, shadow=True
    )
    live_malformed = _issue_malformed_count(
        live, live_observations, shadow=False
    )

    return {
        "window": {
            "since": start,
            "until": end,
            "seconds": end - start,
            "data_since": data_since,
            "truncated": data_since is not None and data_since > start,
        },
        "breakdown_shape_agreement": {
            "agree": agree,
            "disagree": compared - agree,
            "rate": _rate(agree, compared),
            "jobs": {
                "shadow": len(shadow),
                "live": len(live),
                "matched": len(pairs),
                "compared": compared,
            },
            "malformed_output": {
                "shadow": {
                    "count": shadow_malformed,
                    "rate": _rate(shadow_malformed, len(shadow)),
                },
                "live": {
                    "count": live_malformed,
                    "rate": _rate(live_malformed, len(live)),
                },
            },
            "time_per_job": {
                "shadow": _time_stats(shadow),
                "live": _time_stats(live),
            },
        },
    }


def build_report(
    shadow_records: Sequence[Dict],
    live_records: Optional[Sequence[Dict]] = None,
    *,
    mode: str = "review",
    live_verdicts: Optional[Mapping[object, object]] = None,
    live_issue_data: Optional[Mapping[object, object]] = None,
    live_shape_statuses: Optional[Mapping[object, object]] = None,
    now: Optional[float] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Dict[str, object]:
    """Build the JSON report from one or two heartbeat streams.

    Passing one stream (or the same list object for both arguments) activates
    the note-based shadow/live split used by the current Muse schedules.
    Passing two streams treats the first as shadow and the second as live.
    ``live_verdicts`` is an optional caller-supplied mapping of job identity to
    the recorded PR verdict. It is applied only when its head matches a head
    recorded on the job; an absent or mismatched value leaves the heartbeat
    decision as the fallback.
    ``live_issue_data`` and ``live_shape_statuses`` are the corresponding
    caller-supplied maps for breakdown issue sets and live shape jobs.  Shape
    observations must include a transition timestamp so stale reads are
    rejected.
    """
    if mode == "breakdown-shape":
        return build_breakdown_shape_report(
            shadow_records,
            live_records,
            live_issue_data=live_issue_data,
            live_shape_statuses=live_shape_statuses,
            now=now,
            window_seconds=window_seconds,
            since=since,
            until=until,
        )
    if mode != "review":
        raise ValueError("unknown shadow-report mode {!r}".format(mode))
    start, end = window_bounds(
        now=now, window_seconds=window_seconds, since=since, until=until
    )
    if live_records is None or live_records is shadow_records:
        shadow_rows, live_rows = partition_records(shadow_records)
    else:
        shadow_rows, live_rows = list(shadow_records), list(live_records)
    shadow = jobs_from_records(shadow_rows, since=start, until=end)
    live = jobs_from_records(live_rows, since=start, until=end)
    _apply_live_verdicts(live, live_verdicts)
    data_since = _oldest_timestamp(shadow_records, live_records)
    pairs = _pair_jobs(shadow, live)
    comparable = [
        (left, right)
        for left, right in pairs
        if left.get("decision") in {"approved", "rejected"}
        and right.get("decision") in {"approved", "rejected"}
    ]
    agree = sum(
        left["decision"] == right["decision"]
        for left, right in comparable
    )
    disagree = len(comparable) - agree
    shadow_approved = sum(job.get("decision") == "approved" for job in shadow)
    shadow_rejected = sum(job.get("decision") == "rejected" for job in shadow)
    live_approved = sum(job.get("decision") == "approved" for job in live)
    live_rejected = sum(job.get("decision") == "rejected" for job in live)
    shadow_malformed = sum(bool(job.get("malformed")) for job in shadow)
    live_malformed = sum(bool(job.get("malformed")) for job in live)

    return {
        "window": {
            "since": start,
            "until": end,
            "seconds": end - start,
            "data_since": data_since,
            "truncated": data_since is not None and data_since > start,
        },
        "jobs": {
            "shadow": len(shadow),
            "live": len(live),
            "matched": len(pairs),
            "compared": len(comparable),
        },
        "agreement": {
            "agree": agree,
            "disagree": disagree,
            "rate": _rate(agree, len(comparable)),
            "approved": {
                "shadow": shadow_approved,
                "live": live_approved,
            },
            "rejected": {
                "shadow": shadow_rejected,
                "live": live_rejected,
            },
        },
        "malformed_output": {
            "shadow": {
                "count": shadow_malformed,
                "rate": _rate(shadow_malformed, len(shadow)),
            },
            "live": {
                "count": live_malformed,
                "rate": _rate(live_malformed, len(live)),
            },
        },
        "time_per_job": {
            "shadow": _time_stats(shadow),
            "live": _time_stats(live),
        },
    }


# Short aliases make the pure reader convenient to use from a cutover session
# without creating another stateful reporting layer.
report = build_report
summarize = build_report


def _parse_cli_timestamp(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    parsed = _timestamp(value)
    if parsed is None:
        raise ValueError("invalid timestamp {!r}".format(value))
    return parsed


def _current_pr_head(repo: str, pr: int) -> Optional[str]:
    """Read the PR head used to validate a recorded verdict."""
    import funnel

    data = funnel._gh_json(
        "gh", "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid"
    )
    if not isinstance(data, dict):
        return None
    head = data.get("headRefOid")
    return head.strip() if isinstance(head, str) and head.strip() else None


def fetch_live_verdicts(
    live_jobs: Sequence[Dict],
    *,
    latest_verdict: Optional[Callable[[str, int], Optional[Dict]]] = None,
    current_head: Optional[Callable[[str, int], Optional[str]]] = None,
) -> Dict[str, Dict]:
    """Fetch current-head PR verdicts for live jobs, best effort.

    This is the stateful caller-side boundary. The report builder remains
    fixture-pure: it receives the already-read verdicts and keeps heartbeat
    prose as a fallback. A missing PR, verdict, or head is intentionally
    omitted so it cannot inflate agreement.
    """
    import funnel

    read_verdict = latest_verdict or funnel.latest_verdict
    read_head = current_head or _current_pr_head
    found: Dict[str, Dict] = {}
    for job in live_jobs:
        repo = job.get("repo")
        pr = _positive_pr_number(job.get("pr"))
        run = job.get("run")
        if not isinstance(repo, str) or not repo.strip() or pr is None or not run:
            continue
        try:
            verdict = read_verdict(repo, pr)
            head = read_head(repo, pr)
        except Exception:
            # The report is diagnostic. One unreadable PR must not erase the
            # other pairs, and the heartbeat decision remains available.
            continue
        if not funnel.verdict_covers_head(verdict, head):
            continue
        recorded_head = job.get("head_sha")
        if recorded_head and not _heads_match(recorded_head, head):
            continue
        if isinstance(verdict, dict):
            found[str(run)] = verdict
    return found


def load_report(
    shadow_agent: str = DEFAULT_AGENT,
    live_agent: str = DEFAULT_AGENT,
    *,
    mode: str = "review",
    live_verdicts: Optional[Mapping[object, object]] = None,
    live_issue_data: Optional[Mapping[object, object]] = None,
    live_shape_statuses: Optional[Mapping[object, object]] = None,
    now: Optional[float] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Dict[str, object]:
    """Read heartbeat streams, recover live verdicts, and build a report."""
    if mode not in REPORT_MODES:
        raise ValueError("unknown shadow-report mode {!r}".format(mode))
    if shadow_agent == live_agent:
        records = heartbeat.read(shadow_agent)
        if mode == "review":
            shadow_rows, live_rows = partition_records(records)
        else:
            shadow_rows, live_rows = partition_issue_records(records)
    else:
        shadow_rows, live_rows = (
            heartbeat.read(shadow_agent), heartbeat.read(live_agent)
        )
    if mode == "review" and live_verdicts is None:
        start, end = window_bounds(
            now=now, window_seconds=window_seconds, since=since, until=until
        )
        shadow_jobs = jobs_from_records(shadow_rows, since=start, until=end)
        live_jobs = jobs_from_records(live_rows, since=start, until=end)
        matched_live = [right for _, right in _pair_jobs(shadow_jobs, live_jobs)]
        live_verdicts = fetch_live_verdicts(matched_live)
    if mode == "breakdown-shape" and live_shape_statuses is None:
        start, end = window_bounds(
            now=now, window_seconds=window_seconds, since=since, until=until
        )
        shadow_jobs = issue_jobs_from_records(shadow_rows, since=start, until=end)
        live_jobs = issue_jobs_from_records(live_rows, since=start, until=end)
        matched_live = [
            right
            for _, right in _pair_issue_jobs(shadow_jobs, live_jobs)
            if right.get("kind") == "shape"
        ]
        live_shape_statuses = fetch_live_shape_statuses(matched_live)
    return build_report(
        shadow_rows,
        live_rows,
        mode=mode,
        live_verdicts=live_verdicts,
        live_issue_data=live_issue_data,
        live_shape_statuses=live_shape_statuses,
        now=now,
        window_seconds=window_seconds,
        since=since,
        until=until,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="report shadow agreement from heartbeat records"
    )
    parser.add_argument(
        "agents", nargs="*", metavar="AGENT",
        help="optional positional shadow and live agent names",
    )
    parser.add_argument("--shadow-agent", default=None)
    parser.add_argument("--live-agent", default=None)
    parser.add_argument(
        "--mode", choices=sorted(REPORT_MODES), default="review",
        help="report population to read (default: review)",
    )
    parser.add_argument(
        "--hours", "--window-hours", dest="hours", type=float, default=48.0,
        help="trailing window in hours (default: 48)",
    )
    parser.add_argument("--since", default=None, help="window start epoch or ISO timestamp")
    parser.add_argument("--until", default=None, help="window end epoch or ISO timestamp")
    parser.add_argument(
        "--now", default=None,
        help="testable clock value; defaults to the current epoch",
    )
    args = parser.parse_args(argv)
    if len(args.agents) > 2:
        parser.error("at most two positional agents are accepted")
    shadow_agent = args.shadow_agent or (
        args.agents[0] if args.agents else DEFAULT_AGENT
    )
    live_agent = args.live_agent or (
        args.agents[1] if len(args.agents) > 1 else DEFAULT_AGENT
    )
    if args.hours <= 0:
        parser.error("--hours must be positive")
    try:
        report_data = load_report(
            shadow_agent,
            live_agent,
            mode=args.mode,
            now=_parse_cli_timestamp(args.now),
            window_seconds=int(args.hours * 60 * 60),
            since=_parse_cli_timestamp(args.since),
            until=_parse_cli_timestamp(args.until),
        )
    except (ValueError, OSError, heartbeat.HeartbeatError) as exc:
        print("shadow-report: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(report_data, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
