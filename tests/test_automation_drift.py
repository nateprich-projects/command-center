"""The Codex automations must still match the routine they were copied from.

`routines/codex-work.md` is not what runs. Each Codex Scheduled task carries a
pasted copy, and copies drift: all five had by 2026-09-06, and they drifted again
within hours of being synced because the routine was edited afterwards. Nobody
noticed either time. That is the whole problem — the drift is silent, and a stale
automation runs old instructions confidently.

**This is a test rather than a note in a routine** because a prompt cannot enforce
anything. `routines/claude.md`'s merge bar already requires the tests to pass, so
a drifted automation now blocks a merge without anyone remembering to look.

It **skips** where the automations do not exist — CI runners, a fresh clone — so
it is a real check on the Mac that runs the schedules and silent everywhere else.
A skip is honest here: absence of the directory is not evidence of no drift.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "sync_codex_automations", ROOT / "scripts" / "sync_codex_automations.py"
)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def test_every_codex_automation_matches_the_routine():
    files = sorted(sync.AUTOMATIONS.glob(sync.GLOB))
    if not files:
        pytest.skip(
            "no Codex automations on this machine ({}) — nothing to compare. This "
            "check is meaningful only where the schedules actually run.".format(
                sync.AUTOMATIONS)
        )

    wanted = sync.prompt_text()
    drifted = []
    for path in files:
        _, existing = sync.current(path.read_text())
        if existing != wanted:
            drifted.append(path.parent.name)

    assert not drifted, (
        "{} of {} Codex automations no longer match routines/codex-work.md:\n"
        "  {}\n\n"
        "They run the pasted copy, not the file, so these schedules are executing "
        "stale instructions. Fix with:\n"
        "  python3 scripts/sync_codex_automations.py".format(
            len(drifted), len(files), "\n  ".join(drifted))
    )


def test_the_routine_still_separates_setup_notes_from_the_runtime_prompt():
    """`prompt_text` splits on `---`. If that separator were removed, the setup
    notes would be pasted into five live automations as if they were
    instructions."""
    assert sync.SEPARATOR in sync.ROUTINE.read_text()
    body = sync.prompt_text()
    assert "Paste this into" not in body
    assert body.startswith("# Codex routine")
