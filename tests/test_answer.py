"""The accept gate's completion output, refusal guard, and drift report."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "project_items.json"
REPO = "owner/repo"


def fixture_items():
    return [
        item for item in (funnel._from_node(node) for node in json.loads(FIXTURE.read_text()))
        if item
    ]


def project(*, children_total=1, children_done=1, klass="New"):
    return Item(
        repo=REPO,
        number=1,
        title="Project",
        url="https://example.invalid/1",
        state="OPEN",
        status="Building",
        klass=klass,
        item_id="project-id",
        children_total=children_total,
        children_done=children_done,
    )


def test_parking_candidates_are_oldest_open_projects_before_building():
    items = fixture_items()

    assert [item.ref for item in funnel.parking_candidates(items)] == [
        "nateprich/beta#11",
        "nateprich/beta#12",
    ]


def test_accept_prints_ages_and_copy_ready_parking_commands(monkeypatch, capsys):
    items = fixture_items()
    target = next(item for item in items if item.ref == "nateprich-projects/alpha#10")
    target.item_id = "project-item-10"
    writes = []

    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "done-option")
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda query, **variables: writes.append((query, variables)) or {},
    )

    closed = []

    def run(args, capture_output, text=True):
        closed.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_answer(
        items, NOW, "accept", "nateprich-projects/alpha#10", True
    ) == 0

    output = capsys.readouterr().out
    assert "Longest-waiting unstarted projects" in output
    assert "nateprich/beta#11" in output
    assert "Rotate the expiring cert (4 days)" in output
    assert 'funnel park nateprich/beta#11 --reason "<why>"' in output
    assert "Tidy the config loader (3 days)" in output
    assert 'funnel park nateprich/beta#12 --reason "<why>"' in output
    assert 'funnel park nateprich-projects/alpha#10' not in output
    assert closed == [[
        "gh", "issue", "close", "10", "--repo", "nateprich-projects/alpha",
        "--reason", "completed",
    ]]
    assert len(writes) == 1


def test_accept_still_refuses_while_children_are_open(monkeypatch):
    items = fixture_items()
    target = next(item for item in items if item.ref == "nateprich-projects/alpha#10")
    target.item_id = "project-item-10"
    target.children_done = 1

    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: pytest.fail("accept wrote before checking children"),
    )

    with pytest.raises(funnel.GitHubError, match="still has open tickets"):
        funnel.cmd_answer(items, NOW, "accept", target.ref, True)


def test_accept_dry_run_reports_each_drift_signal(monkeypatch, capsys):
    item = project()
    drift = [funnel.DRIFT_PLAN_EDIT, funnel.DRIFT_LATE_TICKET]
    monkeypatch.setattr(funnel, "drift_since_approval", lambda target: drift)

    assert funnel.cmd_answer([item], NOW, "accept", item.ref, False) == 1

    output = capsys.readouterr().out
    assert "drift since approval:" in output
    for signal in drift:
        assert signal in output
    assert "would move {} from Building to Done".format(item.ref) in output


def test_accept_without_drift_stays_quiet_about_drift(monkeypatch, capsys):
    item = project()
    monkeypatch.setattr(funnel, "drift_since_approval", lambda target: [])

    assert funnel.cmd_answer([item], NOW, "accept", item.ref, False) == 1

    output = capsys.readouterr().out
    assert "drift since approval:" not in output
    assert "would move {} from Building to Done".format(item.ref) in output


def test_accept_executed_run_reports_drift_before_moving(monkeypatch, capsys):
    item = project()
    drift = [funnel.DRIFT_REJECTED_REVIEW]
    calls = []
    monkeypatch.setattr(funnel, "drift_since_approval", lambda target: drift)
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "done-option")
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    assert funnel.cmd_answer([item], NOW, "accept", item.ref, True) == 0

    output = capsys.readouterr().out
    assert output.index("drift since approval:") < output.index("→ Done")
    assert drift[0] in output
    assert len(calls) == 1


def test_accept_refuses_open_children_before_reading_drift(monkeypatch):
    item = project(children_done=0)
    monkeypatch.setattr(
        funnel,
        "drift_since_approval",
        lambda target: pytest.fail("drift must not be read for a refusal"),
    )

    with pytest.raises(funnel.GitHubError, match="still has open tickets"):
        funnel.cmd_answer([item], NOW, "accept", item.ref, False)


def test_accept_refuses_no_tickets_without_override_before_reading_drift(
    monkeypatch,
):
    item = project(children_total=0, children_done=0)
    monkeypatch.setattr(
        funnel,
        "drift_since_approval",
        lambda target: pytest.fail("drift must not be read for a refusal"),
    )

    with pytest.raises(funnel.GitHubError, match="no tickets"):
        funnel.cmd_answer([item], NOW, "accept", item.ref, False)
