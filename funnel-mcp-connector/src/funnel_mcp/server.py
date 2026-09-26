"""Authenticated remote MCP tools for the Command Center funnel."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

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


def _command_center_root() -> Path:
    """Find the checkout that contains the authoritative funnel.py command."""
    working_root = Path.cwd().resolve()
    if (working_root / "funnel.py").is_file():
        return working_root

    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "funnel.py").is_file():
        return source_root

    raise RuntimeError(
        "could not find funnel.py; start the MCP server from the command-center repository"
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


def build_server(config: Config) -> FastMCP:
    """Build the authenticated MCP server with its read and gate tools."""
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
            "Command Center tools that return the output of the matching funnel.py "
            "command. funnel.py remains the source of truth for project reads and "
            "ordering. The gate tools approve, start, accept and park record the "
            "verbatim instruction supplied by Nate with nate-relayed provenance."
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
    def start(ref: str, instruction: str) -> str:
        """Start a Ready project after Nate's verbatim instruction is supplied."""
        instruction = _required_text(instruction, "instruction")
        return _run_funnel(
            "start", _required_text(ref, "ref"), "--yes",
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
