#!/usr/bin/env python3
"""Assemble implementation evidence and finish one ticket (#815, #816).

The model changes files and returns one small structured answer.  This module
owns the protocol around that judgement: read the ticket packet, verify the
checkout, then either run its tests, commit and push, open the pull request,
or file the human step, or record the decline — and release the claim and
finish the heartbeat bound to the ticket.

The commit path stages an explicit file list only; it never uses a blanket add
or a status sweep. A pre-PR stray-file check also refuses to ship run scratch.

The package imports ``funnel`` and never the reverse.  ``funnel finish-ticket``
is a process-level CLI forwarder to the standalone entry point, so importing
this module cannot create a dependency cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402

from decline_classifier import (  # noqa: E402
    DECLINE_REVIEW_ROUTING_MARKER,
    classify_decline_reason,
    declined_pending_gate_answer_comment as _declined_pending_gate_answer_comment,
    declined_review_routing_comment as _declined_review_routing_comment,
    declined_unsatisfiable_acceptance_comment
    as _declined_unsatisfiable_acceptance_comment,
)
from engine import shape  # noqa: E402


class ImplementError(RuntimeError):
    """A packet or finish contract could not be completed safely."""


class StrayFileError(ImplementError):
    """The ticket branch contains run scratch that must not reach a PR."""

    def __init__(self, paths: Sequence[str]):
        # Kept apart from the message so a member-repo finish note can count
        # them without naming them (#1796).
        self.paths = list(paths)
        super().__init__(
            "pre-PR stray-file check refused; remove and re-stage run scratch: "
            "{}".format(", ".join(self.paths)))


class CommandTimeoutError(ImplementError):
    """A bounded finish subprocess was abandoned with partial output."""

    def __init__(self, command: Sequence[str], timeout: float,
                 captured_output: str = ""):
        self.timeout_seconds = timeout
        self.captured_output = captured_output
        self.finish_recorded = False
        super().__init__("{} timed out after {:g}s".format(
            _command_label(command), timeout))


class SupersededRunError(ImplementError):
    """A finish cannot establish that this run still owns the ticket claim."""

    def __init__(self, ref: str, reason: str):
        self.ref = ref
        self.reason = reason
        super().__init__("superseded {}: {}".format(ref, reason))


class MergedSuiteError(ImplementError):
    """The suite fails on the merge with origin/main where main does not.

    Its text is shaped like pytest's (``FAILED <id>`` lines and a counts
    line), so the failure note and ticket comment read it as they read a
    head-only run (#1804).
    """


class MergeConflictError(ImplementError):
    """The ticket's work does not merge with origin/main (#1804)."""

    def __init__(self, base: str, paths: Sequence[str]):
        # Kept apart from the message so a member-repo finish note can count
        # them without naming them (#1796).
        self.paths = list(paths)
        super().__init__("merge with origin/main {} conflicts: {}".format(
            base[:12], ", ".join(self.paths)))



#: The blocked_on_human reason enum from the implement answer schema (#794):
#: four capabilities only Nate has, and one a Claude Code session on the Mac
#: mini has (#1901). These are prose in the created sub-issue; the machine
#: signal is the Needs Project field written alongside, which
#: ``HUMAN_STEP_NEEDS`` maps each reason to. Kept here (not in funnel) because
#: only this job validates the implement answer.
CLAUDE_CODE_ENVIRONMENT_REASON = "a Claude Code environment"

BLOCKED_ON_HUMAN_REASONS = (
    "an app UI with no API",
    "entering a credential",
    "an account or billing setting",
    "physical access to a machine",
    CLAUDE_CODE_ENVIRONMENT_REASON,
)

#: The Needs value each blocked reason files its step with. A step a Claude
#: Code session can do goes to a session, not to Nate (#1901).
HUMAN_STEP_NEEDS = {
    reason: ("claude-code-environment"
             if reason == CLAUDE_CODE_ENVIRONMENT_REASON else "human")
    for reason in BLOCKED_ON_HUMAN_REASONS
}


def fetch_ticket(repo: str, number: int) -> dict:
    """Read the ticket and its native parent identity."""
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo, "--json",
        "number,title,url,body,parent",
    )
    if not data:
        raise funnel.GitHubError(
            "could not read ticket {}#{}".format(repo, number)
        )
    data["ref"] = "{}#{}".format(repo, number)
    return data


def parent_repo(repo: str, ticket: dict) -> str:
    """Return the repository that holds the ticket's parent.

    A parent may live in another repository — command-center#1054 parents a
    ticket in every member repo — so the parent's own URL decides, and the
    ticket's repository is only the fallback when that URL is absent.
    """
    url = (ticket.get("parent") or {}).get("url")
    match = re.match(r"https://github\.com/([^/]+/[^/]+)/issues/\d+", url or "")
    return match.group(1) if match else repo


def fetch_plan(repo: str, ticket: dict) -> Optional[dict]:
    """Read the project issue whose body is this ticket's settled plan."""
    parent = ticket.get("parent") or {}
    number = parent.get("number")
    if not isinstance(number, int):
        return None
    repo = parent_repo(repo, ticket)
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo, "--json",
        "number,title,url,body,state",
    )
    if not data:
        raise funnel.GitHubError(
            "could not read plan {}#{}".format(repo, number)
        )
    data["ref"] = "{}#{}".format(repo, number)
    return data


def fetch_verdict_blocking(repo: str, number: int) -> dict:
    """Read the newest verdict for the ticket's open PR, when one exists.

    ``--head`` matches the branch name in any fork, so only the funnel's own
    PR is the ticket's (#1794): a stranger's PR named ``ticket/<n>`` must not
    hand the engineer its number, head or review.
    """
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--head", "ticket/{}".format(number), "--json",
        "number,headRefOid,updatedAt," + funnel.PR_TRUST_JSON_FIELDS,
        "--limit", "10",
    )
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError(
            "could not list open PRs for {}#{}".format(repo, number)
        )
    rows = [row for row in rows if funnel.is_funnel_pr(repo, row)]
    if not rows:
        return {"pr": None, "head_sha": None, "verdict": None,
                "blocking": []}
    row = max(rows, key=lambda value: value.get("updatedAt") or "")
    verdict = funnel.latest_verdict(repo, int(row["number"]))
    blocking = (verdict or {}).get("blocking") or []
    return {
        "pr": row["number"],
        "head_sha": row.get("headRefOid"),
        "verdict": (verdict or {}).get("verdict"),
        "verdict_head_sha": (verdict or {}).get("head_sha"),
        "blocking": list(blocking),
    }


def fetch_prior_run(number: int, agent: str = "codex") -> Optional[dict]:
    """Return prior_run.py's structured digest, or None when no run exists."""
    script = pathlib.Path(__file__).resolve().parent.parent / "prior_run.py"
    proc = subprocess.run(
        [sys.executable, str(script), str(number), "--agent", agent, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise ImplementError(
            "prior-run digest failed: {}".format(
                (proc.stderr or proc.stdout or "unknown error").strip()
            )
        )
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ImplementError("prior-run digest was not valid JSON: {}".format(exc))
    if not isinstance(data, dict):
        raise ImplementError("prior-run digest was not a JSON object")
    return data


#: How much of a target repo's AGENTS.md the packet carries. A packet is read
#: by a model with a finite context, and a repo could in principle hold a very
#: long instruction file; truncating with a visible marker is honest, while
#: silently sending part of it is not.
MAX_AGENTS_MD_CHARS = 40_000

#: What the marker says when the text was cut. Load-bearing: a reader must be
#: able to tell "these are the rules" from "these are the first part of them".
AGENTS_MD_TRUNCATION_MARKER = (
    "\n\n…[AGENTS.md truncated after {} characters; the rest was not sent]"
)


def fetch_agents_md(repo: str) -> Tuple[str, bool, bool]:
    """The target repo's AGENTS.md as ``(text, missing, truncated)``.

    The same read shaping already makes (`engine/shape.fetch_repo_text`), so
    an implementer is shown the rules the reviewer will hold it to. Before
    this, an implementer working a member repo saw its ticket and plan but not
    the repo's own canonical instruction file — the one AGENTS.md itself calls
    the source of truth for every agent working there.

    Missing is recorded as an absent fact, not an error: the ticket is still
    implementable, and a repo without an AGENTS.md is a repo without one. No
    retry, because there is nothing to retry — a 404 is the answer.
    """
    text, missing = shape.fetch_repo_text(repo, "AGENTS.md")
    if missing or not text:
        return "", bool(missing), False
    if len(text) <= MAX_AGENTS_MD_CHARS:
        return text, False, False
    cut = text[:MAX_AGENTS_MD_CHARS] + AGENTS_MD_TRUNCATION_MARKER.format(
        MAX_AGENTS_MD_CHARS
    )
    return cut, False, True


def fetch_ticket_comments(repo: str, number: int) -> List[Dict[str, object]]:
    """Read the ticket's whole comment thread, oldest first; fail closed.

    A member-repo test failure's ids are posted to its ticket rather than to
    the public heartbeat note (#1796), so this is how the next run learns
    what failed. The shape and breakdown packets' paginated reader (#1549,
    #1605): a short read raises rather than handing over part of a thread.
    """
    return funnel.read_issue_comments(repo, number)


def build_packet(*, repo: str, ticket: dict, plan: Optional[dict],
                 verdict: dict, prior_run: Optional[dict],
                 agents_md: str = "", agents_md_missing: bool = False,
                 agents_md_truncated: bool = False,
                 issue_comments: Optional[Sequence[Dict]] = None) -> dict:
    """Build one JSON-serialisable implementation packet from fetched facts."""
    packet = {
        "repo": repo,
        "ticket": ticket,
        "plan": plan,
        "verdict": verdict,
        "prior_run": prior_run,
        # The target repo's canonical rules, carried as text and nothing more.
        # No credential is read, no flag changes, and nothing under .claude/ is
        # executed or fetched: this is one file's contents in a JSON field.
        "agents_md": agents_md,
        "agents_md_missing": agents_md_missing,
        "agents_md_truncated": agents_md_truncated,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    # Rendered as the breakdown packet renders its parent's thread (#1605),
    # and absent when the ticket has no comments, so that packet keeps its
    # old shape.
    issue_thread = shape.issue_thread_section(
        issue_comments if issue_comments is not None else [])
    if issue_thread is not None:
        packet["issue_thread"] = issue_thread
    return packet


def collect(repo: Optional[str], number: int, *, agent: str = "codex") -> dict:
    """Fetch every read-only input needed to implement one ticket."""
    resolved = funnel.resolve_repo(repo)
    ticket = fetch_ticket(resolved, number)
    issue_comments = fetch_ticket_comments(resolved, number)
    # The ticket's own repo, which for a member-repo ticket is not this one.
    agents_md, agents_md_missing, agents_md_truncated = fetch_agents_md(
        parent_repo(resolved, ticket)
    )
    return build_packet(
        repo=resolved,
        ticket=ticket,
        plan=fetch_plan(resolved, ticket),
        verdict=fetch_verdict_blocking(resolved, number),
        prior_run=fetch_prior_run(number, agent),
        agents_md=agents_md,
        agents_md_missing=agents_md_missing,
        agents_md_truncated=agents_md_truncated,
        issue_comments=issue_comments,
    )


def packet_main(argv: Optional[Sequence[str]] = None) -> int:
    """Print one implementation packet as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only implementation packet for a ticket"
    )
    parser.add_argument("ticket", type=int, help="ticket number")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--agent", default="codex",
                        choices=("codex", "muse", "claude"))
    args = parser.parse_args(argv)
    try:
        packet = collect(args.repo, args.ticket, agent=args.agent)
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("implement-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


def parse_answer(raw: str) -> dict:
    """Validate one structured implementation answer.

    Exactly one shape: the done answer, the blocked_on_human answer, or
    the declined answer. Anything mixed or unknown fails closed.
    """
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise ImplementError("answer is not valid JSON: {}".format(exc))
    if not isinstance(answer, dict):
        raise ImplementError("answer must be a JSON object")
    if "done" in answer:
        return _read_done_answer(answer)
    if set(answer) == {"blocked_on_human"}:
        return {"blocked_on_human": _read_blocked_answer(
            answer["blocked_on_human"])}
    if set(answer) == {"declined"}:
        reason = answer["declined"]
        if not isinstance(reason, str) or not reason.strip():
            raise ImplementError("declined reason must be a non-empty string")
        return {"declined": reason.strip()}
    raise ImplementError(
        "answer must be a done, blocked_on_human, or declined answer")


def read_answer(path: str) -> dict:
    """Read a structured answer from ``path`` (or stdin) and validate it."""
    try:
        raw = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text()
    except OSError as exc:
        raise ImplementError("cannot read answer {}: {}".format(path, exc))
    return parse_answer(raw)


def _read_done_answer(answer: dict) -> dict:
    """Validate the happy-path model answer."""
    if answer.get("done") is not True:
        raise ImplementError("happy-path answer must set done to true")
    summary = answer.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ImplementError("answer summary must be a non-empty string")
    departures = answer.get("departures")
    if not isinstance(departures, list) or not all(
        isinstance(value, str) and value.strip() for value in departures
    ):
        raise ImplementError("answer departures must be a list of non-empty strings")
    evidence = answer.get("evidence")
    if "evidence" in answer and (
        not isinstance(evidence, list) or not evidence
        or not all(isinstance(value, str) and value.strip()
                   for value in evidence)
    ):
        raise ImplementError(
            "answer evidence must be a non-empty list of non-empty GitHub URLs")
    # Where the implementer says review should look hardest (#1807). It is
    # optional and may be empty, so an answer without it parses as before.
    risks = answer.get("risks")
    if "risks" in answer and (not isinstance(risks, list) or not all(
        isinstance(value, str) and value.strip() for value in risks
    )):
        raise ImplementError("answer risks must be a list of non-empty strings")
    extra = sorted(
        set(answer) - {"done", "summary", "departures", "evidence", "risks"})
    if extra:
        raise ImplementError("answer has unknown field(s): {}".format(", ".join(extra)))
    found = {
        "done": True,
        "summary": summary.strip(),
        "departures": [value.strip() for value in departures],
    }
    if "evidence" in answer:
        found["evidence"] = [value.strip() for value in evidence]
    if "risks" in answer:
        found["risks"] = [value.strip() for value in risks]
    return found


def _read_blocked_answer(value: object) -> dict:
    """Validate the blocked_on_human answer against the allowlisted reasons."""
    if not isinstance(value, dict):
        raise ImplementError("blocked_on_human must be an object")
    reason = value.get("reason")
    if reason not in BLOCKED_ON_HUMAN_REASONS:
        raise ImplementError(
            "blocked_on_human reason must be one of: {}".format(
                ", ".join(BLOCKED_ON_HUMAN_REASONS)))
    action = value.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ImplementError("blocked_on_human action must be a non-empty string")
    extra = sorted(set(value) - {"reason", "action"})
    if extra:
        raise ImplementError(
            "blocked_on_human has unknown field(s): {}".format(
                ", ".join(extra)))
    return {"reason": reason, "action": action.strip()}


CLAIM_TTL_SECONDS = 2 * 60 * 60

# Measured 2026-09-28 on this checkout: local Git inventory calls took at most
# 0.0054s, disposable-repo add/commit/merge at most 0.0216s, and GitHub
# ls-remote/fetch 0.9282s/1.0166s. The caps retain ample headroom for repo and
# network variance while staying below the two-hour claim TTL.
LOCAL_GIT_TIMEOUT_SECONDS = 2 * 60
REMOTE_GIT_TIMEOUT_SECONDS = 10 * 60

# The latest green CI run (#36331008053) spent 6m54s in pytest. Allow more
# than six times that duration while staying well below the two-hour claim TTL.
TEST_COMMAND_TIMEOUT_SECONDS = 45 * 60
# This checkout's CI syntax-compile command took 0.0800s across seven runs;
# compileall took 0.1176s. Keep compilation separate from the full test suite.
COMPILE_COMMAND_TIMEOUT_SECONDS = 2 * 60
# The Python version probe took at most 0.0160s across seven runs. Unknown
# helper commands share this measured cap; the live finish path has no
# unclassified command site.
INTERPRETER_PROBE_TIMEOUT_SECONDS = 20
OTHER_COMMAND_TIMEOUT_SECONDS = INTERPRETER_PROBE_TIMEOUT_SECONDS


def _command_label(command: Sequence[str]) -> str:
    """Name a timed-out command without echoing arbitrary command arguments."""
    if not command:
        return "subprocess"
    program = os.path.basename(command[0])
    if program == "git":
        operation = command[1] if len(command) > 1 else "operation"
        return "git {}".format(operation)
    if program in ("make", "npm"):
        operation = command[1] if len(command) > 1 else "command"
        return "{} {}".format(program, operation)
    if program in ("pytest", "py.test") or "pytest" in command:
        return "pytest"
    return "{} command".format(program or "subprocess")


def _is_compile_command(command: Sequence[str]) -> bool:
    """Whether the command only compiles source rather than running tests."""
    return any(
        "compileall" in str(part).lower()
        or re.search(r"\bcompile\s*\(", str(part))
        for part in command
    )


def _command_timeout_seconds(command: Sequence[str]) -> float:
    """Choose a per-invocation bound from the observed finish workload."""
    if command and os.path.basename(command[0]) == "git":
        if len(command) > 1 and command[1] in ("fetch", "ls-remote", "push"):
            return REMOTE_GIT_TIMEOUT_SECONDS
        return LOCAL_GIT_TIMEOUT_SECONDS
    rendered = " ".join(str(part) for part in command)
    if re.search(r"(?<![\w.])(?:make|npm|pytest|py\.test)(?![\w.])",
                 rendered):
        return TEST_COMMAND_TIMEOUT_SECONDS
    if any("sys.version_info" in str(part) for part in command):
        return INTERPRETER_PROBE_TIMEOUT_SECONDS
    if _is_compile_command(command):
        return COMPILE_COMMAND_TIMEOUT_SECONDS
    return OTHER_COMMAND_TIMEOUT_SECONDS


def _abandon_child(proc: subprocess.Popen) -> None:
    """Kill the command's process group and close our pipes without waiting."""
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        # It may have exited between communicate's timeout and the kill.
        pass
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _run(command: Sequence[str], *, cwd: pathlib.Path,
         env: Optional[dict] = None, input_text: Optional[str] = None,
         check: bool = True, timeout: Optional[float] = None
         ) -> subprocess.CompletedProcess:
    """Run one bounded local command and turn failures into concise errors."""
    bound = (_command_timeout_seconds(command)
             if timeout is None else timeout)
    proc = subprocess.Popen(
        list(command), cwd=str(cwd), env=env,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=bound)
    except subprocess.TimeoutExpired as exc:
        output = []
        for part in (exc.output, exc.stderr):
            if isinstance(part, bytes):
                part = part.decode("utf-8", errors="replace")
            elif part is not None:
                part = str(part)
            if part:
                output.append(part)
        _abandon_child(proc)
        raise CommandTimeoutError(
            command, bound, captured_output="\n".join(output),
        ) from None
    completed = subprocess.CompletedProcess(
        list(command), proc.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        # Both streams: `compileall` reports on stdout while make prints only
        # its own "Error 1" on stderr, and that line alone hid the cause (#953).
        detail = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr)
            if part and part.strip())
        raise ImplementError(
            "{} failed{}".format(
                shlex.join(command), ": " + detail if detail else ""
            )
        )
    return completed


def checkout_context(cwd: Optional[os.PathLike] = None) -> dict:
    """Derive ticket identity from a real clone on ticket/<n>."""
    here = pathlib.Path(cwd or os.getcwd()).resolve()
    root = pathlib.Path(
        _run(["git", "rev-parse", "--show-toplevel"], cwd=here,
             timeout=LOCAL_GIT_TIMEOUT_SECONDS).stdout.strip()
    ).resolve()
    branch = _run(["git", "branch", "--show-current"], cwd=root,
                  timeout=LOCAL_GIT_TIMEOUT_SECONDS).stdout.strip()
    match = re.fullmatch(r"ticket/([1-9][0-9]*)", branch)
    if match is None:
        raise ImplementError(
            "finish-ticket requires branch ticket/<number>, found {!r}".format(branch)
        )
    _run(["git", "remote", "get-url", "origin"], cwd=root,
         timeout=LOCAL_GIT_TIMEOUT_SECONDS)
    return {"root": root, "branch": branch, "number": int(match.group(1))}


def _toml_single_line_string(value: str) -> Optional[str]:
    """Parse one single-line TOML basic or literal string, or None.

    Anything else shaped — a number, array, inline table, or a triple-quoted
    multi-line string — is not an override. Backslashes stay literal except
    the two needed to write the string itself, so Windows-style paths and
    regex escapes in a command survive unharmed.
    """
    if len(value) >= 2 and value[0] in "\"'":
        quote = value[0]
        chars: List[str] = []
        index = 1
        while index < len(value):
            char = value[index]
            if quote == '"' and char == "\\" and index + 1 < len(value):
                follower = value[index + 1]
                if follower == "\\":
                    chars.append("\\")
                elif follower == '"':
                    chars.append('"')
                else:
                    chars.append("\\" + follower)
                index += 2
                continue
            if char == quote:
                tail = value[index + 1:]
                if tail.strip() and not tail.strip().startswith("#"):
                    return None
                text = "".join(chars)
                return text if text.strip() else None
            if char in "\n\r":
                return None
            chars.append(char)
            index += 1
    return None


def pyproject_test_command(root: pathlib.Path) -> Optional[str]:
    """Return the raw ``[tool.command-center] test`` string, or None.

    The engine runs on interpreters without ``tomllib`` and installs no
    dependencies, so this reads one single-line string key with a section
    scan instead of parsing TOML. A missing file, a missing section, or a
    value that is not a single-line quoted string is not an override.
    """
    try:
        text = (root / "pyproject.toml").read_text()
    except OSError:
        return None
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = re.fullmatch(
                r"\[\s*tool\.command-center\s*\](\s*#.*)?", stripped
            ) is not None
            continue
        if not in_section:
            continue
        match = re.match(r"test\b\s*=\s*(.+?)\s*$", stripped)
        if match:
            return _toml_single_line_string(match.group(1))
    return None


def _strip_yaml_comment(text: str) -> str:
    """Cut a YAML ``#`` comment, honouring single and double quotes."""
    quote: Optional[str] = None
    for index, char in enumerate(text):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or text[index - 1] in " \t"):
            return text[:index].rstrip()
    return text.rstrip()


def _strip_yaml_quotes(text: str) -> str:
    """Strip one pair of matching YAML quotes, without unescaping."""
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        return text[1:-1]
    return text


def _workflow_steps(text: str) -> List[Tuple[str, str]]:
    """Split one workflow file into (name, run) step pairs.

    A minimal indentation scan, not a YAML parser: every ``-`` item starts a
    chunk, where a dash line deeper than the open chunk belongs to its block
    scalar rather than starting a new step. The chunk's first ``name:`` and
    first ``run:`` win; block scalars (``|``, ``>``) and plain continuations
    are collected by indentation, and chunks without a ``run:`` are dropped.
    """
    chunks: List[Tuple[int, List[Tuple[int, str]]]] = []
    current: Optional[Tuple[int, List[Tuple[int, str]]]] = None
    for line in text.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        lstripped = line.strip()
        if lstripped == "-":
            dash_content: Optional[str] = ""
        elif lstripped.startswith("- ") or lstripped.startswith("-\t"):
            dash_content = lstripped[2:]
        else:
            dash_content = None
        if dash_content is not None:
            if current is None or indent <= current[0]:
                current = (indent, [])
                chunks.append(current)
                if dash_content:
                    current[1].append((indent, dash_content))
                continue
        if current is None or indent <= current[0]:
            current = None
            continue
        current[1].append((indent, lstripped))
    steps = []
    for _, chunk in chunks:
        name = ""
        run: Optional[str] = None
        for position, (indent, content) in enumerate(chunk):
            name_match = re.match(r"name\s*:\s*(.*)$", content)
            if name_match and not name:
                name = _strip_yaml_quotes(
                    _strip_yaml_comment(name_match.group(1).strip()))
            run_match = re.match(r"run\s*:\s*(.*)$", content)
            if run_match and run is None:
                run = _workflow_run_value(
                    run_match.group(1).strip(), indent,
                    [entry for entry in chunk[position + 1:]])
        if run is not None:
            steps.append((name, run))
    return steps


def _workflow_run_value(rest: str, indent: int,
                        following: Sequence[Tuple[int, str]]) -> str:
    """Read one ``run:`` value with its block-scalar continuation lines."""
    if re.fullmatch(r"[|>][+\-0-9]*(\s*#.*)?", rest):
        deeper = [content for (found, content) in following
                  if found > indent]
        if rest.startswith("|"):
            return "\n".join(deeper).strip()
        return " ".join(line.strip() for line in deeper if line.strip())
    if not rest:
        deeper = [content for (found, content) in following
                  if found > indent]
        return " ".join(line.strip() for line in deeper if line.strip())
    return _strip_yaml_quotes(_strip_yaml_comment(rest))


def _shell_command(text: str) -> List[str]:
    """Split one CI ``run:`` value into argv, or run it under ``sh -c``.

    CI ``run:`` values are shell by definition. Plain word-shaped lines split
    into argv so the recorded command stays readable; multi-line scripts,
    lines with shell metacharacters, and unbalanced quotes run under
    ``sh -c`` exactly as the workflow would run them. GitHub ``${{ }}``
    expressions are not expanded: such a step fails loudly instead of
    running with a silently empty value.
    """
    stripped = text.strip()
    if "\n" in stripped or re.search(r"[|&;<>()`$]", stripped):
        return ["sh", "-c", stripped]
    try:
        argv = shlex.split(stripped)
    except ValueError:
        return ["sh", "-c", stripped]
    if not argv:
        return ["sh", "-c", stripped]
    return argv


def _segment_invokes_pytest(words: Sequence[str]) -> bool:
    """True when one shell segment's argv runs pytest, not installs it."""
    if not words:
        return False
    first = words[0].rsplit("/", 1)[-1]
    if first == "pytest":
        return True
    if re.fullmatch(r"python[\d.]*|pypy[\d.]*", first):
        return any(
            word == "-m" and index + 1 < len(words)
            and words[index + 1] == "pytest"
            for index, word in enumerate(words)
        )
    if first == "uv" and "run" in words[1:]:
        after = words[words.index("run", 1) + 1:]
        return "pytest" in after
    return False


def _run_invokes_pytest(run: str) -> bool:
    """True when any line of a ``run:`` value invokes pytest.

    ``pip install pytest`` does not count: installing the runner is not
    running the suite. Segments are split quote-blindly, which is fine for
    a matcher — execution always runs the value verbatim.
    """
    for line in run.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for segment in re.split(r"\s*(?:&&|\|\||;)\s*", stripped):
            try:
                words = shlex.split(segment)
            except ValueError:
                words = segment.split()
            if _segment_invokes_pytest(words):
                return True
    return False


def _is_test_step(name: str, run: str) -> bool:
    """True when a workflow step is the suite, not its installation.

    A step whose ``run:`` invokes pytest always counts. Otherwise the name
    must say tests — a ``test``/``tests``/``testing`` word or ``pytest`` —
    and must not say ``install``/``setup``, so ``Install pytest`` and
    ``Set up the test database`` never select an installer as the suite.
    An unnamed step is judged by the name GitHub shows for it, ``Run``
    and the first line of its ``run:``, so ``- run: make check test``
    counts (FF-Weekly-Start-Sit's suite, which fell back to pytest).
    """
    if _run_invokes_pytest(run):
        return True
    lowered = name.strip().lower()
    if not lowered:
        lines = run.strip().splitlines()
        lowered = "run " + lines[0].strip().lower() if lines else ""
    if "install" in lowered or "setup" in lowered or "set up" in lowered:
        return False
    if re.search(r"\btests?\b|\btesting\b", lowered):
        return True
    return "pytest" in lowered


def override_test_command(root: pathlib.Path) -> Optional[Tuple[List[str], str]]:
    """Resolve the repo's declared test command, or None.

    First ``[tool.command-center] test`` in ``pyproject.toml``, split into
    argv (write ``sh -c "..."`` there when the suite needs a shell); else the
    ``run:`` of the first CI workflow test step, across
    ``.github/workflows/*.yml`` and ``*.yaml`` in sorted order. The source
    names the evidence, for the PR body and the heartbeat note.
    """
    raw = pyproject_test_command(root)
    if raw:
        try:
            argv = shlex.split(raw)
        except ValueError:
            argv = []
        if argv:
            return argv, "pyproject.toml [tool.command-center] test"
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        candidates = sorted(
            path for path in workflows.iterdir()
            if path.is_file() and path.suffix in (".yml", ".yaml")
        )
        for path in candidates:
            try:
                text = path.read_text()
            except OSError:
                continue
            for name, run in _workflow_steps(text):
                if not run.strip():
                    continue
                if _is_test_step(name, run):
                    display = (name.strip()
                               or run.strip().splitlines()[0][:60])
                    return (
                        _shell_command(run),
                        'CI {} step "{}"'.format(
                            path.relative_to(root), display),
                    )
    return None


def default_test_plan(
        root: pathlib.Path) -> Tuple[List[List[str]], Optional[str]]:
    """Choose the test commands and name the evidence behind the test slot.

    A declared override always wins, ungated: its presence is the evidence.
    Without one, the default pytest runs only where Python evidence (a
    ``tests/`` tree or a config marker) says a suite exists. The ``npm``
    suite is independent and never suppressed. Returns the commands and the
    test-slot source, which is None when no test slot was resolved.
    """
    commands: List[List[str]] = []
    if (root / "funnel.py").is_file():
        commands.append([
            sys.executable, "-c",
            'from pathlib import Path; compile(Path("funnel.py").read_text(), '
            '"funnel.py", "exec")',
        ])
    override = override_test_command(root)
    if override is not None:
        argv, source = override
        commands.append(argv)
        test_source: Optional[str] = source
    else:
        test_dir = root / "tests"
        has_python_tests = test_dir.is_dir() and any(
            path.is_file() for path in test_dir.rglob("test_*.py")
        )
        python_markers = (
            root / "pytest.ini", root / "pyproject.toml", root / "setup.cfg",
            root / "tox.ini",
        )
        if has_python_tests or any(path.is_file() for path in python_markers):
            # No cache provider: the finish step commits everything dirty, so
            # the test run must not leave .pytest_cache/ behind for it to
            # stage. Resolved commands run verbatim, so run_tests carries
            # the same guard in PYTEST_ADDOPTS for those.
            commands.append(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
            )
            test_source = "default pytest"
        else:
            test_source = None
    if (root / "package.json").is_file():
        commands.append(["npm", "test"])
    if not commands:
        raise ImplementError(
            "could not derive a test command from this checkout"
        )
    return commands, test_source


def default_test_commands(root: pathlib.Path) -> List[List[str]]:
    """Choose the deterministic test command from repository evidence."""
    commands, _ = default_test_plan(root)
    return commands


#: Where a pinned interpreter is looked for when PATH does not name it.
#: Codex's sandbox PATH can miss Homebrew while the binaries are there.
PINNED_PYTHON_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _python_minor(executable: str) -> Optional[str]:
    """The ``M.m`` version an interpreter reports, or None if it won't run."""
    try:
        result = _run(
            [executable, "-c",
             "import sys; print('%d.%d' % sys.version_info[:2])"],
            cwd=pathlib.Path.cwd(), check=False,
            timeout=INTERPRETER_PROBE_TIMEOUT_SECONDS,
        )
    except CommandTimeoutError:
        raise
    except (ImplementError, OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def pinned_interpreter(root: pathlib.Path) -> str:
    """The interpreter the checkout's tests should run under.

    A repo that pins ``.python-version`` (The-League pins 3.12 and its
    Makefile refuses anything else) gets a matching interpreter even when
    finish-ticket itself was started by Apple's 3.9 ``python3`` (#1024).
    Without a pin, or when no matching interpreter exists, this is
    ``sys.executable``, so the repo's own check still names a mismatch.
    """
    try:
        lines = (root / ".python-version").read_text().splitlines()
    except OSError:
        return sys.executable
    parts = lines[0].strip().split(".") if lines else []
    if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
        return sys.executable
    wanted = "{}.{}".format(parts[0], parts[1])
    if "{}.{}".format(*sys.version_info[:2]) == wanted:
        return sys.executable
    name = "python" + wanted
    candidates = [shutil.which(name)] + [
        os.path.join(directory, name) for directory in PINNED_PYTHON_DIRS]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and \
                _python_minor(candidate) == wanted:
            return candidate
    return sys.executable


def run_tests(root: pathlib.Path,
              commands: Optional[Sequence[Sequence[str]]] = None, *,
              timeout: Optional[float] = None
              ) -> Tuple[List[str], Optional[str]]:
    """Run the checkout's tests without writing Python bytecode.

    Returns the rendered commands and the test-command source, which is
    None when the caller injected explicit commands or no test slot was
    resolved. ``timeout`` lowers each command's bound, for a caller with a
    budget of its own (the finish's reproduction run, #1805).
    """
    if commands is not None:
        selected = list(commands)
        source: Optional[str] = None
    else:
        selected, source = default_test_plan(root)
    # PYTHON reaches a resolved `make` whose Makefile says `PYTHON ?= python3`.
    # In Codex's sandbox that bare name is Apple's /usr/bin/python3, whose
    # compileall cannot write its cache (#953); run a chosen interpreter
    # instead, honouring the checkout's .python-version pin (#1024).
    python = pinned_interpreter(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHON=python)
    # Whatever ultimately invokes pytest inherits the no-cache guard, so a
    # resolved `make test` cannot leave .pytest_cache/ for the explicit stage.
    extra = env.get("PYTEST_ADDOPTS", "").strip()
    env["PYTEST_ADDOPTS"] = (
        "{} -p no:cacheprovider".format(extra) if extra
        else "-p no:cacheprovider"
    )
    rendered = []
    for command in selected:
        argv = list(command)
        # A command resolved from CI names `python` or `python3`; the schedule
        # Mac has no bare `python` on PATH (#890), so run the chosen one.
        if argv and argv[0] in ("python", "python3"):
            argv[0] = python
        bound = (COMPILE_COMMAND_TIMEOUT_SECONDS
                 if _is_compile_command(argv)
                 else TEST_COMMAND_TIMEOUT_SECONDS)
        _run(
            argv, cwd=root, env=env,
            timeout=bound if timeout is None else min(bound, timeout),
        )
        rendered.append(shlex.join(argv))
    return rendered, source


def _review_evidence():
    """``scripts/review_evidence.py`` (#1802), loaded when a finish needs it.

    ``scripts/`` is not a package, and the script imports this module, so
    it cannot be imported at the top of this one.
    """
    path = (pathlib.Path(__file__).resolve().parent.parent
            / "scripts" / "review_evidence.py")
    spec = importlib.util.spec_from_file_location("review_evidence", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _merged_failure(record: dict) -> MergedSuiteError:
    """The error for a merged run that fails where main does not.

    It names the command that stopped the plan and the failing ids: those
    main does not share, or every id when the base could not be compared
    (another runner, or a pytest run that stopped early). The counts line
    counts only failures main does not share, which is what fails the
    finish.
    """
    timed_out = [entry for entry in record["commands"]
                 if entry.get("timed_out") is True]
    if timed_out:
        entry = timed_out[-1]
        lines = ["tests timed out: {} timed out on the merge with "
                 "origin/main {}".format(
                     entry["command"], record["base"][:12])]
        if entry.get("output"):
            lines.append(entry["output"])
        return MergedSuiteError("\n".join(lines))
    failed = [entry["command"] for entry in record["commands"]
              if entry["result"] == "fail"]
    lines = ["{} failed on the merge with origin/main {}".format(
        failed[-1] if failed else "the test command", record["base"][:12])]
    lines.extend("FAILED {}".format(node_id) for node_id in
                 record["new_failures"] or record["failing"])
    if record["new_failures"]:
        lines.append("{} failed".format(len(record["new_failures"])))
    return MergedSuiteError("\n".join(lines))


def _run_finish_tests(root: pathlib.Path,
                      commands: Optional[Sequence[Sequence[str]]] = None
                      ) -> Tuple[List[str], Optional[str], Optional[str],
                                 Optional[dict]]:
    """Run the finish's suite on the work merged with current main (#1804).

    A head passing its own suite says nothing about the merge it becomes:
    another PR can land on main meanwhile, and a clash shows only on the
    merge (#1742). So this fetches origin/main and runs
    ``review_evidence.merged_suite`` instead of the head-only run: the
    checkout's HEAD merged into main in a temporary worktree beside the
    checkout, which for a Codex run is its ``codex-runs/`` directory. The
    worktree is removed on every path, and it is outside the checkout, so
    nothing in it can reach ``_commit_if_needed``.

    The merge takes the committed HEAD, and that is the tree this finish
    commits: ``_checkpoint_work`` has already committed every path
    ``_commit_if_needed`` would stage, and nothing runs between the two.

    Only a failure main does not share fails the finish
    (``MergedSuiteError``); a conflict raises ``MergeConflictError``. Where
    no merge can be made (``ReviewEvidenceError``: no origin/main, a
    worktree git refuses, no test command on the merge), the head-only run
    runs as it always has. Explicit ``commands`` are a caller's choice and
    run on the head: the merged suite resolves the repository's own plan.

    Returns the commands run, the test-command source, a phrase for the
    finish note when they ran on the merge, and the merged suite's record
    for the PR's evidence block (#1805); both are None on the head alone.
    """
    if commands is not None:
        tests, source = run_tests(root, commands)
        return tests, source, None, None
    # A failed fetch leaves origin/main where this clone last saw it, which
    # is still a merge worth testing; the record names the base it used.
    _run(["git", "fetch", "origin",
          "+refs/heads/main:refs/remotes/origin/main"],
         cwd=root, check=False, timeout=REMOTE_GIT_TIMEOUT_SECONDS)
    try:
        evidence = _review_evidence()
    except OSError:
        evidence = None
    record = None
    if evidence is not None:
        try:
            record = evidence.merged_suite(
                root, "origin/main", work_dir=root.parent)
        except (evidence.ReviewEvidenceError, OSError):
            record = None
    if record is None:
        tests, source = run_tests(root, None)
        return tests, source, None, None
    if record["result"] == "conflict":
        raise MergeConflictError(record["base"], record["conflicts"])
    if record["blocking"]:
        raise _merged_failure(record)
    tested = "tested on the merge with origin/main"
    if record["result"] == "fail":
        # Every failure is main's own: it does not fail the finish, but the
        # note says the suite was not green (AGENTS.md, "Verification").
        tested += " ({} failing as on origin/main)".format(
            len(record["failing"]))
    return ([entry["command"] for entry in record["commands"]],
            record["test_source"], tested, record)


#: How long a finish gives the classification of the PR's added tests on
#: origin/main, after the merged suite (#1805, plan #1783 ticket 5). It
#: took 3.7s on this repository (#1829); the budget keeps a slow member
#: suite from doubling a finish, and over it the block says so.
REPRODUCTION_BUDGET_SECONDS = 4 * 60

#: The stand-in record when no classification ran: the merge could not be
#: made or tested, or ``reproduction`` itself failed. Its line is the
#: block's only word on it, so no error text reaches the PR body.
_REPRODUCTION_NOT_RUN = {"reproduction": "not run",
                         "line": "reproduction: not run", "tests": []}


def _run_reproduction(root: pathlib.Path,
                      merged: Optional[dict]) -> dict:
    """Classify the PR's added tests on the merged suite's base (#1805).

    ``review_evidence.reproduction`` runs the tests the branch adds or
    changes on the base the merged suite used, within
    ``REPRODUCTION_BUDGET_SECONDS``, and says red, passes-on-base, no
    signal, unsupported or over budget. It runs only after a merged suite:
    where none could be made, the base could not be worked in either.
    Evidence is recorded, never a reason to fail a finish, so this never
    raises; a failure is ``reproduction: not run``.
    """
    if merged is None:
        return dict(_REPRODUCTION_NOT_RUN)
    try:
        evidence = _review_evidence()
    except OSError:
        return dict(_REPRODUCTION_NOT_RUN)
    try:
        return evidence.reproduction(
            root, merged["base"], work_dir=root.parent,
            budget=REPRODUCTION_BUDGET_SECONDS)
    except Exception:
        return dict(_REPRODUCTION_NOT_RUN)


_GITHUB_REMOTE_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def resolve_checkout_repo(root: pathlib.Path, explicit: Optional[str]) -> str:
    """Resolve owner/name from an explicit value, origin, or gh's context.

    The origin remote answers locally. Asking `gh repo view` first cost a
    GraphQL call and stranded a finished ticket on one GitHub 504 (#1030).
    """
    if explicit:
        return explicit
    remote = _run(["git", "remote", "get-url", "origin"], cwd=root,
                  check=False, timeout=LOCAL_GIT_TIMEOUT_SECONDS).stdout.strip()
    match = _GITHUB_REMOTE_RE.match(remote)
    if match:
        return match.group(1)
    data = funnel._gh_json("gh", "repo", "view", "--json", "nameWithOwner")
    repo = (data or {}).get("nameWithOwner")
    if not isinstance(repo, str) or "/" not in repo:
        raise ImplementError("could not resolve the checkout's GitHub repository")
    return repo


#: The runner-written evidence block that ends every finished PR's body
#: (#1805, plan #1783 ticket 5). The review packet carries it when its
#: ``sha`` is the head under review (#1812). Only the runner writes these
#: markers: ``render_pr_body`` strips them from everything else in the
#: body, the model's summary, departures and risks among it, so a model
#: cannot forge a block. Each finish re-renders the whole body, so a
#: continued PR's block is always the latest push's. The format, one line
#: each and in this order:
#:
#:   <!-- command-center-evidence -->
#:   Evidence, written by the runner:
#:   - sha: <the 40-hex commit this finish pushed, after any merge the
#:     push added>
#:   - merged suite: pass on origin/main <12-hex>
#:     | fail, <n> failing as on origin/main <12-hex>
#:     | not run (the head's own suite passed)
#:   - reproduction: red | passes-on-base | no signal | unsupported
#:     | over budget | not run
#:   - added tests: <n> red, <n> passes-on-base, <n> no signal
#:   - <outcome>: <node id>    (command-center only, at most
#:     MAX_EVIDENCE_TEST_IDS, then "- (+<n> more)")
#:   - rewrites prior fix: #<ticket> (<path>:<function>)
#:     (at most MAX_EVIDENCE_PRIOR_FIXES, then "- (+<n> more prior fixes)")
#:   - prior fix scan: not run    (only when its bounded scan was unavailable)
#:   <!-- /command-center-evidence -->
#:
#: Every other repository's block carries the counts and no node id, as
#: its notes do (#1796).
EVIDENCE_MARKER = "<!-- command-center-evidence -->"
EVIDENCE_END_MARKER = "<!-- /command-center-evidence -->"

#: How many added tests a command-center block names; the rest are counted.
#: It keeps the block well inside the review packet's 8 KB (#1812).
MAX_EVIDENCE_TEST_IDS = 30

#: How many prior fixes a command-center block names; the rest are counted.
MAX_EVIDENCE_PRIOR_FIXES = 30

#: Either marker, however spaced or cased, so a near-miss cannot pass for
#: one with a looser reader.
_EVIDENCE_MARKER_RE = re.compile(
    r"<!--\s*/?\s*command-center-evidence\s*-->", re.IGNORECASE)


def _strip_evidence_markers(text: str) -> str:
    """``text`` without either marker, repeated until none is left, so a
    marker split around another cannot close up into one (#1805)."""
    while True:
        stripped = _EVIDENCE_MARKER_RE.sub("", text)
        if stripped == text:
            return text
        text = stripped


def render_evidence_block(*, sha: str, merged: Optional[dict],
                          reproduction: dict, repo: str,
                          prior_fixes: Optional[
                              Sequence[Tuple[int, str, Optional[str]]]
                          ] = ()) -> str:
    """The runner's evidence block for a PR body (``EVIDENCE_MARKER``).

    ``sha`` is the commit the finish pushed, ``merged`` the merged suite's
    record (None when the head's suite ran alone) and ``reproduction`` the
    added-test classification's (#1805). ``prior_fixes`` is the bounded
    prior-fix scan's result; None means it could not run.
    """
    if merged is None:
        suite = "not run (the head's own suite passed)"
    elif merged["result"] == "pass":
        suite = "pass on origin/main {}".format(merged["base"][:12])
    else:
        # A PR opens past a failing merge only when main fails the same way.
        suite = "fail, {} failing as on origin/main {}".format(
            len(merged["failing"]), merged["base"][:12])
    tests = reproduction.get("tests") or []
    counts = Counter(test["outcome"] for test in tests)
    lines = [
        EVIDENCE_MARKER,
        "Evidence, written by the runner:",
        "- sha: {}".format(sha),
        "- merged suite: {}".format(suite),
        "- {}".format(reproduction["line"]),
        "- added tests: {} red, {} passes-on-base, {} no signal".format(
            counts["red"], counts["passes-on-base"], counts["no signal"]),
    ]
    if _is_public_repo(repo):
        shown = tests[:MAX_EVIDENCE_TEST_IDS]
        for test in shown:
            # A node id holds a path the branch chose; it must not carry
            # a marker, or a line break that would start a line of its own.
            node_id = " ".join(_strip_evidence_markers(test["id"]).split())
            lines.append("- {}: {}".format(test["outcome"], node_id[:200]))
        if len(tests) > len(shown):
            lines.append("- (+{} more)".format(len(tests) - len(shown)))
    if prior_fixes is None:
        lines.append("- prior fix scan: not run")
    else:
        found = sorted(set(prior_fixes),
                       key=lambda row: (row[0], row[1], row[2] or ""))
        for ticket_number, path, function in found[:MAX_EVIDENCE_PRIOR_FIXES]:
            # Git paths and hunk headers are branch-controlled input; keep
            # each runner-owned evidence item on one line and marker-free.
            safe_path = " ".join(
                _strip_evidence_markers(str(path)).split())[:200]
            safe_function = " ".join(
                _strip_evidence_markers(str(function or "<unknown>")).split())[:120]
            lines.append("- rewrites prior fix: #{} ({}:{})".format(
                ticket_number, safe_path, safe_function))
        if len(found) > MAX_EVIDENCE_PRIOR_FIXES:
            lines.append("- (+{} more prior fixes)".format(
                len(found) - MAX_EVIDENCE_PRIOR_FIXES))
    lines.append(EVIDENCE_END_MARKER)
    return "\n".join(lines) + "\n"


def _prior_fix_evidence(
        root: pathlib.Path, base: Optional[str], head: str
) -> Optional[List[Tuple[int, str, Optional[str]]]]:
    """Run the optional prior-fix scan without making a finish depend on it."""
    if not base:
        return None
    try:
        import fix_recurrence

        return fix_recurrence.prior_fixes_touched(root, base, head)
    except Exception:
        # Like reproduction evidence, this names confirmed results when
        # available and never turns an evidence-reader failure into a failed
        # ticket finish.
        return None


def render_pr_body(ticket: dict, answer: dict, *, continued: bool,
                   tests: Sequence[str],
                   test_source: Optional[str] = None,
                   evidence: Optional[str] = None) -> str:
    """Render the stable PR template from the model's structured answer.

    ``evidence`` is the runner's block (``render_evidence_block``), which
    ends the body (#1805). Marker text is stripped from everything before
    it, so the runner's block is the only one.
    """
    parent = ticket.get("parent") or {}
    parent_number = parent.get("number")
    lines = []
    if isinstance(parent_number, int):
        own = str(ticket.get("ref") or "").split("#")[0]
        home = parent_repo(own, ticket)
        lines.append("Part of {}#{}.".format(
            "" if home == own else home, parent_number))
        lines.append("")
    lines.extend([
        "Implements #{}.".format(ticket["number"]),
        "",
        "Summary:",
        answer["summary"],
        "",
        "Departures:",
    ])
    if answer["departures"]:
        lines.extend("- " + value for value in answer["departures"])
    else:
        lines.append("- None.")
    if answer.get("risks"):
        # Only when there are some (#1807): a body without risks stays the
        # template it always was. The label also ends the Departures section
        # review.parse_departures reads, so a risk never reads as a departure.
        lines.extend(["", "Risks:"])
        lines.extend("- " + value for value in answer["risks"])
    lines.extend([
        "",
        "Branch:",
        ("Continued the existing remote ticket branch."
         if continued else
         "Created fresh from origin/main; no existing remote ticket branch."),
        "",
        "Verified:",
    ])
    lines.extend("- `{}`".format(value) for value in tests)
    if test_source is not None:
        lines.extend([
            "",
            "Test command source:",
            test_source,
        ])
    # Every line so far is the model's answer or text the branch can shape
    # (a CI step's name), none of it the runner's block (#1805).
    body = _strip_evidence_markers("\n".join(lines) + "\n")
    if evidence is not None:
        body += "\n" + evidence
    return body


def _remote_branch_exists(root: pathlib.Path, branch: str) -> bool:
    proc = _run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin",
         "refs/heads/{}".format(branch)],
        cwd=root,
        check=False,
        timeout=REMOTE_GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode not in (0, 2):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ImplementError("could not inspect remote ticket branch: {}".format(detail))
    return proc.returncode == 0


_RUN_SCRATCH_FILENAMES = frozenset({
    "answer.json",
    "diag.json",
    "packet.json",
    "prompt.json",
    "run.json",
})
_RUN_SCRATCH_PREFIXES = (
    "answer.", "answer-", "diag.", "diag-", "packet.", "packet-",
    "prompt.", "prompt-", "run.", "run-", "scratch.", "scratch-",
)
_RUN_SCRATCH_SUFFIXES = (".scratch", ".tmp")
_RUN_SCRATCH_DIRECTORIES = frozenset({".scratch", "scratch", ".tmp", "tmp"})


def _git_name_paths(root: pathlib.Path, command: Sequence[str]) -> List[str]:
    """Return NUL-delimited Git paths without losing unusual filenames."""
    raw = _run(["git", *command], cwd=root,
               timeout=LOCAL_GIT_TIMEOUT_SECONDS).stdout
    return [path for path in raw.split("\0") if path]


def _working_tree_paths(root: pathlib.Path) -> List[str]:
    """List staged, unstaged, and untracked paths that could be committed."""
    paths = set()
    for command in (
        ("diff", "--name-only", "-z"),
        ("diff", "--cached", "--name-only", "-z"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        paths.update(_git_name_paths(root, command))
    return sorted(paths)


def _staged_paths(root: pathlib.Path) -> List[str]:
    """List the paths currently in the index, using Git's safe NUL format."""
    return sorted(set(_git_name_paths(
        root, ("diff", "--cached", "--name-only", "-z"))))


def _branch_paths(root: pathlib.Path) -> List[str]:
    """List files already present on the ticket branch beyond origin/main."""
    return sorted(set(_git_name_paths(
        root, ("diff", "--name-only", "-z", "origin/main...HEAD"))))


def _addable_paths(root: pathlib.Path, paths: Sequence[str]) -> List[str]:
    """Drop the paths `git add` cannot match, keeping the rest in sorted order.

    A file removed with `git rm` is gone from the worktree *and* the index,
    while `git diff --cached` still reports it as a staged deletion. `git add`
    has nothing to match, so it exits 128 with `pathspec ... did not match any
    files` and takes the whole finish down with it (#1214). Such a path is
    already staged; there is nothing left to add. A file removed with plain
    `rm` is still in the index, so `git add` stages its deletion and is kept.
    """
    selected = sorted(set(paths))
    if not selected:
        return []
    tracked = set(_git_name_paths(root, ("ls-files", "-z", "--", *selected)))
    return [path for path in selected
            if path in tracked or os.path.lexists(os.path.join(root, path))]


def _stage_explicit_paths(root: pathlib.Path, paths: Sequence[str]) -> None:
    """Stage exactly the observed paths, never a blanket add or status sweep."""
    selected = _addable_paths(root, paths)
    if selected:
        _run(["git", "add", "--", *selected], cwd=root,
             timeout=LOCAL_GIT_TIMEOUT_SECONDS)


def _is_run_scratch(path: str) -> bool:
    """Recognise runner scratch names without maintaining a per-repo allowlist."""
    normalized = path.replace("\\", "/")
    parts = pathlib.PurePosixPath(normalized).parts
    lowered_parts = tuple(part.lower() for part in parts)
    name = lowered_parts[-1] if lowered_parts else ""
    if any(part in _RUN_SCRATCH_DIRECTORIES for part in lowered_parts):
        return True
    # The handoff and its companion diagnostics live at the checkout root.
    if len(lowered_parts) != 1:
        return False
    if name in _RUN_SCRATCH_FILENAMES:
        return True
    if name.endswith(_RUN_SCRATCH_SUFFIXES):
        return True
    return name.startswith(_RUN_SCRATCH_PREFIXES) and name.endswith(
        (".json", ".jsonl", ".log", ".txt", ".md"))


def _pre_pr_files(root: pathlib.Path,
                  about_to_commit: Sequence[str] = ()) -> List[str]:
    """List what would ship: branch, staged, and about-to-be-committed paths."""
    paths = set(about_to_commit)
    paths.update(_working_tree_paths(root))
    paths.update(_staged_paths(root))
    paths.update(_branch_paths(root))
    return sorted(paths)


def _check_no_run_scratch(root: pathlib.Path,
                          about_to_commit: Sequence[str] = ()) -> None:
    """Refuse a PR when named run scratch appears in its file list."""
    stray = [path for path in _pre_pr_files(root, about_to_commit)
             if _is_run_scratch(path)]
    if stray:
        raise StrayFileError(stray)


def _commit_if_needed(root: pathlib.Path, number: int, summary: str, *,
                      allow_empty: bool = False) -> Optional[bool]:
    """Commit paths; optionally return None when the branch has no changes."""
    paths = _working_tree_paths(root)
    if paths:
        _stage_explicit_paths(root, paths)
        _check_no_run_scratch(root, about_to_commit=paths)
        subject = summary.splitlines()[0].strip()
        if len(subject) > 60:
            subject = subject[:57].rstrip() + "..."
        _run(
            ["git", "commit", "-m", "Finish #{}: {}".format(number, subject)],
            cwd=root, timeout=LOCAL_GIT_TIMEOUT_SECONDS,
        )
        return True
    _check_no_run_scratch(root)
    ahead = _run(
        ["git", "rev-list", "--count", "origin/main..HEAD"], cwd=root,
        timeout=LOCAL_GIT_TIMEOUT_SECONDS,
    ).stdout.strip()
    if not ahead.isdigit():
        raise ImplementError("could not read changes ahead of origin/main")
    if int(ahead) < 1:
        if allow_empty:
            return None
        raise ImplementError("done answer produced no change to commit")
    return False


def _heartbeat_start(run: str, agent: str) -> datetime:
    """Read the durable heartbeat start for this finish run."""
    try:
        import heartbeat
        records = heartbeat.read_github(agent)
        if not isinstance(records, list):
            raise TypeError("heartbeat records were not a list")
    except Exception as exc:
        raise ImplementError(
            "could not read heartbeat start for run {}: {}".format(run, exc)
        )
    starts = [
        row for row in records
        if isinstance(row, dict)
        and row.get("phase") == "start"
        and row.get("run") == run
    ]
    if len(starts) != 1:
        raise ImplementError(
            "could not read heartbeat start for run {}".format(run)
        )
    stamp = starts[0].get("ts")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise ImplementError(
            "could not read heartbeat start for run {}".format(run)
        )
    try:
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ImplementError(
            "could not read heartbeat start for run {}".format(run)
        )


def _evidence_target(url: str) -> Tuple[str, str, str, int, str]:
    """Resolve a supported GitHub URL to one REST read and its timestamp."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        parsed = None
    if (parsed is None or parsed.scheme != "https"
            or parsed.netloc.lower() != "github.com"
            or parsed.query):
        raise ImplementError("invalid GitHub evidence URL: {}".format(url))
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 4:
        raise ImplementError("unsupported GitHub evidence URL: {}".format(url))
    owner, repository, route, raw_number = parts
    if (owner.lower() != "nateprich-projects"
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository)
            or not raw_number.isdigit()):
        raise ImplementError("invalid GitHub evidence URL: {}".format(url))
    number = int(raw_number)
    fragment = parsed.fragment
    repo_path = "{}/{}".format(owner, repository)
    if route == "issues":
        comment = re.fullmatch(r"issuecomment-(\d+)", fragment)
        if comment:
            return (
                "repos/{}/issues/comments/{}".format(
                    repo_path, comment.group(1)),
                "comment", "created_at", int(comment.group(1)), repo_path,
            )
        if not fragment:
            return (
                "repos/{}/issues/{}".format(repo_path, number),
                "closed", "closed_at", number, repo_path,
            )
    elif route == "pull":
        issue_comment = re.fullmatch(r"issuecomment-(\d+)", fragment)
        if issue_comment:
            return (
                "repos/{}/issues/comments/{}".format(
                    repo_path, issue_comment.group(1)),
                "comment", "created_at", int(issue_comment.group(1)), repo_path,
            )
        review_comment = re.fullmatch(r"discussion_r(\d+)", fragment)
        if review_comment:
            return (
                "repos/{}/pulls/comments/{}".format(
                    repo_path, review_comment.group(1)),
                "comment", "created_at", int(review_comment.group(1)), repo_path,
            )
        if not fragment:
            return (
                "repos/{}/pulls/{}".format(repo_path, number),
                "closed", "closed_at", number, repo_path,
            )
    raise ImplementError("unsupported GitHub evidence URL: {}".format(url))


def _parse_github_timestamp(value: object) -> Optional[datetime]:
    """Parse one timezone-qualified timestamp returned by the GitHub API."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _verify_evidence_url(url: str, started: datetime) -> None:
    """Verify one artifact's repository identity and run-relative timestamp."""
    endpoint, kind, stamp_field, identity, repo_path = _evidence_target(url)
    try:
        data = funnel._gh_api_json(endpoint)
    except Exception as exc:
        raise ImplementError(
            "could not read evidence URL {}: {}".format(url, exc)
        )
    if not isinstance(data, dict):
        raise ImplementError("could not read evidence URL {}".format(url))
    api_url = data.get("url")
    prefix = "https://api.github.com/repos/{}/".format(repo_path)
    if (not isinstance(api_url, str)
            or not api_url.lower().startswith(prefix.lower())):
        raise ImplementError(
            "evidence URL {} did not resolve inside nateprich-projects".format(
                url)
        )
    html_url = data.get("html_url")
    if (not isinstance(html_url, str)
            or html_url.rstrip("/").lower() != url.rstrip("/").lower()):
        raise ImplementError(
            "evidence URL {} did not resolve to its named artifact".format(url)
        )
    identity_field = "id" if kind == "comment" else "number"
    if data.get(identity_field) != identity:
        raise ImplementError(
            "evidence URL {} did not resolve to its named artifact".format(url)
        )
    if kind == "comment" and not funnel.trusted_comment(data):
        # A comment is this run's evidence only when the owner account posted
        # it (#1788); anyone can comment on a public repository mid-run.
        raise ImplementError(
            "evidence URL {} was not posted by the owner account".format(url)
        )
    if kind == "closed" and str(data.get("state", "")).lower() != "closed":
        raise ImplementError("evidence URL {} is not closed".format(url))
    occurred = _parse_github_timestamp(data.get(stamp_field))
    if occurred is None:
        raise ImplementError(
            "could not read the timestamp for evidence URL {}".format(url)
        )
    if occurred < started:
        when = "created" if kind == "comment" else "closed"
        raise ImplementError(
            "evidence URL {} was {} before this run started".format(url, when)
        )


def _verify_done_evidence(urls: Sequence[str], *, run: str,
                          agent: str) -> List[str]:
    """Fail closed unless every named artifact is new enough for this run."""
    if not urls:
        raise ImplementError(
            "done answer produced no change and named no evidence")
    started = _heartbeat_start(run, agent)
    for url in urls:
        _verify_evidence_url(url, started)
    return list(urls)


def close_no_diff_ticket(repo: str, number: int, *,
                         cwd: pathlib.Path) -> None:
    """Close a verified no-diff ticket as completed."""
    proc = funnel._run_gh(
        ["gh", "issue", "close", str(number), "--repo", repo,
         "--reason", "completed"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not close {}#{} as completed: {}".format(
                repo, number, (proc.stderr or "").strip()))


def create_or_update_pr(repo: str, context: dict, ticket: dict,
                        body: str) -> dict:
    """Create the ticket PR, or update the one already open for the branch.

    Only the funnel's own open PR is updated (#1794): ``--head`` also matches
    a fork's PR on a branch of the same name, and editing that would act on
    a stranger's PR.
    """
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--head", context["branch"],
        "--json", "number,url," + funnel.PR_TRUST_JSON_FIELDS,
        "--limit", "10",
    )
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError("could not list the branch's open PR")
    rows = [row for row in rows if funnel.is_funnel_pr(repo, row)]
    title = "{} (#{})".format(ticket["title"], ticket["number"])
    if rows:
        pr = {"number": rows[0].get("number"), "url": rows[0].get("url")}
        proc = funnel._run_gh(
            ["gh", "pr", "edit", str(pr["number"]), "--repo", repo,
             "--title", title, "--body-file", "-"],
            cwd=str(context["root"]), input=body, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise funnel.GitHubError((proc.stderr or "could not update PR").strip())
        return pr
    proc = funnel._run_gh(
        ["gh", "pr", "create", "--repo", repo, "--base", "main",
         "--head", context["branch"], "--title", title,
         "--body-file", "-"],
        cwd=str(context["root"]), input=body, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError((proc.stderr or "could not create PR").strip())
    url = (proc.stdout or "").strip().splitlines()[-1]
    match = re.search(r"/pull/([1-9][0-9]*)/?$", url)
    if match is None:
        raise ImplementError("gh pr create did not return a pull request URL")
    return {"number": int(match.group(1)), "url": url}


def render_human_step_title(action: str) -> str:
    """Render the human-step sub-issue title from the model's action."""
    return "Human step: {}".format(action)


def render_human_step_body(*, parent_number: int, ticket_number: int,
                           reason: str, action: str) -> str:
    """Render the human-step sub-issue body with its reason as prose.

    The ``Human step: <reason>`` line is prose for Nate to read, not a
    marker: the body scanner is deleted (#826) and the machine signal is
    the Needs Project field, written alongside by ``finish_blocked_on_human``.
    A step a Claude Code session can do names the session as its actor,
    not Nate (#1901).
    """
    doing = action.rstrip()
    if not doing.endswith("."):
        doing += "."
    who = ("a Claude Code session on the Mac mini"
           if HUMAN_STEP_NEEDS.get(reason) == "claude-code-environment"
           else "Nate")
    return "\n".join([
        "Part of #{}; discovered while implementing #{}.".format(
            parent_number, ticket_number),
        "",
        "Human step: {}".format(reason),
        "",
        "Action {} must perform: {}".format(who, doing),
        "",
    ])


def parse_created_number(output: str) -> int:
    """Read the created issue number from `gh issue create` output.

    `gh` prints the issue URL; the number is its last path segment. Only
    the last non-empty line is read, so progress chatter above it is
    harmless.
    """
    lines = [line.strip() for line in (output or "").splitlines()
             if line.strip()]
    if not lines:
        raise funnel.GitHubError(
            "gh issue create printed nothing; the ticket may not exist")
    match = re.search(r"/issues/([1-9][0-9]*)\s*$", lines[-1])
    if not match:
        raise funnel.GitHubError(
            "could not read the created issue number from: {}".format(
                lines[-1][:120]))
    return int(match.group(1))


def create_human_step_issue(repo: str, parent_number: int, title: str,
                            body: str, *,
                            cwd: pathlib.Path) -> dict:
    """File the human-step sub-issue under the ticket's parent. One mutation."""
    proc = funnel._run_gh(
        ["gh", "issue", "create", "--repo", repo,
         "--parent", str(parent_number),
         "--title", title, "--body", body],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not file the human-step issue under {}#{}: {}".format(
                repo, parent_number, (proc.stderr or "").strip()))
    number = parse_created_number(proc.stdout or "")
    url = [line.strip() for line in (proc.stdout or "").splitlines()
           if line.strip()][-1]
    return {"number": number, "ref": "{}#{}".format(repo, number), "url": url}


def write_human_step_needs(url: str, ref: str, needs: str = "human") -> None:
    """Set canonical routing fields on a new human-step sub-issue.

    The sub-issue joins the parent's Project automatically with its fields
    blank; ``gh project item-add`` answers its row id whether fresh or
    already present (as breakdown's ``add_to_project`` does), then one
    mutation sets the field. Without this the new ticket would read as
    unset and miss the human_steps section (#826). ``needs`` is ``human``
    for Nate's steps and ``claude-code-environment`` for a session's (#1901).
    """
    from engine import breakdown as breakdown_engine

    item_id = breakdown_engine.add_to_project(url)
    funnel.write_project_select(item_id, "Origin", "agent", ref)
    funnel.write_project_select(item_id, "Risk", "standard", ref)
    breakdown_engine.write_needs(item_id, needs, ref)


def write_session_step_needs(url: str, ref: str) -> None:
    """Route a new step to a Claude Code session on the Mac mini (#1901)."""
    write_human_step_needs(url, ref, needs="claude-code-environment")


def write_declined_needs(url: str, ref: str) -> None:
    """Route a declined implementation back to agents, not Nate."""
    from engine import breakdown as breakdown_engine

    item_id = breakdown_engine.add_to_project(url)
    breakdown_engine.write_needs(item_id, "agent", ref)


def write_declined_external_event_needs(url: str, ref: str) -> None:
    """Wait for a pending gate answer without creating a human block."""
    from engine import breakdown as breakdown_engine

    item_id = breakdown_engine.add_to_project(url)
    breakdown_engine.write_needs(item_id, "external-event", ref)


def write_declined_agent_needs(url: str, ref: str) -> None:
    """Route an unhandled lane decline to the watch-owned agent lane."""
    from engine import breakdown as breakdown_engine

    item_id = breakdown_engine.add_to_project(url)
    breakdown_engine.write_needs(item_id, "agent", ref)


def mark_ticket_blocked(repo: str, number: int, *, blocked_by: Optional[int] = None,
                        cwd: pathlib.Path) -> None:
    """Label one ticket blocked, with the native edge when one exists."""
    command = ["gh", "issue", "edit", str(number), "--repo", repo]
    if blocked_by is not None:
        command += ["--add-blocked-by", str(blocked_by)]
    command += ["--add-label", "blocked"]
    proc = funnel._run_gh(
        command, cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not mark {}#{} blocked: {}".format(
                repo, number, (proc.stderr or "").strip()))


#: The parent's sub-issues, read before a human step is filed so a step Nate
#: closed as not planned is never filed again for the same ticket (#1726).
#: ``totalCount`` is requested so a short read is detectable.
HUMAN_STEP_SIBLINGS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $after) {
        totalCount
        nodes { number state stateReason body repository { nameWithOwner } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

#: The stable line ``render_human_step_body`` writes first in every step. The
#: ticket number is matched whole, so a step for #312 never matches #31.
HUMAN_STEP_TICKET_RE = re.compile(
    r"\bdiscovered while implementing #(?P<number>[1-9][0-9]*)(?![0-9])")

#: The route record type a closed-step routing comment carries under the
#: shared review-routing marker. The loop guard reads it back (#1726).
CLOSED_HUMAN_STEP_ROUTE = "closed-human-step"


def read_parent_sub_issues(repo: str, number: int) -> List[Dict[str, object]]:
    """Read every sub-issue of one parent with its state, reason and body.

    Fails closed: a malformed page, a missing cursor or a count that
    disagrees with the rows raises ``GitHubError``, so a short read can never
    look like "no closed step" and let the run file one again (#1726).
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise funnel.GitHubError("invalid repository ref {}".format(repo))
    parent = "{}#{}".format(repo, number)
    rows: List[Dict[str, object]] = []
    total: Optional[int] = None
    cursor: Optional[str] = None
    seen_cursors = set()
    while True:
        variables: Dict[str, object] = {
            "owner": owner, "name": name, "number": number,
        }
        if cursor is not None:
            variables["after"] = cursor
        data = funnel.gh_graphql(HUMAN_STEP_SIBLINGS_QUERY, **variables)
        repository = data.get("repository") if isinstance(data, dict) else None
        issue = repository.get("issue") if isinstance(repository, dict) else None
        connection = issue.get("subIssues") if isinstance(issue, dict) else None
        if not isinstance(connection, dict):
            raise funnel.GitHubError(
                "could not read the sub-issues of {}".format(parent))
        page_total = connection.get("totalCount")
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if (
            not isinstance(page_total, int) or isinstance(page_total, bool)
            or page_total < 0 or not isinstance(nodes, list)
            or not isinstance(page_info, dict)
        ):
            raise funnel.GitHubError(
                "invalid sub-issue page for {}".format(parent))
        if total is None:
            total = page_total
        elif page_total != total:
            raise funnel.GitHubError(
                "sub-issue count for {} changed while paging".format(parent))
        for node in nodes:
            owned = node.get("repository") if isinstance(node, dict) else None
            child_repo = (
                owned.get("nameWithOwner") if isinstance(owned, dict) else None
            )
            child = node.get("number") if isinstance(node, dict) else None
            if (
                not isinstance(child_repo, str) or "/" not in child_repo
                or not isinstance(child, int) or isinstance(child, bool)
            ):
                raise funnel.GitHubError(
                    "invalid sub-issue row under {}".format(parent))
            rows.append({
                "ref": "{}#{}".format(child_repo, child),
                "repo": child_repo,
                "number": child,
                "state": str(node.get("state") or "").upper(),
                "state_reason": str(node.get("stateReason") or "").upper(),
                "body": node.get("body") if isinstance(
                    node.get("body"), str) else "",
            })
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if (not isinstance(next_cursor, str) or not next_cursor
                or next_cursor in seen_cursors):
            raise funnel.GitHubError(
                "sub-issue page for {} has no usable next cursor".format(parent))
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    if len(rows) != total:
        raise funnel.GitHubError(
            "read {} of {} sub-issues of {}".format(len(rows), total, parent))
    return rows


def closed_human_steps_for(rows: Sequence[Dict[str, object]], repo: str,
                           number: int) -> List[Dict[str, object]]:
    """The human steps Nate closed as not planned for this ticket, oldest first.

    A step names its ticket on the stable line ``render_human_step_body``
    writes, as a short ref that means the step's own repository, so only a
    step in the ticket's repository can name it. Open steps and steps closed
    as completed are not an answer that the step will not happen.
    """
    found = []
    for row in rows:
        if (
            row.get("repo") != repo
            or row.get("state") != "CLOSED"
            or row.get("state_reason") != "NOT_PLANNED"
        ):
            continue
        match = HUMAN_STEP_TICKET_RE.search(str(row.get("body") or ""))
        if match is not None and int(match.group("number")) == number:
            found.append(row)
    return sorted(found, key=lambda row: int(row["number"]))


def read_ticket_comment_bodies(repo: str, number: int) -> List[str]:
    """Read one ticket's trusted comment bodies, oldest first; fail closed.

    The caller parses routing records out of these, so only the owner
    account's comments are returned (#1788): an outsider's forged record would
    read as a route this run already posted, and the guard would skip it.
    """
    payload = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "comments",
    )
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        raise funnel.GitHubError(
            "could not read comments for {}#{}".format(repo, number))
    return [comment.get("body") or ""
            for comment in funnel.trusted_comments(comments)]


def remote_ticket_head(root: pathlib.Path, branch: str) -> Optional[str]:
    """The ticket branch's pushed tip, or None when it was never pushed.

    This is the head a review verdict judges. A blocked run neither commits
    nor pushes, so the next run on an unchanged ticket reads the same value.
    """
    ref = "refs/heads/{}".format(branch)
    proc = _run(["git", "ls-remote", "--exit-code", "--heads", "origin", ref],
                cwd=root, check=False,
                timeout=REMOTE_GIT_TIMEOUT_SECONDS)
    if proc.returncode == 2:
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ImplementError(
            "could not inspect remote ticket branch: {}".format(detail))
    for line in (proc.stdout or "").splitlines():
        sha, _, name = line.partition("\t")
        if name.strip() == ref and re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return sha
    raise ImplementError("could not read the head of {}".format(ref))


def routed_for_closed_step(bodies: Sequence[str], step_ref: str,
                           head: Optional[str]) -> bool:
    """Whether an agent already routed this ticket for the step at this head."""
    for body in bodies:
        if (
            not isinstance(body, str)
            or DECLINE_REVIEW_ROUTING_MARKER not in body
        ):
            continue
        record = funnel._marked_json(body, DECLINE_REVIEW_ROUTING_MARKER)
        provenance = funnel.parse_provenance(body)
        if (
            isinstance(record, dict)
            and record.get("type") == CLOSED_HUMAN_STEP_ROUTE
            and record.get("human_step") == step_ref
            and "head_sha" in record
            and record.get("head_sha") == head
            and provenance is not None
            and provenance.get("voice") == "agent"
        ):
            return True
    return False


def render_closed_step_route(step_ref: str, head: Optional[str],
                             blocked: dict) -> str:
    """Render the review-routing comment citing Nate's closed human step."""
    action = str(blocked.get("action") or "").strip()
    if len(action) > 1200:
        action = action[:1197].rstrip() + "..."
    record = {
        "type": CLOSED_HUMAN_STEP_ROUTE,
        "human_step": step_ref,
        "head_sha": head,
        "requested_reason": blocked.get("reason"),
        "requested_action": action,
    }
    return "\n".join((
        "**Review routing: Closed human step**",
        "",
        "This run asked for a human step, but Nate closed {} as not planned "
        "for this ticket, so no step was filed. Review and shaping: reconcile "
        "the ticket, its plan or the review verdict with that answer rather "
        "than asking for the step again.".format(step_ref),
        "",
        DECLINE_REVIEW_ROUTING_MARKER,
        "```json",
        json.dumps(record, ensure_ascii=False, indent=2),
        "```",
    ))


def render_closed_step_hold(step_ref: str, head: Optional[str]) -> str:
    """Render the citation that hands a re-asking ticket to Nate."""
    at = "head {}".format(head[:12]) if head else "no pushed branch"
    return (
        "**Needs Nate: closed human step {ref}**\n\n"
        "This ticket asked again for a human step after it was routed to "
        "review and shaping for {ref}, which Nate closed as not planned, and "
        "nothing has changed since ({at}). No step was filed. Reopen {ref} "
        "to allow the step, or change the ticket, its plan or the review "
        "verdict, before returning it to the agents."
    ).format(ref=step_ref, at=at)



DECLINED_PREREQUISITE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      state
      subIssuesSummary { total completed }
    }
  }
}
"""


def read_declined_prerequisite(ref: str) -> Optional[Dict[str, object]]:
    """Read a named issue and its complete child-ticket completion summary."""
    match = shape.REF_RE.fullmatch(ref)
    if match is None:
        return None
    owner = match.group("owner")
    name = match.group("repo")
    number = int(match.group("number"))
    data = funnel.gh_graphql(
        DECLINED_PREREQUISITE_QUERY,
        owner=owner,
        name=name,
        number=number,
    )
    repository = data.get("repository") if isinstance(data, dict) else None
    issue = repository.get("issue") if isinstance(repository, dict) else None
    if not isinstance(issue, dict):
        return None
    try:
        returned_number = int(issue.get("number"))
    except (TypeError, ValueError):
        return None
    state = str(issue.get("state") or "").upper()
    summary = issue.get("subIssuesSummary")
    if returned_number != number or state not in {"OPEN", "CLOSED"}:
        return None
    facts: Dict[str, object] = {
        "number": number,
        "state": state,
    }
    if isinstance(summary, dict):
        total = summary.get("total")
        completed = summary.get("completed")
        if (
            isinstance(total, int) and not isinstance(total, bool)
            and isinstance(completed, int) and not isinstance(completed, bool)
            and total >= 0 and 0 <= completed <= total
        ):
            facts["children_total"] = total
            facts["children_completed"] = completed
    return facts


def prerequisite_project_landed(
        facts: Optional[Dict[str, object]]) -> Optional[bool]:
    """Return whether a project with child tickets has completed every ticket."""
    if not isinstance(facts, dict):
        return None
    total = facts.get("children_total")
    completed = facts.get("children_completed")
    if (
        not isinstance(total, int) or isinstance(total, bool)
        or not isinstance(completed, int) or isinstance(completed, bool)
        or total <= 0 or completed < 0 or completed > total
    ):
        return None
    return completed == total


def _landed_prerequisite_evidence(
        ref: str, facts: Dict[str, object]) -> str:
    """Explain when closed child tickets disprove an unlanded claim."""
    match = shape.REF_RE.fullmatch(ref)
    if match is None:
        raise ImplementError("cannot render invalid prerequisite ref")
    total = int(facts["children_total"])
    url = "https://github.com/{}/{}/issues/{}".format(
        match.group("owner"), match.group("repo"), match.group("number"),
    )
    return (
        "**False unlanded-prerequisite check:** GitHub reports [{}]({}) has "
        "all {} child tickets completed ({}/{}). The project is landed by "
        "ticket completion; its own PR link and drift or rejected-review "
        "records do not change that result."
    ).format(ref, url, total, total, total)


def clear_declined_ticket_block(repo: str, number: int, *,
                                cwd: pathlib.Path) -> None:
    """Remove a stale blocked label after routing a decline out of Nate's queue."""
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo,
        "--json", "labels",
    )
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str)
        for label in labels
    ):
        raise funnel.GitHubError(
            "could not read labels for {}#{}".format(repo, number)
        )
    if not any(label.get("name") == "blocked" for label in labels):
        return
    proc = funnel._run_gh(
        ["gh", "issue", "edit", str(number), "--repo", repo,
         "--remove-label", "blocked"],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not remove stale blocked label from {}#{}: {}".format(
                repo, number, (proc.stderr or "").strip())
        )


def add_declined_prerequisite_edge(repo: str, number: int, prerequisite: str,
                                   *, cwd: pathlib.Path) -> None:
    """Add only the native blocked-by edge for a verified prerequisite."""
    values = shape.blocked_by_values([prerequisite], repo)
    if len(values) != 1:
        raise ImplementError("could not resolve declined prerequisite edge")
    proc = funnel._run_gh(
        ["gh", "issue", "edit", str(number), "--repo", repo,
         "--add-blocked-by", values[0]],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not add blocked-by edge from {}#{} to {}: {}".format(
                repo, number, prerequisite, (proc.stderr or "").strip()))


def close_declined_defer_note_proof(
        repo: str, number: int, reason: str, *, run: str, agent: str,
        cwd: pathlib.Path) -> None:
    """Close an explicitly accepted defer-note proof as completed.

    The reason is the model's words, so it is made inert (#1798).
    """
    comment = funnel.append_provenance(
        "{} {}".format(funnel.DECLINED_PREFIX,
                       funnel.inert_comment_text(reason)), "agent",
        at=datetime.now(timezone.utc), run=run, agent=agent,
    )
    proc = funnel._run_gh(
        ["gh", "issue", "close", str(number), "--repo", repo,
         "--reason", "completed", "--comment", comment],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not close {}#{} as completed: {}".format(
                repo, number, (proc.stderr or "").strip()))


def post_agent_comment(repo: str, number: int, body: str, *,
                       run: str, agent: str, cwd: pathlib.Path) -> None:
    """Post one runner-owned comment with the agent voice stamped on it."""
    proc = funnel._run_gh(
        ["gh", "issue", "comment", str(number), "--repo", repo,
         "--body", funnel.append_provenance(
             body, "agent", at=datetime.now(timezone.utc),
             run=run, agent=agent)],
        cwd=str(cwd), capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not comment on {}#{}: {}".format(
                repo, number, (proc.stderr or "").strip()))


def _claim_state(ref: str, run: Optional[str], agent: str
                 ) -> Tuple[str, List[funnel.Item]]:
    """Resolve the holder from the timestamped Project claim and heartbeat binds.

    The Project field intentionally remains a timestamp. The run identity is
    the latest heartbeat binding for this ticket at or after that timestamp.
    A missing, conflicting, or unreadable binding is unknown and fails closed.
    """
    try:
        items = funnel.load_project_items_by_refs([ref])
        if items is None or len(items) != 1 or items[0].ref != ref:
            items = funnel.load_items(include_details=False)
        item = funnel.find(items, ref)
    except Exception as exc:
        raise SupersededRunError(ref, "claim could not be read") from exc
    lock = item.in_motion_since
    if lock is None:
        # Empty and unparseable lock values have always meant unlocked.
        return "empty", items
    if not run:
        return "unknown", items
    try:
        import heartbeat

        records = []
        for owner_agent in funnel.AGENTS_BY_ROLE.get("implement", {}):
            records.extend(heartbeat.read_github_strict(owner_agent))
        bindings = heartbeat.bindings(records)
    except Exception as exc:
        raise SupersededRunError(ref, "heartbeat bindings could not be read") from exc

    try:
        claim_ts = int(lock.timestamp())
    except Exception as exc:
        raise SupersededRunError(ref, "claim timestamp is unreadable") from exc
    candidates = []
    for bound_run, binding in bindings.items():
        bound_ts = binding.get("ts")
        if (
            binding.get("do") == "ticket"
            and str(binding.get("work")) == ref
            and isinstance(bound_ts, int)
            and not isinstance(bound_ts, bool)
            and bound_ts >= claim_ts
        ):
            candidates.append((bound_ts, bound_run))
    if not candidates:
        return "unknown", items
    latest_ts = max(ts for ts, _bound_run in candidates)
    holders = {bound_run for ts, bound_run in candidates if ts == latest_ts}
    if len(holders) != 1:
        return "unknown", items
    return ("owned" if next(iter(holders)) == run else "other"), items


def _require_current_claim(ref: str, run: Optional[str], agent: str) -> str:
    """Refuse writes when another run holds the claim or its owner is unknown."""
    state, _items = _claim_state(ref, run, agent)
    if state not in ("owned", "empty"):
        reason = (
            "another run holds the claim"
            if state == "other" else "claim holder is unknown"
        )
        raise SupersededRunError(ref, reason)
    return state


def release_claim(ref: str, *, run: Optional[str] = None,
                  agent: str = "codex") -> None:
    """Release only this run's claim; an empty claim is already released."""
    state, items = _claim_state(ref, run, agent)
    if state == "empty":
        return
    if state != "owned":
        reason = (
            "another run holds the claim"
            if state == "other" else "claim holder is unknown"
        )
        raise SupersededRunError(ref, reason)
    result = funnel.cmd_release(items, datetime.now(timezone.utc), ref)
    if result != 0:
        raise ImplementError("could not release {}".format(ref))


def finish_heartbeat(agent: str, run: str, outcome: str,
                     note: str, work: str) -> None:
    """Finish the exact bound run through heartbeat's validation path."""
    # Publish this process's GraphQL readings before the finish row aggregates
    # all per-command events for the run.
    funnel.report_api_cost(run=run, agent=agent)
    import heartbeat

    result = heartbeat.main([
        "finish", "--agent", agent, "--run", run, "--outcome", outcome,
        "--note", note, "--work", work,
    ])
    if result != 0:
        raise ImplementError("heartbeat finish refused run {}".format(run))


def _bound_work_ref_for_run(run: str, agent: str) -> str:
    """Recover the issued ticket ref when checkout identity lookup timed out."""
    import heartbeat

    records = heartbeat.read_github(agent)
    binding = heartbeat.bindings(records).get(run)
    ref = binding.get("work") if isinstance(binding, dict) else None
    if not isinstance(ref, str) or shape.REF_RE.fullmatch(ref) is None:
        raise ImplementError(
            "could not resolve the ticket bound to timed-out run {}".format(run)
        )
    return ref


def _record_command_timeout(
        exc: CommandTimeoutError, *, run: str, agent: str, ref: str,
        release: Callable[[str], None],
        heartbeat_finish: Callable[[str, str, str, str, str], None],
        work_kept: bool = False) -> None:
    """Finish a timed-out run without attempting another checkout command."""
    if exc.finish_recorded:
        return
    # Do not repeat either effect if one of them fails part-way through.
    exc.finish_recorded = True
    release(ref)
    kept_note = "work was checkpointed" if work_kept else "work NOT kept"
    heartbeat_finish(
        agent, run, "errored", "{}; {}".format(exc, kept_note), ref,
    )


def _is_public_repo(repo: str) -> bool:
    """Whether a heartbeat finish note may quote ``repo``'s content (#1796).

    Heartbeat ledgers are committed to command-center's ``heartbeat`` branch,
    which is public, and command-center is the one public repository in the
    funnel. Every member repository is private, so a note about one carries
    counts and this module's own words only: never a test id, a file path,
    command output or error text. What such a note withholds goes to the
    ticket in its own repository, or is already there. GitHub compares
    repository names without case.
    """
    return (repo or "").casefold() == funnel.REPO.casefold()


def _with_markers(full: str, member: str) -> str:
    """Keep the classifier phrases a member note would otherwise lose.

    ``heartbeat.classify_error`` sorts errored finishes into weather, setup
    and regressions by fixed phrases in the note (``connection timed out``,
    ``could not derive a test command``, ``tests failed:``), and the brief
    shows the regressions (#1289). The phrases are the classifier's words,
    not the repository's, so a member note names any that the full note
    held and the finish classifies exactly as it would have (#1796).
    """
    import heartbeat

    full_text, member_text = full.casefold(), member.casefold()
    found = [
        marker for marker in (
            heartbeat.BEGIN_TIMEOUT_ERROR_MARKERS
            + heartbeat.FLOOR_ERROR_MARKERS
            + heartbeat.UNCLASSIFIED_ERROR_MARKERS
            + heartbeat.REGRESSION_ERROR_MARKERS
        )
        if marker in full_text and marker not in member_text
    ]
    if not found:
        return member
    return "{} | withheld text named: {}".format(member, ", ".join(found))


def _member_kept(kept: str) -> str:
    """``_keep_work``'s phrase without the git error a failure quotes."""
    return "work NOT kept" if kept.startswith("work NOT kept") else kept


_FAILED_TEST_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
_PYTEST_COUNTS_RE = re.compile(
    r"^=* ?(\d+ (?:failed|passed|error)[^=\n]*?) ?=*$", re.M)

#: One count on pytest's summary line. A member note is rebuilt from these
#: alone, so nothing else on that line -- nor a line some test printed that
#: merely looks like one -- reaches the public heartbeat (#1796).
_PYTEST_COUNT_RE = re.compile(
    r"\b(\d+) (failed|passed|skipped|deselected|xfailed|xpassed|warnings?|"
    r"errors?|rerun)\b")

#: How many failing test ids a member-repo ticket comment lists. The note
#: stopped at five to stay one line; the comment is where the next run finds
#: them now (#1796), so it keeps more and counts the rest.
MAX_FAILED_TEST_IDS_IN_COMMENT = 50


def _failed_test_ids(text: str) -> List[str]:
    """The FAILED and ERROR test ids pytest printed, first mention first."""
    ids: List[str] = []
    for match in _FAILED_TEST_RE.finditer(text):
        if match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def _pytest_counts(text: str) -> Optional[str]:
    """pytest's last counts line as printed, or None when there is none."""
    counts = _PYTEST_COUNTS_RE.findall(text)
    return counts[-1].strip() if counts else None


def _first_output_line(text: str) -> str:
    """The failure's first line, cut to fit one note."""
    first = text.splitlines()[0] if text else "unknown test failure"
    return first[:197].rstrip() + "..." if len(first) > 200 else first


def _failure_note(exc: ImplementError, kept: str = "", *, repo: str) -> str:
    """Summarise a test failure for the heartbeat finish note.

    command-center's note names up to five FAILED or ERROR test ids and
    pytest's counts line, so the next run knows what failed (#877), rather
    than the first line of output, which is only the progress dots.

    Any other repository's note carries pytest's counts alone, rebuilt from
    the numbers (#1796). The ids -- or, when pytest named no test, the first
    line -- go to the ticket in its own repository through
    ``_failure_comment``, where the next run's packet reads them.
    """
    text = str(exc)
    if text.startswith("tests timed out:"):
        public = _is_public_repo(repo)
        if public:
            note = text
        else:
            note = _with_markers(text, "tests timed out: merged suite")
        if kept:
            note += " | " + (_member_kept(kept) if not public else kept)
        return note
    ids = _failed_test_ids(text)
    counts = _pytest_counts(text)
    parts = []
    if ids:
        more = " (+{} more)".format(len(ids) - 5) if len(ids) > 5 else ""
        parts.append("; ".join(ids[:5]) + more)
    if counts:
        parts.append(counts)
    if not parts:
        parts.append(_first_output_line(text))
    note = "tests failed: " + " | ".join(parts)
    if kept:
        note += " | " + kept
    if _is_public_repo(repo):
        return note
    numbers = ", ".join(
        "{} {}".format(number, word)
        for number, word in _PYTEST_COUNT_RE.findall(counts or ""))
    member = "tests failed: " + (numbers or "no pytest counts line")
    if kept:
        member += " | " + _member_kept(kept)
    return _with_markers(note, member)


def _failure_comment(exc: ImplementError, kept: str = "") -> str:
    """What a member-repo failure note withholds, for its ticket (#1796).

    The failing test ids, pytest's counts line and, when pytest named no
    test, the first line of the output: what the note carried before. The
    next run reads it in its packet's issue thread. The branch prints that
    output, so the free-text lines are made inert (#1798); an id is one
    ``\\S+`` token, which no marker fits.
    """
    text = str(exc)
    ids = _failed_test_ids(text)
    counts = _pytest_counts(text)
    # No issue number in the text: posted in a member repository, ``#1796``
    # would link that repository's own issue of the same number.
    lines = [
        "**Tests failed** when this run finished the ticket. The heartbeat "
        "note carries pytest's counts only, because the heartbeat is public "
        "and this repository is not; what failed is recorded here for the "
        "next run.",
        "",
    ]
    if ids:
        shown = ids[:MAX_FAILED_TEST_IDS_IN_COMMENT]
        lines.extend(["Failing tests:", "", "```text", *shown, "```"])
        if len(ids) > len(shown):
            lines.append("(+{} more)".format(len(ids) - len(shown)))
        lines.append("")
    elif counts is None:
        lines.extend(["First line of the output:", "", "```text",
                      funnel.inert_comment_text(_first_output_line(text)),
                      "```", ""])
    if counts is not None:
        lines.append("Counts: `{}`".format(funnel.inert_comment_text(counts)))
    if kept:
        lines.append("Work: {}".format(kept))
    return "\n".join(lines).rstrip() + "\n"


def _checkpoint_note(exc: ImplementError, kept: str, *, repo: str) -> str:
    """The note for a checkpoint that failed before the tests ran.

    Its first line is the failed git command, which for ``git add`` lists
    the checkout's paths, then git's own output. A member note says only
    that the checkpoint failed (#1796).
    """
    first = (str(exc).splitlines() or ["unknown error"])[0][:200]
    note = "checkpoint failed: {}; {}".format(first, kept)
    if _is_public_repo(repo):
        return note
    return _with_markers(
        note, "checkpoint failed; {}".format(_member_kept(kept)))


def _stray_note(exc: StrayFileError, *, repo: str) -> str:
    """The stray-file refusal, naming a member repo's paths only by count.

    A scratch directory (``tmp/``, ``scratch/``) matches at any depth, so
    the refusal can list the repository's own paths (#1796).
    """
    if _is_public_repo(repo):
        return str(exc)
    count = len(exc.paths)
    return _with_markers(str(exc), (
        "pre-PR stray-file check refused {} run-scratch path{}; "
        "names withheld".format(count, "" if count == 1 else "s")))


def _conflict_note(exc: MergeConflictError, kept: str, *, repo: str) -> str:
    """The note for work that does not merge with origin/main (#1804).

    A member repo's note counts the conflicted paths without naming them
    (#1796). Nothing goes to the ticket: merging origin/main, which the
    next run must do anyway, shows them again.
    """
    note = "{} | {}".format(exc, kept)
    if _is_public_repo(repo):
        return note
    count = len(exc.paths)
    return _with_markers(note, (
        "merge with origin/main conflicts in {} path{}, names withheld | "
        "{}".format(count, "" if count == 1 else "s", _member_kept(kept))))


def _note_test_source(source: str, *, repo: str) -> str:
    """Name the test command's source; a member repo's CI step by kind only.

    A CI source names the workflow file and the step's name, or its run line
    when the step has none (``override_test_command``). The PR body keeps
    it; a member repo's public note says only that it was a CI step (#1796).
    """
    if _is_public_repo(repo) or not source.startswith("CI "):
        return source
    return "CI workflow step"


def _push_ticket_branch(root: pathlib.Path, branch: str, *, ref: str,
                        run: Optional[str], agent: str) -> None:
    """Push the ticket branch as a fast-forward, even after a rebase (#890).

    A stale-PR verdict asks the engineer to rebase onto main, which rewrites
    the branch the remote already holds, so a plain push is rejected as
    non-fast-forward and the ticket wedges. When the remote branch exists and
    is not an ancestor of HEAD, record it with an ``ours`` merge: the tree is
    exactly this run's, the old commits stay in the history, and the push is a
    fast-forward. Never a force-push; main squash-merges, so the extra merge
    commit never reaches it. The ownership guard runs immediately before the
    remote branch can be changed.
    """
    _require_current_claim(ref, run, agent)
    fetched = _run(["git", "fetch", "origin",
                    "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch)],
                   cwd=root, check=False,
                   timeout=REMOTE_GIT_TIMEOUT_SECONDS)
    if fetched.returncode == 0:
        remote_ref = "origin/{}".format(branch)
        ancestor = _run(["git", "merge-base", "--is-ancestor", remote_ref, "HEAD"],
                        cwd=root, check=False,
                        timeout=LOCAL_GIT_TIMEOUT_SECONDS)
        if ancestor.returncode != 0:
            _run(["git", "merge", "-s", "ours", "--no-edit", "-m",
                  "Record the previous {} tip before pushing the rebased branch".format(branch),
                  remote_ref], cwd=root,
                 timeout=LOCAL_GIT_TIMEOUT_SECONDS)
    _require_current_claim(ref, run, agent)
    _run(["git", "push", "--set-upstream", "origin", branch], cwd=root,
         timeout=REMOTE_GIT_TIMEOUT_SECONDS)

def _remove_codex_run_checkout(root: pathlib.Path, number: int,
                               agent: str) -> bool:
    """Remove only this Codex ticket checkout under a runtime codex-runs/.

    The per-run clone is named ``ticket-<number>-<UTC timestamp>`` and is
    owner-only. Restrict removal to that exact direct child; finish-ticket also
    runs from session workspaces and other agents' checkouts, which must remain
    untouched.

    Live runs clone into the heartbeat directory's ``codex-runs/``
    (``heartbeat.SPOOL_DIR``), because that is the Codex sandbox's only
    writable root. ``CLAUDE_DIR/codex-runs`` does not exist there, and while
    it was the only root every live checkout was kept (#1856). Either root
    counts, and one that is missing or a symlink is skipped rather than
    fatal. The merged suite's temporary
    worktrees also sit in the runs root (#1804), but one level down, under
    ``review-evidence-*``, so no guard below can match them; they are
    removed by review_evidence itself.

    Call it only once this run has pushed the ticket branch. #1711 also
    removed the checkout once a finish recorded work not kept, but that
    checkout can hold the only copy of the work: a stray-file refusal, a
    ``_keep_work`` failure before its push, a superseded run, a decline or
    a human step. Those paths never ran live while the root went unmatched,
    so matching it (#1856) would have started deleting that work. They leave
    the directory for a later run or Nate to recover: a leaked directory is
    the safe failure, lost work is not.
    """
    if agent != "codex":
        return False
    checkout = pathlib.Path(root)
    if checkout.is_symlink():
        return False
    try:
        checkout = checkout.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    import heartbeat
    runs_root = None
    for candidate in (pathlib.Path(funnel.CLAUDE_DIR) / "codex-runs",
                      pathlib.Path(heartbeat.SPOOL_DIR) / "codex-runs"):
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if checkout.parent == resolved:
            runs_root = resolved
            break
    match = re.fullmatch(
        r"ticket-([1-9][0-9]*)-([0-9]{8}T[0-9]{12}Z)", checkout.name,
    )
    if (match is None or int(match.group(1)) != number
            or runs_root is None):
        return False
    try:
        info = checkout.stat()
        if (not checkout.is_dir() or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            return False
        current = pathlib.Path.cwd().resolve()
        try:
            current.relative_to(checkout)
        except ValueError:
            pass
        else:
            # Keep later GitHub/heartbeat calls in a live working directory.
            os.chdir(runs_root)
        shutil.rmtree(checkout)
        return True
    except OSError:
        # Cleanup is best-effort after the durable finish effect. Never turn a
        # completed push or recorded non-kept result into a stranded claim.
        return False


def _keep_work(root: pathlib.Path, number: int, branch: str, *, ref: str,
               run: Optional[str], agent: str,
               reason: str = "tests failing") -> Tuple[str, bool]:
    """Commit and push explicit paths, so a failure loses time only.

    Never opens a PR. Returns a short phrase for the heartbeat note. The same
    pre-PR scratch check applies here so a WIP branch cannot carry run debris.
    The second result is true only once the branch is pushed, which is the
    only time the Codex checkout may be removed (#1856); every other result
    keeps it, for diagnosis or because it holds the only copy of the work.
    """
    try:
        _require_current_claim(ref, run, agent)
        paths = _working_tree_paths(root)
        if paths:
            _stage_explicit_paths(root, paths)
            _check_no_run_scratch(root, about_to_commit=paths)
            _run(["git", "commit", "-m",
                  "WIP #{}: {}".format(number, reason)], cwd=root,
                 timeout=LOCAL_GIT_TIMEOUT_SECONDS)
        else:
            _check_no_run_scratch(root)
        ahead = _run(["git", "rev-list", "--count", "origin/main..HEAD"],
                     cwd=root,
                     timeout=LOCAL_GIT_TIMEOUT_SECONDS).stdout.strip()
        if not ahead.isdigit() or int(ahead) < 1:
            return "no work to keep", False
        _push_ticket_branch(root, branch, ref=ref, run=run, agent=agent)
        return "work kept on {}".format(branch), True
    except SupersededRunError:
        raise
    except ImplementError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "unknown error"
        return "work NOT kept: {}".format(first[:120]), False


def _checkpoint_work(root: pathlib.Path, number: int, branch: str, *,
                     ref: str, run: Optional[str], agent: str) -> bool:
    """Push the current implementation before a long test run can lose it.

    Ticket work is checkpointed on its deterministic remote branch at each
    meaningful step. The final answer is validated before this helper runs;
    tests and PR creation still happen afterward. A superseded run checks its
    claim before committing and again immediately before pushing.
    """
    paths = _working_tree_paths(root)
    if not paths:
        _check_no_run_scratch(root)
        return False
    _stage_explicit_paths(root, paths)
    _check_no_run_scratch(root, about_to_commit=paths)
    _require_current_claim(ref, run, agent)
    _run(["git", "commit", "-m",
          "WIP #{}: checkpoint implementation".format(number)], cwd=root,
         timeout=LOCAL_GIT_TIMEOUT_SECONDS)
    _push_ticket_branch(root, branch, ref=ref, run=run, agent=agent)
    return True


def _recover_answer_error(
        exc: ImplementError, *, run: str, agent: str = "codex",
        repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
        release: Optional[Callable[[str], None]] = None,
        heartbeat_finish: Callable[[str, str, str, str, str], None]
        = finish_heartbeat) -> bool:
    """Keep a dirty checkout and close its run after an unreadable answer.

    A clean checkout retains the old fail-closed behaviour: there is no model
    work to preserve, so this helper performs no side effects and returns
    false.  Dirty work follows the same WIP-push path as a test failure, but
    the heartbeat names the answer error that prevented validation.
    """
    context = checkout_context(cwd)
    if not _working_tree_paths(context["root"]):
        return False
    resolved = resolve_checkout_repo(context["root"], repo)
    ref = "{}#{}".format(resolved, context["number"])
    release_effect = release or (
        lambda target: release_claim(target, run=run, agent=agent)
    )
    kept, pushed = _keep_work(
        context["root"], context["number"], context["branch"],
        ref=ref, run=run, agent=agent, reason="answer unreadable",
    )
    release_effect(ref)
    first = str(exc).splitlines()[0] if str(exc) else "unknown answer error"
    note = "answer error: {} | {}".format(first[:200], kept)
    if not _is_public_repo(resolved):
        # The answer error is this module's validation text; the kept phrase
        # can quote a failed git command over the checkout's paths (#1796).
        note = _with_markers(note, "answer error: {} | {}".format(
            first[:200], _member_kept(kept)))
    heartbeat_finish(agent, run, "errored", note, ref)
    if pushed:
        _remove_codex_run_checkout(context["root"], context["number"], agent)
    return True


def finish_done(answer: dict, *, run: str, agent: str = "codex",
                repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
                test_commands: Optional[Sequence[Sequence[str]]] = None,
                release: Optional[Callable[[str], None]] = None,
                heartbeat_finish: Callable[[str, str, str, str, str], None]
                = finish_heartbeat,
                pr_effect: Callable[[str, dict, dict, str], dict]
                = create_or_update_pr,
                close_effect: Callable[..., None]
                = close_no_diff_ticket,
                comment_effect: Callable[..., None] = post_agent_comment,
                extra_note: Optional[str] = None) -> dict:
    """Perform every happy-path effect and return the resulting PR identity.

    The suite runs on the work merged with current origin/main where a merge
    can be made (#1804, ``_run_finish_tests``): a failure main does not
    share, or a conflict, finishes ``errored`` like any failing test.

    A member repository's notes withhold its content (#1796): a failing
    test's ids go to the ticket through ``comment_effect`` instead.
    """
    context = checkout_context(cwd)
    resolved = resolve_checkout_repo(context["root"], repo)
    ticket = fetch_ticket(resolved, context["number"])
    ref = ticket["ref"]
    release_effect = release or (
        lambda target: release_claim(target, run=run, agent=agent)
    )
    phase = "checkpoint"
    checkpointed = False
    try:
        continued = _remote_branch_exists(context["root"], context["branch"])
        checkpointed = _checkpoint_work(
            context["root"], context["number"], context["branch"],
            ref=ref, run=run, agent=agent,
        )
        phase = "tests"
        tests, test_source, tested, merged = _run_finish_tests(
            context["root"], test_commands)
        reproduced = _run_reproduction(context["root"], merged)
    except SupersededRunError:
        raise
    except StrayFileError as exc:
        release_effect(ref)
        heartbeat_finish(
            agent, run, "errored", _stray_note(exc, repo=resolved), ref)
        # The note asks for the scratch to be removed and the work
        # re-staged, and the work may be pushed nowhere: keep the checkout
        # (#1856).
        raise
    except CommandTimeoutError as exc:
        _record_command_timeout(
            exc, run=run, agent=agent, ref=ref, release=release_effect,
            heartbeat_finish=heartbeat_finish, work_kept=checkpointed,
        )
        if checkpointed:
            _remove_codex_run_checkout(
                context["root"], context["number"], agent)
        raise
    except ImplementError as exc:
        # A failed checkpoint or test must not strand the run. Retry saving
        # remaining work without opening a PR, then finish errored so the next
        # run continues the branch instead of starting again from main. A
        # conflict with main finishes the same way, so the backoff bounds a
        # ticket that keeps conflicting (#1804).
        conflict = isinstance(exc, MergeConflictError)
        kept, pushed = _keep_work(
            context["root"], context["number"], context["branch"],
            ref=ref, run=run, agent=agent,
            reason=("merge conflict with origin/main" if conflict else
                    "tests failed" if phase == "tests" else
                    "checkpoint retry"),
        )
        posted = None
        if phase == "tests" and not conflict and not _is_public_repo(resolved):
            # The ids leave the public note, so they go to the ticket, and
            # before the release: the run that claims it next reads them in
            # its packet (#1796). A failed post must not strand the run.
            try:
                comment_effect(
                    resolved, context["number"], _failure_comment(exc, kept),
                    run=run, agent=agent, cwd=context["root"],
                )
                posted = True
            except (funnel.GitHubError, OSError, subprocess.SubprocessError):
                posted = False
        release_effect(ref)
        if conflict:
            note = _conflict_note(exc, kept, repo=resolved)
        elif phase == "tests":
            note = _failure_note(exc, kept, repo=resolved)
            if isinstance(exc, MergedSuiteError):
                # This module's words, so a member note keeps them too: the
                # next run must merge main to see the failure.
                note += " | on the merge with origin/main"
        else:
            note = _checkpoint_note(exc, kept, repo=resolved)
        if posted is False:
            note += " | failing tests NOT posted to the ticket"
        heartbeat_finish(agent, run, "errored", note, ref)
        if pushed:
            _remove_codex_run_checkout(
                context["root"], context["number"], agent)
        raise
    try:
        if _working_tree_paths(context["root"]):
            _require_current_claim(ref, run, agent)
        committed = _commit_if_needed(
            context["root"], context["number"], answer["summary"],
            allow_empty=True,
        )
        if committed is None:
            evidence = _verify_done_evidence(
                answer.get("evidence") or [], run=run, agent=agent,
            )
            _require_current_claim(ref, run, agent)
            close_effect(
                resolved, context["number"], cwd=context["root"],
            )
            release_effect(ref)
            note = "no-diff close as completed; verified evidence: {}".format(
                ", ".join(evidence))
            if extra_note:
                note += "; " + extra_note.strip()
            # Nothing was pushed, so the checkout stays (#1856).
            heartbeat_finish(agent, run, "done", note, ref)
            return {"number": context["number"], "url": ticket["url"],
                    "closed": True}
        # Re-read immediately before pushing so a ticket branch never carries
        # scratch files introduced after its commit.
        _check_no_run_scratch(context["root"])
        _push_ticket_branch(
            context["root"], context["branch"],
            ref=ref, run=run, agent=agent,
        )
        # The evidence block is keyed to this, the commit the PR's head now
        # is, after any merge the push added (#1805).
        pushed = _run(["git", "rev-parse", "HEAD"], cwd=context["root"],
                      timeout=LOCAL_GIT_TIMEOUT_SECONDS).stdout.strip()
        # Recheck before the PR effect in case the merge or push surfaced
        # another scratch file.
        _check_no_run_scratch(context["root"])
    except SupersededRunError:
        raise
    except StrayFileError as exc:
        release_effect(ref)
        heartbeat_finish(
            agent, run, "errored", _stray_note(exc, repo=resolved), ref)
        # The note asks for the scratch to be removed and the work
        # re-staged, and the work may be pushed nowhere: keep the checkout
        # (#1856).
        raise
    except CommandTimeoutError as exc:
        _record_command_timeout(
            exc, run=run, agent=agent, ref=ref, release=release_effect,
            heartbeat_finish=heartbeat_finish, work_kept=checkpointed,
        )
        if checkpointed:
            _remove_codex_run_checkout(
                context["root"], context["number"], agent)
        raise
    prior_base = (
        merged.get("base") if isinstance(merged, dict) else "origin/main"
    ) or "origin/main"
    prior_fixes = _prior_fix_evidence(context["root"], prior_base, pushed)
    body = render_pr_body(
        ticket, answer, continued=continued, tests=tests,
        test_source=test_source,
        evidence=render_evidence_block(
            sha=pushed, merged=merged, reproduction=reproduced,
            repo=resolved, prior_fixes=prior_fixes))
    pr = pr_effect(resolved, context, ticket, body)
    release_effect(ref)
    note = "PR #{}".format(pr["number"])
    if test_source is not None:
        note += " (tests: {})".format(
            _note_test_source(test_source, repo=resolved))
    if tested:
        # Absent when the head ran alone, so a lane that can never merge
        # shows in the heartbeat rather than passing silently (#1804).
        note += "; " + tested
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "done", note, ref)
    _remove_codex_run_checkout(context["root"], context["number"], agent)
    return pr

def finish_blocked_on_human(
        blocked: dict, *, run: str, agent: str = "codex",
        repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
        release: Optional[Callable[[str], None]] = None,
        heartbeat_finish: Callable[[str, str, str, str, str], None]
        = finish_heartbeat,
        create_effect: Callable[..., dict] = create_human_step_issue,
        block_effect: Callable[..., None] = mark_ticket_blocked,
        comment_effect: Callable[..., None] = post_agent_comment,
        needs_effect: Callable[[str, str], None] = write_human_step_needs,
        session_needs_effect: Callable[[str, str], None]
        = write_session_step_needs,
        sub_issues_effect: Callable[[str, int], List[Dict[str, object]]]
        = read_parent_sub_issues,
        comments_effect: Callable[[str, int], List[str]]
        = read_ticket_comment_bodies,
        head_effect: Callable[[pathlib.Path, str], Optional[str]]
        = remote_ticket_head,
        route_needs_effect: Callable[[str, str], None] = write_declined_needs,
        human_needs_effect: Callable[[str, str], None]
        = write_human_step_needs,
        extra_note: Optional[str] = None) -> dict:
    """File the human step, block the ticket, release, and finish. No PR.

    No test run, commit, or push happens here: the finish records the
    implementation as not kept and leaves this run's Codex checkout, which
    may hold the only copy of it (#1856; #1711 removed it).
    A failure after the sub-issue exists names it, so the retry starts
    from GitHub's truth rather than filing a second one. The step's Needs
    follows its reason: ``session_needs_effect`` for a step a Claude Code
    session can do, ``needs_effect`` for Nate's (#1901).

    A human step Nate closed as not planned for this ticket is his answer
    that no step will happen, so nothing is filed again (#1726): the ticket
    is routed to review and shaping instead, or, when it was already routed
    for that step at the current head, handed to Nate.
    """
    context = checkout_context(cwd)
    resolved = resolve_checkout_repo(context["root"], repo)
    ticket = fetch_ticket(resolved, context["number"])
    ref = ticket["ref"]
    release_effect = release or (
        lambda target: release_claim(target, run=run, agent=agent)
    )
    parent = ticket.get("parent") or {}
    parent_number = parent.get("number")
    if not isinstance(parent_number, int):
        raise ImplementError(
            "blocked_on_human needs the ticket's parent to file the "
            "human step under")
    # Read before filing: a lane filed a second step for one ticket after Nate
    # closed the first as not planned, re-blocking the ticket he had unblocked.
    # Matching is on the ticket number the step names, never on the action's
    # wording, which no code can judge equivalent (#1658).
    closed = closed_human_steps_for(
        sub_issues_effect(parent_repo(resolved, ticket), parent_number),
        resolved, context["number"])
    if closed:
        return _finish_closed_human_step(
            blocked, str(closed[-1]["ref"]), ticket=ticket, resolved=resolved,
            context=context, run=run, agent=agent, release=release_effect,
            heartbeat_finish=heartbeat_finish, block_effect=block_effect,
            comment_effect=comment_effect, comments_effect=comments_effect,
            head_effect=head_effect, route_needs_effect=route_needs_effect,
            human_needs_effect=human_needs_effect, extra_note=extra_note)
    title = render_human_step_title(blocked["action"])
    body = render_human_step_body(
        parent_number=parent_number, ticket_number=context["number"],
        reason=blocked["reason"], action=blocked["action"])
    created = create_effect(
        resolved, parent_number, title, body, cwd=context["root"])
    step_needs = HUMAN_STEP_NEEDS[blocked["reason"]]
    try:
        if step_needs == "claude-code-environment":
            session_needs_effect(created["url"], created["ref"])
        else:
            needs_effect(created["url"], created["ref"])
        block_effect(resolved, context["number"],
                     blocked_by=created["number"], cwd=context["root"])
        comment_effect(
            resolved, context["number"],
            "**Blocked on #{}:** Complete the human step before resuming "
            "this ticket.".format(created["number"]),
            run=run, agent=agent, cwd=context["root"])
    except funnel.GitHubError as exc:
        raise funnel.GitHubError(
            "{} (already created: {})".format(exc, created["ref"]))
    release_effect(ref)
    note = "stopped: human step filed as #{}; ticket blocked; no PR opened".format(
        created["number"])
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "skipped-human-step", note, ref)
    return {"ticket": ref,
            "human_step": {"number": created["number"],
                           "ref": created["ref"],
                           "url": created["url"]}}


def _finish_closed_human_step(
        blocked: dict, step_ref: str, *, ticket: dict, resolved: str,
        context: dict, run: str, agent: str,
        release: Callable[[str], None],
        heartbeat_finish: Callable[[str, str, str, str, str], None],
        block_effect: Callable[..., None],
        comment_effect: Callable[..., None],
        comments_effect: Callable[[str, int], List[str]],
        head_effect: Callable[[pathlib.Path, str], Optional[str]],
        route_needs_effect: Callable[[str, str], None],
        human_needs_effect: Callable[[str, str], None],
        extra_note: Optional[str]) -> dict:
    """Route a ticket whose step Nate closed as not planned; file nothing.

    Routed like an accept-body conflict: Needs stays with the agents and a
    marked comment cites the closed step for review and shaping. Returning
    the ticket unblocked would loop against the same verdict every ten
    minutes, so a second request at an unchanged head goes to Nate instead
    (#1658). The route comment is the guard's record; if it cannot be posted
    the ticket falls back to Nate's visible queue, as that path does.
    """
    ref = ticket["ref"]
    number = context["number"]
    root = context["root"]
    head = head_effect(root, context["branch"])
    if routed_for_closed_step(comments_effect(resolved, number), step_ref,
                              head):
        human_needs_effect(ticket["url"], ref)
        comment_effect(resolved, number,
                       render_closed_step_hold(step_ref, head),
                       run=run, agent=agent, cwd=root)
        routed = "human"
        outcome = "already routed at this head; Needs human"
    else:
        route_needs_effect(ticket["url"], ref)
        try:
            comment_effect(resolved, number,
                           render_closed_step_route(step_ref, head, blocked),
                           run=run, agent=agent, cwd=root)
            routed = "review"
            outcome = "routed to review and shaping"
        except (funnel.GitHubError, OSError, subprocess.SubprocessError):
            human_needs_effect(ticket["url"], ref)
            block_effect(resolved, number, cwd=root)
            routed = "blocked"
            outcome = "review routing failed; ticket left blocked"
    release(ref)
    note = "human step not filed: {} was closed as not planned; {}".format(
        step_ref, outcome)
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "skipped-blocked", note, ref)
    return {"ticket": ref, "closed_human_step": step_ref, "routed": routed}


def finish_declined(
        reason: str, *, run: str, agent: str = "codex",
        repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
        release: Optional[Callable[[str], None]] = None,
        heartbeat_finish: Callable[[str, str, str, str, str], None]
        = finish_heartbeat,
        block_effect: Callable[..., None] = mark_ticket_blocked,
        comment_effect: Callable[..., None] = post_agent_comment,
        needs_effect: Callable[[str, str], None] = write_declined_needs,
        declined_needs_effect: Callable[[str, str], None]
        = write_declined_agent_needs,
        external_event_needs_effect: Callable[[str, str], None]
        = write_declined_external_event_needs,
        prerequisite_facts_effect: Callable[[str], Optional[Dict[str, object]]]
        = read_declined_prerequisite,
        clear_block_effect: Callable[..., None] = clear_declined_ticket_block,
        prerequisite_edge_effect: Callable[..., None]
        = add_declined_prerequisite_edge,
        defer_note_close_effect: Callable[..., None]
        = close_declined_defer_note_proof,
        extra_note: Optional[str] = None) -> dict:
    """Record the decline and route only verified, parseable reasons."""
    context = checkout_context(cwd)
    resolved = resolve_checkout_repo(context["root"], repo)
    ticket = fetch_ticket(resolved, context["number"])
    ref = ticket["ref"]
    release_effect = release or (
        lambda target: release_claim(target, run=run, agent=agent)
    )
    decline_class, decline_target = classify_decline_reason(
        reason, resolved, ticket.get("body") or "")
    if decline_class == "defer-note-proof":
        defer_note_close_effect(
            resolved, context["number"], reason,
            run=run, agent=agent, cwd=context["root"],
        )
        release_effect(ref)
        note = "closed as completed: allowed defer-note proof"
        if extra_note:
            note += "; " + extra_note.strip()
        heartbeat_finish(agent, run, "done", note, ref)
        return {"ticket": ref, "declined": reason}
    accept_conflict_routed = (
        decline_class == "accept-body-conflict" and decline_target is not None
    )
    prerequisite_recorded = False
    false_unlanded_prerequisite_routed = False
    prerequisite_evidence: Optional[str] = None
    unsatisfiable_acceptance_routed = (
        decline_class == "unsatisfiable-acceptance"
    )
    pending_gate_answer_routed = decline_class == "pending-gate-answer"
    if accept_conflict_routed:
        # Keep this in an agent lane so review and shaping can see the ticket.
        needs_effect(ticket["url"], ref)
    elif decline_class == "prerequisite-ticket" and decline_target is not None:
        try:
            prerequisite_facts = prerequisite_facts_effect(decline_target)
            landed = prerequisite_project_landed(prerequisite_facts)
            if landed is True:
                prerequisite_evidence = _landed_prerequisite_evidence(
                    decline_target, prerequisite_facts or {},
                )
                false_unlanded_prerequisite_routed = True
            elif (
                isinstance(prerequisite_facts, dict)
                and prerequisite_facts.get("state") == "OPEN"
            ):
                prerequisite_edge_effect(
                    resolved, context["number"], decline_target,
                    cwd=context["root"],
                )
                prerequisite_recorded = True
        except (funnel.GitHubError, ImplementError, shape.ShapeError,
                OSError, subprocess.SubprocessError):
            # A failed lookup or edge write keeps today's visible block.
            prerequisite_recorded = False
    if (not prerequisite_recorded and not accept_conflict_routed
            and not false_unlanded_prerequisite_routed
            and not unsatisfiable_acceptance_routed
            and not pending_gate_answer_routed):
        # Unknown declines and failed prerequisite handoffs have no machine-
        # readable condition that can clear them. Keep the ticket blocked and
        # route its Unblock question through the funnel watch.
        declined_needs_effect(ticket["url"], ref)
        block_effect(resolved, context["number"], cwd=context["root"])
    # The reason is the model's words: one line with no ``<!--``, so it can
    # never form a runner marker in the owner's comment (#1798).
    declined_comment = "{} {}".format(
        funnel.DECLINED_PREFIX, funnel.inert_comment_text(reason))
    if prerequisite_evidence is not None:
        declined_comment += "\n\n" + prerequisite_evidence
    comment_effect(resolved, context["number"], declined_comment,
                   run=run, agent=agent, cwd=context["root"])
    if false_unlanded_prerequisite_routed:
        # Record proof before returning a false decline to the agent queue.
        clear_block_effect(
            resolved, context["number"], cwd=context["root"],
        )
        needs_effect(ticket["url"], ref)
    elif unsatisfiable_acceptance_routed:
        clear_block_effect(
            resolved, context["number"], cwd=context["root"],
        )
        needs_effect(ticket["url"], ref)
    elif pending_gate_answer_routed:
        clear_block_effect(
            resolved, context["number"], cwd=context["root"],
        )
        external_event_needs_effect(ticket["url"], ref)
    routing_failed = False
    if accept_conflict_routed:
        try:
            comment_effect(
                resolved, context["number"],
                _declined_review_routing_comment(reason, decline_target),
                run=run, agent=agent, cwd=context["root"],
            )
        except (funnel.GitHubError, OSError, subprocess.SubprocessError):
            # An unposted handoff must not leave a false unblocked ticket.
            declined_needs_effect(ticket["url"], ref)
            block_effect(resolved, context["number"], cwd=context["root"])
            routing_failed = True
    elif unsatisfiable_acceptance_routed or pending_gate_answer_routed:
        try:
            if unsatisfiable_acceptance_routed:
                digest = hashlib.sha256(
                    (ticket.get("body") or "").encode("utf-8")
                ).hexdigest()
                routing_comment = (
                    _declined_unsatisfiable_acceptance_comment(reason, digest)
                )
            elif isinstance(decline_target, str):
                routing_comment = _declined_pending_gate_answer_comment(
                    reason, decline_target,
                )
            else:
                raise ImplementError(
                    "pending gate decline has no named gate answer"
                )
            comment_effect(
                resolved, context["number"], routing_comment,
                run=run, agent=agent, cwd=context["root"],
            )
        except (funnel.GitHubError, ImplementError, OSError,
                subprocess.SubprocessError):
            # The route comment is the durable queue hold. If it cannot be
            # recorded, keep the ticket blocked for the watch to resolve.
            declined_needs_effect(ticket["url"], ref)
            block_effect(resolved, context["number"], cwd=context["root"])
            routing_failed = True
    release_effect(ref)
    first = reason.splitlines()[0] if reason else "no reason given"
    if len(first) > 200:
        first = first[:197].rstrip() + "..."
    note = "declined: {}".format(first)
    if not _is_public_repo(resolved):
        # The reason is the model's prose about a private ticket and can
        # quote its files and tests. The declined comment above already put
        # it on the ticket (#1796).
        note = "declined; reason on the ticket"
    if accept_conflict_routed and not routing_failed:
        note += "; routed to review for Accept/body conflict"
    elif false_unlanded_prerequisite_routed:
        note += (
            "; false unlanded-prerequisite claim disproved by closed child "
            "tickets; returned to agent queue"
        )
    elif unsatisfiable_acceptance_routed:
        note += (
            "; acceptance cannot be met; routed for reshaping"
            if not routing_failed else
            "; reshaping route failed; ticket left blocked"
        )
    elif pending_gate_answer_routed:
        note += (
            "; waiting for the named gate answer"
            if not routing_failed else
            "; gate-answer route failed; ticket left blocked"
        )
    elif routing_failed:
        note += "; review routing failed; ticket left blocked"
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "skipped-blocked", note, ref)
    return {"ticket": ref, "declined": reason}


def dry_run_main() -> int:
    """Print the resolved test plan for this checkout without running it."""
    try:
        context = checkout_context(None)
        commands, source = default_test_plan(context["root"])
    except (ImplementError, OSError, subprocess.SubprocessError) as exc:
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "branch": context["branch"],
        "number": context["number"],
        "test_commands": [shlex.join(command) for command in commands],
        "test_source": source,
    }, indent=2, sort_keys=True))
    return 0


def _record_superseded_finish(args: argparse.Namespace, ref: str,
                              reason: str) -> int:
    """Finish a refused run without releasing, committing, or pushing work.

    The checkout stays: this run keeps none of its work, so the checkout
    may hold the only copy (#1856).
    """
    if not args.run:
        print("finish-ticket: cannot record superseded finish without --run",
              file=sys.stderr)
        return 1
    note = "superseded: {}; work not kept".format(reason)
    try:
        finish_heartbeat(args.agent, args.run, "errored", note, ref)
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("finish-ticket: {}; heartbeat finish failed: {}".format(
            note, exc), file=sys.stderr)
        return 1
    print(json.dumps({
        "ticket": ref, "superseded": True, "work_kept": False,
    }, sort_keys=True))
    return 0


def _finish_ticket(args: argparse.Namespace) -> int:
    try:
        try:
            context = checkout_context()
        except ImplementError:
            # The effect functions validate the checkout before writing. Keep
            # their CLI routing seams usable by callers without a checkout.
            context = None
        if context is not None:
            resolved = resolve_checkout_repo(context["root"], args.repo)
            ref = "{}#{}".format(resolved, context["number"])
            _require_current_claim(ref, args.run, args.agent)
    except SupersededRunError as exc:
        return _record_superseded_finish(args, exc.ref, exc.reason)
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1

    try:
        if args.answer_file is not None:
            answer = read_answer(args.answer_file)
        elif args.answer == "-":
            answer = read_answer("-")
        else:
            answer = parse_answer(args.answer)
    except ImplementError as exc:
        try:
            _recover_answer_error(
                exc, run=args.run, agent=args.agent, repo=args.repo,
                release=lambda target: release_claim(
                    target, run=args.run, agent=args.agent),
                heartbeat_finish=finish_heartbeat,
            )
        except SupersededRunError as recovery_exc:
            return _record_superseded_finish(
                args, recovery_exc.ref, recovery_exc.reason,
            )
        except (funnel.GitHubError, ImplementError, OSError,
                subprocess.SubprocessError) as recovery_exc:
            print(
                "finish-ticket: {}; answer-error recovery failed: {}".format(
                    exc, recovery_exc),
                file=sys.stderr,
            )
            return 1
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1
    try:
        if "done" in answer:
            result = finish_done(
                answer, run=args.run, agent=args.agent, repo=args.repo,
                release=lambda target: release_claim(
                    target, run=args.run, agent=args.agent),
                extra_note=args.note,
            )
        elif "blocked_on_human" in answer:
            result = finish_blocked_on_human(
                answer["blocked_on_human"], run=args.run, agent=args.agent,
                repo=args.repo, release=lambda target: release_claim(
                    target, run=args.run, agent=args.agent),
                extra_note=args.note,
            )
        else:
            result = finish_declined(
                answer["declined"], run=args.run, agent=args.agent,
                repo=args.repo, release=lambda target: release_claim(
                    target, run=args.run, agent=args.agent),
                extra_note=args.note,
            )
    except CommandTimeoutError as exc:
        if not exc.finish_recorded:
            try:
                ref = _bound_work_ref_for_run(args.run, args.agent)
                _record_command_timeout(
                    exc, run=args.run, agent=args.agent, ref=ref,
                    release=lambda target: release_claim(
                        target, run=args.run, agent=args.agent),
                    heartbeat_finish=finish_heartbeat,
                )
            except (funnel.GitHubError, ImplementError, OSError,
                    subprocess.SubprocessError) as recovery_exc:
                print(
                    "finish-ticket: {}; timeout outcome could not be recorded: {}"
                    .format(exc, recovery_exc),
                    file=sys.stderr,
                )
                return 1
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1
    except SupersededRunError as exc:
        return _record_superseded_finish(args, exc.ref, exc.reason)
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def finish_main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI for one structured answer's finish-ticket effect sequence."""
    parser = argparse.ArgumentParser(
        description="publish, block, or decline one ticket, then release "
                    "and finish its run"
    )
    answer_group = parser.add_mutually_exclusive_group()
    answer_group.add_argument(
        "--answer", required=False, default=None,
        help="structured answer as inline JSON, or - for stdin",
    )
    answer_group.add_argument(
        "--answer-file", required=False, default=None,
        help="path to a file containing the structured answer",
    )
    parser.add_argument("--run", required=False, default=None,
                        help="bound heartbeat run id")
    parser.add_argument("--agent", default="codex",
                        choices=("codex", "muse", "zcode", "claude"))
    parser.add_argument("--repo", default=None,
                        help="owner/name; normally derived from the checkout")
    parser.add_argument("--note", default=None,
                        help="extra run note, such as a stale-claim takeover")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved test plan and exit; "
                             "runs nothing and changes nothing")
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run_main()
    if (args.answer is None and args.answer_file is None) or not args.run:
        parser.error(
            "one of --answer/--answer-file and --run are required "
            "without --dry-run")
    caller = funnel.graphql_caller_for_run(args.run, args.agent)
    with funnel.graphql_caller(caller):
        return _finish_ticket(args)
