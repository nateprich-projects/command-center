#!/usr/bin/env python3
"""prior_run.py — digest what a previous run was doing on a ticket.

A run killed mid-work by a rate limit leaves a half-finished branch and no
explanation. The branch shows *what* was done; this shows *what was intended*,
which is the part a diff cannot carry.

    prior_run.py 42                 # most recent prior run on ticket #42
    prior_run.py 42 --agent claude

Three rules govern how the output should be used, and the routines say so too:

1. **It is evidence of intent, never of truth.** The repository is what is true
   now. Verify every claim in here against the branch before acting on it.
2. **The match is heuristic.** A session that merely mentioned the ticket looks
   the same as one that worked on it. Check the `cwd` and the timing.
3. **It is local state**, on this machine only — a recovery aid, like the
   statusline cache, and deliberately not state of record. GitHub remains the
   state.

Output is capped, because re-reading a dead session spends the exact resource
the budget exists to protect.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from typing import Dict, List, Optional

CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")
CLAUDE_SESSIONS = os.path.expanduser("~/.claude/projects/*/*.jsonl")

#: Sessions older than this are not offered as a resume point. A run that died
#: days ago has been overtaken by events.
MAX_AGE_DAYS = 3

#: Hard caps. A digest that costs as much as redoing the work is worthless.
MAX_MESSAGES = 6
MAX_CHARS_PER_MESSAGE = 600
MAX_TOOL_CALLS = 15


def candidates(pattern: str, needles: List[str], max_age_days: int) -> List[str]:
    """Session files mentioning the ticket, newest first."""
    cutoff = time.time() - max_age_days * 86400
    found = []
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, errors="replace") as fh:
                blob = fh.read()
        except OSError:
            continue
        if any(needle in blob for needle in needles):
            found.append(path)
    return sorted(found, key=os.path.getmtime, reverse=True)


def _text(node) -> str:
    """Pull human-readable text out of the several shapes both vendors use."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for key in ("text", "content", "message", "input", "arguments"):
            if key in node:
                out = _text(node[key])
                if out:
                    return out
        return ""
    if isinstance(node, list):
        return " ".join(filter(None, (_text(v) for v in node)))
    return ""


def _read_codex(record):
    """Codex rollout: {type, payload:{role|type, ...}}, meta in session_meta."""
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else record

    if record.get("type") == "session_meta":
        return "meta", {
            k: payload.get(k)
            for k in ("id", "cwd", "timestamp", "originator")
            if payload.get(k)
        }

    role = payload.get("role")
    ptype = payload.get("type") or ""
    if role in ("user", "assistant"):
        return "message", {"role": role, "text": _text(payload).strip()}
    if "tool_call" in ptype or "tool_use" in ptype:
        name = payload.get("name") or payload.get("tool_name") or ptype
        argument = _text(payload.get("input") or payload.get("arguments") or "")
        return "tool", "{}: {}".format(name, argument[:160]).strip()
    return None, None


def _read_claude(record):
    """Claude Code transcript: {type:'user'|'assistant', message:{role,content}}.

    A different shape entirely from Codex's — the role is nested, assistant
    content is a list of blocks, and there is no session_meta because cwd and
    gitBranch ride on every record.
    """
    message = record.get("message")
    if not isinstance(message, dict):
        return None, None

    role = message.get("role")
    if role not in ("user", "assistant"):
        return None, None

    content = message.get("content")
    if isinstance(content, str):
        return "message", {"role": role, "text": content.strip()}

    parts, calls = [], []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text") or "")
        elif btype == "tool_use":
            calls.append(
                "{}: {}".format(block.get("name"), _text(block.get("input"))[:160]).strip()
            )
        # `thinking` blocks are deliberately skipped: they are the largest part
        # of a transcript and the text blocks already carry the intent.
    return "message+tools", (
        {"role": role, "text": " ".join(p for p in parts if p).strip()},
        calls,
    )


def digest(path: str, shape: Optional[str] = None) -> Dict:
    """Reduce a session to intent: what was asked, what was said, what was run.

    The two vendors' transcripts share no structure, so the shape is detected
    rather than assumed. Assuming produced a digest with zero messages that
    still rendered a confident-looking header.
    """
    meta: Dict = {}
    messages: List[Dict] = []
    tools: List[str] = []

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except ValueError:
                continue

            reader = _read_claude if (
                shape == "claude"
                or (shape is None and isinstance(record.get("message"), dict))
            ) else _read_codex

            # Model and effort are not set by a scheduled task — it inherits the
            # app's default at fire time — and nothing else records which was
            # used. They matter: both bear directly on review and breakdown
            # quality, and a default changed mid-week would otherwise alter the
            # routines' behaviour invisibly.
            if record.get("effort") and "effort" not in meta:
                meta["effort"] = record["effort"]
            if isinstance(record.get("message"), dict) and record["message"].get("model"):
                meta.setdefault("model", record["message"]["model"])

            # Claude carries cwd/gitBranch on every record instead of a header.
            if not meta.get("cwd") and record.get("cwd"):
                meta.update({
                    k: record.get(k)
                    for k in ("sessionId", "cwd", "timestamp", "gitBranch")
                    if record.get(k)
                })
                meta["id"] = meta.pop("sessionId", None)

            kind, value = reader(record)
            if kind == "meta" and value:
                meta = value
            elif kind == "message" and value and value["text"]:
                messages.append(value)
            elif kind == "message+tools":
                message, calls = value
                if message["text"]:
                    messages.append(message)
                tools.extend(calls)
            elif kind == "tool" and value:
                tools.append(value)

    # Both vendors inject large context blocks as pseudo-user turns (plugin
    # catalogues, environment dumps). They are not intent, and they would eat
    # the whole cap.
    messages = [m for m in messages if not m["text"].startswith("<")]

    return {
        "session": os.path.basename(path),
        "meta": meta,
        # The tail is what matters — a dying run's last acts say where it got to.
        "messages": messages[-MAX_MESSAGES:],
        "tool_calls": tools[-MAX_TOOL_CALLS:],
        "total_messages": len(messages),
        "total_tool_calls": len(tools),
    }


def stranded(cwd: Optional[str]) -> Optional[Dict]:
    """What the dead run left behind in its working directory.

    Codex desktop gives each session its own directory, so a resuming run does
    not inherit this one — which is why work must be pushed as it goes. The
    directory does survive, though, so naming it lets a human or a later run go
    and rescue anything stranded there.
    """
    if not cwd or not os.path.isdir(os.path.join(cwd, ".git")):
        return None
    import subprocess

    def git(*args):
        try:
            out = subprocess.run(
                ["git", "-C", cwd] + list(args),
                capture_output=True, text=True, timeout=10,
            )
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    branch = git("branch", "--show-current")
    dirty = [ln for ln in git("status", "--porcelain").splitlines() if ln]
    unpushed = [ln for ln in git("log", "--oneline", "@{u}..HEAD").splitlines() if ln]
    if not (dirty or unpushed):
        return {"branch": branch, "clean": True}
    return {
        "branch": branch,
        "clean": False,
        "uncommitted": len(dirty),
        "unpushed_commits": unpushed[:10],
    }


def render(d: Dict, ticket: str) -> str:
    meta = d.get("meta", {})
    lines = [
        "Prior run on #{} — evidence of intent, not of truth.".format(ticket),
        "Verify every claim below against the branch before acting on it.",
        "",
        "session : {}".format(d["session"]),
        "started : {}".format(meta.get("timestamp", "unknown")),
        "cwd     : {}".format(meta.get("cwd", "unknown")),
        "ran as  : {} (effort {})".format(
            meta.get("model", "unknown"), meta.get("effort", "unknown")),
        "size    : {} messages, {} tool calls".format(
            d["total_messages"], d["total_tool_calls"]
        ),
    ]
    if d["total_messages"] > MAX_MESSAGES:
        lines.append(
            "          (showing the last {} — the tail is where it got to)".format(
                MAX_MESSAGES
            )
        )
    lines.append("")

    lines.append("--- last exchanges ---")
    for message in d["messages"]:
        body = message["text"]
        if len(body) > MAX_CHARS_PER_MESSAGE:
            body = body[:MAX_CHARS_PER_MESSAGE] + " …[truncated]"
        lines.append("[{}] {}".format(message["role"], body))
        lines.append("")

    if d["tool_calls"]:
        lines.append("--- last tool calls ---")
        lines.extend("  " + call for call in d["tool_calls"])

    left = d.get("stranded")
    if left is not None:
        lines.append("")
        lines.append("--- what it left in its working directory ---")
        lines.append("  {}".format(meta.get("cwd")))
        if left.get("clean"):
            lines.append("  nothing stranded; branch {}".format(left.get("branch") or "?"))
        else:
            lines.append("  branch {}".format(left.get("branch") or "?"))
            lines.append("  {} uncommitted file(s)".format(left.get("uncommitted", 0)))
            for commit in left.get("unpushed_commits", []):
                lines.append("  unpushed: {}".format(commit))
            lines.append("")
            lines.append("  This directory is NOT the one a new run gets — each Codex")
            lines.append("  session works somewhere new. Anything above exists only here.")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ticket", help="issue number, or a full issue URL")
    parser.add_argument("--agent", choices=["codex", "claude"], default="codex")
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    ticket = args.ticket.rstrip("/").split("/")[-1].lstrip("#")
    # Both forms, because a session may reference either. Bare "#42" alone would
    # match far too much.
    needles = ["issues/{}".format(ticket), "#{}".format(ticket)]

    pattern = CODEX_SESSIONS if args.agent == "codex" else CLAUDE_SESSIONS
    matches = candidates(pattern, needles, args.max_age_days)
    if not matches:
        print(
            "no {} session in the last {} days mentions #{}".format(
                args.agent, args.max_age_days, ticket
            ),
            file=sys.stderr,
        )
        return 1

    d = digest(matches[0], shape=args.agent)
    d["stranded"] = stranded((d.get("meta") or {}).get("cwd"))
    if not d["messages"]:
        print(
            "warning: {} yielded no messages — the transcript shape may have "
            "changed".format(os.path.basename(matches[0])),
            file=sys.stderr,
        )
    if len(matches) > 1:
        d["other_candidates"] = [os.path.basename(p) for p in matches[1:4]]

    if args.json:
        print(json.dumps(d, indent=2))
    else:
        print(render(d, ticket))
        if len(matches) > 1:
            print(
                "\nnote: {} other sessions also mention #{}. The match is "
                "heuristic — confirm the cwd and timing above are the run you "
                "mean.".format(len(matches) - 1, ticket)
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
