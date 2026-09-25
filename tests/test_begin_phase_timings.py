"""Post-load begin timing markers are incremental and review-only."""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
PHASES = [
    "detail_hydration",
    "ticket_pr_facts",
    "reconcile_approved_merges",
    "reconcile_auto_closeable_projects",
    "reconcile_closed_items",
    "reconcile_parked_wakes",
    "reconcile_closed_claims",
    "reconcile_orphaned_starts",
    "review_queue",
    "self_approvals",
    "breakdown_queue",
    "shape_queue",
    "candidate_selection",
    "reserve_gate",
    "run_binding",
]


def _patch_review_path(monkeypatch, *, stop_at=None):
    def reconcile(name):
        def run(*args, **kwargs):
            if stop_at == name:
                raise RuntimeError("simulated begin timeout")
            return []

        return run

    for name in (
        "reconcile_approved_merges",
        "reconcile_auto_closeable_projects",
        "reconcile_closed_items",
        "reconcile_parked_wakes",
        "reconcile_closed_claims",
        "reconcile_orphaned_starts",
    ):
        monkeypatch.setattr(funnel, name, reconcile(name))

    monkeypatch.setattr(funnel, "begin_detail_candidates", lambda *args: [object()])
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: {})
    monkeypatch.setattr(funnel, "review_queue", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        funnel, "sweep_shaped_self_approvals", lambda *args, **kwargs: ([], [])
    )
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda *args: [])
    monkeypatch.setattr(funnel, "shapeable_idea", lambda *args: None)
    monkeypatch.setattr(funnel, "_reserve_verdict", lambda do: None)
    monkeypatch.setattr(funnel, "_bind_run", lambda agent, out: None)


def _run_review_begin(monkeypatch, *, stop_at=None):
    _patch_review_path(monkeypatch, stop_at=stop_at)
    timings = {}
    result = funnel.cmd_begin(
        [],
        NOW,
        "muse",
        "escalated",
        False,
        caller_role="review",
        _detail_loader=lambda candidates: None,
        _preflight=({"run": "timing-test"}, {"provider": "fixture"}),
        timings=timings,
        _phase_started=time.perf_counter() - 1.0,
    )
    return result, timings


def _logged_phases(stderr):
    matches = [
        re.fullmatch(
            r"begin_review_phase phase=([a-z_]+) elapsed_seconds=([0-9]+\.[0-9]{6})",
            line,
        )
        for line in stderr.splitlines()
    ]
    assert all(match is not None for match in matches)
    return [(match.group(1), float(match.group(2))) for match in matches]


def test_review_begin_logs_every_boundary_and_keeps_stdout_json(monkeypatch, capsys):
    assert _run_review_begin(monkeypatch)[0] == 0
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["do"] == "stop"
    assert payload["timings"]["begin_load.ticket_pr_facts"] >= 0
    assert "begin_review_phase" not in captured.out

    logged = _logged_phases(captured.err)
    assert [phase for phase, _ in logged] == PHASES
    elapsed = [seconds for _, seconds in logged]
    assert all(seconds >= 0.9 for seconds in elapsed)
    assert elapsed == sorted(elapsed)


def test_review_begin_timeout_keeps_only_reached_boundaries(monkeypatch, capsys):
    with pytest.raises(RuntimeError, match="simulated begin timeout"):
        _run_review_begin(monkeypatch, stop_at="reconcile_closed_items")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert [phase for phase, _ in _logged_phases(captured.err)] == PHASES[:5]


def test_implement_begin_does_not_emit_review_phase_markers(monkeypatch, capsys):
    _patch_review_path(monkeypatch)
    for name in (
        "clear_satisfied_blocks",
        "reconcile_abandoned_claims",
    ):
        monkeypatch.setattr(funnel, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "awaiting_review", lambda *args, **kwargs: set())
    monkeypatch.setattr(funnel, "approved_conflicting_refs", lambda *args: set())
    monkeypatch.setattr(funnel, "finished_by_comments", lambda *args: set())
    monkeypatch.setattr(funnel, "_backed_off_work", lambda *args, **kwargs: {})
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)
    monkeypatch.setattr(funnel, "held_claims_before", lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "lock_holder", lambda *args, **kwargs: None)
    monkeypatch.setattr(funnel, "readiness_blockers", lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "freeze_withheld", lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "at_capacity", lambda *args, **kwargs: False)
    monkeypatch.setattr(funnel, "_record_queue_empty", lambda *args, **kwargs: None)

    assert funnel.cmd_begin(
        [],
        NOW,
        "codex",
        "standard",
        False,
        caller_role="implement",
        _detail_loader=lambda candidates: pytest.fail(
            "implement path must not hydrate review candidates"
        ),
        _preflight=({"run": "implement-test"}, {"provider": "fixture"}),
        _pr_facts={},
    ) == 0
    captured = capsys.readouterr()

    payload = json.loads(captured.out)
    assert payload["do"] == "stop"
    assert payload["queue"] == "empty"
    assert "begin_review_phase" not in captured.err
