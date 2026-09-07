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

#: Schedules that must also pass the presence test before working.
#:
#: The idle rule refuses to start unless Nate has not touched the five-hour
#: window. Five-hour utilisation is a *proxy* for him being at the keyboard, and
#: it is only worth paying for on a schedule that fires while he might be. The
#: off-hours schedules already know he is away — the hour is the signal — and
#: applying the proxy there refuses legitimate overnight work, because an evening
#: ChatGPT session still shows in the window at 10pm.
#:
#: Keyed by automation id, so the difference lives with the schedule rather than
#: in the routine, which all five share.
IDLE_AUTOMATIONS = {"command-center-tickets-hourly"}

GATE_LINE = "usage.py gate codex"


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
    if automation in IDLE_AUTOMATIONS:
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
