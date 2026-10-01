"""Retention boundaries for the run-keeper's in-place gzip pass."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import session_log_compress


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()
MUSE_PREVIOUS_WINDOW_START = datetime(
    2026, 9, 21, 0, 0, tzinfo=timezone.utc
).timestamp()


def test_muse_window_predicate_keeps_current_and_previous_windows_raw():
    cutoff = session_log_compress.muse_compress_before(NOW)

    assert cutoff == MUSE_PREVIOUS_WINDOW_START
    assert session_log_compress.muse_is_eligible(cutoff - 1, NOW)
    assert not session_log_compress.muse_is_eligible(cutoff, NOW)
    assert not session_log_compress.muse_is_eligible(cutoff + 1, NOW)


def test_codex_predicate_compresses_old_automation_but_keeps_user_raw(tmp_path):
    automation = tmp_path / "automation.jsonl"
    user = tmp_path / "user.jsonl"
    automation.write_text(
        '{"type":"session_meta","payload":{"thread_source":"automation"}}\n'
    )
    user.write_text(
        '{"type":"session_meta","payload":{"thread_source":"user"}}\n'
    )
    old = NOW - 15 * 24 * 60 * 60
    os.utime(automation, (old, old))
    os.utime(user, (old, old))

    assert session_log_compress.codex_is_eligible(automation, NOW)
    assert not session_log_compress.codex_is_eligible(user, NOW)
