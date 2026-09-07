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
NAME = "com.nateprich.command-center-muse-review.plist"
REPO_COPY = ROOT / "launchd" / NAME
INSTALLED = pathlib.Path.home() / "Library" / "LaunchAgents" / NAME


def test_the_repo_carries_the_canonical_plist():
    """Whatever the OS makes us install, the reviewable copy lives here."""
    assert REPO_COPY.is_file()
    text = REPO_COPY.read_text()
    assert "com.nateprich.command-center-muse-review" in text
    assert "scripts/muse-review" in text


def test_the_installed_plist_matches_the_repo():
    if not INSTALLED.exists():
        pytest.skip("plist not installed here — nothing to compare")
    installed = INSTALLED.read_text()
    canonical = REPO_COPY.read_text()
    assert installed == canonical, (
        "the installed launchd plist has drifted from launchd/{}. "
        "Re-copy it: cp launchd/{} ~/Library/LaunchAgents/ && "
        "launchctl bootout gui/$(id -u)/com.nateprich.command-center-muse-review && "
        "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{}".format(
            NAME, NAME, NAME)
    )


def test_the_plist_points_at_the_stable_path():
    """`/Users/nateprich/.claude/command-center` is a symlink to the working
    tree. The plist must use it rather than the resolved external-volume path,
    for the same reason `tests/test_guardrails.py` enforces everywhere else: the
    volume name is not stable and a moved checkout silently stops the schedule.
    """
    import plistlib

    with REPO_COPY.open("rb") as handle:
        plist = plistlib.load(handle)
    args = plist["ProgramArguments"]
    assert any(a.endswith("/scripts/muse-review") for a in args), args
    # Checked against the arguments, not the file text: the header explains the
    # TCC blocker and has to name `/Volumes/External SSD` to do so. Asserting on
    # the whole file made a correct comment fail a test about a path.
    assert not any(a.startswith("/Volumes/") for a in args), args
