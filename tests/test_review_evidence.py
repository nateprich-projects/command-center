"""The fresh-merge suite and the reproduction run: scripts/review_evidence.py
(#1802 and #1803, plan #1783 tickets 2 and 3).

Each test builds a real fixture repository: an ancestor commit, a base commit
on ``main`` and a head commit on a branch, with the checkout on the head. The
repo's own conftest logs every test it starts, and which tree it started in,
so the tests can see what ran on the merge and what ran on the base.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "review_evidence.py"
sys.path.insert(0, str(ROOT))

from engine import implement  # noqa: E402

spec = importlib.util.spec_from_file_location("review_evidence", SCRIPT)
review_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_evidence)

#: The fixture's own commits carry an identity in the environment of their
#: git calls only. Nothing else has one, so the merge must set its own.
FIXTURE_IDENTITY = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.test",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.test",
}

#: Logs "<tree>\t<cwd>\t<node id>" for every test started. The tree is told
#: apart by content: the base adds base.txt and the head adds head.txt, so
#: only the merge has both.
LOG_CONFTEST = '''\
import os


def pytest_runtest_logstart(nodeid, location):
    have = (os.path.exists("base.txt"), os.path.exists("head.txt"))
    tree = {(True, True): "merge", (True, False): "base",
            (False, True): "head"}.get(have, "ancestor")
    with open(os.environ["EVIDENCE_LOG"], "a") as log:
        log.write("{}\\t{}\\t{}\\n".format(tree, os.getcwd(), nodeid))
'''

CALC = "def double(x):\n    return x * 2\n"
CALC_TESTS = (
    "from calc import double\n\n\n"
    "def test_one():\n    assert double(1) == 2\n\n\n"
    "def test_two():\n    assert double(2) == 4\n"
)


@pytest.fixture(autouse=True)
def no_git_identity(tmp_path, monkeypatch):
    """No git identity anywhere, and git forbidden from guessing one."""
    config = tmp_path / "global.gitconfig"
    config.write_text("[user]\n\tuseConfigOnly = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in list(FIXTURE_IDENTITY) + ["EMAIL"]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "evidence.log"
    monkeypatch.setenv("EVIDENCE_LOG", str(path))
    return path


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env=dict(os.environ, **FIXTURE_IDENTITY),
    ).stdout.strip()


def commit(repo, files, message):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path, ancestor, base, head):
    """A checkout on the head commit; returns it with the base and head SHAs."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    commit(repo, dict({"conftest.py": LOG_CONFTEST}, **ancestor), "ancestor")
    git(repo, "branch", "head")
    base_sha = commit(repo, dict({"base.txt": "base\n"}, **base), "base")
    git(repo, "switch", "-q", "head")
    head_sha = commit(repo, dict({"head.txt": "head\n"}, **head), "head")
    return repo, base_sha, head_sha


def ran(log):
    """(tree, node id) for every test started, in order."""
    if not log.exists():
        return []
    rows = [line.split("\t") for line in log.read_text().splitlines()]
    return [(tree, node_id) for tree, _, node_id in rows]


def worktrees(repo):
    return [line for line in git(repo, "worktree", "list", "--porcelain")
            .splitlines() if line.startswith("worktree ")]


def evidence(repo, base_sha, tmp_path):
    """Run it with a work directory the test can inspect afterwards."""
    work = tmp_path / "work"
    record = review_evidence.merged_suite(repo, base_sha, work_dir=work)
    assert worktrees(repo) == ["worktree {}".format(repo.resolve())]
    assert list(work.iterdir()) == []
    return record


def test_a_merge_only_failure_blocks(tmp_path, log):
    # The base adds a test of double(3); the head changes double so that
    # test breaks. Each passes alone; only the merge fails.
    repo, base_sha, head_sha = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
        base={"tests/test_base.py":
              "from calc import double\n\n\n"
              "def test_three():\n    assert double(3) == 6\n"},
        head={"calc.py": "def double(x):\n    return x * 2 if x < 3 else 0\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["schema_version"] == 1
    assert record["result"] == "fail"
    assert record["blocking"] is True
    assert record["head"] == head_sha
    assert record["base"] == base_sha
    # The tested tree is a real merge of the head into the base.
    assert git(repo, "rev-parse", record["merge"] + "^1") == base_sha
    assert git(repo, "rev-parse", record["merge"] + "^2") == head_sha
    assert record["failing"] == ["tests/test_base.py::test_three"]
    assert record["base_rerun"]["ran"] == ["tests/test_base.py::test_three"]
    assert record["base_rerun"]["result"] == "pass"
    assert record["base_rerun"]["already_failing"] == []
    assert record["new_failures"] == ["tests/test_base.py::test_three"]
    assert ("merge", "tests/test_base.py::test_three") in ran(log)
    assert ("base", "tests/test_base.py::test_three") in ran(log)


def test_a_failure_already_on_base_does_not_block(tmp_path, log):
    # Main is red on its own: the head is not to blame. The " - " in the
    # parametrize id is where a naive split of pytest's summary line breaks,
    # and the printed line is captured output, not pytest stopping early.
    red = "tests/test_base.py::test_red[a - b]"
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
        base={"tests/test_base.py":
              "import pytest\n\nfrom calc import double\n\n\n"
              "@pytest.mark.parametrize('label', ['a - b'])\n"
              "def test_red(label):\n"
              "    print('!!!!!!! Interrupted: 1 error during collection !!!!!!!')\n"
              "    assert double(2) == 5\n"},
        head={"tests/test_head.py":
              "from calc import double\n\n\n"
              "def test_four():\n    assert double(4) == 8\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["result"] == "fail"
    assert record["failing"] == [red]
    assert record["base_rerun"]["result"] == "fail"
    assert record["base_rerun"]["already_failing"] == [red]
    assert record["new_failures"] == []
    assert record["blocking"] is False
    assert ("merge", "tests/test_head.py::test_four") in ran(log)


def test_only_the_failing_node_ids_rerun_on_base(tmp_path, log):
    # On the merge four tests fail: one already red on the base, one the
    # head's change to double breaks, one in a file the base lacks, and one
    # the head added to a file the base has.
    base_ids = ["tests/test_base.py::test_base_calc",
                "tests/test_base.py::test_base_red"]
    head_ids = ["tests/test_calc.py::test_new_red",
                "tests/test_head.py::test_head_red"]
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
        base={"tests/test_base.py":
              "from calc import double\n\n\n"
              "def test_base_red():\n    assert False\n\n\n"
              "def test_base_green():\n    assert True\n\n\n"
              "def test_base_calc():\n    assert double(5) == 10\n"},
        # A failing test's captured output is printed before the summary; a
        # line in it shaped like a summary line names no failure.
        head={"calc.py": "def double(x):\n    return x * 2 if x < 5 else 0\n",
              "tests/test_head.py":
              "def test_head_red():\n"
              "    print('FAILED tests/test_ghost.py::test_ghost - fake')\n"
              "    assert False\n",
              "tests/test_calc.py": CALC_TESTS +
              "\n\ndef test_new_red():\n    assert False\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert sorted(record["failing"]) == base_ids + head_ids
    assert sorted(node_id for tree, node_id in ran(log)
                  if tree == "base") == base_ids
    assert sorted(record["base_rerun"]["ran"]) == base_ids
    assert sorted(record["base_rerun"]["absent"]) == head_ids
    assert record["base_rerun"]["already_failing"] == [
        "tests/test_base.py::test_base_red"]
    assert sorted(record["new_failures"]) == [
        "tests/test_base.py::test_base_calc"] + head_ids
    assert record["blocking"] is True
    # The merge ran the whole suite, the base's green test included.
    assert ("merge", "tests/test_base.py::test_base_green") in ran(log)
    assert ("merge", "tests/test_calc.py::test_one") in ran(log)


@pytest.mark.parametrize("ancestor, base", [
    # Main has a test file that cannot be collected, which interrupts the
    # session before any test runs.
    ({}, {"tests/test_broken.py": "from calc import gone\n"}),
    # The repo stops at the first failure, and main's red test runs first.
    ({"pytest.ini": "[pytest]\naddopts = -x\n"},
     {"tests/test_base.py": "def test_red():\n    assert False\n"}),
], ids=["collection-error", "maxfail"])
def test_a_pytest_run_that_stopped_early_blocks(tmp_path, log, ancestor,
                                                base):
    # The head breaks double, which tests/test_calc.py would catch if the
    # run got to it; the one failure the run did report is main's own.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor=dict({"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
                      **ancestor),
        base=base,
        head={"calc.py": "def double(x):\n    return 0\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert ("merge", "tests/test_calc.py::test_one") not in ran(log)
    assert record["result"] == "fail"
    assert len(record["failing"]) == 1
    assert record["blocking"] is True


def test_one_red_base_test_does_not_cover_a_file_the_merge_cannot_collect(
        tmp_path, log):
    # The head renames double, so tests/test_calc.py cannot be collected on
    # the merge. The run is not interrupted (the repo continues past
    # collection errors), and on the base only test_red in that file fails.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS,
                  "pytest.ini":
                  "[pytest]\naddopts = --continue-on-collection-errors\n"},
        base={"tests/test_calc.py":
              CALC_TESTS + "\n\ndef test_red():\n    assert False\n"},
        head={"calc.py": "def twice(x):\n    return x * 2\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["failing"] == ["tests/test_calc.py"]
    assert record["base_rerun"]["already_failing"] == []
    assert record["new_failures"] == ["tests/test_calc.py"]
    assert record["blocking"] is True


def test_a_conftest_that_breaks_only_on_the_merge_blocks(tmp_path, log):
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS,
                  "tests/conftest.py": "import calc\n"},
        base={}, head={"tests/conftest.py": "from calc import gone\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["result"] == "fail"
    assert record["blocking"] is True


@pytest.mark.parametrize("base_red, results, blocking", [
    (True, ["fail", "pass"], False),
    (False, ["fail", "not-run"], True),
])
def test_the_plan_goes_on_only_past_a_failure_the_base_shares(
        tmp_path, log, monkeypatch, base_red, results, blocking):
    # finish-ticket stops at a failing command; a failure that is not the
    # head's must not stop the commands after it from running on the merge.
    red = "def test_red():\n    assert False\n"
    repo, base_sha, _ = make_repo(
        tmp_path, ancestor={"tests/test_calc.py": "def test_one():\n    pass\n"},
        base={"tests/test_base.py": red} if base_red else {},
        head={} if base_red else {"tests/test_head.py": red})
    after = [sys.executable, "-c",
             "import os; open(os.environ['EVIDENCE_LOG'], 'a')"
             ".write('after\\n')"]
    monkeypatch.setattr(implement, "default_test_plan", lambda root: (
        [[sys.executable, "-m", "pytest", "-q"], after], "fixture plan"))

    record = evidence(repo, base_sha, tmp_path)

    assert [entry["result"] for entry in record["commands"]] == results
    assert record["blocking"] is blocking
    assert ("after" in log.read_text().splitlines()) is (not blocking)


def test_a_conflict_is_recorded_and_nothing_runs(tmp_path, log):
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
        base={"calc.py": "def double(x):\n    return x * 3\n"},
        head={"calc.py": "def double(x):\n    return x + x + 0\n"},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["result"] == "conflict"
    assert record["conflicts"] == ["calc.py"]
    assert record["merge"] is None
    assert record["blocking"] is True
    assert record["commands"] == []
    assert ran(log) == []


MAKE_PYPROJECT = '[tool.command-center]\ntest = "make test"\n'


def test_a_make_test_repo_runs_on_the_merge(tmp_path, log):
    # The recipe fails unless both sides' files are there, and records that
    # it ran.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"pyproject.toml": MAKE_PYPROJECT,
                  "Makefile": "test:\n\tcat base.txt head.txt >> "
                              "\"$$EVIDENCE_LOG\"\n"},
        base={}, head={},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["result"] == "pass"
    assert record["blocking"] is False
    assert record["test_source"] == "pyproject.toml [tool.command-center] test"
    assert record["commands"] == [{"command": "make test", "result": "pass"}]
    assert log.read_text() == "base\nhead\n"


def test_a_make_test_failure_on_the_merge_counts_without_a_base_rerun(
        tmp_path, log):
    # Not pytest: no node ids to read, so any merge failure blocks.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"pyproject.toml": MAKE_PYPROJECT,
                  "Makefile": "test:\n\ttest ! -f base.txt -o ! -f head.txt\n"},
        base={}, head={},
    )

    record = evidence(repo, base_sha, tmp_path)

    assert record["result"] == "fail"
    assert record["blocking"] is True
    assert record["commands"] == [{"command": "make test", "result": "fail"}]
    assert record["failing"] == []
    assert record["base_rerun"] is None


@pytest.mark.parametrize("test_command", [
    "make test",
    "python3 -m pytest -q",
])
def test_merged_suite_carries_bounded_timeout_output_and_setup_markers(
        tmp_path, monkeypatch, test_command):
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"pyproject.toml": (
            '[tool.command-center]\ntest = "{}"\n'.format(test_command))},
        base={}, head={})
    captured = "No module named pytest\n" + "progress line\n" * 300

    def timeout_test_command(root, commands=None, timeout=None):
        raise implement.CommandTimeoutError(
            commands[0], 1, captured_output=captured)

    monkeypatch.setattr(implement, "run_tests", timeout_test_command)
    record = evidence(repo, base_sha, tmp_path)

    (command,) = record["commands"]
    assert command["timed_out"] is True
    assert command["result"] == "fail"
    assert command["output"].startswith("[output truncated;")
    assert "setup markers retained: no module named pytest" in command[
        "output"].casefold()
    assert len(command["output"].split("\n", 1)[-1].encode()) <= 2048
    assert "tests timed out:" not in command["output"].casefold()
    assert record["blocking"] is True
    assert record["failing"] == []


@pytest.mark.parametrize("run", ["merged_suite", "reproduction"])
def test_the_worktree_is_removed_when_the_run_raises(tmp_path, monkeypatch,
                                                     run):
    repo, base_sha, _ = make_repo(
        tmp_path, ancestor={"tests/test_calc.py": CALC_TESTS},
        base={}, head={"tests/test_new.py": "def test_new():\n    pass\n"})
    work = tmp_path / "work"
    seen = []

    def explode(root, commands=None):
        seen.append(pathlib.Path(root))
        raise RuntimeError("the runner fell over")

    monkeypatch.setattr(implement, "run_tests", explode)
    with pytest.raises(RuntimeError, match="fell over"):
        getattr(review_evidence, run)(repo, base_sha, work_dir=work)

    # It did get as far as a worktree, outside the checkout.
    assert seen and repo.resolve() not in seen[0].resolve().parents
    assert not seen[0].exists()
    assert worktrees(repo) == ["worktree {}".format(repo.resolve())]
    assert list(work.iterdir()) == []


def test_a_work_dir_inside_the_checkout_is_refused(tmp_path):
    repo, base_sha, _ = make_repo(
        tmp_path, ancestor={"tests/test_calc.py": CALC_TESTS},
        base={}, head={})

    with pytest.raises(review_evidence.ReviewEvidenceError, match="outside"):
        review_evidence.merged_suite(repo, base_sha, work_dir=repo / "tmp")

    assert worktrees(repo) == ["worktree {}".format(repo.resolve())]
    assert list((repo / "tmp").iterdir()) == []


def test_the_cli_prints_the_record_and_refuses_an_unknown_base(
        tmp_path, log, capsys):
    repo, base_sha, head_sha = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
        base={}, head={})

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo), base_sha],
        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    record = json.loads(proc.stdout)
    assert (record["schema_version"], record["result"], record["head"]) == (
        1, "pass", head_sha)
    assert record["truncated"] == {}

    assert review_evidence.main([str(repo), "0" * 40]) == 2
    assert "not a commit" in capsys.readouterr().err
    assert worktrees(repo) == ["worktree {}".format(repo.resolve())]


def test_the_output_is_capped_at_64_kb_and_says_what_it_cut():
    ids = ["tests/test_many.py::test_case[{:05d}-{}]".format(n, "x" * 60)
           for n in range(3000)]
    record = {
        "schema_version": 1, "result": "fail", "blocking": True,
        "failing": ids, "new_failures": ids, "conflicts": [],
        "base_rerun": {"command": "pytest", "ran": ids, "absent": [],
                       "already_failing": []},
        "commands": [{"command": "c" * 10000, "result": "fail"}],
    }

    text = review_evidence.render(record)

    assert len(text.encode("utf-8")) <= 64 * 1024
    out = json.loads(text)
    for field, kept in (("failing", out["failing"]),
                        ("new_failures", out["new_failures"]),
                        ("base_rerun.ran", out["base_rerun"]["ran"])):
        assert out["truncated"][field]["total"] == 3000
        assert out["truncated"][field]["kept"] == len(kept) < 3000
        assert kept == ids[:len(kept)]
    assert out["truncated"]["commands[].command"] == {
        "kept": 4096, "total": 10000}
    assert out["commands"][0]["command"] == "c" * 4096
    assert "conflicts" not in out["truncated"]


# -- the reproduction run (#1803) -------------------------------------------

#: Right only at 2: what a head that fixes double is fixing.
BUGGY_CALC = "def double(x):\n    return x + 2\n"
TWO_TEST = ("from calc import double\n\n\n"
            "def test_two():\n    assert double(2) == 4\n")


def repro(repo, base_sha, tmp_path):
    """Run it with a work directory the test can inspect afterwards."""
    work = tmp_path / "work"
    record = review_evidence.reproduction(repo, base_sha, work_dir=work)
    assert worktrees(repo) == ["worktree {}".format(repo.resolve())]
    assert list(work.iterdir()) == []
    return record


def outcomes(record):
    return {test["id"]: test["outcome"] for test in record["tests"]}


def test_a_test_the_fix_makes_pass_is_red_on_the_base(tmp_path, log):
    # The head fixes double and adds a test of it that takes a fixture from
    # a conftest the head also adds. The conftest is the tests' own and
    # comes from the head; calc.py is the code under test and is the base's.
    repo, base_sha, head_sha = make_repo(
        tmp_path,
        ancestor={"calc.py": BUGGY_CALC, "tests/test_calc.py": TWO_TEST},
        base={},
        head={"calc.py": CALC,
              "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\n"
                                   "def three():\n    return 3\n",
              "tests/test_calc.py": TWO_TEST +
              "\n\ndef test_three(three):\n    assert double(three) == 6\n"},
    )

    record = repro(repo, base_sha, tmp_path)

    assert (record["schema_version"], record["head"], record["base"]) == (
        1, head_sha, base_sha)
    [test] = record["tests"]
    assert (test["id"], test["outcome"]) == (
        "tests/test_calc.py::test_three", "red")
    assert test["detail"].startswith("assert 5 == 6")
    assert record["line"] == "reproduction: red"
    assert json.loads(review_evidence.render(record))["line"] == (
        "reproduction: red")
    # Only the added test ran, and in a tree without the head's code.
    assert ran(log) == [("base", "tests/test_calc.py::test_three")]


def test_a_test_that_passes_without_the_change_passes_on_base(tmp_path, log):
    # A prefactor's test passes on the base as it will after. The other
    # added test fails only for want of the function the head adds, which
    # is no signal, so the one pass decides the line. The head also deletes
    # a conftest whose fixture would break every test: the tests' paths are
    # the head's, deletions included.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": BUGGY_CALC, "tests/conftest.py":
                  "import pytest\n\n\n@pytest.fixture(autouse=True)\n"
                  "def broken():\n    raise RuntimeError('deleted')\n"},
        base={},
        head={"calc.py": BUGGY_CALC + "\n\ndef triple(x):\n    return x * 3\n",
              "tests/test_calc.py":
              "import calc\n\n\n"
              "def test_two():\n    assert calc.double(2) == 4\n\n\n"
              "def test_triple():\n    assert calc.triple(2) == 6\n"},
    )
    git(repo, "rm", "-q", "tests/conftest.py")
    git(repo, "commit", "-q", "-m", "drop the conftest")

    record = repro(repo, base_sha, tmp_path)

    assert outcomes(record) == {
        "tests/test_calc.py::test_two": "passes-on-base",
        "tests/test_calc.py::test_triple": "no signal"}
    assert record["line"] == "reproduction: passes-on-base"


TRIPLE_CALC = BUGGY_CALC + "\n\ndef triple(x):\n    return x * 3\n"


@pytest.mark.parametrize("head, detail", [
    # An ImportError, AttributeError or NameError on a symbol the head adds
    # says only that the base lacks it.
    ({"calc.py": TRIPLE_CALC, "tests/test_new.py":
      "import calc\n\n\ndef test_new():\n    assert calc.triple(2) == 6\n"},
     "AttributeError"),
    ({"calc.py": TRIPLE_CALC, "tests/test_new.py":
      "def test_new():\n    from calc import triple\n"
      "    assert triple(2) == 6\n"},
     "ImportError"),
    ({"calc.py": TRIPLE_CALC, "tests/test_new.py":
      "from calc import *\n\n\ndef test_new():\n    assert triple(2) == 6\n"},
     "NameError"),
    # A module the head adds to a package the base has: the error names
    # the dotted path, whose last part is the new module.
    ({"shapes/trig.py": "def half(x):\n    return x / 2\n",
      "tests/test_new.py": "def test_new():\n    import shapes.trig\n"
                           "    assert shapes.trig.half(4) == 2\n"},
     "ModuleNotFoundError: No module named 'shapes.trig'"),
    # The same at module level stops the file being collected at all.
    ({"calc.py": TRIPLE_CALC, "tests/test_new.py":
      "from calc import triple\n\n\ndef test_new():\n"
      "    assert triple(2) == 6\n"},
     "collection error"),
    # A failure in setup is no signal, even an assertion about the code.
    ({"calc.py": CALC, "tests/test_new.py":
      "import pytest\n\nfrom calc import double\n\n\n@pytest.fixture\n"
      "def six():\n    assert double(3) == 6\n    return 6\n\n\n"
      "def test_new(six):\n    assert six == 6\n"},
     "failed on setup"),
    # A skip on the base is not a pass.
    ({"calc.py": TRIPLE_CALC, "tests/test_new.py":
      "import pytest\n\nimport calc\n\n\ndef test_new():\n"
      "    if not hasattr(calc, 'triple'):\n"
      "        pytest.skip('no triple here')\n"
      "    assert calc.triple(2) == 6\n"},
     "no triple here"),
    # No test added at all.
    ({"calc.py": CALC}, None),
], ids=["attribute", "import", "name", "module", "collection", "setup",
        "skip", "no-tests"])
def test_no_signal(tmp_path, log, head, detail):
    repo, base_sha, _ = make_repo(
        tmp_path, ancestor={"calc.py": BUGGY_CALC, "shapes/__init__.py": "",
                            "tests/test_calc.py": TWO_TEST},
        base={}, head=head)

    record = repro(repo, base_sha, tmp_path)

    if detail is None:
        assert record["tests"] == []
    else:
        [test] = record["tests"]
        assert (test["id"], test["outcome"]) == (
            "tests/test_new.py::test_new", "no signal")
        assert test["detail"].startswith(detail)
    assert record["line"] == "reproduction: no signal"


PARSE = "\n\ndef parse(s):\n    return s.strip()\n"
RATIO = "\n\ndef ratio(a, b):\n    return a / b\n"


@pytest.mark.parametrize("ancestor, head, detail", [
    # The base crashes. The AttributeError names strip, which the fix's own
    # added line calls but does not define: it is not a missing symbol.
    (CALC + PARSE,
     {"calc.py": CALC + PARSE.replace("s.strip()", '(s or "").strip()'),
      "tests/test_new.py":
      "import calc\n\n\ndef test_new():\n    assert calc.parse(None) == ''\n"},
     "AttributeError: 'NoneType' object has no attribute 'strip'"),
    (CALC + RATIO,
     {"calc.py": CALC + RATIO.replace("a / b", "a / b if b else 0.0"),
      "tests/test_new.py":
      "import calc\n\n\n"
      "def test_new():\n    assert calc.ratio(1, 0) == 0.0\n"},
     "ZeroDivisionError"),
    (CALC,
     {"calc.py": "def double(x):\n    if x < 0:\n"
                 "        raise ValueError(x)\n    return x * 2\n",
      "tests/test_new.py":
      "import pytest\n\nimport calc\n\n\ndef test_new():\n"
      "    with pytest.raises(ValueError):\n        calc.double(-1)\n"},
     "Failed: DID NOT RAISE"),
], ids=["attribute-crash", "crash", "did-not-raise"])
def test_a_crash_reproduction_is_red(tmp_path, log, ancestor, head, detail):
    repo, base_sha, _ = make_repo(
        tmp_path, ancestor={"calc.py": ancestor}, base={}, head=head)

    record = repro(repo, base_sha, tmp_path)

    [test] = record["tests"]
    assert (test["id"], test["outcome"]) == (
        "tests/test_new.py::test_new", "red")
    assert test["detail"].startswith(detail)
    assert record["line"] == "reproduction: red"


def test_mixed_results_give_red(tmp_path, log):
    # One file holds a red test, a pass, a parametrized test red for one
    # case, a no-signal test, and a class pytest does not collect (it has
    # an __init__). Another file cannot be collected on the base. Pytest
    # runs nothing at all while either of those ids is asked for.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": BUGGY_CALC},
        base={},
        head={"calc.py": TRIPLE_CALC.replace("x + 2", "x * 2"),
              "tests/test_calc.py":
              "import pytest\n\nimport calc\n\n\n"
              "def test_red():\n    assert calc.double(3) == 6\n\n\n"
              "def test_pass():\n    assert calc.double(2) == 4\n\n\n"
              "@pytest.mark.parametrize('x', [2, 3])\n"
              "def test_param(x):\n    assert calc.double(x) == 2 * x\n\n\n"
              "def test_triple():\n    assert calc.triple(2) == 6\n\n\n"
              "class TestInit:\n    def __init__(self):\n        pass\n\n"
              "    def test_x(self):\n        assert calc.double(3) == 6\n",
              "tests/test_triple.py":
              "from calc import triple\n\n\ndef test_import():\n"
              "    assert triple(1) == 3\n"},
    )

    record = repro(repo, base_sha, tmp_path)

    assert outcomes(record) == {
        "tests/test_calc.py::test_red": "red",
        "tests/test_calc.py::test_pass": "passes-on-base",
        "tests/test_calc.py::test_param": "red",
        "tests/test_calc.py::test_triple": "no signal",
        "tests/test_calc.py::TestInit::test_x": "no signal",
        "tests/test_triple.py::test_import": "no signal"}
    assert {test["id"]: test["detail"] for test in record["tests"]
            if test["detail"] in ("not collected", "collection error")} == {
        "tests/test_calc.py::TestInit::test_x": "not collected",
        "tests/test_triple.py::test_import": "collection error"}
    assert record["line"] == "reproduction: red"
    assert ("base", "tests/test_calc.py::test_red") in ran(log)


UNTOUCHED = '''\
import pytest

from calc import double


def test_one():
    assert double(1) == 2


def test_two():
    assert double(2) == 4


def test_three():
    assert double(3) == 6
    assert double(0) == 0


class TestMore:
    def test_four(self):
        assert double(4) == 8

    def test_five(self):
        assert double(5) == 10


@pytest.mark.parametrize("x", [7])
def test_seven(x):
    assert double(x) == 2 * x
'''


def test_an_untouched_test_in_a_changed_file_is_not_listed(tmp_path, log):
    # The head adds an import, changes test_two and test_four, deletes a
    # line from test_three, widens test_seven's parameters and adds
    # test_six. Main changed test_one since the head branched: that is
    # main's change, not the PR's.
    head_file = (
        "import calc\n" + UNTOUCHED
        .replace("double(2) == 4", "double(-2) == -4")
        .replace("    assert double(0) == 0\n", "")
        .replace("double(4) == 8", "calc.double(4) == 8")
        .replace("[7]", "[7, 8]")
        + "\n\ndef test_six():\n    assert double(6) == 12\n")
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": UNTOUCHED,
                  "tests/test_other.py": "def test_other():\n    pass\n"},
        base={"tests/test_calc.py":
              UNTOUCHED.replace("double(1) == 2", "double(1) + 0 == 2")},
        head={"tests/test_calc.py": head_file},
    )

    record = repro(repo, base_sha, tmp_path)

    listed = ["tests/test_calc.py::test_two",
              "tests/test_calc.py::test_three",
              "tests/test_calc.py::TestMore::test_four",
              "tests/test_calc.py::test_seven",
              "tests/test_calc.py::test_six"]
    assert [test["id"] for test in record["tests"]] == listed
    assert {test["outcome"] for test in record["tests"]} == {
        "passes-on-base"}
    assert {node_id.split("[")[0] for _, node_id in ran(log)} == set(listed)
    assert record["line"] == "reproduction: passes-on-base"


@pytest.mark.parametrize("imports, base_class", [
    ("import unittest\n", "unittest.TestCase"),
    ("from unittest import TestCase\n", "TestCase"),
], ids=["unittest.TestCase", "TestCase"])
def test_a_red_test_in_a_testcase_class_of_any_name_is_listed(
        tmp_path, log, imports, base_class):
    # Pytest collects every unittest.TestCase subclass whatever its name
    # (#1829 review). The head fixes double, adds a regression test to
    # DoubleTests, and rewords a passing test in a Test* class.
    unit = (imports + "\nfrom calc import double\n\n\n"
            "class DoubleTests({}):\n    def test_one(self):\n"
            "        self.assertEqual(double(2), 4)\n".format(base_class))
    misc = ("from calc import double\n\n\nclass TestMisc:\n"
            "    def test_two(self):\n        assert double(2) == 4\n")
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": BUGGY_CALC, "tests/test_unit.py": unit,
                  "tests/test_misc.py": misc},
        base={},
        head={"calc.py": CALC,
              "tests/test_unit.py": unit + "\n    def test_three(self):\n"
                                           "        self.assertEqual("
                                           "double(3), 6)\n",
              "tests/test_misc.py": misc.replace("== 4", "== 4, 'two'")},
    )

    record = repro(repo, base_sha, tmp_path)

    assert outcomes(record) == {
        "tests/test_misc.py::TestMisc::test_two": "passes-on-base",
        "tests/test_unit.py::DoubleTests::test_three": "red"}
    assert record["line"] == "reproduction: red"


def test_a_repo_that_stops_at_the_first_failure_still_runs_every_test(
        tmp_path, log):
    # The repo's addopts say -x. The added tests run in file order: a pass,
    # a no-signal failure, then a red test, which -x would never reach.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": BUGGY_CALC,
                  "pytest.ini": "[pytest]\naddopts = -x\n"},
        base={},
        head={"calc.py": TRIPLE_CALC.replace("x + 2", "x * 2"),
              "tests/test_calc.py":
              "import calc\n\n\n"
              "def test_a_pass():\n    assert calc.double(2) == 4\n\n\n"
              "def test_b_new():\n    assert calc.triple(2) == 6\n\n\n"
              "def test_c_red():\n    assert calc.double(3) == 6\n"},
    )

    record = repro(repo, base_sha, tmp_path)

    assert outcomes(record) == {
        "tests/test_calc.py::test_a_pass": "passes-on-base",
        "tests/test_calc.py::test_b_new": "no signal",
        "tests/test_calc.py::test_c_red": "red"}
    assert record["line"] == "reproduction: red"


def test_a_test_command_that_is_not_pytest_is_unsupported(tmp_path, log):
    # The Makefile runs pytest, but nothing can pick single tests out of it.
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"pyproject.toml": MAKE_PYPROJECT, "calc.py": BUGGY_CALC,
                  "Makefile": "test:\n\tpython3 -m pytest -q\n"},
        base={},
        head={"calc.py": CALC, "tests/test_calc.py":
              "from calc import double\n\n\n"
              "def test_three():\n    assert double(3) == 6\n"},
    )

    record = repro(repo, base_sha, tmp_path)

    assert record["reproduction"] == "unsupported"
    assert record["line"] == "reproduction: unsupported"
    assert record["test_source"] == "pyproject.toml [tool.command-center] test"
    assert record["tests"] == []
    assert ran(log) == []
