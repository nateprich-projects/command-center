"""The paths agents are told to invoke must not be "helpfully" normalised.

`~/.claude/command-center` is a symlink to the repo on an external volume. Codex
rejects a symlinked *writable root* and must be configured with the resolved path
— that is sandbox configuration, and it is correct.

The trap is doing the same thing to the *commands*. A Claude Code permission rule
matches the literal string `Bash(python3 /Users/nateprich/.claude/command-center/*)`.
Rewrite a routine to `/Volumes/External SSD/...` and every heartbeat, gate and
funnel call in the Claude routine starts prompting — the prompt storm that caused
`RUN=$(...)` to be removed, which caused the clobbered run-id pointer in #26.

Both agents run from prompts, and a prompt cannot enforce anything. This can.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The spelling every command must use. Not `~`, which the permission rule's
#: literal match would not recognise either.
CANONICAL = "/Users/nateprich/.claude/command-center"

#: The resolved path. Legitimate in prose — `LEARNINGS.md` documents exactly this
#: trap — but never in a file an agent executes from.
RESOLVED = "/Volumes/"


def command_files():
    """Files an agent reads and runs commands out of."""
    found = sorted(ROOT.glob("routines/*.md"))
    found += sorted(ROOT.glob("skills/*/SKILL.md"))
    found += [ROOT / ".claude" / "settings.json"]
    return [p for p in found if p.exists()]


def test_there_are_command_files_to_check():
    """A glob that matches nothing would make every test below vacuously pass."""
    assert len(command_files()) >= 5


def test_no_command_file_names_the_resolved_path():
    offenders = []
    for path in command_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if RESOLVED in line:
                offenders.append("{}:{}: {}".format(
                    path.relative_to(ROOT), number, line.strip()))
    assert not offenders, (
        "The resolved path belongs only in Codex's sandbox configuration. In a "
        "file an agent executes from it breaks the Claude permission rule, which "
        "matches {!r} literally:\n  {}".format(
            CANONICAL, "\n  ".join(offenders))
    )


def test_every_script_invocation_uses_the_canonical_spelling():
    offenders = []
    for path in command_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            for script in ("funnel.py", "heartbeat.py", "usage.py", "prior_run.py"):
                if script not in line:
                    continue
                # Prose may name a script without invoking it.
                if "python3 " not in line:
                    continue
                if CANONICAL not in line:
                    offenders.append("{}:{}: {}".format(
                        path.relative_to(ROOT), number, line.strip()))
    assert not offenders, (
        "Every invocation must spell the path {!r} so the permission rule "
        "matches:\n  {}".format(CANONICAL, "\n  ".join(offenders))
    )


def test_the_permission_rule_itself_is_intact():
    import json

    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    allow = settings.get("permissions", {}).get("allow", [])
    assert "Bash(python3 {}/*)".format(CANONICAL) in allow, (
        "The allow rule the routines depend on is gone from .claude/settings.json. "
        "Without it every command-center call prompts, and a scheduled run cannot "
        "answer a prompt."
    )
