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
        run="capture-run", agent="claude", origin="agent",
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


@pytest.mark.parametrize("origin", ["nate-relayed", "agent"])
def test_capture_records_each_explicit_origin(monkeypatch, origin):
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
        run="capture-run", agent="claude", origin=origin,
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


def test_shaped_preserves_plan_bytes_above_agent_stamp(tmp_path, monkeypatch):
    plan = "# Plan\n\nKeep this trailing newline exactly.\n"
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


def test_shaped_reads_the_plan_from_stdin_when_asked(monkeypatch):
    """Muse runs with --disable-write and cannot write a plan file (#366)."""
    import io

    plan = "# Plan\n\nPiped, not written.\n"
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
