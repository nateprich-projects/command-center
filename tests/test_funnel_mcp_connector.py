"""The general-chat MCP surface is limited to its read and gate adapters."""

from __future__ import annotations

import importlib
import pathlib
import sys
import types
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONNECTOR_SRC = ROOT / "funnel-mcp-connector" / "src"
sys.path.insert(0, str(CONNECTOR_SRC))


class FakeFastMCP:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.tools = {}
        self.routes = {}

    def tool(self, function=None, **_kwargs):
        def register(candidate):
            self.tools[candidate.__name__] = candidate
            return candidate

        return register(function) if function is not None else register

    def custom_route(self, path, methods):
        def register(function):
            self.routes[(path, tuple(methods))] = function
            return function

        return register


class FakeStaticTokenVerifier:
    def __init__(self, **kwargs):
        self.options = kwargs


class FakeRemoteAuthProvider:
    def __init__(self, **kwargs):
        self.options = kwargs


def _module(name, *, package=False, **attributes):
    value = types.ModuleType(name)
    if package:
        value.__path__ = []
    for key, attribute in attributes.items():
        setattr(value, key, attribute)
    return value


@pytest.fixture
def connector_server(monkeypatch):
    fake_modules = {
        "fastmcp": _module("fastmcp", package=True, FastMCP=FakeFastMCP),
        "fastmcp.server": _module("fastmcp.server", package=True),
        "fastmcp.server.auth": _module(
            "fastmcp.server.auth",
            package=True,
            RemoteAuthProvider=FakeRemoteAuthProvider,
            StaticTokenVerifier=FakeStaticTokenVerifier,
        ),
        "starlette": _module("starlette", package=True),
        "starlette.requests": _module("starlette.requests", Request=object),
        "starlette.responses": _module(
            "starlette.responses", JSONResponse=object
        ),
    }
    for name, value in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, value)
    prior_server = sys.modules.pop("funnel_mcp.server", None)
    loaded_server = importlib.import_module("funnel_mcp.server")
    yield loaded_server
    sys.modules.pop("funnel_mcp.server", None)
    if prior_server is not None:
        sys.modules["funnel_mcp.server"] = prior_server


def test_server_registers_only_the_read_and_gate_tools(
    connector_server,
):
    server = connector_server.build_server(
        connector_server.Config(
            host="127.0.0.1", port=3003, inbound_static_token="x" * 32
        )
    )

    assert set(server.tools) == {
        "brief", "ideas", "show", "queue",
        "approve", "start", "accept", "park",
    }
    assert not set(server.tools) & {
        "next", "claim", "release", "gate", "heartbeat", "merge",
    }


def test_tools_forward_verbatim_instruction_and_supported_arguments(
    connector_server, monkeypatch
):
    server = connector_server.build_server(
        connector_server.Config(
            host="127.0.0.1", port=3003, inbound_static_token="x" * 32
        )
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="gate updated\n", stderr="")

    monkeypatch.setattr(connector_server.subprocess, "run", run)
    instruction = "  Accept the finished work.\nKeep this wording.  "

    assert server.tools["approve"]("owner/repo#1", instruction) == "gate updated\n"
    assert server.tools["start"]("owner/repo#2", instruction) == "gate updated\n"
    assert server.tools["accept"](
        "owner/repo#3", instruction, no_tickets=True
    ) == "gate updated\n"
    assert server.tools["park"](
        "owner/repo#4", "Pause this work", instruction
    ) == "gate updated\n"

    commands = [call[0][2:] for call in calls]
    assert commands == [
        ["approve", "owner/repo#1", "--yes", "--instruction", instruction],
        ["start", "owner/repo#2", "--yes", "--instruction", instruction],
        [
            "accept", "owner/repo#3", "--yes", "--instruction", instruction,
            "--no-tickets",
        ],
        [
            "park", "owner/repo#4", "--reason", "Pause this work",
            "--instruction", instruction,
        ],
    ]
    assert all(
        call[0][:2] == [connector_server.sys.executable, str(ROOT / "funnel.py")]
        for call in calls
    )
    assert all(call[1]["cwd"] == ROOT for call in calls)
    assert all("shell" not in call[1] for call in calls)


def test_tools_reject_blank_instructions_before_running_funnel(
    connector_server, monkeypatch
):
    server = connector_server.build_server(
        connector_server.Config(
            host="127.0.0.1", port=3003, inbound_static_token="x" * 32
        )
    )
    monkeypatch.setattr(
        connector_server.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("blank instruction reached funnel.py"),
    )

    with pytest.raises(ValueError, match="instruction"):
        server.tools["approve"]("owner/repo#1", "  \n")
