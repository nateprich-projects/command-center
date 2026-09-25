"""A capture lands in the run's own repo without a flag (#668, ticket #669)."""

from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from engine import shape  # noqa: E402

FF = "nateprich-projects/FF-Weekly-Start-Sit"
CC = "nateprich-projects/command-center"


def _bound(monkeypatch, run, do, work, repo=None, members=(CC, FF)):
    rec = {"run": run, "agent": "codex", "phase": "bind", "ts": 1, "do": do, "work": work}
    if repo:
        rec["repo"] = repo
    monkeypatch.setattr(heartbeat, "read", lambda agent: [
        {"run": run, "agent": "codex", "phase": "start", "ts": 0}, rec])
    monkeypatch.setattr(funnel, "member_repos", lambda: list(members))
    monkeypatch.setattr(funnel, "_heartbeat_context", lambda r, a: (r or run, a or "codex"))


def _run_shape_apply(monkeypatch, *args):
    applied = []
    monkeypatch.setattr(shape, "validate_answer", lambda data: data)
    monkeypatch.setattr(funnel, "load_items", lambda: [])
    monkeypatch.setattr(
        shape, "apply_shape",
        lambda items, now, ref, answer, **kwargs: applied.append(ref) or 0,
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"plan_markdown": "# Plan"})))
    code = shape.apply_main([*args, "--answer", "-"])
    return code, applied


def test_the_flag_wins_over_the_binding(monkeypatch):
    _bound(monkeypatch, "r1", "ticket", FF + "#29")
    assert funnel.capture_repo(CC, "r1", "codex") == CC


def test_a_run_bound_to_a_ticket_captures_in_that_tickets_repo(monkeypatch):
    _bound(monkeypatch, "r1", "ticket", FF + "#29")
    assert funnel.capture_repo(None, "r1", "codex") == FF


def test_a_review_run_captures_in_the_prs_repo(monkeypatch):
    _bound(monkeypatch, "r1", "review", "60", repo=FF)
    assert funnel.capture_repo(None, "r1", "muse") == FF


def test_no_binding_and_one_member_is_that_member(monkeypatch):
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(funnel, "member_repos", lambda: [CC])
    monkeypatch.setattr(funnel, "_heartbeat_context", lambda r, a: (None, None))
    assert funnel.capture_repo(None, None, None) == CC


def test_no_binding_and_two_members_still_refuses(monkeypatch):
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(funnel, "member_repos", lambda: [CC, FF])
    monkeypatch.setattr(funnel, "_heartbeat_context", lambda r, a: (None, None))
    with pytest.raises(funnel.GitHubError) as refused:
        funnel.capture_repo(None, None, None)
    assert "--repo is required" in str(refused.value)


def test_begin_binds_the_reviews_repo(monkeypatch):
    bound = []
    monkeypatch.setattr(heartbeat, "record_binding",
                        lambda agent, run, do, work, repo=None: bound.append((do, work, repo)) or "pushed")
    out = {"run": "r2", "do": "review", "work": {"pr": 60, "repo": FF, "ref": FF + "#31"}}
    funnel._bind_run("muse", out)
    assert bound == [("review", "60", FF)]
    assert out["bound"] == {"do": "review", "work": "60", "repo": FF}
    assert heartbeat.bindings([{"run": "r2", "phase": "bind", "ts": 1, "do": "review",
                                "work": "60", "repo": FF}])["r2"]["repo"] == FF


def test_shaped_repo_flag_wins_over_the_binding(monkeypatch):
    _bound(monkeypatch, "r1", "ticket", FF + "#29")
    code, applied = _run_shape_apply(monkeypatch, "42", "--repo", CC)
    assert code == 0
    assert applied == [CC + "#42"]


def test_shaped_uses_a_bound_ticket_repo(monkeypatch):
    _bound(monkeypatch, "r1", "ticket", FF + "#29")
    code, applied = _run_shape_apply(
        monkeypatch, "42", "--run", "r1", "--agent", "codex")
    assert code == 0
    assert applied == [FF + "#42"]


def test_shaped_uses_a_bound_review_repo(monkeypatch):
    _bound(monkeypatch, "r1", "review", "60", repo=FF)
    code, applied = _run_shape_apply(
        monkeypatch, "42", "--run", "r1", "--agent", "codex")
    assert code == 0
    assert applied == [FF + "#42"]


def test_shaped_without_a_binding_uses_the_sole_member_repo(monkeypatch):
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(funnel, "member_repos", lambda: [CC])
    monkeypatch.setattr(funnel, "_heartbeat_context", lambda r, a: (None, None))
    code, applied = _run_shape_apply(monkeypatch, "42")
    assert code == 0
    assert applied == [CC + "#42"]


def test_shaped_without_a_binding_refuses_multiple_member_repos(
        monkeypatch, capsys):
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(funnel, "member_repos", lambda: [CC, FF])
    monkeypatch.setattr(funnel, "_heartbeat_context", lambda r, a: (None, None))
    code, applied = _run_shape_apply(monkeypatch, "42")
    assert code == 1
    assert applied == []
    assert "--repo is required" in capsys.readouterr().err
