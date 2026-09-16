"""GraphQL responses expose the cost of the request that produced them."""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


RATE_LIMIT = "rateLimit { cost remaining resetAt }"


def compact(query: str) -> str:
    return " ".join(query.split())


def test_every_read_query_requests_rate_limit():
    queries = [
        funnel.PROJECT_FIELDS_QUERY,
        funnel.REPO_QUERY,
        funnel.ITEM_QUERY,
        funnel.SUB_ISSUES,
    ]

    assert all(RATE_LIMIT in compact(query) for query in queries)


def test_option_lookup_requests_rate_limit(monkeypatch):
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        return {"node": {"options": [{"id": "status", "name": "Status"}]}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    assert funnel._option_id("field", "Status") == "status"
    assert RATE_LIMIT in compact(calls[0][0])


def test_gh_graphql_returns_rate_limit_alongside_existing_data(monkeypatch):
    calls = []
    response = {
        "data": {
            "user": {"login": "nateprich"},
            "rateLimit": {
                "cost": 2,
                "remaining": 3174,
                "resetAt": "2026-09-09T03:52:06Z",
            },
        }
    }

    def run(args, capture_output, text):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(response),
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)

    data = funnel.gh_graphql(funnel.PROJECT_FIELDS_QUERY, login="nateprich", number=2)

    assert data["user"] == {"login": "nateprich"}
    assert data["rateLimit"] == response["data"]["rateLimit"]
    query_arg = next(arg for arg in calls[0] if arg.startswith("query="))
    assert RATE_LIMIT in compact(query_arg.removeprefix("query="))


def test_brief_profile_records_graphql_operation_time_and_count(monkeypatch):
    response = {
        "data": {
            "user": {"projectV2": {"items": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}},
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
        }
    }

    def run(args, capture_output, text):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(response),
            stderr="",
        )

    timings = {}
    token = funnel._ACTIVE_BRIEF_TIMINGS.set(timings)
    monkeypatch.setattr(funnel, "_run_gh", run)
    try:
        funnel.gh_graphql(funnel.ITEM_QUERY, login="nateprich", number=2)
        funnel.gh_graphql(funnel.ITEM_QUERY, login="nateprich", number=2)
    finally:
        funnel._ACTIVE_BRIEF_TIMINGS.reset(token)

    assert timings["graphql.project_items"] >= 0
    assert timings["graphql.project_items.calls"] == 2


@pytest.mark.parametrize(
    "truncated",
    [
        SimpleNamespace(returncode=0, stdout='{"data":', stderr=""),
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "unexpected end of JSON input; GraphQL request ID "
                "DD41:627C:A3F5CB:EEDB4E:6AA66A60"
            ),
        ),
    ],
)
def test_truncated_graphql_response_retries_then_succeeds(
    monkeypatch, truncated
):
    funnel.reset_route_state()
    funnel.reset_api_usage()
    attempts = []
    sleeps = []
    responses = [
        truncated,
        SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"data": {"viewer": {"login": "n"}}}),
            stderr="",
        ),
    ]

    def run(args, **kwargs):
        attempts.append(args)
        return responses.pop(0)

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.time, "sleep", sleeps.append)

    assert funnel.gh_graphql("{viewer{login}}") == {
        "viewer": {"login": "n"}
    }
    assert len(attempts) == 2
    assert sleeps == [funnel.GRAPHQL_RETRY_DELAY_SECONDS]
    assert funnel._API_USAGE["graphql_calls"] == 2
    assert funnel.graphql_spend()["calls"] == 2


def test_non_transient_graphql_failure_does_not_retry(monkeypatch):
    funnel.reset_route_state()
    funnel.reset_api_usage()
    attempts = []

    def run(args, **kwargs):
        attempts.append(args)
        return SimpleNamespace(
            returncode=1, stdout="", stderr="permission denied"
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)

    with pytest.raises(funnel.GitHubError, match="permission denied"):
        funnel.gh_graphql("{viewer{login}}")

    assert len(attempts) == 1
    assert funnel._API_USAGE["graphql_calls"] == 1
    assert funnel.graphql_spend()["calls"] == 1


def test_graphql_error_preserves_the_partial_rate_limit_signal(monkeypatch):
    """A failed payload can still identify an exhausted shared pool."""
    funnel.reset_route_state()
    funnel.reset_api_usage()
    reset_at = "2026-09-16T05:30:32Z"
    response = {
        "data": {
            "rateLimit": {"cost": 0, "remaining": 0, "resetAt": reset_at},
        },
        "errors": [{"message": "API rate limit already exceeded"}],
    }

    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(response), stderr=""
        ),
    )

    with pytest.raises(funnel.GitHubError, match="rate limit"):
        funnel.gh_graphql("{viewer{login}}")

    assert funnel.graphql_spend()["remaining"] == 0
    assert funnel.graphql_spend()["reset_at"] == reset_at
    assert funnel.route_exhausted()["reset_at"] == reset_at
    funnel.reset_route_state()
