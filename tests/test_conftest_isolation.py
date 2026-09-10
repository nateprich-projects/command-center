"""The suite never touches the live heartbeat spool (#516, ticket #531)."""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402

LIVE_SPOOL = pathlib.Path(os.path.expanduser("~/.claude/command-center-heartbeat"))


def test_a_real_append_lands_in_the_temporary_spool(tmp_path):
    assert heartbeat.SPOOL_DIR.startswith(str(tmp_path))
    kept = heartbeat.append("codex", {"run": "isolation-probe", "agent": "codex",
                                      "phase": "start", "ts": 1})
    assert kept == "spooled"
    spooled = heartbeat._spooled("codex")
    assert [r["run"] for r in spooled] == ["isolation-probe"]
    assert not any(
        "isolation-probe" in line
        for path in LIVE_SPOOL.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ) if LIVE_SPOOL.exists() else True


def test_the_transport_is_cut():
    try:
        heartbeat.gh("api", "user")
    except heartbeat.HeartbeatError as exc:
        assert "offline in tests" in str(exc)
    else:  # pragma: no cover - the fixture failed to apply
        raise AssertionError("heartbeat.gh reached the network from a test")


def test_a_live_spool_write_fails_the_test_that_made_it(pytester):
    conftest = (ROOT / "tests" / "conftest.py").read_text().replace(
        "ROOT = pathlib.Path(__file__).resolve().parent.parent",
        "ROOT = pathlib.Path({!r})".format(str(ROOT)),
    )
    pytester.makeconftest(conftest)
    pytester.makepyfile(
        """
        import os, pathlib
        def test_writes_live():
            live = pathlib.Path(os.path.expanduser("~/.claude/command-center-heartbeat"))
            live.mkdir(parents=True, exist_ok=True)
            with open(live / "probe-agent.jsonl", "a") as fh:
                fh.write("{}\\n")
        """
    )
    result = pytester.runpytest("-q", "-p", "no:cacheprovider")
    # The body passes; the fixture catches the write at teardown, which
    # pytest reports as an error against that test.
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*wrote into the live heartbeat spool: probe-agent.jsonl*"])
