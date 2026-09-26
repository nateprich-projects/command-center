"""Startup/account CI stops are distinct from real test failures."""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from engine import review  # noqa: E402


def test_zero_completed_steps_is_could_not_run():
    check = {
        "name": "tests",
        "conclusion": "FAILURE",
        "completed_steps": 0,
    }

    assert funnel.ci_rollup_state([check]) == funnel.CI_COULD_NOT_RUN
    assert funnel.ci_could_not_run_reason([check]) == (
        "failure with zero completed steps"
    )


def test_billing_annotation_is_carried_as_could_not_run_evidence():
    check = {
        "name": "tests",
        "conclusion": "FAILURE",
        "annotations": [{
            "title": "startup_failure",
            "message": (
                "Recent account payments have failed or your spending "
                "limit needs to be increased."
            ),
        }],
    }

    reason = funnel.ci_could_not_run_reason([check])
    assert funnel.ci_rollup_state([check]) == funnel.CI_COULD_NOT_RUN
    assert reason == check["annotations"][0]["message"]


def test_genuine_failure_stays_red():
    check = {
        "name": "tests",
        "conclusion": "FAILURE",
        "steps": [{"name": "pytest", "status": "COMPLETED"}],
    }

    assert funnel.ci_rollup_state([check]) == "red"
    assert funnel.ci_could_not_run_reason([check]) is None


def test_genuine_failure_beats_a_startup_failure_in_the_same_rollup():
    could_not_run = {
        "name": "setup",
        "conclusion": "FAILURE",
        "completed_steps": 0,
    }
    genuine = {
        "name": "pytest",
        "conclusion": "FAILURE",
        "completed_steps": 4,
    }

    assert funnel.ci_rollup_state([could_not_run, genuine]) == "red"


def test_review_packet_carries_the_stop_without_failing_precheck():
    check = {
        "name": "tests",
        "conclusion": "FAILURE",
        "annotations": [{
            "message": "Actions startup failure: spending limit reached",
        }],
    }
    packet = review.build_packet(
        repo="owner/repo",
        pr_number=7,
        pr_view={
            "number": 7,
            "title": "do the thing",
            "headRefName": "ticket/9",
            "headRefOid": "abc123",
            "baseRefName": "main",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [check],
            "files": [],
        },
        diff="",
        ticket=None,
        plan_md="# plan",
        plan_md_missing=False,
        open_prs=[],
        verdict=None,
        stop_counter={"stop_auto_merging": False},
        collected_at="2026-09-18T00:00:00+00:00",
        pr_comments={"status": "empty", "message": "No PR comments.",
                     "comments": []},
    )

    assert packet["ci"]["state"] == funnel.CI_COULD_NOT_RUN
    assert packet["ci"]["annotation"] == check["annotations"][0]["message"]
    assert packet["precheck"] == {"pass": True, "reasons": []}
    json.dumps(packet)


def test_newest_actions_probe_holds_only_the_affected_repo(monkeypatch):
    repo = "owner/repo"
    calls = []

    def fake_api(endpoint, *, cache=False):
        calls.append((endpoint, cache))
        if endpoint.endswith("/actions/runs?per_page=1"):
            return {"workflow_runs": [{"id": 41, "conclusion": "failure"}]}
        if endpoint.endswith("/actions/runs/41/jobs?per_page=100"):
            return {"jobs": [{"id": 51, "conclusion": "failure", "steps": []}]}
        if endpoint.endswith("/check-runs/51/annotations?per_page=100"):
            return {"annotations": [{
                "message": "Recent account payments have failed",
            }]}
        raise AssertionError(endpoint)

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    state, reason = funnel.latest_actions_run_probe(repo)

    assert state == funnel.CI_COULD_NOT_RUN
    assert reason == "Recent account payments have failed"
    assert calls[0] == ("repos/{}/actions/runs?per_page=1".format(repo), False)


def test_begin_readiness_withholds_affected_repo_but_keeps_other_repos_flowing():
    affected = "owner/affected"
    healthy = "owner/healthy"
    rows = [
        funnel.Item(
            repo=affected, number=1, title="affected project", url="",
            state="OPEN", status="Building", klass="Improve",
            children_total=1, origin="agent", risk="standard", needs="none",
        ),
        funnel.Item(
            repo=affected, number=2, title="affected ticket", url="",
            state="OPEN", parent=affected + "#1",
            origin="agent", risk="standard", needs="none",
        ),
        funnel.Item(
            repo=healthy, number=3, title="healthy project", url="",
            state="OPEN", status="Building", klass="Improve",
            children_total=1, origin="agent", risk="standard", needs="none",
        ),
        funnel.Item(
            repo=healthy, number=4, title="healthy ticket", url="",
            state="OPEN", parent=healthy + "#3",
            origin="agent", risk="standard", needs="none",
        ),
    ]
    readiness = {
        affected: funnel.MemberRepoReadiness(
            affected, True, True, (), False,
            ci_state=funnel.CI_COULD_NOT_RUN,
            ci_annotation="spending limit reached",
        ),
        healthy: funnel.MemberRepoReadiness(
            healthy, True, True, (), False, ci_state="green",
        ),
    }

    assert [item.ref for item in funnel.startable(
        rows, repo_readiness=readiness
    )] == [healthy + "#4"]
    assert funnel.readiness_blockers(rows, repo_readiness=readiness) == [{
        "ref": affected + "#2",
        "repo": affected,
        "reasons": ["CI could not run: spending limit reached"],
    }]


def test_merge_refusal_names_the_startup_account_cause(monkeypatch):
    repo = "owner/repo"
    project = funnel.Item(
        repo=repo, number=1, title="project", url="", state="OPEN",
        status="Building", klass="Improve", children_total=1,
    )
    ticket = funnel.Item(
        repo=repo, number=9, title="ticket", url="", state="OPEN",
        parent=project.ref,
    )
    monkeypatch.setattr(
        funnel,
        "rejected_merges",
        lambda items, now: {
            "window_days": 7, "count": 0, "refs": [],
            "stop_auto_merging": False,
        },
    )
    blockers = funnel.merge_blockers(
        repo, 7, [project, ticket],
        review.datetime(2026, 9, 18),
        pr_fact={
            "state": "OPEN",
            "headRefName": "ticket/9",
            "headRefOid": "abc123",
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{
                "name": "tests",
                "conclusion": "FAILURE",
                "annotations": [{
                    "message": "Recent account payments have failed",
                }],
            }],
            "verdict": {
                "verdict": "approved",
                "head_sha": "abc123",
            },
        },
    )

    assert "CI could not run: Recent account payments have failed" in blockers
