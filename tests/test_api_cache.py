"""Pin the narrow ``gh api --cache`` policy for non-gating REST reads."""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"


def _cached(command):
    return (
        "--cache" in command
        and command[command.index("--cache") + 1] == funnel.GH_API_CACHE_DURATION
    )


def test_cacheable_rest_reads_use_one_shared_cli_cache_duration():
    endpoint = "repos/{}/labels?per_page=100".format(REPO)

    assert funnel._gh_api_command(endpoint) == ["gh", "api", endpoint]
    assert funnel._gh_api_command(endpoint, cache=True) == [
        "gh", "api", endpoint, "--cache", funnel.GH_API_CACHE_DURATION,
    ]


def test_advisory_reads_cache_but_ci_and_claim_reads_stay_live(monkeypatch):
    """The #655 baseline was 42 calls and 47 GraphQL points.

    The cache is allowed to reduce repeated advisory REST work, but a stale CI
    or branch response could change queue eligibility or claim recovery, so
    those calls must not carry the cache flag.
    """
    calls = []

    def gh_json(*args):
        calls.append(args)
        endpoint = args[2]
        if endpoint.endswith("/actions/workflows"):
            return {"workflows": [{"path": ".github/workflows/ci.yml"}]}
        if endpoint.endswith("/labels?per_page=100"):
            return []
        if endpoint.endswith("/contents/.github/dependabot.yml"):
            return {"type": "file"}
        if endpoint.endswith("/git/matching-refs/heads/ticket?per_page=100"):
            return []
        raise AssertionError("unexpected endpoint: {}".format(endpoint))

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    readiness = funnel.member_repo_readiness(REPO)
    funnel.ticket_branch_index(REPO)

    assert readiness.ci_workflow is True
    workflow = next(call for call in calls if call[2].endswith("/actions/workflows"))
    labels = next(call for call in calls if call[2].endswith("/labels?per_page=100"))
    dependabot = next(
        call for call in calls
        if call[2].endswith("/contents/.github/dependabot.yml")
    )
    branches = next(
        call for call in calls
        if call[2].endswith("/git/matching-refs/heads/ticket?per_page=100")
    )

    assert not _cached(workflow)
    assert _cached(labels)
    assert _cached(dependabot)
    assert not _cached(branches)


def test_live_gate_and_claim_reads_see_updated_answers(monkeypatch):
    """A changing gate or claim answer cannot be hidden by a stale cache."""
    workflows = iter([
        {"workflows": [{"path": ".github/workflows/ci.yml"}]},
        {"workflows": []},
    ])
    branches = iter([
        [{"ref": "refs/heads/ticket/42"}],
        [],
    ])

    def gh_json(*args):
        endpoint = args[2]
        if endpoint.endswith("/actions/workflows"):
            assert not _cached(args)
            return next(workflows)
        if endpoint.endswith("/git/matching-refs/heads/ticket?per_page=100"):
            assert not _cached(args)
            return next(branches)
        if endpoint.endswith("/labels?per_page=100"):
            assert _cached(args)
            return []
        if endpoint.endswith("/contents/.github/dependabot.yml"):
            assert _cached(args)
            return None
        if endpoint.endswith("/contents/.github/dependabot.yaml"):
            assert _cached(args)
            return None
        raise AssertionError("unexpected endpoint: {}".format(endpoint))

    monkeypatch.setattr(funnel, "_gh_json", gh_json)

    first_readiness = funnel.member_repo_readiness(REPO)
    second_readiness = funnel.member_repo_readiness(REPO)
    first_branches, _ = funnel.ticket_branch_index(REPO)
    second_branches, _ = funnel.ticket_branch_index(REPO)

    assert first_readiness.ci_workflow is True
    assert second_readiness.ci_workflow is False
    assert first_branches == {"{}#42".format(REPO)}
    assert second_branches == set()


def test_heartbeat_health_read_uses_the_non_gating_cache(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(funnel, "_run_gh", run)

    assert funnel.gh_branch_exists() is True
    assert len(calls) == 1
    assert _cached(calls[0])


def test_two_logical_advisory_reads_have_one_live_cli_attempt(monkeypatch):
    """The measured fixture saving is two requests to one live request (50%)."""
    endpoint = "repos/{}/labels?per_page=100".format(REPO)
    cache = {}
    calls = []
    live = []

    def run(command, **kwargs):
        calls.append(command)
        key = command[2]
        if _cached(command) and key in cache:
            payload = cache[key]
        else:
            payload = {"labels": ["advisory"]}
            live.append(key)
            if _cached(command):
                cache[key] = payload
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(funnel, "_run_gh", run)

    assert funnel._gh_api_json(endpoint, cache=True) == {"labels": ["advisory"]}
    assert funnel._gh_api_json(endpoint, cache=True) == {"labels": ["advisory"]}

    assert len(calls) == 2
    assert len(live) == 1
