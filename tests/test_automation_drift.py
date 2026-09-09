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
        if not sync.same_prompt(existing, wanted):
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
            assert command.endswith("--tier " + sync.tier_for(name)), name


def test_the_tier_follows_the_schedule_too():
    """All-day schedules take the cheap continuous lane; the hour-restricted ones
    run while Nate is asleep or at work, which is when the expensive engine can be
    given room. Escalated means *only* escalated — Sol idling beats Sol spending
    its quota on bounded work the cheap engine is already handling."""
    for path in sorted(sync.AUTOMATIONS.glob(sync.GLOB)):
        name = path.parent.name
        # From the derivation, not the presence check: the two were the same
        # function until 2026-09-07 and are deliberately not any more, because
        # suspending the proxy must not move a schedule's tier.
        want = "standard" if sync.fires_all_day(name) else "escalated"
        assert sync.tier_for(name) == want, name
        command = [ln.strip() for ln in sync.prompt_text(name).splitlines()
                   if ln.strip().startswith("python3") and sync.GATE_LINE in ln][0]
        assert "--tier " + want in command, name


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

    # Asserted against the derivation, not `needs_presence_check`, so this keeps
    # testing the rule while `PRESENCE_CHECK_ENABLED` is temporarily off. The
    # design has to come back intact rather than be rebuilt from memory.
    assert sync.fires_all_day(allday)
    assert not sync.fires_all_day(nights)
    # Unknown schedules default to checking: refusing to compete with him is the
    # safe direction, assuming he is out is not.
    assert sync.fires_all_day("does-not-exist")


def test_the_presence_switch_does_not_change_which_tier_a_schedule_works():
    """Both the tier and the idle flag are read off one fact — does this
    schedule fire all day. Suspending the presence proxy must not touch the
    tier: on 2026-09-07 an earlier version of the switch flipped the all-day
    fifteen-minute schedule from `standard` to `escalated`, which would have
    pointed the cheapest, most frequent poller at the riskiest work in the
    queue. Nothing would have reported it."""
    allday = "command-center-tickets-hourly"
    if not (sync.AUTOMATIONS / allday / "automation.toml").exists():
        pytest.skip("automations not present here")
    assert sync.tier_for(allday) == "standard"
    assert sync.fires_all_day(allday)


def test_the_routine_still_separates_setup_notes_from_the_runtime_prompt():
    """`prompt_text` splits on `---`. If that separator were removed, the setup
    notes would be pasted into five live automations as if they were
    instructions."""
    assert sync.SEPARATOR in sync.ROUTINE.read_text()
    body = sync.prompt_text()
    assert "Paste this into" not in body
    assert body.startswith("# Codex routine")


def test_the_trailing_newline_the_codex_app_strips_is_not_drift():
    """The app drops the final newline when it saves an automation.

    Compared byte-exactly, every automation reads as drifted forever. That is
    worse than no check at all: this is the only guard against a schedule
    running a stale pasted prompt, and one that always fires cannot be told from
    one that has caught something. Found 2026-09-07 with two of five automations
    reporting drift whose whole difference was a single `\n`.
    """
    wanted = sync.prompt_text("command-center-tickets-hourly")
    assert wanted.endswith("\n"), "prompt_text is expected to emit a trailing newline"
    assert sync.same_prompt(wanted.rstrip("\n"), wanted)


def test_same_prompt_still_catches_a_real_change():
    """Only *trailing* whitespace is forgiven. Everything else is drift."""
    wanted = sync.prompt_text("command-center-tickets-hourly")
    assert not sync.same_prompt(wanted.replace("--tier standard", "--tier escalated"),
                                wanted)
    assert not sync.same_prompt(" " + wanted, wanted)
    assert not sync.same_prompt(None, wanted)
