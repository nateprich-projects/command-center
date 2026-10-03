"""Verified movement of compressed session logs to the SSD archive."""

from __future__ import annotations

import datetime
import gzip
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import session_log_archive
import session_logs


def roots(tmp_path: Path):
    muse = tmp_path / "internal" / "muse"
    codex = tmp_path / "internal" / "codex"
    volume = tmp_path / "ssd"
    volume.mkdir()
    archive = volume / "Agent-Logs"
    return muse, codex, volume, archive


def archive_fixture(muse: Path, relative: str, contents: bytes) -> Path:
    source = muse / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(contents)
    return source


def run_pass(muse: Path, codex: Path, volume: Path, archive: Path):
    return session_log_archive.archive_pass(
        muse_root=muse,
        codex_root=codex,
        archive_base=archive,
        volume_root=volume,
        mount_check=lambda _path: True,
    )


def test_default_archive_base_uses_the_machine_local_ssd(tmp_path, monkeypatch):
    muse = tmp_path / "internal" / "muse"
    codex = tmp_path / "internal" / "codex"
    source = archive_fixture(
        muse, "2026/09/session.jsonl.gz", gzip.compress(b"archive me", mtime=0)
    )
    destinations = []
    mount_checks = []
    monkeypatch.delenv(session_logs.ARCHIVE_ROOT_ENV, raising=False)
    monkeypatch.setattr(session_log_archive, "_ensure_base", lambda _base: True)
    monkeypatch.setattr(
        session_log_archive,
        "_archive_file",
        lambda _source, destination: destinations.append(destination)
        or ("retained", False),
    )

    session_log_archive.archive_pass(
        muse_root=muse,
        codex_root=codex,
        mount_check=lambda path: mount_checks.append(path) or True,
    )

    assert source.exists()
    # The archive base sits below the volume, so the mount is the volume.
    assert mount_checks == [Path("/Volumes/External SSD")]
    assert destinations == [
        Path(
            "/Volumes/External SSD/Archives/session-logs/muse/"
            "2026/09/session.jsonl.gz"
        )
    ]


def test_archive_pass_copies_then_deletes_only_a_verified_gzip(tmp_path):
    muse, codex, volume, archive = roots(tmp_path)
    contents = b'{"type":"event","value":"archived"}\n'
    muse_compressed = gzip.compress(contents, mtime=0)
    codex_compressed = gzip.compress(
        b'{"type":"session_meta","payload":{"thread_source":"automation"}}\n'
        b'{"type":"event","value":"codex"}\n',
        mtime=0,
    )
    relative = Path("2026") / "09" / "session.jsonl.gz"
    muse_source = archive_fixture(muse, str(relative), muse_compressed)
    codex_source = codex / relative
    codex_source.parent.mkdir(parents=True, exist_ok=True)
    codex_source.write_bytes(codex_compressed)

    summary = run_pass(muse, codex, volume, archive)

    muse_destination = archive / "muse" / relative
    codex_destination = archive / "codex" / relative
    assert muse_destination.read_bytes() == muse_compressed
    assert codex_destination.read_bytes() == codex_compressed
    assert not muse_source.exists()
    assert not codex_source.exists()
    assert session_logs.resolve_path(
        str(muse_source)[:-3], "muse",
        store_root=str(muse), archive_base=str(archive),
    ) == str(muse_destination)
    assert session_logs.resolve_path(
        str(codex_source)[:-3], "codex",
        store_root=str(codex), archive_base=str(archive),
    ) == str(codex_destination)
    assert summary == {
        "status": "ok", "copied": 2, "deleted": 2,
        "retained": 0, "errors": 0,
    }


def test_base_creation_failure_is_reported_non_ok_without_deleting_source(
        tmp_path):
    muse, codex, volume, _archive = roots(tmp_path)
    source = archive_fixture(
        muse, "2026/09/session.jsonl.gz", gzip.compress(b"keep me", mtime=0)
    )
    blocked_base = tmp_path / "base-is-a-file"
    blocked_base.write_text("not a directory")

    summary = run_pass(muse, codex, volume, blocked_base)

    assert source.exists()
    assert summary == {
        "status": "error", "copied": 0, "deleted": 0,
        "retained": 0, "errors": 1,
    }


def test_unwritable_base_is_reported_non_ok_with_no_sources(tmp_path):
    muse, codex, volume, _archive = roots(tmp_path)
    blocked_base = tmp_path / "base-is-a-file"
    blocked_base.write_text("not a directory")

    summary = run_pass(muse, codex, volume, blocked_base)

    assert summary["status"] == "error"
    assert summary["errors"] == 1


def test_daily_pass_archives_once_per_local_day(tmp_path):
    muse, codex, volume, archive = roots(tmp_path)
    first = archive_fixture(
        muse, "2026/09/first.jsonl.gz", gzip.compress(b"first", mtime=0)
    )
    messages = []
    morning = datetime.datetime.now().astimezone().replace(
        hour=6, minute=0, second=0, microsecond=0
    )

    def daily(now):
        return session_log_archive.run_daily_pass(
            now=now,
            muse_root=muse,
            codex_root=codex,
            archive_base=archive,
            volume_root=volume,
            mount_check=lambda _path: True,
            emit=messages.append,
        )

    assert daily(morning) == 0
    later = archive_fixture(
        muse, "2026/09/later.jsonl.gz", gzip.compress(b"later", mtime=0)
    )
    assert daily(morning.replace(hour=21)) == 0

    assert messages == [
        "session-log archive: status=ok copied=1 deleted=1 retained=0 errors=0"
    ]
    assert not first.exists()
    assert later.exists()

    assert daily(morning + datetime.timedelta(days=1)) == 0
    assert not later.exists()
    assert len(messages) == 2


def test_daily_pass_retries_the_same_day_after_an_unwritable_base(tmp_path):
    muse, codex, volume, _archive = roots(tmp_path)
    blocked_base = tmp_path / "base-is-a-file"
    blocked_base.write_text("not a directory")
    messages = []
    morning = datetime.datetime.now().astimezone().replace(
        hour=6, minute=0, second=0, microsecond=0
    )

    for hour in (6, 9):
        session_log_archive.run_daily_pass(
            now=morning.replace(hour=hour),
            muse_root=muse,
            codex_root=codex,
            archive_base=blocked_base,
            volume_root=volume,
            mount_check=lambda _path: True,
            emit=messages.append,
        )

    assert messages == [
        "session-log archive: status=error copied=0 deleted=0 retained=0 errors=1"
    ] * 2


def test_module_runs_as_a_script(tmp_path):
    # Hold the subprocess after 03:00 so the script must reach
    # __main__ -> main() -> run_daily_pass() and emit its mount result.
    (tmp_path / "sitecustomize.py").write_text(
        "import datetime as _datetime\n"
        "class _FixedDateTime(_datetime.datetime):\n"
        "    @classmethod\n"
        "    def now(cls, tz=None):\n"
        "        return cls(2026, 10, 3, 15, 0, tzinfo=_datetime.timezone.utc)\n"
        "_datetime.datetime = _FixedDateTime\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env[session_logs.ARCHIVE_ROOT_ENV] = str(tmp_path / "not-mounted" / "logs")
    pythonpath = [str(tmp_path)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    completed = subprocess.run(
        [sys.executable, str(ROOT / "session_log_archive.py")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == (
        "session-log archive: skipped; external volume is not mounted\n"
    )


def test_per_file_archive_error_is_reported_non_ok_with_counts(tmp_path):
    muse, codex, volume, archive = roots(tmp_path)
    source = archive_fixture(
        muse, "2026/09/session.jsonl.gz", gzip.compress(b"source", mtime=0)
    )
    destination = archive / "muse" / "2026/09/session.jsonl.gz"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(gzip.compress(b"other!", mtime=0))

    summary = run_pass(muse, codex, volume, archive)

    assert source.exists()
    assert summary == {
        "status": "error", "copied": 0, "deleted": 0,
        "retained": 1, "errors": 1,
    }


def test_module_entrypoint_runs_daily_pass_once(monkeypatch):
    calls = []

    def run_daily_pass():
        calls.append("called")
        return 7

    monkeypatch.setattr(session_log_archive, "run_daily_pass", run_daily_pass)

    assert session_log_archive.main() == 7
    assert calls == ["called"]


def test_codex_archival_is_limited_to_non_user_thread_sources(tmp_path):
    muse, codex, volume, archive = roots(tmp_path)
    sources = {}
    for thread_source in ("user", "automation", "subagent", "review", "other"):
        relative = Path("2026") / "09" / f"{thread_source}.jsonl.gz"
        source = codex / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        records = (
            '{"type":"session_meta","payload":{"thread_source":"%s"}}\n'
            % thread_source
        ).encode() + b'{"type":"event"}\n'
        source.write_bytes(gzip.compress(records, mtime=0))
        sources[thread_source] = (source, archive / "codex" / relative)

    summary = run_pass(muse, codex, volume, archive)

    for thread_source in ("automation", "subagent", "review"):
        source, destination = sources[thread_source]
        assert not source.exists()
        assert destination.exists()
    for thread_source in ("user", "other"):
        source, destination = sources[thread_source]
        assert source.exists()
        assert not destination.exists()
    assert summary == {
        "status": "ok", "copied": 3, "deleted": 3,
        "retained": 2, "errors": 0,
    }


def _conflicting_archive(tmp_path: Path, source_bytes: bytes, destination_bytes: bytes):
    muse, codex, volume, archive = roots(tmp_path)
    relative = Path("2026") / "09" / "session.jsonl.gz"
    source = archive_fixture(muse, str(relative), source_bytes)
    destination = archive / "muse" / relative
    destination.parent.mkdir(parents=True)
    destination.write_bytes(destination_bytes)

    summary = run_pass(muse, codex, volume, archive)

    assert source.exists()
    assert destination.read_bytes() == destination_bytes
    assert summary["deleted"] == 0
    assert summary["retained"] == 1
    assert summary["errors"] == 1


def test_size_mismatch_retains_the_internal_copy(tmp_path):
    source = gzip.compress(b"same sample", mtime=0)
    _conflicting_archive(tmp_path, source, source[:-1])


def test_checksum_mismatch_retains_the_internal_copy(tmp_path):
    source = gzip.compress(b"same sample", mtime=0)
    same_sample_different_header = gzip.compress(b"same sample", mtime=1)
    assert len(source) == len(same_sample_different_header)
    _conflicting_archive(tmp_path, source, same_sample_different_header)


def test_readable_sample_mismatch_retains_the_internal_copy(tmp_path):
    source = gzip.compress(b"left", mtime=0)
    different_sample = gzip.compress(b"rght", mtime=0)
    assert len(source) == len(different_sample)
    _conflicting_archive(tmp_path, source, different_sample)


def test_unmounted_volume_reports_skip_and_does_not_fail_the_lane(tmp_path):
    muse = tmp_path / "internal" / "muse"
    codex = tmp_path / "internal" / "codex"
    missing_volume = tmp_path / "not-mounted"
    archive = missing_volume / "Agent-Logs"
    source = archive_fixture(
        muse, "2026/09/session.jsonl.gz", gzip.compress(b"keep me", mtime=0)
    )
    messages = []
    local_three_am = datetime.datetime.now().astimezone().replace(
        hour=3, minute=15, second=0, microsecond=0
    )

    result = session_log_archive.run_daily_pass(
        now=local_three_am,
        muse_root=muse,
        codex_root=codex,
        archive_base=archive,
        volume_root=missing_volume,
        emit=messages.append,
    )

    assert result == 0
    assert messages == [
        "session-log archive: skipped; external volume is not mounted"
    ]
    assert source.exists()
    assert not archive.exists()


def test_archive_error_is_reported_without_failing_the_lane(tmp_path, monkeypatch):
    messages = []
    local_three_am = datetime.datetime.now().astimezone().replace(
        hour=3, minute=15, second=0, microsecond=0
    )

    def fail_archive(**_kwargs):
        raise OSError("archive volume unavailable")

    monkeypatch.setattr(session_log_archive, "archive_pass", fail_archive)
    result = session_log_archive.run_daily_pass(
        now=local_three_am,
        archive_base=tmp_path / "Agent-Logs",
        emit=messages.append,
    )

    assert result == 0
    assert len(messages) == 1
    assert messages[0].startswith("session-log archive: skipped")


def test_daily_pass_is_quiet_outside_the_scheduled_hour(tmp_path):
    messages = []
    local_two_am = datetime.datetime.now().astimezone().replace(
        hour=2, minute=59, second=0, microsecond=0
    )

    result = session_log_archive.run_daily_pass(
        now=local_two_am,
        archive_base=tmp_path / "Agent-Logs",
        volume_root=tmp_path / "missing-volume",
        emit=messages.append,
    )

    assert result == 0
    assert messages == []
