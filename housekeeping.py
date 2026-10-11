#!/usr/bin/env python3
"""Hourly disk housekeeping for pytest debris (ticket 2464).

Dry-run by default; ``--apply`` deletes. Only the pytest numbered-dir sweep
is implemented here: logs, temp dirs and the free-space gate are separate
tickets, and workspace deletion, the owner protocol, the evidence store,
backups and restore tests are out per the slimmed 2026-10-10 approval.

What the sweep does
------------------
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

Every run, dry-run or apply, prints a ``path size action reason`` table to
stdout, appends deletes and skip-alerts to the housekeeping log, and
rewrites the funnel-brief warning artifact with the current warnings
(possibly empty, which clears a stale alert). Findings are identical in
both modes; only deletion differs. Nothing is deleted until enablement:
the run-keeper wiring calls this without ``--apply``.

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
"""

from __future__ import annotations

import argparse
import errno
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def dir_size_bytes(path: Path) -> int:
    """Sum file bytes below a dir, best-effort, never following symlinks."""
    total = 0
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
                    total += entry.stat(follow_symlinks=False).st_size
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


class HousekeepingLog:
    """Append-only ``key=value`` record of deletes, skip-alerts and errors."""

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


def run_pass(
    apply: bool = False,
    explicit_roots: Optional[List[str]] = None,
    log_path: Optional[Path] = None,
    brief_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Tuple[List[Dict[str, object]], int, bool, List[str]]:
    """Plan (and optionally apply) the pytest sweep; write log and warnings.

    Returns ``(rows, total_bytes, capped, notes)``. Skip-alerts go to the
    housekeeping log and the brief artifact in both modes.
    """
    roots, notes = _resolve_roots(list(explicit_roots or []))
    log = HousekeepingLog(log_path)
    rows, total, capped = plan_pytest_sweep(roots, now=now)
    if apply:
        apply_deletions(rows, roots, log)
    warnings = capped_warnings(rows, roots, capped, now=now)
    for warning in warnings:
        log.record(
            "skip-alert",
            str(warning["path"]),
            int(warning["bytes"]),
            str(warning["reason"]),
        )
    write_brief_warnings(warnings, brief_path)
    return rows, total, capped, notes


def render_table(
    rows: List[Dict[str, object]],
    total: int,
    capped: bool,
    notes: List[str],
    apply: bool,
) -> str:
    """The ``path size action reason`` inventory table."""
    lines = [
        "# housekeeping pytest sweep ({})".format(
            "apply" if apply else "dry-run"
        ),
        "# total {} across {} numbered dirs; cap {}; capped={}".format(
            _format_bytes(total),
            len(rows),
            _format_bytes(CAP_BYTES),
            "yes" if capped else "no",
        ),
    ]
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
    lines.append("# summary: {}".format(summary or "no numbered dirs"))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Sweep pytest numbered-dir debris. Dry-run by default; "
            "--apply deletes."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete planned dirs instead of only listing them",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help=(
            "sweep exactly this root instead of discovering the confirmed "
            "pytest temp roots (repeatable; internal disk only)"
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
        rows, total, capped, notes = run_pass(
            apply=args.apply,
            explicit_roots=args.root,
            log_path=Path(args.log) if args.log else None,
            brief_path=Path(args.brief) if args.brief else None,
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # fail loud, never half-applied and silent
        print("housekeeping: pass failed: {}".format(exc), file=sys.stderr)
        return 1
    sys.stdout.write(
        render_table(rows, total, capped, notes, args.apply)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
