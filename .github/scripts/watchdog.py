#!/usr/bin/env python3
"""watchdog.py — read the heartbeats, and file an issue when something is wrong.

Runs in GitHub Actions, outside the machine it watches.

It reports three distinct conditions, because they have different causes and
different fixes:

- **Silent.** No record at all within the window. The app is closed, the Mac
  mini is off or asleep, or the routine was never scheduled.
- **Dying.** Runs that started and never finished. That is the signature of a
  session killed mid-work by a rate limit, and it is the one condition a single
  outcome line could never have detected.
- **Erroring.** Repeated `errored` outcomes. Something is broken in the run
  itself.

Deliberately *not* reported: `skipped-over-pace`, `skipped-locked`, and
`nothing-to-do`. Those are the system working, and paging on them would train
the alert to be ignored.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from typing import Dict, List

# One definition of "which runs are still open" — shared with `heartbeat.py`
# rather than reimplemented here. Two copies of a rule drift, and the drift is
# silent because both produce plausible-looking lists.
sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))
import heartbeat  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "nateprich-projects/command-center")
BRANCH = "heartbeat"
MARKER = "<!-- command-center-watchdog -->"

#: How long an agent may go without recording anything. Codex polls hourly and
#: Claude reviews on its own routine; three hours allows for a missed run, a
#: reboot, or a laptop lid, without waiting so long that a dead system looks
#: healthy all day.
SILENCE_SECONDS = {"codex": 3 * 3600, "claude": 12 * 3600}

#: A run still unfinished after this long is presumed dead. Matches the lock TTL
#: in funnel.py — the same two hours after which its claim becomes takeable.
UNFINISHED_SECONDS = 2 * 3600

#: One dying run is noise. Three in a week means runs are dying.
DYING_THRESHOLD = 3
ERROR_THRESHOLD = 3
WEEK = 7 * 86400


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

    latest = max(r.get("ts") or 0 for r in rows)
    quiet_for = now - latest
    if quiet_for > SILENCE_SECONDS[agent]:
        problems.append(
            "`{}` has recorded nothing for {:.1f} hours (last at <t:{}:f>). "
            "Its app is probably not running on the Mac mini.".format(
                agent, quiet_for / 3600, int(latest)
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


def note(agent: str, rows: List[Dict]) -> str:
    """Informational only — printed to the run log, never filed as an issue."""
    if not rows:
        return "`{}` has never recorded a run (not scheduled yet?)".format(agent)
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
    for agent in ("codex", "claude"):
        rows = records(agent)
        problems += assess(agent, rows, now)
        info = note(agent, rows)
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
        + ["", "Healthy outcomes — over pace, locked, nothing to do — are not "
              "reported here by design. This issue is only raised for silence, "
              "dying runs, or repeated errors.",
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
