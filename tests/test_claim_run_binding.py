"""A direct claim must be recognizable by the same run at finish time."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import heartbeat
import funnel
from engine import implement


REPO = "owner/repo"


def test_funnel_claim_then_finish_recognizes_the_claiming_run(
        monkeypatch, capsys):
    now = datetime(2026, 9, 27, 18, 30, 12, 345678, tzinfo=timezone.utc)
    parent = funnel.Item(
        repo=REPO, number=7, title="parent", url="https://example.invalid/7",
        state="OPEN", status="Building", klass="Broken",
        status_since=now, item_id="PVTI_parent",
    )
    target = funnel.Item(
        repo=REPO, number=42, title="ticket", url="https://example.invalid/42",
        state="OPEN", parent=parent.ref, item_id="PVTI_ticket",
        status_since=now,
    )
    writes = []

    def write_lock(item, value):
        writes.append((item.ref, value))
        item.in_motion_since = datetime.fromisoformat(
            value.removesuffix("Z") + "+00:00"
        )

    monkeypatch.setattr(funnel, "write_lock", write_lock)
    monkeypatch.setattr(funnel, "_begin_parent", lambda *args: None)

    assert funnel.cmd_claim([parent, target], now, target.ref) == 0
    claim = json.loads(capsys.readouterr().out)
    claim_timestamp = claim["claim_timestamp"]
    assert claim_timestamp == "2026-09-27T18:30:12.345678Z"
    assert claim["ref"] == target.ref
    assert writes[-1][0] == target.ref

    monkeypatch.setattr(
        funnel, "load_project_items_by_refs", lambda refs: [target],
    )
    monkeypatch.setattr(heartbeat, "read_github_strict", lambda agent: [])

    # This is finish-ticket's ownership gate after the same session claimed
    # the ticket: a direct claim has no heartbeat binding to resolve its run.
    assert implement._claim_state(
        target.ref, "run-42", "codex", claim_timestamp=claim_timestamp,
    )[0] == "owned"
