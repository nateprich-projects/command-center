"""The launchd keeper installs on change and records health plus readiness.

Besides the bounded, parseable machine-health line, the keeper writes a
changed routine into the installed Codex automations, copies and reloads
changed launchd plists, and records a readiness line (installed equals main,
and which files do not) beside the health line.  Every install path runs
against a fake home, never the Mac's real automations or LaunchAgents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import re
import subprocess


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run-keeper"

#: The derivation the keeper owns, stated independently so these tests can
#: judge its installs: tier and idle flag from the schedule, setup notes
#: split from the runtime prompt.
BEGIN_LINE = "funnel.py begin --agent codex --tier standard"
SEPARATOR = "\n---\n"

DAY_NAME = "command-center-test-day"
DAY_RRULE = "FREQ=HOURLY;INTERVAL=1;BYMINUTE=0"
NIGHT_NAME = "command-center-test-night"
NIGHT_RRULE = "FREQ=WEEKLY;BYDAY=SA;BYHOUR=2;BYMINUTE=1"
PUBLISHER_PLIST = "com.nateprich.command-center-funnel-publisher.plist"
PUBLISHER_LABEL = "com.nateprich.command-center-funnel-publisher"
KEEPER_PLIST = "com.nateprich.command-center-run-keeper.plist"
KEEPER_LABEL = "com.nateprich.command-center-run-keeper"
DEPLOY_PLIST = "com.nateprich.command-center-funnel-deploy.plist"
UNTOUCHED_PLIST = "com.nateprich.command-center-muse-review.plist"


def run_git(*args, cwd=None):
    return subprocess.run(
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def write_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def make_heartbeat_remote(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    run_git("init", "--bare", str(bare))
    run_git("init", str(seed))
    run_git("-C", str(seed), "config", "user.name", "test")
    run_git("-C", str(seed), "config", "user.email", "test@example.com")
    (seed / "README").write_text("main\n")
    run_git("-C", str(seed), "add", "README")
    run_git("-C", str(seed), "commit", "-m", "main")
    run_git("-C", str(seed), "branch", "-M", "main")
    run_git("-C", str(seed), "remote", "add", "origin", str(bare))
    run_git("-C", str(seed), "push", "-u", "origin", "main")
    run_git("--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main")

    run_git("-C", str(seed), "checkout", "--orphan", "heartbeat")
    (seed / "README").unlink()
    (seed / ".keep").write_text("heartbeat\n")
    run_git("-C", str(seed), "add", "-A")
    run_git("-C", str(seed), "commit", "-m", "heartbeat")
    run_git("-C", str(seed), "push", "origin", "heartbeat")
    run_git("-C", str(seed), "checkout", "main")

    checkout = tmp_path / "run"
    run_git("clone", str(bare), str(checkout))
    return bare, checkout


def write_tool_stubs(tools: Path) -> None:
    write_executable(
        tools / "ps",
        """
case "$*" in
  *lstart*) printf '%s\\n' 'Sat Sep 12 21:00:00 2026' ;;
  *-A*) printf '%s\\n' '501' '501' '502' ;;
  *) printf '%s\\n' '501' ;;
esac
""",
    )
    write_executable(tools / "sysctl", "printf '%s\\n' 2666\n")
    write_executable(
        tools / "uptime",
        "printf '%s\\n' '21:00 up 8 days, 1 user, load averages: 1.00 2.00 3.00'\n",
    )
    write_executable(
        tools / "pgrep",
        "case \"$*\" in *codex*) exit 0 ;; *muse*) exit 0 ;; *) exit 1 ;; esac\n",
    )


def write_launchctl_stub(path: Path, log: Path, fail_bootstrap: bool = False) -> None:
    body = 'echo "$@" >> "{}"\n'.format(log)
    if fail_bootstrap:
        body += 'if [ "$1" = "bootstrap" ]; then exit 5; fi\n'
    body += "exit 0\n"
    write_executable(path, body)


def test_keeper_appends_one_parseable_line_per_run(tmp_path):
    bare, checkout = make_heartbeat_remote(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    write_tool_stubs(tools)

    env = os.environ.copy()
    env.update(
        {
            "COMMAND_CENTER_RUN_REPO": str(checkout),
            "COMMAND_CENTER_SENTINEL_PS": str(tools / "ps"),
            "COMMAND_CENTER_SENTINEL_PGREP": str(tools / "pgrep"),
            "COMMAND_CENTER_SENTINEL_SYSCTL": str(tools / "sysctl"),
            "COMMAND_CENTER_SENTINEL_UPTIME": str(tools / "uptime"),
            "COMMAND_CENTER_SENTINEL_FILE": "sentinel.log",
        }
    )

    first = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
    second = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr

    content = run_git("--git-dir", str(bare), "show", "heartbeat:sentinel.log").stdout
    lines = content.splitlines()
    assert len(lines) == 2
    fields = [dict(part.split("=", 1) for part in line.split()) for line in lines]
    assert all(
        record == {
            "timestamp": "Sat_Sep_12_21:00:00_2026",
            "user_processes": "2",
            "maxprocperuid": "2666",
            "load_1": "1.00",
            "load_5": "2.00",
            "load_15": "3.00",
            "codex": "1",
            "claude": "0",
            "muse": "1",
        }
        for record in fields
    )


def make_install_remote(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A heartbeat remote whose main also carries what the keeper installs."""
    bare, checkout = make_heartbeat_remote(tmp_path)
    seed = tmp_path / "seed"
    # Only what the keeper installs: the routine it derives prompts from and
    # the plists it copies. The prompt logic is embedded in the keeper
    # script itself and needs nothing else from the checkout.
    dst = seed / "routines" / "codex-work.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes((ROOT / "routines" / "codex-work.md").read_bytes())
    for plist in sorted((ROOT / "launchd").glob("*.plist")):
        dst = seed / "launchd" / plist.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(plist.read_bytes())
    run_git("-C", str(seed), "add", "-A")
    run_git("-C", str(seed), "commit", "-m", "keeper install files")
    run_git("-C", str(seed), "push", "origin", "main")
    run_git("-C", str(checkout), "pull", "--ff-only")
    return bare, checkout, seed


def commit_and_push(seed: Path, message: str) -> None:
    run_git("-C", str(seed), "add", "-A")
    run_git("-C", str(seed), "commit", "-m", message)
    run_git("-C", str(seed), "push", "origin", "main")


def change_routine(seed: Path) -> None:
    path = seed / "routines" / "codex-work.md"
    text = path.read_text()
    assert "\n---\n" in text
    path.write_text(text + "\n<!-- keeper install test -->\n")


def change_plist(seed: Path, name: str) -> None:
    path = seed / "launchd" / name
    text = path.read_text()
    marker = (
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    )
    assert marker in text
    path.write_text(
        text.replace(marker, marker + "<!-- keeper install test -->\n", 1)
    )
    with path.open("rb") as handle:
        plistlib.load(handle)


def install_launchd_copies(seed: Path, home: Path, skip=()) -> Path:
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    for plist in sorted((seed / "launchd").glob("*.plist")):
        if plist.name in skip:
            continue
        (agents / plist.name).write_bytes(plist.read_bytes())
    return agents


def wanted_prompt(routine: Path, rrule: str) -> str:
    """The prompt the keeper derives for a lane firing on this rrule."""
    body = routine.read_text()
    title = body.splitlines()[0].strip()
    runtime = body.split(SEPARATOR, 1)[1].strip()
    all_day = "BYHOUR=" not in rrule
    tier = "standard" if all_day else "escalated"
    begin = BEGIN_LINE.replace("standard", tier)
    if all_day:
        begin += " --idle"
    return "{}\n\n{}\n".format(
        title, runtime.replace(BEGIN_LINE, begin, 1)
    )


def write_current_automation(home, routine, name, rrule):
    """An installed automation matching the given routine copy.

    The rrule is written first because the lane's tier, and so its prompt, is
    read back off it.
    """
    path = home / ".codex" / "automations" / name / "automation.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'rrule = "RRULE:{}"\n'.format(rrule)
        + "prompt = {}\nupdated_at = 1\n".format(
            json.dumps(wanted_prompt(routine, rrule))
        )
    )
    return path


def write_stale_automation(home, name, rrule, prompt="a stale prompt"):
    path = home / ".codex" / "automations" / name / "automation.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'rrule = "RRULE:{}"\nprompt = {}\nupdated_at = 1\n'.format(
            rrule, json.dumps(prompt)
        )
    )
    return path


def installed_prompt(path: Path):
    match = re.search(r'^prompt = (".*")$', path.read_text(), re.MULTILINE)
    return json.loads(match.group(1))


def keeper_env(checkout: Path, home: Path, tools: Path, launchctl: Path) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "COMMAND_CENTER_RUN_REPO": str(checkout),
            "COMMAND_CENTER_SENTINEL_PS": str(tools / "ps"),
            "COMMAND_CENTER_SENTINEL_PGREP": str(tools / "pgrep"),
            "COMMAND_CENTER_SENTINEL_SYSCTL": str(tools / "sysctl"),
            "COMMAND_CENTER_SENTINEL_UPTIME": str(tools / "uptime"),
            "COMMAND_CENTER_SENTINEL_FILE": "sentinel.log",
            "COMMAND_CENTER_KEEPER_LAUNCHCTL": str(launchctl),
            "HOME": str(home),
        }
    )
    return env


def run_keeper(checkout: Path, home: Path, tools: Path, launchctl: Path):
    return subprocess.run(
        [str(SCRIPT)],
        env=keeper_env(checkout, home, tools, launchctl),
        capture_output=True,
        text=True,
    )


def show_heartbeat_file(bare: Path, name: str) -> str:
    return run_git("--git-dir", str(bare), "show", "heartbeat:" + name).stdout


def parse_record(line: str) -> dict:
    return dict(part.split("=", 1) for part in line.split())


def one_readiness_record(bare: Path) -> dict:
    lines = show_heartbeat_file(bare, "keeper-readiness.log").splitlines()
    assert len(lines) == 1
    return parse_record(lines[0])


def make_tools(tmp_path: Path, fail_bootstrap: bool = False):
    tools = tmp_path / "tools"
    tools.mkdir()
    write_tool_stubs(tools)
    launchctl_log = tmp_path / "launchctl.log"
    write_launchctl_stub(tools / "launchctl", launchctl_log, fail_bootstrap)
    return tools, launchctl_log


def test_keeper_installs_prompts_when_the_routine_changed(tmp_path):
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    routine = checkout / "routines" / "codex-work.md"
    day = write_current_automation(home, routine, DAY_NAME, DAY_RRULE)
    night = write_current_automation(home, routine, NIGHT_NAME, NIGHT_RRULE)
    before = {DAY_NAME: installed_prompt(day), NIGHT_NAME: installed_prompt(night)}
    install_launchd_copies(seed, home)

    change_routine(seed)
    commit_and_push(seed, "routine change")

    tools, launchctl_log = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    routine = checkout / "routines" / "codex-work.md"
    for name, path, rrule in (
        (DAY_NAME, day, DAY_RRULE),
        (NIGHT_NAME, night, NIGHT_RRULE),
    ):
        assert installed_prompt(path) == wanted_prompt(routine, rrule)
        assert installed_prompt(path) != before[name]
        assert path.with_suffix(".toml.bak").is_file()
    # Each lane's tier and idle flag follow when it fires: the all-day lane
    # takes the cheap continuous tier with the presence proxy, the
    # hour-restricted one the escalated tier without it.
    day_begin = next(
        line for line in installed_prompt(day).splitlines()
        if "funnel.py begin" in line
    )
    night_begin = next(
        line for line in installed_prompt(night).splitlines()
        if "funnel.py begin" in line
    )
    assert "--tier standard" in day_begin
    assert " --idle`" in day_begin
    assert "--tier escalated" in night_begin
    assert " --idle" not in night_begin
    # Setup notes stay out of the installed prompt; only the runtime does.
    assert installed_prompt(day).startswith("# Codex routine")
    assert "Paste this into" not in installed_prompt(day)
    assert not launchctl_log.exists()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "yes",
        "files": "-",
        "reload_pending": "-",
    }
    assert len(show_heartbeat_file(bare, "sentinel.log").splitlines()) == 1


def test_keeper_treats_the_stripped_trailing_newline_as_current(tmp_path):
    """The Codex app strips the final newline when it saves an automation.
    That alone must not read as drift."""
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    routine = checkout / "routines" / "codex-work.md"
    day = write_current_automation(home, routine, DAY_NAME, DAY_RRULE)
    text = day.read_text()
    raw = re.search(r'^prompt = (".*")$', text, re.MULTILINE).group(1)
    assert json.loads(raw).endswith("\n")
    day.write_text(
        text.replace(raw, json.dumps(json.loads(raw).rstrip("\n")), 1)
    )
    install_launchd_copies(seed, home)

    tools, _ = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "yes",
        "files": "-",
        "reload_pending": "-",
    }


def test_keeper_reports_prompt_drift_it_did_not_cause(tmp_path):
    """Stale prompts without a routine change are reported, not installed.

    The pull moved nothing, so the keeper has no install trigger; the
    readiness record still names the drifted automations within one cycle.
    """
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    day = write_stale_automation(home, DAY_NAME, DAY_RRULE)
    night = write_stale_automation(home, NIGHT_NAME, NIGHT_RRULE)
    install_launchd_copies(seed, home)

    tools, _ = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert installed_prompt(day) == "a stale prompt"
    assert installed_prompt(night) == "a stale prompt"
    assert not day.with_suffix(".toml.bak").exists()
    assert not night.with_suffix(".toml.bak").exists()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "no",
        "plists": "yes",
        "files": "codex/{},codex/{}".format(DAY_NAME, NIGHT_NAME),
        "reload_pending": "-",
    }


def test_keeper_copies_and_reloads_only_the_changed_plist(tmp_path):
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    routine = checkout / "routines" / "codex-work.md"
    day = write_current_automation(home, routine, DAY_NAME, DAY_RRULE)
    agents = install_launchd_copies(seed, home)
    untouched = agents / UNTOUCHED_PLIST
    old_mtime = int(untouched.stat().st_mtime) - 100
    os.utime(untouched, (old_mtime, old_mtime))

    change_plist(seed, PUBLISHER_PLIST)
    commit_and_push(seed, "plist change")

    tools, launchctl_log = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert (agents / PUBLISHER_PLIST).read_bytes() == (
        checkout / "launchd" / PUBLISHER_PLIST
    ).read_bytes()
    assert int(untouched.stat().st_mtime) == old_mtime
    calls = launchctl_log.read_text()
    assert "bootout gui/" in calls and PUBLISHER_LABEL in calls
    assert "bootstrap gui/" in calls and PUBLISHER_PLIST in calls
    assert calls.count("bootout") == 1
    assert calls.count("bootstrap") == 1
    assert not day.with_suffix(".toml.bak").exists()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "yes",
        "files": "-",
        "reload_pending": "-",
    }


def test_keeper_records_a_failed_reload_and_keeps_both_records(tmp_path):
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    agents = install_launchd_copies(seed, home)

    change_plist(seed, PUBLISHER_PLIST)
    commit_and_push(seed, "plist change")

    tools, _ = make_tools(tmp_path, fail_bootstrap=True)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert (agents / PUBLISHER_PLIST).read_bytes() == (
        checkout / "launchd" / PUBLISHER_PLIST
    ).read_bytes()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "yes",
        "files": "-",
        "reload_pending": PUBLISHER_LABEL,
    }
    assert len(show_heartbeat_file(bare, "sentinel.log").splitlines()) == 1


def test_keeper_copies_but_never_reloads_itself(tmp_path):
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    agents = install_launchd_copies(seed, home)

    change_plist(seed, KEEPER_PLIST)
    commit_and_push(seed, "keeper plist change")

    tools, launchctl_log = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert (agents / KEEPER_PLIST).read_bytes() == (
        checkout / "launchd" / KEEPER_PLIST
    ).read_bytes()
    assert not launchctl_log.exists()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "yes",
        "files": "-",
        "reload_pending": KEEPER_LABEL,
    }


def test_keeper_leaves_a_never_installed_plist_alone(tmp_path):
    """First install belongs to the installer plus a console reload."""
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    agents = install_launchd_copies(seed, home, skip=(DEPLOY_PLIST,))

    change_plist(seed, DEPLOY_PLIST)
    commit_and_push(seed, "deploy plist change")

    tools, launchctl_log = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert not (agents / DEPLOY_PLIST).exists()
    assert not launchctl_log.exists()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "yes",
        "files": "-",
        "reload_pending": "-",
    }


def test_keeper_reports_plist_drift_without_recoping_it(tmp_path):
    """A locally edited plist without a pull change is reported, not fixed."""
    bare, checkout, seed = make_install_remote(tmp_path)
    home = tmp_path / "home"
    agents = install_launchd_copies(seed, home)
    drifted_path = agents / PUBLISHER_PLIST
    drifted_path.write_text(
        drifted_path.read_text() + "<!-- local drift -->\n"
    )

    tools, launchctl_log = make_tools(tmp_path)
    result = run_keeper(checkout, home, tools, tools / "launchctl")
    assert result.returncode == 0, result.stderr

    assert drifted_path.read_text().endswith("<!-- local drift -->\n")
    assert not launchctl_log.exists()
    assert one_readiness_record(bare) == {
        "timestamp": "Sat_Sep_12_21:00:00_2026",
        "prompts": "yes",
        "plists": "no",
        "files": "launchd/" + PUBLISHER_PLIST,
        "reload_pending": "-",
    }
