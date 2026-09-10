"""Suite-wide isolation of the heartbeat spool (#516).

The heartbeat is the record of truth for `agent_health`, `unattended_merges`,
the watchdog and the brief. On 2026-09-10 a test run appended fourteen fake
`bind` records to the live spool under `~/.claude/command-center-heartbeat`,
and three fake events from an earlier run had already reached the `heartbeat`
branch, because tests stubbed the push but not the file write inside
`heartbeat.append()`. Every test now spools to a temporary directory and never
reaches GitHub; a test that still finds a way to grow a live spool file fails,
naming the file. A test-level `monkeypatch` overrides any of this.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402

# The isolation test below drives a nested pytest run; plain `pytest -q` (CI)
# must have the fixture without a command-line flag.
pytest_plugins = ("pytester",)

LIVE_SPOOL = pathlib.Path(os.path.expanduser("~/.claude/command-center-heartbeat"))


def _live_sizes():
    try:
        return {path.name: path.stat().st_size for path in LIVE_SPOOL.glob("*.jsonl")}
    except OSError:
        return {}


@pytest.fixture(autouse=True)
def _heartbeat_stays_out_of_the_live_spool(request, tmp_path, monkeypatch):
    """Redirect the spool and cut the transport for every test."""
    monkeypatch.setattr(heartbeat, "SPOOL_DIR", str(tmp_path / "heartbeat-spool"))

    def offline(*args, **kwargs):
        raise heartbeat.HeartbeatError("offline in tests")

    monkeypatch.setattr(heartbeat, "gh", offline)
    before = _live_sizes()
    yield
    after = _live_sizes()
    # A file that did not exist before counts as growth: the escape on
    # 2026-09-10 would have been a new agent file just as easily.
    grew = sorted(name for name, size in after.items() if size > before.get(name, 0))
    if grew:
        pytest.fail(
            "the test wrote into the live heartbeat spool: {} — every heartbeat "
            "writer in a test must land in the temporary spool this fixture "
            "provides (#516)".format(", ".join(grew))
        )
