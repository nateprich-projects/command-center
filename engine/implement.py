#!/usr/bin/env python3
"""Assemble implementation evidence and finish one ticket (#815).

The model changes files and returns one small structured answer.  This module
owns the protocol around that judgement: read the ticket packet, verify the
checkout, run its tests, commit and push, open the pull request, release the
claim, and finish the heartbeat bound to the ticket.

The package imports ``funnel`` and never the reverse.  ``funnel finish-ticket``
is a process-level CLI forwarder to the standalone entry point, so importing
this module cannot create a dependency cycle.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402


class ImplementError(RuntimeError):
    """A packet or finish contract could not be completed safely."""


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


def fetch_plan(repo: str, ticket: dict) -> Optional[dict]:
    """Read the project issue whose body is this ticket's settled plan."""
    parent = ticket.get("parent") or {}
    number = parent.get("number")
    if not isinstance(number, int):
        return None
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
    """Read the newest verdict for the ticket's open PR, when one exists."""
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--head", "ticket/{}".format(number), "--json",
        "number,headRefOid,updatedAt", "--limit", "10",
    )
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError(
            "could not list open PRs for {}#{}".format(repo, number)
        )
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


def build_packet(*, repo: str, ticket: dict, plan: Optional[dict],
                 verdict: dict, prior_run: Optional[dict]) -> dict:
    """Build one JSON-serialisable implementation packet from fetched facts."""
    return {
        "repo": repo,
        "ticket": ticket,
        "plan": plan,
        "verdict": verdict,
        "prior_run": prior_run,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def collect(repo: Optional[str], number: int, *, agent: str = "codex") -> dict:
    """Fetch every read-only input needed to implement one ticket."""
    resolved = funnel.resolve_repo(repo)
    ticket = fetch_ticket(resolved, number)
    return build_packet(
        repo=resolved,
        ticket=ticket,
        plan=fetch_plan(resolved, ticket),
        verdict=fetch_verdict_blocking(resolved, number),
        prior_run=fetch_prior_run(number, agent),
    )


def packet_main(argv: Optional[Sequence[str]] = None) -> int:
    """Print one implementation packet as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only implementation packet for a ticket"
    )
    parser.add_argument("ticket", type=int, help="ticket number")
    parser.add_argument("--repo", default=None)
    parser.add_argument("--agent", default="codex", choices=("codex", "claude"))
    args = parser.parse_args(argv)
    try:
        packet = collect(args.repo, args.ticket, agent=args.agent)
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("implement-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


def read_answer(path: str) -> dict:
    """Read and validate the happy-path model answer."""
    try:
        raw = sys.stdin.read() if path == "-" else pathlib.Path(path).read_text()
    except OSError as exc:
        raise ImplementError("cannot read answer {}: {}".format(path, exc))
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise ImplementError("answer is not valid JSON: {}".format(exc))
    if not isinstance(answer, dict):
        raise ImplementError("answer must be a JSON object")
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
    extra = sorted(set(answer) - {"done", "summary", "departures"})
    if extra:
        raise ImplementError("answer has unknown field(s): {}".format(", ".join(extra)))
    return {
        "done": True,
        "summary": summary.strip(),
        "departures": [value.strip() for value in departures],
    }


def _run(command: Sequence[str], *, cwd: pathlib.Path,
         env: Optional[dict] = None, input_text: Optional[str] = None,
         check: bool = True) -> subprocess.CompletedProcess:
    """Run one local command and turn failures into a concise engine error."""
    proc = subprocess.run(
        list(command), cwd=str(cwd), env=env, input=input_text,
        capture_output=True, text=True,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ImplementError(
            "{} failed{}".format(
                shlex.join(command), ": " + detail if detail else ""
            )
        )
    return proc


def checkout_context(cwd: Optional[os.PathLike] = None) -> dict:
    """Derive ticket identity from a real clone on ticket/<n>."""
    here = pathlib.Path(cwd or os.getcwd()).resolve()
    root = pathlib.Path(
        _run(["git", "rev-parse", "--show-toplevel"], cwd=here).stdout.strip()
    ).resolve()
    branch = _run(["git", "branch", "--show-current"], cwd=root).stdout.strip()
    match = re.fullmatch(r"ticket/([1-9][0-9]*)", branch)
    if match is None:
        raise ImplementError(
            "finish-ticket requires branch ticket/<number>, found {!r}".format(branch)
        )
    _run(["git", "remote", "get-url", "origin"], cwd=root)
    return {"root": root, "branch": branch, "number": int(match.group(1))}


def default_test_commands(root: pathlib.Path) -> List[List[str]]:
    """Choose the deterministic test command from repository evidence."""
    commands: List[List[str]] = []
    if (root / "funnel.py").is_file():
        commands.append([
            sys.executable, "-c",
            'from pathlib import Path; compile(Path("funnel.py").read_text(), '
            '"funnel.py", "exec")',
        ])
    python_markers = (
        root / "tests", root / "pytest.ini", root / "pyproject.toml",
        root / "setup.cfg", root / "tox.ini",
    )
    if any(path.exists() for path in python_markers):
        commands.append([sys.executable, "-m", "pytest", "-q"])
    elif (root / "package.json").is_file():
        commands.append(["npm", "test"])
    if not commands:
        raise ImplementError(
            "could not derive a test command from this checkout"
        )
    return commands


def run_tests(root: pathlib.Path,
              commands: Optional[Sequence[Sequence[str]]] = None) -> List[str]:
    """Run the checkout's tests without writing Python bytecode."""
    selected = list(commands) if commands is not None else default_test_commands(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    rendered = []
    for command in selected:
        argv = list(command)
        _run(argv, cwd=root, env=env)
        rendered.append(shlex.join(argv))
    return rendered


def resolve_checkout_repo(root: pathlib.Path, explicit: Optional[str]) -> str:
    """Resolve owner/name from an explicit value or the checkout's gh context."""
    if explicit:
        return explicit
    data = funnel._gh_json("gh", "repo", "view", "--json", "nameWithOwner")
    repo = (data or {}).get("nameWithOwner")
    if not isinstance(repo, str) or "/" not in repo:
        raise ImplementError("could not resolve the checkout's GitHub repository")
    return repo


def render_pr_body(ticket: dict, answer: dict, *, continued: bool,
                   tests: Sequence[str]) -> str:
    """Render the stable PR template from the model's structured answer."""
    parent = ticket.get("parent") or {}
    parent_number = parent.get("number")
    lines = []
    if isinstance(parent_number, int):
        lines.append("Part of #{}.".format(parent_number))
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
    return "\n".join(lines) + "\n"


def _remote_branch_exists(root: pathlib.Path, branch: str) -> bool:
    proc = _run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin",
         "refs/heads/{}".format(branch)],
        cwd=root,
        check=False,
    )
    if proc.returncode not in (0, 2):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ImplementError("could not inspect remote ticket branch: {}".format(detail))
    return proc.returncode == 0


def _commit_if_needed(root: pathlib.Path, number: int, summary: str) -> bool:
    dirty = bool(_run(["git", "status", "--porcelain"], cwd=root).stdout.strip())
    if dirty:
        _run(["git", "add", "-A"], cwd=root)
        subject = summary.splitlines()[0].strip()
        if len(subject) > 60:
            subject = subject[:57].rstrip() + "..."
        _run(
            ["git", "commit", "-m", "Finish #{}: {}".format(number, subject)],
            cwd=root,
        )
        return True
    ahead = _run(
        ["git", "rev-list", "--count", "origin/main..HEAD"], cwd=root
    ).stdout.strip()
    if not ahead.isdigit() or int(ahead) < 1:
        raise ImplementError("done answer produced no change to commit")
    return False


def create_or_update_pr(repo: str, context: dict, ticket: dict,
                        body: str) -> dict:
    """Create the ticket PR, or update the one already open for the branch."""
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--head", context["branch"], "--json", "number,url", "--limit", "10",
    )
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError("could not list the branch's open PR")
    title = "{} (#{})".format(ticket["title"], ticket["number"])
    if rows:
        pr = rows[0]
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


def release_claim(ref: str) -> None:
    """Release through funnel's one lock implementation."""
    items = funnel.load_items()
    result = funnel.cmd_release(items, datetime.now(timezone.utc), ref)
    if result != 0:
        raise ImplementError("could not release {}".format(ref))


def finish_heartbeat(agent: str, run: str, outcome: str,
                     note: str, work: str) -> None:
    """Finish the exact bound run through heartbeat's validation path."""
    import heartbeat

    result = heartbeat.main([
        "finish", "--agent", agent, "--run", run, "--outcome", outcome,
        "--note", note, "--work", work,
    ])
    if result != 0:
        raise ImplementError("heartbeat finish refused run {}".format(run))


def finish_done(answer: dict, *, run: str, agent: str = "codex",
                repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
                test_commands: Optional[Sequence[Sequence[str]]] = None,
                release: Callable[[str], None] = release_claim,
                heartbeat_finish: Callable[[str, str, str, str, str], None]
                = finish_heartbeat,
                pr_effect: Callable[[str, dict, dict, str], dict]
                = create_or_update_pr,
                extra_note: Optional[str] = None) -> dict:
    """Perform every happy-path effect and return the resulting PR identity."""
    context = checkout_context(cwd)
    resolved = resolve_checkout_repo(context["root"], repo)
    ticket = fetch_ticket(resolved, context["number"])
    ref = ticket["ref"]
    tests = run_tests(context["root"], test_commands)
    continued = _remote_branch_exists(context["root"], context["branch"])
    _commit_if_needed(context["root"], context["number"], answer["summary"])
    _run(
        ["git", "push", "--set-upstream", "origin", context["branch"]],
        cwd=context["root"],
    )
    body = render_pr_body(ticket, answer, continued=continued, tests=tests)
    pr = pr_effect(resolved, context, ticket, body)
    release(ref)
    note = "PR #{}".format(pr["number"])
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "done", note, ref)
    return pr


def finish_main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI for the happy-path finish-ticket effect sequence."""
    parser = argparse.ArgumentParser(
        description="verify, publish, release, and finish one implemented ticket"
    )
    parser.add_argument("--answer", required=True,
                        help="structured answer file, or - for stdin")
    parser.add_argument("--run", required=True, help="bound heartbeat run id")
    parser.add_argument("--agent", default="codex",
                        choices=("codex", "muse", "zcode", "claude"))
    parser.add_argument("--repo", default=None,
                        help="owner/name; normally derived from the checkout")
    parser.add_argument("--note", default=None,
                        help="extra run note, such as a stale-claim takeover")
    args = parser.parse_args(argv)
    try:
        answer = read_answer(args.answer)
        pr = finish_done(
            answer, run=args.run, agent=args.agent, repo=args.repo,
            extra_note=args.note,
        )
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(pr, sort_keys=True))
    return 0

