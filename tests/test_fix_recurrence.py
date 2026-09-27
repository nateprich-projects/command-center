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
