"""A heartbeat record carries its own agent's meter, not Codex's (#514)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import usage  # noqa: E402


def _reading(source, resets_at):
    return {"source": source, "captured_at": 1.0,
            "windows": {"five_hour": {"used_percent": 10.0, "resets_at": resets_at},
                        "seven_day": {"used_percent": 40.0, "resets_at": resets_at + 1}}}


@pytest.fixture
def meters(monkeypatch):
    monkeypatch.setattr(usage, "read_zai", lambda now: _reading("zai", 1789291561))
    monkeypatch.setattr(usage, "read_codex", lambda: _reading("codex", 1789445736))
    monkeypatch.setattr(usage, "read_claude", lambda: _reading("claude", 1789400000))
    monkeypatch.setattr(usage, "read_claude_local", lambda now=None: None)


def test_a_zcode_record_carries_the_zai_meter_and_never_codexs(meters):
    snapshot = heartbeat.usage_snapshot("zcode")
    assert snapshot["five_hour"]["resets_at"] == 1789291561
    assert "1789445736" not in str(snapshot)


def test_a_muse_record_says_it_was_never_gated(meters):
    assert heartbeat.usage_snapshot("muse") == {"source": "meta", "unmetered": True}


def test_codex_and_claude_keep_the_window_shape(meters):
    codex = heartbeat.usage_snapshot("codex")
    assert set(codex) == {"five_hour", "seven_day"}
    assert codex["five_hour"] == {"used_percent": 10.0, "resets_at": 1789445736}
    claude = heartbeat.usage_snapshot("claude")
    assert claude["five_hour"]["resets_at"] == 1789400000


def test_an_unreadable_meter_is_none_never_another_providers(meters, monkeypatch):
    def boom(now):
        raise OSError("z.ai unreachable")
    monkeypatch.setattr(usage, "read_zai", boom)
    assert heartbeat.usage_snapshot("zcode") is None


def test_an_unregistered_agent_is_none(meters):
    assert heartbeat.usage_snapshot("nobody") is None
