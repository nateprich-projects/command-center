"""The local, read-only checks behind ``funnel doctor``."""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def stub_github_checks(monkeypatch):
    """Keep local-path tests local; GitHub checks have their own seams below."""
    for function_name, check_name in (
        ("check_auth_scope", "gh auth"),
        ("check_project_fields", "Project fields"),
        ("check_topic", "command-center topic"),
    ):
        monkeypatch.setattr(
            funnel, function_name,
            lambda name=check_name: funnel.Check(name, True, "ok", ""),
        )


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


def test_all_local_checks_pass_and_discover_every_skill(tmp_path, monkeypatch):
    checkout, claude = install_fixture(tmp_path)
    (checkout / "skills" / "shape").mkdir()
    os.symlink(checkout / "skills" / "shape", claude / "skills" / "shape")
    stub_github_checks(monkeypatch)

    checks = funnel.doctor_checks(claude_dir=claude, checkout_root=checkout)

    assert [check.name for check in checks] == [
        "install symlinks", "settings.json", "gh auth", "Project fields",
        "command-center topic",
    ]
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


def test_doctor_runs_after_a_broken_symlink_check(tmp_path, monkeypatch):
    checkout, claude = install_fixture(tmp_path)
    (claude / "command-center").unlink()
    stub_github_checks(monkeypatch)

    checks = funnel.doctor_checks(claude_dir=claude, checkout_root=checkout)

    assert len(checks) == 5
    assert not checks[0].ok
    assert checks[1].ok


# -- GitHub wiring ------------------------------------------------------------


def auth_payload(scopes):
    return {"hosts": {"github.com": [{
        "state": "success",
        "active": True,
        "login": "nateprich",
        "scopes": scopes,
    }]}}


def project_payload(status=None, klass=None, lock=True, extra_status=None):
    status = funnel.STAGES if status is None else status
    klass = funnel.LADDER if klass is None else klass
    fields = [
        {"name": "Status", "options": [{"name": option} for option in status]},
        {"name": "Class", "options": [{"name": option} for option in klass]},
    ]
    if extra_status:
        fields[0]["options"].append({"name": extra_status})
    if lock:
        fields.append({"name": funnel.LOCK_FIELD})
    return {"user": {"projectV2": {"fields": {"nodes": fields}}}}


def test_auth_with_project_scope_passes_and_reports_the_scopes(monkeypatch):
    monkeypatch.setattr(funnel, "gh_auth_status",
                        lambda: auth_payload("repo, project, read:org"))

    result = funnel.check_auth_scope()

    assert result.ok
    assert "project" in result.found
    assert "repo" in result.found


def test_auth_without_project_scope_names_the_refresh_fix(monkeypatch):
    monkeypatch.setattr(funnel, "gh_auth_status",
                        lambda: auth_payload(["repo", "read:org"]))

    result = funnel.check_auth_scope()

    assert not result.ok
    assert "missing project scope" in result.found
    assert result.fix == "gh auth refresh -s project"


def test_auth_without_an_account_names_the_login_fix(monkeypatch):
    monkeypatch.setattr(funnel, "gh_auth_status", lambda: {"hosts": {}})

    result = funnel.check_auth_scope()

    assert not result.ok
    assert "not authenticated" in result.found
    assert result.fix == "gh auth login"


def test_auth_command_failure_is_reported_not_raised(monkeypatch):
    def fail():
        raise funnel.GitHubError("could not resolve github.com")

    monkeypatch.setattr(funnel, "gh_auth_status", fail)

    result = funnel.check_auth_scope()

    assert not result.ok
    assert "gh auth status failed" in result.found
    assert result.fix


def test_all_project_fields_and_options_pass(monkeypatch):
    monkeypatch.setattr(funnel, "gh_graphql",
                        lambda query, **variables: project_payload())

    result = funnel.check_project_fields()

    assert result.ok
    assert "Status" in result.found
    assert funnel.LOCK_FIELD in result.found


def test_missing_project_class_field_is_distinct_from_missing_options(monkeypatch):
    # An empty option list still means the field exists; remove it for the
    # field-missing case itself.
    payload = project_payload()
    payload["user"]["projectV2"]["fields"]["nodes"].pop(1)
    monkeypatch.setattr(funnel, "gh_graphql",
                        lambda query, **variables: payload)

    result = funnel.check_project_fields()

    assert not result.ok
    assert "missing field Class" in result.found


def test_missing_project_class_option_names_the_option(monkeypatch):
    missing = [option for option in funnel.LADDER if option != "Broken"]
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: project_payload(klass=missing),
    )

    result = funnel.check_project_fields()

    assert not result.ok
    assert "Class is missing option Broken" in result.found
    assert "missing field Class" not in result.found


def test_extra_status_option_does_not_break_the_project_check(monkeypatch):
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: project_payload(extra_status="Later"),
    )

    assert funnel.check_project_fields().ok


def test_renamed_lock_field_is_reported(monkeypatch):
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: project_payload(lock=False),
    )

    result = funnel.check_project_fields()

    assert not result.ok
    assert "missing field {}".format(funnel.LOCK_FIELD) in result.found


def test_missing_project_is_reported(monkeypatch):
    monkeypatch.setattr(funnel, "gh_graphql",
                        lambda query, **variables: {"user": {"projectV2": None}})

    result = funnel.check_project_fields()

    assert not result.ok
    assert "missing or not visible" in result.found


def test_project_query_failure_is_reported_not_raised(monkeypatch):
    def fail(*args, **kwargs):
        raise funnel.GitHubError("rate limit or network failure")

    monkeypatch.setattr(funnel, "gh_graphql", fail)

    result = funnel.check_project_fields()

    assert not result.ok
    assert "Project query failed" in result.found
    assert result.fix


def test_topic_count_uses_the_member_search(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos",
                        lambda: ["owner/one", "owner/two"])

    result = funnel.check_topic()

    assert result.ok
    assert "2" in result.found


def test_zero_topic_repos_is_broken_with_a_prose_fix(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [])

    result = funnel.check_topic()

    assert not result.ok
    assert "0" in result.found
    assert funnel.TOPIC in result.fix
    assert "add" in result.fix
    assert "gh " not in result.fix


def test_topic_search_failure_is_reported_not_raised(monkeypatch):
    def fail():
        raise funnel.GitHubError("GitHub unreachable")

    monkeypatch.setattr(funnel, "member_repos", fail)

    result = funnel.check_topic()

    assert not result.ok
    assert "topic search failed" in result.found
    assert result.fix


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
