#!/usr/bin/env python3
"""Compress eligible Muse and Codex session logs in place."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Dict, Optional

import usage


DAY = 24 * 60 * 60
CODEX_RAW_DAYS = 14


def muse_compress_before(now: float) -> float:
    """Keep the current and previous provider weekly windows raw."""
    return usage.muse_window_start(now) - usage.SEVEN_DAY


def muse_is_eligible(mtime: float, now: float) -> bool:
    """Whether a Muse journal is older than the two raw weekly windows."""
    return mtime < muse_compress_before(now)


def codex_thread_source(path: Path) -> Optional[str]:
    """Read the rollout's source marker; unknown rollouts stay raw."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"session_meta"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                source = payload.get("thread_source") if isinstance(payload, dict) else None
                return source if isinstance(source, str) and source else None
    except OSError:
        return None
    return None


def codex_is_eligible(path: Path, now: float) -> bool:
    """Compress old Codex rollouts except those started by the user."""
    try:
        if path.stat().st_mtime >= now - CODEX_RAW_DAYS * DAY:
            return False
    except OSError:
        return False
    source = codex_thread_source(path)
    return source is not None and source != "user"


def _signature(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _path_batches(paths):
    batch = []
    size = 0
    for path in paths:
        encoded = os.fsencode(path)
        if batch and size + len(encoded) + 1 > 64 * 1024:
            yield batch
            batch = []
            size = 0
        batch.append(path)
        size += len(encoded) + 1
    if batch:
        yield batch


def _open_paths(paths, lsof: Optional[str]):
    """Return open paths in a few lsof calls, or None when the probe fails."""
    if not paths:
        return set()
    if not lsof:
        return None
    opened = set()
    for batch in _path_batches(paths):
        try:
            result = subprocess.run(
                [lsof, "-F0n", *[str(path) for path in batch]],
                capture_output=True, check=False,
            )
        except OSError:
            return None
        if result.stderr.strip() or result.returncode not in (0, 1):
            return None
        for field in result.stdout.split(b"\0"):
            field = field.lstrip(b"\n")
            if field.startswith(b"n"):
                name = os.fsdecode(field[1:].rstrip(b"\n"))
                if name:
                    opened.add(os.path.realpath(name))
    return opened


def _stage(path: Path):
    """Write gzip bytes to a same-directory temporary file."""
    if path.is_symlink():
        return "skipped", None
    try:
        before = path.lstat()
        before_signature = _signature(path)
    except OSError:
        return "error", None
    if before_signature is None:
        return "skipped", None

    compressed = Path(str(path) + ".gz")
    if compressed.exists():
        return "existing", None

    fd = None
    temporary = None
    keep_temporary = False
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix="." + path.name + ".", suffix=".gz.tmp", dir=str(path.parent)
        )
        temporary = Path(temp_name)
        with path.open("rb") as source, os.fdopen(fd, "wb") as raw_target:
            fd = None
            os.fchmod(raw_target.fileno(), stat.S_IMODE(before.st_mode))
            with gzip.GzipFile(
                filename="", fileobj=raw_target, mode="wb",
                mtime=int(before.st_mtime)
            ) as target:
                shutil.copyfileobj(source, target)
            raw_target.flush()
            os.fsync(raw_target.fileno())
        if _signature(path) != before_signature:
            return "changed", None
        os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
        keep_temporary = True
        return "staged", (path, temporary, before_signature)
    except OSError:
        return "error", None
    finally:
        if fd is not None:
            os.close(fd)
        # A successful stage transfers ownership of the temporary path to its
        # caller; every early return cleans up the incomplete gzip.
        if temporary is not None and not keep_temporary:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _install(path: Path, temporary: Path, before_signature) -> str:
    """Install a staged gzip only while its original source is unchanged."""
    compressed = Path(str(path) + ".gz")
    if compressed.exists():
        return "existing"
    installed = False
    try:
        if _signature(path) != before_signature:
            return "changed"
        # link() fails rather than replacing a gzip another pass created.
        os.link(temporary, compressed)
        installed = True
        if _signature(path) != before_signature:
            compressed.unlink()
            installed = False
            return "changed"
        path.unlink()
        return "compressed"
    except FileExistsError:
        return "existing"
    except OSError:
        if installed and path.exists():
            try:
                compressed.unlink()
            except FileNotFoundError:
                pass
        return "error"


def _files(root: Path, pattern: str):
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob(pattern)
        if path.is_file() and not path.is_symlink()
    )


def compress_pass(
    muse_root: Path, codex_root: Path, now: Optional[float] = None,
    lsof: Optional[str] = None,
) -> Dict[str, int]:
    """Compress eligible files below the two internal stores."""
    now = time.time() if now is None else float(now)
    lsof = lsof if lsof is not None else (
        os.environ.get("COMMAND_CENTER_SESSION_LOG_LSOF") or shutil.which("lsof")
    )
    counts = {
        "compressed": 0, "live": 0, "existing": 0, "changed": 0,
        "skipped": 0, "error": 0,
    }
    muse_cutoff = muse_compress_before(now)

    candidates = []
    for path in _files(muse_root, "session.jsonl"):
        try:
            eligible = path.stat().st_mtime < muse_cutoff
        except OSError:
            counts["error"] += 1
            continue
        if eligible:
            if Path(str(path) + ".gz").exists():
                counts["existing"] += 1
            else:
                candidates.append(path)

    for path in _files(codex_root, "*.jsonl"):
        if codex_is_eligible(path, now):
            if Path(str(path) + ".gz").exists():
                counts["existing"] += 1
            else:
                candidates.append(path)

    opened = _open_paths(candidates, lsof)
    if opened is None:
        counts["error"] += len(candidates)
        return counts

    staged = []
    for path in candidates:
        if os.path.realpath(path) in opened:
            counts["live"] += 1
            continue
        result, staged_file = _stage(path)
        if result == "staged":
            staged.append(staged_file)
        else:
            counts[result] += 1

    staged_paths = [path for path, _temporary, _signature in staged]
    opened_after = _open_paths(staged_paths, lsof)
    if opened_after is None:
        for _path, temporary, _signature in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        counts["error"] += len(staged)
        return counts

    for path, temporary, before_signature in staged:
        try:
            if os.path.realpath(path) in opened_after:
                counts["live"] += 1
            else:
                counts[_install(path, temporary, before_signature)] += 1
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    return counts


def main() -> int:
    muse_root = Path(os.path.expanduser("~/.local/share/muse/sessions"))
    codex_root = Path(os.path.expanduser("~/.codex/sessions"))
    counts = compress_pass(muse_root, codex_root)
    print(
        "session-log compression: compressed={compressed} live={live} "
        "existing={existing} changed={changed} skipped={skipped} "
        "errors={error}".format(**counts)
    )
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
