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


def test_capture_item_add_failure_surfaces_the_error(monkeypatch, capsys):
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

    assert funnel.main([
        "capture", "An idea", "--repo", "owner/repo", "--origin", "nate-relayed",
    ], _items=[]) == 2

    assert len(calls) == 2
    assert calls[0][-2:] == ("--label", "needs-shaping")
    assert "not in project" in capsys.readouterr().err


def test_capture_retries_transient_item_add_then_succeeds(monkeypatch):
    calls = []
    sleeps = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/123\n",
                stderr="",
            )
        if args[1:3] == ["project", "item-add"] and len(
                [call for call in calls if call[1:3] == ("project", "item-add")]
        ) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Something went wrong while executing your query",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "project-item-123"}),
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.time, "sleep", sleeps.append)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "ideas-option")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})

    assert funnel.cmd_capture(
        [], NOW, "An idea", "A note", "owner/repo",
        run="capture-run", agent="codex", origin="nate-relayed",
    ) == 0

    assert len([call for call in calls if call[1:3] == ("project", "item-add")]) == 2
    assert sleeps == [funnel.CAPTURE_ITEM_ADD_RETRY_DELAY_SECONDS]


def test_capture_retry_exhaustion_surfaces_the_last_transient_error(monkeypatch):
    calls = []
    errors = [
        "Something went wrong while executing your query (first)",
        "Post https://api.github.com/graphql: context deadline exceeded (second)",
        "Something went wrong while executing your query (last)",
    ]

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/123\n",
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr=errors.pop(0))

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.time, "sleep", lambda seconds: None)

    with pytest.raises(funnel.GitHubError, match="last"):
        funnel.cmd_capture(
            [], NOW, "An idea", "A note", "owner/repo",
            run="capture-run", agent="codex", origin="nate-relayed",
        )

    assert len([call for call in calls if call[1:3] == ("project", "item-add")]) == 3


def test_capture_non_transient_item_add_failure_does_not_retry(monkeypatch):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/123\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Could not resolve to a Project item",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)

    with pytest.raises(funnel.GitHubError, match="Could not resolve"):
        funnel.cmd_capture(
            [], NOW, "An idea", "A note", "owner/repo",
            run="capture-run", agent="codex", origin="nate-relayed",
        )

    assert len([call for call in calls if call[1:3] == ("project", "item-add")]) == 1


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
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "project-item-42"}),
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "ideas-option")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})

    assert funnel.cmd_capture(
        [], NOW, "An idea", "Raw note", "owner/repo",
        run="capture-run", agent="claude", origin=origin, klass=klass,
    ) == 0

    body = calls[0][calls[0].index("--body") + 1]
    assert funnel.parse_origin(body)["voice"] == origin
    assert funnel.parse_provenance(body)["voice"] == "agent"


def test_capture_records_caused_by_refs(monkeypatch):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/42\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "project-item-42"}),
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "ideas-option")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})

    refs = ["owner/repo#7", "https://github.com/owner/repo/pull/8", "owner/repo#7"]
    assert funnel.cmd_capture(
        [], NOW, "An idea", "Raw note", "owner/repo",
        run="capture-run", agent="claude", origin="nate-relayed",
        caused_by=refs,
    ) == 0

    body = calls[0][calls[0].index("--body") + 1]
    assert funnel.parse_caused_by(body) == refs[:2]
    assert funnel.CAUSED_BY_MARKER in body


def test_capture_cli_passes_repeated_caused_by_refs(monkeypatch):
    received = {}

    def capture(*args):
        received["args"] = args
        return 0

    monkeypatch.setattr(funnel, "cmd_capture", capture)

    assert funnel.main([
        "capture", "An idea", "--repo", "owner/repo",
        "--origin", "nate-relayed", "--caused-by", "#7",
        "--caused-by", "owner/repo#8",
    ], _items=[]) == 0

    assert received["args"][-1] == ["#7", "owner/repo#8"]


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
