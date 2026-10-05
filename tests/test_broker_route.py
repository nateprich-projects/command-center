"""The isolated Codex shell cannot fall back to credentialed local calls."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import credential_broker  # noqa: E402
import broker_route  # noqa: E402
import funnel  # noqa: E402
from engine import implement  # noqa: E402


RUN = "27668f694e9d"


def _no_project_read():
    raise AssertionError("isolated Codex must not read Project from its shell")


def test_only_the_isolated_mac_codex_account_routes_to_broker(monkeypatch):
    assert broker_route.use_broker("codex", uid=506, system="darwin")
    assert not broker_route.use_broker("codex", uid=501, system="darwin")
    assert not broker_route.use_broker("codex", uid=506, system="linux")
    assert not broker_route.use_broker("muse", uid=506, system="darwin")
    monkeypatch.setattr(broker_route, "BROKER_ROUTE_ENABLED", False)
    assert not broker_route.use_broker("codex", uid=506, system="darwin")


def test_isolated_begin_uses_only_broker_before_any_project_read(monkeypatch,
                                                                   capsys):
    calls = []
    monkeypatch.setattr(broker_route, "use_broker", lambda agent: True)
    monkeypatch.setattr(credential_broker, "client_main",
                        lambda args: calls.append(args) or 0)
    assert funnel.main(
        ["begin", "--agent", "codex", "--tier", "standard"],
        _items_loader=_no_project_read,
    ) == 0
    assert calls == [["begin", "--tier", "standard"]]
    assert funnel.main(
        ["begin", "--agent", "codex", "--tier", "standard", "--idle"],
        _items_loader=_no_project_read,
    ) == 2
    assert len(calls) == 1
    assert "direct GitHub fallback is disabled" in capsys.readouterr().err


def test_unreachable_broker_refuses_begin_without_direct_retry(monkeypatch,
                                                               capsys):
    monkeypatch.setattr(broker_route, "use_broker", lambda agent: True)

    def unavailable(_payload):
        raise credential_broker.BrokerError(
            "credential broker is unavailable; no fallback or retry was attempted"
        )

    monkeypatch.setattr(credential_broker, "socket_request", unavailable)
    assert funnel.main(
        ["begin", "--agent", "codex", "--tier", "standard"],
        _items_loader=_no_project_read,
    ) == 1
    assert "no fallback or retry" in capsys.readouterr().err


def test_isolated_finish_routes_answer_without_direct_claim_or_push(
        monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(broker_route, "use_broker", lambda agent: True)
    monkeypatch.setattr(credential_broker, "client_main",
                        lambda args: calls.append(args) or 1)
    monkeypatch.setattr(funnel, "graphql_caller_for_run",
                        lambda *args: _no_project_read())
    assert implement.finish_main([
        "--run", RUN, "--agent", "codex", "--answer", '{"done":true}'
    ]) == 1
    assert calls == [["finish", "--run", RUN, "--answer", '{"done":true}']]
    assert implement.finish_main([
        "--run", RUN, "--agent", "codex", "--repo", "other/repo",
        "--answer", '{"done":true}'
    ]) == 2
    assert len(calls) == 1
    assert "direct GitHub fallback is disabled" in capsys.readouterr().err


def test_isolated_finish_reads_answer_file_only_through_broker_client(
        monkeypatch, tmp_path):
    answer = tmp_path / "answer.json"
    answer.write_text('{"declined":"missing prerequisite"}')
    calls = []
    monkeypatch.setattr(broker_route, "use_broker", lambda agent: True)
    monkeypatch.setattr(credential_broker, "client_main",
                        lambda args: calls.append(args) or 0)
    monkeypatch.setattr(funnel, "graphql_caller_for_run",
                        lambda *args: _no_project_read())
    assert implement.finish_main([
        "--run", RUN, "--agent", "codex", "--answer-file", str(answer)
    ]) == 0
    assert calls == [["finish", "--run", RUN, "--answer-file", str(answer)]]
