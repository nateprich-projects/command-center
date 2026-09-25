"""The remote connector exposes the planned write tools, not local controls."""

from __future__ import annotations

import ast
import pathlib


ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVER = ROOT / "funnel-mcp-connector/src/funnel_mcp/server.py"


def test_write_tools_are_registered_without_local_only_controls():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    server = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_server"
    )
    tools = {
        node.name
        for node in ast.walk(server)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Attribute)
            and decorator.attr == "tool"
            and isinstance(decorator.value, ast.Name)
            and decorator.value.id == "server"
            for decorator in node.decorator_list
        )
    }

    assert {"capture", "shaped"} <= tools
    assert not ({"next", "claim", "release", "gate", "heartbeat"} & tools)
