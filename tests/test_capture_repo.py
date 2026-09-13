"""A capture lands in the run's own repo without a flag (#668, ticket #669)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402

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
