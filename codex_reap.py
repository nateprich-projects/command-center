"""Reap leaked Codex computer-use MCP sets under the Codex app-server (#1656).

Every Codex app session starts one *set* of helper processes under the Codex
app-server and, measured on 2026-09-26, never stops it: 274 sets were counted
under one app-server before it restarted. A set is four processes spawned
together as direct children of the app-server:

- ``SkyComputerUseClient messages mcp``
- ``cua_node/bin/node ./server.mjs``
- ``cua_node/bin/node_repl``
- ``cua_node/bin/node .../cua-repl/bin/cua-repl.mjs``, which holds a second
  ``node_repl`` of its own (observed 2026-09-26 20:50 PDT), so a walk of the
  direct children alone misses it.

The run-keeper calls this once per run. It resolves the app-server by command
match, never a pinned pid; walks its whole descendant tree; groups the matching
processes into sets; and sends SIGTERM to every process of each set whose
oldest process is older than three hours (Nate's accepted bound; nothing here
protects an interactive set). It prints one run of ``key=value`` fields that
the keeper appends to the sentinel line it already writes — GitHub is the
record, so there is no local state of any kind.

Fail closed: a process table that cannot be read or parsed records a fault
and reaps nothing. A kill that fails is counted, never retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import signal
import subprocess
import sys
from typing import Callable, Iterable

#: Nate's accepted bound: a set whose oldest process is older than this is
#: reaped.
MAX_AGE_SECONDS = 3 * 60 * 60

#: The members of one set start within the same second or two (measured:
#: identical ``etime`` for all four, one second later for the grandchild).
#: Anything further apart is a different set.
SPAWN_WINDOW_SECONDS = 5

PS_ARGS = ("-axo", "pid,ppid,etime,command")

APP_SERVER = re.compile(
    r"/codex-cli/CodexCLI\.app/Contents/MacOS/codex(\s.*)?\sapp-server(\s|$)")

#: The measured signature: one pattern per member kind.
SIGNATURE = {
    "sky": re.compile(r"/SkyComputerUseClient\s+messages\s+mcp(\s|$)"),
    "server": re.compile(r"/cua_node/bin/node\s+(\S*/)?\.?/?server\.mjs(\s|$)"),
    "cua_repl": re.compile(
        r"/cua_node/bin/node\s+\S*/cua-repl/bin/cua-repl\.mjs(\s|$)"),
    "node_repl": re.compile(r"/cua_node/bin/node_repl(\s|$)"),
}


class ProcessTableError(Exception):
    """The process table could not be read or parsed."""


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    age: int  # seconds since start, from ps ``etime``
    command: str


@dataclass
class ReapSet:
    members: list = field(default_factory=list)  # anchors: Proc
    procs: list = field(default_factory=list)  # every matching Proc, depth order

    @property
    def age(self) -> int:
        return max(p.age for p in self.procs)


@dataclass
class Outcome:
    sets: int = 0
    procs: int = 0
    failed: int = 0
    fault: str = "-"

    def fields(self) -> str:
        return "reap_sets={} reap_procs={} reap_failed={} reap_fault={}".format(
            self.sets, self.procs, self.failed, self.fault)


def parse_etime(text: str) -> int:
    """Seconds from ps ``etime``: ``[[dd-]hh:]mm:ss``."""
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        if not day_part.isdigit():
            raise ValueError(text)
        days = int(day_part)
    parts = text.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(text)
    if days and len(parts) != 3:
        raise ValueError(text)
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return days * 86400 + seconds


def parse_table(text: str) -> list:
    """Every row of ``ps -axo pid,ppid,etime,command``; any bad row is fatal."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise ProcessTableError("empty")
    header = lines[0].split()
    if header[:3] != ["PID", "PPID", "ELAPSED"]:
        raise ProcessTableError("unexpected header")
    procs = []
    for line in lines[1:]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            raise ProcessTableError("short row")
        try:
            procs.append(Proc(int(parts[0]), int(parts[1]),
                              parse_etime(parts[2]), parts[3].strip()))
        except ValueError:
            raise ProcessTableError("unparseable row") from None
    if not procs:
        raise ProcessTableError("no rows")
    return procs


def kind_of(proc: Proc):
    for kind, pattern in SIGNATURE.items():
        if pattern.search(proc.command):
            return kind
    return None


def find_sets(procs: Iterable) -> list:
    """Every set under every Codex app-server, oldest first."""
    procs = list(procs)
    children = {}
    for proc in procs:
        children.setdefault(proc.ppid, []).append(proc)

    anchors = []  # (anchor, [anchor and its matching descendants])
    for server in procs:
        if not APP_SERVER.search(server.command):
            continue
        # Walk the full descendant tree. The topmost matching process on a
        # branch anchors it; matching processes beneath it join its set.
        stack = [(child, None) for child in children.get(server.pid, [])]
        seen = {server.pid}
        by_anchor = {}
        while stack:
            proc, anchor = stack.pop()
            if proc.pid in seen:
                continue
            seen.add(proc.pid)
            if kind_of(proc) is not None:
                if anchor is None:
                    anchor = proc
                    by_anchor[proc.pid] = (proc, [])
                by_anchor[anchor.pid][1].append(proc)
            stack.extend((child, anchor) for child in children.get(proc.pid, []))
        anchors.extend(by_anchor.values())

    # Group the anchors spawned together. Start is measured as -age so the
    # oldest sorts first; pid breaks ties in spawn order.
    anchors.sort(key=lambda item: (-item[0].age, item[0].pid))
    sets = []
    current = None
    for anchor, members in anchors:
        kind = kind_of(anchor)
        if (current is None
                or kind in {kind_of(m) for m in current.members}
                or current.members[0].age - anchor.age > SPAWN_WINDOW_SECONDS
                or current.members[0].ppid != anchor.ppid):
            current = ReapSet()
            sets.append(current)
        current.members.append(anchor)
        # Descendants before their anchor: signal children first so a parent
        # exiting cannot take a child's pid out from under the count.
        current.procs.extend(reversed(members))
    return sets


def reap(procs: Iterable, kill: Callable[[int], None],
         max_age: int = MAX_AGE_SECONDS) -> Outcome:
    outcome = Outcome()
    for reap_set in find_sets(procs):
        if reap_set.age <= max_age:
            continue
        outcome.sets += 1
        for proc in reap_set.procs:
            try:
                kill(proc.pid)
            except Exception:  # recorded, never retried
                outcome.failed += 1
            else:
                outcome.procs += 1
    return outcome


def read_table(ps: str = "/bin/ps") -> str:
    try:
        done = subprocess.run([ps, *PS_ARGS], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ProcessTableError("ps failed") from None
    if done.returncode != 0:
        raise ProcessTableError("ps exited {}".format(done.returncode))
    return done.stdout


def run(read: Callable[[], str], kill: Callable[[int], None]) -> Outcome:
    """One keeper pass: read, parse, reap. Never raises."""
    try:
        procs = parse_table(read())
    except ProcessTableError:
        return Outcome(fault="ps-unreadable")
    return reap(procs, kill)


def sigterm(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def command_kill(command: str) -> Callable[[int], None]:
    """A kill that runs ``<command> -TERM <pid>``; the keeper's tests use it
    to put a recording stub where the real signal would go."""
    def kill(pid: int) -> None:
        subprocess.run([command, "-TERM", str(pid)], check=True,
                       capture_output=True, timeout=10)
    return kill


def main() -> int:
    ps = os.environ.get("COMMAND_CENTER_SENTINEL_PS", "/bin/ps")
    kill_command = os.environ.get("COMMAND_CENTER_KEEPER_REAP_KILL")
    kill = command_kill(kill_command) if kill_command else sigterm
    print(run(lambda: read_table(ps), kill).fields())
    return 0


if __name__ == "__main__":
    sys.exit(main())
