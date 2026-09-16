#!/usr/bin/env python3
"""Summarise review, breakdown, and shape shadow periods from heartbeats.

The review engine and the older reviewer currently write to the same ``muse``
heartbeat stream.  Shadow finishes carry ``shadow review`` in their note, while
the live reviewer carries ``reviewed PR``.  The report also accepts two
different heartbeat streams, which keeps the reader useful if the schedules
are separated later.

The optional ``breakdown-shape`` mode reads the corresponding issue jobs from
the same streams and reports their matched population.  It is intentionally a
skeleton: outcome comparisons are added by a later slice.

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


def _timestamp(value: object) -> Optional[float]:
    """Return an epoch timestamp from heartbeat's numeric or ISO shapes."""
    if isinstance(value, bool):
        return None
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
            "finished_at": finished_at,
            "malformed": is_malformed(finish),
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


_MISSING = object()


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
    now: Optional[float] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> Dict[str, object]:
    """Build the breakdown/shape report skeleton from heartbeat rows.

    This first mode only establishes the population that a later comparison
    can measure.  It deliberately leaves ``compared`` at zero: no breakdown
    or shape outcomes are compared in this slice.
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
    data_since = _oldest_timestamp(shadow_records, live_records)
    pairs = _pair_issue_jobs(shadow, live)

    return {
        "window": {
            "since": start,
            "until": end,
            "seconds": end - start,
            "data_since": data_since,
            "truncated": data_since is not None and data_since > start,
        },
        "breakdown_shape_agreement": {
            "jobs": {
                "shadow": len(shadow),
                "live": len(live),
                "matched": len(pairs),
                "compared": 0,
            },
            "malformed_output": {
                "shadow": {
                    "count": sum(bool(job.get("malformed")) for job in shadow),
                    "rate": _rate(
                        sum(bool(job.get("malformed")) for job in shadow),
                        len(shadow),
                    ),
                },
                "live": {
                    "count": sum(bool(job.get("malformed")) for job in live),
                    "rate": _rate(
                        sum(bool(job.get("malformed")) for job in live),
                        len(live),
                    ),
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
    """
    if mode == "breakdown-shape":
        return build_breakdown_shape_report(
            shadow_records,
            live_records,
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
    return build_report(
        shadow_rows,
        live_rows,
        mode=mode,
        live_verdicts=live_verdicts,
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
