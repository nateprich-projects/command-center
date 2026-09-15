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
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402


class ImplementError(RuntimeError):
    """A packet or finish contract could not be completed safely."""


class StrayFileError(ImplementError):
    """The ticket branch contains run scratch that must not reach a PR."""


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
    extra = sorted(set(answer) - {"done", "summary", "departures"})
    if extra:
        raise ImplementError("answer has unknown field(s): {}".format(", ".join(extra)))
    return {
        "done": True,
        "summary": summary.strip(),
        "departures": [value.strip() for value in departures],
    }


def _read_blocked_answer(value: object) -> dict:
    """Validate the blocked_on_human answer against the allowlisted reasons."""
    if not isinstance(value, dict):
        raise ImplementError("blocked_on_human must be an object")
    reason = value.get("reason")
    if reason not in funnel.HUMAN_STEP_REASONS:
        raise ImplementError(
            "blocked_on_human reason must be one of: {}".format(
                ", ".join(funnel.HUMAN_STEP_REASONS)))
    action = value.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ImplementError("blocked_on_human action must be a non-empty string")
    extra = sorted(set(value) - {"reason", "action"})
    if extra:
        raise ImplementError(
            "blocked_on_human has unknown field(s): {}".format(
                ", ".join(extra)))
    return {"reason": reason, "action": action.strip()}


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
    """
    if _run_invokes_pytest(run):
        return True
    lowered = name.strip().lower()
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


def run_tests(root: pathlib.Path,
              commands: Optional[Sequence[Sequence[str]]] = None
              ) -> Tuple[List[str], Optional[str]]:
    """Run the checkout's tests without writing Python bytecode.

    Returns the rendered commands and the test-command source, which is
    None when the caller injected explicit commands or no test slot was
    resolved.
    """
    if commands is not None:
        selected = list(commands)
        source: Optional[str] = None
    else:
        selected, source = default_test_plan(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
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
        # Mac has no bare `python` on PATH (#890), so run this interpreter.
        if argv and argv[0] in ("python", "python3"):
            argv[0] = sys.executable
        _run(argv, cwd=root, env=env)
        rendered.append(shlex.join(argv))
    return rendered, source


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
                   tests: Sequence[str],
                   test_source: Optional[str] = None) -> str:
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
    if test_source is not None:
        lines.extend([
            "",
            "Test command source:",
            test_source,
        ])
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
    raw = _run(["git", *command], cwd=root).stdout
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


def _stage_explicit_paths(root: pathlib.Path, paths: Sequence[str]) -> None:
    """Stage exactly the observed paths, never a blanket add or status sweep."""
    selected = sorted(set(paths))
    if selected:
        _run(["git", "add", "--", *selected], cwd=root)


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
        raise StrayFileError(
            "pre-PR stray-file check refused; remove and re-stage run scratch: "
            "{}".format(", ".join(stray)))


def _commit_if_needed(root: pathlib.Path, number: int, summary: str) -> bool:
    """Commit explicit paths after the pre-PR scratch check."""
    paths = _working_tree_paths(root)
    if paths:
        _stage_explicit_paths(root, paths)
        _check_no_run_scratch(root, about_to_commit=paths)
        subject = summary.splitlines()[0].strip()
        if len(subject) > 60:
            subject = subject[:57].rstrip() + "..."
        _run(
            ["git", "commit", "-m", "Finish #{}: {}".format(number, subject)],
            cwd=root,
        )
        return True
    _check_no_run_scratch(root)
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


def render_human_step_title(action: str) -> str:
    """Render the human-step sub-issue title from the model's action."""
    return "Human step: {}".format(action)


def render_human_step_body(*, parent_number: int, ticket_number: int,
                           reason: str, action: str) -> str:
    """Render the human-step sub-issue body with its exact marker line.

    The ``Human step: <reason>`` line must match ``HUMAN_STEP_LINE``
    exactly, because the body scanner reads that marker until the Needs
    field covers every open ticket.
    """
    doing = action.rstrip()
    if not doing.endswith("."):
        doing += "."
    return "\n".join([
        "Part of #{}; discovered while implementing #{}.".format(
            parent_number, ticket_number),
        "",
        "{}{}".format(funnel.HUMAN_STEP_PREFIX, reason),
        "",
        "Action Nate must perform: {}".format(doing),
        "",
        "Risk: standard",
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


def _failure_note(exc: ImplementError, kept: str = "") -> str:
    """Summarise a test failure so the next run knows what failed (#877).

    Names up to five FAILED or ERROR test ids and pytest's counts line, rather
    than the first line of output, which is only the progress dots.
    """
    text = str(exc)
    ids = []
    for match in re.finditer(r"^(?:FAILED|ERROR) (\S+)", text, re.M):
        if match.group(1) not in ids:
            ids.append(match.group(1))
    counts = re.findall(r"^=* ?(\d+ (?:failed|passed|error)[^=\n]*?) ?=*$", text, re.M)
    parts = []
    if ids:
        more = " (+{} more)".format(len(ids) - 5) if len(ids) > 5 else ""
        parts.append("; ".join(ids[:5]) + more)
    if counts:
        parts.append(counts[-1].strip())
    if not parts:
        first = text.splitlines()[0] if text else "unknown test failure"
        parts.append(first[:197].rstrip() + "..." if len(first) > 200 else first)
    note = "tests failed: " + " | ".join(parts)
    if kept:
        note += " | " + kept
    return note


def _push_ticket_branch(root: pathlib.Path, branch: str) -> None:
    """Push the ticket branch as a fast-forward, even after a rebase (#890).

    A stale-PR verdict asks the engineer to rebase onto main, which rewrites
    the branch the remote already holds, so a plain push is rejected as
    non-fast-forward and the ticket wedges. When the remote branch exists and
    is not an ancestor of HEAD, record it with an ``ours`` merge: the tree is
    exactly this run's, the old commits stay in the history, and the push is a
    fast-forward. Never a force-push; main squash-merges, so the extra merge
    commit never reaches it.
    """
    fetched = _run(["git", "fetch", "origin",
                    "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch)],
                   cwd=root, check=False)
    if fetched.returncode == 0:
        remote_ref = "origin/{}".format(branch)
        ancestor = _run(["git", "merge-base", "--is-ancestor", remote_ref, "HEAD"],
                        cwd=root, check=False)
        if ancestor.returncode != 0:
            _run(["git", "merge", "-s", "ours", "--no-edit", "-m",
                  "Record the previous {} tip before pushing the rebased branch".format(branch),
                  remote_ref], cwd=root)
    _run(["git", "push", "--set-upstream", "origin", branch], cwd=root)


def _keep_work(root: pathlib.Path, number: int, branch: str, *,
               reason: str = "tests failing") -> str:
    """Commit and push explicit paths, so a failure loses time only.

    Never opens a PR. Returns a short phrase for the heartbeat note. The same
    pre-PR scratch check applies here so a WIP branch cannot carry run debris.
    """
    try:
        paths = _working_tree_paths(root)
        if paths:
            _stage_explicit_paths(root, paths)
            _check_no_run_scratch(root, about_to_commit=paths)
            _run(["git", "commit", "-m",
                  "WIP #{}: {}".format(number, reason)], cwd=root)
        else:
            _check_no_run_scratch(root)
        ahead = _run(["git", "rev-list", "--count", "origin/main..HEAD"],
                     cwd=root).stdout.strip()
        if not ahead.isdigit() or int(ahead) < 1:
            return "no work to keep"
        _push_ticket_branch(root, branch)
        return "work kept on {}".format(branch)
    except ImplementError as exc:
        first = str(exc).splitlines()[0] if str(exc) else "unknown error"
        return "work NOT kept: {}".format(first[:120])


def _recover_answer_error(
        exc: ImplementError, *, run: str, agent: str = "codex",
        repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
        release: Callable[[str], None] = release_claim,
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
    kept = _keep_work(
        context["root"], context["number"], context["branch"],
        reason="answer unreadable",
    )
    release(ref)
    first = str(exc).splitlines()[0] if str(exc) else "unknown answer error"
    note = "answer error: {} | {}".format(first[:200], kept)
    heartbeat_finish(agent, run, "errored", note, ref)
    return True


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
    try:
        tests, test_source = run_tests(context["root"], test_commands)
    except ImplementError as exc:
        # A failing checkout must not strand the run or lose its work (#877):
        # commit and push the ticket branch without opening a PR, release the
        # claim, and finish errored naming what failed, so the next run
        # continues the branch instead of starting again from main.
        kept = _keep_work(context["root"], context["number"], context["branch"])
        release(ref)
        heartbeat_finish(agent, run, "errored", _failure_note(exc, kept), ref)
        raise
    continued = _remote_branch_exists(context["root"], context["branch"])
    try:
        _commit_if_needed(context["root"], context["number"], answer["summary"])
        _push_ticket_branch(context["root"], context["branch"])
        # Re-read immediately before the PR effect so the guard remains the
        # last local file-list check, even when an existing branch was merged.
        _check_no_run_scratch(context["root"])
    except StrayFileError as exc:
        release(ref)
        heartbeat_finish(agent, run, "errored", str(exc), ref)
        raise
    body = render_pr_body(ticket, answer, continued=continued, tests=tests,
                         test_source=test_source)
    pr = pr_effect(resolved, context, ticket, body)
    release(ref)
    note = "PR #{}".format(pr["number"])
    if test_source is not None:
        note += " (tests: {})".format(test_source)
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "done", note, ref)
    return pr


def finish_blocked_on_human(
        blocked: dict, *, run: str, agent: str = "codex",
        repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
        release: Callable[[str], None] = release_claim,
        heartbeat_finish: Callable[[str, str, str, str, str], None]
        = finish_heartbeat,
        create_effect: Callable[..., dict] = create_human_step_issue,
        block_effect: Callable[..., None] = mark_ticket_blocked,
        comment_effect: Callable[..., None] = post_agent_comment,
        extra_note: Optional[str] = None) -> dict:
    """File the human step, block the ticket, release, and finish. No PR.

    No test run, commit, or push happens here: the checkout holds an
    unfinished implementation and must stay exactly as the model left it.
    A failure after the sub-issue exists names it, so the retry starts
    from GitHub's truth rather than filing a second one.
    """
    context = checkout_context(cwd)
    resolved = resolve_checkout_repo(context["root"], repo)
    ticket = fetch_ticket(resolved, context["number"])
    ref = ticket["ref"]
    parent = ticket.get("parent") or {}
    parent_number = parent.get("number")
    if not isinstance(parent_number, int):
        raise ImplementError(
            "blocked_on_human needs the ticket's parent to file the "
            "human step under")
    title = render_human_step_title(blocked["action"])
    body = render_human_step_body(
        parent_number=parent_number, ticket_number=context["number"],
        reason=blocked["reason"], action=blocked["action"])
    created = create_effect(
        resolved, parent_number, title, body, cwd=context["root"])
    try:
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
    release(ref)
    note = "stopped: human step filed as #{}; ticket blocked; no PR opened".format(
        created["number"])
    if extra_note:
        note += "; " + extra_note.strip()
    heartbeat_finish(agent, run, "skipped-human-step", note, ref)
    return {"ticket": ref,
            "human_step": {"number": created["number"],
                           "ref": created["ref"],
                           "url": created["url"]}}


def finish_declined(
        reason: str, *, run: str, agent: str = "codex",
        repo: Optional[str] = None, cwd: Optional[os.PathLike] = None,
        release: Callable[[str], None] = release_claim,
        heartbeat_finish: Callable[[str, str, str, str, str], None]
        = finish_heartbeat,
        block_effect: Callable[..., None] = mark_ticket_blocked,
        comment_effect: Callable[..., None] = post_agent_comment,
        extra_note: Optional[str] = None) -> dict:
    """Label the declined ticket blocked, record why, release, finish. No PR."""
    context = checkout_context(cwd)
    resolved = resolve_checkout_repo(context["root"], repo)
    ticket = fetch_ticket(resolved, context["number"])
    ref = ticket["ref"]
    block_effect(resolved, context["number"], cwd=context["root"])
    comment_effect(resolved, context["number"],
                   "**Declined:** {}".format(reason),
                   run=run, agent=agent, cwd=context["root"])
    release(ref)
    first = reason.splitlines()[0] if reason else "no reason given"
    if len(first) > 200:
        first = first[:197].rstrip() + "..."
    note = "declined: {}".format(first)
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
                release=release_claim, heartbeat_finish=finish_heartbeat,
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
                extra_note=args.note,
            )
        elif "blocked_on_human" in answer:
            result = finish_blocked_on_human(
                answer["blocked_on_human"], run=args.run, agent=args.agent,
                repo=args.repo, extra_note=args.note,
            )
        else:
            result = finish_declined(
                answer["declined"], run=args.run, agent=args.agent,
                repo=args.repo, extra_note=args.note,
            )
    except (funnel.GitHubError, ImplementError, OSError,
            subprocess.SubprocessError) as exc:
        print("finish-ticket: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
