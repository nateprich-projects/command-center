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

    drifted = []
    for path in files:
        # Per automation: the idle flag differs by schedule.
        wanted = sync.prompt_text(path.parent.name)
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


def test_only_the_schedules_that_need_it_get_the_presence_check():
    """`--idle` refuses to start unless Nate has not touched the five-hour
    window. That proxy is worth paying for on a schedule firing while he might be
    working, and wrong on one firing at 2am: an evening ChatGPT session still
    shows in the window hours after he has gone to bed, so it would refuse
    legitimate overnight work."""
    def gate_command(text):
        # The prose explains the flag, so only the command line is evidence.
        return [ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("python3") and sync.GATE_LINE in ln][0]

    for path in sorted(sync.AUTOMATIONS.glob(sync.GLOB)):
        name = path.parent.name
        command = gate_command(sync.prompt_text(name))
        if sync.needs_presence_check(name):
            assert command.endswith("--idle"), name
        else:
            assert command.endswith("gate codex"), name


def test_a_schedule_restricted_to_hours_needs_no_presence_proxy(tmp_path, monkeypatch):
    """The clock already answers the question the proxy approximates.

    Derived from the schedule rather than a list of names: the old version keyed
    on `command-center-tickets-hourly`, so renaming that automation in the app
    would have silently switched the presence check off.
    """
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    def make(name, rrule):
        d = tmp_path / name
        d.mkdir()
        (d / "automation.toml").write_text('rrule = "RRULE:{}"\n'.format(rrule))
        return name

    allday = make("whenever", "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0,15,30,45")
    nights = make("nights", "FREQ=WEEKLY;BYDAY=SA;BYHOUR=2,3;BYMINUTE=0")

    assert sync.needs_presence_check(allday)
    assert not sync.needs_presence_check(nights)
    # Unknown schedules default to checking: refusing to compete with him is the
    # safe direction, assuming he is out is not.
    assert sync.needs_presence_check("does-not-exist")


def test_the_routine_still_separates_setup_notes_from_the_runtime_prompt():
    """`prompt_text` splits on `---`. If that separator were removed, the setup
    notes would be pasted into five live automations as if they were
    instructions."""
    assert sync.SEPARATOR in sync.ROUTINE.read_text()
    body = sync.prompt_text()
    assert "Paste this into" not in body
    assert body.startswith("# Codex routine")
