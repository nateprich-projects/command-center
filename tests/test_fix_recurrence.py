"""fix_recurrence: fix-on-fix share and hotspots from git (#1684)."""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fix_recurrence as fr

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _git(repo, *args, when=None):
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    })
    if when is not None:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+0000")
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True, text=True)


def _commit(repo, files, subject, when):
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", subject, when=when)


def _body(name, lines):
    return "def {}():\n".format(name) + "".join(
        "    {}\n".format(line) for line in lines)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    old = NOW - timedelta(days=20)
    _commit(root, {"app.py": _body("begin", ["a = 1", "b = 2", "c = 3"])
                   + _body("brief", ["x = 1", "y = 2"])},
            "Initial app", old)
    # Fix for project 10 rewrites old lines of begin: not fix-on-fix.
    _commit(root, {"app.py": _body("begin", ["a = 10", "b = 20", "c = 30"])
                   + _body("brief", ["x = 1", "y = 2"])},
            "Fix begin (#11) (#101)", NOW - timedelta(days=3))
    # Fix for project 20 rewrites project 10's fresh lines: fix-on-fix.
    _commit(root, {"app.py": _body("begin", ["a = 11", "b = 21", "c = 30"])
                   + _body("brief", ["x = 1", "y = 2"])},
            "Fix begin again (#21) (#102)", NOW - timedelta(days=2))
    # Project 20's own later ticket continues it: not fix-on-fix.
    _commit(root, {"app.py": _body("begin", ["a = 12", "b = 21", "c = 30"])
                   + _body("brief", ["x = 1", "y = 2"])},
            "Continue (#22) (#103)", NOW - timedelta(days=1, hours=12))
    # A test-only fix is not counted at all.
    _commit(root, {"tests/test_app.py": "def test_x():\n    assert True\n"},
            "Test only (#31) (#104)", NOW - timedelta(days=1))
    # An Improve commit is outside the fix set.
    _commit(root, {"app.py": _body("begin", ["a = 12", "b = 21", "c = 30"])
                   + _body("brief", ["x = 5", "y = 2"])},
            "Feature (#41) (#105)", NOW - timedelta(hours=10))
    return root


FIXES = {11: 10, 21: 20, 22: 20, 31: 30}


def test_counts_only_cross_project_rewrites_of_fresh_fix_lines(repo):
    result = fr.measure(repo, FIXES, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 3)
    assert result["share"] == pytest.approx(0.333)
    assert [row["ticket"] for row in result["examples"]] == [21]


def test_hotspots_count_distinct_projects_per_function(repo):
    result = fr.measure(repo, FIXES, NOW)

    hot = {(row["path"], row["function"]): row for row in result["hotspots"]}
    begin = hot[("app.py", "begin")]
    assert begin["projects"] == [10, 20]
    assert begin["count"] == 2
    assert ("app.py", "brief") not in hot


def test_window_excludes_older_fixes(repo):
    result = fr.measure(repo, FIXES, NOW + timedelta(days=10))

    assert (result["numerator"], result["denominator"]) == (0, 0)
    assert result["share"] is None


def test_code_path_excludes_tests_docs_and_data():
    assert fr.is_code_path("funnel.py")
    assert fr.is_code_path("scripts/muse-review-engine")
    assert fr.is_code_path("dashboard/public/app.js")
    assert not fr.is_code_path("tests/test_x.py")
    assert not fr.is_code_path("docs/notes.md")
    assert not fr.is_code_path("dashboard/fixtures/execution_metrics.json")


def test_snapshot_reader_accepts_brief_or_wrapper():
    rows = [{"ticket": 5, "project": 4}, {"ticket": 6, "project": 4}]
    brief = {"recorded_cause_regressions": {"broken_fix_tickets": rows}}

    assert fr.fix_projects_from_snapshot(brief) == {5: 4, 6: 4}
    assert fr.fix_projects_from_snapshot({"brief": brief}) == {5: 4, 6: 4}


def test_snapshot_without_field_is_an_error_not_zero():
    with pytest.raises(fr.RecurrenceError):
        fr.fix_projects_from_snapshot({"recorded_cause_regressions": {}})


def test_cli_prints_json_and_exits_nonzero_on_missing_field(repo, tmp_path,
                                                            capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"recorded_cause_regressions": {
        "broken_fix_tickets": [{"ticket": t, "project": p}
                               for t, p in FIXES.items()]}}))
    assert fr.main(["--snapshot", str(good), "--repo", str(repo),
                    "--now", NOW.isoformat()]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["numerator"] == 1

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"recorded_cause_regressions": {}}))
    assert fr.main(["--snapshot", str(bad), "--repo", str(repo)]) == 2


def _new_repo(tmp_path, name="r"):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return root


def test_ticket_accept_scenario_three_fix_commits(tmp_path):
    """#1684 Accept, literally: one cross-fix rewrite, one older, one tests."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20"])},
            "Older lines (#1) (#101)", NOW - timedelta(days=3))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21"])},
            "Rewrite the fix (#2) (#102)", NOW - timedelta(days=2))
    _commit(root, {"tests/test_app.py": "def test_x():\n    pass\n"},
            "Tests only (#3) (#103)", NOW - timedelta(days=1))

    result = fr.measure(root, {1: 100, 2: 200, 3: 300}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)


def test_a_fix_older_than_the_window_is_not_recurrence(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20"])},
            "Old fix (#5) (#105)", NOW - timedelta(days=10))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21"])},
            "New fix (#6) (#106)", NOW - timedelta(days=1))

    result = fr.measure(root, {5: 500, 6: 600}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 1)


def test_exactly_half_recent_fix_lines_is_not_a_majority(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2", "c = 3", "d = 4"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20", "c = 3", "d = 4"])},
            "Fix two (#7) (#107)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21", "c = 31", "d = 41"])},
            "Fix four (#8) (#108)", NOW - timedelta(days=1))

    result = fr.measure(root, {7: 700, 8: 800}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 2)


def test_hotspots_leave_out_a_function_only_one_project_touched(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("one", ["a = 2"]) + _body("two", ["b = 1"])},
            "Fix one (#9) (#109)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("one", ["a = 3"]) + _body("two", ["b = 2"])},
            "Fix both (#10) (#110)", NOW - timedelta(days=1))

    hot = fr.measure(root, {9: 900, 10: 1000}, NOW)["hotspots"]

    assert [(row["function"], row["count"]) for row in hot] == [("one", 2)]


def test_removed_code_line_starting_with_dashes_is_not_a_header(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"query.sql": "select 1;\n-- one\n-- two\n"},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"query.sql": "select 1;\n-- uno\n-- dos\n"},
            "Fix (#11) (#111)", NOW - timedelta(days=2))
    _commit(root, {"query.sql": "select 1;\n-- eins\n-- zwei\n"},
            "Fix again (#12) (#112)", NOW - timedelta(days=1))

    result = fr.measure(root, {11: 1100, 12: 1200}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)


def test_paths_with_spaces_and_noprefix_config_are_read(tmp_path):
    root = _new_repo(tmp_path)
    _git(root, "config", "diff.noprefix", "true")
    _commit(root, {"my mod.py": _body("run", ["a = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"my mod.py": _body("run", ["a = 2"])},
            "Fix (#13) (#113)", NOW - timedelta(days=2))
    _commit(root, {"my mod.py": _body("run", ["a = 3"])},
            "Fix again (#14) (#114)", NOW - timedelta(days=1))

    result = fr.measure(root, {13: 1300, 14: 1400}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)


def test_a_root_commit_naming_a_ticket_is_skipped(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1"])},
            "Root (#15) (#115)", NOW - timedelta(days=1))

    result = fr.measure(root, {15: 1500}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 0)


@pytest.mark.parametrize("path, code", [
    ("dashboard/test/page.test.js", False),
    ("funnel-mcp-connector/smoke_test.py", False),
    (".gitignore", False),
    ("dashboard/public/favicon.svg", False),
    ("engine/review.py", True),
])
def test_code_path_rules(path, code):
    assert fr.is_code_path(path) is code
