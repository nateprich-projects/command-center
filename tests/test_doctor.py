"""The local, read-only checks behind ``funnel doctor``."""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def install_fixture(tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "skills" / "funnel").mkdir(parents=True)
    (checkout / "skills" / "funnel" / "SKILL.md").write_text("funnel")
    (checkout / "statusline.sh").write_text("#!/bin/sh\n")

    claude = tmp_path / "claude"
    (claude / "skills").mkdir(parents=True)
    os.symlink(checkout, claude / "command-center")
    os.symlink(checkout / "statusline.sh", claude / "statusline.sh")
    os.symlink(checkout / "skills" / "funnel", claude / "skills" / "funnel")
    (claude / "settings.json").write_text(json.dumps({
        "statusLine": {
            "type": "command",
            "command": "~/.claude/statusline.sh",
        }
    }))
    return checkout, claude


def test_all_local_checks_pass_and_discover_every_skill(tmp_path):
    checkout, claude = install_fixture(tmp_path)
    (checkout / "skills" / "shape").mkdir()
    os.symlink(checkout / "skills" / "shape", claude / "skills" / "shape")

    checks = funnel.doctor_checks(claude_dir=claude, checkout_root=checkout)

    assert [check.name for check in checks] == ["install symlinks", "settings.json"]
    assert all(check.ok for check in checks)
    assert "4 links" in checks[0].found


@pytest.mark.parametrize("kind", ["missing", "real file", "outside", "dangling"])
def test_each_bad_link_is_reported_with_an_install_fix(tmp_path, kind):
    checkout, claude = install_fixture(tmp_path)
    link = claude / "statusline.sh"
    link.unlink()

    if kind == "real file":
        link.write_text("not a link")
    elif kind == "outside":
        outside = tmp_path / "outside.sh"
        outside.write_text("outside")
        os.symlink(outside, link)
    elif kind == "dangling":
        os.symlink(tmp_path / "gone.sh", link)

    result = funnel.check_symlinks(claude_dir=claude, checkout_root=checkout)

    assert not result.ok
    assert "statusline.sh" in result.found
    assert result.fix == funnel.INSTALL_FIX


def test_a_second_skill_is_checked_without_doctor_changes(tmp_path):
    checkout, claude = install_fixture(tmp_path)
    (checkout / "skills" / "shape").mkdir()

    result = funnel.check_symlinks(claude_dir=claude, checkout_root=checkout)

    assert not result.ok
    assert "skills/shape" in result.found


def test_an_unmounted_volume_names_the_volume_and_does_not_suggest_a_rerun(tmp_path):
    root = pathlib.Path("/Volumes/External SSD/Repositories/command-center")

    result = funnel.check_symlinks(claude_dir=tmp_path / "claude",
                                   checkout_root=root)

    assert not result.ok
    assert "External SSD" in result.found
    assert "unmounted" in result.found
    assert "re-run" not in result.fix


def test_settings_missing_has_the_installer_fix(tmp_path):
    _, claude = install_fixture(tmp_path)
    (claude / "settings.json").unlink()

    result = funnel.check_settings(claude)

    assert not result.ok
    assert "missing" in result.found
    assert result.fix == funnel.INSTALL_FIX


def test_settings_invalid_json_has_a_different_fix(tmp_path):
    _, claude = install_fixture(tmp_path)
    (claude / "settings.json").write_text("{not valid json")

    result = funnel.check_settings(claude)

    assert not result.ok
    assert "not valid JSON" in result.found
    assert "install.sh" not in result.fix


@pytest.mark.parametrize(
    "payload, phrase",
    [
        ({}, "missing"),
        ({"statusLine": {"type": "command", "command": "elsewhere"}}, "points"),
    ],
)
def test_settings_missing_or_wrong_statusline_is_distinct(tmp_path, payload, phrase):
    _, claude = install_fixture(tmp_path)
    (claude / "settings.json").write_text(json.dumps(payload))

    result = funnel.check_settings(claude)

    assert not result.ok
    assert phrase in result.found
    assert result.fix == funnel.INSTALL_FIX


def test_doctor_runs_after_a_broken_symlink_check(tmp_path):
    checkout, claude = install_fixture(tmp_path)
    (claude / "command-center").unlink()

    checks = funnel.doctor_checks(claude_dir=claude, checkout_root=checkout)

    assert len(checks) == 2
    assert not checks[0].ok
    assert checks[1].ok


def test_main_doctor_does_not_load_github(monkeypatch):
    def fail_if_called():
        raise AssertionError("doctor must run before load_items")

    monkeypatch.setattr(funnel, "load_items", fail_if_called)
    monkeypatch.setattr(
        funnel, "doctor_checks", lambda: [funnel.Check("local", True, "ok", "")])

    assert funnel.main(["doctor"]) == 0


def test_main_doctor_returns_one_when_any_check_is_broken(monkeypatch):
    monkeypatch.setattr(
        funnel, "doctor_checks", lambda: [funnel.Check("local", False, "broken", "fix")])

    assert funnel.main(["doctor"]) == 1


def test_doctor_renderer_puts_the_fix_only_on_broken_lines(capsys):
    funnel.render_checks([
        funnel.Check("good", True, "found it", ""),
        funnel.Check("bad", False, "missing it", "run the fix"),
    ])

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "good: found it",
        "bad: missing it — fix: run the fix",
    ]
