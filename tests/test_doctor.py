"""The local, read-only checks behind ``funnel doctor``."""

from __future__ import annotations

import json
import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = 1_788_600_000.0


def stub_heartbeat_checks(monkeypatch):
    """Keep local-path tests offline; heartbeat has focused tests below."""
    monkeypatch.setattr(
        funnel, "check_usage_cache",
        lambda cache_path=None, now=None: funnel.Check(
            "usage cache", True, "ok", ""),
    )
    monkeypatch.setattr(
        funnel, "check_heartbeat",
        lambda spool_dir=None, now=None: funnel.Check(
            "heartbeat branch", True, "ok", ""),
    )


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
    stub_heartbeat_checks(monkeypatch)
    stub_github_checks(monkeypatch)

    checks = funnel.doctor_checks(claude_dir=claude, checkout_root=checkout)

    assert [check.name for check in checks] == [
        "install symlinks", "checkout staleness", "settings.json", "gh auth", "Project fields",
        "command-center topic", "usage cache", "heartbeat branch",
    ]
    assert all(check.ok for check in checks)
    assert "3 links" in checks[0].found


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
    if kind == "real file":
        assert result.fix == "move {} aside, then run {}".format(
            link, funnel.INSTALL_FIX)
    else:
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


def test_doctor_does_not_require_a_self_referential_checkout_link(tmp_path, monkeypatch):
    checkout, claude = install_fixture(tmp_path)
    (claude / "command-center").unlink()
    stub_heartbeat_checks(monkeypatch)
    stub_github_checks(monkeypatch)

    checks = funnel.doctor_checks(claude_dir=claude, checkout_root=checkout)

    assert len(checks) == 8
    assert checks[0].ok
    assert checks[2].ok


def test_missing_checkout_target_gets_manual_restore_steps(tmp_path):
    checkout, claude = install_fixture(tmp_path)
    (checkout / "statusline.sh").unlink()

    result = funnel.check_symlinks(claude_dir=claude, checkout_root=checkout)

    assert not result.ok
    assert "does not resolve" in result.found
    assert result.fix == "restore {}, then run {}".format(
        checkout / "statusline.sh", funnel.INSTALL_FIX)


def test_checkout_staleness_reports_a_legacy_routine_while_behind(
    tmp_path, monkeypatch
):
    checkout, claude = install_fixture(tmp_path)
    routines = checkout / "routines"
    routines.mkdir()
    old_funnel = str(claude / "command-center" / "funnel.py")
    (routines / "codex.md").write_text("python3 " + old_funnel + " doctor\n")
    (routines / "claude.md").write_text("python3 " + old_funnel + " brief\n")
    monkeypatch.setattr(funnel, "_git_ahead_behind", lambda root: (0, 3))

    result = funnel.check_checkout_staleness(
        claude_dir=claude, checkout_root=checkout)

    assert not result.ok
    assert "3 commit(s) behind origin/main" in result.found
    assert "2 routine invocation(s)" in result.found
    assert result.fix == funnel.CHECKOUT_STALENESS_FIX


def test_checkout_staleness_is_silent_after_routines_move_to_the_run_clone(
    tmp_path, monkeypatch
):
    checkout, claude = install_fixture(tmp_path)
    routines = checkout / "routines"
    routines.mkdir()
    (routines / "codex.md").write_text(
        "python3 ~/.claude/command-center-run/funnel.py doctor\n")
    (routines / "muse.md").write_text(
        "Never touch {}/funnel.py.\n".format(
            claude / "command-center"
        )
    )
    monkeypatch.setattr(
        funnel, "_git_ahead_behind",
        lambda root: pytest.fail("retired staleness check must not inspect git"),
    )

    result = funnel.check_checkout_staleness(
        claude_dir=claude, checkout_root=checkout)

    assert result == funnel.Check("checkout staleness", True, "", "")


def test_checkout_staleness_reports_current_legacy_checkout(
    tmp_path, monkeypatch
):
    checkout, claude = install_fixture(tmp_path)
    routines = checkout / "routines"
    routines.mkdir()
    old_funnel = str(claude / "command-center" / "funnel.py")
    (routines / "codex.md").write_text("python3 " + old_funnel + " doctor\n")
    monkeypatch.setattr(funnel, "_git_ahead_behind", lambda root: (0, 0))

    result = funnel.check_checkout_staleness(
        claude_dir=claude, checkout_root=checkout)

    assert result.ok
    assert "matches origin/main" in result.found


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


def test_item_consistency_reports_each_contradiction_without_writing(monkeypatch):
    items = [
        funnel.Item(
            repo="owner/repo", number=1, title="Closed idea", url="", state="CLOSED",
            status="Ideas",
        ),
        funnel.Item(
            repo="owner/repo", number=2, title="Closed done", url="", state="CLOSED",
            status="Done",
        ),
        funnel.Item(
            repo="owner/repo", number=3, title="Open done", url="", state="OPEN",
            status="Done",
        ),
        funnel.Item(
            repo="owner/repo", number=4, title="Flagged shaped", url="", state="OPEN",
            status="Shaped", labels=["needs-shaping"],
        ),
        funnel.Item(
            repo="owner/repo", number=5, title="Flagged idea", url="", state="OPEN",
            status="Ideas", labels=["needs-shaping"],
        ),
        funnel.Item(
            repo="owner/repo", number=6, title="Closed parked", url="", state="CLOSED",
            status="Parked",
        ),
        funnel.Item(
            repo="owner/repo", number=7, title="Closed ticket", url="", state="CLOSED",
            parent="owner/repo#99",
        ),
    ]
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda *args, **kwargs: pytest.fail(
            "consistency check must not write Project state"
        ),
    )

    result = funnel.check_item_consistency(items)

    assert not result.ok
    assert result.fix == ""
    assert result.found.splitlines() == [
        "owner/repo#1: state is CLOSED but Status is Ideas",
        "owner/repo#3: state is OPEN but Status is Done",
        "owner/repo#4: label needs-shaping is present but Status is Shaped",
    ]


def test_item_consistency_is_silent_for_a_consistent_board():
    items = [
        funnel.Item(
            repo="owner/repo", number=1, title="Idea", url="", state="OPEN",
            status="Ideas", labels=["needs-shaping"],
        ),
        funnel.Item(
            repo="owner/repo", number=2, title="Done", url="", state="CLOSED",
            status="Done",
        ),
        funnel.Item(
            repo="owner/repo", number=3, title="Parked", url="", state="CLOSED",
            status="Parked",
        ),
    ]

    result = funnel.check_item_consistency(items)

    assert result == funnel.Check("item consistency", True, "", "")


def test_item_consistency_reports_an_open_ticket_with_a_merged_pr():
    ticket = funnel.Item(
        repo="owner/repo", number=7, title="Merged ticket", url="", state="OPEN",
        parent="owner/repo#99",
    )

    result = funnel.check_item_consistency(
        [ticket],
        merged_pr_facts=funnel.MergedPRFacts(frozenset([ticket.ref]), False),
    )

    assert result == funnel.Check(
        "item consistency", False,
        "owner/repo#7: open ticket has a merged PR",
        "",
    )


def test_item_consistency_does_not_report_an_unmerged_ticket():
    ticket = funnel.Item(
        repo="owner/repo", number=7, title="Open ticket", url="", state="OPEN",
        parent="owner/repo#99",
    )

    result = funnel.check_item_consistency(
        [ticket],
        merged_pr_facts=funnel.MergedPRFacts(frozenset(), False),
    )

    assert result == funnel.Check("item consistency", True, "", "")


def test_merged_pr_facts_intersects_one_bounded_repo_scan(monkeypatch):
    ticket = funnel.Item(
        repo="owner/repo", number=7, title="Open ticket", url="", state="OPEN",
        parent="owner/repo#99",
    )
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        return [
            {"headRefName": "ticket/7"},
            {"headRefName": "feature/not-a-ticket"},
        ]

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)

    result = funnel.merged_pr_facts([ticket])

    assert result == funnel.MergedPRFacts(frozenset([ticket.ref]), False)
    assert len(calls) == 1
    assert calls[0][0:6] == (
        "gh", "pr", "list", "--repo", "owner/repo", "--state",
    )
    assert calls[0][-3:] == (
        "headRefName", "--limit", str(funnel.MERGED_PR_SCAN_LIMIT + 1),
    )
    assert str(funnel.MERGED_PR_SCAN_LIMIT + 1) in calls[0]


def test_merged_pr_scan_truncation_is_visible_in_the_finding(monkeypatch):
    ticket = funnel.Item(
        repo="owner/repo", number=7, title="Open ticket", url="", state="OPEN",
        parent="owner/repo#99",
    )
    rows = [{"headRefName": "ticket/7"}]
    rows.extend(
        {"headRefName": "feature/{}".format(number)}
        for number in range(funnel.MERGED_PR_SCAN_LIMIT)
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: rows)

    facts = funnel.merged_pr_facts([ticket])
    result = funnel.check_item_consistency([ticket], merged_pr_facts=facts)

    assert facts.truncated
    assert "open ticket has a merged PR" in result.found
    assert "merged PR scan truncated after newest {} entries".format(
        funnel.MERGED_PR_SCAN_LIMIT
    ) in result.found


def test_class_assignment_dump_is_sorted_and_skips_unassigned_items(monkeypatch):
    items = [
        funnel.Item(
            repo="owner/zeta", number=7, title="Zeta", url="", state="OPEN",
            klass="New",
        ),
        funnel.Item(
            repo="owner/alpha", number=3, title="Alpha", url="", state="OPEN",
            klass="Broken",
        ),
        funnel.Item(
            repo="owner/alpha", number=4, title="Unset", url="", state="OPEN",
        ),
    ]
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda *args, **kwargs: pytest.fail(
            "Class assignment dump must not write Project state"
        ),
    )

    result = funnel.check_class_assignments(items)

    assert result == funnel.Check(
        "Class assignments",
        True,
        "owner/alpha#3 | issue number 3 | Class Broken\n"
        "owner/zeta#7 | issue number 7 | Class New",
        "",
    )


def test_doctor_includes_class_assignment_dump_with_loaded_items(monkeypatch):
    stub_heartbeat_checks(monkeypatch)
    stub_github_checks(monkeypatch)

    checks = funnel.doctor_checks(items=[
        funnel.Item(
            repo="owner/repo", number=1, title="Broken", url="", state="OPEN",
            klass="Broken",
        ),
    ])

    assert [check.name for check in checks][-3:] == [
        "item consistency", "Class assignments", "block comments",
    ]
    assert checks[-2].found == "owner/repo#1 | issue number 1 | Class Broken"


def test_doctor_reports_unparseable_block_comments_with_loaded_items(monkeypatch):
    stub_heartbeat_checks(monkeypatch)
    stub_github_checks(monkeypatch)

    checks = funnel.doctor_checks(items=[
        funnel.Item(
            repo="owner/repo", number=7, title="Bad comment", url="", state="OPEN",
            labels=["blocked"],
            unparseable_block_comments=[
                "**Blocked on #77, 2026-09-07.** Legacy format."
            ],
        ),
    ])

    result = checks[-1]
    assert result == funnel.Check(
        "block comments", False,
        "owner/repo#7: **Blocked on #77, 2026-09-07.** Legacy format.",
        "",
    )


def test_main_doctor_loads_project_items_for_consistency(monkeypatch):
    loaded = []

    monkeypatch.setattr(
        funnel, "doctor_checks",
        lambda items=None, merged_pr_facts=None: [
            funnel.Check("local", True, "ok", "")
        ],
    )
    monkeypatch.setattr(funnel, "load_items", lambda: loaded)

    assert funnel.main(["doctor"]) == 0


def test_main_doctor_reports_consistency_load_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "doctor_checks",
        lambda items=None: [funnel.Check("local", True, "ok", "")],
    )
    monkeypatch.setattr(
        funnel, "load_items",
        lambda: (_ for _ in ()).throw(funnel.GitHubError("offline")),
    )

    assert funnel.main(["doctor"]) == 1
    assert "Project items could not be loaded: offline" in capsys.readouterr().out


def test_main_doctor_returns_one_when_any_check_is_broken(monkeypatch):
    monkeypatch.setattr(
        funnel, "doctor_checks",
        lambda items=None, merged_pr_facts=None: [
            funnel.Check("local", False, "broken", "fix")
        ],
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [])

    assert funnel.main(["doctor"]) == 1


def test_doctor_renderer_puts_the_fix_only_on_broken_lines(capsys):
    funnel.render_checks([
        funnel.Check("good", True, "found it", ""),
        funnel.Check("bad", False, "missing it", "run the fix"),
        funnel.Check("silent", True, "", ""),
    ])

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "good: found it",
        "bad: missing it — fix: run the fix",
    ]


# -- usage cache -------------------------------------------------------------


def write_usage_cache(path, captured_at):
    path.write_text(json.dumps({
        "captured_at": captured_at,
        "five_hour": {"used_percentage": 1.0, "resets_at": NOW + 600},
    }))


def test_missing_usage_cache_names_opening_claude_code(tmp_path):
    result = funnel.check_usage_cache(tmp_path / "missing.json", now=NOW)

    assert not result.ok
    assert "missing" in result.found
    assert "age unavailable" in result.found
    assert result.fix == "open Claude Code on the Mac mini"


def test_fresh_usage_cache_reports_its_age(tmp_path):
    cache = tmp_path / "usage.json"
    write_usage_cache(cache, NOW - 5 * 60)

    result = funnel.check_usage_cache(cache, now=NOW)

    assert result.ok
    assert "present and fresh" in result.found
    assert "age 5 minutes" in result.found


def test_unparseable_usage_cache_is_distinct_from_missing(tmp_path):
    cache = tmp_path / "usage.json"
    cache.write_text("{not valid json")

    result = funnel.check_usage_cache(cache, now=NOW)

    assert not result.ok
    assert "present but unparseable" in result.found
    assert "missing" not in result.found
    assert result.fix == "open Claude Code on the Mac mini"


def test_old_usage_cache_reports_age_and_is_broken(tmp_path):
    cache = tmp_path / "usage.json"
    write_usage_cache(cache, NOW - 2 * 3600)

    result = funnel.check_usage_cache(cache, now=NOW)

    assert not result.ok
    assert "stale" in result.found
    assert "age 2 hours" in result.found
    assert "open Claude Code on the Mac mini" == result.fix


# -- heartbeat branch and spool --------------------------------------------


def test_existing_heartbeat_branch_with_empty_spool_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(funnel, "gh_branch_exists", lambda: True)

    result = funnel.check_heartbeat(tmp_path / "spool", now=NOW)

    assert result.ok
    assert "heartbeat branch `heartbeat` exists" in result.found
    assert "spool" in result.found and "empty" in result.found


def test_absent_heartbeat_branch_is_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(funnel, "gh_branch_exists", lambda: False)

    result = funnel.check_heartbeat(tmp_path / "spool", now=NOW)

    assert not result.ok
    assert "heartbeat branch `heartbeat` is absent" in result.found
    assert result.fix


def test_gh_reports_a_missing_ref_as_absent(monkeypatch):
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="HTTP 404: Not Found"),
    )

    assert funnel.gh_branch_exists() is False


def test_heartbeat_query_failure_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="GitHub unreachable"),
    )

    result = funnel.check_heartbeat(tmp_path / "spool", now=NOW)

    assert not result.ok
    assert "could not check heartbeat branch" in result.found
    assert "GitHub unreachable" in result.found


def test_three_spool_files_report_count_and_oldest_age(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir()
    for name, age in (("one.jsonl", 2 * 86400),
                      ("two.jsonl", 3600),
                      ("three.jsonl", 60)):
        path = spool / name
        path.write_text("record\n")
        os.utime(path, (NOW - age, NOW - age))
    monkeypatch.setattr(funnel, "gh_branch_exists", lambda: True)

    result = funnel.check_heartbeat(spool, now=NOW)

    assert not result.ok
    assert "3 file(s)" in result.found
    assert "oldest is 2 days" in result.found
