"""Authenticated remote MCP tools for the Command Center funnel."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.auth import RemoteAuthProvider, StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Config, ConfigError, load_config

log = logging.getLogger(__name__)


def _required_text(value: str, name: str) -> str:
    """Reject empty tool input without changing the caller's text."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


_ISSUE_REF = re.compile(
    r"\A(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#)?#?"
    r"(?P<number>[1-9][0-9]*)\Z"
)
_ISSUE_URL = re.compile(
    r"\Ahttps?://github\.com/(?P<repo>[A-Za-z0-9_.-]+/"
    r"[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)/?\Z",
    re.IGNORECASE,
)


def _command_center_root() -> Path:
    """Find the checkout that contains the authoritative funnel.py command."""
    working_root = Path.cwd().resolve()
    if (working_root / "funnel.py").is_file():
        return working_root

    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "funnel.py").is_file():
        return source_root

    raise RuntimeError(
        "could not find funnel.py; start the MCP server from the "
        "command-center repository"
    )


def _run_funnel(*arguments: str) -> str:
    """Return exactly the stdout from one funnel.py command."""
    root = _command_center_root()
    command = [sys.executable, str(root / "funnel.py"), *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("could not start funnel.py: {}".format(exc)) from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if not detail:
            detail = "no error output"
        raise RuntimeError(
            "funnel.py {} failed with exit status {}: {}".format(
                arguments[0], completed.returncode, detail
            )
        )
    return completed.stdout


def _run_shape_apply(answer: dict, *arguments: str) -> str:
    """Send a structured answer to the shared shape engine through stdin."""
    root = _command_center_root()
    command = [sys.executable, str(root / "shape-apply"), *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            input=json.dumps(answer),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("could not start shape-apply: {}".format(exc)) from exc

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if not detail:
            detail = "no error output"
        raise RuntimeError(
            "shape-apply failed with exit status {}: {}".format(
                completed.returncode, detail
            )
        )
    return completed.stdout


def _shape_target(
    ref: str, repo: Optional[str] = None
) -> tuple[str, Optional[str]]:
    """Split an issue reference while refusing conflicting repo choices."""
    value = ref.strip() if isinstance(ref, str) else ""
    match = _ISSUE_URL.fullmatch(value) or _ISSUE_REF.fullmatch(value)
    if match is None:
        raise ValueError(
            "ref must be an issue number, owner/repo#number, or GitHub issue URL"
        )

    explicit_repo = repo.strip() if isinstance(repo, str) else None
    if repo is not None and not explicit_repo:
        raise ValueError("repo must be a non-empty owner/name")
    ref_repo = match.group("repo")
    if explicit_repo and ref_repo and explicit_repo != ref_repo:
        raise ValueError(
            "repo conflicts with the repository in ref; use one target repo"
        )
    return match.group("number"), explicit_repo or ref_repo


def build_server(config: Config) -> FastMCP:
    """Build the authenticated MCP server with read, gate, and write tools."""
    token_verifier = StaticTokenVerifier(
        tokens={
            config.inbound_static_token: {
                "client_id": "command-center",
                "scopes": [],
            }
        }
    )
    auth = RemoteAuthProvider(
        token_verifier=token_verifier,
        authorization_servers=[],
        base_url=config.public_url,
    )
    server = FastMCP(
        name="command-center",
        instructions=(
            "Authenticated Command Center tools call the repository's canonical funnel "
            "and shape commands. Read tools preserve funnel.py as the source of truth "
            "for queries and ordering. Capture, shaped, and gate writes record "
            "nate-relayed provenance; repo resolution refuses ambiguity."
        ),
        auth=auth,
    )

    @server.tool
    def brief() -> str:
        """Return the current Command Center brief from `funnel.py brief`."""
        return _run_funnel("brief")

    @server.tool
    def ideas() -> str:
        """Return captured ideas from `funnel.py ideas`."""
        return _run_funnel("ideas")

    @server.tool
    def show(ref: str) -> str:
        """Return an item's details from `funnel.py show <ref>`."""
        return _run_funnel("show", ref)

    @server.tool
    def queue() -> str:
        """Return the ordered Command Center queue from `funnel.py queue`."""
        return _run_funnel("queue")

    @server.tool
    def approve(ref: str, instruction: str) -> str:
        """Approve a Shaped project after Nate's verbatim instruction is supplied."""
        instruction = _required_text(instruction, "instruction")
        return _run_funnel(
            "approve", _required_text(ref, "ref"), "--yes",
            "--instruction", instruction,
        )

    @server.tool
    def accept(ref: str, instruction: str, no_tickets: bool = False) -> str:
        """Accept a completed project using Nate's verbatim instruction."""
        instruction = _required_text(instruction, "instruction")
        args = [
            "accept", _required_text(ref, "ref"), "--yes",
            "--instruction", instruction,
        ]
        if no_tickets:
            args.append("--no-tickets")
        return _run_funnel(*args)

    @server.tool
    def park(ref: str, reason: str, instruction: str) -> str:
        """Park a project with its required reason and verbatim instruction."""
        return _run_funnel(
            "park", _required_text(ref, "ref"),
            "--reason", _required_text(reason, "reason"),
            "--instruction", _required_text(instruction, "instruction"),
        )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "command-center-mcp"})

    @server.tool
    def capture(title: str, note: Optional[str] = None,
                repo: Optional[str] = None) -> str:
        """Capture a Nate-raised idea, with optional note and target repo.

        If repo is omitted, funnel.py uses the bound work repo, the sole member
        repo, or refuses when more than one member repo is possible.
        """
        arguments = [
            "capture", title,
            "--origin", "nate-relayed",
            "--voice", "nate-relayed",
        ]
        if note is not None:
            arguments.extend(("--note", note))
        if repo is not None:
            arguments.extend(("--repo", repo))
        return _run_funnel(*arguments)

    @server.tool
    def shaped(
        ref: str,
        plan_markdown: str,
        decided_from_precedent: list[dict[str, str]],
        decided_by_agent: list[dict[str, str]],
        needs_nate: dict[str, list[str] | None],
        proposed_class: str,
        escalated_risk: list[dict[str, str]],
        depends_on: list[str],
        premises: list[dict[str, str]],
        repo: Optional[str] = None,
    ) -> str:
        """Record a validated plan using the canonical shape engine.

        Supply all shape answer fields, including plan_markdown as text. Use
        empty lists and null Needs Nate categories when there is no content.
        Each needs_nate value is null or a list of questions; its keys are
        exposure, gates, scope, and preference. repo is optional: an embedded
        repo in ref wins unless repo disagrees, otherwise the shape engine uses
        the bound work repo, sole member repo, or refuses ambiguity. The issue
        body receives nate-relayed provenance.
        """
        number, target_repo = _shape_target(ref, repo)
        answer = {
            "plan_markdown": plan_markdown,
            "decided_from_precedent": decided_from_precedent,
            "decided_by_agent": decided_by_agent,
            "needs_nate": needs_nate,
            "proposed_class": proposed_class,
            "escalated_risk": escalated_risk,
            "depends_on": depends_on,
            "premises": premises,
        }
        arguments = [number, "--answer", "-", "--voice", "nate-relayed"]
        if target_repo is not None:
            arguments.extend(("--repo", target_repo))
        return _run_shape_apply(answer, *arguments)

    return server


def build_app(config: Config):
    """Return the ASGI app served by Uvicorn."""
    return build_server(config).http_app(path="/mcp")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    log.info("starting MCP server on %s:%s", config.host, config.port)
    import uvicorn

    uvicorn.run(build_app(config), host=config.host, port=config.port, access_log=False)


if __name__ == "__main__":
    main()
