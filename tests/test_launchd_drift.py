"""The installed launchd plist must still match the one in the repo.

The repo copy was meant to be symlinked into `~/Library/LaunchAgents`, which
would have made drift impossible. **launchd refuses a symlinked plist** —
`Bootstrap failed: 5: Input/output error`, measured 2026-09-07 — so the installed
file is a copy after all, and a copy drifts. That is the surface #52 exists for,
reintroduced by an OS constraint rather than by choice.

So it gets the same treatment the Codex automations got: a test, not a note.
A prompt cannot enforce anything, and `routines/*.md` already require the suite
to pass before a merge, so a drifted plist now blocks a merge without anyone
remembering to look.

It **skips** where the plist is not installed — CI runners, a fresh clone — so it
is a real check on the Mac that runs the schedule and silent everywhere else.
A skip is honest: absence of the file is not evidence of no drift.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVIEWER_NAMES = [
    "com.nateprich.command-center-muse-review.plist",           # escalated, hourly
    "com.nateprich.command-center-muse-review-standard.plist",  # standard, /5
]
KEEPER_NAME = "com.nateprich.command-center-run-keeper.plist"
NAMES = REVIEWER_NAMES + [KEEPER_NAME]
LAUNCH_AGENTS = pathlib.Path.home() / "Library" / "LaunchAgents"


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


@pytest.mark.parametrize("name", NAMES)
def test_the_installed_plist_matches_the_repo(name):
    installed_path = LAUNCH_AGENTS / name
    repo_copy = ROOT / "launchd" / name
    if not installed_path.exists():
        pytest.skip("plist not installed here — nothing to compare")
    installed = installed_path.read_text()
    canonical = repo_copy.read_text()
    assert installed == canonical, (
        "the installed launchd plist has drifted from launchd/{}. "
        "Re-copy it: cp launchd/{} ~/Library/LaunchAgents/ && "
        "launchctl bootout gui/$(id -u)/{} && "
        "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{}".format(
            name, name, name[: -len(".plist")], name)
    )


@pytest.mark.parametrize("name", REVIEWER_NAMES)
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
    assert any(a.endswith("/scripts/muse-review") for a in args), args
    # Checked against the arguments, not the file text: the header explains the
    # TCC blocker and has to name `/Volumes/External SSD` to do so. Asserting on
    # the whole file made a correct comment fail a test about a path.
    assert not any(a.startswith("/Volumes/") for a in args), args


def test_the_two_schedules_do_not_collide():
    """Both run as agent `muse`, so two open runs at once make heartbeat
    attribution guesswork. Escalated fires at :07, standard on the quarter."""
    import plistlib

    def minutes(name):
        with (ROOT / "launchd" / name).open("rb") as handle:
            sched = plistlib.load(handle)["StartCalendarInterval"]
        if isinstance(sched, dict):
            sched = [sched]
        return {entry["Minute"] for entry in sched}

    assert not minutes(NAMES[0]) & minutes(NAMES[1])


def test_each_schedule_asks_for_its_own_tier_and_effort():
    """One runner serves both, so the arguments are the only thing that
    distinguishes them. Escalated gets max effort because that is the work where
    judgement matters most; standard gets high, which is cheaper."""
    import plistlib

    def args(name):
        with (ROOT / "launchd" / name).open("rb") as handle:
            return plistlib.load(handle)["ProgramArguments"][2:]

    assert args(NAMES[0]) == ["escalated", "max"]
    assert args(NAMES[1]) == ["standard", "high"]


def test_the_keeper_only_fast_forwards_the_run_clone():
    """The unattended job must never reconcile or mutate Nate's working tree."""
    import plistlib

    with (ROOT / "launchd" / KEEPER_NAME).open("rb") as handle:
        plist = plistlib.load(handle)
    assert plist["ProgramArguments"] == [
        "/usr/bin/git",
        "-C",
        "/Users/nateprich/.claude/command-center-run",
        "pull",
        "--ff-only",
    ]


def test_the_keeper_fires_at_least_as_often_as_the_fastest_routine():
    """A routine must not outrun the job responsible for refreshing its code."""
    import plistlib

    def maximum_gap(name):
        with (ROOT / "launchd" / name).open("rb") as handle:
            schedule = plistlib.load(handle)["StartCalendarInterval"]
        if isinstance(schedule, dict):
            schedule = [schedule]
        minutes = sorted(entry["Minute"] for entry in schedule)
        gaps = [right - left for left, right in zip(minutes, minutes[1:])]
        gaps.append(minutes[0] + 60 - minutes[-1])
        return max(gaps)

    fastest_routine_gap = min(maximum_gap(name) for name in REVIEWER_NAMES)
    assert maximum_gap(KEEPER_NAME) <= fastest_routine_gap
