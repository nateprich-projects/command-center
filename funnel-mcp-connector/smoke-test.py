#!/usr/bin/env python3
"""Exercise the running MCP endpoint, probing caller auth before MCP features.

  ./smoke-test.py http://127.0.0.1:3003

The token is read from INBOUND_STATIC_TOKEN or --token. After the auth checks, the
authenticated MCP session verifies exactly the four read tools and the three gate
tools are registered.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx
from fastmcp import Client


async def check_auth_boundary(base: str) -> list[str]:
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for label, headers in (
            ("no token", {}),
            ("bad token", {"Authorization": "Bearer definitely-not-valid"}),
        ):
            try:
                response = await client.post(
                    f"{base}/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream", **headers},
                )
            except httpx.HTTPError as exc:
                failures.append(f"{label}: could not connect ({exc})")
                continue
            if response.status_code in (401, 403):
                print(f"  PASS  {label} -> {response.status_code}")
            else:
                failures.append(f"{label}: expected 401/403, got {response.status_code}")
    return failures


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", help="e.g. http://127.0.0.1:3003")
    parser.add_argument(
        "--token",
        default=os.environ.get("INBOUND_STATIC_TOKEN"),
        help="INBOUND_STATIC_TOKEN for the authenticated MCP session",
    )
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print("auth boundary:")
    failures = await check_auth_boundary(base)

    if not args.token:
        failures.append("INBOUND_STATIC_TOKEN or --token is required for the authenticated MCP session")

    if args.token:
        print("authenticated MCP session:")
        try:
            async with Client(f"{base}/mcp", auth=args.token) as client:
                tools = await client.list_tools()
                expected = {
                    "brief", "ideas", "show", "queue",
                    "approve", "accept", "park",
                }
                actual = {tool.name for tool in tools}
                if actual != expected:
                    failures.append(
                        "expected tools {}, got {}".format(
                            sorted(expected), sorted(actual)
                        )
                    )
                else:
                    print("  PASS  initialize and tools/list completed; read and gate tools are registered")
        except Exception as exc:  # noqa: BLE001 - report protocol and auth failures
            failures.append(f"authenticated MCP session: {type(exc).__name__}: {exc}")

    print("health:")
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(f"{base}/healthz")
            if response.status_code == 200 and response.json() == {
                "status": "ok",
                "service": "command-center-mcp",
            }:
                print("  PASS  /healthz -> 200")
            else:
                failures.append(f"healthz: expected service health JSON, got {response.status_code} {response.text}")
        except httpx.HTTPError as exc:
            failures.append(f"healthz: {exc}")

    print()
    if failures:
        print("FAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
