"""Repository selection for commands that write to a member repo."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
ONE = "owner/one"
TWO = "owner/two"


def test_explicit_repo_skips_membership_lookup(monkeypatch):
    def unexpected_lookup():
        raise AssertionError("explicit --repo must not inspect membership")

    monkeypatch.setattr(funnel, "member_repos", unexpected_lookup)

    assert funnel.resolve_repo("owner/explicit") == "owner/explicit"


def test_single_member_repo_is_selected(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [ONE])

    assert funnel.resolve_repo(None) == ONE


def test_duplicate_membership_rows_still_describe_one_repo(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [ONE, ONE])

    assert funnel.resolve_repo(None) == ONE


def test_ambiguous_membership_names_every_repo_and_requires_flag(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [TWO, ONE])

    with pytest.raises(funnel.GitHubError) as exc:
        funnel.resolve_repo(None)

    message = str(exc.value)
    assert ONE in message
    assert TWO in message
    assert "--repo is required" in message


def test_no_members_fail_closed(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [])

    with pytest.raises(funnel.GitHubError, match="--repo is required"):
        funnel.resolve_repo(None)


def test_each_command_refuses_ambiguous_repo_before_side_effects(monkeypatch):
    monkeypatch.setattr(funnel, "member_repos", lambda: [ONE, TWO])
    monkeypatch.setattr(
        funnel, "subprocess", SimpleNamespace(run=lambda *args, **kwargs: (
            pytest.fail("ambiguous repo reached a subprocess")
        )),
    )
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: pytest.fail("ambiguous repo reached GitHub JSON"),
    )
    monkeypatch.setattr(
        funnel, "merge_blockers",
        lambda *args: pytest.fail("ambiguous repo reached merge checks"),
    )

    calls = [
        lambda: funnel.cmd_capture([], NOW, "idea", None, None,
                                    origin="agent", klass="Broken"),
        lambda: funnel.cmd_review(None, 7, "approved", "green", [], None),
        lambda: funnel.cmd_merge([], NOW, None, 7, False),
    ]
    for call in calls:
        with pytest.raises(funnel.GitHubError) as exc:
            call()
        assert ONE in str(exc.value)
        assert TWO in str(exc.value)
        assert "--repo is required" in str(exc.value)


def test_capture_uses_the_only_member_and_reports_it(monkeypatch, capsys):
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/one/issues/7\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"id":"project-item-7"}',
            stderr="",
        )

    monkeypatch.setattr(funnel, "member_repos", lambda: [ONE])
    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "ideas")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})

    assert funnel.cmd_capture(
        [], NOW, "An idea", "A note", None, run="run", agent="codex",
        origin="agent", klass="Broken"
    ) == 0

    assert "--repo" in calls[0]
    assert calls[0][calls[0].index("--repo") + 1] == ONE
    assert "in {}".format(ONE) in capsys.readouterr().out


def test_review_uses_the_only_member_and_reports_it(monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(funnel, "member_repos", lambda: [ONE])
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"state": "OPEN", "headRefOid": "abc123"},
    )

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_review(
        None, 7, "approved", "green", [], None, run="run", agent="codex"
    ) == 0

    assert calls[0][calls[0].index("--repo") + 1] == ONE
    assert "in {}".format(ONE) in capsys.readouterr().out


def test_merge_uses_the_only_member_and_reports_it(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "member_repos", lambda: [ONE])
    monkeypatch.setattr(funnel, "merge_blockers", lambda *args: [])

    assert funnel.cmd_merge([], NOW, None, 7, False) == 0

    assert "in {}".format(ONE) in capsys.readouterr().out


def test_all_commands_honor_explicit_repo_with_multiple_members(monkeypatch, capsys):
    explicit = "owner/explicit"
    monkeypatch.setattr(
        funnel, "member_repos",
        lambda: pytest.fail("explicit --repo must bypass membership count"),
    )

    capture_calls = []

    def capture_run(args, capture_output, text=True):
        capture_calls.append(tuple(args))
        if args[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/explicit/issues/7\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"id":"project-item-7"}',
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", capture_run)
    monkeypatch.setattr(funnel, "_option_id", lambda field_id, name: "ideas")
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})
    assert funnel.cmd_capture(
        [], NOW, "An idea", "A note", explicit, run="run", agent="codex",
        origin="agent", klass="Broken"
    ) == 0
    assert explicit in capsys.readouterr().out

    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"state": "OPEN", "headRefOid": "abc123"},
    )
    assert funnel.cmd_review(
        explicit, 7, "approved", "green", [], None,
        run="run", agent="codex",
    ) == 0
    assert explicit in capsys.readouterr().out

    monkeypatch.setattr(funnel, "merge_blockers", lambda *args: [])
    assert funnel.cmd_merge([], NOW, explicit, 7, False) == 0
    assert explicit in capsys.readouterr().out
