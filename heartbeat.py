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
    "skipped-api-reserve",  # GraphQL budget below the reserve floor (#273)
    "prompt-drift",        # routine literal did not match the checked-in file
    "errored",             # tried and failed
]


#: Which pool an agent spends. Deliberately separate from the model: routing will
#: put more than one model on a pool, and the budget is per pool.
PROVIDERS = {"claude": "anthropic", "codex": "openai", "zcode": "zai",
             "muse": "meta"}

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
                **detect_model(args.agent),
            })
            _report(kept)
            print(run_id)
            return 0

        run_id, candidates = resolve_run(read(args.agent), args.run)
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
            **detect_model(args.agent),
        }
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
