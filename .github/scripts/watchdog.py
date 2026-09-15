#!/usr/bin/env python3
"""watchdog.py — read the heartbeats, and file an issue when something is wrong.

Runs in GitHub Actions, outside the machine it watches.

It reports three distinct conditions, because they have different causes and
different fixes:

- **Silent.** No record at all within the window. The watchdog can report that
  absence and when the last record arrived, but cannot observe its cause.
- **Dying.** Runs that started and never finished. That is the signature of a
  session killed mid-work by a rate limit, and it is the one condition a single
  outcome line could never have detected.
- **Erroring.** Repeated `errored` outcomes. Something is broken in the run
  itself.
- **Stale runtime.** Three consecutive scheduled runs used a checkout that
  GitHub reports behind `main` after the normal keeper lag.

Deliberately *not* reported: any `skipped-*` outcome and `nothing-to-do`. Those
are the system working, and paging on them would train the alert to be ignored.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

# One definition of "which runs are still open" — shared with `heartbeat.py`
# rather than reimplemented here. Two copies of a rule drift, and the drift is
# silent because both produce plausible-looking lists.
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))
import heartbeat  # noqa: E402
import agent_health as _agent_health  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "nateprich-projects/command-center")
BRANCH = "heartbeat"
MARKER = "<!-- command-center-watchdog -->"

# Keep the existing watchdog names as compatibility hooks for its calibration
# tests and callers. The values live in the shared module so the Actions job and
# the local funnel cannot silently acquire different thresholds.
NORMAL_PERCENTILE = _agent_health.NORMAL_PERCENTILE
NORMAL_MULTIPLE = _agent_health.NORMAL_MULTIPLE
SILENCE_FLOOR_SECONDS = _agent_health.SILENCE_FLOOR_SECONDS
ABSOLUTE_SILENCE_SECONDS = _agent_health.ABSOLUTE_SILENCE_SECONDS
MINIMUM_HISTORY = _agent_health.MINIMUM_HISTORY
HISTORY_WINDOW_SECONDS = _agent_health.HISTORY_WINDOW_SECONDS
UNFINISHED_SECONDS = _agent_health.UNFINISHED_SECONDS
DYING_THRESHOLD = _agent_health.DYING_THRESHOLD
ERROR_THRESHOLD = _agent_health.ERROR_THRESHOLD
WEEK = _agent_health.WEEK

RUNTIME_WATCHED_AGENTS = frozenset({"codex", "muse", "zcode"})
RUNTIME_RUN_COUNT = 3
RUNTIME_GRACE_SECONDS = 15 * 60


def gh(*args: str) -> str:
    proc = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout


def runtime_compare(head: str) -> Optional[Dict]:
    """Return GitHub's comparison for one recorded runtime head."""
    try:
        payload = json.loads(
            gh("api", "repos/{}/compare/{}...main".format(REPO, head))
        )
    except (RuntimeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _row_timestamp(row: Dict) -> Optional[float]:
    value = row.get("ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _runtime_head(row: Dict) -> Optional[str]:
    runtime = row.get("runtime")
    if not isinstance(runtime, dict):
        return None
    head = runtime.get("head")
    if not isinstance(head, str) or not head.strip():
        return None
    return head.strip()


def _timestamp(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _main_head_timestamp(comparison: Dict) -> Optional[float]:
    """Read the main head date from a compare response."""
    direct = _timestamp(comparison.get("main_head_date"))
    if direct is not None:
        return direct

    # `head_commit` keeps the helper compatible with compact fixture payloads.
    for name in ("head_commit",):
        commit = comparison.get(name)
        if not isinstance(commit, dict):
            continue
        details = commit.get("commit")
        if not isinstance(details, dict):
            continue
        for role in ("committer", "author"):
            person = details.get(role)
            if not isinstance(person, dict):
                continue
            timestamp = _timestamp(person.get("date"))
            if timestamp is not None:
                return timestamp

    # GitHub's compare response does not expose a top-level head_commit. For
    # `<runtime>...main`, its commits list is the main-only side and ends at
    # main's head, so use the newest listed commit rather than base_commit
    # (which is the runtime checkout itself).
    commits = comparison.get("commits")
    if isinstance(commits, list):
        for commit in reversed(commits):
            if not isinstance(commit, dict):
                continue
            details = commit.get("commit")
            if not isinstance(details, dict):
                continue
            for role in ("committer", "author"):
                person = details.get(role)
                if not isinstance(person, dict):
                    continue
                timestamp = _timestamp(person.get("date"))
                if timestamp is not None:
                    return timestamp
    return None


def _runtime_lag_problem(agent: str, rows: List[Dict], now: float) -> Optional[str]:
    if agent not in RUNTIME_WATCHED_AGENTS:
        return None

    starts = sorted(
        (row for row in rows
         if row.get("phase") == "start" and _row_timestamp(row) is not None),
        key=lambda row: _row_timestamp(row),
    )
    recent = starts[-RUNTIME_RUN_COUNT:]
    if len(recent) < RUNTIME_RUN_COUNT:
        return None

    heads = [_runtime_head(row) for row in recent]
    if any(head is None for head in heads):
        return None

    comparisons = {}
    for head in set(heads):
        comparison = runtime_compare(head)
        # The API compares the recorded runtime head as the base to `main` as
        # the target. A runtime that is behind main therefore has status
        # `ahead`, and `ahead_by` is the number of missing main commits.
        if not isinstance(comparison, dict) or comparison.get("status") != "ahead":
            return None
        ahead_by = comparison.get("ahead_by")
        if isinstance(ahead_by, bool) or not isinstance(ahead_by, int) or ahead_by <= 0:
            return None
        main_head = _main_head_timestamp(comparison)
        if main_head is None:
            return None
        comparisons[head] = (ahead_by, main_head)

    latest_start = _row_timestamp(recent[-1])
    if latest_start is None:
        return None
    main_head = max(value[1] for value in comparisons.values())
    if latest_start - main_head <= RUNTIME_GRACE_SECONDS:
        return None

    lag_by = max(value[0] for value in comparisons.values())
    unit = "commit" if lag_by == 1 else "commits"
    return (
        "`{}` has run from a checkout {} {} behind `main` for its last three "
        "runs. The most recent started more than fifteen minutes after `main`'s "
        "head commit; refresh the runtime checkout."
    ).format(agent, lag_by, unit)


def records(agent: str) -> List[Dict]:
    try:
        raw = gh("api", "repos/{}/contents/{}.jsonl?ref={}".format(REPO, agent, BRANCH))
    except RuntimeError:
        return []
    content = base64.b64decode(json.loads(raw).get("content", "")).decode("utf-8", "replace")
    out = []
    for line in content.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _history(rows: List[Dict], now: float) -> Tuple[List[float], List[float]]:
    return _agent_health._history(
        rows, now, history_window_seconds=HISTORY_WINDOW_SECONDS
    )


_percentile = _agent_health._percentile
_duration = _agent_health._duration


def _normal_gap(rows: List[Dict], now: float) -> Optional[Tuple[float, float, int]]:
    return _agent_health._normal_gap(
        rows,
        now,
        normal_percentile=NORMAL_PERCENTILE,
        minimum_history=MINIMUM_HISTORY,
        history_window_seconds=HISTORY_WINDOW_SECONDS,
    )


def _window_label() -> str:
    return _agent_health._window_label(HISTORY_WINDOW_SECONDS)


def health_line(agent: str, rows: List[Dict], now: float) -> str:
    """Render the run accounting that accompanies the watchdog notes."""
    summary = heartbeat.run_summary(rows, now=now)
    if not any(summary.values()):
        return ""
    return (
        "`{}` run health: {} starts, {} finishes, {} re-begins."
    ).format(
        agent,
        summary["starts"],
        summary["finishes"],
        summary["re_begins"],
    )


def assess(agent: str, rows: List[Dict], now: float) -> List[str]:
    """Compatibility wrapper around the shared heartbeat assessment."""
    problems = _agent_health.assess(
        agent,
        rows,
        now,
        normal_percentile=NORMAL_PERCENTILE,
        normal_multiple=NORMAL_MULTIPLE,
        silence_floor_seconds=SILENCE_FLOOR_SECONDS,
        absolute_silence_seconds=ABSOLUTE_SILENCE_SECONDS,
        minimum_history=MINIMUM_HISTORY,
        history_window_seconds=HISTORY_WINDOW_SECONDS,
        unfinished_seconds=UNFINISHED_SECONDS,
        dying_threshold=DYING_THRESHOLD,
        week=WEEK,
        error_threshold=ERROR_THRESHOLD,
    )
    runtime_problem = _runtime_lag_problem(agent, rows, now)
    if runtime_problem:
        problems.append(runtime_problem)
    return problems


def note(agent: str, rows: List[Dict], now: Optional[float] = None) -> str:
    """Informational only — printed to the run log, never filed as an issue."""
    if not rows:
        return "`{}` has never recorded a run (not scheduled yet?)".format(agent)
    if now is None:
        now = time.time()
    notes = []
    line = health_line(agent, rows, now)
    if line:
        notes.append(line)
    timestamps, gaps = _history(rows, now)
    if len(gaps) < MINIMUM_HISTORY:
        notes.append((
            "`{}` has only {} gap(s) in the trailing {} history; "
            "silence threshold not inferred yet"
        ).format(agent, len(gaps), _window_label()))
    return " ".join(notes)


def existing_issue() -> Dict:
    raw = gh("api", "repos/{}/issues?state=open&per_page=100".format(REPO))
    for issue in json.loads(raw):
        if MARKER in (issue.get("body") or ""):
            return issue
    return {}


def main() -> int:
    now = time.time()
    problems = []
    for agent in sorted(heartbeat.PROVIDERS):
        if agent in getattr(heartbeat, "RETIRED_AGENTS", ()):
            continue  # a stopped schedule is not a dying one (#431)
        rows = records(agent)
        problems += assess(agent, rows, now)
        info = note(agent, rows, now)
        if info:
            print("note: " + info)

    open_issue = existing_issue()

    if not problems:
        if open_issue:
            gh("api", "-X", "POST",
               "repos/{}/issues/{}/comments".format(REPO, open_issue["number"]),
               "-f", "body=Heartbeats are healthy again. Closing.")
            gh("api", "-X", "PATCH",
               "repos/{}/issues/{}".format(REPO, open_issue["number"]),
               "-f", "state=closed", "-f", "state_reason=completed")
            print("recovered; closed #{}".format(open_issue["number"]))
        else:
            print("healthy")
        return 0

    body = "\n".join(
        [MARKER, "", "The watchdog found problems with the scheduled runs.", ""]
        + ["- " + p for p in problems]
        + ["", "Healthy outcomes — any `skipped-*` result or `nothing-to-do` — "
           "are not reported here by design. This issue is only raised for "
              "silence, dying runs, repeated errors, or a stale "
              "runtime checkout.",
           "", "It closes itself once the heartbeats recover."]
    )

    if open_issue:
        # Update in place rather than filing a second issue: a watchdog that
        # files one alert an hour is a watchdog nobody reads.
        gh("api", "-X", "PATCH",
           "repos/{}/issues/{}".format(REPO, open_issue["number"]),
           "-f", "body=" + body)
        print("updated #{}".format(open_issue["number"]))
    else:
        created = json.loads(gh(
            "api", "-X", "POST", "repos/{}/issues".format(REPO),
            "-f", "title=Scheduled runs need attention",
            "-f", "body=" + body,
            "-f", "labels[]=blocked",
        ))
        print("opened #{}".format(created["number"]))

    for problem in problems:
        print("  " + problem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
