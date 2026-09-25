"""Session-local token extraction for outcome attribution."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import session_usage  # noqa: E402


START = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
FINISH = datetime(2026, 9, 10, 12, 5, tzinfo=timezone.utc)


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_codex_sums_turn_usage_and_ignores_cumulative_event_messages(
    tmp_path, monkeypatch
):
    session_id = "codex-session"
    path = tmp_path / session_id / "rollout.jsonl"
    path.parent.mkdir()
    write_jsonl(path, [
        {
            "type": "token_usage_record",
            "timestamp": "2026-09-10T12:01:00Z",
            "payload": {
                "turn_id": "turn-1",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "cache_write_input_tokens": 4,
                    "output_tokens": 7,
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-09-10T12:01:01Z",
            "payload": {"info": {"total_token_usage": {
                "input_tokens": 100,
                "cached_input_tokens": 60,
                "output_tokens": 7,
            }}},
        },
        {
            "type": "token_usage_record",
            "timestamp": "2026-09-10T12:02:00Z",
            "payload": {
                "turn_id": "turn-2",
                "usage": {
                    "input_tokens": 50,
                    "cached_input_tokens": 20,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 3,
                },
            },
        },
    ])
    monkeypatch.setitem(
        session_usage.SESSION_GLOBS,
        "codex",
        str(tmp_path / "*" / "*.jsonl"),
    )

    assert session_usage.usage_for_session(
        "codex", session_id, START, FINISH
    ) == {
        "fresh_input_tokens": 70,
        "cache_read_input_tokens": 80,
        "cache_write_input_tokens": 4,
        "output_tokens": 10,
    }


def test_claude_deduplicates_replayed_assistant_messages(tmp_path, monkeypatch):
    session_id = "claude-session"
    path = tmp_path / (session_id + ".jsonl")
    row = {
        "type": "assistant",
        "timestamp": "2026-09-10T12:01:00Z",
        "uuid": "outer-1",
        "message": {
            "id": "message-1",
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 4,
                "output_tokens": 5,
            },
        },
    }
    write_jsonl(path, [row, dict(row, uuid="outer-2")])
    monkeypatch.setitem(
        session_usage.SESSION_GLOBS,
        "claude",
        str(tmp_path / "*.jsonl"),
    )

    assert session_usage.usage_for_session(
        "claude", session_id, START, FINISH
    ) == {
        "fresh_input_tokens": 2,
        "cache_read_input_tokens": 30,
        "cache_write_input_tokens": 4,
        "output_tokens": 5,
    }


def test_muse_journal_extracts_embedded_token_usage(tmp_path, monkeypatch):
    session_id = "muse-session"
    path = tmp_path / session_id / "journal-00000000.bin"
    path.parent.mkdir()
    events = []
    for turn, cached, output in (("turn-1", 0, 7), ("turn-2", 20, 3)):
        events.append(json.dumps({
            "method": "session/tokenUsage",
            "params": {
                "turnId": turn,
                "usage": {
                    "inputTokens": 100 if turn == "turn-1" else 50,
                    "cacheReadTokens": cached,
                    "cacheWriteTokens": 0,
                    "outputTokens": output,
                },
            },
        }).encode())
    path.write_bytes(b"binary-prefix" + b"\x00".join(events) + b"binary-suffix")
    monkeypatch.setitem(
        session_usage.SESSION_GLOBS,
        "muse",
        str(tmp_path / "*" / "*"),
    )

    assert session_usage.usage_for_session("muse", session_id) == {
        "fresh_input_tokens": 130,
        "cache_read_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
    }


def _write_muse_session(tmp_path, session_id, input_tokens, cached_tokens,
                        output_tokens):
    path = tmp_path / session_id / "journal-00000000.bin"
    path.parent.mkdir()
    event = json.dumps({
        "method": "session/tokenUsage",
        "params": {
            "turnId": "turn-1",
            "usage": {
                "inputTokens": input_tokens,
                "cacheReadTokens": cached_tokens,
                "cacheWriteTokens": 0,
                "outputTokens": output_tokens,
            },
        },
    }).encode()
    path.write_bytes(b"binary-prefix" + event + b"binary-suffix")


def test_muse_sums_every_recorded_session_with_complete_coverage(
    tmp_path, monkeypatch
):
    _write_muse_session(tmp_path, "muse-one", 10, 2, 3)
    _write_muse_session(tmp_path, "muse-two", 20, 5, 7)
    monkeypatch.setitem(
        session_usage.SESSION_GLOBS,
        "muse",
        str(tmp_path / "*" / "*"),
    )

    usage, coverage = session_usage.usage_for_sessions(
        "muse", ["muse-one", "muse-two"], 2
    )

    assert usage == {
        "fresh_input_tokens": 23,
        "cache_read_input_tokens": 7,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
    }
    assert coverage == {
        "status": "complete",
        "captured_calls": 2,
        "made_calls": 2,
        "readable_journals": 2,
        "unreadable_journals": 0,
        "uncaptured_calls": 0,
    }


def test_muse_missing_session_journal_is_partial_not_zero(tmp_path, monkeypatch):
    _write_muse_session(tmp_path, "muse-one", 10, 2, 3)
    monkeypatch.setitem(
        session_usage.SESSION_GLOBS,
        "muse",
        str(tmp_path / "*" / "*"),
    )

    usage, coverage = session_usage.usage_for_sessions(
        "muse", ["muse-one", "missing"], 2
    )

    assert usage is None
    assert coverage == {
        "status": "partial",
        "captured_calls": 2,
        "made_calls": 2,
        "readable_journals": 1,
        "unreadable_journals": 1,
        "uncaptured_calls": 0,
        "reasons": ["unreadable_session_journals_or_usage"],
    }


def test_malformed_session_list_is_a_fault_not_a_partial_sum():
    usage, coverage = session_usage.usage_for_sessions(
        "muse", ["muse-one"], 2
    )

    assert usage is None
    assert coverage["status"] == "fault"
    assert coverage["reasons"] == ["call_count_mismatch"]


def test_missing_token_kind_stays_null(tmp_path, monkeypatch):
    session_id = "codex-session"
    path = tmp_path / session_id / "rollout.jsonl"
    path.parent.mkdir()
    write_jsonl(path, [{
        "type": "token_usage_record",
        "timestamp": "2026-09-10T12:01:00Z",
        "payload": {
            "turn_id": "turn-1",
            "usage": {"input_tokens": 100, "cached_input_tokens": 20,
                       "output_tokens": 5},
        },
    }])
    monkeypatch.setitem(
        session_usage.SESSION_GLOBS,
        "codex",
        str(tmp_path / "*" / "*.jsonl"),
    )

    usage = session_usage.usage_for_session("codex", session_id, START, FINISH)
    assert usage["cache_write_input_tokens"] is None
    assert usage["fresh_input_tokens"] == 80
