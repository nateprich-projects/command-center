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
import json
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
                if sync.GATE_LINE in ln][0]

    for path in sorted(sync.AUTOMATIONS.glob(sync.GLOB)):
        name = path.parent.name
        command = gate_command(sync.prompt_text(name))
        if sync.needs_presence_check(name):
            assert " --idle`" in command, name
        else:
            assert "--tier " + sync.tier_for(name) in command, name
            assert " --idle`" not in command, name


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
                   if sync.GATE_LINE in ln][0]
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
    five-minute schedule from `standard` to `escalated`, which would have
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


def test_derived_lane_prompts_do_not_carry_a_routine_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    for name, rrule in (
        ("all-day", "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0"),
        ("overnight", "FREQ=WEEKLY;BYDAY=SA;BYHOUR=2;BYMINUTE=0"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "automation.toml").write_text(
            'rrule = "RRULE:{}"\n'.format(rrule)
        )

    for name in ("all-day", "overnight"):
        command = next(
            line.strip()
            for line in sync.prompt_text(name).splitlines()
            if sync.GATE_LINE in line
        )
        assert "--routine-sha" not in command


def _write_fixture_automation(root, name, prompt):
    directory = root / name
    directory.mkdir()
    (directory / "automation.toml").write_text(
        'rrule = "RRULE:FREQ=HOURLY;BYMINUTE=0"\n'
        "prompt = {}\n"
        "updated_at = 1\n".format(json.dumps(prompt))
    )
    return directory / "automation.toml"


def test_write_attempts_remaining_schedules_after_an_unwritable_one(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    bad = "command-center-a-unwritable"
    good = "command-center-z-updated"
    _write_fixture_automation(tmp_path, bad, "stale")
    good_path = _write_fixture_automation(tmp_path, good, "stale")

    attempted = []
    real_copy2 = sync.shutil.copy2

    def copy2(source, destination):
        attempted.append(pathlib.Path(source).parent.name)
        if pathlib.Path(source).parent.name == bad:
            raise OSError("protected schedule")
        return real_copy2(source, destination)

    monkeypatch.setattr(sync.shutil, "copy2", copy2)

    assert sync.main([]) == 1
    assert attempted == [bad, good]
    assert sync.current(good_path.read_text())[1] == sync.prompt_text(good)
    output = capsys.readouterr()
    assert "unwritable" in output.err
    assert bad in output.out
    assert good in output.out
    assert "Stale prompts remain:" in output.out


def test_write_summary_names_updated_current_and_unwritable_outcomes(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    current = "command-center-a-current"
    updated = "command-center-b-updated"
    unwritable = "command-center-c-unwritable"
    _write_fixture_automation(tmp_path, current, sync.prompt_text(current))
    _write_fixture_automation(tmp_path, updated, "stale")
    _write_fixture_automation(tmp_path, unwritable, "stale")

    real_copy2 = sync.shutil.copy2

    def copy2(source, destination):
        if pathlib.Path(source).parent.name == unwritable:
            raise OSError("protected schedule")
        return real_copy2(source, destination)

    monkeypatch.setattr(sync.shutil, "copy2", copy2)

    assert sync.main([]) == 1
    output = capsys.readouterr().out
    summary = output.split("Sync summary:\n", 1)[1].split(
        "\nStale prompts remain:", 1
    )[0]
    lines = summary.splitlines()
    assert len(lines) == 3  # one line per schedule
    assert any("already current" in line and current in line for line in lines)
    assert any("updated" in line and updated in line for line in lines)
    assert any("unwritable" in line and unwritable in line for line in lines)


def test_write_exit_is_zero_only_when_all_fixture_prompts_are_current(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    current = "command-center-a-current"
    stale = "command-center-b-stale"
    _write_fixture_automation(tmp_path, current, sync.prompt_text(current))
    stale_path = _write_fixture_automation(tmp_path, stale, "stale")

    assert sync.main([]) == 0
    capsys.readouterr()
    stale_path.write_text(
        stale_path.read_text().replace(
            json.dumps(sync.prompt_text(stale)), json.dumps("stale"), 1
        )
    )

    def fail_copy2(source, destination):
        raise OSError("protected schedule")

    monkeypatch.setattr(sync.shutil, "copy2", fail_copy2)
    assert sync.main([]) == 1
    output = capsys.readouterr().out
    assert "Stale prompts remain:" in output
    assert stale in output


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


def test_the_installed_lanes_fire_in_distinct_minutes():
    """Two lanes in the same minute are handed the same top ticket (#667)."""
    files = sorted(sync.AUTOMATIONS.glob(sync.GLOB))
    if not files:
        pytest.skip("no Codex automations on this machine — nothing to compare")
    problems = sync.same_minute_lanes(path.parent.name for path in files)
    assert not problems, (
        "Codex lanes are not staggered one minute apart: {}. Edit each rrule's "
        "BYMINUTE by hand (#678).".format(problems)
    )


def _write_rrule(root, name, rrule, with_prompt=False):
    """The rrule is written first: the lane's tier, and so its prompt, is read
    back off it."""
    path = root / name / "automation.toml"
    path.parent.mkdir()
    path.write_text('rrule = "RRULE:{}"\n'.format(rrule))
    if with_prompt:
        with path.open("a") as fh:
            fh.write("prompt = {}\n".format(json.dumps(sync.prompt_text(name))))


def test_check_fails_when_two_lanes_share_a_fire_minute(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    hourly = "command-center-a-hourly"
    nights = "command-center-b-nights"
    _write_rrule(tmp_path, hourly, "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0,20,40", with_prompt=True)
    _write_rrule(tmp_path, nights, "FREQ=WEEKLY;BYDAY=SA;BYHOUR=2;BYMINUTE=0;BYSECOND=0", with_prompt=True)

    assert sync.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "SAME-MINUTE {} and {} both fire at minute 0".format(hourly, nights) in output
    assert "DRIFTED" not in output


def test_check_passes_on_the_one_minute_stagger(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    lanes = [("command-center-0-hourly", "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0,20,40")]
    lanes += [
        ("command-center-{}-window".format(n),
         "FREQ=WEEKLY;BYDAY=MO;BYHOUR={};BYMINUTE={};BYSECOND=0".format(n, n))
        for n in range(1, 5)
    ]
    for name, rrule in lanes:
        _write_rrule(tmp_path, name, rrule, with_prompt=True)

    assert sync.main(["--check"]) == 0
    assert "SAME-MINUTE" not in capsys.readouterr().out
    assert [sync.fire_minutes(name) for name, _ in lanes][1:] == [
        frozenset({n}) for n in range(1, 5)
    ]


def test_an_unreadable_minute_is_not_a_verified_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "AUTOMATIONS", tmp_path)
    _write_rrule(tmp_path, "no-minute", "FREQ=WEEKLY;BYDAY=SA;BYHOUR=2")
    _write_rrule(tmp_path, "minute-seven", "FREQ=HOURLY;BYMINUTE=7")

    assert sync.fire_minutes("no-minute") is None
    assert sync.fire_minutes("does-not-exist") is None
    assert sync.same_minute_lanes(["no-minute", "minute-seven"]) == [
        ("no-minute", None, None)
    ]


def test_hourly_lane_documentation_matches_its_actual_cadence():
    source = (ROOT / "scripts" / "sync_codex_automations.py").read_text()
    assert "The `-hourly`" in source
    assert "historical" in source
    assert "every five minutes, 12 times an" in source
    assert "every fifteen minutes" not in source
