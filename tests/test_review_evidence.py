"""The fresh-merge suite: scripts/review_evidence.py (#1802, plan #1783 ticket 2).

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
    # parametrize id is where a naive split of pytest's summary line breaks.
    red = "tests/test_base.py::test_red[a - b]"
    repo, base_sha, _ = make_repo(
        tmp_path,
        ancestor={"calc.py": CALC, "tests/test_calc.py": CALC_TESTS},
        base={"tests/test_base.py":
              "import pytest\n\nfrom calc import double\n\n\n"
              "@pytest.mark.parametrize('label', ['a - b'])\n"
              "def test_red(label):\n    assert double(2) == 5\n"},
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


def test_the_worktree_is_removed_when_the_run_raises(tmp_path, monkeypatch):
    repo, base_sha, _ = make_repo(
        tmp_path, ancestor={"tests/test_calc.py": CALC_TESTS},
        base={}, head={})
    work = tmp_path / "work"
    seen = []

    def explode(root, commands=None):
        seen.append(pathlib.Path(root))
        raise RuntimeError("the runner fell over")

    monkeypatch.setattr(implement, "run_tests", explode)
    with pytest.raises(RuntimeError, match="fell over"):
        review_evidence.merged_suite(repo, base_sha, work_dir=work)

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
