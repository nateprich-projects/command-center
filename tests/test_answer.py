"""The accept gate's completion output and refusal guard."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "project_items.json"


def fixture_items():
    return [
        item for item in (funnel._from_node(node) for node in json.loads(FIXTURE.read_text()))
        if item
    ]


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
