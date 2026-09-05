"""prior_run.py — recovering a dead run's intent.

The output is read by an agent that is about to act on it, so the failure that
matters is a confident digest of the wrong session, or one so large that reading
it costs as much as redoing the work.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import prior_run  # noqa: E402


def rollout(path, messages, cwd="/tmp/work", stamp="2026-09-05T08:00:00.000Z"):
    lines = [json.dumps({"type": "session_meta", "payload": {
        "id": "abc", "cwd": cwd, "timestamp": stamp, "originator": "test"}})]
    for role, text in messages:
        lines.append(json.dumps(
            {"type": "response_item", "payload": {"role": role, "content": text}}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_injected_context_blocks_are_not_intent(tmp_path):
    """Both vendors inject catalogues and environment dumps as pseudo-user
    turns. They would eat the whole cap and say nothing about intent."""
    f = rollout(tmp_path / "r.jsonl", [
        ("user", "<recommended_plugins>" + "x" * 5000 + "</recommended_plugins>"),
        ("user", "Please do issue #42"),
        ("assistant", "Reading the issue now."),
    ])
    d = prior_run.digest(str(f))
    assert [m["text"] for m in d["messages"]] == ["Please do issue #42",
                                                  "Reading the issue now."]


def test_the_tail_is_kept_because_that_is_where_it_got_to(tmp_path):
    f = rollout(tmp_path / "r.jsonl",
                [("assistant", "step %d" % i) for i in range(20)])
    d = prior_run.digest(str(f))
    assert len(d["messages"]) == prior_run.MAX_MESSAGES
    assert d["messages"][-1]["text"] == "step 19"
    assert d["total_messages"] == 20


def test_long_messages_are_truncated_in_the_render(tmp_path):
    f = rollout(tmp_path / "r.jsonl", [("assistant", "y" * 5000)])
    out = prior_run.render(prior_run.digest(str(f)), "42")
    assert "[truncated]" in out
    assert len(out) < 3000


def test_sessions_older_than_the_window_are_not_offered(tmp_path):
    old = rollout(tmp_path / "old.jsonl", [("user", "issue #42")])
    os.utime(old, (0, 0))
    assert prior_run.candidates(str(tmp_path / "*.jsonl"), ["#42"], 3) == []


def test_the_newest_matching_session_wins(tmp_path):
    a = rollout(tmp_path / "a.jsonl", [("user", "issues/42 first")])
    b = rollout(tmp_path / "b.jsonl", [("user", "issues/42 second")])
    os.utime(a, (1, 1))
    found = prior_run.candidates(str(tmp_path / "*.jsonl"), ["issues/42"], 99999)
    assert [os.path.basename(p) for p in found] == ["b.jsonl", "a.jsonl"]


def test_a_session_that_never_mentions_the_ticket_is_not_a_candidate(tmp_path):
    rollout(tmp_path / "r.jsonl", [("user", "something else entirely")])
    assert prior_run.candidates(str(tmp_path / "*.jsonl"), ["#42"], 99999) == []


def test_missing_prior_run_exits_1_rather_than_inventing_one(tmp_path, monkeypatch):
    monkeypatch.setattr(prior_run, "CODEX_SESSIONS", str(tmp_path / "*.jsonl"))
    assert prior_run.main(["42"]) == 1


def test_a_ticket_url_is_accepted_as_well_as_a_number(tmp_path, monkeypatch):
    rollout(tmp_path / "r.jsonl", [("user", "work on issues/42 please")])
    monkeypatch.setattr(prior_run, "CODEX_SESSIONS", str(tmp_path / "*.jsonl"))
    assert prior_run.main(
        ["https://github.com/nateprich-projects/command-center/issues/42"]) == 0


# -- the two vendors share no transcript structure -------------------------


def claude_transcript(path, records, cwd="/tmp/work", model="claude-opus-5",
                      effort="high"):
    lines = []
    for role, content in records:
        message = {"role": role, "content": content}
        if role == "assistant":
            message["model"] = model
        lines.append(json.dumps({
            "type": role, "cwd": cwd, "sessionId": "s1", "gitBranch": "ticket/42",
            "timestamp": "2026-09-05T08:00:00.000Z", "effort": effort,
            "message": message,
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_claude_transcript_is_parsed(tmp_path):
    """Claude nests the role under `message` and has no session_meta. Assuming
    Codex's shape produced a digest with zero messages that still rendered a
    confident header."""
    f = claude_transcript(tmp_path / "c.jsonl", [
        ("user", "Please review PR #42"),
        ("assistant", [{"type": "text", "text": "Reading the diff against plan.md."}]),
    ])
    d = prior_run.digest(str(f), shape="claude")
    assert [m["text"] for m in d["messages"]] == [
        "Please review PR #42", "Reading the diff against plan.md."]
    assert d["meta"]["cwd"] == "/tmp/work"


def test_the_model_and_effort_are_captured(tmp_path):
    """A scheduled task sets neither — it inherits the app default at fire time,
    and nothing else records which was used. Both bear on review and breakdown
    quality, so a default changed mid-week must not be invisible."""
    f = claude_transcript(tmp_path / "c.jsonl",
                          [("assistant", [{"type": "text", "text": "hi"}])],
                          model="claude-opus-5", effort="high")
    d = prior_run.digest(str(f), shape="claude")
    assert d["meta"]["model"] == "claude-opus-5"
    assert d["meta"]["effort"] == "high"
    assert "claude-opus-5 (effort high)" in prior_run.render(d, "42")


def test_claude_tool_uses_are_collected(tmp_path):
    f = claude_transcript(tmp_path / "c.jsonl", [
        ("assistant", [{"type": "tool_use", "name": "Bash",
                        "input": {"command": "git status"}}]),
    ])
    d = prior_run.digest(str(f), shape="claude")
    assert any("Bash" in call for call in d["tool_calls"])


def test_claude_thinking_blocks_are_skipped(tmp_path):
    """They are the largest part of a transcript, and the text blocks already
    carry the intent."""
    f = claude_transcript(tmp_path / "c.jsonl", [
        ("assistant", [{"type": "thinking", "thinking": "z" * 9000},
                       {"type": "text", "text": "Done."}]),
    ])
    d = prior_run.digest(str(f), shape="claude")
    assert [m["text"] for m in d["messages"]] == ["Done."]


def test_the_shape_is_detected_when_not_declared(tmp_path):
    codex = rollout(tmp_path / "a.jsonl", [("user", "codex side")])
    claude = claude_transcript(tmp_path / "b.jsonl", [("user", "claude side")])
    assert prior_run.digest(str(codex))["messages"][0]["text"] == "codex side"
    assert prior_run.digest(str(claude))["messages"][0]["text"] == "claude side"


# -- stranded work ----------------------------------------------------------


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args),
                   capture_output=True, check=True)


def test_stranded_reports_uncommitted_work_left_behind(tmp_path):
    """Each Codex session gets its own directory, so a later run does not
    inherit this one. Naming it is what makes rescue possible."""
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("committed")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "first")
    (repo / "b.txt").write_text("never committed")

    left = prior_run.stranded(str(repo))
    assert left["clean"] is False
    assert left["uncommitted"] == 1


def test_stranded_says_clean_when_nothing_was_left(tmp_path):
    repo = tmp_path / "work"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "first")
    assert prior_run.stranded(str(repo))["clean"] is True


def test_a_vanished_working_directory_is_not_an_error(tmp_path):
    assert prior_run.stranded(str(tmp_path / "gone")) is None
    assert prior_run.stranded(None) is None
