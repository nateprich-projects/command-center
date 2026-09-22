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
])
def test_a_commented_out_invocation_is_not_reported(tmp_path, body):
    """`#` only. Neither scanned language comments with `//`, and
    treating it as a comment marker ate real calls written after a URL
    (see test_a_url_earlier_on_the_line_does_not_hide_the_call)."""
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
    """A check nothing runs is not a check.

    Every GitHub-backed check is stubbed: this asserts registration, and
    without the stubs it spends seconds in `gh` retry backoff to do it.
    """
    monkeypatch.setattr(funnel, "MUSE_SCAN_ROOTS", (tmp_path,))
    monkeypatch.setattr(funnel, "MUSE_SCAN_GLOBS", ())
    for name, label in (
            ("member_repos", None),
            ("check_auth_scope", "gh auth"),
            ("check_project_fields", "Project fields"),
            ("check_topic", "command-center topic"),
    ):
        if label is None:
            monkeypatch.setattr(funnel, name, lambda: [])
        else:
            monkeypatch.setattr(
                funnel, name,
                lambda label=label: funnel.Check(label, True, "", ""))
    for name, label in (
            ("check_usage_cache", "usage cache"),
            ("check_heartbeat", "heartbeat branch"),
            ("check_repository_drift", "repository drift"),
            ("check_process_table", "process table"),
    ):
        monkeypatch.setattr(
            funnel, name,
            lambda *a, label=label, **k: funnel.Check(label, True, "", ""))
    names = [check.name for check in funnel.doctor_checks(
        claude_dir=tmp_path, checkout_root=tmp_path)]
    assert "muse model pins" in names


def test_this_repository_pins_every_call_site():
    """The repo may not regress into the failure it is fixing."""
    assert funnel.muse_unpinned_invocations([funnel.CHECKOUT_ROOT]) == []


# --- shapes an independent review of the first draft found missing -------
#
# Every case below was a probe that broke the scanner as first written.
# They are the regression set: the check's value is exactly the set of
# real invocations it can see, so each miss it once had gets a test.


@pytest.mark.parametrize("body", [
    'subprocess.run(["muse", "exec", "--json", prompt], check=False)\n',
    'os.execvp("muse", ["muse", "exec", "--json"])\n',
    "run([ 'muse' , 'exec' , '--json' ])\n",
])
def test_a_literal_argv_list_is_an_invocation(tmp_path, body):
    """The more idiomatic Python spelling. career-agent's scorer used a
    constant, so the one real finding on this machine was caught by luck
    of spelling; this is the form that would have hidden."""
    write(tmp_path, "call.py", body)
    assert len(findings(tmp_path)) == 1


def test_a_literal_argv_list_that_pins_is_not_reported(tmp_path):
    write(tmp_path, "call.py",
          'subprocess.run(["muse", "exec", "--model", M, "--json"])\n')
    assert findings(tmp_path) == []


def test_an_unbalanced_paren_in_a_prompt_cannot_clear_the_finding(tmp_path):
    """The worst failure available to this check: not missing a call, but
    running the statement on until it swallows an unrelated `--model` and
    reports the file clean."""
    path = write(tmp_path, "launder.sh", (
        'muse exec --json "score this ( see notes"\n'
        'echo done\n'
        'somewhere --model muse-spark-1.3\n'
    ))
    assert findings(tmp_path) == ["{}:1".format(path)]


def test_a_comment_promising_a_model_is_not_a_pin(tmp_path):
    path = write(tmp_path, "todo.py",
                 'run([MUSE, "exec", "--json"])  # TODO: pass --model\n')
    assert findings(tmp_path) == ["{}:1".format(path)]


def test_a_match_inside_an_already_open_list_follows_to_the_close(tmp_path):
    """The match lands on a continuation line, so the depth this can see
    starts at zero; the trailing comma is what carries it forward."""
    write(tmp_path, "cmd.py", (
        'cmd = [\n'
        '    MUSE_BIN, "exec",\n'
        '    "--json",\n'
        '    "--model", "muse-spark-1.3",\n'
        ']\n'
    ))
    assert findings(tmp_path) == []


def test_a_closing_paren_inside_a_string_does_not_end_the_statement(tmp_path):
    write(tmp_path, "smiley.py", (
        'subprocess.run(\n'
        '    [MUSE, "exec", "--prompt", "done :))",\n'
        '     "--model", "muse-spark-1.3"],\n'
        ')\n'
    ))
    assert findings(tmp_path) == []


def test_a_url_earlier_on_the_line_does_not_hide_the_call(tmp_path):
    """`//` is not a comment in either scanned language, and treating it
    as one ate every invocation written after a URL."""
    path = write(tmp_path, "fetch.sh",
                 'curl -s https://api.example.com/x && muse exec --json\n')
    assert findings(tmp_path) == ["{}:1".format(path)]


@pytest.mark.parametrize("body", [
    "muse exec <<'EOF'\nhello\nEOF\n",
    "muse exec >out.json\n",
    "muse exec | tee log\n",
    "muse exec && echo ok\n",
    "muse exec; echo ok\n",
    "muse exec 2>&1\n",
])
def test_ordinary_shell_punctuation_still_reads_as_an_invocation(
        tmp_path, body):
    write(tmp_path, "shapes.sh", body)
    assert len(findings(tmp_path)) == 1


@pytest.mark.parametrize("body", [
    'sh -c "muse exec --json"\n',
    "bash -c 'muse exec --json'\n",
])
def test_a_command_handed_to_a_shell_is_still_a_command(tmp_path, body):
    write(tmp_path, "wrap.sh", body)
    assert len(findings(tmp_path)) == 1


def test_a_shell_wrapped_command_that_pins_is_not_reported(tmp_path):
    write(tmp_path, "wrap.sh",
          'sh -c "muse exec --model muse-spark-1.3"\n')
    assert findings(tmp_path) == []


@pytest.mark.parametrize("body", [
    '"""Wrap muse exec --json for the scorer."""\n',
    'USAGE = "usage: muse exec --json <prompt>"\n',
])
def test_prose_that_reads_exactly_like_a_call_is_not_reported(
        tmp_path, body):
    """Followed by a flag, so the trailing lookahead cannot tell; the
    match beginning inside a string literal is what gives it away."""
    write(tmp_path, "doc.py", body)
    assert findings(tmp_path) == []


def test_the_perl_wrapped_runner_shape_is_seen(tmp_path):
    """What both funnel runners actually execute. The match begins *at*
    the quote of `"$MUSE_BIN"`, which is not inside a literal."""
    pinned = tmp_path / "pinned"
    unpinned = tmp_path / "unpinned"
    body = ("perl -e 'setpgrp(0, 0); exec @ARGV' \"$MUSE_BIN\" exec \\\n"
            "{}  --json\n")
    write(pinned, "run", body.format("  --model muse-spark-1.3 \\\n"))
    path = write(unpinned, "run", body.format(""))
    assert funnel.muse_unpinned_invocations([pinned]) == []
    assert funnel.muse_unpinned_invocations([unpinned]) == \
        ["{}:1".format(path)]


def test_findings_sort_by_line_number_not_lexically(tmp_path):
    path = write(tmp_path, "many.sh", "\n".join(
        ["muse exec --json"] + ["filler"] * 8 + ["muse exec --json"]
        + ["filler"] * 90 + ["muse exec --json"]) + "\n")
    assert funnel.muse_unpinned_invocations([tmp_path]) == [
        "{}:1".format(path), "{}:10".format(path), "{}:101".format(path)]


def test_a_long_word_run_does_not_hang_the_scan(tmp_path):
    """The first draft's argv alternative began at a bare word class and
    was quadratic in the length of a word-character run: 28s measured on
    32 KB, 7m23s on 128 KB, against a 512 KB per-file cap. Anchoring on
    the bracket or comma removed it."""
    import time
    write(tmp_path, "blob.py", "x" * 200_000 + "\n")
    started = time.monotonic()
    assert findings(tmp_path) == []
    assert time.monotonic() - started < 5.0
