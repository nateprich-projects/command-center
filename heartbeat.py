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
import contextlib
import json
import os
import glob
import math
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - the schedule Mac and CI are Unix
    fcntl = None  # type: ignore

import session_usage

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
#: ``COMMAND_CENTER_HEARTBEAT_SPOOL`` overrides the location. The test suite
#: sets it so that every subprocess a test launches inherits a temporary spool
#: (#876); production never sets it.
SPOOL_DIR = os.environ.get("COMMAND_CENTER_HEARTBEAT_SPOOL") or os.path.expanduser(
    "~/.claude/command-center-heartbeat")

#: Four attempts over roughly eleven seconds. Long enough to ride out a blip,
#: short enough not to eat a run's time when GitHub is genuinely down.
BACKOFF = [1, 3, 7]

#: Records kept per agent. At the measured Muse rate of about 135 records per
#: hour, 10,000 records cover roughly 74 hours, holding a 48-hour window with
#: margin while remaining bounded.
KEEP = 10000

#: The run accounting shown in the brief and watchdog log. Keep it short
#: enough to describe current health without making old one-off runs dominate
#: the line.
RUN_SUMMARY_WINDOW_SECONDS = 7 * 86400

OUTCOMES = [
    "done",                # worked a ticket and opened or merged something
    "nothing-to-do",       # the funnel was empty
    "skipped-locked",      # another run holds the single-in-motion lock
    "skipped-over-pace",   # budget gate refused
    "skipped-nate-active",  # the five-hour window was already in use; Codex only
    "skipped-usage-unknown",  # could not read usage; failed closed
    "skipped-blocked",     # prerequisite has not landed; no change made
    "skipped-human-step",   # paused for a required human action
    "skipped-api-reserve",  # GraphQL budget below the reserve floor (#273)
    "config-drift",        # a Codex run's settings differ from codex_run.py (#1316);
                           # not `skipped-*`, which the watchdog treats as healthy
    "skipped-provider-quota",  # the model provider refused: its usage window is spent
    "skipped-outside-window",  # Claude's Saturday-morning window is closed (#1557)
    "budget-exhausted",     # begin could not start after the GraphQL pool hit zero
    "errored",             # tried and failed
]

#: The per-command GitHub measurements funnel records as non-terminal events.
#: Keep the names here so finish can aggregate each budget independently and
#: never turn an unreadable value into zero.
API_COST_FIELDS = ("graphql_points", "gh_calls")
GRAPHQL_CALLER_NAMES = frozenset((
    "standard", "escalated", "publisher", "watch", "begin", "breakdown",
    "unattributed",
))
GRAPHQL_CALLER_COST_FIELDS = ("calls", "points", "remaining")

# These are the note signatures the execution plan can currently distinguish.
# Anything outside these explicit rules stays unclassified; the runtime head is
# required as the code revision that was active when the finish was written.
FLOOR_ERROR_MARKERS = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "ratelimit",
    "provider refused",
    "provider refusal",
    "provider quota",
    "quota exhausted",
    "request timed out",
    "connection timed out",
    "gateway timeout",
    "context deadline exceeded",
    "tls handshake timeout",
    "i/o timeout",
)
BEGIN_TIMEOUT_ERROR_MARKERS = ("reply-timeout", "slow command: begin")
REGRESSION_ERROR_MARKERS = ("tests failed:",)
UNCLASSIFIED_ERROR_MARKERS = (
    "could not derive a test command",
    "no module named pytest",
    "requires python ",
)


def classify_error(note: Optional[str], runtime: Optional[Dict]) -> str:
    """Classify a failed finish from its note and runtime code revision.

    The classifier is deliberately a small explicit lookup. Missing evidence
    and notes with no matching rule are ``unclassified`` rather than weather.
    """
    head = runtime.get("head") if isinstance(runtime, dict) else None
    if not isinstance(head, str) or not head.strip():
        return "unclassified"
    if not isinstance(note, str) or not note.strip():
        return "unclassified"

    normalized = note.casefold()
    if all(marker in normalized for marker in BEGIN_TIMEOUT_ERROR_MARKERS):
        return "begin-timeout"
    if any(marker in normalized for marker in FLOOR_ERROR_MARKERS):
        return "floor"
    if any(marker in normalized for marker in UNCLASSIFIED_ERROR_MARKERS):
        return "unclassified"
    if any(marker in normalized for marker in REGRESSION_ERROR_MARKERS):
        return "regression"
    return "unclassified"


def _non_negative_int(value: str) -> int:
    """Parse a structured count without admitting booleans or negatives."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _optional_text(value: str) -> Optional[str]:
    """Treat the empty CLI value as the explicit JSON null it represents."""
    return value if value else None


def _muse_call_record(value: str) -> Dict[str, object]:
    """Parse the ephemeral call capture summary written by Muse's runner."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a Muse call record JSON object")
    if not isinstance(parsed, dict) or set(parsed) != {"session_ids", "calls_made"}:
        raise argparse.ArgumentTypeError(
            "must contain exactly session_ids and calls_made"
        )
    session_ids = parsed.get("session_ids")
    calls_made = parsed.get("calls_made")
    if (
        not isinstance(session_ids, list)
        or isinstance(calls_made, bool)
        or not isinstance(calls_made, int)
        or calls_made < 0
        or calls_made != len(session_ids)
        or any(
            session_id is not None
            and (not isinstance(session_id, str) or not session_id.strip())
            for session_id in session_ids
        )
    ):
        raise argparse.ArgumentTypeError(
            "session_ids must contain one non-empty id or null per call"
        )
    return parsed


#: Which pool an agent spends. Deliberately separate from the model: routing will
#: put more than one model on a pool, and the budget is per pool.
PROVIDERS = {"claude": "anthropic", "codex": "openai", "zcode": "zai",
             "muse": "meta"}

#: When z.ai stops answering the standard judgement tier: 2026-10-07 00:00
#: in Beijing time (UTC+8), 2026-10-06 09:00 PDT. The cancelled z.ai plan
#: expires on 2026-10-07 (Nate, 2026-09-23), and z.ai may count that date in
#: its own zone, so the lane ends at the earliest reading of it rather than
#: risk runs erroring on an expired key (#1411). `scripts/muse-review-engine`
#: routes on the same instant and a test pins the two together.
ZAI_STANDARD_UNTIL = 1791302400


def retired_agents(now: Optional[float] = None) -> frozenset:
    """Agents whose schedules are stopped on purpose, as of ``now``.

    Their records stay readable and every command still accepts them, but
    the watchdog and `agent_health` must not read their silence as a run that
    died. zcode was retired on 2026-09-09 by Nate's decision: measured over
    24h it did work in 18 of 93 runs and was refused on the z.ai pace line in
    63, while Muse carried every job it had on the separate Meta pool (#431).
    It runs again, as the engine's z.ai standard tier, from 2026-09-23 until
    ``ZAI_STANDARD_UNTIL``, and retires again at that instant by itself, so
    its silence after the plan expires is not read as a lane that died.
    codex was retired from 2026-09-18 to 2026-09-22, while Muse implemented
    both tiers (#1106). It returned when its automations went live again
    (Nate, #1315, #1325): Codex implements both tiers, so its silence is a
    lane that stopped, not the pause working.
    """
    now = time.time() if now is None else now
    return frozenset() if now < ZAI_STANDARD_UNTIL else frozenset({"zcode"})


#: Read once per process. Every reader is a short-lived command (a brief, a
#: watchdog pass, one engine run), so the set is current for the run that
#: reads it and the zcode lapse needs no edit.
RETIRED_AGENTS = retired_agents()

#: Which application ran it. Distinct from provider and model: one provider can
#: be reached through more than one harness, and harnesses differ in ways that
#: change outcomes — agent loop, tool selection, context handling, retries.
HARNESSES = {"claude": "claude-code", "codex": "codex", "zcode": "zcode",
             "muse": "muse-code"}

# Harness session ids let a fresh `begin` distinguish its own abandoned start
# from another run of the same agent. Missing ids are deliberately treated as
# unknown: closing an uncertain start would hide a genuinely live run.
SESSION_ENVS = {
    "claude": ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"),
    "codex": ("CODEX_SESSION_ID",),
    "zcode": ("ZCODE_SESSION_ID",),
    "muse": ("MUSE_SESSION_ID",),
}

#: Where each agent records what it actually is. **Read, never asked.** A prompt
#: that reports its own model reports what it believes, and one confident wrong
#: answer silently poisons the routing dataset this exists to build.
#:
#: Both are files `usage.py` already globs for quota, so this adds no new source.
MODEL_SOURCES = {
    "claude": "~/.claude/projects/*/*.jsonl",
    "codex": "~/.codex/sessions/*/*/*/*.jsonl",
    # zai-exec's call log since 2026-09-23, in the zcode app's model-io shape.
    # Not the retired app's own rollout directory: its newest file is from
    # 2026-09-09, and reading it would stamp that session's model and input
    # counts onto every engine run.
    "zcode": "~/.local/share/zai-exec/rollout/*.jsonl",
    # Muse writes whole-file JSON snapshots rather than JSONL, so it is parsed
    # by its own branch below. `HEAD.json` in the same directory carries no
    # model, which is why this globs snapshots specifically.
    "muse": "~/.local/share/muse/sessions/.msp-view-v1/*/snapshot-*.json",
}

#: Claude's transcripts record the model but not the effort level.
CLAUDE_SETTINGS = "~/.claude/settings.json"


class HeartbeatError(RuntimeError):
    pass


def gh(*args: str, **kw) -> str:
    try:
        proc = subprocess.run(["gh"] + list(args), capture_output=True, text=True, **kw)
    except OSError as exc:
        # E2BIG, a missing binary, a sandbox refusal: GitHub was not reached, which
        # is what HeartbeatError means to every caller. As a bare OSError it
        # skipped `_push`'s retries and read as "spooled" with no reason (#949).
        raise HeartbeatError("gh could not run: {}".format(exc)) from exc
    if proc.returncode != 0:
        raise HeartbeatError(proc.stderr.strip() or "gh exited {}".format(proc.returncode))
    return proc.stdout


def _path(agent: str) -> str:
    return "{}.jsonl".format(agent)


def _fetch(agent: str, timeout: Optional[float] = None):
    """Current file content and blob sha, or (None, None) if absent."""
    try:
        args = (
            "api",
            "repos/{}/contents/{}?ref={}".format(REPO, _path(agent), BRANCH),
        )
        raw = gh(*args, **({"timeout": timeout} if timeout is not None else {}))
    except HeartbeatError:
        return None, None
    payload = json.loads(raw)
    if payload.get("encoding") == "none" or (payload.get("size") and not payload.get("content")):
        # Above 1 MB the Contents API returns metadata only. Reading that as an
        # empty file would let `_push` rewrite the branch from nothing, so read
        # the raw body instead, and let a failure raise rather than return "" (#949).
        text = gh(*args, "-H", "Accept: application/vnd.github.raw",
                  **({"timeout": timeout} if timeout is not None else {}))
        return text, payload.get("sha")
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


@contextlib.contextmanager
def _spool_lock(agent: str):
    """Hold a best-effort advisory lock on the agent's spool file (#1238).

    Yields the open spool handle while an exclusive non-blocking `flock` is
    held, or `None` when there is nothing to hold: no `fcntl` on this
    platform, no spool file yet, the lock already taken by a concurrent
    lane, or a filesystem that will not lock. Every one of those falls
    through to the unlocked path — instrumentation must not gate the thing
    it instruments, and the dedup predicate in `_push` catches the
    duplicate a missed lock lets through.

    The lock is on the spool that is already there: opening it read-only
    never creates a file, and removing the lock leaves dedup-only
    correctness behind.
    """
    if fcntl is None:
        yield None
        return
    try:
        fh = open(_spool_path(agent), "r")
    except OSError:
        yield None
        return
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            fh.close()
        except OSError:
            pass
        yield None
        return
    try:
        yield fh
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except OSError:
            pass


def _push(agent: str, extra: Optional[List[Dict]] = None) -> None:
    """Drain the spool to GitHub. Raises if it cannot.

    `extra` carries records that never reached the spool — a full disk, or a
    sandbox that will not let us write there. The spool is a write-ahead buffer,
    not a prerequisite: when the buffer fails and the destination is up, send the
    record straight to the destination rather than losing it.

    The Contents API is compare-and-swap on the blob sha, so a concurrent write
    is rejected rather than silently lost, and the retry re-reads before
    rewriting. Both agents can be running at once, even though only one Codex
    run can.

    A best-effort non-blocking `flock` on the spool narrows the
    concurrent-drain window; it is never waited on and never fatal, and a
    missed lock only costs the dedup predicate a line to skip (#1238).
    """
    with _spool_lock(agent):
        _push_drain(agent, extra)


def _push_drain(agent: str, extra: Optional[List[Dict]] = None) -> None:
    """The drain itself; `_push` holds the spool lock around it."""
    from_spool = _spooled(agent)
    pending = from_spool + list(extra or [])
    if not pending:
        return
    _ensure_branch()

    for attempt in range(len(BACKOFF) + 1):
        content, sha = _fetch(agent)
        lines = [ln for ln in (content or "").splitlines() if ln.strip()]
        # A concurrent lane may have drained the same spool first, or an
        # earlier PUT may have landed while its reply was lost; either way
        # the re-fetched blob already holds the pending record as a
        # byte-identical line, so appending it again would write it twice
        # (#1237). The comparison is exact, not semantic: pending records
        # are serialised the same way they are written.
        seen = set(lines)
        for record in pending:
            line = json.dumps(record, sort_keys=True)
            if line not in seen:
                seen.add(line)
                lines.append(line)
        body = ("\n".join(lines[-KEEP:]) + "\n").encode("utf-8")

        # The body goes on stdin: as an argument it overran ARG_MAX (1 MB on
        # macOS) once the file passed about 768 KB, and every push failed (#949).
        request = {
            "message": "heartbeat: {} +{} record(s)".format(agent, len(pending)),
            "branch": BRANCH,
            "content": base64.b64encode(body).decode("ascii"),
        }
        if sha:
            request["sha"] = sha
        try:
            gh("api", "-X", "PUT",
               "repos/{}/contents/{}".format(REPO, _path(agent)),
               "--input", "-", input=json.dumps(request))
            # Only clear what came *from the spool*: a record spooled while this
            # was in flight must survive to the next drain, and an `extra` was
            # never in the spool to begin with.
            #
            # Skip it entirely when nothing came from the spool — which is exactly
            # the unwritable-spool case that `extra` exists for. Rewriting the file
            # there raises, and a push that already succeeded would be reported as
            # a lost record.
            if from_spool:
                remaining = _spooled(agent)[len(from_spool):]
                with open(_spool_path(agent), "w") as fh:
                    for r in remaining:
                        fh.write(json.dumps(r, sort_keys=True) + "\n")
            return
        except HeartbeatError:
            if attempt >= len(BACKOFF):
                raise
            time.sleep(BACKOFF[attempt])


def append(agent: str, record: Dict) -> str:
    """Record one heartbeat. Never raises.

    Returns `"pushed"`, `"spooled"` or `"lost"`. The caller proceeds either way —
    a run must not be stopped by its own bookkeeping — but it must be told the
    truth about what happened: `plan.md` requires that anything which cannot be
    spooled is **reported as lost rather than as saved**. A run told its record
    was kept when it was not is worse than a run told nothing, because the gap in
    the record then looks like a run that never happened.

    A record that could not be spooled is lost even if the push then succeeds:
    `_push` drains the spool, so it can only send what reached the spool. That is
    how two Codex runs on 2026-09-06 reported "spooled locally" while writing
    nothing anywhere — the sandbox denied writes outside its working directory.
    """
    spooled = True
    try:
        _spool(agent, record)
    except OSError:
        # A full disk, a bad path, or a sandbox that will not let us write here.
        # Not fatal on its own: try GitHub directly, since the spool exists to
        # survive GitHub being down, not the other way round.
        spooled = False
    try:
        _push(agent, extra=None if spooled else [record])
    except (HeartbeatError, OSError):
        return "spooled" if spooled else "lost"
    return "pushed"


def record_event(agent: str, run: Optional[str], outcome: str,
                 **fields) -> str:
    """Record a non-terminal outcome attached to an already-running session.

    A misfiled finish can be re-attached to its still-open run as an event,
    and the API-reserve gate can record its refusal the same way. An event
    makes the outcome visible to the watchdog without closing the run or
    making a second finish race with the routine.
    """
    if outcome not in OUTCOMES:
        raise ValueError("unknown heartbeat outcome: {}".format(outcome))
    record = {
        "run": run,
        "agent": agent,
        "phase": "event",
        "ts": int(time.time()),
        "outcome": outcome,
    }
    record.update(fields)
    kept = append(agent, record)
    _report(kept)
    return kept


def _api_cost_number(value):
    """Return a measured non-negative integer, or ``None`` if unreadable."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def record_api_cost(agent: str, run: Optional[str], api_cost: Dict) -> str:
    """Append one funnel command's API measurements to the heartbeat stream.

    It is an event rather than a mutation of the start record: several funnel
    processes can belong to one run, and appending preserves every command's
    contribution in the write-ahead spool.  An unattributed measurement is not
    useful to a later finish, so it is ignored rather than assigned by guess.
    """
    if not run or agent not in PROVIDERS:
        return "unattributed"
    values = api_cost if isinstance(api_cost, dict) else {}
    record = {
        "run": run,
        "agent": agent,
        "phase": "api_cost",
        "ts": int(time.time()),
        "api_cost": {
            name: _api_cost_number(values.get(name))
            for name in API_COST_FIELDS
        },
    }
    if isinstance(values.get("graphql_by_caller"), dict):
        record["graphql_by_caller"] = _clean_graphql_by_caller(
            values["graphql_by_caller"]
        )
    kept = append(agent, record)
    _report(kept)
    return kept


def record_binding(agent: str, run: str, do: str, work: str,
                   repo: Optional[str] = None) -> str:
    """Bind the work `funnel begin` issued to the run that received it (#497).

    Its own record, because the spool is append-only and the start record is
    already on its way to GitHub. Readers that count starts and finishes ignore
    the phase; `bindings()` is the reader for this one. Never raises for
    bookkeeping: `append` reports what happened and the run proceeds.
    """
    record = {
        "run": run,
        "agent": agent,
        "phase": "bind",
        "ts": int(time.time()),
        "do": do,
        "work": work,
    }
    if repo:
        # A ticket ref carries its repo; a review PR is a bare number, so the
        # repo travels beside it for anything that must write there (#668).
        record["repo"] = repo
    kept = append(agent, record)
    _report(kept)
    return kept


def record_job(agent: str, run: Optional[str], job: str) -> str:
    """Record the scheduled job identity for one run.

    Codex learns the automation name from the app's run settings after the
    start record is already written. Keep that identity as an append-only
    heartbeat fact so the eventual finish can carry it.
    """
    if not run or not isinstance(job, str) or not job.strip():
        return "unattributed"
    record = {
        "run": run,
        "agent": agent,
        "phase": "job",
        "ts": int(time.time()),
        "job": job.strip(),
    }
    kept = append(agent, record)
    _report(kept)
    return kept


def job_for_run(records: List[Dict], run: Optional[str],
                agent: str) -> Optional[str]:
    """Return one unambiguous scheduled job recorded for this run."""
    if not run:
        return None
    jobs = {
        row.get("job")
        for row in records
        if isinstance(row, dict)
        and row.get("phase") == "job"
        and row.get("run") == run
        and row.get("agent") == agent
        and isinstance(row.get("job"), str)
        and row["job"].strip()
    }
    return next(iter(jobs)) if len(jobs) == 1 else None


def session_id(agent: str) -> Optional[str]:
    """Return the current harness session id, when the harness exposes one."""
    for name in SESSION_ENVS.get(agent, ()):
        value = os.environ.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _record_session_id(record: Dict) -> Optional[str]:
    """Read the session id while tolerating the pre-feature record shape."""
    value = record.get("session_id")
    if value is None:
        value = record.get("session")
    return value if isinstance(value, str) and value else None


def close_rebegun_starts(agent: str, records: List[Dict], run: str,
                         current_session: Optional[str]) -> List[Dict]:
    """Close bound starts re-begun by this same harness session.

    A new begin is an honest blocked decline of the previous work: the earlier
    start no longer represents a run that may still be progressing. Matching
    both the agent and the harness session is required, and a missing session
    id never authorizes a close. The append-only finish is written before the
    caller records the fresh start.
    """
    if not run or not current_session:
        return []

    bound = bindings(records)
    closed = []
    for start in open_starts(records):
        old_run = start.get("run")
        if (
            start.get("agent") != agent
            or not old_run
            or _record_session_id(start) != current_session
            or old_run not in bound
        ):
            continue
        record = {
            "run": old_run,
            "agent": agent,
            "phase": "finish",
            "ts": int(time.time()),
            "outcome": "skipped-blocked",
            "note": "same-session re-begin by run {}; closed the prior "
                    "bound start as skipped-blocked".format(run),
            "re_begun_by": run,
            "session_id": current_session,
        }
        kept = append(agent, record)
        _report(kept)
        closed.append({
            "run": old_run,
            "agent": agent,
            "re_begun_by": run,
            "kept": kept,
        })
    return closed


def distinct_records(records: List[Dict]) -> List[Dict]:
    """``records`` with byte-identical duplicates removed, order kept.

    The canonical de-duplication for readers that sum values rather than count
    runs. A run legitimately writes several ``api_cost`` events — one per funnel
    command — which differ in their timestamps and numbers, so they must not be
    collapsed by run id. What is never legitimate is the same record twice, and
    that is exactly the shape the duplicate writes took: 250 of the 251
    duplicate-finish runs measured on 2026-09-21 were byte-identical with the
    same ``ts``.

    Compared by the same serialisation ``_push`` writes, so equality here means
    equality in the blob.
    """
    seen = set()
    found = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            key = json.dumps(record, sort_keys=True)
        except (TypeError, ValueError):
            found.append(record)
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(record)
    return found


def one_record_per_run(records: List[Dict]) -> List[Dict]:
    """The newest record per run id, for readers that count runs.

    The canonical counting rule: a count read from heartbeat records is a count
    of runs, not of rows. Before this existed, `agent_health` read 67 muse
    errors against 55 true runs — the alarm was reporting the write duplication
    rather than the lane's health.

    Records with no run id are kept as they are: they cannot be attributed, and
    dropping them would quietly lose history.
    """
    newest: Dict[str, Dict] = {}
    unattributed: List[Dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        run = record.get("run")
        if not run:
            unattributed.append(record)
            continue
        stamp = record.get("ts")
        stamp = float(stamp) if isinstance(stamp, (int, float)) and not isinstance(
            stamp, bool
        ) else float("-inf")
        current = newest.get(str(run))
        if current is None:
            newest[str(run)] = record
            continue
        current_stamp = current.get("ts")
        current_stamp = float(current_stamp) if isinstance(
            current_stamp, (int, float)
        ) and not isinstance(current_stamp, bool) else float("-inf")
        if stamp >= current_stamp:
            newest[str(run)] = record
    return list(newest.values()) + unattributed


def api_cost_for_run(records: List[Dict], run: Optional[str]) -> Dict[str, Optional[int]]:
    """Sum the per-command API events for one run, independently by field.

    No matching event means the run issued no funnel command.  If any command
    could not provide one field, that field stays null while a separately
    measurable field may still be summed.  Malformed telemetry is treated the
    same way; this function is diagnostic and must never make finish fail.
    """
    result = {name: None for name in API_COST_FIELDS}
    if not run:
        return result

    # Exact duplicates only: a run writes one api_cost event per funnel
    # command, so collapsing by run id would throw away real numbers, while
    # the same event twice is always a duplicate write (#1225).
    events = [
        record.get("api_cost")
        for record in distinct_records(records)
        if record.get("phase") == "api_cost" and record.get("run") == run
    ]
    if not events:
        return result

    for name in API_COST_FIELDS:
        values = []
        readable = True
        for event in events:
            if not isinstance(event, dict):
                readable = False
                break
            value = _api_cost_number(event.get(name))
            if value is None:
                readable = False
                break
            values.append(value)
        if readable:
            result[name] = sum(values)
    return result


def _clean_graphql_by_caller(value: Dict) -> Dict[str, Dict[str, Optional[int]]]:
    """Keep only the bounded caller schema and measured non-negative integers."""
    cleaned: Dict[str, Dict[str, Optional[int]]] = {}
    for name, entry in value.items():
        caller = (
            name if isinstance(name, str) and name in GRAPHQL_CALLER_NAMES
            else "unattributed"
        )
        if not isinstance(entry, dict):
            entry = {}
        target = cleaned.setdefault(caller, {
            "calls": None, "points": None, "remaining": None,
        })
        for field in GRAPHQL_CALLER_COST_FIELDS:
            number = _api_cost_number(entry.get(field))
            if number is not None:
                previous = target[field]
                target[field] = number if previous is None else previous + number
    return cleaned


def graphql_by_caller_for_run(
        records: List[Dict], run: Optional[str]
        ) -> Optional[Dict[str, Dict[str, Optional[int]]]]:
    """Aggregate per-command caller readings without hiding unreadable costs.

    `points` and `calls` sum across this run. `remaining` is the latest valid
    GraphQL response value for that caller, not a delta against the shared
    account window. Older api_cost events have no caller map; their known
    points, or an unreadable value, stay visible under `unattributed`.
    """
    if not run:
        return None
    events = [
        record for record in distinct_records(records)
        if record.get("phase") == "api_cost" and record.get("run") == run
    ]
    if not events:
        return None

    totals: Dict[str, Dict[str, object]] = {}
    for index, record in enumerate(events):
        raw = record.get("graphql_by_caller")
        if not isinstance(raw, dict):
            api = record.get("api_cost")
            api = api if isinstance(api, dict) else {}
            raw = {
                "unattributed": {
                    "calls": None,
                    "points": _api_cost_number(api.get("graphql_points")),
                    "remaining": None,
                }
            }
        for name, entry in _clean_graphql_by_caller(raw).items():
            bucket = totals.setdefault(name, {
                "calls": 0, "points": 0, "remaining": None,
                "calls_readable": True, "points_readable": True,
                "remaining_stamp": (float("-inf"), -1),
            })
            if not isinstance(entry, dict):
                entry = {}
            for field in ("calls", "points"):
                value = _api_cost_number(entry.get(field))
                if value is None:
                    bucket[field + "_readable"] = False
                elif bucket[field + "_readable"]:
                    bucket[field] = int(bucket[field]) + value
            remaining = _api_cost_number(entry.get("remaining"))
            if remaining is not None:
                stamp_value = record.get("ts")
                stamp = (
                    float(stamp_value)
                    if isinstance(stamp_value, (int, float))
                    and not isinstance(stamp_value, bool)
                    else float("-inf")
                )
                if (stamp, index) >= bucket["remaining_stamp"]:
                    bucket["remaining"] = remaining
                    bucket["remaining_stamp"] = (stamp, index)

    result: Dict[str, Dict[str, Optional[int]]] = {}
    caller_order = [*sorted(GRAPHQL_CALLER_NAMES - {"unattributed"}),
                    "unattributed"]
    for caller in caller_order:
        bucket = totals.get(caller)
        if bucket is None:
            continue
        result[caller] = {
            "calls": (
                int(bucket["calls"])
                if bucket["calls_readable"] else None
            ),
            "points": (
                int(bucket["points"])
                if bucket["points_readable"] else None
            ),
            "remaining": bucket["remaining"],
        }
    return result


def bindings(records: List[Dict]) -> Dict[str, Dict]:
    """The work each run was issued, by run id. The latest binding wins."""
    found: Dict[str, Dict] = {}
    rows = sorted(
        (r for r in records if r.get("phase") == "bind" and r.get("run")),
        key=lambda r: r.get("ts") or 0,
    )
    for rec in rows:
        found[rec["run"]] = {
            "do": rec.get("do"), "work": rec.get("work"), "ts": rec.get("ts"),
            "repo": rec.get("repo"),
        }
    return found


def verify_finish(records: List[Dict], run_id: str, *,
                  merged=None, work: Optional[str] = None):
    """Check a finish against the binding of the run it names.

    Returns ``None`` when nothing contradicts: no binding was recorded (a stop
    run, or a start that predates bindings -- instrumentation never gates the
    thing it instruments), or the finish names no work, or the named work is
    the bound work. Otherwise returns ``(right_run, reason)``: the still-open
    run that *was* issued the named work, or ``None`` when there is none, and
    a sentence saying what the named run was actually issued.

    Membership alone let a stale id from an earlier tick accept an outcome
    whenever that id was still open; the observed case (#497) was saved only
    because the stale run had already finished.
    """
    bound = bindings(records).get(run_id)
    if bound is None:
        return None
    if work:
        named = str(work)
        matches = str(bound.get("work")) == named
    elif merged is not None:
        named = "a merge of PR {}".format(merged)
        matches = (bound.get("do") == "review"
                   and str(bound.get("work")) == str(merged))
    else:
        return None
    if matches:
        return None
    opens = {r.get("run") for r in open_starts(records)}
    right = None
    for other, other_bound in bindings(records).items():
        if other == run_id or other not in opens:
            continue
        if work and str(other_bound.get("work")) == str(work):
            right = other
            break
        if (merged is not None and other_bound.get("do") == "review"
                and str(other_bound.get("work")) == str(merged)):
            right = other
            break
    reason = "run {} was issued {} {}, not {}".format(
        run_id, bound.get("do"), bound.get("work"), named)
    return right, reason


def _report(kept: str) -> None:
    """Say plainly where the record ended up. Silence means it reached GitHub."""
    if kept == "spooled":
        print(
            "heartbeat: GitHub unreachable; record spooled locally and will be "
            "pushed by a later run. Continuing.",
            file=sys.stderr,
        )
    elif kept == "lost":
        print(
            "heartbeat: RECORD LOST — the local spool at {} could not be written "
            "AND GitHub could not be reached. Nothing will recover this one. "
            "Continuing, but this run will look like it never happened.".format(
                SPOOL_DIR),
            file=sys.stderr,
        )


#: Nate's own working tree. No routine has any business writing it: the engineers
#: work in their own clones and the reviewers are read-only. Codex is stopped by
#: its sandbox; zcode has none, so for it this is a rule in a prompt.
#:
#: Recording the tree's state at both ends of a run does not prevent a violation —
#: nothing available here can — but it makes one **visible**. Without this a
#: routine could move his checkout and the only evidence would be his own
#: surprise, weeks later.
CANONICAL_REPO = "/Users/nateprich/.claude/command-center"


def runtime_state() -> Optional[Dict]:
    """Best-effort HEAD of the checkout this script is running from."""
    root = Path(__file__).resolve().parent
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            return None
        value = head.stdout.strip()
        if not value:
            return None
        return {"root": str(root), "head": value[:12]}
    except Exception:
        return None


def repo_state() -> Optional[Dict]:
    """HEAD and dirtiness of the canonical checkout. Best effort, never fatal."""
    try:
        head = subprocess.run(
            ["git", "-C", CANONICAL_REPO, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10)
        dirt = subprocess.run(
            ["git", "-C", CANONICAL_REPO, "status", "--porcelain"],
            capture_output=True, text=True, timeout=15)
        if head.returncode != 0:
            return None
        changed = [ln for ln in dirt.stdout.splitlines() if ln.strip()]
        return {"head": head.stdout.strip()[:12], "dirty": len(changed)}
    except Exception:
        return None


def _merged_pr(value):
    """A PR number, or nothing. Never a count.

    `--merged` is documented as "PR number merged unattended", and two agents
    read it as "how many": on 2026-09-07 zcode recorded `0` on runs that merged
    nothing and Muse recorded `1` for PR #96. Every value in the dataset was
    wrong, which makes the unattended-merge record `plan.md:156` requires
    meaningless.

    Coerced rather than rejected. This file's rule is that telemetry which can
    stop a run is worse than telemetry that is occasionally absent, so a value
    that cannot be a PR number is recorded as null with a warning, not raised.
    """
    if value in (None, ""):
        return None
    try:
        number = int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        print("heartbeat: --merged {!r} is not a PR number; recording null"
              .format(value), file=sys.stderr)
        return None
    if number <= 0:
        print("heartbeat: --merged {} looks like a count, not a PR number. It "
              "is the number of the PR that was merged; recording null."
              .format(number), file=sys.stderr)
        return None
    return number


def _session_files(agent: str) -> List[str]:
    """The agent's session files, newest first; for zcode, this run's only.

    zai-exec names its call log by `ZCODE_SESSION_ID`, one file per engine
    run, so zcode need not guess by mtime. It must not: a run that stopped
    before any model call would otherwise inherit the previous run's model and
    tokens (#1411). No session id, or no file for it, reads as unknown.
    """
    paths = glob.glob(os.path.expanduser(MODEL_SOURCES[agent]))
    if agent == "zcode":
        session = session_id("zcode")
        if not session:
            return []
        wanted = "model-io-{}.jsonl".format(session)
        paths = [path for path in paths if os.path.basename(path) == wanted]
    return sorted(paths, key=os.path.getmtime, reverse=True)


def detect_model(agent: str) -> Dict[str, Optional[str]]:
    """What model is running, from the agent's own session file.

    Best effort and never fatal — telemetry that can stop a run is worse than
    telemetry that is occasionally absent. A missing value is recorded as null
    rather than guessed.

    The newest session file by mtime is taken to be this run's. That is an
    inference: a run writes to its own transcript continuously, so it is almost
    always the newest, but two agents of the same kind running at once could
    cross. Recorded as `model_source: "detected"` so a later reader knows this
    was observed rather than declared.
    """
    found = {"provider": PROVIDERS.get(agent), "harness": HARNESSES.get(agent),
             "model": None, "reasoning_effort": None, "model_source": "detected"}
    try:
        paths = _session_files(agent)
        if not paths:
            return found
        if agent == "muse":
            # Whole-file JSON, not JSONL, and the id is nested under `modelId`
            # beside the token counts. Muse records no reasoning effort
            # anywhere, so it stays null: this file's own rule is that a
            # missing value is recorded rather than guessed, and the effort the
            # runner passes is a declaration, not an observation.
            snapshot = json.load(open(paths[0]))
            stack = [snapshot]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if isinstance(node.get("modelId"), str):
                        found["model"] = node["modelId"]
                        break
                    stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
            return found
        for line in open(paths[0]):
            if '"model"' not in line and '"effort"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") or {}
            message = record.get("message") or {}
            model = (payload.get("model") or message.get("model")
                     or record.get("model"))
            # zcode records the model as an object rather than a string.
            if isinstance(model, dict):
                if isinstance(model.get("modelId"), str):
                    found["model"] = model["modelId"]
                if isinstance(model.get("role"), str):
                    found["model_role"] = model["role"]
            elif isinstance(model, str):
                found["model"] = model
            effort = payload.get("effort") or (
                (payload.get("collaboration_mode") or {}).get("settings") or {}
            ).get("reasoning_effort")
            if isinstance(effort, str):
                found["reasoning_effort"] = effort
    except Exception:
        pass

    if agent == "claude" and found["reasoning_effort"] is None:
        # Claude's transcripts do not carry effort; its settings file does.
        try:
            settings = json.load(open(os.path.expanduser(CLAUDE_SETTINGS)))
            level = settings.get("effortLevel")
            if isinstance(level, str):
                found["reasoning_effort"] = level
        except Exception:
            pass
    return found


def _heartbeat_datetime(value) -> Optional[datetime]:
    """Turn a heartbeat epoch value into the boundary used by transcript reads."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _empty_token_usage() -> Dict[str, Optional[int]]:
    """Return an explicit unknown value for every token kind."""
    return {kind: None for kind in session_usage.TOKEN_KINDS}


def _start_for_run(records: List[Dict], run_id: Optional[str]) -> Dict:
    """Find the latest start row for a run, if the durable records have one."""
    if not run_id:
        return {}
    starts = [
        row for row in records
        if isinstance(row, dict)
        and row.get("phase") == "start"
        and row.get("run") == run_id
    ]
    if not starts:
        return {}
    return max(
        starts,
        key=lambda row: (
            isinstance(row.get("ts"), (int, float))
            and not isinstance(row.get("ts"), bool),
            row.get("ts") if isinstance(row.get("ts"), (int, float)) else 0,
        ),
    )


def token_usage_for_run(
    agent: str,
    records: List[Dict],
    run_id: Optional[str],
    finished_at,
) -> Dict[str, Optional[int]]:
    """Capture one run's four token kinds from its bound session, best effort.

    Token telemetry must never prevent a heartbeat finish.  A missing binding,
    session id, transcript or token kind therefore remains an explicit null
    rather than becoming a zero or raising from the instrumentation path.
    """
    unknown = _empty_token_usage()
    start = _start_for_run(records, run_id)
    session = start.get("session_id")
    if not isinstance(session, str) or not session:
        return unknown

    try:
        usage = session_usage.usage_for_session(
            agent,
            session,
            started_at=_heartbeat_datetime(start.get("ts")),
            finished_at=_heartbeat_datetime(finished_at),
        )
    except Exception:
        return unknown
    if not isinstance(usage, dict):
        return unknown

    found = {}
    for kind in session_usage.TOKEN_KINDS:
        value = usage.get(kind)
        found[kind] = (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else None
        )
    return found


def _number(value):
    """Return a non-negative integer token count, or nothing."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _first_number(mapping, *names):
    """Read the first present numeric field without treating zero as absent."""
    for name in names:
        if name in mapping:
            return _number(mapping.get(name))
    return None


def _input_pair(usage: Dict) -> Optional[tuple]:
    """Return total and fresh input from one harness usage object."""
    total = _first_number(
        usage, "total_input_tokens", "input_tokens", "inputTokens")
    fresh = _first_number(usage, "fresh_input_tokens", "freshInputTokens")
    cached = _first_number(
        usage, "cached_input_tokens", "cache_read_input_tokens", "cacheReadTokens")
    if fresh is None and total is not None and cached is not None:
        if cached > total:
            return None
        fresh = total - cached
    if total is None or fresh is None or fresh > total:
        return None
    return total, fresh


def _input_record(total: int, fresh: int) -> Dict[str, Optional[float]]:
    """Build the stored input metric without inventing a zero-denominator ratio."""
    return {
        "total_input_tokens": total,
        "fresh_input_tokens": fresh,
        "ratio": total / fresh if fresh else None,
    }


def _codex_input_usage(path: str) -> Optional[Dict[str, Optional[float]]]:
    """Read per-turn or cumulative input counts from one Codex rollout."""
    per_turn = []
    cumulative = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") or {}
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    pair = _input_pair(usage)
                    if pair is None:
                        return None
                    per_turn.append(pair)
                    continue
                info = payload.get("info") or {}
                total_usage = info.get("total_token_usage")
                if isinstance(total_usage, dict):
                    pair = _input_pair(total_usage)
                    if pair is None:
                        return None
                    cumulative.append(pair)
    except (OSError, ValueError):
        return None

    if per_turn:
        return _input_record(
            sum(total for total, _ in per_turn),
            sum(fresh for _, fresh in per_turn),
        )
    if cumulative:
        total, fresh = cumulative[-1]
        return _input_record(total, fresh)
    return None


def _zcode_input_usage(path: str) -> Optional[Dict[str, Optional[float]]]:
    """Read per-response input counts from one zcode model-io rollout."""
    total = 0
    fresh = 0
    found = False
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") != "model_io":
                    continue
                response = record.get("response") or {}
                usage = response.get("usage")
                if not isinstance(usage, dict):
                    return None
                pair = _input_pair(usage)
                if pair is None:
                    return None
                found = True
                total += pair[0]
                fresh += pair[1]
    except (OSError, ValueError):
        return None

    return _input_record(total, fresh) if found else None


def input_usage(agent: str) -> Optional[Dict[str, Optional[float]]]:
    """Read total and fresh input for the newest session, best effort.

    Codex and zcode expose these counts in their own session files. Other
    harnesses do not expose the complete pair used by this metric, so they
    produce no estimate and no heartbeat field.
    """
    try:
        paths = _session_files(agent)
        if not paths or agent not in ("codex", "zcode"):
            return None
        if agent == "codex":
            return _codex_input_usage(paths[0])
        return _zcode_input_usage(paths[0])
    except (KeyError, OSError):
        return None


def _finite_number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def usage_snapshot(agent: str) -> Optional[Dict]:
    """Best-effort usage reading. Never fatal — a heartbeat that cannot be
    written because usage was unreadable would hide the very run it documents."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import usage

        reading = usage.read_agent(agent, time.time())
        if not reading:
            return None
        if reading.get("unmetered"):
            return {
                "source": reading.get("source"),
                "unmetered": True,
            }
        # Both figures, not just the percentage. `resets_at` is what identifies
        # *which* five-hour window a run belonged to, and the idle gate in
        # usage.py needs that to ask whether this window was already open.
        snapshots = {}
        for name, window in reading.get("windows", {}).items():
            snapshot = {
                "used_percent": window.get("used_percent"),
                "resets_at": window.get("resets_at"),
            }
            # Muse's reader already prices provider usage at the standard
            # card. Carry that existing value through both heartbeat ends so
            # readers can measure the run's delta without duplicating rates.
            if agent == "muse":
                spent = _finite_number(window.get("spent_dollars"))
                if spent is not None and spent >= 0:
                    snapshot["spent_dollars"] = spent
            snapshots[name] = snapshot
        return snapshots
    except Exception:
        return None


def _record_timestamp(record: Dict) -> Optional[float]:
    return _finite_number(record.get("ts"))


def _muse_window_reading(record: Dict) -> Optional[tuple]:
    usage = record.get("usage")
    window = usage.get("seven_day") if isinstance(usage, dict) else None
    if not isinstance(window, dict):
        return None
    reset = _finite_number(window.get("resets_at"))
    spent = _finite_number(window.get("spent_dollars"))
    if reset is None or spent is None or reset < 0 or spent < 0:
        return None
    return reset, spent


def muse_window_consumption(records: List[Dict]) -> List[Dict[str, object]]:
    """Sum paired Muse usage deltas by the reset stamp both readings share.

    Muse currently resets Monday at 00:00 UTC. The recorded ``resets_at`` is
    the window identity, rather than a recomputed lattice value, so a future
    provider shift follows the stamp already carried by each reading.
    Repeated heartbeat rows and reads count a run once. Pairs that cross a
    reset, move backwards, or lack a readable standard-rate amount are omitted
    because their consumption cannot be assigned to one window safely.
    """
    starts: Dict[str, List[Dict]] = {}
    finishes: Dict[str, List[Dict]] = {}
    for record in distinct_records(records):
        if not isinstance(record, dict) or record.get("agent") != "muse":
            continue
        run = record.get("run")
        if not isinstance(run, str) or not run:
            continue
        phase = record.get("phase")
        if phase == "start":
            starts.setdefault(run, []).append(record)
        elif phase == "finish":
            finishes.setdefault(run, []).append(record)

    consumed_by_reset: Dict[float, List[float]] = {}
    for run, run_starts in starts.items():
        run_finishes = finishes.get(run, [])
        if not run_finishes:
            continue

        ordered_starts = sorted(
            (row for row in run_starts if _record_timestamp(row) is not None),
            key=lambda row: _record_timestamp(row),
        )
        if not ordered_starts:
            continue
        start = next(
            ((row, _muse_window_reading(row)) for row in ordered_starts
             if _muse_window_reading(row) is not None),
            None,
        )
        if start is None:
            continue
        start_row, start_reading = start
        start_at = _record_timestamp(start_row)
        if start_at is None or start_reading is None:
            continue

        ordered_finishes = sorted(
            (row for row in run_finishes if _record_timestamp(row) is not None),
            key=lambda row: _record_timestamp(row),
        )
        end = next(
            ((row, _muse_window_reading(row)) for row in ordered_finishes
             if _record_timestamp(row) >= start_at
             and _muse_window_reading(row) is not None),
            None,
        )
        if end is None:
            continue
        _, end_reading = end
        if end_reading is None or end_reading[0] != start_reading[0]:
            continue
        delta = end_reading[1] - start_reading[1]
        if delta < 0:
            continue
        consumed_by_reset.setdefault(start_reading[0], []).append(delta)

    return [
        {
            "resets_at": reset,
            "consumed_dollars": round(math.fsum(deltas), 6),
            "runs": len(deltas),
        }
        for reset, deltas in sorted(
            consumed_by_reset.items(), reverse=True
        )
    ]


def record_muse_quota_hit(run: Optional[str], reset_stamp: Optional[str],
                          raw_refusal: str,
                          observed_at: Optional[float] = None) -> str:
    """Record a provider refusal and the paired usage known for its window.

    Only a future reset exactly on Muse's observed Monday 00:00 UTC lattice
    can use the weekly paired-run total. An off-lattice, malformed, or stale
    stamp stays unclassified; the local hold may still use its existing
    fallback, but that fallback is never represented as a provider reset.
    """
    observed_at = time.time() if observed_at is None else observed_at
    if _finite_number(observed_at) is None:
        observed_at = time.time()
    observed_at = float(observed_at)

    reset_epoch = None
    seconds_to_reset = None
    weekly_lattice = False
    classification = "unclassified"
    degraded = []
    if not isinstance(reset_stamp, str) or not reset_stamp.strip():
        degraded.append("provider reset stamp is missing")
    else:
        try:
            parsed = datetime.fromisoformat(
                reset_stamp.strip().replace("Z", "+00:00")
            )
        except ValueError:
            parsed = None
        if parsed is None:
            degraded.append("provider reset stamp is malformed")
        elif parsed.tzinfo is None:
            degraded.append("provider reset stamp has no timezone")
        else:
            reset_utc = parsed.astimezone(timezone.utc)
            reset_epoch = reset_utc.timestamp()
            seconds_to_reset = reset_epoch - observed_at
            weekly_lattice = (
                reset_utc.weekday() == 0
                and reset_utc.hour == 0
                and reset_utc.minute == 0
                and reset_utc.second == 0
                and reset_utc.microsecond == 0
            )
            if not weekly_lattice:
                degraded.append(
                    "reset is off the Monday 00:00 UTC lattice; "
                    "window type remains unclassified"
                )
            elif seconds_to_reset <= 0:
                degraded.append(
                    "weekly reset stamp is not in the future; "
                    "window remains unclassified"
                )
            else:
                classification = "weekly"

    total_dollars = None
    total_runs = None
    if classification == "weekly" and reset_epoch is not None:
        read_failed = False
        try:
            records = read("muse", timeout=10)
            matches = [
                row for row in muse_window_consumption(records)
                if abs(float(row["resets_at"]) - reset_epoch) < 0.5
            ]
        except Exception:
            matches = []
            read_failed = True
        if matches:
            total_dollars = matches[0].get("consumed_dollars")
            total_runs = matches[0].get("runs")
        if total_dollars is None or total_runs is None:
            total_dollars = None
            total_runs = None
            if read_failed:
                degraded.append("paired Muse usage total could not be read")
            else:
                degraded.append(
                    "no paired Muse usage total matches this weekly reset"
                )

    record = {
        "run": run if isinstance(run, str) and run else None,
        "agent": "muse",
        "phase": "quota_hit",
        "ts": int(observed_at),
        "reset_stamp": reset_stamp,
        "seconds_to_reset": (
            round(seconds_to_reset, 3)
            if seconds_to_reset is not None else None
        ),
        "weekly_lattice": weekly_lattice,
        "raw_refusal": raw_refusal,
        "classification": classification,
        "anchored_window_total_dollars": total_dollars,
        "anchored_window_runs": total_runs,
        "degraded_note": "; ".join(degraded) if degraded else None,
    }
    kept = append("muse", record)
    _report(kept)
    return kept


def _parse_records(content: Optional[str]) -> List[Dict]:
    records = []
    for line in (content or "").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def read_github(agent: str, timeout: Optional[float] = None) -> List[Dict]:
    """Read only the durable records on GitHub, excluding the local spool."""
    if timeout is None:
        content, _ = _fetch(agent)
    else:
        content, _ = _fetch(agent, timeout=timeout)
    return _parse_records(content)


def read(agent: str, timeout: Optional[float] = None) -> List[Dict]:
    """Every record this machine knows about — pushed and still spooled."""
    return read_github(agent, timeout=timeout) + _spooled(agent)


def open_starts(records: List[Dict]) -> List[Dict]:
    """Starts with no finish, oldest first.

    A finish naming a run clears that run. An *unresolved* finish — written when
    the run id could not be determined — clears the **oldest** of its candidates.
    It means one of them ended, and the run that finishes is usually the one that
    started first. Clearing all of them would hide a run that really did die;
    clearing none would report a completed run as a start that never came back,
    which is the false alarm this whole mechanism exists to avoid.
    """
    starts = sorted(
        (r for r in records if r.get("phase") == "start" and r.get("run")),
        key=lambda r: r.get("ts") or 0,
    )
    named = {
        r.get("run") for r in records
        if r.get("phase") == "finish" and r.get("run")
    }
    opens = [r for r in starts if r.get("run") not in named]

    for rec in sorted(
        (r for r in records
         if r.get("phase") == "finish" and r.get("unresolved")),
        key=lambda r: r.get("ts") or 0,
    ):
        candidates = set(rec.get("candidates") or [])
        for i, start in enumerate(opens):
            if start.get("run") in candidates:
                opens.pop(i)
                break
    return opens


def is_rebegin_finish(record: Dict) -> bool:
    """Whether a finish is the synthetic close written by a second begin.

    ``skipped-blocked`` is also a normal outcome when a prerequisite is open,
    so the outcome alone cannot identify a same-session re-begin. The durable
    ``re_begun_by`` marker is written only by :func:`close_rebegun_starts` and
    is the part of the record that makes this classification unambiguous.
    """
    return (
        isinstance(record, dict)
        and record.get("phase") == "finish"
        and record.get("outcome") == "skipped-blocked"
        and isinstance(record.get("re_begun_by"), str)
        and bool(record.get("re_begun_by").strip())
    )


def run_summary(
    records: List[Dict],
    now: Optional[float] = None,
    window_seconds: Optional[int] = RUN_SUMMARY_WINDOW_SECONDS,
) -> Dict[str, int]:
    """Count starts, ordinary finishes, and same-session re-begins.

    Counts are per run id rather than per JSONL row, so a retry that repeats an
    already durable row cannot inflate the health line. A re-begin is a close
    for liveness purposes, but it is deliberately excluded from ``finishes``;
    otherwise a session that declined once and then worked would look like two
    completed runs. When ``now`` is omitted, all timestamped history is counted
    so fixture and offline callers can use the helper without a wall-clock
    assumption.
    """
    starts = set()
    finishes = set()
    re_begins = set()

    for record in records:
        if not isinstance(record, dict):
            continue
        run = record.get("run")
        if not run:
            continue
        timestamp = record.get("ts")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
        ):
            continue
        if now is not None:
            if timestamp > now:
                continue
            if (
                window_seconds is not None
                and timestamp < now - window_seconds
            ):
                continue

        phase = record.get("phase")
        if phase == "start":
            starts.add(run)
        elif phase == "finish":
            if is_rebegin_finish(record):
                re_begins.add(run)
            else:
                finishes.add(run)

    # A malformed or duplicated history must not put the same run in both
    # categories. The synthetic marker wins because it is the more specific
    # observation of what happened to that start.
    finishes.difference_update(re_begins)
    return {
        "starts": len(starts),
        "finishes": len(finishes),
        "re_begins": len(re_begins),
    }


def resolve_run(records: List[Dict], requested: Optional[str]):
    """Which run a `finish` belongs to: `(run_id, candidates)`.

    A `run_id` means it is known. A `None` run_id with candidates means it could
    not be determined, and the record must be written as unresolved rather than
    guessed — attributing one run's outcome to another is worse than admitting
    the ambiguity, because it makes one run look finished and leaves the other
    looking dead.

    Deliberately *not* "the most recent unfinished start". Replay the incident
    this fixes: A starts at 10:42, B starts at 10:45, A finishes at 10:51. The
    most recent open start is B, so A's outcome would land on B — the same bug
    through a new mechanism. The run that finishes is usually the one that
    started earlier, so there is no safe guess. Refusing to guess is the fix.
    """
    if requested:
        starts = {r.get("run") for r in records if r.get("phase") == "start"}
        # An empty set means the records could not be read at all — GitHub
        # unreachable and nothing spooled. Instrumentation must not gate the
        # thing it instruments, so trust the caller rather than refuse.
        if starts and requested not in starts:
            raise HeartbeatError(
                "no start recorded for run {} — refusing to record a finish "
                "against a run that never began".format(requested)
            )
        finished = {
            r.get("run") for r in records
            if r.get("phase") == "finish" and r.get("run")
        }
        if requested in finished:
            raise HeartbeatError(
                "run {} already finished — refusing to record a second "
                "finish for it".format(requested)
            )
        return requested, None

    opens = open_starts(records)
    if len(opens) == 1:
        return opens[0].get("run"), None
    return None, [r.get("run") for r in opens]


def unfinished(records: List[Dict], now: float, ttl_seconds: int) -> List[Dict]:
    """Runs that started, never finished, and are past the point of plausibility.

    This is the rate-limit death signature, and the reason start and finish are
    recorded separately.
    """
    return [
        r for r in open_starts(records)
        if now - (r.get("ts") or 0) > ttl_seconds
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="record that a run began")
    start.add_argument("--agent", required=True, choices=sorted(PROVIDERS))
    start.add_argument("--ticket", default=None)
    start.add_argument("--attempt", type=int, default=None,
                       help="which attempt at this ticket this run is, from 1")
    start.add_argument("--escalated-from", default=None,
                       help="the model this run escalated from, if any")
    start.add_argument("--tier", default=None,
                       choices=("standard", "escalated"),
                       help="the queue this run serves, when begin knows it; "
                            "lets a reader tell a lane's runs apart (#1320)")

    finish = sub.add_parser("finish", help="record how a run ended")
    finish.add_argument("--agent", required=True, choices=sorted(PROVIDERS))
    finish.add_argument(
        "--run", default=None,
        help="the id printed by `start`; if omitted, resolved from the records, "
             "and recorded as unattributable when more than one run is open",
    )
    finish.add_argument("--outcome", required=True, choices=OUTCOMES)
    finish.add_argument("--attempt", type=int, default=None)
    finish.add_argument("--escalated-from", default=None)
    finish.add_argument("--ci-green", choices=["yes", "no", "unknown"],
                        default=None)
    finish.add_argument("--review-result",
                        choices=["approved", "rejected", "none"], default=None)
    finish.add_argument(
        "--shape-status", choices=["Shaped", "Ready"], default=None,
        help="structured outcome written by shape-apply",
    )
    finish.add_argument(
        "--ticket-count", type=_non_negative_int, default=None,
        help="structured number of tickets created by breakdown-apply",
    )
    finish.add_argument(
        "--needs-decision", type=_optional_text, default=None,
        help="structured breakdown question; an empty value means none",
    )
    finish.add_argument(
        "--muse-call-record", type=_muse_call_record, default=None,
        help="JSON session-id list and calls-made count from Muse exec results",
    )
    finish.add_argument("--human-intervention", action="store_true",
                        help="Nate had to step in for this run to progress")
    finish.add_argument("--note", default=None)
    finish.add_argument(
        "--merged", default=None,
        help="PR number merged unattended; recorded so the brief can surface it",
    )
    finish.add_argument(
        "--work", default=None,
        help="the ticket ref or PR number this run was issued, as `funnel begin` "
             "printed it under `bound`; checked against the run's binding",
    )

    show = sub.add_parser("read", help="print an agent's records as JSON")
    show.add_argument("--agent", required=True, choices=sorted(PROVIDERS))

    args = parser.parse_args(argv)

    try:
        if args.command == "read":
            print(json.dumps(read(args.agent), indent=2))
            return 0

        if args.command == "start":
            run_id = uuid.uuid4().hex[:12]
            current_session = session_id(args.agent)
            records = read(args.agent)
            close_rebegun_starts(
                args.agent, records, run_id, current_session
            )
            kept = append(args.agent, {
                "run": run_id,
                "agent": args.agent,
                "phase": "start",
                "ts": int(time.time()),
                "ticket": args.ticket,
                "session_id": current_session,
                "usage": usage_snapshot(args.agent),
                "attempt": args.attempt,
                "escalated_from": args.escalated_from,
                "tier": args.tier,
                "repo": repo_state(),
                "runtime": runtime_state(),
                **detect_model(args.agent),
            })
            _report(kept)
            print(run_id)
            return 0

        records = read(args.agent)
        run_id, candidates = resolve_run(records, args.run)
        if args.run and run_id:
            misfiled = verify_finish(
                records, run_id, merged=_merged_pr(args.merged), work=args.work
            )
            if misfiled is not None:
                right, reason = misfiled
                # Never dropped, never filed under the wrong run: the outcome
                # becomes an event on the run that was issued the work, or an
                # unattributed event naming the candidates when none is open.
                event = {"misfiled_from": run_id, "note": args.note}
                if args.work:
                    event["work"] = args.work
                if _merged_pr(args.merged) is not None:
                    event["merged"] = _merged_pr(args.merged)
                if right is None:
                    event["candidates"] = [
                        r.get("run") for r in open_starts(records)
                    ]
                record_event(args.agent, right, args.outcome, **event)
                raise HeartbeatError(
                    "{}. {}".format(
                        reason,
                        "Use --run {} (still open); this outcome has been filed "
                        "as an event on it.".format(right)
                        if right else
                        "No open run is bound to that work; this outcome has "
                        "been filed as an unattributed event.",
                    )
                )
        if run_id is None:
            print(
                "heartbeat: could not tell which run this finishes ({} open). "
                "Recording it as unattributable rather than guessing.".format(
                    len(candidates) if candidates else 0),
                file=sys.stderr,
            )
        finished_at = time.time()
        runtime = runtime_state()
        record = {
            "run": run_id,
            "agent": args.agent,
            "phase": "finish",
            "ts": int(finished_at),
            "outcome": args.outcome,
            "note": args.note,
            # Recorded as a field, not scraped out of the note. plan.md requires
            # unattended merges to appear in the brief as a record, and a record
            # that has to be parsed out of prose is not a record.
            "merged": _merged_pr(args.merged),
            "usage": usage_snapshot(args.agent),
            "attempt": args.attempt,
            "escalated_from": args.escalated_from,
            "ci_green": args.ci_green,
            "review_result": args.review_result,
            "human_intervention_required": args.human_intervention or None,
            "repo": repo_state(),
            "runtime": runtime,
            "api_cost": api_cost_for_run(records, run_id),
            "graphql_by_caller": graphql_by_caller_for_run(
                records, run_id
            ),
            **detect_model(args.agent),
        }
        if args.agent != "muse":
            # Muse usage is derived from its session ids at read time. Do not
            # freeze a first-session snapshot into the finish record.
            record["token_usage"] = token_usage_for_run(
                args.agent, records, run_id, finished_at
            )
        job = job_for_run(records, run_id, args.agent)
        if job is not None:
            record["job"] = job
        if args.outcome == "errored":
            record["error_class"] = classify_error(args.note, runtime)
        if args.shape_status is not None:
            record["shape_status"] = args.shape_status
        if args.ticket_count is not None:
            record["ticket_count"] = args.ticket_count
            record["needs_decision"] = args.needs_decision
        if args.muse_call_record is not None:
            if args.agent != "muse":
                raise HeartbeatError("Muse call records require --agent muse")
            record["muse_calls_made"] = args.muse_call_record["calls_made"]
            # A one-call run is already bound by the start record's session_id;
            # keep the id-list shape stable and add the list when later calls
            # need distinct ids.
            if args.muse_call_record["calls_made"] > 1:
                record["muse_session_ids"] = args.muse_call_record["session_ids"]
        metric = input_usage(args.agent)
        if metric is not None:
            record["input_usage"] = metric
        if run_id is None:
            # The watchdog reads this as a finish, so a completed run is not
            # reported as dying, while the record still says the id is unknown
            # rather than asserting a wrong one.
            record["unresolved"] = True
            record["candidates"] = candidates
        _report(append(args.agent, record))
        return 0
    except HeartbeatError as exc:
        print("heartbeat: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
