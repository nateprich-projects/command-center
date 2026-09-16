"""Shared test isolation for heartbeat writes.

The heartbeat is deliberately best-effort in production, but tests should not
depend on GitHub or append to the machine's write-ahead spool.  Keep that
boundary here so a new test gets the safe default without having to remember a
module-specific patch.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import heartbeat


LIVE_SPOOL_DIR = Path("~/.claude/command-center-heartbeat").expanduser()


@pytest.fixture(scope="session")
def offline_bin(tmp_path_factory):
    """A directory holding a ``gh`` that refuses, outside every tmp_path."""
    directory = tmp_path_factory.mktemp("offline-bin")
    offline = directory / "gh"
    offline.write_text(
        "#!/bin/sh\necho 'gh is offline in tests' >&2\nexit 1\n")
    offline.chmod(0o755)
    return directory


@pytest.fixture(autouse=True)
def heartbeat_isolation(monkeypatch, tmp_path, offline_bin):
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

    # Subprocesses must not reach the real, authenticated gh either: on the
    # schedule Mac one live call made while the shared GraphQL budget was
    # exhausted tripped funnel's process-wide stop and failed 32 later
    # tests (#942). A test that needs a fake gh still prepends its own.
    monkeypatch.setenv(
        "PATH", "{}{}{}".format(offline_bin, os.pathsep,
                                os.environ.get("PATH", "")))

    yield test_spool
