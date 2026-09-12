"""The shared subprocess launch path retries only transient fork pressure."""

from __future__ import annotations

import errno
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EAGAIN, "Resource temporarily unavailable"),
        OSError(errno.EWOULDBLOCK, "Resource temporarily unavailable"),
        OSError(35, "Resource temporarily unavailable"),
        OSError("fork Resource temporarily unavailable"),
    ],
)
def test_the_fork_pressure_signatures_are_transient(error):
    assert funnel._is_transient_fork_error(error)


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ENOENT, "No such file or directory"),
        OSError(errno.EPERM, "Operation not permitted"),
        RuntimeError("fork Resource temporarily unavailable"),
    ],
)
def test_ordinary_launch_errors_are_not_transient(error):
    assert not funnel._is_transient_fork_error(error)


def test_transient_fork_failure_is_retried_until_launch_succeeds(monkeypatch):
    calls = []
    sleeps = []
    failures = [
        OSError(errno.EAGAIN, "Resource temporarily unavailable"),
        OSError(35, "Resource temporarily unavailable"),
    ]

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if failures:
            raise failures.pop(0)
        return funnel.subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.time, "sleep", sleeps.append)
    monkeypatch.setattr(funnel.random, "uniform", lambda low, high: 1.0)

    result = funnel._run_bounded_subprocess(["gh", "api", "graphql"])

    assert result.stdout == "ok"
    assert len(calls) == 3
    assert sleeps == [5.0, 15.0]


def test_ordinary_launch_failure_is_not_retried(monkeypatch):
    calls = []
    error = OSError(errno.ENOENT, "No such file or directory")

    def run(command, **kwargs):
        calls.append(command)
        raise error

    monkeypatch.setattr(funnel.subprocess, "run", run)

    with pytest.raises(OSError) as raised:
        funnel._run_bounded_subprocess(["missing-command"])

    assert raised.value is error
    assert calls == [["missing-command"]]


def test_persistent_fork_failure_stays_inside_the_retry_window(monkeypatch):
    calls = []
    sleeps = []
    clock = [0.0]

    def run(command, **kwargs):
        calls.append(command)
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(funnel.time, "sleep", sleep)
    monkeypatch.setattr(funnel.random, "uniform", lambda low, high: 1.0)

    with pytest.raises(OSError, match="retry on the next scheduled run") as raised:
        funnel._run_bounded_subprocess(["gh", "api"])

    assert len(calls) == funnel.FORK_RETRY_MAX_ATTEMPTS
    assert sum(sleeps) <= funnel.FORK_RETRY_WINDOW_SECONDS
    assert clock[0] <= funnel.FORK_RETRY_WINDOW_SECONDS
    assert "gh" in str(raised.value)
