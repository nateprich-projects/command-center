"""One retry of a main job that never really ran (#1178, #1220)."""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "nateprich-projects/command-center"
SHA = "a3c97b14bbbbcccc0000111122223333444455556"


def row(**kw):
    base = {
        "repo": REPO,
        "sha": SHA,
        "job": "test",
        "job_id": 106173463211,
        "run_id": 35546576274,
        "attempt": 1,
        "verdict": funnel.MAIN_CI_INFRA,
        "reason": "lost communication with the server",
    }
    base.update(kw)
    return base


def recorder(returncode=0, stderr=""):
    calls = []

    def rerun(command, **kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    return calls, rerun


def test_an_infrastructure_stop_on_its_first_attempt_is_rerun():
    calls, rerun = recorder()

    decision = funnel.main_ci_retry(REPO, row=row(), rerun=rerun)

    assert decision["retry"] == funnel.MAIN_CI_RETRIED
    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "run", "rerun"]
    assert "35546576274" in calls[0]
    assert "--job" in calls[0] and "106173463211" in calls[0]


def test_a_real_failure_is_never_rerun():
    """Rerunning a genuine break burns runner time and hides the signal."""
    calls, rerun = recorder()

    decision = funnel.main_ci_retry(
        REPO, row=row(verdict=funnel.MAIN_CI_REAL), rerun=rerun
    )

    assert decision["retry"] == funnel.MAIN_CI_NOT_RETRIED
    assert "needs a person" in decision["retry_reason"]
    assert calls == []


def test_a_second_red_on_the_same_sha_stays_red():
    """A rerun raises GitHub's own attempt count, so once-per-SHA needs no
    state file: the fact is already in the run."""
    calls, rerun = recorder()

    decision = funnel.main_ci_retry(REPO, row=row(attempt=2), rerun=rerun)

    assert decision["retry"] == funnel.MAIN_CI_NOT_RETRIED
    assert "already retried once" in decision["retry_reason"]
    assert calls == []


def test_an_unreadable_attempt_count_does_not_retry():
    """Without it a first failure and a second are indistinguishable, and
    retrying blindly is how a loop starts."""
    calls, rerun = recorder()

    decision = funnel.main_ci_retry(REPO, row=row(attempt=None), rerun=rerun)

    assert decision["retry"] == funnel.MAIN_CI_NOT_RETRIED
    assert "attempted" in decision["retry_reason"]
    assert calls == []


def test_a_missing_run_id_does_not_retry():
    calls, rerun = recorder()

    decision = funnel.main_ci_retry(REPO, row=row(run_id=None), rerun=rerun)

    assert decision["retry"] == funnel.MAIN_CI_NOT_RETRIED
    assert calls == []


def test_a_refused_rerun_is_reported_rather_than_claimed():
    calls, rerun = recorder(returncode=1, stderr="HTTP 403")

    decision = funnel.main_ci_retry(REPO, row=row(), rerun=rerun)

    assert decision["retry"] == funnel.MAIN_CI_NOT_RETRIED
    assert "HTTP 403" in decision["retry_reason"]
    assert len(calls) == 1


def test_a_green_main_has_nothing_to_retry(monkeypatch):
    monkeypatch.setattr(funnel, "main_ci_row", lambda repo: None)

    assert funnel.main_ci_retry(REPO) is None


def test_the_retry_runs_once_per_job_across_repos(monkeypatch):
    calls, rerun = recorder()
    other = "nateprich-projects/The-League"
    monkeypatch.setattr(
        funnel, "main_ci_row",
        lambda repo: row(repo=repo) if repo == REPO else row(
            repo=repo, verdict=funnel.MAIN_CI_REAL
        ),
    )

    decisions = funnel.main_ci_retries([REPO, other], rerun=rerun)

    assert [d["retry"] for d in decisions] == [
        funnel.MAIN_CI_RETRIED, funnel.MAIN_CI_NOT_RETRIED,
    ]
    assert len(calls) == 1


def test_the_attempt_count_is_read_from_either_wire_spelling():
    assert funnel._main_ci_attempt({"run_attempt": 2}) == 2
    assert funnel._main_ci_attempt({"runAttempt": "3"}) == 3
    assert funnel._main_ci_attempt({}) is None
    assert funnel._main_ci_attempt({"run_attempt": True}) is None
