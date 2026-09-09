"""The Codex work routine does not let one blocked head starve the queue.

The retry loop is deliberately prompt-level: `funnel.py` owns ordering and its
`--not` option is the only per-run exclusion mechanism. These fixtures pin the
commands and the bound in the routine so a later edit cannot quietly restore the
old one-ticket-and-stop behaviour.
"""

from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
ROUTINE = ROOT / "routines" / "codex-work.md"


def routine() -> str:
    return ROUTINE.read_text(encoding="utf-8")


def test_the_opening_is_one_begin_call_that_already_claims_the_ticket():
    body = routine()

    assert body.count("funnel.py begin --agent codex --tier standard") == 1
    assert "heartbeat.py start --agent codex" not in body
    assert "usage.py gate codex" not in body
    assert "already claimed" in body


def test_a_declined_ticket_is_released_before_the_next_filtered_lookup():
    body = routine()
    release = "funnel.py release <declined-ref>"
    retry = "funnel.py next --tier standard --not <declined-ref>"

    assert release in body
    assert retry in body
    assert body.index(release) < body.index(retry)


def test_the_next_lookup_accumulates_every_earlier_decline():
    body = routine()

    assert (
        "funnel.py next --tier standard --not <declined-ref-1> "
        "--not <declined-ref-2>"
    ) in body


def test_three_declines_stop_without_queue_state_or_a_fourth_candidate():
    body = routine()
    prose = " ".join(body.split())

    assert "consider at most three candidates in one run" in body
    assert "Do not ask for a fourth candidate" in prose
    assert "finish with `skipped-blocked`" in body
    assert "naming **every** declined ref" in body
    assert "do not persist a decline or reorder the queue" in body


def test_blocked_finish_example_names_all_three_declined_refs():
    body = routine()
    finish = (
        "--outcome skipped-blocked --note "
        '"declined <ref-1>; declined <ref-2>; declined <ref-3>"'
    )

    assert finish in body


def test_human_step_stop_uses_only_the_human_step_outcome():
    body = routine()
    start = body.index("### Mid-work discovery: convert, record, stop")
    end = body.index("## 4. Open a pull request")
    handoff = body[start:end]

    assert handoff.count("--outcome skipped-human-step") == 1
    assert "--outcome errored" not in handoff
    assert "`skipped-human-step` outcome records" in handoff
    assert 'finish with `--outcome errored --note "<what broke>"`' in body
