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


def _spool_sizes(directory: Path):
    """Return the current byte size of every live heartbeat spool file."""
    try:
        paths = sorted(directory.glob("*.jsonl"))
    except OSError:
        return {}
    return {
        path: path.stat().st_size
        for path in paths
        if path.is_file()
    }


@pytest.fixture(autouse=True)
def heartbeat_isolation(monkeypatch, tmp_path):
    """Keep heartbeat records local to this test and GitHub calls offline."""
    live_before = _spool_sizes(LIVE_SPOOL_DIR)
    test_spool = tmp_path / "heartbeat-spool"

    monkeypatch.setattr(heartbeat, "SPOOL_DIR", str(test_spool))

    def offline_gh(*args, **kwargs):
        raise heartbeat.HeartbeatError("offline in tests")

    monkeypatch.setattr(heartbeat, "gh", offline_gh)

    yield test_spool

    live_after = _spool_sizes(LIVE_SPOOL_DIR)
    grown = sorted(
        path for path, size in live_after.items()
        if size > live_before.get(path, 0)
    )
    if grown:
        pytest.fail(
            "live heartbeat spool grew: {}".format(
                ", ".join(str(path) for path in grown)
            )
        )
