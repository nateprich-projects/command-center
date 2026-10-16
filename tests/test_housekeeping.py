"""The pytest debris sweep deletes only old dead-PID dirs and keeps the rest.

Dead-PID numbered dirs older than 14 days and beyond the newest 3 are
deleted oldest-first down to the 3 GiB cap; alive locks, the newest 3,
recent failure evidence and uncertain locks are kept in both dry-run and
``--apply``. When only protected dirs remain above the cap the sweep
skips and alerts through the housekeeping log and the funnel-brief
warning artifact.

The log trim keeps the newest 25 MiB per file and caps total logs around
200 MiB: old unheld files over 25 MiB trim to their newest 25 MiB, while
held-open files, recent failure evidence and small files are kept in
both modes. When protected files alone exceed the total the sweep skips
and alerts on both channels.

The temp sweep removes only ``muse-*`` dirs older than 24 h that are
strictly empty: younger, non-empty, unowned and unreadable dirs are kept
in both modes, and symlinks, files and non-matching names are never
inventoried.

The workspace flag listing is inventory only: every leftover ``run.*``
dir gets a ``flag`` row in both dry-run and ``--apply``, and no mode
ever unlinks a workspace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import housekeeping


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 24 * 60 * 60
GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


def _dead_pid() -> int:
    """A PID that is certainly dead: a reaped child."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _make_numbered(
    root: Path,
    number: int,
    *,
    pid_text=None,
    age_days: float = 20.0,
    payload_bytes: int = 0,
    sparse: bool = True,
) -> Path:
    """Build a ``pytest-N`` fixture dir; the dir mtime is set last.

    Payloads are sparse by default (seek plus one byte: large ``st_size``
    with almost no disk write). Pass ``sparse=False`` for fully written
    payloads, where apparent and allocated sizes agree, so cap tests can
    push real on-disk bytes over a patched cap quickly.
    """
    path = root / "pytest-{}".format(number)
    path.mkdir(parents=True, exist_ok=True)
    if pid_text is not None:
        (path / ".lock").write_text(pid_text, encoding="utf-8")
    if payload_bytes:
        payload = path / "payload.bin"
        if sparse:
            with payload.open("wb") as handle:
                handle.seek(payload_bytes - 1)
                handle.write(b"\0")
        else:
            with payload.open("wb") as handle:
                chunk = b"x" * (1024 * 1024)
                remaining = payload_bytes
                while remaining > 0:
                    piece = chunk[: min(len(chunk), remaining)]
                    handle.write(piece)
                    remaining -= len(piece)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _allocated_bytes(path: Path) -> int:
    """On-disk bytes for one fixture file, from the OS, not the walk.

    The ``du`` semantics (``st_blocks * 512``) with the same ``st_size``
    fallback; builds test expectations without calling ``dir_size_bytes``,
    so the walk, summation and de-dup are what the test verifies.
    """
    info = path.stat()
    blocks = getattr(info, "st_blocks", None)
    if blocks is None or blocks < 0:
        return info.st_size
    return blocks * 512


def _expected_dir_total(path: Path) -> int:
    """Sum of ``_allocated_bytes`` over the plain files directly walked."""
    return sum(
        _allocated_bytes(entry)
        for entry in sorted(path.rglob("*"))
        if entry.is_file() and not entry.is_symlink()
    )


def _run_pass(tmp_path, **kwargs):
    log = tmp_path / "housekeeping.log"
    brief = tmp_path / "housekeeping-brief.json"
    (
        rows,
        pytest_total,
        pytest_capped,
        log_total,
        log_capped,
        notes,
        logs_swept,
        _temps_swept,
        _workspaces_swept,
    ) = housekeeping.run_pass(
        log_path=log, brief_path=brief, now=NOW, **kwargs
    )
    return (
        rows,
        pytest_total,
        pytest_capped,
        log_total,
        log_capped,
        notes,
        logs_swept,
        log,
        brief,
    )


def _by_name(rows, number):
    wanted = "pytest-{}".format(number)
    return next(row for row in rows if row["path"].endswith(wanted))


def _make_log(root, name, age_days, size_bytes, sparse=False):
    """Build a log fixture; the file mtime is set last.

    Non-sparse files carry a head marker and a tail marker so a trim can
    prove it kept the newest bytes. Sparse files (seek plus one byte)
    give a large st_size without the disk write, for cap tests.
    """
    path = root / name
    if sparse:
        with path.open("wb") as handle:
            handle.seek(size_bytes - 1)
            handle.write(b"\0")
    else:
        head = b"HEAD-" + name.encode() + b"\n"
        tail = b"TAIL-" + name.encode() + b"\n"
        with path.open("wb") as handle:
            handle.write(head)
            remaining = size_bytes - len(head) - len(tail)
            chunk = b"x" * (1024 * 1024)
            while remaining > 0:
                piece = chunk[: min(len(chunk), remaining)]
                handle.write(piece)
                remaining -= len(piece)
            handle.write(tail)
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _by_basename(rows, name):
    return next(
        row for row in rows if row["path"].endswith("/" + name)
    )


def _empty_pytest_root(tmp_path):
    root = tmp_path / "pytest-of-empty"
    root.mkdir(parents=True, exist_ok=True)
    return root


HOUR = 60 * 60


def _make_temp(root, name, age_hours, with_file=False):
    """Build a temp-dir fixture; the dir mtime is set last."""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    if with_file:
        (path / "leftover.txt").write_text("live", encoding="utf-8")
    stamp = NOW - age_hours * HOUR
    os.utime(path, (stamp, stamp))
    return path


def _empty_log_root(tmp_path):
    root = tmp_path / "empty-logs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _temp_rows(rows):
    return [row for row in rows if row.get("kind") == "temp"]


def _empty_temp_root(tmp_path):
    root = tmp_path / "empty-temp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _empty_workspace_root(tmp_path):
    root = tmp_path / "empty-workspaces"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_workspace(root, name, files=()):
    """Build a leftover ``run.*`` fixture with nested content."""
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    for relative, content in files:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return path


def _workspace_rows(rows):
    return [row for row in rows if row.get("kind") == "workspace"]


def _scoped_roots(tmp_path, workspace_root):
    """Explicit roots for every sweep, so workspace tests stay hermetic."""
    return {
        "explicit_roots": [str(_empty_pytest_root(tmp_path))],
        "explicit_log_roots": [str(_empty_log_root(tmp_path))],
        "explicit_temp_roots": [str(_empty_temp_root(tmp_path))],
        "explicit_workspace_roots": [str(workspace_root)],
    }


def test_lock_pid_reading_treats_missing_and_bad_locks_as_uncertain(tmp_path):
    missing = tmp_path / "pytest-1"
    missing.mkdir()
    assert housekeeping.read_lock_pid(missing) is None

    for text in ("", "   \n", "abc", "12x", "0", "-5", "0.0"):
        path = tmp_path / "pytest-{}".format(abs(hash(text)) % 100000 + 2)
        path.mkdir()
        (path / ".lock").write_text(text, encoding="utf-8")
        assert housekeeping.read_lock_pid(path) is None, text

    live = tmp_path / "pytest-9"
    live.mkdir()
    (live / ".lock").write_text("  {}  \n".format(os.getpid()), encoding="utf-8")
    assert housekeeping.read_lock_pid(live) == os.getpid()


def test_pid_liveness_dead_is_false_alive_and_uncertain_are_true():
    assert housekeeping.pid_is_alive(_dead_pid()) is False
    assert housekeeping.pid_is_alive(os.getpid()) is True
    # PID 1 exists on every Unix; whether kill answers success or EPERM,
    # it must read as alive.
    assert housekeeping.pid_is_alive(1) is True


def test_dead_old_dirs_beyond_newest_three_are_deleted_oldest_first(
    tmp_path, monkeypatch
):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Five fully written 256 KiB dirs over a patched 3.5-payload cap: the
    # newest three stay, the oldest two go. Payloads are real bytes because
    # sparse files count at their (near-zero) allocated size.
    payload = 256 * 1024
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 7 * payload // 2)
    _make_numbered(root, 1, pid_text=dead, age_days=30, payload_bytes=payload,
                   sparse=False)
    _make_numbered(root, 2, pid_text=dead, age_days=20, payload_bytes=payload,
                   sparse=False)
    for number in (3, 4, 5):
        _make_numbered(root, number, pid_text=dead, age_days=20,
                       payload_bytes=payload, sparse=False)

    (
        rows,
        pytest_total,
        pytest_capped,
        _log_total,
        _log_capped,
        _notes,
        logs_swept,
        _log,
        brief,
    ) = _run_pass(tmp_path, explicit_roots=[str(root)])
    assert logs_swept is False
    assert pytest_total == sum(
        _expected_dir_total(root / "pytest-{}".format(n)) for n in (1, 2, 3, 4, 5)
    )
    assert pytest_capped is False
    assert _by_name(rows, 1)["action"] == "would-delete"
    assert _by_name(rows, 2)["action"] == "would-delete"
    for number in (3, 4, 5):
        row = _by_name(rows, number)
        assert row["action"] == "keep"
        assert "newest 3" in row["reason"]
    assert json.loads(brief.read_text(encoding="utf-8")) == []

    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_roots=[str(root)], apply=True
    )
    assert not (root / "pytest-1").exists()
    assert not (root / "pytest-2").exists()
    for number in (3, 4, 5):
        assert (root / "pytest-{}".format(number)).exists()
    assert _by_name(rows, 1)["action"] == "delete"
    assert _by_name(rows, 2)["action"] == "delete"


def test_oldest_first_uses_mtime_not_number(tmp_path, monkeypatch):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Only one deletion is needed to reach the cap; the older mtime wins
    # even though its number is higher. Payloads are real bytes because
    # sparse files count at their (near-zero) allocated size.
    payload = 256 * 1024
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 3 * payload // 2)
    _make_numbered(root, 1, pid_text=dead, age_days=20,
                   payload_bytes=payload, sparse=False)
    _make_numbered(root, 2, pid_text=dead, age_days=30,
                   payload_bytes=payload, sparse=False)
    for number in (3, 4, 5):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    rows, _, pytest_capped, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_roots=[str(root)]
    )
    assert pytest_capped is False
    assert _by_name(rows, 2)["action"] == "would-delete"
    assert _by_name(rows, 1)["action"] == "keep"


def test_alive_lock_and_recent_failure_are_kept_in_both_modes(
    tmp_path, monkeypatch
):
    """Review focus: the sweep never deletes data a live run still needs."""
    # Protected bytes must clear the cap on disk, not just apparently:
    # sparse payloads count at allocated size, so write real bytes.
    payload = 256 * 1024
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 64 * 1024)
    for mode in ("dry", "apply"):
        root = tmp_path / mode / "pytest-of-test"
        root.mkdir(parents=True)
        dead = str(_dead_pid())
        alive = _make_numbered(
            root, 1, pid_text=str(os.getpid()), age_days=30,
            payload_bytes=payload, sparse=False,
        )
        recent = _make_numbered(
            root, 2, pid_text=dead, age_days=1, payload_bytes=payload,
            sparse=False,
        )
        for number in (3, 4, 5):
            _make_numbered(root, number, pid_text=dead, age_days=20)

        rows, _, pytest_capped, _, _, _, _, log, brief = _run_pass(
            tmp_path / mode,
            explicit_roots=[str(root)],
            apply=(mode == "apply"),
        )
        assert alive.exists()
        assert recent.exists()
        assert _by_name(rows, 1)["action"] == "keep"
        assert "alive" in _by_name(rows, 1)["reason"]
        assert _by_name(rows, 2)["action"] == "keep"
        assert "under 14 days" in _by_name(rows, 2)["reason"]
        # Protected debris over the patched cap: nothing eligible,
        # so capped with alerts on both channels.
        assert pytest_capped is True
        assert "action=skip-alert" in log.read_text(encoding="utf-8")
        warnings = json.loads(brief.read_text(encoding="utf-8"))
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["kind"] == "pytest-cap"
        assert warning["path"] == os.path.realpath(root)
        assert warning["cap_bytes"] == housekeeping.CAP_BYTES
        assert warning["bytes"] > housekeeping.CAP_BYTES
        assert "recorded_at" in warning and "retry" in warning


def test_missing_and_unparseable_locks_are_never_deleted(
    tmp_path, monkeypatch
):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Uncertain locks keep real on-disk bytes over the patched cap.
    payload = 256 * 1024
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 64 * 1024)
    missing = _make_numbered(root, 1, age_days=30, payload_bytes=payload,
                             sparse=False)
    garbled = _make_numbered(
        root, 2, pid_text="not-a-pid", age_days=30,
        payload_bytes=payload, sparse=False,
    )
    for number in (3, 4, 5):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    rows, _, pytest_capped, _, _, _, _, log, brief = _run_pass(
        tmp_path, explicit_roots=[str(root)], apply=True
    )
    assert missing.exists()
    assert garbled.exists()
    assert _by_name(rows, 1)["action"] == "keep"
    assert "uncertain" in _by_name(rows, 1)["reason"]
    assert _by_name(rows, 2)["action"] == "keep"
    assert pytest_capped is True
    assert "action=skip-alert" in log.read_text(encoding="utf-8")
    assert len(json.loads(brief.read_text(encoding="utf-8"))) == 1


def test_symlinks_garbage_and_non_dirs_are_untouched(tmp_path, monkeypatch):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Real (not sparse) payload bytes so the big dir counts over the cap.
    payload = 256 * 1024
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 64 * 1024)
    target = _make_numbered(root, 1, pid_text=dead, age_days=30,
                            payload_bytes=payload, sparse=False)
    current = root / "pytest-current"
    current.symlink_to(target)
    garbage = root / "garbage-1234"
    garbage.mkdir()
    (garbage / "payload.bin").write_bytes(b"x" * 1024)
    named = root / "pytest-foo"
    named.mkdir()
    (root / "pytest-99").write_text("a file, not a dir", encoding="utf-8")
    for number in (2, 3, 4):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    # Expected on-disk total before --apply deletes the big dir.
    expected_total = sum(
        _expected_dir_total(root / "pytest-{}".format(n))
        for n in (1, 2, 3, 4)
    )
    rows, pytest_total, _, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_roots=[str(root)], apply=True
    )
    # Only the four numeric dirs are inventoried; the big one is gone.
    assert sorted(row["path"] for row in rows) == [
        str(root / "pytest-{}".format(n)) for n in (1, 2, 3, 4)
    ]
    assert pytest_total == expected_total
    assert not target.exists()
    assert current.is_symlink()
    assert (garbage / "payload.bin").read_bytes() == b"x" * 1024
    assert named.is_dir()
    assert (root / "pytest-99").is_file()


def test_clean_pass_clears_a_stale_brief_warning(tmp_path):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    _make_numbered(root, 1, pid_text=dead, age_days=20)
    brief = tmp_path / "housekeeping-brief.json"
    brief.write_text(
        json.dumps([{"kind": "pytest-cap", "stale": True}]), encoding="utf-8"
    )

    _, _, pytest_capped, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_roots=[str(root)]
    )
    assert pytest_capped is False
    assert json.loads(brief.read_text(encoding="utf-8")) == []


def test_cli_table_has_path_size_action_reason_columns(
    tmp_path, capsys, monkeypatch
):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Real (not sparse) payload bytes so the big dir counts over the cap.
    payload = 256 * 1024
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 64 * 1024)
    _make_numbered(root, 1, pid_text=dead, age_days=30,
                   payload_bytes=payload, sparse=False)
    for number in (2, 3, 4):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    code = housekeeping.main([
        "--root", str(root),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (dry-run)"
    assert out[1].startswith("# pytest total ")
    assert out[2] == "path\tsize_bytes\taction\treason"
    body = dict(
        (line.split("\t")[0], line.split("\t")[1:]) for line in out[3:-1]
    )
    size, action, reason = body[str(root / "pytest-1")]
    assert int(size) >= payload
    assert action == "would-delete"
    assert reason != ""


def test_cli_apply_banner_and_delete_action(tmp_path, capsys, monkeypatch):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Real (not sparse) payload bytes so the big dir counts over the cap.
    monkeypatch.setattr(housekeeping, "CAP_BYTES", 64 * 1024)
    gone = _make_numbered(root, 1, pid_text=dead, age_days=30,
                          payload_bytes=256 * 1024, sparse=False)
    for number in (2, 3, 4):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    code = housekeeping.main([
        "--apply",
        "--root", str(root),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    assert not gone.exists()
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (apply)"
    assert "\tdelete\t" in "\n".join(out)


def test_cli_refuses_a_non_internal_disk_root(tmp_path, capsys):
    code = housekeeping.main([
        "--root", "/Volumes/External SSD/pytest-of-test",
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 2
    assert "refusing non-internal-disk root" in capsys.readouterr().err


def test_internal_disk_guard_and_root_ownership(tmp_path, monkeypatch):
    assert housekeeping.is_internal_disk(Path("/Volumes/SSD/x")) is False
    assert housekeeping.is_internal_disk(Path("/Volumes")) is False
    assert housekeeping.is_internal_disk(tmp_path) is True
    assert housekeeping.is_internal_disk(Path("/private/tmp")) is True
    assert housekeeping.root_owned_by_me(tmp_path) is True
    monkeypatch.setattr(os, "geteuid", lambda: 59999)
    assert housekeeping.root_owned_by_me(tmp_path) is False


def test_brief_warnings_reader_fails_open(tmp_path):
    assert housekeeping.brief_warnings(tmp_path / "absent.json") == []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert housekeeping.brief_warnings(corrupt) == []
    shape = tmp_path / "shape.json"
    shape.write_text('{"kind": "pytest-cap"}', encoding="utf-8")
    assert housekeeping.brief_warnings(shape) == []
    rows = [{"kind": "pytest-cap", "path": "/tmp/x"}]
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(rows), encoding="utf-8")
    assert housekeeping.brief_warnings(valid) == rows


def test_pytest_roots_follow_tmpdir_and_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    roots = housekeeping.pytest_roots(user="testuser")
    assert roots[0] == tmp_path.resolve() / "pytest-of-testuser"
    assert all(root.name == "pytest-of-testuser" for root in roots)

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("TMPDIR")
    roots = housekeeping.pytest_roots(user="testuser")
    assert len(roots) == 1

    # The TMPDIR-unset probe restores its caller's environment.
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    tempfile.gettempdir()  # warm the cache as any earlier caller would
    before_env = dict(os.environ)
    before_cache = tempfile.tempdir
    housekeeping.pytest_roots(user="testuser")
    assert dict(os.environ) == before_env
    assert tempfile.tempdir == before_cache


def _make_brief_readers_safe(monkeypatch, funnel):
    for name, value in {
        "parked_json": [],
        "closed_itself_json": [],
        "cleared_blocks_json": [],
        "unattended_merges": [],
        "unattended_approvals": [],
        "agent_run_summary": [],
        "agent_health": [],
        "working_tree_touched": [],
        "rejected_merges": {},
    }.items():
        monkeypatch.setattr(
            funnel, name, lambda *args, _value=value, **kwargs: _value
        )


def test_brief_surfaces_housekeeping_warnings(monkeypatch, capsys, tmp_path):
    import funnel

    record = tmp_path / "housekeeping-brief.json"
    record.write_text(json.dumps([{
        "kind": "pytest-cap",
        "recorded_at": "2026-10-10T12:00:00Z",
        "path": "/private/tmp/pytest-of-nateprich",
        "reason": "pytest debris keeps 4 GiB over the 3 GiB cap",
        "bytes": 4 * GIB,
        "cap_bytes": 3 * GIB,
        "retry": "wait for runs to finish",
    }]), encoding="utf-8")
    monkeypatch.setattr(
        funnel.housekeeping, "BRIEF_WARNINGS_PATH", record
    )
    _make_brief_readers_safe(monkeypatch, funnel)
    monkeypatch.setattr(funnel, "connector_gate_answers",
                        lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "api_cost", lambda: {})
    monkeypatch.setattr(funnel, "graphql_caller_spend", lambda: {})

    assert funnel.cmd_brief(
        [], datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    ) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["housekeeping_warnings"] == [{
        "kind": "pytest-cap",
        "recorded_at": "2026-10-10T12:00:00Z",
        "path": "/private/tmp/pytest-of-nateprich",
        "reason": "pytest debris keeps 4 GiB over the 3 GiB cap",
        "bytes": 4 * GIB,
        "cap_bytes": 3 * GIB,
        "retry": "wait for runs to finish",
    }]


def test_brief_has_no_housekeeping_warnings_without_an_artifact(
    monkeypatch, capsys, tmp_path
):
    import funnel

    monkeypatch.setattr(
        funnel.housekeeping, "BRIEF_WARNINGS_PATH",
        tmp_path / "absent.json",
    )
    _make_brief_readers_safe(monkeypatch, funnel)
    monkeypatch.setattr(funnel, "connector_gate_answers",
                        lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "api_cost", lambda: {})
    monkeypatch.setattr(funnel, "graphql_caller_spend", lambda: {})

    assert funnel.cmd_brief(
        [], datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    ) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["housekeeping_warnings"] == []


def test_held_open_log_is_never_trimmed(tmp_path):
    """First verification: the real holder check blocks trim on an open file.

    The fixture stays open in this process through plan and apply, so the
    real ``lsof`` (or ``/proc`` where ``lsof`` is missing) must report it
    held and the sweep must keep it in both modes without touching a byte.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    target = _make_log(
        logs, "command-center-held.log", 30, 30 * MIB
    )
    before = target.read_bytes()

    for apply in (False, True):
        with target.open("rb"):
            assert housekeeping.log_is_held(target) is True
            rows, _, _, log_total, log_capped, _, _, _, _ = _run_pass(
                tmp_path,
                explicit_roots=[str(_empty_pytest_root(tmp_path))],
                explicit_log_roots=[str(logs)],
                apply=apply,
            )
        row = _by_basename(rows, "command-center-held.log")
        assert row["action"] == "keep"
        assert "held open" in row["reason"]
        assert log_total == 30 * MIB
        assert log_capped is False
        assert target.read_bytes() == before

    # Closed, the same file reports unheld and becomes trim-eligible.
    assert housekeeping.log_is_held(target) is False


def test_old_unheld_log_trims_to_newest_25mib(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    target = _make_log(
        logs, "command-center-old.log", 30, 30 * MIB
    )

    rows, _, _, log_total, log_capped, _, _, _, _ = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(logs)],
    )
    assert log_total == 30 * MIB
    assert log_capped is False
    row = _by_basename(rows, "command-center-old.log")
    assert row["action"] == "would-trim"
    assert target.stat().st_size == 30 * MIB

    rows, _, _, _, _, _, _, log_path, _ = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(logs)],
        apply=True,
    )
    assert _by_basename(rows, "command-center-old.log")["action"] == "trim"
    assert target.stat().st_size == 25 * MIB
    content = target.read_bytes()
    assert content.startswith(b"x" * 1024)
    assert content.endswith(b"TAIL-command-center-old.log\n")
    assert b"HEAD-command-center-old.log" not in content
    assert "action=trim" in log_path.read_text(encoding="utf-8")


def test_recent_and_small_logs_are_kept_in_both_modes(tmp_path):
    """Recent failure evidence is never trimmed, even over 25 MiB."""
    for mode in ("dry", "apply"):
        logs = tmp_path / mode / "logs"
        logs.mkdir(parents=True)
        recent = _make_log(
            logs, "command-center-recent.log", 1, 30 * MIB
        )
        small = _make_log(
            logs, "command-center-small.log", 30, 1024
        )

        rows, _, _, log_total, log_capped, _, _, _, _ = _run_pass(
            tmp_path / mode,
            explicit_roots=[
                str(_empty_pytest_root(tmp_path / mode))
            ],
            explicit_log_roots=[str(logs)],
            apply=(mode == "apply"),
        )
        assert recent.stat().st_size == 30 * MIB
        assert small.stat().st_size == 1024
        assert _by_basename(rows, "command-center-recent.log")["action"] == (
            "keep"
        )
        assert "under 14 days" in _by_basename(
            rows, "command-center-recent.log"
        )["reason"]
        assert _by_basename(rows, "command-center-small.log")["action"] == (
            "keep"
        )
        assert log_total == 30 * MIB + 1024
        assert log_capped is False


def test_log_total_cap_skips_and_alerts_when_only_protected_remain(
    tmp_path,
):
    logs = tmp_path / "logs"
    logs.mkdir()
    # 240 MiB of protected bytes: one recent, one held open. Nothing is
    # eligible, so the 200 MiB total cannot be met.
    recent = _make_log(
        logs, "command-center-recent.log", 1, 120 * MIB, sparse=True
    )
    held = _make_log(
        logs, "command-center-held.log", 30, 120 * MIB, sparse=True
    )

    with held.open("rb"):
        (
            rows,
            _,
            _,
            log_total,
            log_capped,
            _,
            _,
            log_path,
            brief,
        ) = _run_pass(
            tmp_path,
            explicit_roots=[str(_empty_pytest_root(tmp_path))],
            explicit_log_roots=[str(logs)],
            apply=True,
        )
        assert recent.exists()
        assert held.exists()
        assert _by_basename(rows, "command-center-recent.log")["action"] == (
            "keep"
        )
        assert _by_basename(rows, "command-center-held.log")["action"] == (
            "keep"
        )
        assert log_total == 240 * MIB
        assert log_capped is True
        assert "action=skip-alert" in log_path.read_text(encoding="utf-8")
        warnings = json.loads(brief.read_text(encoding="utf-8"))
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["kind"] == "log-cap"
        assert warning["path"] == os.path.realpath(logs)
        assert warning["cap_bytes"] == housekeeping.LOG_TOTAL_CAP_BYTES
        assert warning["bytes"] == 240 * MIB
        assert "recorded_at" in warning and "retry" in warning


def test_log_trim_meets_total_cap_without_alert(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    # 210 MiB total, all eligible; trims bring it well under the cap.
    _make_log(logs, "command-center-a.log", 30, 30 * MIB, sparse=True)
    _make_log(logs, "command-center-b.log", 30, 30 * MIB, sparse=True)
    _make_log(logs, "command-center-c.log", 30, 150 * MIB, sparse=True)

    rows, _, _, log_total, log_capped, _, _, _, brief = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(logs)],
        apply=True,
    )
    assert log_total == 210 * MIB
    # Each 30 MiB file trims to 25 MiB (5 MiB reclaimed each); the 150 MiB
    # file trims to 25 MiB (125 MiB reclaimed). Remaining 75 MiB.
    assert log_capped is False
    assert _by_basename(rows, "command-center-a.log")["action"] == "trim"
    assert _by_basename(rows, "command-center-b.log")["action"] == "trim"
    assert _by_basename(rows, "command-center-c.log")["action"] == "trim"
    assert json.loads(brief.read_text(encoding="utf-8")) == []


def test_log_files_skip_symlinks_dirs_and_nonmatching(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    target = _make_log(logs, "command-center-real.log", 30, 1024)
    (logs / "command-center-dir.log").mkdir()
    (logs / "other.log").write_text("not ours", encoding="utf-8")
    (logs / "command-center-link.log").symlink_to(target)

    rows, _, _, log_total, _, _, _, _, _ = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(logs)],
    )
    assert [row["path"] for row in rows] == [str(target)]
    assert log_total == 1024


def test_log_holder_probe_fails_closed_on_uncertainty(
    tmp_path, monkeypatch
):
    target = tmp_path / "command-center-x.log"
    target.write_text("x", encoding="utf-8")
    # A missing lsof binary and no /proc scan result must read as held.
    monkeypatch.setattr(
        housekeeping, "_proc_reports_open", lambda path: None
    )
    assert housekeeping.log_is_held(target, lsof="/nonexistent-lsof") is True
    # A probe that errors (stderr, bad exit) also reads as held.
    monkeypatch.setattr(
        housekeeping, "_lsof_reports_open", lambda path, lsof: None
    )
    assert housekeeping.log_is_held(target, lsof="/bin/true") is True


def test_cli_log_table_and_log_root_guard(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    _make_log(logs, "command-center-old.log", 30, 30 * MIB, sparse=True)
    empty = _empty_pytest_root(tmp_path)

    code = housekeeping.main([
        "--root", str(empty),
        "--log-root", str(logs),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (dry-run)"
    assert out[1].startswith("# pytest total ")
    assert out[2].startswith("# log total ")
    assert "per-file 25.0 MiB" in out[2] and "total cap 200.0 MiB" in out[2]
    header = out.index("path\tsize_bytes\taction\treason")
    body = "\n".join(out[header + 1:])
    assert "command-center-old.log" in body
    assert "\twould-trim\t" in body

    code = housekeeping.main([
        "--log-root", "/Volumes/External SSD/logs",
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 2
    assert "refusing non-internal-disk log root" in capsys.readouterr().err


def test_empty_old_temp_dirs_are_removed_only_in_apply(tmp_path):
    temp = tmp_path / "tmp"
    temp.mkdir()
    target = _make_temp(temp, "muse-old-empty", 30)

    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(_empty_log_root(tmp_path))],
        explicit_temp_roots=[str(temp)],
    )
    row = _by_basename(rows, "muse-old-empty")
    assert row["action"] == "would-delete"
    assert row["size_bytes"] == 0
    assert target.exists()

    rows, _, _, _, _, _, _, log_path, brief = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(_empty_log_root(tmp_path))],
        explicit_temp_roots=[str(temp)],
        apply=True,
    )
    assert _by_basename(rows, "muse-old-empty")["action"] == "delete"
    assert not target.exists()
    assert "action=delete" in log_path.read_text(encoding="utf-8")
    # No cap on this sweep, so no warning artifact is ever written.
    assert json.loads(brief.read_text(encoding="utf-8")) == []


def test_temp_nonempty_recent_and_unowned_are_kept_in_both_modes(
    tmp_path, monkeypatch
):
    """Only empty old owned muse-* dirs go; the rest are kept, never listed
    for removal, in both dry-run and --apply."""
    for mode in ("dry", "apply"):
        temp = tmp_path / mode / "tmp"
        temp.mkdir(parents=True)
        busy = _make_temp(temp, "muse-busy", 30, with_file=True)
        fresh = _make_temp(temp, "muse-fresh", 1)

        rows, _, _, _, _, _, _, _, _ = _run_pass(
            tmp_path / mode,
            explicit_roots=[
                str(_empty_pytest_root(tmp_path / mode))
            ],
            explicit_log_roots=[str(_empty_log_root(tmp_path / mode))],
            explicit_temp_roots=[str(temp)],
            apply=(mode == "apply"),
        )
        assert busy.exists()
        assert fresh.exists()
        assert _by_basename(rows, "muse-busy")["action"] == "keep"
        assert "not empty" in _by_basename(rows, "muse-busy")["reason"]
        assert _by_basename(rows, "muse-fresh")["action"] == "keep"
        assert "24 hours" in _by_basename(rows, "muse-fresh")["reason"]

    # Nothing owned by another user is ever eligible.
    temp = tmp_path / "owned" / "tmp"
    temp.mkdir(parents=True)
    foreign = _make_temp(temp, "muse-foreign", 30)
    monkeypatch.setattr(os, "geteuid", lambda: 59999)
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path / "owned",
        explicit_roots=[str(_empty_pytest_root(tmp_path / "owned"))],
        explicit_log_roots=[str(_empty_log_root(tmp_path / "owned"))],
        explicit_temp_roots=[str(temp)],
        apply=True,
    )
    assert foreign.exists()
    assert _by_basename(rows, "muse-foreign")["action"] == "keep"
    assert "not owned" in _by_basename(rows, "muse-foreign")["reason"]


def test_temp_symlinks_files_and_nonmatching_are_untouched(tmp_path):
    for mode in ("dry", "apply"):
        temp = tmp_path / mode / "tmp"
        temp.mkdir(parents=True)
        target = _make_temp(temp, "muse-real", 30)
        (temp / "muse-file").write_text(
            "a file, not a dir", encoding="utf-8"
        )
        (temp / "muse-link").symlink_to(target)
        other = temp / "other-old"
        other.mkdir()
        os.utime(other, (NOW - 30 * HOUR, NOW - 30 * HOUR))

        rows, _, _, _, _, _, _, _, _ = _run_pass(
            tmp_path / mode,
            explicit_roots=[
                str(_empty_pytest_root(tmp_path / mode))
            ],
            explicit_log_roots=[str(_empty_log_root(tmp_path / mode))],
            explicit_temp_roots=[str(temp)],
            apply=(mode == "apply"),
        )
        # Only the matching real dir is inventoried; it was empty and old.
        assert [row["path"] for row in _temp_rows(rows)] == [str(target)]
        if mode == "dry":
            assert _by_basename(rows, "muse-real")["action"] == "would-delete"
            assert target.exists()
        else:
            assert _by_basename(rows, "muse-real")["action"] == "delete"
            assert not target.exists()
        assert (temp / "muse-file").is_file()
        assert (temp / "muse-link").is_symlink()
        assert other.is_dir()


def test_temp_roots_follow_tmpdir_and_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    roots = housekeeping.temp_roots()
    assert roots[0] == tmp_path.resolve()
    assert len(roots) == len({str(root) for root in roots})

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("TMPDIR")
    assert len(housekeeping.temp_roots()) == 1

    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv(
        housekeeping.TEMP_DIR_ENV, str(tmp_path / "override")
    )
    assert housekeeping.temp_roots() == [tmp_path / "override"]


def test_temp_scoped_runs_sweep_only_what_they_ask(tmp_path):
    temp = tmp_path / "tmp"
    temp.mkdir()
    target = _make_temp(temp, "muse-old-empty", 30)

    # A pytest-scoped run sweeps no temp dirs.
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_roots=[str(_empty_pytest_root(tmp_path))]
    )
    assert _temp_rows(rows) == []
    assert target.exists()

    # A log-scoped run sweeps no temp dirs either.
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_log_roots=[str(_empty_log_root(tmp_path))]
    )
    assert _temp_rows(rows) == []
    assert target.exists()


def test_cli_temp_table_and_temp_root_guard(tmp_path, capsys):
    temp = tmp_path / "tmp"
    temp.mkdir()
    _make_temp(temp, "muse-old-empty", 30)
    empty = _empty_pytest_root(tmp_path)
    empty_logs = _empty_log_root(tmp_path)

    code = housekeeping.main([
        "--root", str(empty),
        "--log-root", str(empty_logs),
        "--temp-root", str(temp),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (dry-run)"
    assert out[1].startswith("# pytest total ")
    assert out[2].startswith("# log total ")
    assert out[3].startswith("# temp ")
    assert "muse-* dirs" in out[3]
    header = out.index("path\tsize_bytes\taction\treason")
    body = "\n".join(out[header + 1:])
    assert "muse-old-empty" in body
    assert "\twould-delete\t" in body

    # A --root-only run prints no temp line.
    code = housekeeping.main([
        "--root", str(empty),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (dry-run)"
    assert out[1].startswith("# pytest total ")
    assert out[2] == "path\tsize_bytes\taction\treason"

    code = housekeeping.main([
        "--temp-root", "/Volumes/External SSD/tmp",
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 2
    assert "refusing non-internal-disk temp root" in capsys.readouterr().err


def test_leftover_workspaces_are_flagged_in_both_modes(tmp_path):
    """Every leftover run.* dir gets a flag row; nothing is ever removed."""
    for mode in ("dry", "apply"):
        base = tmp_path / mode
        root = base / "workspaces"
        root.mkdir(parents=True)
        first = _make_workspace(
            root, "run.abc123", [("answer.json", b'{"done":true}')]
        )
        second = _make_workspace(
            root,
            "run.def456",
            [("nested/deep.bin", b"x" * 4096), ("notes.txt", b"wip")],
        )

        rows, _, _, _, _, _, _, _, _ = _run_pass(
            base,
            apply=(mode == "apply"),
            **_scoped_roots(base, root),
        )
        flagged = {
            row["path"]: row for row in _workspace_rows(rows)
        }
        assert sorted(flagged) == [str(first), str(second)]
        for path, row in flagged.items():
            assert row["action"] == "flag"
            assert "never deleted" in row["reason"]
            assert row["size_bytes"] > 0
        assert flagged[str(first)]["size_bytes"] == _expected_dir_total(
            first
        )
        assert flagged[str(second)]["size_bytes"] == _expected_dir_total(
            second
        )
        # Flagging touches nothing, in either mode.
        assert (first / "answer.json").read_bytes() == b'{"done":true}'
        assert (second / "nested" / "deep.bin").read_bytes() == b"x" * 4096
        assert (second / "notes.txt").read_bytes() == b"wip"


def test_apply_never_unlinks_a_workspace(tmp_path):
    """--apply leaves the whole workspace tree byte-identical."""
    root = tmp_path / "workspaces"
    root.mkdir()
    target = _make_workspace(
        root,
        "run.XiM9MQ",
        [
            ("answer.json", b'{"blocked_on_human":{}}'),
            (".git/HEAD", b"ref: refs/heads/ticket/1\n"),
            ("src/main.py", b"print('wip')\n"),
        ],
    )
    before = {
        path.relative_to(target): path.read_bytes()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }

    rows, _, _, _, _, _, _, log_path, brief = _run_pass(
        tmp_path, apply=True, **_scoped_roots(tmp_path, root)
    )
    assert _by_basename(rows, "run.XiM9MQ")["action"] == "flag"
    assert target.is_dir()
    after = {
        path.relative_to(target): path.read_bytes()
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }
    assert after == before
    # Flag-only inventory writes no log records and no warning.
    assert not log_path.exists()
    assert json.loads(brief.read_text(encoding="utf-8")) == []


def test_workspace_symlinks_files_and_nonmatching_are_unlisted(tmp_path):
    for mode in ("dry", "apply"):
        base = tmp_path / mode
        root = base / "workspaces"
        root.mkdir(parents=True)
        target = _make_workspace(root, "run.real", [("kept.txt", b"kept")])
        (root / "run-file").write_text(
            "a file, not a dir", encoding="utf-8"
        )
        (root / "run-link").symlink_to(target)
        other = root / "other-old"
        other.mkdir()
        (other / "payload.txt").write_text("not a workspace", encoding="utf-8")

        rows, _, _, _, _, _, _, _, _ = _run_pass(
            base,
            apply=(mode == "apply"),
            **_scoped_roots(base, root),
        )
        # Only the matching real dir is inventoried, flagged in both modes.
        assert [row["path"] for row in _workspace_rows(rows)] == [
            str(target)
        ]
        assert _by_basename(rows, "run.real")["action"] == "flag"
        assert target.is_dir()
        assert (root / "run-file").is_file()
        assert (root / "run-link").is_symlink()
        assert other.is_dir()


def test_workspace_roots_default_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv(housekeeping.WORKSPACE_DIR_ENV, raising=False)
    assert housekeeping.workspace_roots() == [
        Path(os.path.expanduser(housekeeping.WORKSPACE_DEFAULT))
    ]
    assert housekeeping.WORKSPACE_DEFAULT.endswith(
        "command-center-muse-implement-workspaces"
    )

    monkeypatch.setenv(
        housekeeping.WORKSPACE_DIR_ENV, str(tmp_path / "override")
    )
    assert housekeeping.workspace_roots() == [tmp_path / "override"]


def test_workspace_scoped_runs_sweep_only_what_they_ask(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    target = _make_workspace(root, "run.leftover", [("a.txt", b"a")])

    # A pytest-scoped run lists no workspaces.
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_roots=[str(_empty_pytest_root(tmp_path))]
    )
    assert _workspace_rows(rows) == []
    assert target.exists()

    # A log-scoped run lists no workspaces either.
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path, explicit_log_roots=[str(_empty_log_root(tmp_path))]
    )
    assert _workspace_rows(rows) == []
    assert target.exists()

    # A temp-scoped run lists no workspaces either.
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_log_roots=[str(_empty_log_root(tmp_path))],
        explicit_temp_roots=[str(_empty_temp_root(tmp_path))],
    )
    assert _workspace_rows(rows) == []
    assert target.exists()

    # A workspace-scoped run lists workspaces and no log or temp rows.
    rows, _, _, _, _, _, _, _, _ = _run_pass(
        tmp_path,
        explicit_roots=[str(_empty_pytest_root(tmp_path))],
        explicit_workspace_roots=[str(root)],
    )
    assert [row["path"] for row in _workspace_rows(rows)] == [str(target)]
    assert _by_basename(rows, "run.leftover")["action"] == "flag"
    assert [
        row for row in rows if row.get("kind") in ("log", "temp")
    ] == []


def test_cli_workspace_table_and_workspace_root_guard(tmp_path, capsys):
    root = tmp_path / "workspaces"
    root.mkdir()
    _make_workspace(root, "run.leftover", [("a.txt", b"a")])
    empty = _empty_pytest_root(tmp_path)
    empty_logs = _empty_log_root(tmp_path)
    empty_temp = _empty_temp_root(tmp_path)

    code = housekeeping.main([
        "--root", str(empty),
        "--log-root", str(empty_logs),
        "--temp-root", str(empty_temp),
        "--workspace-root", str(root),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (dry-run)"
    assert out[1].startswith("# pytest total ")
    assert out[2].startswith("# log total ")
    assert out[3].startswith("# temp ")
    assert out[4].startswith("# workspace ")
    assert "run.* dirs" in out[4]
    header = out.index("path\tsize_bytes\taction\treason")
    body = "\n".join(out[header + 1:])
    assert "run.leftover" in body
    assert "\tflag\t" in body

    # A --root-only run prints no workspace line.
    code = housekeeping.main([
        "--root", str(empty),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping sweep (dry-run)"
    assert out[1].startswith("# pytest total ")
    assert out[2] == "path\tsize_bytes\taction\treason"

    code = housekeeping.main([
        "--workspace-root", "/Volumes/External SSD/workspaces",
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 2
    assert "refusing non-internal-disk workspace root" in capsys.readouterr().err


def test_dir_size_bytes_counts_hard_linked_inode_once(tmp_path):
    target = tmp_path / "debris"
    target.mkdir()
    original = target / "payload.bin"
    original.write_bytes(b"x" * 65536)
    os.link(original, target / "payload.link-a")
    os.link(original, target / "payload.link-b")
    assert housekeeping.dir_size_bytes(target) == 65536


def test_dir_size_bytes_counts_sparse_file_at_allocated_size_once(tmp_path):
    """Sparse file plus hardlink: allocated bytes counted once.

    A sparse file's apparent ``st_size`` can exceed its on-disk bytes by
    orders of magnitude; the sweep must count the allocation once (the
    #2529 same-inode de-dup), not the apparent size twice.
    """
    target = tmp_path / "debris"
    target.mkdir()
    apparent = 64 * MIB
    sparse = target / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.seek(apparent - 1)
        handle.write(b"\0")
    os.link(sparse, target / "sparse.link")
    info = sparse.stat()
    assert info.st_size == apparent
    blocks = getattr(info, "st_blocks", None)
    if blocks is None or blocks < 0:
        pytest.skip("platform reports no block counts")
    if blocks * 512 >= apparent:
        pytest.skip("filesystem materialized the sparse fixture")
    allocated = _allocated_bytes(sparse)
    assert housekeeping.dir_size_bytes(target) == allocated
    # Within 5% of du -sk where du exists.
    du = shutil.which("du")
    if du is None:
        pytest.skip("no du binary for the parity check")
    proc = subprocess.run(
        [du, "-sk", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    parity = int(proc.stdout.split()[0]) * 1024
    sized = housekeeping.dir_size_bytes(target)
    assert abs(sized - parity) <= 0.05 * max(parity, 1)
