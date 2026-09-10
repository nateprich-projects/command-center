"""Issue-body writers stamp their agent-authored content."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def test_capture_stamps_the_created_body_as_agent(monkeypatch):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/42\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="not in project")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_capture(
        [], NOW, "An idea", "Raw note", "owner/repo",
        run="capture-run", agent="claude", origin="agent", klass="Broken",
    ) == 0

    body = calls[0][calls[0].index("--body") + 1]
    assert body.startswith("Raw note")
    assert funnel.parse_provenance(body) == {
        "agent": "claude",
        "at": NOW.isoformat(),
        "run": "capture-run",
        "voice": "agent",
    }
    assert funnel.parse_origin(body) == {
        "agent": "claude",
        "at": NOW.isoformat(),
        "run": "capture-run",
        "voice": "agent",
    }
    assert calls[0][-2:] == ("--label", "needs-shaping")


@pytest.mark.parametrize(
    ("origin", "klass"),
    [("nate-relayed", None), ("agent", "Improve")],
)
def test_capture_records_each_explicit_origin(monkeypatch, origin, klass):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/42\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="not in project")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_capture(
        [], NOW, "An idea", "Raw note", "owner/repo",
        run="capture-run", agent="claude", origin=origin, klass=klass,
    ) == 0

    body = calls[0][calls[0].index("--body") + 1]
    assert funnel.parse_origin(body)["voice"] == origin
    assert funnel.parse_provenance(body)["voice"] == "agent"


def test_capture_requires_an_explicit_origin_before_resolving_repo(monkeypatch):
    monkeypatch.setattr(
        funnel, "resolve_repo", lambda repo: pytest.fail("repo was resolved")
    )

    with pytest.raises(funnel.GitHubError, match="explicit --origin"):
        funnel.cmd_capture([], NOW, "An idea", "Raw note", "owner/repo")


def test_agent_capture_requires_a_class_before_resolving_repo(monkeypatch):
    monkeypatch.setattr(
        funnel, "resolve_repo", lambda repo: pytest.fail("repo was resolved")
    )

    with pytest.raises(funnel.GitHubError, match="--class.*--origin agent"):
        funnel.cmd_capture(
            [], NOW, "An idea", "Raw note", "owner/repo", origin="agent"
        )


def test_shaped_preserves_plan_bytes_above_agent_stamp(tmp_path, monkeypatch):
    plan = "# Plan\n\nProposed class: New\n\nKeep this trailing newline exactly.\n"
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(plan)
    item = Item(
        repo="owner/repo", number=42, title="An idea",
        url="https://github.com/owner/repo/issues/42", state="OPEN",
        status="Ideas", item_id="project-item-42",
    )
    calls = []

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        return {"node": {"options": [{"id": "shaped-option", "name": "Shaped"}]}}

    def run(args, capture_output, text=True):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file),
        run="shape-run", agent="claude",
    ) == 0

    edit = calls[0][1]
    assert edit[:6] == (
        "gh", "issue", "edit", "42", "--repo", "owner/repo",
    )
    body = edit[-1]
    assert body.startswith(plan)
    assert funnel.parse_provenance(body) == {
        "agent": "claude",
        "at": NOW.isoformat(),
        "run": "shape-run",
        "voice": "agent",
    }


def test_shaped_prints_advisory_overlap_candidates(tmp_path, monkeypatch, capsys):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "Proposed class: New\n\nTouch `funnel.py` and follow #91.\n"
    )
    item = Item(
        repo="owner/repo", number=42, title="An idea",
        url="https://github.com/owner/repo/issues/42", state="OPEN",
        status="Ideas", item_id="project-item-42",
    )
    other = Item(
        repo="owner/repo", number=89, title="Existing plan",
        url="https://github.com/owner/repo/issues/89", state="OPEN",
        body="Update `funnel.py` and cite #91.", status="Ready",
    )

    def graphql(query, **variables):
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        return {"node": {"options": [{"id": "shaped-option", "name": "Shaped"}]}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    assert funnel.cmd_shaped(
        [item, other], NOW, item.ref, str(plan_file),
        run="shape-run", agent="claude",
    ) == 0

    output = capsys.readouterr().out
    assert "--- plan overlap candidates (advisory) ---" in output
    assert "Read each candidate and record the conclusion in the plan:" in output
    assert "owner/repo#42 and owner/repo#89 both touch `funnel.py`" in output
    assert "owner/repo#42 and owner/repo#89 both reference #91" in output


def test_shaped_without_overlap_still_succeeds_and_reports_none(tmp_path, monkeypatch,
                                                                 capsys):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "Proposed class: New\n\nA plan with no shared signals.\n"
    )
    item = Item(
        repo="owner/repo", number=42, title="An idea",
        url="https://github.com/owner/repo/issues/42", state="OPEN",
        status="Ideas", item_id="project-item-42",
    )

    def graphql(query, **variables):
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        return {"node": {"options": [{"id": "shaped-option", "name": "Shaped"}]}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file),
        run="shape-run", agent="claude",
    ) == 0

    output = capsys.readouterr().out
    assert "--- plan overlap candidates (advisory) ---" in output
    assert "  none found" in output


def test_shaped_records_each_authority_signal_on_a_ready_marker(
    tmp_path, monkeypatch, capsys
):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("""
Proposed class: Broken

The check refuses a self-approval and does not add a decision he must answer
on the happy path. The plan also records why unattended approvals are visible.
It changes the gate's question and who answers it.

## Needs you

Exposure: nothing outstanding. no new surface.
Gates: nothing outstanding. no gate change.
Scope and priority: nothing outstanding. bounded.
Preference: nothing outstanding. no taste choice.
""")
    item = Item(
        repo="owner/repo", number=42, title="An idea",
        url="https://github.com/owner/repo/issues/42", state="OPEN",
        status="Ideas", klass="Broken", item_id="project-item-42",
        body=funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="claude"
        ),
    )
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        return {"node": {"options": [
            {"id": "shaped-option", "name": "Shaped"},
            {"id": "ready-option", "name": "Ready"},
        ]}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    def run(args, capture_output, text=True):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file),
        run="shape-run", agent="claude",
    ) == 0

    status_write = next(variables for query, variables in calls
                        if query == funnel.SET_FIELD)
    assert status_write["option"] == "ready-option"
    marker_comments = [
        call for call in calls
        if call[0] == "run" and call[1][:3] == ("gh", "issue", "comment")
    ]
    assert len(marker_comments) == 1
    marker_body = marker_comments[0][1][marker_comments[0][1].index("--body") + 1]
    assert funnel.parse_self_approval(marker_body) == (
        "plan declares nothing open; no escalated risk; authority signals: "
        "gate authority, unattended authority"
    )
    output = capsys.readouterr().out
    assert "--- self-approval advisory ---" in output
    assert "self-approval refused" not in output
    assert (
        "gate authority: changes a gate's question, answer, or owner"
        in output
    )
    assert (
        "unattended authority: changes what an agent may do unattended"
        in output
    )
    assert "owner/repo#42 → Ready" in output
    assert "It now waits on you: is the plan good?" not in output


def test_shaped_all_clear_plan_without_origin_stays_at_shaped(
    tmp_path, monkeypatch, capsys
):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("""
Proposed class: Broken

The plan discusses a gate and a Status field as subject matter, but changes
neither and grants no additional authority to an agent.

## Needs you

Nothing.
""")
    item = Item(
        repo="owner/repo", number=42, title="An idea",
        url="https://github.com/owner/repo/issues/42", state="OPEN",
        status="Ideas", klass="Broken", item_id="project-item-42",
    )
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        return {"node": {"options": [{"id": "shaped-option", "name": "Shaped"}]}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file),
        run="shape-run", agent="claude",
    ) == 0

    status_write = next(variables for query, variables in calls
                        if query == funnel.SET_FIELD)
    assert status_write["option"] == "shaped-option"
    output = capsys.readouterr().out
    assert "self-approval refused" not in output
    assert "owner/repo#42 → Shaped" in output
    assert "origin is Nate's" in output
    assert "It now waits on you: is the plan good?" in output


def test_capture_always_labels_the_issue_and_reports_it(monkeypatch, capsys):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(args)
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/nateprich-projects/command-center/issues/123\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "project-item-123"}),
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "ideas-option")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})

    assert funnel.cmd_capture(
        [], NOW, "An idea", "A note", funnel.REPO,
        run="capture-run", agent="codex", origin="nate-relayed",
    ) == 0

    assert calls[0][-2:] == ["--label", "needs-shaping"]
    assert capsys.readouterr().out == (
        "https://github.com/nateprich-projects/command-center/issues/123"
        "  → Ideas (needs-shaping) in nateprich-projects/command-center\n"
    )


def test_agent_capture_sets_class_after_project_add(monkeypatch):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(("run", tuple(args)))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/123\n",
                stderr="",
            )
        if args[1:3] == ["project", "item-add"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"id": "project-item-123"}),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel, "_option_id", lambda field_id, name: "{}-option".format(name)
    )

    assert funnel.cmd_capture(
        [], NOW, "An observed defect", "A note", "owner/repo",
        run="capture-run", agent="codex", origin="agent", klass="Broken",
    ) == 0

    add_index = next(
        index for index, call in enumerate(calls)
        if call == ("run", (
            "gh", "project", "item-add", str(funnel.PROJECT_NUMBER),
            "--owner", funnel.PROJECT_OWNER, "--url",
            "https://github.com/owner/repo/issues/123", "--format", "json",
        ))
    )
    class_index = next(
        index for index, call in enumerate(calls)
        if call[0] == "graphql"
        and call[2].get("field") == funnel.CLASS_FIELD_ID
    )
    assert class_index > add_index
    assert calls[class_index][2]["option"] == "Broken-option"


def test_nate_relayed_capture_without_class_leaves_class_unset(monkeypatch):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(("run", tuple(args)))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/123\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "project-item-123"}),
            stderr="",
        )

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "option")

    assert funnel.cmd_capture(
        [], NOW, "A Nate idea", "A note", "owner/repo",
        run="capture-run", agent="codex", origin="nate-relayed",
    ) == 0

    assert not any(
        call[0] == "graphql" and call[2].get("field") == funnel.CLASS_FIELD_ID
        for call in calls
    )


def test_capture_flag_is_rejected_before_github_is_loaded(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main(["capture", "An idea", "--origin", "agent",
                     "--needs-shaping"])

    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_capture_origin_is_required_before_github_is_loaded(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main(["capture", "An idea"])

    assert exc.value.code == 2
    assert "--origin" in capsys.readouterr().err


def test_capture_agent_class_is_required_before_github_is_loaded(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main(["capture", "An idea", "--origin", "agent"])

    assert exc.value.code == 2
    assert "--class is required with --origin agent" in capsys.readouterr().err


def test_shaped_reads_the_plan_from_stdin_when_asked(monkeypatch):
    """Muse runs with --disable-write and cannot write a plan file (#366)."""
    import io

    plan = "# Plan\n\nProposed class: New\n\nPiped, not written.\n"
    item = Item(
        repo="owner/repo", number=43, title="An idea",
        url="https://github.com/owner/repo/issues/43", state="OPEN",
        status="Ideas", item_id="project-item-43",
    )
    calls = []

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        return {"node": {"options": [{"id": "shaped-option", "name": "Shaped"}]}}

    def run(args, capture_output, text=True):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.sys, "stdin", io.StringIO(plan))

    assert funnel.cmd_shaped([item], NOW, item.ref, "-",
                             run="shape-run", agent="muse") == 0

    edit = calls[0][1]
    body = edit[edit.index("--body") + 1]
    assert body.startswith(plan)


def test_shaped_refuses_an_unclassed_agent_origin_before_any_write(
    tmp_path, monkeypatch
):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n")
    item = Item(
        repo="owner/repo", number=45, title="An observed idea",
        url="https://github.com/owner/repo/issues/45", state="OPEN",
        status="Ideas", body=funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="muse"
        ), item_id="project-item-45",
    )
    calls = []

    def run(args, **kwargs):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    with pytest.raises(funnel.GitHubError, match="agent-origin.*--class"):
        funnel.cmd_shaped(
            [item], NOW, item.ref, str(plan_file),
            run="shape-run", agent="muse",
        )

    assert calls == []


def test_shaped_class_flag_is_forwarded_by_the_cli(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(funnel, "load_items", lambda: [])
    monkeypatch.setattr(
        funnel,
        "cmd_shaped",
        lambda *args: calls.append(args) or 0,
    )

    assert funnel.main([
        "shaped", "owner/repo#45", "--plan", str(tmp_path / "plan.md"),
        "--class", "Broken",
    ]) == 0
    assert calls[0][-1] == "Broken"


def test_shaped_fills_an_unclassed_agent_class_before_status(
    tmp_path, monkeypatch
):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n\n## Needs you\n\nChoose a direction.\n")
    item = Item(
        repo="owner/repo", number=46, title="An observed idea",
        url="https://github.com/owner/repo/issues/46", state="OPEN",
        status="Ideas", body=funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="muse"
        ), item_id="project-item-46",
    )
    calls = []

    def run(args, **kwargs):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {"ok": True}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel, "_option_id", lambda field_id, name: "{}-option".format(name)
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file),
        run="shape-run", agent="muse", klass="Improve",
    ) == 0

    status_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert [call[2]["field"] for call in status_writes] == [
        funnel.CLASS_FIELD_ID, funnel.STATUS_FIELD_ID,
    ]
    assert status_writes[0][2]["option"] == "Improve-option"
    assert status_writes[1][2]["option"] == "Shaped-option"


@pytest.mark.parametrize("origin", ["nate-relayed", None])
def test_shaped_refuses_an_unclassed_nate_origin_without_a_proposal(
    tmp_path, monkeypatch, origin
):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Plan\n\n## Needs you\n\nChoose a direction.\n")
    body = (
        funnel.origin_block(origin, at=NOW, run="capture-run", agent="claude")
        if origin is not None else "Legacy capture."
    )
    item = Item(
        repo="owner/repo", number=47, title="A Nate idea",
        url="https://github.com/owner/repo/issues/47", state="OPEN",
        status="Ideas", body=body, item_id="project-item-47",
    )
    calls = []

    def run(args, **kwargs):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    with pytest.raises(funnel.GitHubError, match="Proposed class:"):
        funnel.cmd_shaped([item], NOW, item.ref, str(plan_file))

    assert calls == []


def test_shaped_leaves_nate_origin_class_unset_when_plan_proposes_one(
    tmp_path, monkeypatch
):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plan\n\nProposed class: Improve\n\n"
        "## Needs you\n\nChoose a direction.\n"
    )
    item = Item(
        repo="owner/repo", number=48, title="A Nate idea",
        url="https://github.com/owner/repo/issues/48", state="OPEN",
        status="Ideas", body=funnel.origin_block(
            "nate-relayed", at=NOW, run="capture-run", agent="claude"
        ), item_id="project-item-48",
    )
    calls = []

    def run(args, **kwargs):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {"ok": True}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel, "_option_id", lambda field_id, name: "{}-option".format(name)
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file),
        run="shape-run", agent="claude",
    ) == 0

    status_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert [call[2]["field"] for call in status_writes] == [funnel.STATUS_FIELD_ID]
    item.status = "Shaped"
    assert funnel.needs_class(item)


def _shaped_status_fixture(
    monkeypatch, plan_file, plan, status_option, **item_kwargs
):
    plan_file.write_text(plan)
    item = Item(
        repo="owner/repo", number=44, title="An idea",
        url="https://github.com/owner/repo/issues/44", state="OPEN",
        status="Ideas", item_id="project-item-44", **item_kwargs
    )
    calls = []

    def run(args, capture_output, text=True):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        return {"ok": True}

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(
        funnel, "_option_id", lambda field_id, name: status_option[name]
    )
    return item, calls


@pytest.mark.parametrize(
    ("klass", "origin", "extra", "expected"),
    (
        (
            "New", "agent", "",
            "class New is not self-approvable",
        ),
        (
            "Broken", "nate-relayed", "",
            "origin is Nate's",
        ),
        (
            "Broken", "agent", "Risk: escalated — destructive\n",
            "escalated risk (declared: destructive)",
        ),
    ),
)
def test_shaped_holds_when_a_self_approval_condition_fails(
    tmp_path, monkeypatch, capsys, klass, origin, extra, expected
):
    plan_file = tmp_path / "plan.md"
    plan = (
        "# Plan\n\nProposed class: {}\n\n{}"
        "## Needs you\n"
        "Exposure: nothing outstanding. no new surface.\n"
        "Gates: nothing outstanding. no gate change.\n"
        "Scope and priority: nothing outstanding. bounded.\n"
        "Preference: nothing outstanding. no taste choice.\n"
    ).format(klass, extra)
    item, calls = _shaped_status_fixture(
        monkeypatch,
        plan_file,
        plan,
        {"Shaped": "shaped-option"},
        klass=klass,
        body=funnel.origin_block(
            origin, at=NOW, run="capture-run", agent="claude"
        ),
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file), run="shape-run", agent="claude"
    ) == 0

    status_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert status_writes[0][2]["option"] == "shaped-option"
    assert not [
        call for call in calls
        if call[0] == "run" and call[1][:3] == ("gh", "issue", "comment")
    ]
    assert expected in capsys.readouterr().out


def test_investigate_agent_plan_can_self_approve(tmp_path, monkeypatch, capsys):
    plan_file = tmp_path / "plan.md"
    plan = (
        "# Plan\n\nProposed class: Investigate\n\n"
        "## Needs you\n"
        "Exposure: nothing outstanding. no new surface.\n"
        "Gates: nothing outstanding. no gate change.\n"
        "Scope and priority: nothing outstanding. bounded.\n"
        "Preference: nothing outstanding. no taste choice.\n"
    )
    item, calls = _shaped_status_fixture(
        monkeypatch,
        plan_file,
        plan,
        {"Ready": "ready-option"},
        klass="Investigate",
        body=funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="claude"
        ),
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file), run="shape-run", agent="claude"
    ) == 0

    status_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert status_writes[0][2]["option"] == "ready-option"
    marker_comments = [
        call for call in calls
        if call[0] == "run" and call[1][:3] == ("gh", "issue", "comment")
    ]
    assert len(marker_comments) == 1
    marker_body = marker_comments[0][1][marker_comments[0][1].index("--body") + 1]
    assert funnel.parse_self_approval(marker_body) == (
        "plan declares nothing open; no escalated risk"
    )
    assert "owner/repo#44 → Ready" in capsys.readouterr().out


def test_shaped_advances_a_plan_that_declares_nothing_open(
    tmp_path, monkeypatch, capsys
):
    plan_file = tmp_path / "plan.md"
    plan = "# Plan\n\nProposed class: Broken\n\n## Needs you\nNothing.\n"
    item, calls = _shaped_status_fixture(
        monkeypatch, plan_file, plan, {"Ready": "ready-option"},
        klass="Broken",
        body=funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="claude"
        ),
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file), run="shape-run", agent="claude"
    ) == 0

    status_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert status_writes[0][2]["option"] == "ready-option"
    marker_comments = [
        call for call in calls
        if call[0] == "run" and call[1][:3] == ("gh", "issue", "comment")
    ]
    assert len(marker_comments) == 1
    marker_body = marker_comments[0][1][marker_comments[0][1].index("--body") + 1]
    assert funnel.parse_self_approval(marker_body) == (
        "plan declares nothing open; no escalated risk"
    )
    output = capsys.readouterr().out
    assert "advanced to Ready: plan declares nothing open" in output


@pytest.mark.parametrize(
    ("plan", "reason"),
    (
        ("# Plan\n\nProposed class: Broken\n\n## Needs you\nWhich repository should this use?\n",
         "plan has an open question"),
        ("# Plan\n\nProposed class: Broken\n\n## Decided\nUse the existing repository.\n",
         "plan has no ## Needs you section"),
    ),
)
def test_shaped_holds_when_the_plan_needs_nate(
    tmp_path, monkeypatch, capsys, plan, reason
):
    plan_file = tmp_path / "plan.md"
    item, calls = _shaped_status_fixture(
        monkeypatch, plan_file, plan, {"Shaped": "shaped-option"},
        klass="Broken",
        body=funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="claude"
        ),
    )

    assert funnel.cmd_shaped(
        [item], NOW, item.ref, str(plan_file), run="shape-run", agent="claude"
    ) == 0

    status_writes = [
        call for call in calls
        if call[0] == "graphql" and call[1] == funnel.SET_FIELD
    ]
    assert status_writes[0][2]["option"] == "shaped-option"
    output = capsys.readouterr().out
    assert "held at Shaped: {}".format(reason) in output
