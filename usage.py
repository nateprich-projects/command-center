#!/usr/bin/env python3
"""usage.py — read subscription usage for either agent, and gate on pace.

Both vendors expose the same two windows under different names, so this
normalises them to one shape and applies one pace rule. Neither routine
implements the gate itself, for the same reason neither ranks anything itself.

    usage.py claude          # the two windows, as JSON
    usage.py codex
    usage.py gate codex      # exit 0 to proceed, 1 if over pace, 2 if unknown

**The gate must be called from inside a live session**, after that session has
made at least one model call. Both sources are written *by* a running session,
so a reading taken before one is stale — and a stale reading always understates
usage, because a window's used percentage only rises. Gating on a stale value
systematically overestimates headroom, which is the exact failure the budget
exists to prevent. Hence: start, do a turn, read, then exit if over pace. A
no-op session costs nothing; the work is what costs.

Unknown fails closed. A run that cannot read its budget does not work.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Dict, Optional

CLAUDE_CACHE = os.path.expanduser("~/.claude/command-center-usage.json")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")

#: A reading older than this is not trusted. Both sources are refreshed by the
#: reading session itself, so anything older than a few minutes means the gate
#: was called before the session did any work — or that the app is not running.
MAX_AGE = 15 * 60

#: The week's budget: aim to have used no more than this by the end of the
#: 7-day window, leaving ~10% for Nate.
WEEKLY_TARGET = 90.0

#: Never burn a 5-hour window past this. The rolling 5-hour window, not the
#: weekly one, is what actually locks Nate out of his own account.
FIVE_HOUR_CEILING = 80.0

#: What a run is assumed to cost, reserved before it is allowed to start.
#:
#: A gate that only asks "are we under the line *now*" is a start check, not a
#: bound on spend — it will wave through a run that then blows straight past the
#: line, because nothing can cap a session's consumption once it begins. So the
#: gate reserves the cost of the run it is authorising.
#:
#: These are deliberately high bootstrap values, not measurements. Measured
#: across 14 real Codex sessions on 2026-09-05: median weekly cost ~1%, but one
#: session cost 65% of the week. None of those were one-ticket routine runs,
#: because no routine had run yet. **Replace these with the measured p90 once
#: the heartbeat has recorded real runs** — it records usage at run start and
#: end for exactly this purpose. Erring high costs a refused run; erring low
#: costs the week.
WEEKLY_RESERVE = 15.0
FIVE_HOUR_RESERVE = 30.0

FIVE_HOUR = 300 * 60
SEVEN_DAY = 10080 * 60


def read_claude() -> Optional[Dict]:
    """Claude Code's statusline cache. See statusline.sh."""
    try:
        with open(CLAUDE_CACHE) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    windows = {k: raw[k] for k in ("five_hour", "seven_day") if k in raw}
    if not windows:
        return None
    return {
        "source": "claude",
        "captured_at": raw.get("captured_at"),
        "windows": {
            name: {
                "used_percent": w.get("used_percentage"),
                "resets_at": w.get("resets_at"),
            }
            for name, w in windows.items()
        },
    }


def read_codex() -> Optional[Dict]:
    """Codex writes rate_limits into its session rollout JSONL.

    There is no single current-state file, so this takes the most recent
    reading across every session — which is the routine's own, when called from
    inside a run.
    """
    newest = None
    for path in glob.glob(CODEX_SESSIONS):
        try:
            with open(path) as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    limits = _find_rate_limits(record)
                    stamp = record.get("timestamp") or ""
                    if limits and (newest is None or stamp > newest[0]):
                        newest = (stamp, limits)
        except OSError:
            continue

    if newest is None:
        return None
    stamp, limits = newest

    # Match windows by their length, not by their key. "primary" and
    # "secondary" are positional names a vendor is free to renumber, whereas
    # "300 minutes" is the durable fact. A window whose length matches neither
    # is dropped rather than guessed at.
    windows = {}
    for key, window in limits.items():
        if not isinstance(window, dict) or "window_minutes" not in window:
            continue
        seconds = (window.get("window_minutes") or 0) * 60
        for name, length in (("five_hour", FIVE_HOUR), ("seven_day", SEVEN_DAY)):
            if abs(seconds - length) <= length * 0.1:
                windows[name] = {
                    "used_percent": window.get("used_percent"),
                    "resets_at": window.get("resets_at"),
                }
                break

    return {
        "source": "codex",
        "captured_at": _epoch(stamp),
        "windows": windows,
    } if windows else None


def _find_rate_limits(node):
    if isinstance(node, dict):
        limits = node.get("rate_limits")
        if isinstance(limits, dict):
            return limits
        for value in node.values():
            found = _find_rate_limits(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_rate_limits(value)
            if found:
                return found
    return None


def _epoch(stamp: str) -> Optional[int]:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            import calendar
            import datetime

            return calendar.timegm(datetime.datetime.strptime(stamp, fmt).timetuple())
        except ValueError:
            continue
    return None


def pace(reading: Dict, now: float) -> Dict:
    """Is this agent within its budget?

    The weekly line is proportional: by the time a fraction f of the 7-day
    window has elapsed, at most f x WEEKLY_TARGET should be spent. That keeps
    the burn even instead of letting a routine spend the week's capacity on
    Monday, and it needs no persistence — the window's own resets_at says how
    far through it we are.
    """
    verdicts = []
    windows = reading.get("windows", {})

    five = windows.get("five_hour") or {}
    if five.get("used_percent") is not None:
        verdicts.append(
            {
                "window": "five_hour",
                "used_percent": five["used_percent"],
                "reserve": FIVE_HOUR_RESERVE,
                "allowed_percent": FIVE_HOUR_CEILING,
                "over": five["used_percent"] + FIVE_HOUR_RESERVE > FIVE_HOUR_CEILING,
            }
        )

    seven = windows.get("seven_day") or {}
    if seven.get("used_percent") is not None and seven.get("resets_at"):
        remaining = max(0.0, float(seven["resets_at"]) - now)
        elapsed_fraction = max(0.0, min(1.0, 1.0 - remaining / SEVEN_DAY))
        allowed = WEEKLY_TARGET * elapsed_fraction
        verdicts.append(
            {
                "window": "seven_day",
                "used_percent": seven["used_percent"],
                "reserve": WEEKLY_RESERVE,
                "allowed_percent": round(allowed, 1),
                "elapsed_fraction": round(elapsed_fraction, 3),
                "over": seven["used_percent"] + WEEKLY_RESERVE > allowed,
            }
        )

    return {
        "windows": verdicts,
        "over_pace": any(v["over"] for v in verdicts),
        # No window read at all is not "under pace" — it is unknown.
        "known": bool(verdicts),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("claude", "codex"):
        sub.add_parser(name, help="read {}'s usage".format(name))
    gate = sub.add_parser("gate", help="exit 1 if over pace, 2 if unknown")
    gate.add_argument("agent", choices=["claude", "codex"])
    args = parser.parse_args(argv)

    now = time.time()
    agent = args.agent if args.command == "gate" else args.command
    reading = read_claude() if agent == "claude" else read_codex()

    if reading is None:
        print("usage: no reading available for {}".format(agent), file=sys.stderr)
        return 2

    age = now - reading["captured_at"] if reading.get("captured_at") else None
    reading["age_seconds"] = int(age) if age is not None else None

    if args.command != "gate":
        print(json.dumps(reading, indent=2))
        return 0

    # A reading from the future is as untrustworthy as an old one — clock skew
    # or a malformed timestamp would otherwise sail straight through the gate,
    # and every failure here reads *low*, which is the direction that burns the
    # week.
    if age is None or age > MAX_AGE or age < -60:
        if age is None:
            why = "undated"
        elif age < 0:
            why = "dated {:.0f}s in the future".format(-age)
        else:
            why = "{:.0f}s old".format(age)
        print(
            "usage: {} reading is {} — refusing to guess".format(agent, why),
            file=sys.stderr,
        )
        return 2

    verdict = pace(reading, now)
    if not verdict["known"]:
        print("usage: no usable window for {}".format(agent), file=sys.stderr)
        return 2

    for window in verdict["windows"]:
        print(
            "{:<10} {:>5.1f}% used + {:>4.1f}% reserved = {:>5.1f}%, "
            "{:>5.1f}% allowed  {}".format(
                window["window"],
                window["used_percent"],
                window["reserve"],
                window["used_percent"] + window["reserve"],
                window["allowed_percent"],
                "OVER" if window["over"] else "ok",
            ),
            file=sys.stderr,
        )
    return 1 if verdict["over_pace"] else 0


if __name__ == "__main__":
    sys.exit(main())
