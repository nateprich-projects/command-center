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
whose base re-run cannot be compared.

The worktrees are removed on every path. A merge that cannot be made at all
(an unknown base, a worktree git refuses) raises ``ReviewEvidenceError``, so
a caller can fall back to the head-only run; a conflict is evidence and is
recorded instead. Uncommitted changes in the checkout are not part of HEAD
and are not tested.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import re
import shlex
import shutil
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

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


def failing_node_ids(output: str) -> List[str]:
    """The node ids pytest's short summary names as failed or errored.

    Only lines after the last summary header count, so a test that prints
    ``FAILED`` into its captured output cannot add an id. Pytest's default
    report characters (``fE``) print the summary; a repo that turns them
    off leaves nothing to read, and the caller treats that as unknown.
    """
    lines = output.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _SUMMARY_HEADER_RE.match(line.strip()):
            start = index + 1
    if start is None:
        return []
    ids: List[str] = []
    for line in lines[start:]:
        match = _SUMMARY_LINE_RE.match(line)
        if match:
            node_id = _summary_node_id(match.group(1))
            if node_id and node_id not in ids:
                ids.append(node_id)
    return ids


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

    A collection error is reported for its file, so ``t.py`` covers
    ``t.py::f`` either way round.
    """
    return any(node_id == other or node_id.startswith(other + "::")
               or other.startswith(node_id + "::") for other in base_ids)


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

def _run_one(root: pathlib.Path, argv: Sequence[str]) -> Tuple[bool, str]:
    """Run one command through ``run_tests``: (passed, failure output).

    ``run_tests`` raises on a failure with both streams in the message
    (#953), which is where the failing node ids are read from. A timeout
    or a missing program is a failure with nothing to read.
    """
    try:
        implement.run_tests(root, commands=[list(argv)])
    except (implement.ImplementError, OSError) as exc:
        return False, str(exc)
    return True, ""


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
        passed, output = _run_one(base_root, argv)
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


def merged_suite(checkout: os.PathLike, base: str, *,
                 work_dir: Optional[os.PathLike] = None) -> dict:
    """Merge the checkout's HEAD into ``base`` and run the suite there.

    ``work_dir`` is where the temporary worktrees go (default: the system
    temporary directory); it must be outside the checkout. Returns the
    record ``render`` serialises.
    """
    root, head_sha, base_sha = _resolve(checkout, base)
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
        return _merge_and_run(worktree, head_sha, base_sha)
    except (implement.ImplementError, OSError) as exc:
        # run_tests' own failures are caught where they run; anything here
        # is git plumbing or plan resolution, so no merge could be tested.
        raise ReviewEvidenceError(str(exc)) from None
    finally:
        _remove(root, temp, added)


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
        passed, output = _run_one(merge_root, argv)
        entry["result"] = "pass" if passed else "fail"
        if passed:
            continue
        record["result"] = "fail"
        prefix = pytest_prefix(argv)
        failing = failing_node_ids(output) if prefix else []
        if not failing:
            # Another runner, or pytest ids that cannot be read: nothing
            # shows the base shares it, so it counts, and the rest of the
            # plan would not have run on the finish either.
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
