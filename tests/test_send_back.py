"""The explicit, reasoned return from Shaped to Ideas."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "nateprich/beta"
REASON = "The plan needs a clearer treatment of migration risk"
INSTRUCTION = "Nate asked me to send this plan back to Ideas for that revision."
NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)


def shaped_item(**overrides):
    fields = dict(
        repo=REPO,
        number=42,
        title="A shaped project",
        url="https://github.com/nateprich/beta/issues/42",
        state="OPEN",
        body="# Existing plan\n\nKeep this body unchanged.",
        status="Ready",  # The loaded view can be stale; the fresh read is Shaped.
        klass="Broken",
        origin="Nate",
        risk="escalated",
        needs="human",
        item_id="project-item-42",
    )
    fields.update(overrides)
    return funnel.Item(**fields)


def stub_github(monkeypatch, item, fresh):
    events = []
    fresh_reads = []

    def load_fresh(refs):
        fresh_reads.append(tuple(refs))
        return [fresh] if refs == [item.ref] else None

    def graphql(query, **variables):
        events.append(("graphql", query, variables))
        if query == funnel.SET_FIELD:
            return {
                "updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": fresh.item_id}
                }
            }
        pytest.fail("unexpected GraphQL request")

    def run(args, capture_output, text=True):
        events.append(("gh", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    monkeypatch.setattr(funnel, "load_project_items_by_refs", load_fresh)
    monkeypatch.setattr(funnel, "live_issue_state", lambda current: "OPEN")
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "ideas-option")
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    return events, fresh_reads


def test_send_back_reproduction_moves_fresh_shaped_item_and_posts_reason(monkeypatch):
    item = shaped_item()
    fresh = shaped_item(status="Shaped")
    events, fresh_reads = stub_github(monkeypatch, item, fresh)

    assert funnel.main([
        "send-back", item.ref, "--reason", REASON, "--instruction", INSTRUCTION,
        "--yes", "--run", "run-42", "--agent", "codex",
    ]) == 0

    writes = [event for event in events if event[0] == "graphql"]
    comments = [
        event for event in events
        if event[0] == "gh" and event[1][:3] == ("gh", "issue", "comment")
    ]
    assert fresh_reads == [(item.ref,)]
    assert len(writes) == 1
    assert writes[0][2] == {
        "project": funnel.PROJECT_ID,
        "item": item.item_id,
        "field": funnel.STATUS_FIELD_ID,
        "option": "ideas-option",
    }
    assert len(comments) == 1
    assert comments[0][1][:6] == (
        "gh", "issue", "comment", "42", "--repo", REPO,
    )
    posted = comments[0][1][-1]
    assert funnel._visible_comment(posted) == "Send-back to Ideas: " + REASON
    provenance = funnel.parse_provenance(posted)
    assert provenance["voice"] == "nate-relayed"
    assert provenance["instruction"] == INSTRUCTION
    assert item.status == "Ideas"
    assert (item.klass, item.origin, item.risk, item.needs) == (
        "Broken", "Nate", "escalated", "human",
    )
    assert item.body == "# Existing plan\n\nKeep this body unchanged."


def test_send_back_dry_run_prints_both_writes_and_changes_nothing(
    monkeypatch, capsys
):
    item = shaped_item(status="Shaped")
    fresh = shaped_item(status="Shaped")
    events, fresh_reads = stub_github(monkeypatch, item, fresh)

    assert funnel.main([
        "send-back", item.ref, "--reason", REASON, "--instruction", INSTRUCTION,
    ]) == 1

    assert fresh_reads == [(item.ref,)]
    assert events == []
    assert item.status == "Shaped"
    output = capsys.readouterr().out
    assert "would move {} from Shaped to Ideas".format(item.ref) in output
    assert "Send-back to Ideas: " + REASON in output
    assert "Nothing was changed" in output


@pytest.mark.parametrize("fresh_status", ["Ideas", "Ready"])
def test_send_back_refuses_every_fresh_status_other_than_shaped(
    monkeypatch, capsys, fresh_status
):
    item = shaped_item(status="Shaped")
    fresh = shaped_item(status=fresh_status)
    events, fresh_reads = stub_github(monkeypatch, item, fresh)

    assert funnel.main([
        "send-back", item.ref, "--reason", REASON,
        "--instruction", INSTRUCTION, "--yes",
    ]) == 2

    assert fresh_reads == [(item.ref,)]
    assert events == []
    assert item.status == "Shaped"
    assert "fresh Status is {}".format(fresh_status) in capsys.readouterr().err


def test_send_back_requires_nate_instruction_before_loading(monkeypatch, capsys):
    loads = []
    monkeypatch.setattr(funnel, "load_items", lambda: loads.append("loaded") or [])

    with pytest.raises(SystemExit) as exc:
        funnel.main(["send-back", "42", "--reason", REASON, "--yes"])

    assert exc.value.code == 2
    assert loads == []
    assert "--instruction" in capsys.readouterr().err


def test_send_back_refuses_an_empty_reason_before_loading(monkeypatch, capsys):
    loads = []
    monkeypatch.setattr(funnel, "load_items", lambda: loads.append("loaded") or [])

    with pytest.raises(SystemExit) as exc:
        funnel.main([
            "send-back", "42", "--reason", "", "--instruction", INSTRUCTION,
            "--yes",
        ])

    assert exc.value.code == 2
    assert loads == []
    assert "non-empty send-back reason" in capsys.readouterr().err
