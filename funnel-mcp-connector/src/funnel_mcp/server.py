"""Remote MCP server skeleton. Tool implementations are intentionally separate tickets."""

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
    """Find the checkout that contains the authoritative funnel commands."""
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
    """Return stdout from one command handled by the canonical funnel.py."""
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
    """Build the authenticated MCP server and its write tools."""
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
            "and shape commands. The capture and shaped writes record nate-relayed "
            "provenance; repo resolution refuses ambiguity."
        ),
        auth=auth,
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
