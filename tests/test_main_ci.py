"""The main-branch CI check: infrastructure stop, or a real failure (#1178)."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "nateprich-projects/command-center"
SHA = "a3c97b14bbbbcccc0000111122223333444455556"


def _reads(monkeypatch, head, runs, jobs=None, annotations=None):
    """Stand in for the three REST reads main_ci_row makes."""

    def fake(endpoint, cache=False):
        if endpoint.startswith("repos/{}/commits/main".format(REPO)):
            return head
        if "actions/runs?branch=main" in endpoint:
            return runs
        if "actions/runs/" in endpoint and endpoint.endswith("jobs?per_page=100"):
            return jobs
        if "check-runs/" in endpoint:
            return annotations
        raise AssertionError("unexpected read: {}".format(endpoint))

    monkeypatch.setattr(funnel, "_gh_api_json", fake)


def test_zero_completed_steps_reads_as_an_infrastructure_stop(monkeypatch):
    _reads(
        monkeypatch,
        head={"sha": SHA},
        runs={"workflow_runs": [
            {"id": 35546576274, "conclusion": "failure", "head_sha": SHA},
        ]},
        jobs={"jobs": [
            {"id": 106173463211, "name": "test", "conclusion": "failure",
             "steps": []},
        ]},
        annotations=[],
    )

    row = funnel.main_ci_row(REPO)

    assert row["verdict"] == funnel.MAIN_CI_INFRA
    assert row["sha"] == SHA
    assert row["job"] == "test"
    assert "zero completed steps" in row["reason"]


def test_a_lost_runner_annotation_reads_as_an_infrastructure_stop(monkeypatch):
    """The #1178 signature: the job reports steps, but the runner went away."""
    _reads(
        monkeypatch,
        head={"sha": SHA},
        runs={"workflow_runs": [
            {"id": 35546576274, "conclusion": "failure", "head_sha": SHA},
        ]},
        jobs={"jobs": [
            {"id": 106173463211, "name": "test", "conclusion": "failure",
             "steps": [{"name": "setup-node", "status": "completed",
                        "conclusion": "failure"}]},
        ]},
        annotations=[{
            "message": "The self-hosted runner: mac-mini-1 lost communication "
                       "with the server. Verify the machine is running and has "
                       "a healthy network connection.",
        }],
    )

    row = funnel.main_ci_row(REPO)

    assert row["verdict"] == funnel.MAIN_CI_INFRA
    assert row["job"] == "test"
    assert "lost communication with the server" in row["reason"]


def test_a_test_failure_reads_as_real(monkeypatch):
    _reads(
        monkeypatch,
        head={"sha": SHA},
        runs={"workflow_runs": [
            {"id": 35546576274, "conclusion": "failure", "head_sha": SHA},
        ]},
        jobs={"jobs": [
            {"id": 106173463211, "name": "test", "conclusion": "failure",
             "steps": [
                 {"name": "checkout", "status": "completed",
                  "conclusion": "success"},
                 {"name": "pytest", "status": "completed",
                  "conclusion": "failure"},
             ]},
        ]},
        annotations=[{"message": "tests/test_ordering.py::test_rank failed"}],
    )

    row = funnel.main_ci_row(REPO)

    assert row["verdict"] == funnel.MAIN_CI_REAL
    assert row["job"] == "test"


def test_an_unreadable_annotation_reads_as_real(monkeypatch):
    """Ambiguity fails towards a person looking, never towards a rerun."""
    def explode(endpoint, cache=False):
        if endpoint.startswith("repos/{}/commits/main".format(REPO)):
            return {"sha": SHA}
        raise funnel.GitHubError("HTTP 502 reading actions runs")

    monkeypatch.setattr(funnel, "_gh_api_json", explode)

    row = funnel.main_ci_row(REPO)

    assert row["verdict"] == funnel.MAIN_CI_REAL
    assert "could not read main CI" in row["reason"]


def test_a_cancelled_run_is_not_called_infrastructure(monkeypatch):
    """A person pressing cancel looks like a dead host; it reads as real."""
    _reads(
        monkeypatch,
        head={"sha": SHA},
        runs={"workflow_runs": [
            {"id": 1, "conclusion": "failure", "head_sha": SHA},
        ]},
        jobs={"jobs": [
            {"id": 2, "name": "test", "conclusion": "failure",
             "steps": [{"name": "pytest", "status": "completed",
                        "conclusion": "cancelled"}]},
        ]},
        annotations=[{"message": "The operation was canceled."}],
    )

    assert funnel.main_ci_row(REPO)["verdict"] == funnel.MAIN_CI_REAL


def test_a_green_main_reports_nothing(monkeypatch):
    _reads(
        monkeypatch,
        head={"sha": SHA},
        runs={"workflow_runs": [
            {"id": 1, "conclusion": "success", "head_sha": SHA},
        ]},
    )

    assert funnel.main_ci_row(REPO) is None


def test_a_head_with_no_run_yet_reports_nothing(monkeypatch):
    _reads(monkeypatch, head={"sha": SHA}, runs={"workflow_runs": []})

    assert funnel.main_ci_row(REPO) is None


def test_a_running_main_reports_nothing(monkeypatch):
    _reads(
        monkeypatch,
        head={"sha": SHA},
        runs={"workflow_runs": [
            {"id": 1, "status": "in_progress", "head_sha": SHA},
        ]},
    )

    assert funnel.main_ci_row(REPO) is None


def test_the_brief_section_names_repo_sha_job_and_verdict(monkeypatch):
    monkeypatch.setattr(
        funnel, "main_ci_row",
        lambda repo: {
            "repo": repo, "sha": SHA, "job": "test",
            "verdict": funnel.MAIN_CI_INFRA,
            "reason": "lost communication with the server",
        } if repo == REPO else None,
    )

    rows = funnel.main_ci_json([REPO, "nateprich-projects/The-League"])

    assert rows == [{
        "repo": REPO, "sha": SHA, "job": "test",
        "verdict": "infra",
        "reason": "lost communication with the server",
    }]


def test_the_brief_carries_the_section(monkeypatch, capsys):
    import json

    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(funnel, "_read_portfolio_metrics", lambda items, now: None)
    monkeypatch.setattr(
        funnel, "main_ci_json",
        lambda *args: [{"repo": REPO, "sha": SHA, "job": "test",
                        "verdict": "infra", "reason": "lost communication "
                                                      "with the server"}],
    )

    from datetime import datetime, timezone
    now = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    assert funnel.cmd_brief([], now) == 0
    brief = json.loads(capsys.readouterr().out)

    assert brief["main_ci"] == [{
        "repo": REPO, "sha": SHA, "job": "test", "verdict": "infra",
        "reason": "lost communication with the server",
    }]
