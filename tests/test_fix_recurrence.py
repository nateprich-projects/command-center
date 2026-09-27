"""fix_recurrence: fix-on-fix share and hotspots from git (#1684)."""

import json
import os
import pathlib
import subprocess
import sys
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
    # Commits in the 7-14 day look-back feed the blame, never the hotspots.
    assert result["hotspots"] == []


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
    assert "broken_fix_tickets" in capsys.readouterr().err


def _new_repo(tmp_path, name="r"):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return root


def test_ticket_accept_scenario_three_fix_commits(tmp_path):
    """#1684 Accept, literally: one cross-fix rewrite, one older, one tests."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2"]),
                   "tests/test_app.py": "def test_x():\n    assert 1\n"},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20"])},
            "Older lines (#1) (#101)", NOW - timedelta(days=3))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21"])},
            "Rewrite the fix (#2) (#102)", NOW - timedelta(days=2))
    # Edits existing test lines, so only the non-test filter keeps it out.
    _commit(root, {"tests/test_app.py": "def test_x():\n    assert 2\n"},
            "Tests only (#3) (#103)", NOW - timedelta(days=1))

    result = fr.measure(root, {1: 100, 2: 200, 3: 300}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)
    assert [(row["path"], row["function"], row["projects"])
            for row in result["hotspots"]] == [("app.py", "run", [100, 200])]


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
    pad = "".join("select {};\n".format(n) for n in range(10))
    _commit(root, {"query.sql": "-- one\n" + pad + "-- two\n"},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"query.sql": "-- uno\n" + pad + "-- dos\n"},
            "Fix (#11) (#111)", NOW - timedelta(days=2))
    _commit(root, {"query.sql": "-- eins\n" + pad + "-- zwei\n"},
            "Fix again (#12) (#112)", NOW - timedelta(days=1))

    result = fr.measure(root, {11: 1100, 12: 1200}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)
    # Hunks outside any function are never hotspots.
    assert result["hotspots"] == []


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


def test_look_back_reaches_a_fix_just_before_the_window(tmp_path):
    """History spans two windows, so an early-window fix sees its cause."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20"])},
            "Earlier fix (#16) (#116)", NOW - timedelta(days=10))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21"])},
            "Early-window fix (#17) (#117)", NOW - timedelta(days=6))

    result = fr.measure(root, {16: 1600, 17: 1700}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 1)


def test_a_pure_insertion_is_counted_for_hotspots_only(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2"])},
            "Insert (#18) (#118)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2", "c = 3"])},
            "Insert more (#19) (#119)", NOW - timedelta(days=1))

    result = fr.measure(root, {18: 1800, 19: 1900}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 0)
    assert [(row["function"], row["count"]) for row in result["hotspots"]] == [
        ("run", 2)
    ]


def test_most_but_not_all_lines_from_another_fix_counts(tmp_path):
    """Three of four modified lines are the other project's: a majority."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2", "c = 3", "d = 4"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20", "c = 30", "d = 4"])},
            "Fix three (#20) (#120)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21", "c = 31", "d = 41"])},
            "Fix four (#21) (#121)", NOW - timedelta(days=1))

    result = fr.measure(root, {20: 2000, 21: 2100}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)


def test_commits_after_now_are_outside_the_window(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 2"])},
            "Fix (#22) (#122)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 3"])},
            "Later fix (#23) (#123)", NOW + timedelta(hours=1))

    result = fr.measure(root, {22: 2200, 23: 2300}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 1)


def test_only_the_squash_subject_form_names_a_ticket(tmp_path):
    """'Title (#ticket) (#pr)' only; a bare '(#N)' is not a ticket commit."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 2"])},
            "Fix (#24) (#124)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 3"])},
            "Unrelated mention (#25)", NOW - timedelta(days=1))

    result = fr.measure(root, {24: 2400, 25: 2500}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 1)


def test_hotspots_order_by_count_then_path_and_name_class_methods(tmp_path):
    """Newest-first processing meets the lower count first; output is sorted."""
    root = _new_repo(tmp_path)
    klass = "class Item:\n    def size(self):\n        return {}\n"
    _commit(root, {"a.py": _body("zed", ["x = 1"]), "b.py": klass.format(1)},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"b.py": klass.format(2)},
            "Fix (#25) (#125)", NOW - timedelta(days=4))
    _commit(root, {"b.py": klass.format(3)},
            "Fix (#26) (#126)", NOW - timedelta(days=3))
    _commit(root, {"a.py": _body("zed", ["x = 2"]), "b.py": klass.format(4)},
            "Fix (#27) (#127)", NOW - timedelta(days=2))
    _commit(root, {"a.py": _body("zed", ["x = 3"])},
            "Fix (#28) (#128)", NOW - timedelta(days=1))

    hot = fr.measure(root, {25: 2500, 26: 2600, 27: 2700, 28: 2800},
                     NOW)["hotspots"]

    assert [(row["path"], row["function"], row["count"]) for row in hot] == [
        ("b.py", "Item", 3), ("a.py", "zed", 2)]


def test_blame_covers_only_the_modified_hunk_mid_file(tmp_path):
    """Recent neighbour lines outside the hunk must not tip the majority."""
    root = _new_repo(tmp_path)
    lines = ["v{} = {}".format(n, n) for n in range(6)]
    _commit(root, {"app.py": _body("run", lines)},
            "Initial", NOW - timedelta(days=30))
    fixed = list(lines)
    fixed[3] = "v3 = 30"
    fixed[4] = "v4 = 40"
    _commit(root, {"app.py": _body("run", fixed)},
            "Fix neighbours (#29) (#129)", NOW - timedelta(days=2))
    again = list(fixed)
    again[2] = "v2 = 20"
    _commit(root, {"app.py": _body("run", again)},
            "Fix one old line (#30) (#130)", NOW - timedelta(days=1))

    result = fr.measure(root, {29: 2900, 30: 3000}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 2)


def test_examples_count_exactly_the_hunk_lines(tmp_path):
    root = _new_repo(tmp_path)
    lines = ["v{} = {}".format(n, n) for n in range(6)]
    _commit(root, {"app.py": _body("run", lines)},
            "Initial", NOW - timedelta(days=30))
    fixed = list(lines)
    fixed[1], fixed[2] = "v1 = 10", "v2 = 20"
    _commit(root, {"app.py": _body("run", fixed)},
            "Fix (#31) (#131)", NOW - timedelta(days=2))
    again = list(fixed)
    again[1], again[2] = "v1 = 11", "v2 = 21"
    _commit(root, {"app.py": _body("run", again)},
            "Fix again (#32) (#132)", NOW - timedelta(days=1))

    result = fr.measure(root, {31: 3100, 32: 3200}, NOW)

    assert [(row["ticket"], row["lines"], row["recent_fix_lines"])
            for row in result["examples"]] == [(32, 2, 2)]


def test_blame_looks_back_seven_days_from_each_commit_not_from_now(tmp_path):
    """A fix early in the window must not credit old lines to a fix before it."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 2"])},
            "Fix other lines (#33) (#133)", NOW - timedelta(days=9))
    _commit(root, {"app.py": _body("one", ["a = 3"]) + _body("two", ["b = 2"])},
            "Fix old lines (#34) (#134)", NOW - timedelta(days=6))

    result = fr.measure(root, {33: 3300, 34: 3400}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 1)


def test_an_unrelated_commit_between_two_fixes_does_not_hide_the_cause(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("one", ["a = 2"]) + _body("two", ["b = 1"])},
            "Fix (#35) (#135)", NOW - timedelta(days=10))
    _commit(root, {"app.py": _body("one", ["a = 2"]) + _body("two", ["b = 9"])},
            "Unrelated edit", NOW - timedelta(days=8))
    _commit(root, {"app.py": _body("one", ["a = 3"]) + _body("two", ["b = 9"])},
            "Fix again (#36) (#136)", NOW - timedelta(days=6))

    result = fr.measure(root, {35: 3500, 36: 3600}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 1)


def test_a_non_broken_ticket_commit_is_never_a_cause(tmp_path):
    """Only Broken fixes count as causes: rewriting feature work is not recurrence."""
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 2"])},
            "Feature (#37) (#137)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 3"])},
            "Fix (#38) (#138)", NOW - timedelta(days=1))

    result = fr.measure(root, {38: 3800}, NOW)

    assert (result["numerator"], result["denominator"]) == (0, 1)


@pytest.mark.parametrize("cause_age, counted", [
    (timedelta(days=6, hours=23), True),
    (timedelta(days=7, hours=1), False),
])
def test_the_cause_must_fall_within_seven_days_of_the_fix(tmp_path, cause_age,
                                                          counted):
    root = _new_repo(tmp_path)
    fix_at = NOW - timedelta(hours=1)
    _commit(root, {"app.py": _body("run", ["a = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 2"])},
            "Cause (#39) (#139)", fix_at - cause_age)
    _commit(root, {"app.py": _body("run", ["a = 3"])},
            "Fix (#40) (#140)", fix_at)

    result = fr.measure(root, {39: 3900, 40: 4000}, NOW)

    assert result["numerator"] == (1 if counted else 0)


def test_the_measured_window_is_seven_days_to_the_hour(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("one", ["a = 2"]) + _body("two", ["b = 1"])},
            "Just outside (#41) (#141)", NOW - timedelta(days=7, hours=1))
    _commit(root, {"app.py": _body("one", ["a = 2"]) + _body("two", ["b = 2"])},
            "Just inside (#42) (#142)", NOW - timedelta(days=6, hours=23))

    result = fr.measure(root, {41: 4100, 42: 4200}, NOW)

    assert result["denominator"] == 1


@pytest.mark.parametrize("since_shift", [timedelta(0), timedelta(hours=1),
                                         timedelta(days=1)])
def test_blame_boundary_lines_never_count_as_recent(tmp_path, monkeypatch,
                                                    since_shift):
    """A fix just inside the edge touching other lines must not be credited
    with old lines, however far --since sits from the cause window."""
    root = _new_repo(tmp_path)
    fix_at = NOW - timedelta(hours=1)
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 1"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("one", ["a = 1"]) + _body("two", ["b = 2"])},
            "Other lines (#43) (#143)",
            fix_at - timedelta(days=7) + timedelta(minutes=30))
    _commit(root, {"app.py": _body("one", ["a = 3"]) + _body("two", ["b = 2"])},
            "Fix old lines (#44) (#144)", fix_at)
    real_blame = fr._blame

    def shifted(repo, sha, path, ranges, since):
        return real_blame(repo, sha, path, ranges, since + since_shift)

    monkeypatch.setattr(fr, "_blame", shifted)

    result = fr.measure(root, {43: 4300, 44: 4400}, NOW)

    assert result["numerator"] == 0


def test_two_of_three_lines_from_another_fix_is_a_majority(tmp_path):
    root = _new_repo(tmp_path)
    _commit(root, {"app.py": _body("run", ["a = 1", "b = 2", "c = 3"])},
            "Initial", NOW - timedelta(days=30))
    _commit(root, {"app.py": _body("run", ["a = 10", "b = 20", "c = 3"])},
            "Fix two (#45) (#145)", NOW - timedelta(days=2))
    _commit(root, {"app.py": _body("run", ["a = 11", "b = 21", "c = 31"])},
            "Fix three (#46) (#146)", NOW - timedelta(days=1))

    result = fr.measure(root, {45: 4500, 46: 4600}, NOW)

    assert (result["numerator"], result["denominator"]) == (1, 2)


def test_the_installed_script_honours_now_far_from_the_wall_clock(repo, tmp_path):
    """Run the real entry point, as the watch does, at a --now years away."""
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"recorded_cause_regressions": {
        "broken_fix_tickets": [{"ticket": t, "project": p}
                               for t, p in FIXES.items()]}}))
    script = pathlib.Path(fr.__file__).resolve()

    done = subprocess.run(
        [sys.executable, str(script), "--snapshot", str(snap),
         "--repo", str(repo), "--now", NOW.isoformat()],
        capture_output=True, text=True, check=True,
    )
    later = subprocess.run(
        [sys.executable, str(script), "--snapshot", str(snap),
         "--repo", str(repo), "--now", (NOW + timedelta(days=400)).isoformat()],
        capture_output=True, text=True, check=True,
    )

    assert json.loads(done.stdout)["numerator"] == 1
    assert json.loads(later.stdout)["denominator"] == 0
