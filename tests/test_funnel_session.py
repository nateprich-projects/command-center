"""A Muse run reuses one disposable in-memory funnel load."""

from __future__ import annotations

import pathlib
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
