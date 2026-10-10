"""Class options: Improve rename, new phases and legacy block (#2407, #2425).

Pure over fixtures and names: Improve reads as Implement, Curate, Describe,
Hypothesize, Test and Implement accept fresh assignments, and legacy New and
Replace refuse while their open items remain.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def item(**values):
    defaults = dict(
        repo="owner/repo", number=1, title="Work", url="u", state="OPEN",
        status="Ready",
    )
    defaults.update(values)
    return funnel.Item(**defaults)


def test_ladder_holds_phases_implement_and_legacy_in_order():
    assert funnel.LADDER == [
        "Investigate", "Broken", "Maintenance", "Curate", "Describe",
        "Hypothesize", "Test", "Implement", "New", "Replace", "Bug",
    ]


def test_legacy_is_new_and_replace_only():
    assert funnel.LEGACY_CLASSES == frozenset({"New", "Replace"})


def test_assignable_is_everything_but_legacy():
    assert funnel.ASSIGNABLE_CLASSES == frozenset(
        klass for klass in funnel.LADDER
        if klass not in ("New", "Replace"))
    for klass in ("Curate", "Describe", "Hypothesize", "Test", "Implement"):
        assert klass in funnel.ASSIGNABLE_CLASSES


def test_normalize_maps_improve_to_implement():
    assert funnel.normalize_class("Improve") == "Implement"
    assert funnel.normalize_class("Implement") == "Implement"
    assert funnel.normalize_class("Curate") == "Curate"
    assert funnel.normalize_class("New") == "New"
    assert funnel.normalize_class(None) is None
    assert funnel.normalize_class("nonsense") == "nonsense"


@pytest.mark.parametrize("klass", [
    "Investigate", "Broken", "Maintenance", "Curate", "Describe",
    "Hypothesize", "Test", "Implement", "Bug", "Improve",
])
def test_assignable_names_accept(klass):
    assert funnel.is_assignable_class(klass) is True
    assert funnel.class_assignment_refusal(klass) is None


@pytest.mark.parametrize("klass", ["New", "Replace"])
def test_legacy_names_refuse_without_items(klass):
    assert funnel.is_assignable_class(klass) is False
    refusal = funnel.class_assignment_refusal(klass)
    assert refusal is not None and "legacy" in refusal
    assert klass in refusal


def test_legacy_refusal_names_open_holders():
    rows = [
        item(number=1, klass="New"),
        item(number=2, klass="New"),
        item(number=3, klass="New", state="CLOSED"),
        item(number=4, klass="Implement"),
    ]
    refusal = funnel.class_assignment_refusal("New", rows)
    assert refusal is not None and "2 open" in refusal
    assert "New" in refusal
    assert funnel.class_assignment_refusal("Replace", rows) is not None


def test_unknown_and_missing_names_refuse():
    assert funnel.is_assignable_class(None) is False
    assert funnel.is_assignable_class("nonsense") is False
    assert funnel.class_assignment_refusal(None) is not None
    assert funnel.class_assignment_refusal("nonsense") is not None


@pytest.mark.parametrize("klass", [
    "Curate", "Describe", "Hypothesize", "Test", "Implement",
])
def test_proposals_accept_the_phases(klass):
    body = "# Plan\n\nDo it.\n\nProposed class: {}\n".format(klass)
    assert funnel.proposed_class_for_approval(body) == (
        klass, "Proposed class: {}".format(klass))


@pytest.mark.parametrize("klass", ["New", "Replace"])
def test_proposals_refuse_legacy(klass):
    body = "# Plan\n\nDo it.\n\nProposed class: {}\n".format(klass)
    assert funnel.proposed_class_for_approval(body) is None


def test_proposals_accept_improve_as_the_old_name():
    body = "# Plan\n\nDo it.\n\nProposed class: Improve\n"
    assert funnel.proposed_class_for_approval(body) == (
        "Improve", "Proposed class: Improve")


def test_improve_ranks_as_implement():
    assert (funnel.ladder_index("Improve")
            == funnel.ladder_index("Implement"))
    ranks = [funnel.ladder_index(klass) for klass in funnel.LADDER]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(funnel.LADDER)


def test_improve_and_legacy_count_as_held():
    assert funnel.needs_class(item(status="Ready", klass="Improve")) is False
    assert funnel.needs_class(item(status="Ready", klass="New")) is False
    assert funnel.needs_class(
        item(status="Ready", klass="Replace")) is False
    assert funnel.needs_class(
        item(status="Ready", klass="Implement")) is False
    assert funnel.needs_class(item(status="Ready", klass="Curate")) is False
    assert funnel.needs_class(item(status="Ready", klass=None)) is True
    assert funnel.needs_class(item(status="Ready", klass="nonsense")) is True


def test_effective_class_reads_improve_as_implement():
    parent = item(number=1, klass="Improve")
    ticket = item(number=2, parent=parent.ref)
    by_ref = {parent.ref: parent, ticket.ref: ticket}
    assert funnel.effective_class(parent, by_ref) == "Implement"
    assert funnel.effective_class(ticket, by_ref) == "Implement"


def test_capture_refuses_legacy_while_open_items_remain(monkeypatch):
    rows = [item(number=1, klass="New")]
    monkeypatch.setattr(
        funnel, "resolve_repo", lambda repo: pytest.fail("repo was resolved"))

    with pytest.raises(funnel.GitHubError, match="legacy"):
        funnel.cmd_capture(
            rows, funnel.datetime.now(funnel.timezone.utc),
            "An idea", "Raw note", "owner/repo",
            origin="agent", klass="New")
    with pytest.raises(funnel.GitHubError, match="legacy"):
        funnel.cmd_capture(
            rows, funnel.datetime.now(funnel.timezone.utc),
            "An idea", "Raw note", "owner/repo",
            origin="agent", klass="Replace")


@pytest.mark.parametrize("klass", [
    "Curate", "Describe", "Hypothesize", "Test", "Implement",
])
def test_capture_accepts_the_phases(monkeypatch, klass):
    writes = []

    def run(args, capture_output, text=True):
        if args[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/42\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"id": "project-item-42"}',
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        funnel, "_option_id",
        lambda field_id, name: writes.append(name) or "opt-{}".format(name))
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        funnel, "write_project_select", lambda *args: None)

    assert funnel.cmd_capture(
        [], funnel.datetime.now(funnel.timezone.utc),
        "An idea", "Raw note", "owner/repo",
        run="capture-run", agent="claude", origin="agent", klass=klass,
    ) == 0
    assert klass in writes


def test_capture_writes_improve_as_implement(monkeypatch):
    writes = []

    def run(args, capture_output, text=True):
        if args[:3] == ["gh", "issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/42\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='{"id": "project-item-42"}',
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        funnel, "_option_id",
        lambda field_id, name: writes.append(name) or "opt-{}".format(name))
    monkeypatch.setattr(funnel, "gh_graphql", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        funnel, "write_project_select", lambda *args: None)

    assert funnel.cmd_capture(
        [], funnel.datetime.now(funnel.timezone.utc),
        "An idea", "Raw note", "owner/repo",
        run="capture-run", agent="claude", origin="agent", klass="Improve",
    ) == 0
    assert "Implement" in writes
    assert "Improve" not in writes
