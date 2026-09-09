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
- **Drifted.** A run reported that the prompt it received differs from the
  checked-in routine.

Deliberately *not* reported: any `skipped-*` outcome and `nothing-to-do`. Those
are the system working, and paging on them would train the alert to be ignored.
"""

from __future__ import annotations

import base64
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
MINIMUM_HISTORY = _agent_health.MINIMUM_HISTORY
HISTORY_WINDOW_SECONDS = _agent_health.HISTORY_WINDOW_SECONDS
UNFINISHED_SECONDS = _agent_health.UNFINISHED_SECONDS
DYING_THRESHOLD = _agent_health.DYING_THRESHOLD
ERROR_THRESHOLD = _agent_health.ERROR_THRESHOLD
WEEK = _agent_health.WEEK
PROMPT_DRIFT_OUTCOME = _agent_health.PROMPT_DRIFT_OUTCOME


def gh(*args: str) -> str:
    proc = subprocess.run(["gh"] + list(args), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip())
    return proc.stdout


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


def assess(agent: str, rows: List[Dict], now: float) -> List[str]:
    """Compatibility wrapper around the shared heartbeat assessment."""
    return _agent_health.assess(
        agent,
        rows,
        now,
        normal_percentile=NORMAL_PERCENTILE,
        normal_multiple=NORMAL_MULTIPLE,
        silence_floor_seconds=SILENCE_FLOOR_SECONDS,
        minimum_history=MINIMUM_HISTORY,
        history_window_seconds=HISTORY_WINDOW_SECONDS,
        unfinished_seconds=UNFINISHED_SECONDS,
        dying_threshold=DYING_THRESHOLD,
        week=WEEK,
        prompt_drift_outcome=PROMPT_DRIFT_OUTCOME,
        error_threshold=ERROR_THRESHOLD,
    )


def note(agent: str, rows: List[Dict], now: Optional[float] = None) -> str:
    """Informational only — printed to the run log, never filed as an issue."""
    if not rows:
        return "`{}` has never recorded a run (not scheduled yet?)".format(agent)
    if now is None:
        now = time.time()
    timestamps, gaps = _history(rows, now)
    if len(gaps) < MINIMUM_HISTORY:
        return (
            "`{}` has only {} gap(s) in the trailing {} history; "
            "silence threshold not inferred yet"
        ).format(agent, len(gaps), _window_label())
    return ""


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
              "silence, dying runs, prompt drift, or repeated errors.",
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
