#!/usr/bin/env python3
"""Print a routine's opening command with its current ``--routine-sha``.

    python3 scripts/paste_routine_sha.py routines/zcode.md
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


# Keep the command itself intact: its path spelling and all of its existing
# flags are part of the routine prompt.  Only the self-referential argument is
# replaced.  This mirrors funnel.ROUTINE_SHA_ARGUMENT; funnel.routine_sha() is
# the single implementation used for the actual hash.
BEGIN_COMMAND = re.compile(
    r"^[ \t]*(?P<command>python3[ \t]+\S*funnel\.py[ \t]+begin(?:[ \t]+.*)?)$",
    re.MULTILINE,
)
ROUTINE_SHA_ARGUMENT = re.compile(r"(?:[ \t]+)--routine-sha[ \t]+\S+")


def opening_command(path: pathlib.Path) -> str:
    """Return the routine's one literal ``funnel.py begin`` command."""
    text = path.read_text(encoding="utf-8")
    matches = [match.group("command").strip()
               for match in BEGIN_COMMAND.finditer(text)]
    if not matches:
        raise ValueError("no python3 funnel.py begin command found")
    if len(matches) > 1:
        raise ValueError("more than one python3 funnel.py begin command found")
    return matches[0]


def paste_command(path: pathlib.Path) -> str:
    """Return the opening command with the routine's stable current hash."""
    command = ROUTINE_SHA_ARGUMENT.sub("", opening_command(path))
    return "{} --routine-sha {}".format(command, funnel.routine_sha(path))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="print a routine's literal funnel begin command")
    parser.add_argument("routine", type=pathlib.Path,
                        help="routine file containing a funnel.py begin command")
    args = parser.parse_args(argv)

    try:
        print(paste_command(args.routine))
    except (OSError, UnicodeError, ValueError) as exc:
        print("{}: {}".format(parser.prog, exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
