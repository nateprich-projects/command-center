"""Authenticated MCP server for the Command Center funnel."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Config, ConfigError, load_config

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[3]
FUNNEL_PATH = REPO_ROOT / "funnel.py"


def _required_text(value: str, name: str) -> str:
    """Reject empty tool input without changing the caller's text."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _run_funnel(*args: str) -> str:
    """Run one established funnel command without a shell or parallel logic."""
    try:
        result = subprocess.run(
            [sys.executable, str(FUNNEL_PATH), *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"could not launch funnel.py: {exc}") from exc

    output = (result.stdout or "").strip()
    if result.returncode != 0:
        detail = (result.stderr or output or "no command output").strip()
        raise RuntimeError(
            f"funnel {args[0]} failed with exit code {result.returncode}: {detail}"
        )
    return output or f"funnel {args[0]} completed"


def build_server(config: Config) -> FastMCP:
    """Build the MCP server and its four user-directed gate tools."""
    token_verifier = StaticTokenVerifier(
        tokens={
            config.inbound_static_token: {
                "client_id": "command-center",
                "scopes": [],
            }
        }
    )
    server = FastMCP(
        name="command-center",
        instructions=(
            "Command Center MCP server. The four gate tools record the verbatim "
            "instruction supplied by Nate with nate-relayed provenance."
        ),
        auth=token_verifier,
    )

    @server.tool()
    def approve(ref: str, instruction: str) -> str:
        """Approve a Shaped project after Nate's verbatim instruction is supplied."""
        instruction = _required_text(instruction, "instruction")
        return _run_funnel(
            "approve", _required_text(ref, "ref"), "--yes",
            "--instruction", instruction,
        )

    @server.tool()
    def start(ref: str, instruction: str) -> str:
        """Start a Ready project after Nate's verbatim instruction is supplied."""
        instruction = _required_text(instruction, "instruction")
        return _run_funnel(
            "start", _required_text(ref, "ref"), "--yes",
            "--instruction", instruction,
        )

    @server.tool()
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

    @server.tool()
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
