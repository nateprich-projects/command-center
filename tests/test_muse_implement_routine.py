"""The Muse implement prompt is judgement text; the runner owns the protocol (#817)."""

from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTINE = ROOT / "routines" / "muse-implement.md"


def routine() -> str:
    return ROUTINE.read_text(encoding="utf-8")


def test_runtime_is_judgement_text_with_the_packet_placeholder_once():
    body = routine()
    _, runtime = body.split("\n---\n", 1)

    assert runtime.count("PACKET_JSON") == 1
    assert "the packet after these instructions is the implementation evidence" in " ".join(
        runtime.split()).lower()
    assert "ticket" in runtime and "parent plan" in runtime
    assert "blocking" in runtime and "prior-run digest" in runtime


def test_runtime_returns_one_structured_answer_as_a_file():
    runtime = routine().split("\n---\n", 1)[1]
    normalized = " ".join(runtime.split())

    assert "answer.json" in runtime
    assert '"done":true' in runtime.replace(" ", "")
    assert '"blocked_on_human"' in runtime
    assert '"declined"' in runtime
    assert "an app UI with no API" in normalized
    assert "entering a credential" in normalized
    assert "an account or billing setting" in normalized
    assert "physical access to a machine" in normalized
    assert "say what you did not do" in normalized.lower()
    assert "validates the answer, tests the checkout, commits and pushes" in normalized
    assert "releases the claim, and finishes the run" in normalized


def test_runtime_names_no_protocol_the_runner_owns():
    runtime = routine().split("\n---\n", 1)[1]
    normalized = " ".join(runtime.split())

    assert "Do not run `funnel.py` or `gh`" in normalized
    for protocol in ("funnel.py begin", "funnel.py release", "funnel.py claim",
                     "funnel.py next", "heartbeat.py", "gh repo", "gh issue",
                     "gh pr", "```bash", "prior_run.py"):
        assert protocol not in runtime, (
            "judgement text only: {!r} is the runner's job".format(protocol))


def test_routine_is_under_the_500_word_acceptance_limit():
    assert len(routine().split()) < 500


def test_setup_header_keeps_the_runner_boundary_for_the_reader():
    setup, _ = routine().split("\n---\n", 1)
    normalized = " ".join(setup.split())

    assert "scripts/muse-implement" in normalized
    assert "ticket/<n>" in normalized
    assert "finish-ticket" in normalized
    assert "makes `funnel.py` and `gh` unnecessary rather than impossible" in normalized
    assert "Never add an implementation mode to `muse-review-engine`" in normalized
