"""The pytest debris sweep deletes only old dead-PID dirs and keeps the rest.

Dead-PID numbered dirs older than 14 days and beyond the newest 3 are
deleted oldest-first down to the 3 GiB cap; alive locks, the newest 3,
recent failure evidence and uncertain locks are kept in both dry-run and
``--apply``. When only protected dirs remain above the cap the sweep
skips and alerts through the housekeeping log and the funnel-brief
warning artifact.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import housekeeping


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 24 * 60 * 60
GIB = 1024 * 1024 * 1024


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
) -> Path:
    """Build a ``pytest-N`` fixture dir; the dir mtime is set last."""
    path = root / "pytest-{}".format(number)
    path.mkdir(parents=True, exist_ok=True)
    if pid_text is not None:
        (path / ".lock").write_text(pid_text, encoding="utf-8")
    if payload_bytes:
        payload = path / "payload.bin"
        with payload.open("wb") as handle:
            handle.seek(payload_bytes - 1)
            handle.write(b"\0")
    stamp = NOW - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _run_pass(tmp_path, **kwargs):
    log = tmp_path / "housekeeping.log"
    brief = tmp_path / "housekeeping-brief.json"
    rows, total, capped, notes = housekeeping.run_pass(
        log_path=log, brief_path=brief, now=NOW, **kwargs
    )
    return rows, total, capped, notes, log, brief


def _by_name(rows, number):
    wanted = "pytest-{}".format(number)
    return next(row for row in rows if row["path"].endswith(wanted))


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


def test_dead_old_dirs_beyond_newest_three_are_deleted_oldest_first(tmp_path):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Five 900 MiB dirs: the newest three stay (2.7 GiB, under the cap),
    # the oldest two go.
    payload = 900 * 1024 * 1024
    _make_numbered(root, 1, pid_text=dead, age_days=30, payload_bytes=payload)
    _make_numbered(root, 2, pid_text=dead, age_days=20, payload_bytes=payload)
    for number in (3, 4, 5):
        _make_numbered(root, number, pid_text=dead, age_days=20,
                       payload_bytes=payload)

    rows, total, capped, _notes, _log, brief = _run_pass(
        tmp_path, explicit_roots=[str(root)]
    )
    assert total == 5 * payload + 5 * len(dead)
    assert capped is False
    assert _by_name(rows, 1)["action"] == "would-delete"
    assert _by_name(rows, 2)["action"] == "would-delete"
    for number in (3, 4, 5):
        row = _by_name(rows, number)
        assert row["action"] == "keep"
        assert "newest 3" in row["reason"]
    assert json.loads(brief.read_text(encoding="utf-8")) == []

    rows, _total, _capped, _notes, _log, _brief = _run_pass(
        tmp_path, explicit_roots=[str(root)], apply=True
    )
    assert not (root / "pytest-1").exists()
    assert not (root / "pytest-2").exists()
    for number in (3, 4, 5):
        assert (root / "pytest-{}".format(number)).exists()
    assert _by_name(rows, 1)["action"] == "delete"
    assert _by_name(rows, 2)["action"] == "delete"


def test_oldest_first_uses_mtime_not_number(tmp_path):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    # Only one deletion is needed to reach the cap; the older mtime wins
    # even though its number is higher.
    _make_numbered(root, 1, pid_text=dead, age_days=20, payload_bytes=2 * GIB)
    _make_numbered(root, 2, pid_text=dead, age_days=30, payload_bytes=2 * GIB)
    for number in (3, 4, 5):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    rows, _total, capped, _notes, _log, _brief = _run_pass(
        tmp_path, explicit_roots=[str(root)]
    )
    assert capped is False
    assert _by_name(rows, 2)["action"] == "would-delete"
    assert _by_name(rows, 1)["action"] == "keep"


def test_alive_lock_and_recent_failure_are_kept_in_both_modes(tmp_path):
    """Review focus: the sweep never deletes data a live run still needs."""
    for mode in ("dry", "apply"):
        root = tmp_path / mode / "pytest-of-test"
        root.mkdir(parents=True)
        dead = str(_dead_pid())
        alive = _make_numbered(
            root, 1, pid_text=str(os.getpid()), age_days=30,
            payload_bytes=2 * GIB,
        )
        recent = _make_numbered(
            root, 2, pid_text=dead, age_days=1, payload_bytes=2 * GIB
        )
        for number in (3, 4, 5):
            _make_numbered(root, number, pid_text=dead, age_days=20)

        rows, _total, capped, _notes, log, brief = _run_pass(
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
        # 4 GiB of protected debris: nothing eligible, so capped with
        # alerts on both channels.
        assert capped is True
        assert "action=skip-alert" in log.read_text(encoding="utf-8")
        warnings = json.loads(brief.read_text(encoding="utf-8"))
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning["kind"] == "pytest-cap"
        assert warning["path"] == os.path.realpath(root)
        assert warning["cap_bytes"] == housekeeping.CAP_BYTES
        assert warning["bytes"] > housekeeping.CAP_BYTES
        assert "recorded_at" in warning and "retry" in warning


def test_missing_and_unparseable_locks_are_never_deleted(tmp_path):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    missing = _make_numbered(root, 1, age_days=30, payload_bytes=2 * GIB)
    garbled = _make_numbered(
        root, 2, pid_text="not-a-pid", age_days=30, payload_bytes=2 * GIB
    )
    for number in (3, 4, 5):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    rows, _total, capped, _notes, log, brief = _run_pass(
        tmp_path, explicit_roots=[str(root)], apply=True
    )
    assert missing.exists()
    assert garbled.exists()
    assert _by_name(rows, 1)["action"] == "keep"
    assert "uncertain" in _by_name(rows, 1)["reason"]
    assert _by_name(rows, 2)["action"] == "keep"
    assert capped is True
    assert "action=skip-alert" in log.read_text(encoding="utf-8")
    assert len(json.loads(brief.read_text(encoding="utf-8"))) == 1


def test_symlinks_garbage_and_non_dirs_are_untouched(tmp_path):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    target = _make_numbered(root, 1, pid_text=dead, age_days=30,
                            payload_bytes=4 * GIB)
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

    rows, total, _capped, _notes, _log, _brief = _run_pass(
        tmp_path, explicit_roots=[str(root)], apply=True
    )
    # Only the four numeric dirs are inventoried; the 4 GiB one is gone.
    assert sorted(row["path"] for row in rows) == [
        str(root / "pytest-{}".format(n)) for n in (1, 2, 3, 4)
    ]
    assert total >= 4 * GIB
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

    _rows, _total, capped, _notes, _log, _brief = _run_pass(
        tmp_path, explicit_roots=[str(root)]
    )
    assert capped is False
    assert json.loads(brief.read_text(encoding="utf-8")) == []


def test_cli_table_has_path_size_action_reason_columns(tmp_path, capsys):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    _make_numbered(root, 1, pid_text=dead, age_days=30, payload_bytes=4 * GIB)
    for number in (2, 3, 4):
        _make_numbered(root, number, pid_text=dead, age_days=20)

    code = housekeeping.main([
        "--root", str(root),
        "--log", str(tmp_path / "housekeeping.log"),
        "--brief", str(tmp_path / "housekeeping-brief.json"),
    ])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# housekeeping pytest sweep (dry-run)"
    assert out[2] == "path\tsize_bytes\taction\treason"
    body = dict(
        (line.split("\t")[0], line.split("\t")[1:]) for line in out[3:-1]
    )
    size, action, reason = body[str(root / "pytest-1")]
    assert int(size) >= 4 * GIB
    assert action == "would-delete"
    assert reason != ""


def test_cli_apply_banner_and_delete_action(tmp_path, capsys):
    root = tmp_path / "pytest-of-test"
    root.mkdir()
    dead = str(_dead_pid())
    gone = _make_numbered(root, 1, pid_text=dead, age_days=30,
                          payload_bytes=4 * GIB)
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
    assert out[0] == "# housekeeping pytest sweep (apply)"
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
