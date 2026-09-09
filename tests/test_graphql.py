"""GraphQL responses expose the cost of the request that produced them."""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

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
