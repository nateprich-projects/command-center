#!/usr/bin/env python3
"""Move verified compressed session logs to the machine-local SSD archive.

The funnel watch runs ``python3 session_log_archive.py`` on its existing
app-hosted cadence; ``run_daily_pass`` archives once per local day, on the
first call at or after 03:00. It must not be called by the launchd
run-keeper, which cannot write to the external volume.
"""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Callable, Dict, Optional, Tuple, Union
import zlib

import session_logs


ARCHIVE_HOUR = 3
DAILY_STAMP_NAME = ".last-daily-pass"
READABLE_SAMPLE_BYTES = 64 * 1024
CODEX_ARCHIVE_THREAD_SOURCES = frozenset({"automation", "subagent", "review"})
Pathish = Union[str, Path]
MountCheck = Callable[[Path], bool]


def _volume_root(base: Path) -> Path:
    """The external mount holding ``base``: ``/Volumes/<name>`` when under it."""
    parts = base.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return Path(*parts[:3])
    return base.parent


def _ensure_base(base: Path) -> bool:
    """Create the archive base and confirm this user can write into it."""
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return base.is_dir() and os.access(str(base), os.W_OK | os.X_OK)


def _mounted(volume_root: Path) -> bool:
    """A present directory is not enough; require the external mount itself."""
    try:
        return volume_root.is_dir() and os.path.ismount(str(volume_root))
    except OSError:
        return False


def _signature(path: Path) -> Optional[Tuple[int, int, int, int]]:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sample(path: Path) -> bytes:
    with gzip.open(str(path), "rb") as handle:
        return handle.read(READABLE_SAMPLE_BYTES)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_copy(source: Pathish, destination: Pathish) -> Tuple[bool, str]:
    """Check compressed size, readable sample, and full-file checksum."""
    source = Path(source)
    destination = Path(destination)
    source_signature = _signature(source)
    destination_signature = _signature(destination)
    if source_signature is None or destination_signature is None:
        return False, "not a regular file"
    if source_signature[2] != destination_signature[2]:
        return False, "size mismatch"
    try:
        if _sample(source) != _sample(destination):
            return False, "readable sample mismatch"
        if _sha256(source) != _sha256(destination):
            return False, "checksum mismatch"
    except (OSError, EOFError, zlib.error):
        return False, "unreadable sample or checksum"
    return True, "verified"


def _archive_file(source: Path, destination: Path) -> Tuple[str, bool]:
    """Install and verify one copy before unlinking its internal source."""
    original = _signature(source)
    if original is None:
        return "error", False

    copied = False
    created_destination = False
    temporary: Optional[Path] = None
    try:
        if destination.is_symlink():
            return "retained", False

        if destination.exists():
            verified, _reason = verify_copy(source, destination)
            if not verified:
                return "retained", False
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix="." + destination.name + ".",
                suffix=".tmp",
                dir=str(destination.parent),
            )
            temporary = Path(temp_name)
            with os.fdopen(fd, "wb") as output_file:
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())

            if _signature(source) != original:
                return "retained", False
            verified, _reason = verify_copy(source, temporary)
            if not verified:
                return "retained", False

            try:
                # link() installs without replacing an archive another pass
                # may have created after the exists() check.
                os.link(str(temporary), str(destination))
                created_destination = True
                copied = True
            except FileExistsError:
                if destination.is_symlink():
                    return "retained", False
                verified, _reason = verify_copy(source, destination)
                if not verified:
                    return "retained", False

        verified, _reason = verify_copy(source, destination)
        if not verified or _signature(source) != original:
            if created_destination:
                try:
                    destination.unlink()
                except OSError:
                    pass
            return "retained", False

        source.unlink()
        return "moved", copied
    except OSError:
        if created_destination:
            try:
                destination.unlink()
            except OSError:
                pass
        return "error", False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _compressed_logs(root: Path):
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*.jsonl.gz")
        if _signature(path) is not None
    )


def _codex_thread_source(path: Path) -> Optional[str]:
    """Read the Codex rollout source marker from a compressed log."""
    with gzip.open(
        str(path), "rt", encoding="utf-8", errors="replace"
    ) as handle:
        for line in handle:
            if '"session_meta"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (
                not isinstance(record, dict)
                or record.get("type") != "session_meta"
            ):
                continue
            payload = record.get("payload")
            source = (
                payload.get("thread_source") if isinstance(payload, dict) else None
            )
            return source if isinstance(source, str) and source else None
    return None


def archive_pass(
    muse_root: Optional[Pathish] = None,
    codex_root: Optional[Pathish] = None,
    archive_base: Optional[Pathish] = None,
    volume_root: Optional[Pathish] = None,
    mount_check: Optional[MountCheck] = None,
) -> Dict[str, Union[int, str]]:
    """Archive internal gzip logs, retaining every source that fails checks."""
    base = _archive_base(archive_base)
    mount_value = volume_root if volume_root is not None else _volume_root(base)
    mounted_path = Path(os.path.expanduser(str(mount_value)))
    check_mount = mount_check or _mounted
    summary: Dict[str, Union[int, str]] = {
        "status": "ok", "copied": 0, "deleted": 0,
        "retained": 0, "errors": 0,
    }

    if not check_mount(mounted_path):
        summary["status"] = "unmounted"
        return summary
    if not _ensure_base(base):
        # No source is touched when the archive base cannot hold copies.
        summary["status"] = "error"
        summary["errors"] = 1
        return summary

    roots = {
        "muse": Path(muse_root or session_logs.STORE_ROOTS["muse"]).expanduser(),
        "codex": Path(codex_root or session_logs.STORE_ROOTS["codex"]).expanduser(),
    }
    for tool, root in roots.items():
        archive_root = base / tool
        for source in _compressed_logs(root):
            if tool == "codex":
                try:
                    thread_source = _codex_thread_source(source)
                except (OSError, EOFError, zlib.error):
                    summary["retained"] = int(summary["retained"]) + 1
                    summary["errors"] = int(summary["errors"]) + 1
                    continue
                if thread_source not in CODEX_ARCHIVE_THREAD_SOURCES:
                    summary["retained"] = int(summary["retained"]) + 1
                    continue
            destination = archive_root / source.relative_to(root)
            result, copied = _archive_file(source, destination)
            if copied:
                summary["copied"] = int(summary["copied"]) + 1
            if result == "moved":
                summary["deleted"] = int(summary["deleted"]) + 1
            else:
                summary["retained"] = int(summary["retained"]) + 1
                summary["errors"] = int(summary["errors"]) + 1
    if summary["errors"]:
        summary["status"] = "error"
    return summary


def _summary_line(summary: Dict[str, Union[int, str]]) -> str:
    if summary["status"] == "unmounted":
        return "session-log archive: skipped; external volume is not mounted"
    return (
        "session-log archive: status={status} copied={copied} deleted={deleted} "
        "retained={retained} errors={errors}"
    ).format(**summary)


def _archive_base(archive_base: Optional[Pathish]) -> Path:
    base_value = (
        archive_base
        or os.environ.get(session_logs.ARCHIVE_ROOT_ENV)
        or session_logs.DEFAULT_ARCHIVE_BASE
    )
    return Path(os.path.expanduser(str(base_value)))


def _ran_today(stamp: Path, today: str) -> bool:
    try:
        return stamp.read_text(encoding="utf-8").strip() == today
    except OSError:
        return False


def _record_run(stamp: Path, today: str) -> None:
    try:
        stamp.write_text(today + "\n", encoding="utf-8")
    except OSError:
        pass


def run_daily_pass(
    now: Optional[datetime.datetime] = None,
    *,
    muse_root: Optional[Pathish] = None,
    codex_root: Optional[Pathish] = None,
    archive_base: Optional[Pathish] = None,
    volume_root: Optional[Pathish] = None,
    mount_check: Optional[MountCheck] = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Archive once per local day, on the first call at or after 03:00.

    A pass that reaches the archive base records the day on the volume; an
    unmounted volume or an unwritable base records nothing, so a later call
    the same day tries again.
    """
    local_now = datetime.datetime.now().astimezone() if now is None else now.astimezone()
    if local_now.hour < ARCHIVE_HOUR:
        return 0
    today = local_now.date().isoformat()
    stamp = _archive_base(archive_base) / DAILY_STAMP_NAME
    if _ran_today(stamp, today):
        return 0
    try:
        summary = archive_pass(
            muse_root=muse_root,
            codex_root=codex_root,
            archive_base=archive_base,
            volume_root=volume_root,
            mount_check=mount_check,
        )
        emit(_summary_line(summary))
        if summary["status"] != "unmounted" and stamp.parent.is_dir():
            _record_run(stamp, today)
    except Exception:
        # Archiving is maintenance; a missing or unhealthy destination must
        # not stop the app-hosted funnel lane.
        emit("session-log archive: skipped after an error; funnel lane continues")
    return 0


def main() -> int:
    return run_daily_pass()


if __name__ == "__main__":
    raise SystemExit(main())
