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
import json
import pathlib
import re
import shutil
import sys
import time

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


def needs_presence_check(automation: str) -> bool:
    """True when a schedule fires at any hour, so the clock cannot vouch for Nate
    being away. Unknown schedules default to needing the check — the safe
    direction is refusing to compete with him, not assuming he is out."""
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
    return "{}\n\n{}\n".format(title, runtime)


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
        if existing == wanted:
            print("  ok       {}".format(path.parent.name))
            continue
        drifted.append(path.parent.name)
        if args.check:
            print("  DRIFTED  {}".format(path.parent.name))
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
