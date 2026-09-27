"""The run-keeper's reap pass for leaked Codex computer-use MCP sets (#1656).

Every kill here is an injected recorder: no test sends a real signal. The
recorded fixture is the Codex app-server subtree on the Mac mini at
2026-09-26 22:44 PDT, plus two unrelated node processes from the same table.
The 274-set census is synthetic, built in the recorded shape, because that
app-server had restarted before the census could be captured.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codex_reap  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "codex_reap" / "ps-2026-09-26-2244.txt"
HEADER = "  PID  PPID     ELAPSED COMMAND"

APP_SERVER_CMD = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/"
    "Contents/MacOS/codex -c features.code_mode_host=true app-server "
    "--analytics-default-enabled")
SKY = ("/Users/nateprich/.codex/computer-use/Codex Computer Use.app/Contents/"
       "SharedSupport/SkyComputerUseClient.app/Contents/MacOS/"
       "SkyComputerUseClient messages mcp")
NODE = "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node"
SERVER = NODE + " ./server.mjs"
NODE_REPL = NODE + "_repl"
CUA_REPL = (NODE + " /Applications/ChatGPT.app/Contents/Resources/cua_node/"
            "lib/node_modules/@oai/cua-repl/bin/cua-repl.mjs")

#: The three sets in the recording older than three hours, by cua-repl pid,
#: with their five members each.
RECORDED_OLD = {
    28041: {28041, 28042, 28043, 28044, 28046},
    28414: {28414, 28415, 28416, 28417, 28435},
    29254: {29254, 29255, 29256, 29257, 29264},
}


class Recorder:
    """Stands where SIGTERM would go; optionally fails for chosen pids."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, pid):
        self.calls.append(pid)
        if pid in self.fail:
            raise PermissionError(pid)


def etime(seconds: int) -> str:
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return "{:02d}-{:02d}:{:02d}:{:02d}".format(days, hours, minutes, seconds)
    if hours:
        return "{:02d}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return "{:02d}:{:02d}".format(minutes, seconds)


def row(pid, ppid, age, command):
    return "{:>5} {:>5} {:>11} {}".format(pid, ppid, etime(age), command)


def one_set(first_pid, ppid, age):
    """Rows for one set in the recorded shape: four children of the
    app-server, and a node_repl under the cua-repl host."""
    return [
        row(first_pid, ppid, age, SKY),
        row(first_pid + 1, ppid, age, SERVER),
        row(first_pid + 2, ppid, age, NODE_REPL),
        row(first_pid + 3, ppid, age, CUA_REPL),
        row(first_pid + 4, first_pid + 3, max(age - 1, 0), NODE_REPL),
    ]


def table(*rows):
    return "\n".join([HEADER, *rows]) + "\n"


def app_server(pid=500, age=50 * 3600):
    return row(pid, 1, age, APP_SERVER_CMD)


# --- the recorded snapshot -------------------------------------------------


def test_recorded_snapshot_groups_seventeen_sets_of_five():
    procs = codex_reap.parse_table(FIXTURE.read_text())
    sets = codex_reap.find_sets(procs)
    assert len(sets) == 17
    assert all(len(s.procs) == 5 for s in sets)
    assert all(
        sorted(codex_reap.kind_of(p) for p in s.procs)
        == ["cua_repl", "node_repl", "node_repl", "server", "sky"]
        for s in sets)


def test_recorded_snapshot_reaps_exactly_the_sets_older_than_three_hours():
    kill = Recorder()
    outcome = codex_reap.run(FIXTURE.read_text, kill)
    assert set(kill.calls) == set().union(*RECORDED_OLD.values())
    assert len(kill.calls) == len(set(kill.calls)) == 15
    assert (outcome.sets, outcome.procs, outcome.failed, outcome.fault) == (
        3, 15, 0, "-")
    # Never the app-server, its code-mode host, or the unrelated nodes.
    for pid in (27580, 29314, 668, 60073):
        assert pid not in kill.calls


def test_children_are_signalled_before_the_cua_repl_host_that_holds_them():
    kill = Recorder()
    codex_reap.run(FIXTURE.read_text, kill)
    assert kill.calls.index(28046) < kill.calls.index(28041)


# --- the 274-set census (synthetic, recorded shape) -------------------------


def census_table():
    rows = [app_server(pid=500, age=60 * 3600)]
    ages = []
    for index in range(274):
        # One set every ten minutes, the newest four minutes old: 256 of
        # them older than three hours.
        age = 240 + index * 600 + 7
        ages.append(age)
        rows.extend(one_set(1000 + index * 10, 500, age))
    # Noise that must never join or form a set.
    rows.append(row(9000, 500, 59 * 3600, "/Applications/ChatGPT.app/Contents/"
                    "Resources/codex-cli/bin/codex-code-mode-host"))
    rows.append(row(9001, 1, 59 * 3600, "./externals/node20/bin/node "
                    "./bin/RunnerService.js"))
    return table(*rows), ages


def test_census_of_274_sets_matches_exactly():
    text, ages = census_table()
    sets = codex_reap.find_sets(codex_reap.parse_table(text))
    assert len(sets) == 274
    assert all(len(s.procs) == 5 for s in sets)

    kill = Recorder()
    outcome = codex_reap.run(lambda: text, kill)
    old = sum(1 for age in ages if age > codex_reap.MAX_AGE_SECONDS)
    assert old == 256
    assert (outcome.sets, outcome.procs, outcome.failed) == (old, old * 5, 0)
    assert 9000 not in kill.calls and 9001 not in kill.calls


# --- required cases ---------------------------------------------------------


def test_a_one_hour_old_set_reaps_nothing():
    text = table(app_server(), *one_set(1000, 500, 3600))
    kill = Recorder()
    outcome = codex_reap.run(lambda: text, kill)
    assert kill.calls == []
    assert outcome.fields() == (
        "reap_sets=0 reap_procs=0 reap_failed=0 reap_fault=-")


def test_the_bound_is_strictly_older_than_three_hours():
    at = table(app_server(), *one_set(1000, 500, 3 * 3600))
    past = table(app_server(), *one_set(1000, 500, 3 * 3600 + 1))
    assert codex_reap.run(lambda: at, Recorder()).sets == 0
    assert codex_reap.run(lambda: past, Recorder()).sets == 1


def test_unrelated_node_processes_never_match():
    old = 30 * 3600
    text = table(
        app_server(),
        # The same binaries, old, but not under the Codex app-server.
        row(2000, 1, old, SKY),
        row(2001, 1, old, SERVER),
        row(2002, 1, old, NODE_REPL),
        row(2003, 1, old, CUA_REPL),
        row(2004, 2003, old, NODE_REPL),
        # Other node processes under the app-server itself.
        row(2010, 500, old, "/opt/homebrew/bin/node ./server.mjs"),
        row(2011, 500, old, "/opt/homebrew/bin/node /tmp/cua-repl.mjs"),
        row(2012, 500, old, "zcode-node-repl-mcp"),
        row(2013, 500, old, "/Applications/ChatGPT.app/Contents/Resources/"
                            "codex-cli/bin/codex-code-mode-host"),
        row(2014, 1, old, "./externals/node20/bin/node ./bin/RunnerService.js"),
    )
    kill = Recorder()
    outcome = codex_reap.run(lambda: text, kill)
    assert kill.calls == []
    assert outcome.sets == 0


def test_a_grandchild_node_repl_under_cua_repl_matches_through_the_walk():
    # The grandchild sits under an intermediate that is not in the signature,
    # two levels below the cua-repl host: only a full descendant walk finds it.
    age = 4 * 3600
    text = table(
        app_server(),
        *one_set(1000, 500, age)[:4],
        row(1100, 1003, age, "/bin/sh -c node_repl"),
        row(1101, 1100, age, NODE_REPL),
    )
    kill = Recorder()
    outcome = codex_reap.run(lambda: text, kill)
    assert 1101 in kill.calls
    assert 1100 not in kill.calls  # not in the signature, not signalled
    assert (outcome.sets, outcome.procs) == (1, 5)


def test_the_oldest_member_decides_even_when_it_is_the_grandchild():
    text = table(
        app_server(),
        *one_set(1000, 500, 3600)[:4],
        row(1004, 1003, 4 * 3600, NODE_REPL),
    )
    assert codex_reap.run(lambda: text, Recorder()).sets == 1


def test_the_app_server_is_found_by_command_not_by_pid():
    for pid in (27580, 4242, 88):
        text = table(app_server(pid=pid), *one_set(1000, pid, 4 * 3600))
        assert codex_reap.run(lambda: text, Recorder()).sets == 1


def test_no_app_server_reaps_nothing_and_is_not_a_fault():
    text = table(*one_set(1000, 1, 40 * 3600))
    kill = Recorder()
    outcome = codex_reap.run(lambda: text, kill)
    assert kill.calls == []
    assert outcome.fault == "-"


@pytest.mark.parametrize("reader", [
    lambda: "",
    lambda: "garbage without a header\n",
    lambda: HEADER + "\n",
    lambda: table(app_server(), "  12 notanumber 01:00 node_repl"),
    lambda: table(app_server(), *one_set(1000, 500, 4 * 3600),
                  "  99  500   xx:yy " + NODE_REPL),
])
def test_an_unreadable_table_records_a_fault_and_reaps_nothing(reader):
    kill = Recorder()
    outcome = codex_reap.run(reader, kill)
    assert kill.calls == []
    assert outcome.fields() == (
        "reap_sets=0 reap_procs=0 reap_failed=0 reap_fault=ps-unreadable")


def test_a_ps_that_fails_records_a_fault(tmp_path):
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\nexit 1\n")
    ps.chmod(0o755)
    kill = Recorder()
    outcome = codex_reap.run(lambda: codex_reap.read_table(str(ps)), kill)
    assert kill.calls == []
    assert outcome.fault == "ps-unreadable"
    missing = codex_reap.run(
        lambda: codex_reap.read_table(str(tmp_path / "absent")), kill)
    assert missing.fault == "ps-unreadable"


def test_a_failed_kill_is_recorded_and_not_retried():
    text = table(app_server(), *one_set(1000, 500, 4 * 3600))
    kill = Recorder(fail={1002})
    outcome = codex_reap.run(lambda: text, kill)
    assert kill.calls.count(1002) == 1
    assert len(kill.calls) == 5
    assert outcome.fields() == (
        "reap_sets=1 reap_procs=4 reap_failed=1 reap_fault=-")


def test_etime_forms():
    assert codex_reap.parse_etime("00:07") == 7
    assert codex_reap.parse_etime("03:41:23") == 3 * 3600 + 41 * 60 + 23
    assert codex_reap.parse_etime("10-23:22:28") == (
        10 * 86400 + 23 * 3600 + 22 * 60 + 28)
    for bad in ("", "7", "a:b", "1-02:03", "1:2:3:4"):
        with pytest.raises(ValueError):
            codex_reap.parse_etime(bad)


def test_the_command_line_prints_the_sentinel_fields(tmp_path):
    """End to end through main(): a stub ps and a stub kill command that logs
    instead of signalling."""
    snapshot = tmp_path / "snapshot.txt"
    snapshot.write_text(table(app_server(), *one_set(1000, 500, 4 * 3600),
                              *one_set(1010, 500, 600)))
    ps = tmp_path / "ps"
    ps.write_text("#!/bin/sh\ncat '{}'\n".format(snapshot))
    ps.chmod(0o755)
    log = tmp_path / "kills"
    kill = tmp_path / "kill"
    kill.write_text("#!/bin/sh\necho \"$@\" >> '{}'\n".format(log))
    kill.chmod(0o755)
    env = dict(os.environ, COMMAND_CENTER_SENTINEL_PS=str(ps),
               COMMAND_CENTER_KEEPER_REAP_KILL=str(kill))
    done = subprocess.run([sys.executable, str(ROOT / "codex_reap.py")],
                          env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == (
        "reap_sets=1 reap_procs=5 reap_failed=0 reap_fault=-")
    assert sorted(log.read_text().splitlines()) == sorted(
        "-TERM {}".format(pid) for pid in range(1000, 1005))
