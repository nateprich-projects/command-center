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

REPO = os.environ.get("GITHUB_REPOSITORY", "nateprich-projects/command-center")
BRANCH = "heartbeat"
MARKER = "<!-- command-center-watchdog -->"

#: Calibrated by the offline replay in tests/test_watchdog_calibration.py against
#: the heartbeat branch snapshot in tests/fixtures/heartbeat_history.json.
NORMAL_PERCENTILE = 90
NORMAL_MULTIPLE = 5
SILENCE_FLOOR_SECONDS = 3600
MINIMUM_HISTORY = 8

#: The normal rhythm is learned from the trailing fortnight. This is deliberately
#: separate from the four provisional calibration parameters above: the ticket
#: calls for the window shown in the alarm evidence, "p90 over 14 days".
HISTORY_WINDOW_SECONDS = 14 * 86400

#: A run still unfinished after this long is presumed dead. Matches the lock TTL
#: in funnel.py — the same two hours after which its claim becomes takeable.
UNFINISHED_SECONDS = 2 * 3600

#: One dying run is noise. Three in a week means runs are dying.
DYING_THRESHOLD = 3
ERROR_THRESHOLD = 3
WEEK = 7 * 86400
PROMPT_DRIFT_OUTCOME = "prompt-drift"


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
    """Return recent record timestamps and their positive consecutive gaps."""
    cutoff = now - HISTORY_WINDOW_SECONDS
    timestamps = sorted(
        float(row["ts"])
        for row in rows
        if isinstance(row.get("ts"), (int, float))
        and cutoff <= float(row["ts"]) <= now
    )
    gaps = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])
            if later > earlier]
    return timestamps, gaps


def _percentile(values: List[float], percentile: float) -> float:
    """Return a linearly interpolated percentile without a third-party library."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _normal_gap(rows: List[Dict], now: float) -> Optional[Tuple[float, float, int]]:
    """Return ``(normal gap, latest record, record count)`` when inferable."""
    timestamps, gaps = _history(rows, now)
    if len(gaps) < MINIMUM_HISTORY:
        return None
    return _percentile(gaps, NORMAL_PERCENTILE), timestamps[-1], len(timestamps)


def _duration(seconds: float) -> str:
    """Render a duration compactly enough to scan in an issue body."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return "{}s".format(total)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append("{}d".format(days))
    if hours:
        parts.append("{}h".format(hours))
    if minutes:
        parts.append("{}m".format(minutes))
    if not parts:
        parts.append("0m")
    return "".join(parts)


def _window_label() -> str:
    days = HISTORY_WINDOW_SECONDS / 86400
    if days.is_integer():
        return "{} days".format(int(days))
    return _duration(HISTORY_WINDOW_SECONDS)


def assess(agent: str, rows: List[Dict], now: float) -> List[str]:
    """Problems worth filing an issue about. Empty means healthy.

    An agent that has *never* run is not a problem: it may simply not be
    scheduled yet, and filing an alarm before the thing exists is how a watchdog
    teaches its reader to ignore it. Silence only becomes meaningful once there
    is a history to have gone quiet against. `note()` reports that case instead.
    """
    problems = []

    if not rows:
        return []

    inferred = _normal_gap(rows, now)
    if inferred is not None:
        normal, latest, record_count = inferred
        quiet_for = now - latest
        threshold = max(SILENCE_FLOOR_SECONDS, NORMAL_MULTIPLE * normal)
        if quiet_for > threshold:
            ratio = quiet_for / normal if normal else float("inf")
            ratio_text = "{:.0f}x normal".format(ratio) if ratio != float("inf") else "unbounded"
            problems.append(
                "`{}`: normal gap {} (p{} over {}, {} records). "
                "Nothing recorded for {} — {} (alarm threshold {}x normal; "
                "last at <t:{}:f>).".format(
                    agent,
                    _duration(normal),
                    NORMAL_PERCENTILE,
                    _window_label(),
                    record_count,
                    _duration(quiet_for),
                    ratio_text,
                    NORMAL_MULTIPLE,
                    int(latest),
                )
            )

    # An unresolved finish counts as a finish for one of its candidates. A run
    # that completed but could not name itself must not be reported as dying —
    # that false alarm is the failure this signal exists to avoid.
    dying = [
        r for r in heartbeat.open_starts(rows)
        if now - (r.get("ts") or 0) > UNFINISHED_SECONDS
        and now - (r.get("ts") or 0) < WEEK
    ]
    if len(dying) >= DYING_THRESHOLD:
        problems.append(
            "`{}` has {} runs this week that started and never finished — "
            "tickets {}. That is what a session killed mid-work by a rate limit "
            "looks like. Check whether the reserves in `usage.py` are too low.".format(
                agent,
                len(dying),
                ", ".join(str(r.get("ticket")) for r in dying[-5:]),
            )
        )

    prompt_drift = [
        r for r in rows
        if r.get("outcome") == PROMPT_DRIFT_OUTCOME
        and now - (r.get("ts") or 0) < WEEK
    ]
    if prompt_drift:
        problems.append(
            "`{}` reported prompt drift {} time(s) this week. The routine "
            "prompt differs from the checked-in file; sync it before relying "
            "on scheduled work. Most recent at <t:{}:f>.".format(
                agent,
                len(prompt_drift),
                int(prompt_drift[-1].get("ts") or 0),
            )
        )

    errored = [
        r for r in rows
        if r.get("outcome") == "errored" and now - (r.get("ts") or 0) < WEEK
    ]
    if len(errored) >= ERROR_THRESHOLD:
        notes = [r.get("note") for r in errored[-3:] if r.get("note")]
        problems.append(
            "`{}` errored {} times this week.{}".format(
                agent, len(errored),
                (" Most recent: " + "; ".join(notes)) if notes else "",
            )
        )
    return problems


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
