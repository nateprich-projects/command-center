"""What model actually ran, recorded rather than reported.

Routing across models is only worth doing if the results can be attributed, and
attribution has to survive being wrong. A prompt asked to name its own model
answers with what it believes; one confident wrong answer poisons the dataset
silently, and the dataset is the whole point.

So the model is read from the session file the agent writes anyway — the same
files `usage.py` already globs for quota.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def point_at(monkeypatch, agent, pattern):
    monkeypatch.setitem(heartbeat.MODEL_SOURCES, agent, str(pattern))


def test_codex_model_and_effort_are_read_from_the_rollout(tmp_path, monkeypatch):
    write(tmp_path / "rollout.jsonl",
          [{"payload": {"model": "gpt-5.6-sol", "effort": "high"}}])
    point_at(monkeypatch, "codex", tmp_path / "*.jsonl")
    found = heartbeat.detect_model("codex")
    assert found["provider"] == "openai"
    assert found["model"] == "gpt-5.6-sol"
    assert found["reasoning_effort"] == "high"
    assert found["model_source"] == "detected"


def test_claude_model_is_read_from_the_transcript(tmp_path, monkeypatch):
    write(tmp_path / "session.jsonl",
          [{"message": {"model": "claude-opus-5"}}])
    point_at(monkeypatch, "claude", tmp_path / "*.jsonl")
    monkeypatch.setattr(heartbeat, "CLAUDE_SETTINGS", str(tmp_path / "none.json"))
    assert heartbeat.detect_model("claude")["model"] == "claude-opus-5"


def test_the_newest_session_wins(tmp_path, monkeypatch):
    import os, time
    old = write(tmp_path / "old.jsonl", [{"payload": {"model": "gpt-old"}}])
    new = write(tmp_path / "new.jsonl", [{"payload": {"model": "gpt-new"}}])
    os.utime(old, (time.time() - 600, time.time() - 600))
    point_at(monkeypatch, "codex", tmp_path / "*.jsonl")
    assert heartbeat.detect_model("codex")["model"] == "gpt-new"


def test_a_missing_session_records_null_rather_than_guessing(tmp_path, monkeypatch):
    point_at(monkeypatch, "codex", tmp_path / "nothing-here" / "*.jsonl")
    found = heartbeat.detect_model("codex")
    assert found["model"] is None
    assert found["provider"] == "openai"


def test_unreadable_session_is_never_fatal(tmp_path, monkeypatch):
    (tmp_path / "broken.jsonl").write_text("{not json at all\n")
    point_at(monkeypatch, "codex", tmp_path / "*.jsonl")
    assert heartbeat.detect_model("codex")["model"] is None


def test_claude_effort_falls_back_to_its_settings(tmp_path, monkeypatch):
    write(tmp_path / "session.jsonl", [{"message": {"model": "claude-opus-5"}}])
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"effortLevel": "high"}))
    point_at(monkeypatch, "claude", tmp_path / "session.jsonl")
    monkeypatch.setattr(heartbeat, "CLAUDE_SETTINGS", str(settings))
    assert heartbeat.detect_model("claude")["reasoning_effort"] == "high"


def test_provider_is_the_pool_not_the_model(tmp_path, monkeypatch):
    """Routing will put more than one model on a pool. The budget is per pool."""
    assert heartbeat.PROVIDERS["codex"] == "openai"
    assert heartbeat.PROVIDERS["claude"] == "anthropic"
