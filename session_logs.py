"""Read Muse and Codex session logs from their internal or archive stores."""

from __future__ import annotations

import glob
import gzip
import os
from typing import List, Optional


STORE_ROOTS = {
    "muse": os.path.expanduser("~/.local/share/muse/sessions"),
    "codex": os.path.expanduser("~/.codex/sessions"),
}
DEFAULT_ARCHIVE_BASE = "/Volumes/External SSD/Agent-Logs"
ARCHIVE_ROOT_ENV = "COMMAND_CENTER_AGENT_LOG_ARCHIVE_ROOT"


def archive_root(tool: str, base: Optional[str] = None) -> str:
    """The per-tool archive directory, with a machine-local base override."""
    if tool not in STORE_ROOTS:
        raise ValueError("unsupported session-log tool: {}".format(tool))
    base = base or os.environ.get(ARCHIVE_ROOT_ENV) or DEFAULT_ARCHIVE_BASE
    return os.path.join(os.path.expanduser(base), tool)


def _absolute(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _relative(path: str, root: str) -> Optional[str]:
    path = _absolute(path)
    root = _absolute(root)
    try:
        if os.path.commonpath((path, root)) != root:
            return None
    except ValueError:
        return None
    return os.path.relpath(path, root)


def resolve_path(path: str, tool: str, *, store_root: Optional[str] = None,
                 archive_base: Optional[str] = None) -> Optional[str]:
    """Resolve one uncompressed store path to its readable copy, if present.

    Prefer the internal raw file, then an internal gzip file, then its gzip
    archive copy. Archive paths mirror the content below the internal store
    root, under ``<archive-base>/<tool>/``.
    """
    if tool not in STORE_ROOTS:
        raise ValueError("unsupported session-log tool: {}".format(tool))
    path = _absolute(path)
    if path.endswith(".gz"):
        path = path[:-3]
    store_root = store_root or STORE_ROOTS[tool]
    candidates = [path, path + ".gz"]
    relative = _relative(path, store_root)
    if relative is not None:
        candidates.append(os.path.join(
            archive_root(tool, archive_base), relative + ".gz"))
    return next((candidate for candidate in candidates
                 if os.path.isfile(candidate)), None)


def paths(pattern: str, tool: str, *, store_root: Optional[str] = None,
          archive_base: Optional[str] = None) -> List[str]:
    """List readable session paths, choosing one copy for each logical log."""
    if tool not in STORE_ROOTS:
        raise ValueError("unsupported session-log tool: {}".format(tool))
    store_root = store_root or STORE_ROOTS[tool]
    pattern = os.path.expanduser(pattern)
    canonical = set(glob.glob(pattern))
    canonical.update(path[:-3] for path in glob.glob(pattern + ".gz"))

    relative_pattern = _relative(pattern, store_root)
    if relative_pattern is not None:
        archive_pattern = os.path.join(
            archive_root(tool, archive_base), relative_pattern + ".gz")
        archive_root_path = archive_root(tool, archive_base)
        for archived in glob.glob(archive_pattern):
            relative = _relative(archived, archive_root_path)
            if relative is not None and relative.endswith(".gz"):
                canonical.add(os.path.join(store_root, relative[:-3]))

    resolved = []
    for path in sorted(canonical):
        found = resolve_path(
            path, tool, store_root=store_root, archive_base=archive_base)
        if found is not None:
            resolved.append(found)
    return resolved


def open_text(path: str, *, errors: str = "replace"):
    """Open a raw JSONL file or its ``.gz`` form as text."""
    if path.endswith(".gz"):
        return gzip.open(path, mode="rt", errors=errors)
    return open(path, mode="r", errors=errors)
