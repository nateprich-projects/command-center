"""A Muse run reuses one disposable in-memory funnel load."""

from __future__ import annotations

import io
import json
import pathlib
import socket
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def test_a_session_defers_its_load_until_the_first_command_and_reuses_it(
    monkeypatch,
):
    loaded = []
    items = object()
    calls = []

    monkeypatch.setattr(
        funnel,
        "main",
        lambda argv, *, _items=None, _items_loader=None, _reset_api_usage=True:
            calls.append((argv, _items_loader(), _reset_api_usage)) or 0,
    )
    monkeypatch.setattr(funnel, "report_api_cost", lambda: None)
    monkeypatch.setattr(funnel, "report_graphql_spend", lambda: None)

    session = funnel.FunnelSession(
        loader=lambda: loaded.append("load") or items,
    )

    assert session.items is None
    assert session.dispatch(["begin", "--agent", "muse"])[0] == 0
    assert session.dispatch(["brief"])[0] == 0

    assert loaded == ["load"]
    assert calls == [
        (["begin", "--agent", "muse"], items, False),
        (["brief"], items, False),
    ]


def test_main_accepts_the_session_view_without_loading_the_project_again(
    monkeypatch,
):
    items = []
    received = []

    monkeypatch.setattr(
        funnel,
        "load_items",
        lambda: pytest.fail("a session command must not reload the Project"),
    )
    monkeypatch.setattr(
        funnel,
        "cmd_next_review",
        lambda current, tier: received.append((current, tier)) or 0,
    )

    assert funnel.main(["next-review", "--tier", "standard"], _items=items) == 0
    assert received == [(items, "standard")]


def test_a_session_passes_one_loaded_view_to_normal_commands():
    loaded = []
    session = funnel.FunnelSession(
        loader=lambda: loaded.append("load") or [],
    )

    assert session.dispatch(["next-review", "--tier", "standard"])[0] == 1
    assert session.dispatch(["next-review", "--tier", "standard"])[0] == 1
    assert loaded == ["load"]


def test_a_session_reset_keeps_the_first_load_in_the_command_measurement(
    monkeypatch,
):
    items = []
    reset_calls = []
    main_calls = []

    monkeypatch.setattr(
        funnel,
        "reset_api_usage",
        lambda: reset_calls.append("reset"),
    )
    monkeypatch.setattr(
        funnel,
        "main",
        lambda argv, *, _items=None, _items_loader=None, _reset_api_usage=True:
            main_calls.append(
                (argv, _items if _items is not None else _items_loader(),
                 _reset_api_usage)
            ) or 0,
    )
    monkeypatch.setattr(funnel, "report_api_cost", lambda: None)
    monkeypatch.setattr(funnel, "report_graphql_spend", lambda: None)

    session = funnel.FunnelSession(loader=lambda: items)
    session.dispatch(["begin"])
    session.dispatch(["brief"])

    assert reset_calls == ["reset", "reset"]
    assert main_calls == [
        (["begin"], items, False),
        (["brief"], items, False),
    ]


def test_the_first_command_keeps_the_lazy_load_cost(monkeypatch):
    observed = []

    def loader():
        funnel._API_USAGE["graphql_calls"] = 1
        funnel._GRAPHQL_COST_READS = 1
        funnel._GRAPHQL_SPEND.update({
            "calls": 1,
            "cost": 12,
            "remaining": 4_988,
            "reset_at": None,
        })
        return []

    def fake_main(argv, *, _items=None, _items_loader=None, _reset_api_usage=True):
        if _items is None:
            _items = _items_loader()
        observed.append(funnel.api_cost())
        return 0

    monkeypatch.setattr(funnel, "main", fake_main)
    monkeypatch.setattr(funnel, "report_api_cost", lambda: None)
    monkeypatch.setattr(funnel, "report_graphql_spend", lambda: None)

    funnel.FunnelSession(loader=loader).dispatch(["begin"])

    assert observed == [{"graphql_points": 12, "gh_calls": 1}]
    funnel.reset_api_usage()


def test_the_muse_runner_starts_a_session_and_stops_it_with_the_run():
    runner = (ROOT / "scripts" / "muse-review").read_text()
    assert "session-server" in runner
    assert "FUNNEL_SESSION" in runner
    assert "session-stop" in runner
    assert "--parent-pid \"$$\"" in runner


def test_session_client_reports_a_connect_timeout_as_session_unreachable(
    monkeypatch, capsys
):
    monkeypatch.setenv(funnel.SESSION_ENV, "127.0.0.1:1234:token")

    def connect_timeout(*args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(funnel.socket, "create_connection", connect_timeout)

    assert funnel._session_client(["brief"]) == 2
    assert capsys.readouterr().err == (
        "funnel: connect-timeout: FUNNEL_SESSION session unreachable within "
        "30s: timed out\n"
    )


def test_session_client_reports_a_reply_timeout_as_a_busy_session(
    monkeypatch, capsys
):
    monkeypatch.setenv(funnel.SESSION_ENV, "127.0.0.1:1234:token")

    class TimeoutStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, payload):
            return len(payload)

        def flush(self):
            pass

        def readline(self, limit):
            raise socket.timeout("timed out")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def makefile(self, mode):
            return TimeoutStream()

    monkeypatch.setattr(
        funnel.socket, "create_connection", lambda *args, **kwargs: Connection()
    )

    assert funnel._session_client(["brief"]) == 2
    assert capsys.readouterr().err == (
        "funnel: reply-timeout: FUNNEL_SESSION session busy past the 30s "
        "reply budget (slow section unknown): timed out\n"
    )


def test_session_client_forwards_piped_stdin_in_the_request(monkeypatch):
    monkeypatch.setenv(funnel.SESSION_ENV, "127.0.0.1:1234:token")
    monkeypatch.setattr(
        funnel.sys, "stdin", io.BytesIO("# Plan\n☃\n".encode("utf-8"))
    )
    requests = []

    class ResponseStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, payload):
            requests.append(json.loads(payload.decode("utf-8")))
            return len(payload)

        def flush(self):
            pass

        def readline(self, limit):
            return b'{"code":0,"stdout":"","stderr":""}\n'

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def makefile(self, mode):
            return ResponseStream()

    monkeypatch.setattr(
        funnel.socket, "create_connection", lambda *args, **kwargs: Connection()
    )

    assert funnel._session_client(["shaped", "owner/repo#1", "--plan", "-"]) == 0
    assert requests == [{
        "token": "token",
        "argv": ["shaped", "owner/repo#1", "--plan", "-"],
        "stdin": "# Plan\n\u2603\n",
    }]


def test_session_client_does_not_read_tty_stdin(monkeypatch):
    monkeypatch.setenv(funnel.SESSION_ENV, "127.0.0.1:1234:token")
    requests = []

    class TTYStdin:
        encoding = "utf-8"

        def isatty(self):
            return True

        def read(self, *args):
            pytest.fail("TTY stdin must not be read")

    monkeypatch.setattr(funnel.sys, "stdin", TTYStdin())

    class ResponseStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, payload):
            requests.append(json.loads(payload.decode("utf-8")))
            return len(payload)

        def flush(self):
            pass

        def readline(self, limit):
            return b'{"code":0,"stdout":"","stderr":""}\n'

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def makefile(self, mode):
            return ResponseStream()

    monkeypatch.setattr(
        funnel.socket, "create_connection", lambda *args, **kwargs: Connection()
    )

    argv = ["shaped", "owner/repo#1", "--plan", "-"]
    assert funnel._session_client(argv) == 0
    assert requests == [{"token": "token", "argv": argv}]


def test_session_client_rejects_stdin_over_the_cap_before_connecting(
    monkeypatch, capsys
):
    monkeypatch.setenv(funnel.SESSION_ENV, "127.0.0.1:1234:token")
    monkeypatch.setattr(
        funnel.sys,
        "stdin",
        io.BytesIO(b"x" * (funnel.SESSION_STDIN_LIMIT + 1)),
    )
    monkeypatch.setattr(
        funnel.socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("oversize stdin must not connect"),
    )

    assert funnel._session_client(
        ["shaped", "owner/repo#1", "--plan", "-"]
    ) == 2
    assert capsys.readouterr().err == (
        "funnel: stdin exceeds the 1000000-byte session limit\n"
    )


def test_session_dispatch_exposes_stdin_only_during_main_and_restores_it(
    monkeypatch,
):
    original_stdin = sys.stdin
    observed = []

    def fake_main(argv, **kwargs):
        observed.append(sys.stdin.read())
        return 0

    monkeypatch.setattr(funnel, "main", fake_main)
    monkeypatch.setattr(funnel, "report_api_cost", lambda: None)
    monkeypatch.setattr(funnel, "report_graphql_spend", lambda: None)

    session = funnel.FunnelSession(loader=lambda: [])
    assert session.dispatch(["shaped"], stdin="# Plan\n") == (0, "", "")
    assert observed == ["# Plan\n"]
    assert sys.stdin is original_stdin


def test_session_dispatch_restores_stdin_when_main_raises(monkeypatch):
    original_stdin = sys.stdin

    def fail_main(argv, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(funnel, "main", fail_main)
    monkeypatch.setattr(funnel, "report_api_cost", lambda: None)
    monkeypatch.setattr(funnel, "report_graphql_spend", lambda: None)

    result = funnel.FunnelSession(loader=lambda: []).dispatch(
        ["shaped"], stdin="# Plan\n"
    )
    assert result == (2, "", "funnel: boom\n")
    assert sys.stdin is original_stdin


def test_muse_documents_direct_piped_shaped_calls_without_a_session_workaround():
    routine = (ROOT / "routines" / "muse.md").read_text()
    assert "shaped --plan -" in routine
    assert "env -u FUNNEL_SESSION" not in routine
