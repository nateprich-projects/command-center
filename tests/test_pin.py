"""Project-level pin writes stay explicit, reversible, and attributable."""

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


def project(number=42, *, item_id="project-item-42", parent=None):
    return Item(
        repo="owner/repo",
        number=number,
        title="A project",
        url="https://github.com/owner/repo/issues/{}".format(number),
        state="OPEN",
        status="Ready",
        item_id=item_id,
        parent=parent,
    )


def test_from_node_reads_pinned_option_and_defaults_to_unset():
    def node(pinned=None):
        return {
            "id": "project-item-42",
            "status": {"name": "Ready"},
            "class": {"name": "Improve"},
            "pinned": pinned or {},
            "content": {
                "number": 42,
                "title": "A project",
                "url": "https://github.com/owner/repo/issues/42",
                "state": "OPEN",
                "stateReason": None,
                "closedAt": None,
                "repository": {"nameWithOwner": "owner/repo"},
                "labels": {"nodes": []},
                "assignees": {"nodes": []},
                "parent": None,
                "subIssuesSummary": {"total": 0, "completed": 0},
                "timelineItems": {"nodes": []},
            },
        }

    assert funnel._from_node(node()).pinned is False
    assert funnel._from_node(node({"name": "Pinned"})).pinned is True


@pytest.mark.parametrize("verb", ["pin", "unpin"])
def test_pin_commands_are_dry_runs_without_yes(monkeypatch, capsys, verb):
    item = project()
    writes = []
    comments = []

    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: writes.append((query, variables)),
    )
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda args, **kwargs: comments.append(args),
    )

    command = funnel.cmd_pin if verb == "pin" else funnel.cmd_unpin
    assert command([item], NOW, item.ref, False) == 1

    output = capsys.readouterr().out
    assert "would {} {}".format(verb, item.ref) in output
    assert "Nothing was changed" in output
    assert writes == []
    assert comments == []


def test_pin_sets_the_single_select_and_posts_nate_provenance(monkeypatch):
    item = project()
    writes = []
    comments = []

    monkeypatch.setattr(
        funnel,
        "_option_id",
        lambda field_id, name: writes.append(("option", field_id, name))
        or "pinned-option",
    )
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda query, **variables: writes.append((query, variables)),
    )

    def run(args, capture_output, text=True):
        comments.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_pin(
        [item], NOW, item.ref, True, run="run-42", agent="codex"
    ) == 0

    assert writes == [
        ("option", funnel.PINNED_FIELD_ID, funnel.PINNED_OPTION),
        (funnel.SET_FIELD, {
            "project": funnel.PROJECT_ID,
            "item": item.item_id,
            "field": funnel.PINNED_FIELD_ID,
            "option": "pinned-option",
        }),
    ]
    posted = comments[0][-1]
    assert posted.startswith("**Pinned:** Nate decided to pin this project.")
    assert funnel.parse_provenance(posted) == {
        "agent": "codex",
        "at": NOW.isoformat(),
        "run": "run-42",
        "voice": "nate-relayed",
    }


def test_unpin_clears_the_value_and_posts_nate_provenance(monkeypatch):
    item = project()
    writes = []
    comments = []

    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda query, **variables: writes.append((query, variables)),
    )

    def run(args, capture_output, text=True):
        comments.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_unpin(
        [item], NOW, item.ref, True, run="run-43", agent="codex"
    ) == 0

    assert writes == [
        (funnel.CLEAR_FIELD, {
            "project": funnel.PROJECT_ID,
            "item": item.item_id,
            "field": funnel.PINNED_FIELD_ID,
        }),
    ]
    posted = comments[0][-1]
    assert posted.startswith("**Unpinned:** Nate decided to unpin this project.")
    assert funnel.parse_provenance(posted)["voice"] == "nate-relayed"


def test_pin_refuses_ticket_targets_before_writing(monkeypatch):
    ticket = project(parent="owner/repo#7")
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: pytest.fail("ticket pin must not write"),
    )

    with pytest.raises(funnel.GitHubError, match="only projects can be pinned"):
        funnel.cmd_pin([ticket], NOW, ticket.ref, True)
