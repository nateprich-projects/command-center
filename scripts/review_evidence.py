#!/usr/bin/env python3
"""review_evidence.py — run the repo's suite on the head merged with the base (#1802).

A head checkout passing its own suite says nothing about the merge it will
become: another PR can land on main in the meantime, and a behavioural clash
shows only on the merge (#1742). So this merges the checkout's committed HEAD
into a base SHA in a temporary worktree outside the checkout, and runs the
repo's test command there, as ``engine.implement.default_test_plan`` resolves
it and with ``run_tests``' interpreter and environment.

What it records, as schema-versioned JSON of at most 64 KB:

* pass or fail, or a conflict with the conflicted paths;
* for a pytest command, the failing node ids, and those ids alone re-run on
  the base at normal priority, recording which already fail there. A
  failure the base shares is not the head's doing (#1783, ticket 2);
* for any other command, a failure is a failure: nothing can say which part
  of it the base shares.

``blocking`` is the one answer a caller acts on: the head does not merge, or
the merge fails in a way the base does not share or cannot be shown to
share. A pytest failure whose node ids cannot be read counts, as does one
whose base re-run cannot be compared, as does a pytest run that stopped
early: a collection error or ``-x``/``--maxfail`` leaves the rest of the
suite unrun, so what it would have found is unknown.

The worktrees are removed on every path. A merge that cannot be made at all
(an unknown base, a worktree git refuses) raises ``ReviewEvidenceError``, so
a caller can fall back to the head-only run; a conflict is evidence and is
recorded instead. Uncommitted changes in the checkout are not part of HEAD
and are not tested.

``reproduction`` asks the other question a reviewer has: do the tests the
PR adds or changes fail without its code (#1803, plan #1783 ticket 3)? It
runs them on the base, with the PR's test paths from the head, and writes
one line: ``reproduction: red``, ``passes-on-base``, ``no signal``, or
``unsupported`` for a test command that is not pytest. Given a budget, as a
finish gives it (#1805), a run that outlasts it writes ``over budget``.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import json
import os
import pathlib
import re
import shlex
import shutil
import sys
import tempfile
import time
from typing import Dict, List, Optional, Sequence, Set, Tuple
from xml.etree import ElementTree

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import implement  # noqa: E402

SCHEMA_VERSION = 1

#: The output bound. Lists are cut, and the cut is named under
#: ``truncated``, rather than exceed it.
MAX_OUTPUT_BYTES = 64 * 1024

#: No string in the output is longer than this. A CI ``run:`` block can be
#: any length, and with every list cut the rest of the record must still fit.
MAX_STRING_CHARS = 4096

#: The merge commit's identity. A schedule machine may have no git identity
#: configured, and git refuses to commit without one.
COMMITTER = ("Command Center review evidence",
             "review-evidence@command-center.invalid")

#: Configuration every git call here carries. The member repo's hooks are
#: for its own commits, not a throwaway merge (``worktree add`` runs
#: post-checkout), and a signing setup could stop the merge on a prompt.
_GIT_CONFIG = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "commit.gpgsign=false",
    "-c", "user.name={}".format(COMMITTER[0]),
    "-c", "user.email={}".format(COMMITTER[1]),
)


class ReviewEvidenceError(RuntimeError):
    """No merge could be made to test, so no evidence was recorded."""


def _git(cwd: pathlib.Path, *args: str, check: bool = True):
    return implement._run(
        ["git", *_GIT_CONFIG, *args], cwd=cwd, check=check,
        timeout=implement.LOCAL_GIT_TIMEOUT_SECONDS)


# -- pytest output ---------------------------------------------------------

_SUMMARY_HEADER_RE = re.compile(r"^=+ short test summary info =+$")
_SUMMARY_LINE_RE = re.compile(r"^(?:FAILED|ERROR) (.+)$")
_NOT_FOUND_RE = re.compile(
    r"^ERROR: (?:file or directory )?not found: (.+)$")
_STOPPED_RE = re.compile(
    r"^!+ (?:Interrupted: .*|stopping after .*|KeyboardInterrupt) !+$")


def _summary_node_id(rest: str) -> str:
    """The node id from one summary line's text after ``FAILED``/``ERROR``.

    The line is ``<id> - <message>``, but a parametrized id can hold " - "
    itself (``test_p[a - b]``), so a bracket still open at the first " - "
    runs to the ``]`` that ends the id.
    """
    head, sep, _ = rest.partition(" - ")
    if sep and "[" in head and not head.endswith("]"):
        match = re.match(r"(.*?\])(?: - |$)", rest)
        if match:
            return match.group(1)
    return head.strip()


def _summary_lines(output: str) -> List[str]:
    """The lines after pytest's last short-summary header, or none.

    Only these count, so a test that prints ``FAILED`` or ``Interrupted``
    into its captured output, which comes before the summary, cannot
    change what is read.
    """
    lines = output.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _SUMMARY_HEADER_RE.match(line.strip()):
            start = index + 1
    return [] if start is None else lines[start:]


def failing_node_ids(output: str) -> List[str]:
    """The node ids pytest's short summary names as failed or errored.

    Pytest's default report characters (``fE``) print the summary; a repo
    that turns them off leaves nothing to read, and the caller treats that
    as unknown.
    """
    ids: List[str] = []
    for line in _summary_lines(output):
        match = _SUMMARY_LINE_RE.match(line)
        if match:
            node_id = _summary_node_id(match.group(1))
            if node_id and node_id not in ids:
                ids.append(node_id)
    return ids


def stopped_early(output: str) -> bool:
    """Whether pytest stopped before running the whole selection.

    A collection error interrupts the session before any test runs, and
    ``-x``/``--maxfail`` stop it at the first failures; either way the
    ids it names are not all that fails (#1824 review).
    """
    return any(_STOPPED_RE.match(line.strip())
               for line in _summary_lines(output))


def _not_found(output: str, candidates: Sequence[str]) -> List[str]:
    """The candidates pytest refused as not found.

    Pytest names them by absolute path (``ERROR: not found: /…/t.py::f``)
    and runs nothing else, so they are matched by suffix and dropped.
    """
    targets = [match.group(1).strip() for match in
               (_NOT_FOUND_RE.match(line) for line in output.splitlines())
               if match]
    return [node_id for node_id in candidates
            if any(target == node_id or target.endswith("/" + node_id)
                   for target in targets)]


def _shares(node_id: str, base_ids: Sequence[str]) -> bool:
    """Whether the base's failures cover this id.

    A collection error is reported for its file, so a base that cannot
    collect ``t.py`` covers ``t.py::f``. Not the other way round: one red
    test on the base does not cover a whole file the merge cannot collect
    (#1824 review).
    """
    return any(node_id == other or node_id.startswith(other + "::")
               for other in base_ids)


def pytest_prefix(argv: Sequence[str]) -> Optional[List[str]]:
    """The argv up to and including ``pytest``, or None for another runner.

    Only a direct invocation counts (``python3 -m pytest``, ``pytest``,
    ``uv run pytest``). A shell line or ``make test`` may run pytest
    somewhere inside, but nothing here can re-run chosen ids through it.
    """
    words = list(argv)
    if not implement._segment_invokes_pytest(words):
        return None
    for index, word in enumerate(words):
        if os.path.basename(word) in ("pytest", "py.test"):
            return words[:index + 1]
    return None


# -- runs ------------------------------------------------------------------

_TIMEOUT_OUTPUT_LIMIT_BYTES = 2048
_TIMEOUT_SETUP_MARKERS = (
    "could not derive a test command",
    "no module named pytest",
    "requires python ",
)


def _timeout_output_excerpt(output: str) -> str:
    """Keep a bounded tail and safe setup markers from a timed-out command."""
    encoded = output.encode("utf-8", errors="replace")
    if len(encoded) <= _TIMEOUT_OUTPUT_LIMIT_BYTES:
        return output
    excerpt = encoded[-_TIMEOUT_OUTPUT_LIMIT_BYTES:].decode(
        "utf-8", errors="replace")
    folded, shown = output.casefold(), excerpt.casefold()
    preserved = [marker for marker in _TIMEOUT_SETUP_MARKERS
                 if marker in folded and marker not in shown]
    marker_note = ("; setup markers retained: {}".format(
        ", ".join(preserved)) if preserved else "")
    return ("[output truncated; showing final {} bytes{}]\n{}".format(
        _TIMEOUT_OUTPUT_LIMIT_BYTES, marker_note, excerpt))


def _run_one(root: pathlib.Path, argv: Sequence[str],
             timeout: Optional[float] = None, *,
             capture_timeout_output: bool = False
             ) -> Tuple[bool, str, bool]:
    """Run one command: (passed, failure output, timed out).

    ``run_tests`` raises on a failure with both streams in the message
    (#953), which is where the failing node ids are read from. A timeout
    or a missing program is a failure with nothing to read, except that a
    run given its own ``timeout`` (the reproduction's budget, #1805) raises
    ``CommandTimeoutError`` past it: running out of budget says nothing
    about the tests.
    """
    try:
        if timeout is None:
            implement.run_tests(root, commands=[list(argv)])
        else:
            implement.run_tests(root, commands=[list(argv)], timeout=timeout)
    except implement.TestCommandStartError:
        # Preserve the failure kind so the finish can use its fixed,
        # path-free heartbeat reason.
        raise
    except implement.CommandTimeoutError as exc:
        if timeout is not None:
            raise
        output = (exc.captured_output if capture_timeout_output else str(exc))
        return False, output, True
    except (implement.ImplementError, OSError) as exc:
        return False, str(exc), False
    return True, "", False


def rerun_on_base(base_root: pathlib.Path, prefix: Sequence[str],
                  node_ids: Sequence[str]) -> dict:
    """Re-run only ``node_ids`` in a base checkout; record which fail there.

    An id whose file is not in the base is new with the merge and is not
    run: pytest stops at the first missing file and runs nothing. An id in
    a file the base has but naming a test it lacks is refused the same
    way, so those are dropped once and the rest run again. ``result`` is
    None when nothing was left to run.
    """
    ran = [node_id for node_id in node_ids
           if (base_root / node_id.split("::", 1)[0]).exists()]
    absent = [node_id for node_id in node_ids if node_id not in ran]
    record = {"command": None, "result": None, "ran": ran,
              "absent": absent, "already_failing": []}
    for attempt in range(2):
        if not ran:
            break
        # -rfE: the base run's summary is what is read, whatever the repo's
        # own report characters. The cache guard is run_tests' own.
        argv = list(prefix) + ["-q", "-rfE"] + ran
        passed, output, _ = _run_one(base_root, argv)
        missing = [] if passed or attempt else _not_found(output, ran)
        if missing:
            ran = [node_id for node_id in ran if node_id not in missing]
            absent.extend(missing)
            continue
        record["command"] = shlex.join(argv)
        record["result"] = "pass" if passed else "fail"
        if not passed:
            base_failing = failing_node_ids(output)
            record["already_failing"] = [
                node_id for node_id in ran if _shares(node_id, base_failing)]
        break
    record["ran"] = ran
    record["absent"] = absent
    return record


def _resolve(checkout: os.PathLike, base: str) -> Tuple[pathlib.Path, str, str]:
    """The checkout's top level, its HEAD SHA and the base's commit SHA."""
    try:
        top = implement._run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=pathlib.Path(checkout), check=False,
            timeout=implement.LOCAL_GIT_TIMEOUT_SECONDS)
    except OSError as exc:
        raise ReviewEvidenceError("cannot read the checkout: {}".format(exc))
    if top.returncode != 0:
        raise ReviewEvidenceError("not a git checkout: {}".format(checkout))
    root = pathlib.Path(top.stdout.strip()).resolve()
    shas = []
    for name, ref in (("head", "HEAD"), ("base", base)):
        proc = _git(root, "rev-parse", "--verify", "--quiet",
                    "{}^{{commit}}".format(ref), check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise ReviewEvidenceError(
                "the {} {!r} is not a commit in {}".format(name, ref, root))
        shas.append(proc.stdout.strip())
    return root, shas[0], shas[1]


@contextlib.contextmanager
def _worktrees(root: pathlib.Path, work_dir: Optional[os.PathLike]):
    """Yield ``worktree(name, sha)``, which adds a detached worktree.

    They go in one temporary directory under ``work_dir`` (default: the
    system temporary directory), which must be outside the checkout, and
    are removed with it on every path. Shared by the merged suite and the
    reproduction run (#1803).
    """
    if work_dir is not None:
        pathlib.Path(work_dir).mkdir(parents=True, exist_ok=True)
    temp = pathlib.Path(tempfile.mkdtemp(
        prefix="review-evidence-",
        dir=str(work_dir) if work_dir is not None else None)).resolve()
    added: List[pathlib.Path] = []

    def worktree(name: str, sha: str) -> pathlib.Path:
        path = temp / name
        if path in added:
            return path
        proc = _git(root, "worktree", "add", "--detach", "--quiet",
                    str(path), sha, check=False)
        if proc.returncode != 0:
            raise ReviewEvidenceError("git worktree add failed: {}".format(
                (proc.stderr or proc.stdout).strip()))
        added.append(path)
        return path

    try:
        if temp == root or root in temp.parents:
            raise ReviewEvidenceError(
                "the merge worktree must be outside the checkout: "
                "{}".format(temp))
        yield worktree
    except (implement.ImplementError, OSError) as exc:
        # run_tests' own failures are caught where they run; anything here
        # is git plumbing or plan resolution, so no merge could be tested.
        raise ReviewEvidenceError(str(exc)) from None
    finally:
        _remove(root, temp, added)


def merged_suite(checkout: os.PathLike, base: str, *,
                 work_dir: Optional[os.PathLike] = None) -> dict:
    """Merge the checkout's HEAD into ``base`` and run the suite there.

    ``work_dir`` is where the temporary worktrees go (default: the system
    temporary directory); it must be outside the checkout. Returns the
    record ``render`` serialises.
    """
    root, head_sha, base_sha = _resolve(checkout, base)
    with _worktrees(root, work_dir) as worktree:
        return _merge_and_run(worktree, head_sha, base_sha)


def _merge_and_run(worktree, head_sha: str, base_sha: str) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "head": head_sha,
        "base": base_sha,
        "merge": None,
        "result": "conflict",
        "blocking": True,
        "conflicts": [],
        "test_source": None,
        "commands": [],
        "failing": [],
        "base_rerun": None,
        "new_failures": [],
    }
    merge_root = worktree("merge", base_sha)
    # --ff overrides a repo's merge.ff=only or false: either is a policy for
    # its branches, not for a throwaway test merge.
    proc = _git(merge_root, "merge", "--ff", "--no-edit", "--quiet",
                head_sha, check=False)
    if proc.returncode != 0:
        conflicts = [path for path in _git(
            merge_root, "diff", "--name-only", "--diff-filter=U", "-z",
            check=False).stdout.split("\0") if path]
        if not conflicts:
            raise ReviewEvidenceError("git merge failed: {}".format(
                (proc.stderr or proc.stdout).strip()))
        record["conflicts"] = conflicts
        return record
    record["merge"] = _git(merge_root, "rev-parse", "HEAD").stdout.strip()
    commands, source = implement.default_test_plan(merge_root)
    record["test_source"] = source
    record["result"] = "pass"
    record["blocking"] = False
    entries = [{"command": shlex.join(argv), "result": "not-run"}
               for argv in commands]
    record["commands"] = entries
    for argv, entry in zip(commands, entries):
        try:
            passed, output, timed_out = _run_one(
                merge_root, argv, capture_timeout_output=True)
        except implement.TestCommandStartError:
            entry["result"] = "fail"
            entry["start_failed"] = True
            record["result"] = "fail"
            record["blocking"] = True
            break
        entry["result"] = "pass" if passed else "fail"
        if passed:
            continue
        if timed_out:
            entry["timed_out"] = True
            entry["output"] = _timeout_output_excerpt(output)
        record["result"] = "fail"
        prefix = pytest_prefix(argv)
        failing = failing_node_ids(output) if prefix and not timed_out else []
        if not failing or stopped_early(output):
            # Another runner, pytest ids that cannot be read, or a pytest
            # run that stopped before the rest of the suite ran: nothing
            # shows the base shares it, so it counts, and the rest of the
            # plan would not have run on the finish either.
            record["failing"] = failing
            record["blocking"] = True
            break
        # One pytest command at most: default_test_plan has one test slot.
        rerun = rerun_on_base(worktree("base", base_sha), prefix, failing)
        record["failing"] = failing
        record["base_rerun"] = rerun
        record["new_failures"] = [
            node_id for node_id in failing
            if node_id not in rerun["already_failing"]]
        if record["new_failures"]:
            record["blocking"] = True
            break
        # Every failure is already on the base: the head did not cause it,
        # so the rest of the plan still runs.
    return record


def _remove(root: pathlib.Path, temp: pathlib.Path,
            added: Sequence[pathlib.Path]) -> None:
    """Remove the worktrees and their directory; never raise.

    Cleanup runs on every path, including an exception's, so it must not
    replace that exception with its own.
    """
    removed = True
    for path in reversed(added):
        try:
            proc = _git(root, "worktree", "remove", "--force", str(path),
                        check=False)
            removed = removed and proc.returncode == 0
        except (implement.ImplementError, OSError):
            removed = False
    shutil.rmtree(str(temp), ignore_errors=True)
    if not removed:
        try:
            _git(root, "worktree", "prune", check=False)
        except (implement.ImplementError, OSError):
            pass


# -- reproduction ----------------------------------------------------------

#: One added test's outcome on the base, and the line's value, which is the
#: tests' outcomes combined; ``unsupported`` is the line's alone (#1803).
RED = "red"
PASSES_ON_BASE = "passes-on-base"
NO_SIGNAL = "no signal"
UNSUPPORTED = "unsupported"
#: The line's value when the tests outlast the budget a caller gives
#: (#1805): no test has an outcome then.
OVER_BUDGET = "over budget"

#: A call-phase failure that only says the base lacks a symbol. When the PR
#: adds that symbol, the test never reached the behaviour it is about.
_MISSING_SYMBOL_RE = re.compile(
    r"^(?:ImportError|ModuleNotFoundError|AttributeError|NameError): "
    r"(?:cannot import name '(\w+)'|No module named '([\w.]+)'"
    r"|.*?has no attribute '(\w+)'|name '(\w+)' is not defined)")

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _is_test_file(path: str) -> bool:
    """Whether pytest collects the file by default (``test_*.py``,
    ``*_test.py``)."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py"))


def _is_test_path(path: str) -> bool:
    """Whether a path belongs to the tests rather than the code under test.

    A test file, a conftest, or anything under a ``tests``/``test``
    directory, so helpers and fixture data travel with the tests.
    """
    parts = path.split("/")
    return (_is_test_file(path) or parts[-1] == "conftest.py"
            or any(part in ("test", "tests") for part in parts[:-1]))


def _changed_paths(root: pathlib.Path, old: str,
                   new: str) -> List[Tuple[str, str]]:
    """(status, path) for every path that differs from ``old`` to ``new``.

    With renames off, a rename is a delete and an add, so each side's path
    is named as itself.
    """
    fields = _git(root, "diff", "--name-status", "--no-renames", "-z",
                  old, new).stdout.split("\0")
    return [(fields[index][:1], fields[index + 1])
            for index in range(0, len(fields) - 1, 2) if fields[index]]


def _blob(root: pathlib.Path, sha: str, path: str) -> Optional[str]:
    """The file's text at ``sha``, or None where it does not exist."""
    proc = _git(root, "cat-file", "blob", "{}:{}".format(sha, path),
                check=False)
    return proc.stdout if proc.returncode == 0 else None


def _is_test_class(node: ast.ClassDef) -> bool:
    """A ``Test*`` class, or a ``unittest.TestCase`` subclass of any name,
    which pytest collects too (#1829 review). A class it turns out not to
    collect is dropped as not found."""
    return node.name.startswith("Test") or any(
        (isinstance(base, ast.Name) and base.id.endswith("TestCase"))
        or (isinstance(base, ast.Attribute)
            and base.attr.endswith("TestCase"))
        for base in node.bases)


def _test_spans(source: str) -> List[Tuple[str, int, int]]:
    """(name, first line, last line) of each test pytest collects by default.

    ``test*`` functions at module level and in test classes, nested ones
    included, named as in a node id (``TestC::test_f``). A test's lines
    start at its first decorator, so a changed parametrize list changes
    the test. A file that does not parse has none.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans: List[Tuple[str, int, int]] = []

    def walk(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test"):
                first = min([node.lineno] + [
                    decorator.lineno for decorator in node.decorator_list])
                spans.append((prefix + node.name, first, node.end_lineno))
            elif isinstance(node, ast.ClassDef) and _is_test_class(node):
                walk(node.body, prefix + node.name + "::")

    walk(tree.body, "")
    return spans


def _pr_test_ids(root: pathlib.Path, merge_base: str, head_sha: str,
                 paths: Sequence[str]) -> List[str]:
    """The node ids of the tests the PR adds or changes, in file order.

    Read from the hunks of the PR's own diff, from the merge base as a PR
    shows it, so a test the base changed since is not the PR's. A test is
    changed when a line the PR adds lies in it at the head, or a line the
    PR removes lay in it at the merge base and it is still there. A test
    whose own lines are untouched is not listed, even in a changed file.
    """
    ids: List[str] = []
    for path in paths:
        diff = _git(root, "--literal-pathspecs", "diff", "--no-color",
                    "--no-ext-diff", "--no-textconv", "--no-renames", "-U0",
                    merge_base, head_sha, "--", path).stdout
        added_lines, removed_lines = set(), set()
        for line in diff.splitlines():
            match = _HUNK_RE.match(line)
            if not match:
                continue
            old, old_count, new, new_count = (
                int(group) if group is not None else 1
                for group in match.groups())
            removed_lines.update(range(old, old + old_count))
            added_lines.update(range(new, new + new_count))
        head_spans = _test_spans(_blob(root, head_sha, path) or "")
        touched = {name for name, first, last in head_spans
                   if any(first <= line <= last for line in added_lines)}
        touched.update(
            name for name, first, last in
            _test_spans(_blob(root, merge_base, path) or "")
            if any(first <= line <= last for line in removed_lines))
        for name, _, _ in head_spans:
            node_id = "{}::{}".format(path, name)
            if name in touched and node_id not in ids:
                ids.append(node_id)
    return ids


def _defined_names(source: str) -> Set[str]:
    """Every name a module defines or binds: functions, classes, assigned
    names and attributes, and imported names."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.Attribute) \
                and isinstance(node.ctx, ast.Store):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".")[0])
    return names


def _added_symbols(root: pathlib.Path, base_sha: str, head_sha: str,
                   paths: Sequence[str]) -> Set[str]:
    """The names the PR's code defines that the base does not.

    Across the non-test Python files the PR changes: what the head's copy
    defines less what the base's does, and for a module the base lacks, its
    own name and its packages'. Defined, not mentioned: a fix that adds a
    line calling ``.strip()`` does not make an AttributeError on ``strip``
    a missing symbol.
    """
    added: Set[str] = set()
    for path in paths:
        if not path.endswith(".py"):
            continue
        head = _blob(root, head_sha, path)
        if head is None:
            continue
        base = _blob(root, base_sha, path)
        added |= _defined_names(head) - _defined_names(base or "")
        if base is None:
            added.update(part for part in path[:-3].split("/")
                         if part != "__init__")
    return added


def _junit_key(node_id: str) -> Tuple[str, str]:
    """The (classname, name) pytest's JUnit report gives a node id.

    ``tests/t.py::TestC::test_f`` is ``("tests.t.TestC", "test_f")``; a
    file's collection error is reported as ``("", "tests.t")``.
    """
    names = node_id.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    return ".".join(names[:-1]), names[-1]


def _junit_cases(path: pathlib.Path) -> List[Tuple[Tuple[str, str], str,
                                                   str]]:
    """(key, kind, first message line) per case in pytest's JUnit report.

    The key drops parameters, so every case of a parametrized test shares
    its function's. The kind is ``failure`` (a call-phase failure: pytest
    reports setup and teardown failures as ``error``), ``error`` (those,
    and a file that cannot be collected), ``skipped``, or ``pass``.
    """
    try:
        tree = ElementTree.parse(str(path))
    except (OSError, ElementTree.ParseError):
        return []
    cases = []
    for case in tree.iter("testcase"):
        key = (case.get("classname") or "",
               (case.get("name") or "").split("[", 1)[0])
        kind, message = "pass", ""
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                kind, message = tag, (child.get("message") or "").strip()
                break
        cases.append((key, kind, message.splitlines()[0] if message else ""))
    return cases


class _OverBudget(Exception):
    """The reproduction's test runs outlasted its budget (#1805)."""


def _run_added(tree: pathlib.Path, prefix: Sequence[str],
               node_ids: Sequence[str], report: pathlib.Path,
               deadline: Optional[float] = None
               ) -> Tuple[list, Dict[str, str]]:
    """Run the node ids in the tree: (JUnit cases, dropped id → reason).

    Pytest runs nothing when one node id is in a file it cannot collect or
    names a test it does not find. So when the first run ran none of them,
    those ids are dropped, and the rest run once more: a file's collection
    error is read from the report, a test not found from the output, as
    the base re-run does.

    With a ``deadline`` (``time.monotonic``), each run gets what is left of
    it, and ``_OverBudget`` is raised when none is (#1805).
    """
    pending = list(node_ids)
    dropped: Dict[str, str] = {}
    cases: list = []
    for attempt in range(2):
        if not pending:
            break
        # A report left by the first run must not be read as the second's.
        with contextlib.suppress(FileNotFoundError):
            report.unlink()
        # --maxfail=0 overrides a repo's -x: a red test after the first
        # failure must still run (#1829 review).
        argv = list(prefix) + ["-q", "--maxfail=0",
                               "--junitxml={}".format(report)] + pending
        remaining = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _OverBudget()
        try:
            passed, output, _ = _run_one(tree, argv, timeout=remaining)
        except implement.CommandTimeoutError:
            raise _OverBudget() from None
        cases = _junit_cases(report)
        keys = {key for key, _, _ in cases}
        if passed or attempt or any(
                _junit_key(node_id) in keys for node_id in pending):
            break
        uncollected = {key for key, kind, _ in cases
                       if kind == "error" and not key[0]}
        for node_id in pending:
            if _junit_key(node_id.split("::", 1)[0]) in uncollected:
                dropped[node_id] = "collection error"
        for node_id in _not_found(output, pending):
            dropped.setdefault(node_id, "not collected")
        if not any(node_id in dropped for node_id in pending):
            break
        pending = [node_id for node_id in pending if node_id not in dropped]
    return cases, dropped


def _combine(outcomes: Sequence[str]) -> str:
    """Red if any is red; else passes-on-base if any passes; else no
    signal, which is also the answer for no tests at all."""
    if RED in outcomes:
        return RED
    if PASSES_ON_BASE in outcomes:
        return PASSES_ON_BASE
    return NO_SIGNAL


def _case_outcome(kind: str, message: str, added: Set[str]) -> str:
    """A pass passes on the base; a call-phase failure is red, a crash and
    ``DID NOT RAISE`` included, unless it only names a missing symbol the
    PR adds. A setup error, a collection error or a skip is no signal."""
    if kind == "pass":
        return PASSES_ON_BASE
    if kind != "failure":
        return NO_SIGNAL
    match = _MISSING_SYMBOL_RE.match(message)
    if match:
        symbol = next(group for group in match.groups() if group)
        if symbol.rsplit(".", 1)[-1] in added:
            return NO_SIGNAL
    return RED


def reproduction(checkout: os.PathLike, base: str, *,
                 work_dir: Optional[os.PathLike] = None,
                 budget: Optional[float] = None) -> dict:
    """Run the tests the checkout's HEAD adds or changes on ``base``.

    The tree is the base with the PR's changed test paths (tests, conftests,
    their helpers and data) taken from the head, so every non-test path the
    PR changes is the base's. ``work_dir`` is as for ``merged_suite``.
    Returns the record ``render`` serialises; ``line`` is the one
    reproduction line.

    ``budget`` bounds the call in seconds, from its start (#1805: a finish
    gives it four minutes). The test runs get what is left of it; one that
    outlasts it is stopped, and the line is ``reproduction: over budget``
    with no test outcomes. The worktree is removed on that path too.
    """
    deadline = None if budget is None else time.monotonic() + budget
    root, head_sha, base_sha = _resolve(checkout, base)
    with _worktrees(root, work_dir) as worktree:
        return _reproduce(root, worktree, head_sha, base_sha, deadline)


def _reproduce(root: pathlib.Path, worktree, head_sha: str,
               base_sha: str, deadline: Optional[float] = None) -> dict:
    record = {
        "schema_version": SCHEMA_VERSION,
        "head": head_sha,
        "base": base_sha,
        "reproduction": NO_SIGNAL,
        "line": None,
        "test_source": None,
        "tests": [],
    }
    proc = _git(root, "merge-base", base_sha, head_sha, check=False)
    if proc.returncode != 0:
        raise ReviewEvidenceError("the head and the base share no history")
    merge_base = proc.stdout.strip()
    changed = _changed_paths(root, merge_base, head_sha)
    test_paths = [(status, path) for status, path in changed
                  if _is_test_path(path)]
    tree = worktree("reproduction", base_sha)
    kept = [path for status, path in test_paths if status != "D"]
    if kept:
        _git(tree, "--literal-pathspecs", "checkout", head_sha, "--", *kept)
    for status, path in test_paths:
        if status == "D" and (tree / path).is_file():
            (tree / path).unlink()
    commands, record["test_source"] = implement.default_test_plan(tree)
    prefix = next(filter(None, map(pytest_prefix, commands)), None)
    if prefix is None:
        # Nothing here can pick single tests out of another runner.
        record["reproduction"] = UNSUPPORTED
    else:
        node_ids = _pr_test_ids(root, merge_base, head_sha, [
            path for path in kept if _is_test_file(path)])
        added = _added_symbols(root, base_sha, head_sha, [
            path for _, path in changed if not _is_test_path(path)])
        try:
            cases, dropped = _run_added(tree, prefix, node_ids,
                                        tree.parent / "reproduction.xml",
                                        deadline)
        except _OverBudget:
            record["reproduction"] = OVER_BUDGET
            record["line"] = "reproduction: {}".format(OVER_BUDGET)
            return record
        for node_id in node_ids:
            key = _junit_key(node_id)
            results = [(_case_outcome(kind, message, added), message)
                       for case_key, kind, message in cases
                       if case_key == key]
            if node_id in dropped or not results:
                outcome = NO_SIGNAL
                detail = dropped.get(node_id, "not run")
            else:
                outcome = _combine([result for result, _ in results])
                detail = next(message for result, message in results
                              if result == outcome)
            record["tests"].append(
                {"id": node_id, "outcome": outcome, "detail": detail})
        record["reproduction"] = _combine(
            [test["outcome"] for test in record["tests"]])
    record["line"] = "reproduction: {}".format(record["reproduction"])
    return record


# -- output ----------------------------------------------------------------

def _cap_strings(value, path: str, truncated: Dict[str, dict]):
    """Cut every string to ``MAX_STRING_CHARS``, naming each cut path.

    A list's items share one path (``failing[]``), whose ``total`` is the
    longest, so the cuts cannot outgrow the bound themselves.
    """
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            entry = truncated.setdefault(
                path, {"kept": MAX_STRING_CHARS, "total": 0})
            entry["total"] = max(entry["total"], len(value))
            return value[:MAX_STRING_CHARS]
        return value
    if isinstance(value, dict):
        return {key: _cap_strings(item, "{}.{}".format(path, key)
                                  if path else key, truncated)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_cap_strings(item, path + "[]", truncated) for item in value]
    return value


def _lists(value, path: str = ""):
    """Every (dotted path, parent, key) whose value is a list."""
    if isinstance(value, dict):
        for key, item in value.items():
            child = "{}.{}".format(path, key) if path else key
            if isinstance(item, list):
                yield child, value, key
            else:
                yield from _lists(item, child)


def render(record: dict) -> str:
    """Serialise a record as JSON of at most ``MAX_OUTPUT_BYTES``.

    Over the bound, the longest list is halved until it fits, and each cut
    is named under ``truncated`` with how many items were kept of how many,
    so a count survives even when its list does not.
    """
    truncated: Dict[str, dict] = {}
    out = _cap_strings(copy.deepcopy(record), "", truncated)
    out["truncated"] = truncated
    while True:
        text = json.dumps(out, sort_keys=True)
        if len(text.encode("utf-8")) <= MAX_OUTPUT_BYTES:
            return text
        candidates = [(len(parent[key]), path, parent, key)
                      for path, parent, key in _lists(out) if parent[key]]
        if not candidates:
            raise ReviewEvidenceError("record does not fit in {} bytes".format(
                MAX_OUTPUT_BYTES))
        size, path, parent, key = max(candidates, key=lambda item: item[0])
        entry = truncated.setdefault(path, {"kept": size, "total": size})
        parent[key] = parent[key][:size // 2]
        entry["kept"] = len(parent[key])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("checkout", help="the head checkout; its HEAD is merged")
    parser.add_argument("base", help="the base SHA to merge it into")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        record = merged_suite(pathlib.Path(args.checkout), args.base)
        text = render(record)
    except ReviewEvidenceError as exc:
        print("review_evidence: {}".format(exc), file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
