"""Doctor check: every `muse exec` must name its model (#1303).

Muse's model catalog marks ``muse-spark-1.3-contributor`` as
``is_default: true``, so an invocation that omits ``--model`` runs on Meta's
Discounted Services tier. The check looks for the omission, because the
omission is what actually happened: career-toolset's job scorer put 72
sessions there without anyone choosing it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def write(root: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def findings(root: pathlib.Path):
    return funnel.muse_unpinned_invocations([root])


def test_a_pinned_invocation_is_not_reported(tmp_path):
    write(tmp_path, "run.sh",
          'muse exec --model muse-spark-1.3 --json "$PROMPT"\n')
    assert findings(tmp_path) == []


def test_an_unpinned_invocation_is_reported_with_file_and_line(tmp_path):
    path = write(tmp_path, "run.sh", '#!/bin/sh\nmuse exec --json "$P"\n')
    assert findings(tmp_path) == ["{}:2".format(path)]


def test_model_on_a_later_continuation_line_still_counts(tmp_path):
    write(tmp_path, "runner", (
        '"$MUSE_BIN" exec \\\n'
        '  --reasoning-effort max \\\n'
        '  --model muse-spark-1.3 \\\n'
        '  --json\n'
    ))
    assert findings(tmp_path) == []


def test_a_continuation_that_never_names_the_model_is_reported(tmp_path):
    path = write(tmp_path, "runner", (
        '"$MUSE_BIN" exec \\\n'
        '  --reasoning-effort max \\\n'
        '  --json\n'
    ))
    assert findings(tmp_path) == ["{}:1".format(path)]


def test_a_python_argv_list_spanning_lines_counts_as_one_statement(tmp_path):
    write(tmp_path, "score.py", (
        'result = subprocess.run(\n'
        '    [MUSE_COMMAND, "exec", "--json",\n'
        '     "--model", "muse-spark-1.3", prompt],\n'
        '    check=False,\n'
        ')\n'
    ))
    assert findings(tmp_path) == []


def test_the_real_shape_of_the_career_agent_defect_is_reported(tmp_path):
    """The exact call this check was written for."""
    path = write(tmp_path, "score.py", (
        'result = subprocess.run(\n'
        '    [MUSE_COMMAND, "exec", "--json", prompt],\n'
        '    capture_output=True,\n'
        ')\n'
    ))
    assert findings(tmp_path) == ["{}:2".format(path)]


@pytest.mark.parametrize("body", [
    "# muse exec --json prompt\n",
    "  # a bare muse exec would use the contributor default\n",
    "// muse exec --json\n",
])
def test_a_commented_out_invocation_is_not_reported(tmp_path, body):
    write(tmp_path, "notes.sh", body)
    assert findings(tmp_path) == []


@pytest.mark.parametrize("body", [
    'echo "muse-implement: muse exec failed (exit $status)" >&2\n',
    'failure "muse exec failed (exit $status): $muse_err" || true\n',
    '    assert "muse exec failed (exit 1)" in heartbeat\n',
    'When the window empties, muse exec fails in a few seconds.\n',
])
def test_prose_about_a_failed_invocation_is_not_reported(tmp_path, body):
    """Both runners log this sentence and three test files assert on it.
    Reporting them would bury the one real finding under seven false ones."""
    write(tmp_path, "runner.sh", body)
    assert findings(tmp_path) == []


def test_documentation_is_not_scanned(tmp_path):
    write(tmp_path, "README.md", "Run `muse exec --json` to try it.\n")
    write(tmp_path, "routine.md", "muse exec --json\n")
    assert findings(tmp_path) == []


def test_vendored_and_metadata_directories_are_skipped(tmp_path):
    for skipped in sorted(funnel.MUSE_SCAN_SKIP_DIRS):
        write(tmp_path, "{}/vendor.sh".format(skipped), "muse exec --json\n")
    assert findings(tmp_path) == []


def test_a_file_too_large_to_be_a_script_is_skipped(tmp_path):
    padding = "x = 1\n" * 200_000
    write(tmp_path, "huge.py", padding + 'muse exec --json\n')
    assert findings(tmp_path) == []


def test_a_missing_root_is_skipped_quietly(tmp_path):
    """Not every machine holds every checkout; absence is not a failure."""
    absent = tmp_path / "not-here"
    assert funnel.muse_unpinned_invocations([absent]) == []
    check = funnel.check_muse_model_pins([absent])
    assert check.ok
    assert check.found == ""


def test_several_roots_are_scanned_and_findings_are_sorted(tmp_path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    a = write(first, "a.sh", "muse exec --json\n")
    b = write(second, "b.sh", "muse exec --json\n")
    assert funnel.muse_unpinned_invocations([first, second]) == \
        sorted(["{}:1".format(a), "{}:1".format(b)])


def test_the_same_root_twice_reports_each_finding_once(tmp_path):
    path = write(tmp_path, "a.sh", "muse exec --json\n")
    assert funnel.muse_unpinned_invocations([tmp_path, tmp_path]) == \
        ["{}:1".format(path)]


def test_check_passes_when_every_call_site_is_pinned(tmp_path):
    write(tmp_path, "run.sh", "muse exec --model muse-spark-1.3 --json\n")
    check = funnel.check_muse_model_pins([tmp_path])
    assert check.ok
    assert check.name == "muse model pins"
    assert check.found == ""


def test_check_names_each_offender_and_carries_a_fix(tmp_path):
    path = write(tmp_path, "run.sh", "muse exec --json\n")
    check = funnel.check_muse_model_pins([tmp_path])
    assert not check.ok
    assert "1 muse exec invocation(s) do not pass --model" in check.found
    assert "{}:1".format(path) in check.found
    assert "--model" in check.fix
    assert "muse-spark-1.3-contributor" in check.fix


def test_check_is_registered_in_doctor(tmp_path, monkeypatch):
    """A check nothing runs is not a check."""
    monkeypatch.setattr(funnel, "MUSE_SCAN_ROOTS", (tmp_path,))
    monkeypatch.setattr(funnel, "MUSE_SCAN_GLOBS", ())
    monkeypatch.setattr(funnel, "member_repos", lambda: [])
    names = [check.name for check in funnel.doctor_checks()]
    assert "muse model pins" in names


def test_this_repository_pins_every_call_site():
    """The repo may not regress into the failure it is fixing."""
    assert funnel.muse_unpinned_invocations([funnel.CHECKOUT_ROOT]) == []
