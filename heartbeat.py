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
import glob
from pathlib import Path
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
    "skipped-nate-active",  # the five-hour window was already in use; Codex only
    "skipped-usage-unknown",  # could not read usage; failed closed
    "skipped-blocked",     # prerequisite has not landed; no change made
    "skipped-human-step",   # paused for a required human action
    "skipped-api-reserve",  # GraphQL budget below the reserve floor (#273)
    "prompt-drift",        # routine literal did not match the checked-in file
    "errored",             # tried and failed
]

#: The per-command GitHub measurements funnel records as non-terminal events.
#: Keep the names here so finish can aggregate each budget independently and
#: never turn an unreadable value into zero.
API_COST_FIELDS = ("graphql_points", "gh_calls")


#: Which pool an agent spends. Deliberately separate from the model: routing will
#: put more than one model on a pool, and the budget is per pool.
PROVIDERS = {"claude": "anthropic", "codex": "openai", "zcode": "zai",
             "muse": "meta"}

#: Agents whose schedules have been stopped on purpose. Their records stay
#: readable and every command still accepts them, so re-enabling is a schedule
#: paste and removing the name here; but the watchdog and `agent_health` must
#: not read their silence as a run that died. zcode was retired on 2026-09-09
#: by Nate's decision: measured over 24h it did work in 18 of 93 runs and was
#: refused on the z.ai pace line in 63, while Muse carried every job it had on
#: an unmetered pool (#431).
RETIRED_AGENTS = frozenset({"zcode"})

#: Which application ran it. Distinct from provider and model: one provider can
#: be reached through more than one harness, and harnesses differ in ways that
#: change outcomes — agent loop, tool selection, context handling, retries.
HARNESSES = {"claude": "claude-code", "codex": "codex", "zcode": "zcode",
             "muse": "muse-code"}

#: Where each agent records what it actually is. **Read, never asked.** A prompt
#: that reports its own model reports what it believes, and one confident wrong
#: answer silently poisons the routing dataset this exists to build.
#:
#: Both are files `usage.py` already globs for quota, so this adds no new source.
MODEL_SOURCES = {
    "claude": "~/.claude/projects/*/*.jsonl",
    "codex": "~/.codex/sessions/*/*/*/*.jsonl",
    "zcode": "~/.zcode/cli/rollout/*.jsonl",
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
    """
    from_spool = _spooled(agent)
    pending = from_spool + list(extra or [])
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

    `funnel begin` can discover prompt drift after the start record is written,
    but the routine must still do its normal work and write its ordinary finish
    record later. An event makes the fault visible to the watchdog without
    closing the run or making a second finish race with the routine.
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
    kept = append(agent, record)
    _report(kept)
    return kept


def record_binding(agent: str, run: str, do: str, work: str) -> str:
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
    kept = append(agent, record)
    _report(kept)
    return kept


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

    events = [
        record.get("api_cost")
        for record in records
        if isinstance(record, dict)
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
        paths = sorted(glob.glob(os.path.expanduser(MODEL_SOURCES[agent])),
                       key=os.path.getmtime, reverse=True)
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
        paths = sorted(
            glob.glob(os.path.expanduser(MODEL_SOURCES[agent])),
            key=os.path.getmtime,
            reverse=True,
        )
        if not paths or agent not in ("codex", "zcode"):
            return None
        if agent == "codex":
            return _codex_input_usage(paths[0])
        return _zcode_input_usage(paths[0])
    except (KeyError, OSError):
        return None


def usage_snapshot(agent: str) -> Optional[Dict]:
    """Best-effort usage reading. Never fatal — a heartbeat that cannot be
    written because usage was unreadable would hide the very run it documents."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import usage

        reading = usage.read_claude() if agent == "claude" else usage.read_codex()
        if not reading:
            return None
        # Both figures, not just the percentage. `resets_at` is what identifies
        # *which* five-hour window a run belonged to, and the idle gate in
        # usage.py needs that to ask whether this window was already open.
        return {
            name: {
                "used_percent": window.get("used_percent"),
                "resets_at": window.get("resets_at"),
            }
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
            kept = append(args.agent, {
                "run": run_id,
                "agent": args.agent,
                "phase": "start",
                "ts": int(time.time()),
                "ticket": args.ticket,
                "usage": usage_snapshot(args.agent),
                "attempt": args.attempt,
                "escalated_from": args.escalated_from,
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
        record = {
            "run": run_id,
            "agent": args.agent,
            "phase": "finish",
            "ts": int(time.time()),
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
            "runtime": runtime_state(),
            "api_cost": api_cost_for_run(records, run_id),
            **detect_model(args.agent),
        }
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
