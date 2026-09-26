"""The project gates' output, refusal guards, and durable records."""

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


def shaped_project(body, *, klass=None):
    return Item(
        repo=REPO,
        number=2,
        title="Shaped project",
        url="https://example.invalid/2",
        state="OPEN",
        body=body,
        status="Shaped",
        klass=klass,
        item_id="shaped-project-id",
    )


def stub_approve_writes(monkeypatch, item):
    calls = []

    def option_id(field, name):
        calls.append(("option", field, name))
        return "option-{}".format(name)

    def graphql(query, **variables):
        calls.append(("graphql", query, dict(variables)))
        if variables.get("field") == funnel.STATUS_FIELD_ID:
            item.status = variables["option"].removeprefix("option-")
        return {}

    def run(args, capture_output, text=True):
        calls.append(("run", list(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "_option_id", option_id)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_heartbeat_context", lambda run, agent: (run, agent))
    monkeypatch.setattr(funnel.subprocess, "run", run)
    return calls


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

    monkeypatch.setattr(funnel, "drift_since_approval", lambda item: [])
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


def test_approve_adopts_one_exact_class_before_status_and_records_source(
    monkeypatch, capsys
):
    item = shaped_project("# Plan\n\nProposed class: Improve\n\nDo the work.\n")
    calls = stub_approve_writes(monkeypatch, item)

    assert funnel.cmd_answer([item], NOW, "approve", item.ref, True) == 0

    writes = [
        call[2] for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert [variables["field"] for variables in writes] == [
        funnel.CLASS_FIELD_ID, funnel.STATUS_FIELD_ID,
    ]
    assert item.klass == "Improve"
    assert item.status == "Ready"

    comment = next(
        call[1] for call in calls
        if call[0] == "run" and call[1][1:3] == ["issue", "comment"]
    )
    body = comment[comment.index("--body") + 1]
    assert "**Class adopted:** Improve" in body
    assert "Source line: `Proposed class: Improve`" in body
    assert funnel.CLASS_ADOPTION_OVERRIDE_NOTE in body

    output = capsys.readouterr().out
    assert "adopted Class Improve" in output
    assert "Proposed class: Improve" in output
    assert funnel.CLASS_ADOPTION_OVERRIDE_NOTE in output


@pytest.mark.parametrize(
    ("verb", "status", "expected"),
    [
        ("approve", "Shaped", "Ready"),
        ("accept", "Building", "Done"),
    ],
)
def test_gate_answers_record_instruction_and_move_to_next_stage(
    monkeypatch, verb, status, expected
):
    item = Item(
        repo=REPO,
        number=3,
        title="Project at a gate",
        url="https://example.invalid/3",
        state="OPEN",
        status=status,
        item_id="ready-project-id",
        children_total=1,
        children_done=1,
    )
    calls = stub_approve_writes(monkeypatch, item)
    instruction = "Answer this gate now.\nUse the approved plan verbatim."
    monkeypatch.setattr(funnel, "drift_since_approval", lambda target: [])

    assert funnel.main([
        verb, item.ref, "--yes", "--instruction", instruction,
    ], _items=[item]) == 0

    assert item.status == expected
    comment = next(
        call[1] for call in calls
        if call[0] == "run" and call[1][1:3] == ["issue", "comment"]
    )
    posted = comment[comment.index("--body") + 1]
    provenance = funnel.parse_provenance(posted)
    assert provenance["voice"] == "nate-relayed"
    assert provenance["instruction"] == instruction


@pytest.mark.parametrize(
    "body",
    [
        "# Plan\n\nNo proposal.\n",
        "# Plan\n\nProposed class:\n",
        "# Plan\n\nProposed class: choose a class.\n",
        "# Plan\n\nProposed class: Improve or New\n",
        "# Plan\n\nThe proposed class is Improve.\n",
    ],
)
def test_approve_does_not_infer_class_from_missing_fuzzy_or_ambiguous_text(
    monkeypatch, body
):
    item = shaped_project(body)
    calls = stub_approve_writes(monkeypatch, item)

    assert funnel.cmd_answer([item], NOW, "approve", item.ref, True) == 0

    writes = [
        call[2] for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert [variables["field"] for variables in writes] == [funnel.STATUS_FIELD_ID]
    assert item.klass is None
    assert not any(
        call[0] == "run" and call[1][1:3] == ["issue", "comment"]
        for call in calls
    )


def test_class_adoption_dry_run_does_not_advance_the_shaped_gate(monkeypatch, capsys):
    item = shaped_project("Proposed class: Improve\n")
    calls = stub_approve_writes(monkeypatch, item)

    assert funnel.cmd_answer([item], NOW, "approve", item.ref, False) == 1

    assert item.klass is None
    assert item.status == "Shaped"
    assert not any(kind == "graphql" for kind, *_ in calls)
    output = capsys.readouterr().out
    assert "would adopt Class Improve" in output
    assert "Nothing was changed" in output


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
