#!/usr/bin/env python3
"""Hourly disk housekeeping for pytest debris, logs, temp dirs and
leftover workspaces (tickets 2464, 2465, 2466, 2467).

Dry-run by default; ``--apply`` deletes and trims. Four sweeps share one
``path size action reason`` table, one housekeeping log and one
funnel-brief warning artifact: the pytest numbered-dir sweep, the
command-center log trim, the empty temp-dir sweep and the leftover
workspace flag listing. The free-space gate is a separate ticket, and
workspace deletion, the owner protocol, the evidence store, backups and
restore tests are out per the slimmed 2026-10-10 approval.

What the pytest sweep does
--------------------------
Under each pytest temp root it inspects the numbered ``pytest-N`` dirs:

* keep the newest 3 by number (mirrors pytest's own
  ``tmp_path_retention_count`` default of 3);
* keep any dir whose ``.lock`` PID is alive (``kill -0`` succeeds, or EPERM);
* skip (never delete) any dir whose lock is missing or unparseable:
  ownership is uncertain, and pytest's own cleanup already handles
  lockless dirs from clean exits;
* keep dead-PID dirs younger than 14 days as protected failure evidence;
* cap total debris at 3 GiB by deleting dead-PID dirs older than 14 days,
  oldest first; when only protected dirs remain above the cap, skip and
  alert (housekeeping log plus funnel-brief warning) instead of evicting.

What the log trim does
---------------------
Under each log root (``~/Library/Logs`` by default) it inspects the
``command-center-*.log`` files, internal-disk only via the shared guard:

* keep any file younger than 14 days as protected failure evidence;
* keep any file held open by a live process (``lsof``, checked immediately
  before trimming; uncertainty keeps, never trims);
* keep any file at or under 25 MiB;
* trim every other file to its newest 25 MiB;
* cap total logs around 200 MiB by summed bytes; when protected files
  alone still exceed the cap after every eligible trim, skip and alert
  (housekeeping log plus funnel-brief warning) instead of evicting.

What the temp sweep does
-----------------------
Under each temp root (the current ``tempfile.gettempdir()`` plus the
TMPDIR-unset fallback, deduplicated) it inspects the ``muse-*``
directories directly below, internal-disk only via the shared guard:

* remove only directories matching ``muse-*`` that are older than 24 h
  and strictly empty (no entries remaining);
* keep anything younger than 24 h, anything non-empty, anything not
  owned by this user, and anything unreadable;
* never touch symlinks, files, or non-matching names: they are not
  inventoried at all.

What the workspace flag listing does
------------------------------------
Under the implement-workspaces root
(``~/.claude/command-center-muse-implement-workspaces`` by default) it
lists the leftover ``run.*`` directories directly below, internal-disk
only via the shared guard:

* every leftover ``run.*`` real dir gets one ``flag`` row carrying its
  summed file bytes;
* the listing never deletes: ``--apply`` leaves every workspace
  untouched, and there is no would-delete state for this sweep;
* symlinks, files and non-matching names are not inventoried at all;
* removal stays where it is: the funnel watch archives leftovers to the
  SSD as it did by hand on 2026-10-07, unchanged by this listing.

Every run, dry-run or apply, prints a ``path size action reason`` table to
stdout, appends deletes, trims and skip-alerts to the housekeeping log,
and rewrites the funnel-brief warning artifact with the current warnings
(possibly empty, which clears a stale alert). Findings are identical in
both modes; only deletion and trimming differ. Nothing is deleted or
trimmed until enablement: the run-keeper wiring calls this without
``--apply``.

First verification, recorded 2026-10-10 (ticket requirement)
------------------------------------------------------------
The plan's premise was "launchd sets no TMPDIR so pytest temp lands under
pytest-of-nateprich on internal disk" (label: inferred). Measured on the
Mac mini:

* GUI-domain launchd processes DO inherit the login session's TMPDIR.
  ``ps -E`` on launchd-spawned funnel session-servers showed
  ``TMPDIR=/var/folders/1j/y46jt30117595jtm_qccrr2h0000gn/T/``. The
  run-keeper is a ``gui/501`` LaunchAgent, so housekeeping running from it
  resolves the same root as a shell: ``tempfile.gettempdir()`` resolves to
  ``/private/var/folders/1j/y46jt30117595jtm_qccrr2h0000gn/T/`` and the
  confirmed root is
  ``/private/var/folders/1j/y46jt30117595jtm_qccrr2h0000gn/T/pytest-of-nateprich``.
  The "no TMPDIR" premise holds only for daemon (non-GUI) context.
* Sample listing of the confirmed root: ``pytest-8599``, ``pytest-8600``,
  ``pytest-8601`` (no ``.lock`` each: clean exits awaiting pytest's own
  atexit cleanup), ``pytest-8602`` (``.lock`` holds live pid 60911),
  ``pytest-current`` symlink to ``pytest-8602``. Sizes 2.0M, 2.0M, 166M,
  370M.
* With TMPDIR unset, ``tempfile.gettempdir()`` returns ``/tmp`` (CPython's
  first fallback), resolved ``/private/tmp``, giving the fallback root
  ``/private/tmp/pytest-of-nateprich``. Sample listing: ``pytest-38``,
  ``pytest-39``, ``pytest-40`` (no locks, 4.5M total), ``pytest-current``
  symlink. Debris from a non-GUI context.

The sweep covers both roots: the current-context root plus the TMPDIR-unset
fallback, deduplicated. Both are internal-disk only; any root resolving
under ``/Volumes`` is refused.

Log trim first verification, recorded 2026-10-11 (ticket requirement)
---------------------------------------------------------------------
The plan's hazard was "trimming a log while launchd holds it open loses
output" (label: inferred). Confirmed on the Mac mini with the real
holder check (``lsof -F0n``): a fixture file held open by the test
process reports held (exit 0, path listed) and the plan marks it keep in
both dry-run and apply, never trimmed; the same file closed reports
unheld (exit 1, no output). The fixture proof lives in
``tests/test_housekeeping.py::test_held_open_log_is_never_trimmed``.

Sample of the confirmed log root ``~/Library/Logs`` (24
``command-center-*.log`` files, ~340 MiB total): ``muse-implement.log``
105 MiB held by 3 processes (2026-10-10, recent),
``muse-implement-standard.log`` 105 MiB held by 3 (2026-10-10, recent),
``muse-review-standard.log`` 100 MiB unheld (2026-09-20, old, trim
candidate), ``muse-review.log`` 26 MiB unheld (2026-10-07, recent, kept).
Old unheld large logs trim; recent or held logs keep.

Safety notes
------------
* ``pytest-current`` and any other symlink, ``garbage-*`` transient renames
  pytest owns mid-cleanup, and non-numeric names are never touched.
* Deletion re-reads the lock and re-checks PID death immediately before
  removing each dir, and refuses targets outside the swept root or on a
  non-internal disk.
* Sizes sum file bytes best-effort; unreadable entries are skipped, which
  undercounts and therefore deletes less, never more.
* Age is the numbered dir's own mtime: pytest tmp dirs are written during
  the run and abandoned after, so dir mtime approximates run end, with the
  14-day evidence window absorbing stragglers.
* Log trim re-checks age and the held-open probe immediately before
  trimming each file, keeps the newest 25 MiB via an atomic same-dir
  rename, and never trims below 25 MiB to meet the total: protected
  files alone over the cap mean skip and alert.
* Log age is the file's own mtime; a trim updates it to the trim moment.
  Files younger than 14 days are never trimmed, even over 25 MiB.
* Temp removal re-checks ownership, age and strict emptiness immediately
  before removing each dir, refuses targets outside the swept root or on
  a non-internal disk, and removes with ``os.rmdir``, which fails rather
  than recursing when a dir became non-empty after the check.
* Temp roots are world-writable (``/tmp``), so the root itself is not
  ownership-checked; every candidate dir is.
* Workspace rows are flag-only inventory: no apply step touches them, so
  a workspace can never be unlinked by any mode, root scoping or guard
  failure in this file.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import getpass
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

#: Mirrors pytest's ``tmp_path_retention_count`` default: its own cleanup
#: already targets everything beyond these.
KEEP_NEWEST = 3
#: Total pytest debris allowed across all swept roots before the sweep
#: starts deleting eligible dirs, oldest first.
CAP_BYTES = 3 * 1024 * 1024 * 1024
#: Dead-PID dirs younger than this are protected failure evidence and are
#: never deleted to meet the cap.
EVIDENCE_DAYS = 14
EVIDENCE_SECONDS = EVIDENCE_DAYS * 24 * 60 * 60

PYTEST_PREFIX = "pytest-"
LOCK_NAME = ".lock"

LOG_ENV = "COMMAND_CENTER_HOUSEKEEPING_LOG"
LOG_DEFAULT = "~/.claude/command-center-housekeeping.log"
#: Append-only record of deletes, skip-alerts and errors. Read at import so
#: tests can monkeypatch the constant; the keeper sets it per run.
LOG_PATH = Path(os.environ.get(LOG_ENV) or os.path.expanduser(LOG_DEFAULT))

BRIEF_ENV = "COMMAND_CENTER_HOUSEKEEPING_BRIEF"
BRIEF_DEFAULT = "~/.claude/command-center-housekeeping-brief.json"
#: Machine-local funnel-brief warning payload, rewritten every run.
#: ``funnel.py brief`` reads it as the ``housekeeping_warnings`` section.
BRIEF_WARNINGS_PATH = Path(
    os.environ.get(BRIEF_ENV) or os.path.expanduser(BRIEF_DEFAULT)
)

NUMBERED = re.compile(r"^pytest-(\d+)$")

#: What to do when the cap cannot be met from eligible dirs. Surfaced in
#: the brief warning so the retry is attached to the alert, not tribal.
RETRY_ACTION = (
    "no action needed: deletions resume automatically once live runs "
    "finish and debris ages past 14 days; if the warning persists, a live "
    "run may be leaking pytest dirs"
)

#: Newest bytes kept per log file. Larger old unheld logs trim to this.
LOG_PER_FILE_BYTES = 25 * 1024 * 1024
#: Total log bytes allowed across all swept roots. "Around" because
#: protected files alone may exceed it after every eligible trim; then
#: the sweep skips and alerts instead of evicting.
LOG_TOTAL_CAP_BYTES = 200 * 1024 * 1024
#: Log files swept: launchd stdout/stderr logs. ``*.err.log`` matches
#: because it ends in ``.log``.
LOG_PATTERN = "command-center-*.log"

#: Override the discovered log dir (single path). Tests use explicit
#: ``--log-root``; the keeper test uses this for a hermetic discovery.
LOG_DIR_ENV = "COMMAND_CENTER_HOUSEKEEPING_LOG_DIR"
#: Override the ``lsof`` binary for the held-open probe. Follows the
#: ``session_log_compress.py`` precedent (``COMMAND_CENTER_SESSION_LOG_LSOF``).
LSOF_ENV = "COMMAND_CENTER_HOUSEKEEPING_LSOF"

#: What to do when protected logs alone exceed the total cap.
LOG_RETRY_ACTION = (
    "no action needed: trims resume automatically once live runs "
    "finish and logs age past 14 days; if the warning persists, a live "
    "run may be leaking log output"
)

#: Temp dirs swept: empty ``muse-*`` dirs left by finished runs.
TEMP_PATTERN = "muse-*"
#: A temp dir must be older than this to be removed.
TEMP_AGE_SECONDS = 24 * 60 * 60
#: Override the discovered temp dirs (single path). Tests use explicit
#: ``--temp-root``; the keeper test uses this for a hermetic discovery.
TEMP_DIR_ENV = "COMMAND_CENTER_HOUSEKEEPING_TEMP_DIR"

#: Workspaces listed: leftover ``run.*`` dirs under the implement
#: workspaces root. Flag-only: inventoried, never deleted.
WORKSPACE_PATTERN = "run.*"
WORKSPACE_DEFAULT = "~/.claude/command-center-muse-implement-workspaces"
#: Override the discovered workspaces root (single path). Tests use
#: explicit ``--workspace-root``; the keeper test uses this for a
#: hermetic discovery.
WORKSPACE_DIR_ENV = "COMMAND_CENTER_HOUSEKEEPING_WORKSPACE_DIR"


def _utc_now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def current_user() -> str:
    """The pytest temp namespace, mirroring ``_pytest.tmpdir.get_user``."""
    try:
        return getpass.getuser()
    except (ImportError, OSError, KeyError):
        return "unknown"


def _tmpdir_without_tmpdir_env() -> str:
    """What ``tempfile.gettempdir`` answers with TMPDIR unset.

    Answers the launchd-daemon-context question literally: temporarily drop
    TMPDIR (and tempfile's cache) and ask again. Single-threaded CLI use
    only; the previous state is always restored.
    """
    saved_environ = os.environ.pop("TMPDIR", None)
    saved_cache = tempfile.tempdir
    try:
        tempfile.tempdir = None
        return tempfile.gettempdir()
    finally:
        if saved_environ is not None:
            os.environ["TMPDIR"] = saved_environ
        tempfile.tempdir = saved_cache


def pytest_roots(user: Optional[str] = None) -> List[Path]:
    """Confirmed pytest temp roots: current context plus TMPDIR-unset fallback.

    The current-context root is what pytest itself uses here
    (``tempfile.gettempdir`` resolved, ``pytest-of-{user}`` below it). The
    fallback covers daemon-context runs that never saw TMPDIR
    (``/private/tmp/pytest-of-{user}`` on this Mac). Deduplicated so a
    TMPDIR-less process sweeps each root once.
    """
    name = user or current_user()
    roots = [
        Path(tempfile.gettempdir()).resolve() / "pytest-of-{}".format(name),
        Path(_tmpdir_without_tmpdir_env()).resolve()
        / "pytest-of-{}".format(name),
    ]
    seen = set()
    ordered = []
    for root in roots:
        if str(root) not in seen:
            seen.add(str(root))
            ordered.append(root)
    return ordered


def log_roots() -> List[Path]:
    """Confirmed log dirs: ``~/Library/Logs`` or the test override.

    ``COMMAND_CENTER_HOUSEKEEPING_LOG_DIR`` replaces discovery with one
    explicit dir, so the keeper test sweeps a fixture instead of the
    machine's real logs.
    """
    override = os.environ.get(LOG_DIR_ENV)
    if override:
        return [Path(override)]
    return [Path(os.path.expanduser("~/Library/Logs"))]


def log_files(root: Path) -> List[Path]:
    """Sorted ``command-center-*.log`` regular files directly under a root.

    Skips symlinks, dirs and non-matching names. Non-recursive: launchd
    writes its logs flat in ``~/Library/Logs``.
    """
    found = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if not fnmatch.fnmatch(entry.name, LOG_PATTERN):
            continue
        found.append(Path(entry.path))
    found.sort()
    return found


def temp_roots() -> List[Path]:
    """Confirmed temp dirs: current context plus TMPDIR-unset fallback.

    ``COMMAND_CENTER_HOUSEKEEPING_TEMP_DIR`` replaces discovery with one
    explicit dir, so the keeper test sweeps a fixture instead of the
    machine's real temp dirs.
    """
    override = os.environ.get(TEMP_DIR_ENV)
    if override:
        return [Path(override)]
    roots = [
        Path(tempfile.gettempdir()).resolve(),
        Path(_tmpdir_without_tmpdir_env()).resolve(),
    ]
    seen = set()
    ordered = []
    for root in roots:
        if str(root) not in seen:
            seen.add(str(root))
            ordered.append(root)
    return ordered


def temp_candidate_dirs(root: Path) -> List[Path]:
    """Sorted ``muse-*`` real dirs directly under a temp root.

    Skips symlinks, files and non-matching names. Non-recursive: temp
    dirs sit flat in the temp dir.
    """
    found = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if not fnmatch.fnmatch(entry.name, TEMP_PATTERN):
            continue
        found.append(Path(entry.path))
    found.sort()
    return found


def workspace_roots() -> List[Path]:
    """Confirmed workspaces root: the implement-workspaces dir or override.

    ``COMMAND_CENTER_HOUSEKEEPING_WORKSPACE_DIR`` replaces discovery with
    one explicit dir, so the keeper test lists a fixture instead of the
    machine's real leftover workspaces.
    """
    override = os.environ.get(WORKSPACE_DIR_ENV)
    if override:
        return [Path(override)]
    return [Path(os.path.expanduser(WORKSPACE_DEFAULT))]


def workspace_candidate_dirs(root: Path) -> List[Path]:
    """Sorted ``run.*`` real dirs directly under a workspaces root.

    Skips symlinks, files and non-matching names. Non-recursive:
    implement workspaces sit flat in the workspaces root.
    """
    found = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if not fnmatch.fnmatch(entry.name, WORKSPACE_PATTERN):
            continue
        found.append(Path(entry.path))
    found.sort()
    return found


def is_internal_disk(path: Path) -> bool:
    """True when a path resolves to the internal disk (never the SSD)."""
    try:
        resolved = os.path.realpath(os.fspath(path))
    except OSError:
        return False
    return resolved != "/Volumes" and not resolved.startswith("/Volumes/")


def _is_within(target: Path, root: Path) -> bool:
    try:
        resolved_target = os.path.realpath(os.fspath(target))
        resolved_root = os.path.realpath(os.fspath(root))
    except OSError:
        return False
    return resolved_target == resolved_root or resolved_target.startswith(
        resolved_root + os.sep
    )


def root_owned_by_me(root: Path) -> bool:
    """True when the sweep root is owned by this process's user.

    ``/private/tmp`` is world-writable; never delete under a root another
    user planted in our namespace.
    """
    try:
        return root.stat().st_uid == os.geteuid()
    except (OSError, AttributeError):
        return False


def read_lock_pid(numbered_dir: Path) -> Optional[int]:
    """The PID in a numbered dir's ``.lock``, or None when uncertain.

    Missing, unreadable, empty and non-numeric locks are all uncertain, as
    are non-positive PIDs (``kill`` gives pid <= 0 process-group semantics,
    which liveness probing must never invoke).
    """
    try:
        text = (numbered_dir / LOCK_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        pid = int(text.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def pid_is_alive(pid: int) -> bool:
    """True when a lock PID must be treated as live.

    ``kill -0`` success means alive; EPERM means a live process owned by
    someone else; ESRCH means dead. Any other failure treats the PID as
    alive: uncertainty keeps, never deletes.
    """
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _resolve_lsof(explicit: Optional[str] = None) -> Optional[str]:
    """The ``lsof`` binary for the held-open probe, if any."""
    if explicit is not None:
        return explicit or None
    return os.environ.get(LSOF_ENV) or shutil.which("lsof")


def _lsof_reports_open(path: Path, lsof: str) -> Optional[bool]:
    """Whether ``lsof`` lists a holder for one path, or None when uncertain.

    Exit 0 with the path listed means held; exit 1 with no output means
    no holder. Anything else (stderr, another exit code, spawn failure)
    is a failed probe, mirroring ``session_log_compress._open_paths``.
    """
    try:
        result = subprocess.run(
            [lsof, "-F0n", str(path)],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.stderr.strip() or result.returncode not in (0, 1):
        return None
    try:
        wanted = os.path.realpath(os.fspath(path))
    except OSError:
        return None
    for field in result.stdout.split(b"\0"):
        field = field.lstrip(b"\n")
        if field.startswith(b"n"):
            name = os.fsdecode(field[1:].rstrip(b"\n"))
            if name and os.path.realpath(name) == wanted:
                return True
    return False


def _proc_reports_open(path: Path) -> Optional[bool]:
    """Whether a ``/proc`` scan finds a holder, or None without ``/proc``.

    Linux fallback when ``lsof`` is missing (CI runners). Unreadable
    entries are skipped best-effort; a missing ``/proc`` (macOS) reads as
    no information rather than no holder.
    """
    proc = Path("/proc")
    try:
        if not proc.is_dir():
            return None
    except OSError:
        return None
    try:
        wanted = os.path.realpath(os.fspath(path))
    except OSError:
        return None
    try:
        pids = list(os.scandir(proc))
    except OSError:
        return None
    for pid_entry in pids:
        try:
            if not pid_entry.name.isdigit():
                continue
            fds = os.scandir(os.path.join(proc, pid_entry.name, "fd"))
        except OSError:
            continue
        try:
            for fd_entry in fds:
                try:
                    target = os.readlink(fd_entry.path)
                except OSError:
                    continue
                # Deleted-but-held files read as "path (deleted)".
                if target.endswith(" (deleted)"):
                    target = target[: -len(" (deleted)")]
                try:
                    if os.path.realpath(target) == wanted:
                        return True
                except OSError:
                    continue
        finally:
            try:
                fds.close()
            except OSError:
                pass
    return False


def log_is_held(path: Path, lsof: Optional[str] = None) -> bool:
    """True when a log must be treated as held open by a live process.

    ``lsof`` first, ``/proc`` when ``lsof`` is missing or its probe fails,
    held on every uncertainty: a failed probe keeps, never trims. The
    caller checks immediately before trimming, so a stale answer is at
    most one trim behind a newly opened handle.
    """
    binary = _resolve_lsof(lsof)
    if binary:
        answer = _lsof_reports_open(path, binary)
        if answer is not None:
            return answer
        fallback = _proc_reports_open(path)
        return True if fallback is None else fallback
    fallback = _proc_reports_open(path)
    return True if fallback is None else fallback


def _allocated_size(st: "os.stat_result") -> int:
    """On-disk bytes for one file: ``st_blocks * 512``, ``du`` semantics.

    A sparse file's apparent ``st_size`` can exceed its allocated bytes by
    orders of magnitude, so cap accounting must count what the disk holds,
    not the logical length. Falls back to ``st_size`` where the platform
    reports no block count (no ``st_blocks`` attribute) or the count is
    unusable (negative).
    """
    blocks = getattr(st, "st_blocks", None)
    if blocks is None or blocks < 0:
        return st.st_size
    return blocks * 512


def dir_size_bytes(path: Path) -> int:
    """Sum allocated file bytes below a dir, best-effort, never following symlinks.

    Counts what the disk holds (``st_blocks * 512`` per file, the ``du -sk``
    semantics) rather than apparent ``st_size``, so sparse pytest debris no
    longer inflates cap accounting. Hard-linked files count once: the walk
    tracks seen ``(st_dev, st_ino)`` pairs and adds the allocated size only
    on first sight, matching ``du`` semantics.
    """
    total = 0
    seen = set()
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    total += _allocated_size(st)
            except OSError:
                continue
    return total


def numbered_dirs(root: Path) -> List[Tuple[int, Path]]:
    """``(number, path)`` for real ``pytest-N`` dirs, number ascending.

    Skips the ``pytest-current`` symlink, ``garbage-*`` renames pytest owns
    mid-cleanup, files, and anything else that is not a numeric dir.
    """
    found = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        match = NUMBERED.match(entry.name)
        if not match:
            continue
        found.append((int(match.group(1)), Path(entry.path)))
    found.sort()
    return found


def _dir_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def plan_pytest_sweep(
    roots: List[Path], now: Optional[float] = None
) -> Tuple[List[Dict[str, object]], int, bool]:
    """Plan the pytest sweep across roots without touching anything.

    Returns ``(rows, total_bytes, capped)``. Each row carries ``path``,
    ``size_bytes``, ``action`` (``keep`` or ``would-delete``) and ``reason``.
    ``capped`` is True when the total still exceeds the cap after every
    eligible dir is planned for deletion: only protected dirs remain.
    """
    now = _utc_now() if now is None else float(now)
    cutoff = now - EVIDENCE_SECONDS
    rows: List[Dict[str, object]] = []
    eligible: List[Tuple[float, str, Dict[str, object]]] = []
    total = 0

    for root in roots:
        try:
            entries = numbered_dirs(root)
        except OSError:
            continue
        newest = {number for number, _ in entries[-KEEP_NEWEST:]} if entries else set()
        for number, path in entries:
            size = dir_size_bytes(path)
            total += size
            row: Dict[str, object] = {
                "path": str(path),
                "size_bytes": size,
                "action": "keep",
                "reason": "",
                "kind": "pytest",
            }
            rows.append(row)
            if number in newest:
                row["reason"] = "within newest {} numbered dirs".format(
                    KEEP_NEWEST
                )
                continue
            pid = read_lock_pid(path)
            if pid is None:
                row["reason"] = (
                    "lock missing or unparseable; ownership uncertain"
                )
                continue
            if pid_is_alive(pid):
                row["reason"] = "lock pid {} alive".format(pid)
                continue
            mtime = _dir_mtime(path)
            if mtime is None or mtime >= cutoff:
                row["reason"] = (
                    "dead lock pid {}; failure evidence under {} days".format(
                        pid, EVIDENCE_DAYS
                    )
                )
                continue
            row["reason"] = "dead lock pid {}; older than {} days".format(
                pid, EVIDENCE_DAYS
            )
            eligible.append((mtime, str(path), row))

    eligible.sort()
    remaining = total
    for _mtime, _path, row in eligible:
        if remaining <= CAP_BYTES:
            break
        row["action"] = "would-delete"
        row["reason"] = (
            "{}. over {} cap; oldest eligible first".format(
                row["reason"], _format_bytes(CAP_BYTES)
            )
        )
        remaining -= int(row["size_bytes"])

    capped = remaining > CAP_BYTES
    if capped:
        # The plan cannot meet the cap: every remaining byte is protected.
        # Rows keep their individual keep reasons; the alert carries the
        # aggregate (log plus brief warning, written by the caller).
        pass
    return rows, total, capped


def _format_bytes(count: int) -> str:
    if count >= 1024 * 1024 * 1024:
        text = "{:.1f} GiB".format(count / (1024 * 1024 * 1024))
        return text.replace(".0 GiB", " GiB")
    if count >= 1024 * 1024:
        return "{:.1f} MiB".format(count / (1024 * 1024))
    if count >= 1024:
        return "{:.1f} KiB".format(count / 1024)
    return "{} B".format(count)


def _still_dead(path: Path, pid: int) -> bool:
    """Re-read the lock immediately before deleting: same dead PID only."""
    current = read_lock_pid(path)
    return current == pid and not pid_is_alive(current)


def apply_deletions(
    rows: List[Dict[str, object]],
    roots: List[Path],
    log: "HousekeepingLog",
) -> None:
    """Delete ``would-delete`` rows that still verify, in place.

    Each target is re-checked immediately before removal: still inside a
    swept root, still on the internal disk, lock still the same dead PID.
    Anything that fails revalidation is kept and logged instead.
    """
    for row in rows:
        if row.get("action") != "would-delete":
            continue
        path = Path(str(row["path"]))
        pid = read_lock_pid(path)
        if pid is None or pid_is_alive(pid):
            row["action"] = "skip"
            row["reason"] = (
                "revalidation before delete found a live or uncertain "
                "lock; kept"
            )
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if not any(_is_within(path, root) for root in roots):
            row["action"] = "skip"
            row["reason"] = "no longer inside a swept root; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if not is_internal_disk(path):
            row["action"] = "skip"
            row["reason"] = "not on the internal disk; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if not _still_dead(path, pid):
            row["action"] = "skip"
            row["reason"] = "lock changed before delete; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            row["action"] = "error"
            row["reason"] = "delete failed: {}".format(exc)
            log.record(
                "error",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        row["action"] = "delete"
        log.record(
            "delete", str(path), int(row["size_bytes"]), str(row["reason"])
        )


def _log_stat(path: Path) -> Tuple[Optional[int], Optional[float], Optional[int]]:
    """``(size, mtime, mode)`` for a log file, Nones when unreadable."""
    try:
        info = path.stat()
    except OSError:
        return None, None, None
    if not stat.S_ISREG(info.st_mode):
        return None, None, None
    return info.st_size, info.st_mtime, stat.S_IMODE(info.st_mode)


def plan_log_sweep(
    roots: List[Path],
    now: Optional[float] = None,
    lsof: Optional[str] = None,
) -> Tuple[List[Dict[str, object]], int, bool]:
    """Plan the log trim across roots without touching anything.

    Returns ``(rows, total_bytes, capped)``. Each row carries ``path``,
    ``size_bytes``, ``action`` (``keep`` or ``would-trim``) and ``reason``.
    Every old unheld file over 25 MiB is marked ``would-trim``; ``capped``
    is True when protected files alone still exceed the 200 MiB total
    after every eligible trim.
    """
    now = _utc_now() if now is None else float(now)
    cutoff = now - EVIDENCE_SECONDS
    rows: List[Dict[str, object]] = []
    total = 0
    reclaimable = 0

    for root in roots:
        try:
            entries = log_files(root)
        except OSError:
            continue
        for path in entries:
            size, mtime, _mode = _log_stat(path)
            if size is None:
                row: Dict[str, object] = {
                    "path": str(path),
                    "size_bytes": 0,
                    "action": "keep",
                    "reason": "unreadable; kept",
                    "kind": "log",
                }
                rows.append(row)
                continue
            total += size
            row = {
                "path": str(path),
                "size_bytes": size,
                "action": "keep",
                "reason": "",
                "kind": "log",
            }
            rows.append(row)
            if mtime is None or mtime >= cutoff:
                row["reason"] = (
                    "failure evidence under {} days; kept".format(
                        EVIDENCE_DAYS
                    )
                )
                continue
            if log_is_held(path, lsof=lsof):
                row["reason"] = "held open by a live process; kept"
                continue
            if size <= LOG_PER_FILE_BYTES:
                row["reason"] = "within {} per-file bound".format(
                    _format_bytes(LOG_PER_FILE_BYTES)
                )
                continue
            row["action"] = "would-trim"
            row["reason"] = "older than {} days, unheld, over {} per-file".format(
                EVIDENCE_DAYS, _format_bytes(LOG_PER_FILE_BYTES)
            )
            reclaimable += size - LOG_PER_FILE_BYTES

    remaining = total - reclaimable
    capped = remaining > LOG_TOTAL_CAP_BYTES
    return rows, total, capped


def _trim_file_to_tail(path: Path, keep_bytes: int, mode: Optional[int]) -> None:
    """Replace a file with its newest ``keep_bytes`` bytes, atomically.

    Reads the tail, writes a same-directory temp file, then renames over
    the original. The mtime updates to the trim moment; the mode is
    preserved when known.
    """
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        if size <= keep_bytes:
            return
        source.seek(size - keep_bytes)
        tail = source.read()
    fd, temp_name = tempfile.mkstemp(
        prefix="." + path.name + ".",
        suffix=".trim.tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as target:
            if mode is not None:
                try:
                    os.fchmod(target.fileno(), mode)
                except OSError:
                    pass
            target.write(tail)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def apply_log_trims(
    rows: List[Dict[str, object]],
    roots: List[Path],
    log: "HousekeepingLog",
    now: Optional[float] = None,
    lsof: Optional[str] = None,
) -> None:
    """Trim ``would-trim`` rows that still verify, in place.

    Each target is re-checked immediately before trimming: still inside
    a swept root, still on the internal disk, still old, still unheld,
    still over the per-file bound. Anything that fails revalidation is
    kept and logged instead.
    """
    now = _utc_now() if now is None else float(now)
    cutoff = now - EVIDENCE_SECONDS
    for row in rows:
        if row.get("action") != "would-trim":
            continue
        path = Path(str(row["path"]))
        size, mtime, mode = _log_stat(path)
        if size is None or mtime is None:
            row["action"] = "skip"
            row["reason"] = "unreadable before trim; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if not any(_is_within(path, root) for root in roots):
            row["action"] = "skip"
            row["reason"] = "no longer inside a swept root; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if not is_internal_disk(path):
            row["action"] = "skip"
            row["reason"] = "not on the internal disk; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if mtime >= cutoff:
            row["action"] = "skip"
            row["reason"] = "written since the plan; failure evidence kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if log_is_held(path, lsof=lsof):
            row["action"] = "skip"
            row["reason"] = "held open before trim; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        if size <= LOG_PER_FILE_BYTES:
            row["action"] = "skip"
            row["reason"] = "shrank below the per-file bound; kept"
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        try:
            _trim_file_to_tail(path, LOG_PER_FILE_BYTES, mode)
        except OSError as exc:
            row["action"] = "error"
            row["reason"] = "trim failed: {}".format(exc)
            log.record(
                "error",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        row["action"] = "trim"
        row["reason"] = "{}. kept newest {}".format(
            row["reason"], _format_bytes(LOG_PER_FILE_BYTES)
        )
        log.record(
            "trim", str(path), int(row["size_bytes"]), str(row["reason"])
        )


def _dir_is_empty(path: Path) -> Optional[bool]:
    """True when a dir holds no entries, None when it cannot be listed.

    ``os.scandir`` never yields ``.`` or ``..``, so any first entry
    proves non-empty. Uncertainty keeps, never deletes.
    """
    try:
        with os.scandir(path) as entries:
            for _entry in entries:
                return False
    except OSError:
        return None
    return True


def plan_temp_sweep(
    roots: List[Path],
    now: Optional[float] = None,
) -> List[Dict[str, object]]:
    """Plan the empty temp-dir sweep across roots without touching anything.

    Each row carries ``path``, ``size_bytes`` (always 0: removed dirs are
    empty), ``action`` (``keep`` or ``would-delete``) and ``reason``.
    Only ``muse-*`` dirs older than 24 h that are strictly empty and
    owned by this user are marked ``would-delete``; everything else is
    kept.
    """
    now = _utc_now() if now is None else float(now)
    cutoff = now - TEMP_AGE_SECONDS
    rows: List[Dict[str, object]] = []

    for root in roots:
        try:
            entries = temp_candidate_dirs(root)
        except OSError:
            continue
        for path in entries:
            row: Dict[str, object] = {
                "path": str(path),
                "size_bytes": 0,
                "action": "keep",
                "reason": "",
                "kind": "temp",
            }
            rows.append(row)
            mtime = _dir_mtime(path)
            if mtime is None:
                row["reason"] = "unreadable; kept"
                continue
            if not root_owned_by_me(path):
                row["reason"] = "not owned by this user; kept"
                continue
            if mtime >= cutoff:
                row["reason"] = "younger than 24 hours; kept"
                continue
            empty = _dir_is_empty(path)
            if empty is None:
                row["reason"] = "could not list entries; kept"
                continue
            if not empty:
                row["reason"] = "not empty; kept"
                continue
            row["action"] = "would-delete"
            row["reason"] = "empty and older than 24 hours"

    return rows


def apply_temp_deletions(
    rows: List[Dict[str, object]],
    roots: List[Path],
    log: "HousekeepingLog",
    now: Optional[float] = None,
) -> None:
    """Remove ``would-delete`` temp rows that still verify, in place.

    Each target is re-checked immediately before removal: still inside
    a swept root, still on the internal disk, still a real dir owned by
    this user, still old, still strictly empty. Removal is ``os.rmdir``,
    which fails rather than recursing if the dir became non-empty after
    the check; that race reads as a skip, never an error. Anything else
    that fails revalidation is kept and logged instead.
    """
    now = _utc_now() if now is None else float(now)
    cutoff = now - TEMP_AGE_SECONDS
    for row in rows:
        if row.get("action") != "would-delete":
            continue
        path = Path(str(row["path"]))

        def _skip(reason: str) -> None:
            row["action"] = "skip"
            row["reason"] = reason
            log.record(
                "skip",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )

        if path.is_symlink():
            _skip("became a symlink before delete; kept")
            continue
        if not path.is_dir():
            _skip("no longer a directory before delete; kept")
            continue
        if not any(_is_within(path, root) for root in roots):
            _skip("no longer inside a swept root; kept")
            continue
        if not is_internal_disk(path):
            _skip("not on the internal disk; kept")
            continue
        if not root_owned_by_me(path):
            _skip("no longer owned by this user; kept")
            continue
        mtime = _dir_mtime(path)
        if mtime is None or mtime >= cutoff:
            _skip("written since the plan; kept")
            continue
        if _dir_is_empty(path) is not True:
            _skip("no longer empty before delete; kept")
            continue
        try:
            os.rmdir(path)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                _skip("became non-empty before delete; kept")
                continue
            row["action"] = "error"
            row["reason"] = "delete failed: {}".format(exc)
            log.record(
                "error",
                str(path),
                int(row["size_bytes"]),
                str(row["reason"]),
            )
            continue
        row["action"] = "delete"
        log.record(
            "delete", str(path), int(row["size_bytes"]), str(row["reason"])
        )


def plan_workspace_sweep(
    roots: List[Path],
) -> List[Dict[str, object]]:
    """List leftover ``run.*`` workspaces across roots without touching them.

    Each row carries ``path``, ``size_bytes`` (summed file bytes,
    best-effort), ``action`` (always ``flag``) and ``reason``. There is
    deliberately no apply step for these rows: ``--apply`` leaves every
    workspace untouched, and removal stays with the funnel-watch SSD
    archive path, which this listing does not change.
    """
    rows: List[Dict[str, object]] = []

    for root in roots:
        try:
            entries = workspace_candidate_dirs(root)
        except OSError:
            continue
        for path in entries:
            rows.append({
                "path": str(path),
                "size_bytes": dir_size_bytes(path),
                "action": "flag",
                "reason": (
                    "leftover implement workspace; flagged only, "
                    "never deleted (funnel-watch archives to SSD)"
                ),
                "kind": "workspace",
            })

    return rows


class HousekeepingLog:
    """Append-only ``key=value`` record of deletes, trims, skip-alerts and errors."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or LOG_PATH

    def record(
        self, action: str, path: str, size: int, reason: str
    ) -> None:
        line = "ts={} action={} path={} size_bytes={} reason={}\n".format(
            _iso(_utc_now()), action, path, size, reason
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            print(
                "housekeeping: cannot write log {}: {}".format(
                    self.path, exc
                ),
                file=sys.stderr,
            )


def capped_warnings(
    rows: List[Dict[str, object]],
    roots: List[Path],
    capped: bool,
    now: Optional[float] = None,
) -> List[Dict[str, object]]:
    """One warning per root still holding protected bytes over the cap.

    Empty when the plan meets the cap. Each warning carries the plan's
    timestamp, path, reason, bytes, cap and retry action; free-space and
    evidence-store fields belong to the out-of-scope gate and backup
    tickets and are deliberately absent.
    """
    if not capped:
        return []
    now = _utc_now() if now is None else float(now)
    remaining_by_root: Dict[str, int] = {}
    for row in rows:
        if row.get("action") in ("would-delete", "delete"):
            continue
        try:
            resolved = os.path.realpath(str(row["path"]))
        except OSError:
            continue
        for root in roots:
            if _is_within(Path(resolved), root):
                key = os.path.realpath(os.fspath(root))
                remaining_by_root[key] = remaining_by_root.get(key, 0) + int(
                    row["size_bytes"]
                )
                break
    remaining_total = sum(remaining_by_root.values())
    warnings = []
    for root_path in sorted(remaining_by_root):
        warnings.append({
            "kind": "pytest-cap",
            "recorded_at": _iso(now),
            "path": root_path,
            "reason": (
                "pytest debris keeps {} over the {} cap after every "
                "eligible deletion; only protected dirs remain "
                "(newest {}, live locks, or failure evidence under "
                "{} days)".format(
                    _format_bytes(remaining_total),
                    _format_bytes(CAP_BYTES),
                    KEEP_NEWEST,
                    EVIDENCE_DAYS,
                )
            ),
            "bytes": remaining_by_root[root_path],
            "cap_bytes": CAP_BYTES,
            "retry": RETRY_ACTION,
        })
    return warnings


def log_capped_warnings(
    rows: List[Dict[str, object]],
    roots: List[Path],
    capped: bool,
    now: Optional[float] = None,
) -> List[Dict[str, object]]:
    """One warning per log root still holding protected bytes over the cap.

    Empty when the plan meets the cap. Trimmed rows count at their
    post-trim size (25 MiB); kept rows count whole. Each warning carries
    the plan's timestamp, path, reason, bytes, cap and retry action.
    """
    if not capped:
        return []
    now = _utc_now() if now is None else float(now)
    remaining_by_root: Dict[str, int] = {}
    for row in rows:
        if row.get("kind") != "log":
            continue
        if row.get("action") in ("would-trim", "trim"):
            counted = min(int(row["size_bytes"]), LOG_PER_FILE_BYTES)
        else:
            counted = int(row["size_bytes"])
        try:
            resolved = os.path.realpath(str(row["path"]))
        except OSError:
            continue
        for root in roots:
            if _is_within(Path(resolved), root):
                key = os.path.realpath(os.fspath(root))
                remaining_by_root[key] = (
                    remaining_by_root.get(key, 0) + counted
                )
                break
    remaining_total = sum(remaining_by_root.values())
    warnings = []
    for root_path in sorted(remaining_by_root):
        warnings.append({
            "kind": "log-cap",
            "recorded_at": _iso(now),
            "path": root_path,
            "reason": (
                "log files keep {} over the {} cap after every "
                "eligible trim; only protected files remain "
                "(held open or failure evidence under "
                "{} days)".format(
                    _format_bytes(remaining_total),
                    _format_bytes(LOG_TOTAL_CAP_BYTES),
                    EVIDENCE_DAYS,
                )
            ),
            "bytes": remaining_by_root[root_path],
            "cap_bytes": LOG_TOTAL_CAP_BYTES,
            "retry": LOG_RETRY_ACTION,
        })
    return warnings


def write_brief_warnings(
    warnings: List[Dict[str, object]],
    path: Optional[Path] = None,
) -> None:
    """Rewrite the funnel-brief warning artifact with current warnings."""
    target = path or BRIEF_WARNINGS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(warnings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            "housekeeping: cannot write brief warnings {}: {}".format(
                target, exc
            ),
            file=sys.stderr,
        )


def brief_warnings(
    path: Optional[Path] = None,
) -> List[Dict[str, object]]:
    """Current housekeeping warnings for the ``housekeeping_warnings`` brief section.

    Missing, unreadable and malformed artifacts all read as no warnings:
    the file exists to carry alerts, and the next hourly run rewrites it.
    """
    target = path or BRIEF_WARNINGS_PATH
    try:
        raw = Path(target).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        loaded = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(loaded, list):
        return []
    return [row for row in loaded if isinstance(row, dict)]


def _resolve_roots(explicit: List[str]) -> Tuple[List[Path], List[str]]:
    """Sweep roots: explicit ``--root`` values or the confirmed discovery.

    Returns ``(roots, notes)``; notes describe skipped roots for the table
    header. Explicit SSD roots are refused outright; discovered problems
    skip with a note.
    """
    notes: List[str] = []
    if explicit:
        roots = []
        for value in explicit:
            root = Path(value)
            if not is_internal_disk(root):
                raise SystemExit(
                    "housekeeping: refusing non-internal-disk root {}".format(
                        value
                    )
                )
            roots.append(root)
        return roots, notes
    roots = []
    for root in pytest_roots():
        if not is_internal_disk(root):
            notes.append("skipped non-internal root {}".format(root))
            continue
        if not root.is_dir():
            notes.append("root not present {}".format(root))
            continue
        if not root_owned_by_me(root):
            notes.append(
                "root not owned by this user; skipped {}".format(root)
            )
            continue
        roots.append(root)
    return roots, notes


def _resolve_log_roots(
    explicit: List[str],
) -> Tuple[List[Path], List[str]]:
    """Log roots: explicit ``--log-root`` values or the confirmed discovery.

    Returns ``(roots, notes)``; notes describe skipped roots for the table
    header. Explicit SSD roots are refused outright; discovered problems
    skip with a note.
    """
    notes: List[str] = []
    if explicit:
        roots = []
        for value in explicit:
            root = Path(value)
            if not is_internal_disk(root):
                raise SystemExit(
                    "housekeeping: refusing non-internal-disk log root {}".format(
                        value
                    )
                )
            roots.append(root)
        return roots, notes
    roots = []
    for root in log_roots():
        if not is_internal_disk(root):
            notes.append("skipped non-internal log root {}".format(root))
            continue
        if not root.is_dir():
            notes.append("log root not present {}".format(root))
            continue
        if not root_owned_by_me(root):
            notes.append(
                "log root not owned by this user; skipped {}".format(root)
            )
            continue
        roots.append(root)
    return roots, notes


def _resolve_temp_roots(
    explicit: List[str],
) -> Tuple[List[Path], List[str]]:
    """Temp roots: explicit ``--temp-root`` values or the confirmed discovery.

    Returns ``(roots, notes)``; notes describe skipped roots for the table
    header. Explicit SSD roots are refused outright; discovered problems
    skip with a note. The root itself is not ownership-checked: ``/tmp``
    is world-writable by design, so every candidate dir is checked
    instead.
    """
    notes: List[str] = []
    if explicit:
        roots = []
        for value in explicit:
            root = Path(value)
            if not is_internal_disk(root):
                raise SystemExit(
                    "housekeeping: refusing non-internal-disk temp root {}".format(
                        value
                    )
                )
            roots.append(root)
        return roots, notes
    roots = []
    for root in temp_roots():
        if not is_internal_disk(root):
            notes.append("skipped non-internal temp root {}".format(root))
            continue
        if not root.is_dir():
            notes.append("temp root not present {}".format(root))
            continue
        roots.append(root)
    return roots, notes


def _resolve_workspace_roots(
    explicit: List[str],
) -> Tuple[List[Path], List[str]]:
    """Workspaces roots: explicit ``--workspace-root`` values or discovery.

    Returns ``(roots, notes)``; notes describe skipped roots for the table
    header. Explicit SSD roots are refused outright; discovered problems
    skip with a note. Listing only: no deletion path reads these roots.
    """
    notes: List[str] = []
    if explicit:
        roots = []
        for value in explicit:
            root = Path(value)
            if not is_internal_disk(root):
                raise SystemExit(
                    "housekeeping: refusing non-internal-disk workspace "
                    "root {}".format(value)
                )
            roots.append(root)
        return roots, notes
    roots = []
    for root in workspace_roots():
        if not is_internal_disk(root):
            notes.append(
                "skipped non-internal workspace root {}".format(root)
            )
            continue
        if not root.is_dir():
            notes.append("workspace root not present {}".format(root))
            continue
        if not root_owned_by_me(root):
            notes.append(
                "workspace root not owned by this user; skipped {}".format(
                    root
                )
            )
            continue
        roots.append(root)
    return roots, notes


def run_pass(
    apply: bool = False,
    explicit_roots: Optional[List[str]] = None,
    log_path: Optional[Path] = None,
    brief_path: Optional[Path] = None,
    now: Optional[float] = None,
    explicit_log_roots: Optional[List[str]] = None,
    lsof: Optional[str] = None,
    explicit_temp_roots: Optional[List[str]] = None,
    explicit_workspace_roots: Optional[List[str]] = None,
) -> Tuple[
    List[Dict[str, object]],
    int,
    bool,
    int,
    bool,
    List[str],
    bool,
    bool,
    bool,
]:
    """Plan (and optionally apply) all sweeps; write log and warnings.

    Returns ``(rows, pytest_total, pytest_capped, log_total, log_capped,
    notes, logs_swept, temps_swept, workspaces_swept)``. ``rows`` is
    pytest rows, then log rows, then temp rows, then workspace rows. Log,
    temp and workspace roots are their explicit values, or the discovered
    dirs when no sweep was scoped explicitly; an explicit run sweeps only
    the sweeps it was asked for, so fixtures stay hermetic. Workspace
    rows are flag-only in both modes: no apply step touches them.
    Skip-alerts go to the housekeeping log and the brief artifact in both
    modes.
    """
    roots, notes = _resolve_roots(list(explicit_roots or []))
    if explicit_log_roots is not None:
        log_roots_resolved, log_notes = _resolve_log_roots(
            list(explicit_log_roots)
        )
        logs_swept = True
    elif explicit_roots is not None or explicit_workspace_roots is not None:
        log_roots_resolved, log_notes = [], []
        logs_swept = False
    else:
        log_roots_resolved, log_notes = _resolve_log_roots([])
        logs_swept = True
    if explicit_temp_roots is not None:
        temp_roots_resolved, temp_notes = _resolve_temp_roots(
            list(explicit_temp_roots)
        )
        temps_swept = True
    elif (
        explicit_roots is not None
        or explicit_log_roots is not None
        or explicit_workspace_roots is not None
    ):
        temp_roots_resolved, temp_notes = [], []
        temps_swept = False
    else:
        temp_roots_resolved, temp_notes = _resolve_temp_roots([])
        temps_swept = True
    if explicit_workspace_roots is not None:
        workspace_roots_resolved, workspace_notes = (
            _resolve_workspace_roots(list(explicit_workspace_roots))
        )
        workspaces_swept = True
    elif (
        explicit_roots is not None
        or explicit_log_roots is not None
        or explicit_temp_roots is not None
    ):
        workspace_roots_resolved, workspace_notes = [], []
        workspaces_swept = False
    else:
        workspace_roots_resolved, workspace_notes = (
            _resolve_workspace_roots([])
        )
        workspaces_swept = True
    notes = (
        list(notes) + list(log_notes) + list(temp_notes)
        + list(workspace_notes)
    )
    log = HousekeepingLog(log_path)
    pytest_rows, pytest_total, pytest_capped = plan_pytest_sweep(
        roots, now=now
    )
    log_rows, log_total, log_capped = plan_log_sweep(
        log_roots_resolved, now=now, lsof=lsof
    )
    temp_rows = plan_temp_sweep(temp_roots_resolved, now=now)
    workspace_rows = plan_workspace_sweep(workspace_roots_resolved)
    if apply:
        apply_deletions(pytest_rows, roots, log)
        apply_log_trims(log_rows, log_roots_resolved, log, now=now, lsof=lsof)
        apply_temp_deletions(temp_rows, temp_roots_resolved, log, now=now)
        # No workspace apply step: flag rows stay flags under --apply.
    warnings = capped_warnings(
        pytest_rows, roots, pytest_capped, now=now
    ) + log_capped_warnings(log_rows, log_roots_resolved, log_capped, now=now)
    for warning in warnings:
        log.record(
            "skip-alert",
            str(warning["path"]),
            int(warning["bytes"]),
            str(warning["reason"]),
        )
    write_brief_warnings(warnings, brief_path)
    return (
        pytest_rows + log_rows + temp_rows + workspace_rows,
        pytest_total,
        pytest_capped,
        log_total,
        log_capped,
        notes,
        logs_swept,
        temps_swept,
        workspaces_swept,
    )


def render_table(
    rows: List[Dict[str, object]],
    pytest_total: int,
    pytest_capped: bool,
    log_total: int,
    log_capped: bool,
    notes: List[str],
    apply: bool,
    logs_swept: bool = True,
    temps_swept: bool = True,
    workspaces_swept: bool = True,
) -> str:
    """The ``path size action reason`` inventory table for all sweeps."""
    pytest_rows = [row for row in rows if row.get("kind") == "pytest"]
    log_rows = [row for row in rows if row.get("kind") == "log"]
    temp_rows = [row for row in rows if row.get("kind") == "temp"]
    workspace_rows = [
        row for row in rows if row.get("kind") == "workspace"
    ]
    lines = [
        "# housekeeping sweep ({})".format(
            "apply" if apply else "dry-run"
        ),
        "# pytest total {} across {} numbered dirs; cap {}; capped={}".format(
            _format_bytes(pytest_total),
            len(pytest_rows),
            _format_bytes(CAP_BYTES),
            "yes" if pytest_capped else "no",
        ),
    ]
    if logs_swept:
        lines.append(
            "# log total {} across {} files; per-file {}; total cap {}; capped={}".format(
                _format_bytes(log_total),
                len(log_rows),
                _format_bytes(LOG_PER_FILE_BYTES),
                _format_bytes(LOG_TOTAL_CAP_BYTES),
                "yes" if log_capped else "no",
            )
        )
    if temps_swept:
        lines.append(
            "# temp {} muse-* dirs (remove only empty dirs older than 24 h)".format(
                len(temp_rows)
            )
        )
    if workspaces_swept:
        lines.append(
            "# workspace {} run.* dirs (flag-only, never deleted)".format(
                len(workspace_rows)
            )
        )
    lines.extend("# note: {}".format(note) for note in notes)
    lines.append("path\tsize_bytes\taction\treason")
    for row in rows:
        lines.append("{}\t{}\t{}\t{}".format(
            row["path"], row["size_bytes"], row["action"], row["reason"]
        ))
    counts: Dict[str, int] = {}
    for row in rows:
        action = str(row["action"])
        counts[action] = counts.get(action, 0) + 1
    summary = ", ".join(
        "{} {}".format(counts[action], action) for action in sorted(counts)
    )
    lines.append("# summary: {}".format(summary or "no rows"))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep pytest numbered-dir debris, trim command-center logs, "
            "remove empty muse-* temp dirs and flag leftover run.* "
            "workspaces. "
            "Dry-run by default; --apply deletes and trims."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete and trim planned paths instead of only listing them",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=None,
        help=(
            "sweep exactly this pytest root instead of discovering the "
            "confirmed pytest temp roots (repeatable; internal disk only)"
        ),
    )
    parser.add_argument(
        "--log-root",
        action="append",
        default=None,
        dest="log_root",
        help=(
            "sweep exactly this log dir instead of discovering "
            "~/Library/Logs (repeatable; internal disk only)"
        ),
    )
    parser.add_argument(
        "--temp-root",
        action="append",
        default=None,
        dest="temp_root",
        help=(
            "sweep exactly this temp dir instead of discovering "
            "the confirmed temp dirs (repeatable; internal disk only)"
        ),
    )
    parser.add_argument(
        "--workspace-root",
        action="append",
        default=None,
        dest="workspace_root",
        help=(
            "list exactly this workspaces dir instead of discovering "
            "the implement-workspaces dir (repeatable; internal disk "
            "only; flag-only, never deleted)"
        ),
    )
    parser.add_argument(
        "--log",
        default=None,
        help="housekeeping log path (default: {})".format(LOG_ENV),
    )
    parser.add_argument(
        "--brief",
        default=None,
        help="brief warnings artifact path (default: {})".format(BRIEF_ENV),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        (
            rows,
            pytest_total,
            pytest_capped,
            log_total,
            log_capped,
            notes,
            logs_swept,
            temps_swept,
            workspaces_swept,
        ) = run_pass(
            apply=args.apply,
            explicit_roots=args.root,
            log_path=Path(args.log) if args.log else None,
            brief_path=Path(args.brief) if args.brief else None,
            explicit_log_roots=args.log_root,
            explicit_temp_roots=args.temp_root,
            explicit_workspace_roots=args.workspace_root,
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # fail loud, never half-applied and silent
        print("housekeeping: pass failed: {}".format(exc), file=sys.stderr)
        return 1
    sys.stdout.write(
        render_table(
            rows,
            pytest_total,
            pytest_capped,
            log_total,
            log_capped,
            notes,
            args.apply,
            logs_swept,
            temps_swept,
            workspaces_swept,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
