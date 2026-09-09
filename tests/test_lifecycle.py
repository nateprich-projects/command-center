"""The documented project lifecycle remains reachable end to end.

The fix for #343 restored the producer of ``Building``. This fixture walks the
whole path so a test cannot exercise a stage's reader while its writer is gone.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def lifecycle():
    project = Item(
        repo="nateprich/beta",
        number=1,
        title="Ship the widget",
        url="https://github.com/nateprich/beta/issues/1",
        state="OPEN",
        body="## Needs you\n\nWhich repository should this use?\n",
        status="Shaped",
        klass="New",
        status_since=NOW,
        item_id="project-item-1",
        children_total=1,
    )
    ticket = Item(
        repo=project.repo,
        number=2,
        title="Wire the widget to the bus",
        url="https://github.com/nateprich/beta/issues/2",
        state="OPEN",
        parent=project.ref,
        item_id="ticket-item-2",
    )
    return project, ticket, [project, ticket]


def test_shaped_project_reaches_done_through_every_documented_transition(
    monkeypatch, lifecycle
):
    project, ticket, items = lifecycle
    status_writes = []
    lock_writes = []

    options = {
        "opt-Ready": "Ready",
        "opt-Building": "Building",
        "opt-Done": "Done",
    }

    def graphql(query, **variables):
        assert query == funnel.SET_FIELD
        option = variables["option"]
        status_writes.append((variables["item"], options[option]))
        if variables["item"] == project.item_id:
            project.status = options[option]
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": project.item_id}}}

    def write_lock(item, value):
        lock_writes.append((item.ref, value))
        if value:
            item.in_motion_since = NOW

    def run(args, capture_output, text=True):
        if args[1:3] == ["issue", "close"]:
            project.state = "CLOSED"
            project.state_reason = "COMPLETED"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt-" + name)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    # Accepting now reports drift (#57, PR #377), which reads three histories
    # from GitHub. This walk is about the transitions, not drift, so hand it
    # an empty, drift-free fact set rather than faking three query shapes
    # (#412). `gh_graphql` above still refuses anything but SET_FIELD.
    monkeypatch.setattr(
        funnel, "fetch_drift_facts",
        lambda item: funnel.DriftFacts(
            ready_at=None, building_at=None, plan_edit_times=(),
            review_verdicts=(), regression_pr_numbers=(),
            ticket_created_at=(),
        ),
    )
    monkeypatch.setattr(funnel, "write_lock", write_lock)
    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.gate_question(project) == "Is the plan good?"
    assert funnel.cmd_answer(items, NOW, "approve", project.ref, True) == 0
    assert project.status == "Ready"
    assert funnel.startable(items) == [ticket]

    assert funnel.cmd_claim(items, NOW, ticket.ref) == 0
    assert project.status == "Building"
    assert lock_writes == [(ticket.ref, "2026-09-09T12:00:00Z")]

    ticket.state = "CLOSED"
    project.children_done = 1
    assert funnel.gate_question(project) == "Accept it?"
    assert funnel.cmd_answer(items, NOW, "accept", project.ref, True) == 0
    assert project.status == "Done"
    assert project.state == "CLOSED"
    assert project.state_reason == "COMPLETED"
    assert status_writes == [
        (project.item_id, "Ready"),
        (project.item_id, "Building"),
        (project.item_id, "Done"),
    ]
