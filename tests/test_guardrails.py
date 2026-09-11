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
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The spelling every command must use. Not `~`, which the permission rule's
#: literal match would not recognise either.
CANONICAL = "/Users/nateprich/.claude/command-center"

#: The resolved path. Legitimate in prose — `LEARNINGS.md` documents exactly this
#: trap — but never in a file an agent executes from.
RESOLVED = "/Volumes/"

CAPTURE_ROUTINES = (
    "claude.md",
    "muse.md",
    "zcode.md",
    "codex-work.md",
)
AGENT_CAPTURE_ROUTINES = (
    "claude.md",
    "muse.md",
    "codex-work.md",
)
CAPTURE_RULE = (
    "When this run observes a defect (broken behaviour, a failing command, or a "
    "misbehaving run — evidence, not speculation), record it before finishing "
    "with `funnel capture`. Put the observed evidence in the note, choose its "
    "class at capture using `skills/shape`'s \"Class it when you file it\" rule, "
    "and say why. Agents class their own captures, never his existing issues."
)
CAPTURE_EXCEPTION = (
    "This is the sanctioned exception to the review rule to act only on the PR "
    "you were given: capture records the observed defect; it does not act on the "
    "thing observed."
)

# These are the load-bearing plan sections described by skills/shape. Keep the
# canonical names here so a prose edit cannot silently drift away from the
# vocabulary the plan parser and self-approval path understand.
PLAN_SECTION_NAMES = (
    "Decided from precedent",
    "Decided by the agent",
    "Needs Nate",
)
NEEDS_SECTION_ALIASES = ("Needs Nate", "Needs you")

# Python's default cache location is not writable in the managed checkout.
# Reject explicit bytecode writers in agent-run documents: py_compile and
# compileall both write bytecode even when the environment disables implicit
# cache writes. The fast syntax check instead uses in-memory compile().
_VERIFICATION_COMMAND = re.compile(
    r"(?P<prefix>PYTHONDONTWRITEBYTECODE=1[ \t]+)?"
    r"(?P<command>(?<![\w-])"
    r"(?:python(?:3)?[ \t]+-[ \t]*m[ \t]+"
    r"(?:py_compile|compileall|pytest)|pytest)"
    r"(?=$|[\s`'\";|&)>]))"
)


def command_files():
    """Files an agent reads and runs commands out of."""
    found = sorted(ROOT.glob("routines/*.md"))
    found += sorted(ROOT.glob("skills/*/SKILL.md"))
    found += [ROOT / ".claude" / "settings.json"]
    return [p for p in found if p.exists()]


def verification_command_files():
    """Agent-run documents whose Python verification commands we guard."""
    found = sorted(ROOT.glob("routines/*.md"))
    found += sorted(ROOT.glob("skills/*/SKILL.md"))
    return [p for p in found if p.exists()]


def verification_command_offenders(text):
    """Return cache-writing or unprotected Python verification commands."""
    offenders = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in _VERIFICATION_COMMAND.finditer(line):
            command = match.group("command")
            if (command.endswith(("py_compile", "compileall"))
                    or match.group("prefix") is None):
                offenders.append((number, line.strip()))
    return offenders


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
                # A syntax-check target such as ``py_compile funnel.py``
                # names a file; it is not a Command Center invocation.
                if not re.search(
                    r"python3\s+\S*/{}\b".format(re.escape(script)), line
                ):
                    continue
                if CANONICAL not in line:
                    offenders.append("{}:{}: {}".format(
                        path.relative_to(ROOT), number, line.strip()))
    assert not offenders, (
        "Every invocation must spell the path {!r} so the permission rule "
        "matches:\n  {}".format(CANONICAL, "\n  ".join(offenders))
    )


@pytest.mark.parametrize(
    ("snippet", "is_offender"),
    (
        ("python3 -m py_compile funnel.py", True),
        ("python3 -m compileall .", True),
        ("pytest -q", True),
        ("PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile funnel.py", True),
        ("PYTHONDONTWRITEBYTECODE=1 python3 -m compileall .", True),
        (
            "PYTHONDONTWRITEBYTECODE=1 python3 -c 'from pathlib import Path; "
            "compile(Path(\"funnel.py\").read_text(), \"funnel.py\", \"exec\")'",
            False,
        ),
        ("PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q", False),
    ),
)
def test_verification_guardrail_rejects_bytecode_writes(snippet, is_offender):
    assert bool(verification_command_offenders(snippet)) is is_offender


def test_agent_run_documents_use_no_bytecode_verification_commands():
    offenders = []
    for path in verification_command_files():
        for number, line in verification_command_offenders(
            path.read_text(encoding="utf-8")
        ):
            offenders.append("{}:{}: {}".format(
                path.relative_to(ROOT), number, line
            ))
    assert not offenders, (
        "Agent-run documents must use in-memory compile() for fast syntax checks, "
        "PYTHONDONTWRITEBYTECODE=1 for pytest, and must not use py_compile or "
        "compileall:\n  {}"
        .format("\n  ".join(offenders))
    )


def test_codex_routine_documents_the_canonical_verification_commands():
    body = (ROOT / "routines" / "codex-work.md").read_text(encoding="utf-8")
    assert (
        "PYTHONDONTWRITEBYTECODE=1 python3 -c 'from pathlib import Path; "
        "compile(Path(\"funnel.py\").read_text(), \"funnel.py\", \"exec\")'"
    ) in body
    assert "PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile funnel.py" not in body
    assert "PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q" in body


def test_the_permission_rule_itself_is_intact():
    import json

    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    allow = settings.get("permissions", {}).get("allow", [])
    assert "Bash(python3 {}/*)".format(CANONICAL) in allow, (
        "The allow rule the routines depend on is gone from .claude/settings.json. "
        "Without it every command-center call prompts, and a scheduled run cannot "
        "answer a prompt."
    )


def test_every_work_routine_captures_observed_defects_before_finishing():
    offenders = []
    for filename in CAPTURE_ROUTINES:
        path = ROOT / "routines" / filename
        body = " ".join(path.read_text(encoding="utf-8").split())
        for rule in (CAPTURE_RULE, CAPTURE_EXCEPTION):
            if rule not in body:
                offenders.append("{} is missing: {}".format(filename, rule))
        if "funnel.py capture" not in body:
            offenders.append("{} is missing the funnel capture command".format(filename))

    assert not offenders, "\n".join(offenders)


def test_active_work_routines_pass_origin_and_class_to_capture():
    offenders = []
    expected = "--origin agent --class <Broken|Maintenance|Improve|New|Replace>"
    for filename in AGENT_CAPTURE_ROUTINES:
        body = " ".join(
            (ROOT / "routines" / filename).read_text(encoding="utf-8").split()
        )
        if expected not in body:
            offenders.append(
                "{} is missing the agent capture arguments: {}".format(
                    filename, expected
                )
            )

    assert not offenders, "\n".join(offenders)

def test_shape_skill_pins_plan_section_names():
    skill = (ROOT / "skills" / "shape" / "SKILL.md").read_text()
    missing = [name for name in PLAN_SECTION_NAMES if name not in skill]
    assert not missing, (
        "skills/shape/SKILL.md must keep the plan section names stable; missing: {}"
        .format(", ".join(missing))
    )

    missing_aliases = [name for name in NEEDS_SECTION_ALIASES if name not in skill]
    assert not missing_aliases, (
        "skills/shape/SKILL.md must document both Needs-section spellings; missing: {}"
        .format(", ".join(missing_aliases))
    )
