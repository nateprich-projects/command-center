#!/usr/bin/env python3
"""fix_recurrence.py — does a Broken fix hold? Measured from git (#1682).

A squash commit on main names its ticket in its subject, ``… (#<ticket>)
(#<pr>)``. Given which tickets belong to Broken projects, this reads the
trailing window of those commits and answers two questions (Broken here
means a defect project, Broken or Bug: the brief's ``broken_fix_tickets``
join counts both since #1845):

* **Fix-on-fix share.** Of the Broken-fix commits that modify existing
  non-test code, how many rewrite mostly lines that another Broken project's
  fix wrote in the seven days before? Measured with ``git blame`` on the
  commit's parent, so nobody has to report a cause (#1682: the optional
  ``--caused-by`` marker had 7 uses against about 23 prose citations).
* **Hotspots.** Which functions, named by diff hunk headers, do distinct
  Broken projects keep touching? Three in a week is the redesign threshold.

Everything is derived from git on each call; nothing is stored.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

WINDOW = timedelta(days=7)
HOTSPOT_THRESHOLD = 3
PRIOR_FIX_WINDOW = timedelta(days=30)
PRIOR_FIX_BUDGET_SECONDS = 4 * 60

#: The squash subject the merge gate writes: ``Title (#ticket) (#pr)``.
TICKET_SUBJECT_RE = re.compile(r"\(#(\d+)\) \(#\d+\)\s*$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@(.*)$")
_FUNCTION_RE = re.compile(
    r"(?:^|\s)(?:async\s+)?(?:def|class|function)\s+([A-Za-z_]\w*)"
)
_NON_CODE_SUFFIXES = (
    ".md", ".json", ".jsonl", ".txt", ".yml", ".yaml", ".toml", ".plist",
    ".csv", ".lock",
)


class RecurrenceError(RuntimeError):
    """Input the measurement cannot use; the caller reports a gap."""


def is_code_path(path: str) -> bool:
    """Non-test source: what a fix changes when it changes behaviour."""
    parts = path.split("/")
    name = parts[-1]
    if any(part in ("tests", "test", "docs", "fixtures") for part in parts[:-1]):
        return False
    if (name.startswith(("test_", ".")) or name.endswith(
            ("_test.py", ".test.js", ".spec.js", ".svg"))):
        return False
    return not name.endswith(_NON_CODE_SUFFIXES)


def _diff_path(header: str) -> Optional[str]:
    """The path in a ``--- a/x`` or ``+++ b/x`` header, or None for /dev/null.

    Git appends a tab to a path containing a space and C-quotes a path with
    unusual bytes; both are undone here rather than silently mis-read.
    """
    text = header[4:].rstrip("\n")
    if text.endswith("\t"):
        text = text[:-1]
    if text == "/dev/null":
        return None
    if text.startswith('"') and text.endswith('"'):
        raw = text[1:-1].encode("latin-1", "backslashreplace")
        text = raw.decode("unicode_escape").encode("latin-1").decode(
            "utf-8", "replace")
    return text[2:] if text[:2] in ("a/", "b/") else text


def _git(repo: Path, *args: str, timeout: Optional[float] = None) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RecurrenceError("git {} timed out".format(args[0])) from exc
    if result.returncode != 0:
        raise RecurrenceError(
            "git {} failed: {}".format(args[0], result.stderr.strip()))
    return result.stdout


def ticket_commits(repo: Path, since: datetime, until: datetime,
                   ref: str = "HEAD", *,
                   timeout: Optional[float] = None
                   ) -> List[Tuple[str, datetime, int]]:
    """First-parent commits in ``[since, until]`` whose subject names a ticket."""
    out = _git(repo, "log", "--first-parent", "--format=%H%x09%ct%x09%P%x09%s",
               "--since={}".format(since.isoformat()),
               "--until={}".format(until.isoformat()), ref, timeout=timeout)
    rows = []
    for line in out.splitlines():
        sha, stamp, parents, subject = line.split("\t", 3)
        match = TICKET_SUBJECT_RE.search(subject)
        if match and parents.strip():
            rows.append((sha, datetime.fromtimestamp(int(stamp), timezone.utc),
                         int(match.group(1))))
    return rows


def _hunks(repo: Path, sha: str, *, base: Optional[str] = None,
           timeout: Optional[float] = None
           ) -> List[Tuple[str, int, int, Optional[str]]]:
    """``(path, old_start, old_count, function)`` for each code hunk.

    The diff flags pin the output format against user git config (external
    diff drivers, colour, ``diff.noprefix``). A ``---``/``+++`` line is a
    header only directly after ``diff --git`` and its extended headers, so a
    removed code line that begins ``-- `` is never taken for one. By default
    the comparison is ``sha^..sha``; ``base`` selects an explicit old tree.
    """
    old = base if base is not None else sha + "^"
    diff = _git(repo, "diff", "-U0", "--no-color", "--no-ext-diff",
                "-M", "--inter-hunk-context=0",
                "--src-prefix=a/", "--dst-prefix=b/", old, sha,
                timeout=timeout)
    old_path: Optional[str] = None
    in_header = False
    hunks = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header = True
            old_path = None
            continue
        if in_header and line.startswith("--- "):
            old_path = _diff_path(line)
            continue
        if in_header and line.startswith("+++ "):
            # A new file has no old lines to blame; old_path stays None.
            in_header = False
            continue
        if line.startswith("@@"):
            in_header = False
        match = _HUNK_RE.match(line)
        if match and old_path and is_code_path(old_path):
            count = int(match.group(2)) if match.group(2) is not None else 1
            function = _FUNCTION_RE.search(match.group(3) or "")
            hunks.append((old_path, int(match.group(1)), count,
                          function.group(1) if function else None))
    return hunks


def _blame(repo: Path, sha: str, path: str,
           ranges: Sequence[Tuple[int, int]], since: datetime, *,
           at: Optional[str] = None, timeout: Optional[float] = None
           ) -> List[Optional[str]]:
    """The commit that last wrote each listed line of ``path`` before ``sha``.

    ``--since`` only bounds how far blame walks. When the walk stops, git
    credits every older line to the *boundary* commit, which did not write
    it; such lines come back as ``None`` so they can never read as a recent
    fix, whatever ``since`` is (#1687 review 6).
    """
    # An empty ignore-revs file overrides any blame.ignoreRevsFile setting.
    args = ["blame", "--porcelain", "--ignore-revs-file", "",
            "--since={}".format(since.isoformat())]
    for start, count in ranges:
        args += ["-L", "{},+{}".format(start, count)]
    target = at if at is not None else sha + "^"
    out = _git(repo, *args, target, "--", path, timeout=timeout)
    authors: List[Optional[str]] = []
    boundary: Set[str] = set()
    current = None
    for line in out.splitlines():
        header = re.match(r"^([0-9a-f]{40}) \d+ \d+", line)
        if header:
            current = header.group(1)
        elif line == "boundary" and current:
            boundary.add(current)
        elif line.startswith("\t") and current:
            authors.append(current)
    return [None if author in boundary else author for author in authors]


def measure(repo: Path, fix_projects: Mapping[int, int], now: datetime,
            window: timedelta = WINDOW) -> Dict[str, object]:
    """The fix-on-fix pair and the hotspot list for the window ending ``now``.

    ``fix_projects`` maps each Broken-fix ticket to its parent project. A
    commit counts in the numerator when most of the existing lines it
    modifies were written, within the window before it, by a fix for a
    *different* Broken project; a project's own later ticket continuing its
    earlier one is ordinary sequencing, not recurrence.
    """
    history = ticket_commits(repo, now - 2 * window, now)
    fix_sha: Dict[str, Tuple[datetime, int]] = {
        sha: (when, fix_projects[ticket])
        for sha, when, ticket in history if ticket in fix_projects
    }
    numerator = denominator = 0
    touched: Dict[Tuple[str, str], Set[int]] = {}
    examples: List[Dict[str, object]] = []
    for sha, when, ticket in history:
        if when < now - window or ticket not in fix_projects:
            continue
        project = fix_projects[ticket]
        hunks = _hunks(repo, sha)
        for path, _start, _count, function in hunks:
            if function:
                touched.setdefault((path, function), set()).add(project)
        by_path: Dict[str, List[Tuple[int, int]]] = {}
        for path, start, count, _function in hunks:
            if count > 0:
                by_path.setdefault(path, []).append((start, count))
        if not by_path:
            continue
        lines = recent_fix = 0
        for path, ranges in by_path.items():
            for author in _blame(repo, sha, path, ranges, when - window):
                lines += 1
                earlier = fix_sha.get(author) if author else None
                if (earlier and earlier[1] != project
                        and when - window <= earlier[0] < when):
                    recent_fix += 1
        if not lines:
            continue
        denominator += 1
        if recent_fix * 2 > lines:
            numerator += 1
            examples.append({"sha": sha[:12], "ticket": ticket,
                             "project": project, "lines": lines,
                             "recent_fix_lines": recent_fix})
    hotspots = [
        {"path": path, "function": function,
         "projects": sorted(projects), "count": len(projects)}
        for (path, function), projects in touched.items()
        if len(projects) >= 2
    ]
    hotspots.sort(key=lambda row: (-row["count"], row["path"], row["function"]))
    return {
        "window_days": window.days,
        "numerator": numerator,
        "denominator": denominator,
        "share": round(numerator / denominator, 3) if denominator else None,
        "hotspot_threshold": HOTSPOT_THRESHOLD,
        "hotspots": hotspots,
        "examples": examples,
    }


def fix_projects_from_snapshot(snapshot: Mapping[str, object]) -> Dict[int, int]:
    """Read ``broken_fix_tickets`` from a brief or a published snapshot."""
    brief = snapshot.get("brief") if isinstance(snapshot.get("brief"), Mapping) else snapshot
    section = brief.get("recorded_cause_regressions") if isinstance(brief, Mapping) else None
    rows = section.get("broken_fix_tickets") if isinstance(section, Mapping) else None
    if not isinstance(rows, list):
        raise RecurrenceError(
            "the snapshot's recorded_cause_regressions carries no "
            "broken_fix_tickets")
    mapping = {}
    for row in rows:
        if not (isinstance(row, Mapping) and isinstance(row.get("ticket"), int)
                and isinstance(row.get("project"), int)):
            raise RecurrenceError(
                "malformed broken_fix_tickets row: {!r}".format(row))
        mapping[row["ticket"]] = row["project"]
    return mapping


def _published_snapshot() -> Mapping[str, object]:
    """Read the newest locally published brief snapshot without running a brief."""
    try:
        import funnel

        data, _warnings = funnel._newest_snapshot_entry(
            funnel._dashboard_spool_dir())
    except Exception as exc:
        raise RecurrenceError("could not read the published snapshot") from exc
    if data is None:
        raise RecurrenceError("no published snapshot is available")
    try:
        snapshot = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecurrenceError("the published snapshot is not valid JSON") from exc
    if not isinstance(snapshot, Mapping):
        raise RecurrenceError("the published snapshot is not an object")
    return snapshot


def prior_fixes_touched(
        repo: Path, base: str, head: str
) -> List[Tuple[int, str, Optional[str]]]:
    """List prior Broken-fix tickets whose code lines this diff rewrites.

    The snapshot limits candidates to recent fixes in this repository. Blame
    runs against ``base`` so it names the author of the old lines the PR
    removes, and the scan stops after ``PRIOR_FIX_BUDGET_SECONDS``. Failures
    are reported as ``RecurrenceError`` for the finish caller to treat as
    optional evidence.
    """
    fix_projects = fix_projects_from_snapshot(_published_snapshot())
    deadline = monotonic() + PRIOR_FIX_BUDGET_SECONDS

    def remaining() -> float:
        seconds = deadline - monotonic()
        if seconds <= 0:
            raise RecurrenceError("prior-fix scan exceeded its budget")
        return seconds

    raw_timestamp = _git(
        repo, "show", "-s", "--format=%ct", head, timeout=remaining()).strip()
    try:
        head_at = datetime.fromtimestamp(int(raw_timestamp), timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RecurrenceError("could not read the PR head timestamp") from exc
    since = head_at - PRIOR_FIX_WINDOW
    fix_commits = {
        sha: ticket
        for sha, when, ticket in ticket_commits(
            repo, since, head_at, ref=base, timeout=remaining())
        if ticket in fix_projects and since <= when <= head_at
    }
    if not fix_commits:
        return []

    by_hunk: Dict[Tuple[str, Optional[str]], List[Tuple[int, int]]] = {}
    for path, start, count, function in _hunks(
            repo, head, base=base, timeout=remaining()):
        if count > 0:
            by_hunk.setdefault((path, function), []).append((start, count))

    touched: Set[Tuple[int, str, Optional[str]]] = set()
    for (path, function), ranges in sorted(
            by_hunk.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        authors = _blame(
            repo, head, path, ranges, since, at=base, timeout=remaining())
        for author in authors:
            ticket = fix_commits.get(author) if author else None
            if ticket is not None:
                touched.add((ticket, path, function))
    return sorted(touched, key=lambda row: (row[0], row[1], row[2] or ""))


def _parse_now(text: Optional[str]) -> datetime:
    if not text:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", required=True,
                        help="brief or published snapshot JSON")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent),
                        help="git checkout to read; default this file's")
    parser.add_argument("--now", help="window end, ISO 8601; default now")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        snapshot = json.loads(Path(args.snapshot).read_text())
        result = measure(Path(args.repo), fix_projects_from_snapshot(snapshot),
                         _parse_now(args.now))
    except (OSError, ValueError, RecurrenceError) as exc:
        print("fix_recurrence: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
