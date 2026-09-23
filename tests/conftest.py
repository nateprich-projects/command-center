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


#: What the shared seam answers when a test does not say otherwise.
CODEX_SETTINGS_MATCH = {
    "ok": True,
    "rollout": None,
    "effective": {"model": "gpt-6-luna", "effort": "max"},
    "drift": [],
    "why": "",
}


@pytest.fixture(autouse=True)
def codex_settings_match(monkeypatch):
    """Codex runs in tests match the manifest unless a test says otherwise.

    ``begin --agent codex`` reads the run's own rollout under
    ``~/.codex/sessions`` (#1316). A test must not pass or fail on whatever
    this machine's Codex app last wrote there, so the seam answers "matches"
    by default. The check itself is tested against fixture rollouts in
    ``test_codex_run.py``, which gets the real seam from this fixture's
    value when it needs it.
    """
    import funnel

    real = funnel._codex_settings_check
    monkeypatch.setattr(funnel, "_codex_settings_check",
                        lambda: dict(CODEX_SETTINGS_MATCH))
    # A passing check against fixture roots can name this machine's real
    # automation directory; the suite must never reset its memory file.
    # Tests of the reset call codex_run.reset_memory on temporary
    # directories, or put the seam back themselves.
    monkeypatch.setattr(funnel, "_codex_memory_reset",
                        lambda directory: "skipped: stubbed in tests")
    yield real

@pytest.fixture
def resolver_clearing():
    """Build a copy of the real resolver with some repositories cleared.

    The production allowlist is empty since #1315, so every repository
    resolves to the fallback, and a runner that ignored the resolver would
    look the same as one that followed it. Runner tests write this source
    into their temporary repository to make the resolver's answer differ.

    The override goes immediately above the ``__main__`` guard. It then
    binds before the runners call the module as a script, and it does not
    depend on how the allowlist itself is written.
    """
    source = (Path(__file__).resolve().parent.parent
              / "muse_model.py").read_text()
    guard = '\nif __name__ == "__main__":'
    if source.count(guard) != 1:
        raise AssertionError(
            "muse_model.py no longer has exactly one __main__ guard; the "
            "resolver_clearing fixture inserts its override above it")

    def clearing(*names):
        override = "\nCONTRIBUTOR_REPOS = frozenset({!r})\n".format(
            sorted(names))
        return source.replace(guard, override + guard, 1)

    return clearing
