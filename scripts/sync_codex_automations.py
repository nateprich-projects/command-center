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


def prompt_text() -> str:
    body = ROUTINE.read_text()
    title = body.splitlines()[0].strip()
    if SEPARATOR not in body:
        raise SystemExit("{}: no '---' separator; cannot tell setup notes from the "
                         "runtime prompt".format(ROUTINE))
    runtime = body.split(SEPARATOR, 1)[1].strip()
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

    wanted = prompt_text()
    files = sorted(AUTOMATIONS.glob(GLOB))
    if not files:
        print("no Command Center automations found under {}".format(AUTOMATIONS),
              file=sys.stderr)
        return 1

    drifted = []
    for path in files:
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
