"""The Codex prompt is judgement text; the engine owns the protocol (#818)."""

from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTINE = ROOT / "routines" / "codex-work.md"


def routine() -> str:
    return ROUTINE.read_text(encoding="utf-8")


def test_runtime_has_one_begin_and_one_finish_ticket_command():
    body = routine()
    runtime = body.split("\n---\n", 1)[1]

    assert runtime.count(
        "python3 /Users/nateprich/.claude/command-center-run/funnel.py begin "
        "--agent codex --tier standard"
    ) == 1
    assert runtime.count(
        "python3 /Users/nateprich/.claude/command-center-run/funnel.py "
        "finish-ticket --run <run> --answer-file <path>"
    ) == 1
    assert "--routine-sha" not in runtime
    assert "funnel.py next" not in runtime
    assert "funnel.py release" not in runtime
    assert "funnel.py claim" not in runtime


def test_runtime_follows_the_packet_and_returns_one_structured_judgement():
    body = routine()
    runtime = body.split("\n---\n", 1)[1]
    normalized = " ".join(runtime.split())

    assert "the ticket is already claimed" in runtime
    assert "Treat `packet` as the implementation evidence" in normalized
    assert "Treat `vendor` as binding" in normalized
    assert "several minutes" in funnel_wait_rule()
    assert '"done":true' in runtime
    assert '"blocked_on_human"' in runtime
    assert '"declined"' in runtime
    assert "tests the checkout, commits and pushes, opens" in runtime
    assert "outside the ticket checkout" in normalized


def test_runtime_waits_for_the_same_slow_begin_session():
    runtime = routine().split("\n---\n", 1)[1]
    normalized = " ".join(runtime.split())

    assert "`begin` can take several minutes" in normalized
    assert "keep reading that same exec session until the process exits" in normalized
    assert "Never treat that yield as a failure" in normalized
    assert "never invoke `begin` again" in normalized


def funnel_wait_rule() -> str:
    import funnel

    return funnel.CODEX_IMPLEMENT_VENDOR["begin_wait"]


def test_routine_is_under_the_500_word_acceptance_limit():
    assert len(routine().split()) < 500


def test_setup_header_keeps_the_sandbox_boundary_for_nate():
    setup, _ = routine().split("\n---\n", 1)

    assert "per-session directory" in setup
    assert "command-center-heartbeat" in setup
    assert "/Users/nateprich/.claude/command-center-run" in setup
    assert "read-and-execute access only" in setup
    assert "Never invoke the Codex CLI headlessly" in setup
