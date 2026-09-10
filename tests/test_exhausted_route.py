"""An exhausted GraphQL route is refused, not retried into (#429, ticket #502).

Before this change a funnel command that hit the exhausted route kept issuing
one failing `gh` call per ticket or PR it examined. The measurement below
replays that shape: thirty lookups against an exhausted route cost thirty
attempts before, and one attempt plus twenty-nine refusals after.
"""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

EXHAUSTED = "GraphQL: API rate limit already exceeded for user ID 263208503."


@pytest.fixture(autouse=True)
def _fresh_route():
    funnel.reset_route_state()
    funnel.reset_api_usage()
    yield
    funnel.reset_route_state()
    funnel.reset_api_usage()


def _exhausted_subprocess(monkeypatch, attempts):
    def run(command, **kwargs):
        attempts.append(list(command))
        return SimpleNamespace(returncode=1, stdout="", stderr=EXHAUSTED)
    monkeypatch.setattr(funnel.subprocess, "run", run)


def test_one_exhausted_answer_stops_every_later_graphql_call(monkeypatch):
    attempts = []
    _exhausted_subprocess(monkeypatch, attempts)

    with pytest.raises(funnel.GitHubError) as first:
        funnel.gh_graphql("{ viewer { login } }")
    assert "rate limit" in str(first.value).lower()

    with pytest.raises(funnel.GitHubError) as second:
        funnel.gh_graphql("{ viewer { login } }")
    assert "exhausted earlier in this run" in str(second.value)
    assert "not attempted" in str(second.value)
    assert len(attempts) == 1
    assert funnel.route_exhausted() is not None


def test_cli_lookups_on_the_same_route_are_refused_too(monkeypatch):
    attempts = []
    _exhausted_subprocess(monkeypatch, attempts)

    assert funnel._gh_json("gh", "pr", "list", "--repo", "o/r") is None  # the one real attempt
    with pytest.raises(funnel.GitHubError) as refused:
        funnel._gh_json("gh", "issue", "view", "7", "--repo", "o/r")
    assert "gh issue view 7" in str(refused.value)
    assert len(attempts) == 1


def test_rest_calls_keep_going_after_graphql_is_exhausted(monkeypatch):
    attempts = []
    _exhausted_subprocess(monkeypatch, attempts)
    funnel._gh_json("gh", "pr", "list", "--repo", "o/r")  # marks the route

    def rest_ok(command, **kwargs):
        attempts.append(list(command))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    monkeypatch.setattr(funnel.subprocess, "run", rest_ok)

    assert funnel._gh_json("gh", "api", "repos/o/r/issues/7") == {}
    assert attempts[-1][:3] == ["gh", "api", "repos/o/r/issues/7"]


def test_a_transient_failure_is_not_exhaustion(monkeypatch):
    attempts = []

    def flaky(command, **kwargs):
        attempts.append(list(command))
        if len(attempts) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer")
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setattr(funnel.subprocess, "run", flaky)

    assert funnel._gh_json("gh", "pr", "list") is None
    assert funnel._gh_json("gh", "pr", "list") == []
    assert funnel.route_exhausted() is None
    assert len(attempts) == 2


def test_zero_remaining_in_a_successful_answer_is_the_same_signal(monkeypatch):
    attempts = []

    def run(command, **kwargs):
        attempts.append(list(command))
        payload = {"data": {"viewer": {"login": "n"},
                            "rateLimit": {"cost": 1, "remaining": 0,
                                          "resetAt": "2026-09-10T07:00:45Z"}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.gh_graphql("{ viewer { login } }")["viewer"]["login"] == "n"
    with pytest.raises(funnel.GitHubError) as refused:
        funnel.gh_graphql("{ viewer { login } }")
    assert "resets 2026-09-10T07:00:45Z" in str(refused.value)
    assert len(attempts) == 1


def test_thirty_lookups_cost_one_attempt_instead_of_thirty(monkeypatch):
    """The measurement quoted in the PR: 30 -> 1."""
    attempts = []
    _exhausted_subprocess(monkeypatch, attempts)

    refused = 0
    for number in range(1, 31):
        try:
            funnel._gh_json("gh", "issue", "view", str(number), "--repo", "o/r")
        except funnel.GitHubError:
            refused += 1

    usage = funnel.api_usage()
    assert len(attempts) == 1
    assert refused == 29
    assert usage["cli_calls"] == 1 and usage["refused_exhausted"] == 29


def test_the_reserve_gate_is_untouched_by_the_breaker(monkeypatch):
    """`_reserve_verdict` still reads what the responses reported."""
    monkeypatch.setattr(funnel, "graphql_spend",
                        lambda: {"calls": 1, "cost": 1, "remaining": 4000,
                                 "reset_at": None})
    assert funnel._reserve_verdict("review") is None
