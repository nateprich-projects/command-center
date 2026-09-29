"""A Bug that happens moves to Broken, and the issue carries why (#1847)."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 28, 20, 0, 0, tzinfo=timezone.utc)
EVIDENCE = "CI run https://github.com/owner/repo/actions/runs/77 failed on main"


def project(number=42, *, klass="Bug", state="OPEN"):
    return Item(
        repo="owner/repo",
        number=number,
        title="A latent defect",
        url="https://github.com/owner/repo/issues/{}".format(number),
        state=state,
        status="Ready",
        item_id="project-item-{}".format(number),
        klass=klass,
    )


def doubles(monkeypatch, comment_returncode=0):
    """Record every comment post and field write, in the order they happen."""
    calls = []

    def run(args, capture_output, text=True):
        calls.append(("comment", tuple(args)))
        return SimpleNamespace(
            returncode=comment_returncode, stdout="", stderr="comment refused")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: calls.append(("graphql", query, variables)),
    )
    monkeypatch.setattr(
        funnel, "_option_id", lambda field_id, name: "{}-option".format(name)
    )
    return calls


def test_promote_posts_the_observed_comment_then_sets_class_broken(
        monkeypatch, capsys):
    item = project()
    calls = doubles(monkeypatch)

    assert funnel.main(
        ["promote", item.ref, "--observed", EVIDENCE,
         "--run", "run-42", "--agent", "claude"],
        _items=[item],
    ) == 0

    assert [call[0] for call in calls] == ["comment", "graphql"]
    comment = calls[0][1]
    assert comment[:5] == ("gh", "issue", "comment", "42", "--repo")
    body = comment[comment.index("--body") + 1]
    assert body.startswith("**Observed:** " + EVIDENCE + "\n")
    provenance = funnel.parse_provenance(body)
    assert provenance["voice"] == "agent"
    assert provenance["run"] == "run-42"
    assert provenance["agent"] == "claude"
    assert calls[1][1:] == (funnel.SET_FIELD, {
        "project": funnel.PROJECT_ID,
        "item": "project-item-42",
        "field": funnel.CLASS_FIELD_ID,
        "option": "Broken-option",
    })
    assert item.klass == "Broken"
    assert "owner/repo#42 → Broken (was Bug)" in capsys.readouterr().out


@pytest.mark.parametrize("klass", ["Broken", "Improve", "Maintenance", None])
def test_promote_refuses_an_item_that_is_not_a_bug(monkeypatch, klass):
    item = project(klass=klass)
    calls = doubles(monkeypatch)

    with pytest.raises(funnel.GitHubError) as exc:
        funnel.cmd_promote([item], NOW, item.ref, EVIDENCE)

    assert "promote moves a Bug to Broken" in str(exc.value)
    assert "Class {}".format(klass or "unset") in str(exc.value)
    assert calls == []
    assert item.klass == klass


def test_promote_refuses_a_closed_bug_and_points_at_a_new_capture(
        monkeypatch):
    item = project(state="CLOSED")
    calls = doubles(monkeypatch)

    with pytest.raises(funnel.GitHubError) as exc:
        funnel.cmd_promote([item], NOW, item.ref, EVIDENCE)

    assert "is closed" in str(exc.value)
    assert "--caused-by owner/repo#42" in str(exc.value)
    assert calls == []


@pytest.mark.parametrize("observed", ["", "  \n "])
def test_promote_refuses_blank_evidence(monkeypatch, observed):
    item = project()
    calls = doubles(monkeypatch)

    with pytest.raises(funnel.GitHubError, match="requires --observed"):
        funnel.cmd_promote([item], NOW, item.ref, observed)

    assert calls == []


def test_promote_without_observed_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(
        funnel, "load_items", lambda: pytest.fail("GitHub should not be loaded")
    )

    with pytest.raises(SystemExit) as exc:
        funnel.main(["promote", "42"])

    assert exc.value.code == 2
    assert "--observed" in capsys.readouterr().err


def test_a_failed_comment_leaves_the_bug_unpromoted(monkeypatch):
    """The evidence is written first, so the issue never reads Broken
    without it: a comment that fails stops before the Class write."""
    item = project()
    calls = doubles(monkeypatch, comment_returncode=1)

    with pytest.raises(funnel.GitHubError, match="comment refused"):
        funnel.cmd_promote([item], NOW, item.ref, EVIDENCE)

    assert [call[0] for call in calls] == ["comment"]
    assert item.klass == "Bug"
