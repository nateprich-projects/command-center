#!/usr/bin/env python3
"""heartbeat.py — record what each run did, so a dead one is visible.

    heartbeat.py start  --agent codex --ticket 42     # prints a run id
    heartbeat.py finish --agent codex --run <id> --outcome skipped-over-pace
    heartbeat.py read   --agent codex

**Start and finish are separate records on purpose.** A single outcome line
cannot distinguish a run that died before writing one from a run that never
started — and a rate limit produces exactly that. A start with no finish is the
signal that matters.

Each record carries a usage reading, so the paired start/finish readings measure
what a run actually costs. That is what replaces the bootstrap reserves in
`usage.py` with measured numbers.

Records live on an orphan `heartbeat` branch, one JSONL file per agent, written
through the GitHub Contents API. Not on `main`, because a commit per run would
bury the history of the code; and on GitHub rather than the Mac mini, because
**the watchdog cannot live inside the thing it watches** — an app that quit is
invisible to every other signal on a machine that is otherwise fine.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional

REPO = "nateprich-projects/command-center"
BRANCH = "heartbeat"

#: Local write-ahead spool. A record lands here first and is pushed to GitHub
#: afterwards, so **the heartbeat can never stop a run**.
#:
#: It used to. A GitHub blip made `heartbeat start` raise, the run stopped at
#: step one, and — because the thing that failed *is* the record — it left no
#: trace of having stopped. That is indistinguishable from never having run,
#: which is the exact state the heartbeat exists to rule out. Instrumentation
#: must not gate the thing it instruments.
#:
#: Like the usage cache and the session transcripts, this is local and is not
#: state of record: it is a buffer that drains into GitHub, which remains the
#: state. Anything undrained is flushed by the next run that gets through.
SPOOL_DIR = os.path.expanduser("~/.claude/command-center-heartbeat")

#: Four attempts over roughly eleven seconds. Long enough to ride out a blip,
#: short enough not to eat a run's time when GitHub is genuinely down.
BACKOFF = [1, 3, 7]

#: Records kept per agent. Enough to measure run cost over several weeks;
#: bounded so the file never needs pagination.
KEEP = 1000

OUTCOMES = [
    "done",                # worked a ticket and opened or merged something
    "nothing-to-do",       # the funnel was empty
    "skipped-locked",      # another run holds the single-in-motion lock
    "skipped-over-pace",   # budget gate refused
    "skipped-usage-unknown",  # could not read usage; failed closed
    "errored",             # tried and failed
]


class HeartbeatError(RuntimeError):
    pass


def gh(*args: str, **kw) -> str:
    proc = subprocess.run(["gh"] + list(args), capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        raise HeartbeatError(proc.stderr.strip() or "gh exited {}".format(proc.returncode))
    return proc.stdout


def _path(agent: str) -> str:
    return "{}.jsonl".format(agent)


def _fetch(agent: str):
    """Current file content and blob sha, or (None, None) if absent."""
    try:
        raw = gh(
            "api",
            "repos/{}/contents/{}?ref={}".format(REPO, _path(agent), BRANCH),
        )
    except HeartbeatError:
        return None, None
    payload = json.loads(raw)
    content = base64.b64decode(payload.get("content", "")).decode("utf-8", "replace")
    return content, payload.get("sha")


def _ensure_branch() -> None:
    """Create the orphan branch on first use, pointing at an empty tree."""
    try:
        gh("api", "repos/{}/git/ref/heads/{}".format(REPO, BRANCH))
        return
    except HeartbeatError:
        pass
    tree = json.loads(gh("api", "-X", "POST", "repos/{}/git/trees".format(REPO),
                         "-f", "tree[][path]=.keep",
                         "-f", "tree[][mode]=100644",
                         "-f", "tree[][type]=blob",
                         "-f", "tree[][content]=heartbeat records; not part of main"))
    commit = json.loads(gh("api", "-X", "POST", "repos/{}/git/commits".format(REPO),
                           "-f", "message=Start the heartbeat branch",
                           "-f", "tree=" + tree["sha"]))
    gh("api", "-X", "POST", "repos/{}/git/refs".format(REPO),
       "-f", "ref=refs/heads/" + BRANCH, "-f", "sha=" + commit["sha"])


def _spool_path(agent: str) -> str:
    return os.path.join(SPOOL_DIR, "{}.jsonl".format(agent))


def _spool(agent: str, record: Dict) -> None:
    os.makedirs(SPOOL_DIR, exist_ok=True)
    with open(_spool_path(agent), "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _spooled(agent: str) -> List[Dict]:
    try:
        with open(_spool_path(agent)) as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return []


def _push(agent: str) -> None:
    """Drain the spool to GitHub. Raises if it cannot.

    The Contents API is compare-and-swap on the blob sha, so a concurrent write
    is rejected rather than silently lost, and the retry re-reads before
    rewriting. Both agents can be running at once, even though only one Codex
    run can.
    """
    pending = _spooled(agent)
    if not pending:
        return
    _ensure_branch()

    for attempt in range(len(BACKOFF) + 1):
        content, sha = _fetch(agent)
        lines = [ln for ln in (content or "").splitlines() if ln.strip()]
        lines += [json.dumps(r, sort_keys=True) for r in pending]
        body = ("\n".join(lines[-KEEP:]) + "\n").encode("utf-8")

        args = [
            "api", "-X", "PUT", "repos/{}/contents/{}".format(REPO, _path(agent)),
            "-f", "message=heartbeat: {} +{} record(s)".format(agent, len(pending)),
            "-f", "branch=" + BRANCH,
            "-f", "content=" + base64.b64encode(body).decode("ascii"),
        ]
        if sha:
            args += ["-f", "sha=" + sha]
        try:
            gh(*args)
            # Only clear what was actually sent; a record spooled while this was
            # in flight must survive to the next drain.
            remaining = _spooled(agent)[len(pending):]
            with open(_spool_path(agent), "w") as fh:
                for r in remaining:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
            return
        except HeartbeatError:
            if attempt >= len(BACKOFF):
                raise
            time.sleep(BACKOFF[attempt])


def append(agent: str, record: Dict) -> bool:
    """Record one heartbeat. Never raises.

    Returns True only if the record reached GitHub. False means it is spooled
    for the next drain, or — if even the spool could not be written — lost.
    Either way the caller proceeds: a run must not be stopped by its own
    bookkeeping.
    """
    recorded = True
    try:
        _spool(agent, record)
    except OSError:
        # Even the fallback can fail — a full disk, a bad path. A run must not
        # die because its bookkeeping could not be written, but it must not be
        # told the record was kept either: an unspooled record is simply lost,
        # and `_push` would otherwise find an empty spool and report success.
        recorded = False
    try:
        _push(agent)
    except (HeartbeatError, OSError):
        return False
    return recorded


def usage_snapshot(agent: str) -> Optional[Dict]:
    """Best-effort usage reading. Never fatal — a heartbeat that cannot be
    written because usage was unreadable would hide the very run it documents."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import usage

        reading = usage.read_claude() if agent == "claude" else usage.read_codex()
        if not reading:
            return None
        return {
            name: window.get("used_percent")
            for name, window in reading.get("windows", {}).items()
        }
    except Exception:
        return None


def read(agent: str) -> List[Dict]:
    """Every record this machine knows about — pushed and still spooled."""
    content, _ = _fetch(agent)
    records = []
    for line in (content or "").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records + _spooled(agent)


def unfinished(records: List[Dict], now: float, ttl_seconds: int) -> List[Dict]:
    """Runs that started, never finished, and are past the point of plausibility.

    This is the rate-limit death signature, and the reason start and finish are
    recorded separately.
    """
    finished = {r.get("run") for r in records if r.get("phase") == "finish"}
    return [
        r
        for r in records
        if r.get("phase") == "start"
        and r.get("run") not in finished
        and now - (r.get("ts") or 0) > ttl_seconds
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="record that a run began")
    start.add_argument("--agent", required=True, choices=["codex", "claude"])
    start.add_argument("--ticket", default=None)

    finish = sub.add_parser("finish", help="record how a run ended")
    finish.add_argument("--agent", required=True, choices=["codex", "claude"])
    finish.add_argument("--run", required=True)
    finish.add_argument("--outcome", required=True, choices=OUTCOMES)
    finish.add_argument("--note", default=None)
    finish.add_argument(
        "--merged", default=None,
        help="PR number merged unattended; recorded so the brief can surface it",
    )

    show = sub.add_parser("read", help="print an agent's records as JSON")
    show.add_argument("--agent", required=True, choices=["codex", "claude"])

    args = parser.parse_args(argv)

    try:
        if args.command == "read":
            print(json.dumps(read(args.agent), indent=2))
            return 0

        if args.command == "start":
            run_id = uuid.uuid4().hex[:12]
            pushed = append(args.agent, {
                "run": run_id,
                "agent": args.agent,
                "phase": "start",
                "ts": int(time.time()),
                "ticket": args.ticket,
                "usage": usage_snapshot(args.agent),
            })
            if not pushed:
                print(
                    "heartbeat: GitHub unreachable; record spooled locally and "
                    "will be pushed by a later run. Continuing.",
                    file=sys.stderr,
                )
            print(run_id)
            return 0

        pushed = append(args.agent, {
            "run": args.run,
            "agent": args.agent,
            "phase": "finish",
            "ts": int(time.time()),
            "outcome": args.outcome,
            "note": args.note,
            # Recorded as a field, not scraped out of the note. plan.md requires
            # unattended merges to appear in the brief as a record, and a record
            # that has to be parsed out of prose is not a record.
            "merged": args.merged,
            "usage": usage_snapshot(args.agent),
        })
        if not pushed:
            print(
                "heartbeat: GitHub unreachable; record spooled locally.",
                file=sys.stderr,
            )
        return 0
    except HeartbeatError as exc:
        print("heartbeat: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
