"""Verified movement of compressed session logs to the SSD archive."""

from __future__ import annotations

import datetime
import gzip
from pathlib import Path
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


def test_archive_pass_copies_then_deletes_only_a_verified_gzip(tmp_path):
    muse, codex, volume, archive = roots(tmp_path)
    contents = b'{"type":"event","value":"archived"}\n'
    muse_compressed = gzip.compress(contents, mtime=0)
    codex_compressed = gzip.compress(
        b'{"type":"event","value":"codex"}\n', mtime=0
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


def test_codex_app_routine_runs_archive_after_a_passing_gate():
    routine = (ROOT / "routines" / "codex-work.md").read_text()
    begin_call = routine.index("funnel.py begin")
    archive_call = routine.index("session_log_archive.py")
    stop_handling = routine.index('When `do` is `stop`')

    assert begin_call < archive_call < stop_handling
    assert 'only when its JSON has `gate: "ok"`' in routine
    assert "Skip it for every other gate" in routine
    assert "never\nin the launchd run-keeper" in routine


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
