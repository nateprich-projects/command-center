"""The repo's launchd plists are canonical, well-formed, and correctly scheduled.

The repo copy was meant to be symlinked into `~/Library/LaunchAgents`, which
would have made drift impossible. **launchd refuses a symlinked plist** —
`Bootstrap failed: 5: Input/output error`, measured 2026-09-07 — so the installed
file is a copy after all, and a copy drifts.

That installed-versus-repo comparison used to live here, skipping where the
plist was not installed. It moved to the run-keeper's readiness record (#821),
which compares every run on the schedule host itself — the only machine where
the installed copies exist. This file keeps the repo-side invariants: the
canonical copies, their shape, their schedules, and the installer that copies
them. Nothing here reads the Mac's installed state.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVIEWER_NAMES = [
    "com.nateprich.command-center-muse-review.plist",           # escalated, hourly
    "com.nateprich.command-center-muse-review-standard.plist",  # standard, /5
]
#: Muse's implement schedules, retired when implementation moved to Codex
#: (Nate, 2026-09-22, #1315, #1322). `scripts/muse-implement` stays as the
#: reversal path; restoring these files is how Muse would implement again.
RETIRED_IMPLEMENTER_NAMES = [
    "com.nateprich.command-center-muse-implement.plist",
    "com.nateprich.command-center-muse-implement-standard.plist",
]
MUSE_SCHEDULE_NAMES = REVIEWER_NAMES
KEEPER_NAME = "com.nateprich.command-center-run-keeper.plist"
#: The Remote Control listener. Not a schedule; see the carve-out in `AGENTS.md`.
REMOTE_CONTROL_NAME = "com.nateprich.command-center-remote-control.plist"
#: The funnel snapshot publisher (#652). A poll loop, not a routine schedule.
PUBLISHER_NAME = "com.nateprich.command-center-funnel-publisher.plist"
#: The dashboard auto-deploy (#653). A five-minute poll in the keeper mould.
DEPLOY_NAME = "com.nateprich.command-center-funnel-deploy.plist"
# The FF runtime poller; a ten-minute deploy window matches its fastest tick.
FF_DEPLOY_NAME = "com.nateprich.command-center-ff-deploy.plist"
#: The daily outcome-record derivation (#1288). A once-a-day Python run, not a
#: routine schedule; outcomes.py had no scheduler at all before it.
OUTCOMES_NAME = "com.nateprich.command-center-outcomes-derive.plist"
NAMES = MUSE_SCHEDULE_NAMES + [
    KEEPER_NAME, REMOTE_CONTROL_NAME, PUBLISHER_NAME, DEPLOY_NAME,
    FF_DEPLOY_NAME, OUTCOMES_NAME]
INSTALL_NAMES = MUSE_SCHEDULE_NAMES + [
    KEEPER_NAME, PUBLISHER_NAME, DEPLOY_NAME, FF_DEPLOY_NAME, OUTCOMES_NAME]


def console_reload_hint(name):
    label = name[: -len(".plist")]
    return (
        "Run the reload from a Terminal in the logged-in console (Aqua) session, "
        "not an automation shell: cp launchd/{} ~/Library/LaunchAgents/ && "
        "launchctl bootout gui/$(id -u)/{} && "
        "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{} && "
        "launchctl print gui/$(id -u)/{}"
    ).format(name, label, name, label)


@pytest.mark.parametrize("name", NAMES)
def test_the_repo_carries_the_canonical_plist(name):
    """Whatever the OS makes us install, the reviewable copy lives here."""
    repo_copy = ROOT / "launchd" / name
    assert repo_copy.is_file()


@pytest.mark.parametrize("name", NAMES)
def test_the_plist_is_well_formed_xml(name):
    """`plutil -lint` accepts a double hyphen inside a comment and `plistlib`
    rejects it, so a plist can lint clean and still be unparseable. Both files
    had that on 2026-09-07, written by an agent using `--` as an em dash."""
    import plistlib

    with (ROOT / "launchd" / name).open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["Label"] == name[: -len(".plist")]


def test_drift_hint_requires_the_console_session():
    hint = console_reload_hint(KEEPER_NAME)

    assert "logged-in console (Aqua) session" in hint
    assert "not an automation shell" in hint
    assert "launchctl print gui/$(id -u)/com.nateprich.command-center-run-keeper" in hint


@pytest.mark.parametrize("name", MUSE_SCHEDULE_NAMES)
def test_the_plist_points_at_the_stable_path(name):
    """`/Users/nateprich/.claude/command-center` is a symlink to the working
    tree. The plist must use it rather than the resolved external-volume path,
    for the same reason `tests/test_guardrails.py` enforces everywhere else: the
    volume name is not stable and a moved checkout silently stops the schedule.
    """
    import plistlib

    with (ROOT / "launchd" / name).open("rb") as handle:
        plist = plistlib.load(handle)
    args = plist["ProgramArguments"]
    assert args[0] == "/bin/bash"
    # Both review tiers run the engine: standard since 2026-09-20 (#813),
    # escalated since 2026-09-21 on Nate's override of #806's shadow gate.
    # The shadow plist is retired with them, and the implement plists
    # since 2026-09-22 (#1322).
    script = "muse-review-engine"
    assert any(a.endswith("/scripts/{}".format(script)) for a in args), args
    # Checked against the arguments, not the file text: the header explains the
    # TCC blocker and has to name `/Volumes/External SSD` to do so. Asserting on
    # the whole file made a correct comment fail a test about a path.
    assert not any(a.startswith("/Volumes/") for a in args), args


def test_the_keeper_points_at_the_stable_wrapper():
    """The keeper must run the checked-in wrapper from the maintained clone."""
    import plistlib

    with (ROOT / "launchd" / KEEPER_NAME).open("rb") as handle:
        args = plistlib.load(handle)["ProgramArguments"]

    assert args == [
        "/bin/bash",
        "/Users/nateprich/.claude/command-center-run/scripts/run-keeper",
    ]


def test_the_escalated_reviewer_polls_every_fifteen_minutes_while_794_clears():
    """Both review schedules run as agent `muse`. The escalated one polls on a
    fixed interval while #794's escalated tickets clear (Nate, 2026-09-14,
    #859), matching the implementer (#831); the standard one keeps its
    calendar slots. Overlapping fires of the two labels are accepted for the
    duration: launchd starts no second instance of either label while one
    runs, and heartbeat attribution carries the tier. When #794 closes this
    reverts to the two-hourly :07 calendar schedule and the collision test it
    replaced: escalated minutes disjoint from standard minutes."""
    import plistlib

    with (ROOT / "launchd" / NAMES[0]).open("rb") as handle:
        escalated = plistlib.load(handle)
    with (ROOT / "launchd" / NAMES[1]).open("rb") as handle:
        standard = plistlib.load(handle)

    assert escalated["StartInterval"] == 900
    assert "StartCalendarInterval" not in escalated
    assert "StartCalendarInterval" in standard


def test_each_schedule_asks_for_its_own_tier_and_effort():
    """One runner serves both, so the arguments are the only thing that
    distinguishes them. Escalated gets max effort because that is the work where
    judgement matters most.

    Both review schedules run `max` (Nate, 2026-09-22, #1315): judgement
    runs on the private model at max effort, and the review tiers now differ
    in queue and cadence, not in effort. Muse no longer implements (#1322).
    History: standard ran `high` from #1191 (Nate, 2026-09-21, #1189); the
    standard reviewer briefly took the engine's default `max` at its
    2026-09-20 cutover.
    """
    import plistlib

    def args(name):
        with (ROOT / "launchd" / name).open("rb") as handle:
            return plistlib.load(handle)["ProgramArguments"][2:]

    assert args(NAMES[0]) == ["escalated", "max"]
    assert args(NAMES[1]) == ["standard", "max"]


def test_muse_has_no_implement_schedule():
    """Implementation moved to Codex (Nate, 2026-09-22, #1315). The keeper
    never uninstalls a plist removed from the repository, so the installed
    copies are moved out of LaunchAgents by hand (#1323); the runner script
    stays as the way back."""
    for name in RETIRED_IMPLEMENTER_NAMES:
        assert not (ROOT / "launchd" / name).exists(), name
    assert (ROOT / "scripts" / "muse-implement").exists()


def test_the_installer_copies_all_launchd_plists(tmp_path):
    """The launchd copies are refreshed with the rest of the installation."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts" / "install.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "logged-in console (Aqua) session" in result.stdout
    assert "launchd was not reloaded" in result.stdout
    assert "Bootstrap failed: 5" in result.stdout
    for name in INSTALL_NAMES:
        installed = tmp_path / "Library" / "LaunchAgents" / name
        assert installed.read_text() == (ROOT / "launchd" / name).read_text()


def test_the_keeper_runs_the_read_only_wrapper():
    """The unattended job must never run an agent CLI or touch Nate's tree."""
    import plistlib

    with (ROOT / "launchd" / KEEPER_NAME).open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"] == [
        "/bin/bash",
        "/Users/nateprich/.claude/command-center-run/scripts/run-keeper",
    ]


def test_the_keeper_fires_at_least_as_often_as_the_fastest_routine():
    """A routine must not outrun the job responsible for refreshing its code."""
    import plistlib

    def maximum_gap(name):
        with (ROOT / "launchd" / name).open("rb") as handle:
            plist = plistlib.load(handle)
        if "StartInterval" in plist:
            # A fixed interval (#831, #859) fires every N seconds; its gap is
            # that interval in minutes.
            return plist["StartInterval"] / 60
        schedule = plist["StartCalendarInterval"]
        if isinstance(schedule, dict):
            schedule = [schedule]
        minutes = sorted(entry["Minute"] for entry in schedule)
        gaps = [right - left for left, right in zip(minutes, minutes[1:])]
        gaps.append(minutes[0] + 60 - minutes[-1])
        return max(gaps)

    fastest_routine_gap = min(maximum_gap(name) for name in REVIEWER_NAMES)
    assert maximum_gap(KEEPER_NAME) <= fastest_routine_gap


def test_the_publisher_runs_the_run_clone_copy():
    """The unattended job must run the maintained checkout's publisher under
    the pinned interpreter, never Nate's working tree."""
    import plistlib

    with (ROOT / "launchd" / PUBLISHER_NAME).open("rb") as handle:
        args = plistlib.load(handle)["ProgramArguments"]

    assert args == [
        "/usr/bin/python3",
        "/Users/nateprich/.claude/command-center-run/publisher.py",
    ]
    assert not any(a.startswith("/Volumes/") for a in args), args


def test_the_publisher_polls_every_minute():
    """The plan polls Cloudflare every minute; a poll loop uses StartInterval
    rather than the keeper's 60-entry calendar schedule."""
    import plistlib

    with (ROOT / "launchd" / PUBLISHER_NAME).open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["StartInterval"] == 60


def test_the_ff_deployer_polls_every_ten_minutes_from_the_stable_checkout():
    """The FF deployer follows the runtime cadence without a machine path."""
    import plistlib

    with (ROOT / "launchd" / FF_DEPLOY_NAME).open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["StartInterval"] == 600
    assert plist["ProgramArguments"][:2] == ["/bin/sh", "-c"]
    command = plist["ProgramArguments"][2]
    assert '"$HOME/.claude/command-center/ff_deploy.py"' in command
    assert "/Users/" not in command
    assert "launchctl" not in command
    assert "StartCalendarInterval" not in plist


def test_the_publisher_logs_to_its_own_files():
    """Publisher failures must land in the publisher's logs, never in an
    agent run's output."""
    import plistlib

    with (ROOT / "launchd" / PUBLISHER_NAME).open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["StandardOutPath"] == (
        "/Users/nateprich/Library/Logs/command-center-funnel-publisher.log"
    )
    assert plist["StandardErrorPath"] == (
        "/Users/nateprich/Library/Logs/command-center-funnel-publisher.err.log"
    )


def test_the_deploy_runs_the_run_clone_copy():
    """The unattended job must run the maintained checkout's deployer under
    the pinned interpreter, never Nate's working tree."""
    import plistlib

    with (ROOT / "launchd" / DEPLOY_NAME).open("rb") as handle:
        args = plistlib.load(handle)["ProgramArguments"]

    assert args == [
        "/usr/bin/python3",
        "/Users/nateprich/.claude/command-center-run/dashboard_deploy.py",
    ]
    assert not any(a.startswith("/Volumes/") for a in args), args


def test_the_deploy_polls_every_minute():
    """A merged dashboard/ change goes live about a minute later.

    Nate, 2026-09-16: nine minutes from merge to live was too long, and five
    of those were the old calendar grid (the other four were a lost tick).
    A fixed interval is right for a poll: drift of seconds does not matter,
    launchd starts no second instance while one runs, and the script's own
    lock remains the real guard.
    """
    import plistlib

    with (ROOT / "launchd" / DEPLOY_NAME).open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["StartInterval"] == 60
    assert "StartCalendarInterval" not in plist


def test_the_deploy_logs_to_its_own_files():
    """Deploy failures must land in the deployer's logs, never in an
    agent run's output."""
    import plistlib

    with (ROOT / "launchd" / DEPLOY_NAME).open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["StandardOutPath"] == (
        "/Users/nateprich/Library/Logs/command-center-funnel-deploy.log"
    )
    assert plist["StandardErrorPath"] == (
        "/Users/nateprich/Library/Logs/command-center-funnel-deploy.err.log"
    )


def test_the_outcomes_derivation_runs_daily_and_scans_every_member_repo():
    """#1288: `outcomes.py derive` had no scheduler between 2026-09-13 and this
    job, so every signal drawn from `outcomes.jsonl` was as stale as the last
    hand-run while still rendering as current.

    Two things are asserted rather than the schedule alone. It must carry **no**
    repository argument: the repositories come from the funnel topic at run time
    (`--all-members`), because AGENTS.md's "GitHub is the state" rules out a
    second list kept in a plist, and `funnel.member_repos` calls itself "never
    an allowlist". And it must be a calendar schedule: daily is a calendar
    notion, and `StartInterval` would restart the run 86,400 seconds after
    whenever launchd last felt like starting it.
    """
    import plistlib

    with (ROOT / "launchd" / OUTCOMES_NAME).open("rb") as handle:
        plist = plistlib.load(handle)

    args = plist["ProgramArguments"]
    assert args[0] == "/usr/bin/python3"
    assert args[1].endswith("/outcomes.py")
    assert args[2] == "derive"
    assert "--all-members" in args
    assert "--repo" not in args, args
    assert not any(a.startswith("/Volumes/") for a in args), args

    assert plist["StartCalendarInterval"] == {"Hour": 3, "Minute": 20}
    assert "StartInterval" not in plist
