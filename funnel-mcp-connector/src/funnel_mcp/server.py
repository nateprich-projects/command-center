"""Authenticated remote MCP tools for the Command Center funnel."""

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
    """Return exactly the stdout from one read-only funnel.py command."""
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
    """Build the authenticated MCP server and its read-only funnel tools."""
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
            "Read-only Command Center tools that return the output of the matching "
            "funnel.py command. funnel.py remains the source of truth for project reads "
            "and ordering."
        ),
        auth=token_verifier,
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
