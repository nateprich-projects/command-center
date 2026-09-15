"""Shared test isolation for heartbeat writes.

The heartbeat is deliberately best-effort in production, but tests should not
depend on GitHub or append to the machine's write-ahead spool.  Keep that
boundary here so a new test gets the safe default without having to remember a
module-specific patch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import heartbeat


LIVE_SPOOL_DIR = Path("~/.claude/command-center-heartbeat").expanduser()


@pytest.fixture(autouse=True)
def heartbeat_isolation(monkeypatch, tmp_path):
    """Keep heartbeat records local to this test and GitHub calls offline.

    In-process writes are redirected by patching ``heartbeat.SPOOL_DIR``;
    subprocesses inherit ``COMMAND_CENTER_HEARTBEAT_SPOOL``. Until #876 this
    fixture also failed any test during which a live spool file grew in size.
    On the schedule host the agents append to that spool every few minutes, so
    the size check reported concurrency as a leak and the full suite could not
    pass there; ``test_heartbeat_isolation.py`` now proves isolation by content
    instead.
    """
    test_spool = tmp_path / "heartbeat-spool"
    monkeypatch.setenv(
        "COMMAND_CENTER_DASHBOARD_SPOOL",
        str(tmp_path / "dashboard-spool"),
    )
    monkeypatch.setenv("COMMAND_CENTER_HEARTBEAT_SPOOL", str(test_spool))

    monkeypatch.setattr(heartbeat, "SPOOL_DIR", str(test_spool))

    def offline_gh(*args, **kwargs):
        raise heartbeat.HeartbeatError("offline in tests")

    monkeypatch.setattr(heartbeat, "gh", offline_gh)

    yield test_spool
