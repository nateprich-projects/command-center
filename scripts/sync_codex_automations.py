#!/usr/bin/env python3
"""Push `routines/codex-work.md` into every Codex automation that runs it.

The routine file is not what runs. Each Codex Scheduled task carries its own
pasted copy in `~/.codex/automations/<id>/automation.toml`, and by 2026-09-06 all
five had drifted from the repo: they still used `RUN=$(...)`, which the routine
forbids; they predated the `skipped-nate-active` outcome, so every idle refusal
would have been misrecorded as a budget refusal; and none knew to clone, so all
five would have failed the moment the canonical checkout became read-only.

A document that five copies drift from is not a source of truth. This makes it one.

    python3 scripts/sync_codex_automations.py --check   # report drift, change nothing
    python3 scripts/sync_codex_automations.py           # write it

Only the `prompt` field is touched. Schedule, model, status and everything else
are left exactly as the app wrote them.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import re
import shutil
import sys
import time
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTINE = ROOT / "routines" / "codex-work.md"
AUTOMATIONS = pathlib.Path.home() / ".codex" / "automations"
GLOB = "command-center-*/automation.toml"

#: Everything above this line is setup documentation for Nate — how to paste it,
#: how to scope the sandbox. The agent at runtime needs what is below it.
SEPARATOR = "\n---\n"

#: Which schedules must also pass the presence test, **derived from when they
#: fire** rather than from a list of names.
#:
#: The idle rule refuses to start unless Nate has not touched the five-hour
#: window. That is a *proxy* for him being at the keyboard, and it is only worth
#: paying for on a schedule that fires while he might be. A schedule restricted
#: to particular hours already encodes the answer — those are the hours he is
#: asleep or at work — and applying the proxy there would refuse legitimate
#: overnight work, since an evening ChatGPT session still shows in the window at
#: 10pm.
#:
#: So: a schedule with `BYHOUR` fires only in chosen windows and needs no proxy.
#: One without it fires all day and does.
#:
#: This was a set of ids, keyed on `command-center-tickets-hourly`. That name is
#: already wrong — the schedule runs every fifteen minutes — and worse, renaming
#: it in the app would have silently switched the presence check off. Reading the
#: rule the schedule already carries has no such trap, and a new all-day schedule
#: gets the check without anyone remembering to add it.
IDLE_RRULE_MARKER = "BYHOUR="


GATE_LINE = "usage.py gate codex"
TIER_LINE = "funnel.py next --tier standard"

#: Which tier a schedule works, derived from when it fires — the same signal as
#: the presence check, and for a related reason.
#:
#: A schedule that fires all day is the cheap continuous lane: bounded tickets,
#: little and often. The hour-restricted schedules run while Nate is asleep or at
#: work, which is when the expensive engine can be given room — nobody is waiting
#: on the machine and a long, hard ticket costs nothing but time. So they take
#: `escalated`, and **only** escalated: the expensive engine is reserved for work
#: that needs it. Idling costs nothing, because the all-day schedule is already
#: working the ordinary queue — including overnight, where its presence check
#: passes while Nate is asleep.
#:
#: The model itself is set per automation in the Codex app, not here.
def fires_all_day(automation: str) -> bool:
    """True when a schedule fires at any hour, so the clock cannot vouch for Nate
    being away. Unknown schedules default to True — the safe direction is
    refusing to compete with him, not assuming he is out.

    **The derivation, kept separate from the switch above.** Two different things
    are read off this one fact: which tier a schedule works, and whether it pays
    the presence proxy. `PRESENCE_CHECK_ENABLED` suspends the second. It must not
    touch the first — turning the proxy off once flipped the all-day schedule to
    `escalated`, which would have pointed the cheap fifteen-minute poller at the
    riskiest work in the queue. Caught before it shipped, 2026-09-07.
    """
    if not automation:
        return False
    path = AUTOMATIONS / automation / "automation.toml"
    try:
        rule = re.search(r'^rrule = "(.*)"$', path.read_text(), re.MULTILINE)
    except OSError:
        return True
    if not rule:
        return True
    return IDLE_RRULE_MARKER not in rule.group(1)


def tier_for(automation: str) -> str:
    """Which tier this schedule works. Derived from when it fires, and
    **deliberately not** from `needs_presence_check` — see `fires_all_day`."""
    return "standard" if fires_all_day(automation) else "escalated"


def needs_presence_check(automation: str) -> bool:
    """Whether this schedule pays the presence proxy.

    Kept separate from `tier_for` even though both read one fact: the two were
    the same function until 2026-09-07, and suspending the proxy silently moved
    a schedule's tier. The suspension itself now lives in `usage.py`, because
    the Codex app rewrites this prompt and a switch it can overwrite is not one.
    """
    return fires_all_day(automation)


def prompt_text(automation: str = "") -> str:
    """The runtime prompt for one automation.

    Everything above the `---` is setup documentation for Nate; below it is what
    the agent runs. The only per-automation difference is the idle flag.
    """
    body = ROUTINE.read_text()
    title = body.splitlines()[0].strip()
    if SEPARATOR not in body:
        raise SystemExit("{}: no '---' separator; cannot tell setup notes from the "
                         "runtime prompt".format(ROUTINE))
    runtime = body.split(SEPARATOR, 1)[1].strip()
    if needs_presence_check(automation):
        runtime = runtime.replace(GATE_LINE, GATE_LINE + " --idle", 1)
    tier = tier_for(automation)
    if tier != "standard":
        runtime = runtime.replace(
            TIER_LINE, TIER_LINE.replace("standard", tier), 1)
    return "{}\n\n{}\n".format(title, runtime)


def same_prompt(existing: Optional[str], wanted: str) -> bool:
    """Whether a stored prompt matches the routine, ignoring the trailing newline.

    The Codex app strips the final newline when it saves an automation, so a
    byte-exact comparison reports drift on every automation forever. That is
    worse than not checking: this is the only guard against a schedule running a
    stale pasted prompt — the failure that left zcode's breakdown job dead for
    seventy-five minutes on 2026-09-06 — and a check that always fires cannot be
    told from one that has caught something real.

    Only *trailing* whitespace is forgiven. Any difference in the prompt itself,
    including leading or interior whitespace, is still drift.
    """
    if existing is None:
        return False
    return existing.rstrip("\n") == wanted.rstrip("\n")


def current(text: str):
    match = re.search(r'^prompt = (".*")$', text, re.MULTILINE)
    if not match:
        return None, None
    return match.group(1), json.loads(match.group(1))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report drift and change nothing")
    args = parser.parse_args(argv)

    files = sorted(AUTOMATIONS.glob(GLOB))
    if not files:
        print("no Command Center automations found under {}".format(AUTOMATIONS),
              file=sys.stderr)
        return 1

    drifted = []
    for path in files:
        wanted = prompt_text(path.parent.name)
        text = path.read_text()
        raw, existing = current(text)
        if raw is None:
            print("{}: no prompt field — skipped".format(path.parent.name),
                  file=sys.stderr)
            continue
        if same_prompt(existing, wanted):
            print("  ok       {}".format(path.parent.name))
            continue
        drifted.append(path.parent.name)
        if args.check:
            # Record *when* it changed, not only that it did. Without this the
            # only evidence is a mismatch, which cannot tell a file someone
            # rewrote from an expected value that moved underneath it — and
            # guessing between those produced a confident wrong explanation on
            # 2026-09-06.
            stat = path.stat()
            print("  DRIFTED  {}".format(path.parent.name))
            print("           file written {}  ({} bytes, sha {})".format(
                datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(
                    timespec="seconds"),
                stat.st_size,
                hashlib.sha256(path.read_bytes()).hexdigest()[:12]))
            print("           routine written {}  (expected prompt sha {})".format(
                datetime.datetime.fromtimestamp(
                    ROUTINE.stat().st_mtime).isoformat(timespec="seconds"),
                hashlib.sha256(wanted.encode()).hexdigest()[:12]))
            first = next((i for i, (a, b) in enumerate(
                zip((existing or "").splitlines(), wanted.splitlines()))
                if a != b), None)
            if first is not None:
                stored = (existing or "").splitlines()[first]
                print("           first differing line {}:".format(first + 1))
                print("             stored:   {}".format(stored[:88]))
                print("             expected: {}".format(
                    wanted.splitlines()[first][:88]))
            continue

        shutil.copy2(path, path.with_suffix(".toml.bak"))
        text = text.replace(raw, json.dumps(wanted), 1)
        text = re.sub(r"^updated_at = \d+$",
                      "updated_at = {}".format(int(time.time() * 1000)),
                      text, count=1, flags=re.MULTILINE)
        path.write_text(text)

        _, written = current(path.read_text())
        if written != wanted:
            raise SystemExit("{}: wrote the prompt but it did not read back "
                             "identically — restore from the .bak".format(path))
        print("  updated  {}".format(path.parent.name))

    if args.check and drifted:
        print("\n{} of {} automations have drifted from {}".format(
            len(drifted), len(files), ROUTINE.relative_to(ROOT)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
