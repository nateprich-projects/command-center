"""The opening command reports only queues it actually consulted."""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import usage  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _bindings_never_touch_the_real_spool(monkeypatch):
    """`begin` now writes a binding record through the heartbeat (#497). The
    subprocess stubs in these tests cover the push but not the local spool, so
    stub the writer itself; tests that care patch it again explicitly."""
    import heartbeat

    monkeypatch.setattr(heartbeat, "record_binding", lambda *args, **kwargs: "pushed")
    monkeypatch.setattr(funnel, "finished_by_comments", lambda items: set())
    monkeypatch.setattr(funnel, "reconcile_orphaned_starts", lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "reconcile_abandoned_claims", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        funnel,
        "implementation_packet",
        lambda repo, number, agent: {
            "repo": repo,
            "ticket": {"number": number},
            "plan": None,
            "verdict": {"blocking": []},
            "prior_run": None,
        },
    )


def _allow_begin(monkeypatch):
    funnel.reset_api_usage()

    def run(argv, **kwargs):
        if argv[:3] == ["gh", "api", "graphql"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {
                        "rateLimit": {
                            "cost": 1,
                            "remaining": 5_000,
                            "resetAt": "later",
                        },
                    },
                }),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0, stdout="run-id\n", stderr=""
        )

    monkeypatch.setattr(
        funnel.subprocess,
        "run", run,
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: {"over_pace": False},
    )
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: {})

    def member_repos(after_first_response=None):
        if after_first_response is not None:
            after_first_response({
                "rateLimit": {"cost": 1, "remaining": 5_000},
            })
        return []

    monkeypatch.setattr(funnel, "member_repos", member_repos)


def _begin(monkeypatch, capsys, *, breakdown):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    assert funnel.cmd_begin([], NOW, "zcode", "standard", False, breakdown) == 0
    return json.loads(capsys.readouterr().out)


def _ticket(number, parent, *, body="Risk: standard", klass="Improve",
            in_motion_since=None):
    project = funnel.Item(
        repo="nateprich/example",
        number=parent,
        title="Project {}".format(parent),
        url="https://github.com/nateprich/example/issues/{}".format(parent),
        state="OPEN",
        status="Building",
        klass=klass,
        origin="agent",
        risk="standard",
        needs="none",
        children_total=1,
    )
    ticket = funnel.Item(
        repo=project.repo,
        number=number,
        title="Ticket {}".format(number),
        url="https://github.com/nateprich/example/issues/{}".format(number),
        state="OPEN",
        body=body,
        origin="agent",
        risk=("escalated" if "Risk: escalated" in body else "standard"),
        needs="none",
        parent=project.ref,
        item_id="item-{}".format(number),
        in_motion_since=in_motion_since,
    )
    return project, ticket


def _implementing_begin(monkeypatch, capsys, items, *, agent="codex",
                        tier="standard", repo_readiness=None, pr_facts=None,
                        caller_role=None, current_claims=None, reconcile=None,
                        band=None):
    _allow_begin(monkeypatch)
    if band is not None:
        _tight_budget(monkeypatch, band)
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        reconcile if reconcile is not None else (lambda *args: []),
    )
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(
        funnel, "ticket_pr_facts", lambda rows: pr_facts or {}
    )
    bodies = {item.number: item.body for item in items}
    monkeypatch.setattr(
        funnel, "_ticket_body", lambda repo, number: bodies.get(number) or ""
    )
    monkeypatch.setattr(
        funnel,
        "read_lock",
        lambda item: (
            item.in_motion_since
            if current_claims is None
            else current_claims.get(item.ref)
        ),
    )
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )
    assert funnel.cmd_begin(
        items, NOW, agent, tier, False,
        repo_readiness=repo_readiness,
        caller_role=caller_role,
    ) == 0
    return json.loads(capsys.readouterr().out), writes


def test_begin_prints_a_transient_json_envelope_when_project_load_is_truncated(
    monkeypatch, capsys
):
    _allow_begin(monkeypatch)
    funnel.reset_route_state()
    funnel.reset_api_usage()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "unexpected end of JSON input; GraphQL request ID "
                    "DD41:627C:A3F5CB:EEDB4E:6AA66A60"
                ),
            )
        if any("heartbeat.py" in str(part) for part in argv):
            return SimpleNamespace(
                returncode=0, stdout="begin-run\n", stderr=""
            )
        raise AssertionError("unexpected subprocess: {}".format(argv))

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(funnel.time, "sleep", lambda seconds: None)

    def load_truncated_project():
        funnel.gh_graphql("{viewer{login}}")
        return []

    assert funnel.main(
        ["begin", "--agent", "codex", "--tier", "standard"],
        _items_loader=load_truncated_project,
    ) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["agent"] == "codex"
    assert result["run"] == "begin-run"
    assert result["gate"] == "unknown"
    assert result["do"] == "stop"
    assert result["transient"] is True
    assert "transient" in result["why"]
    assert "DD41:627C:A3F5CB:EEDB4E:6AA66A60" in result["why"]
    assert funnel._API_USAGE["graphql_calls"] == 3
    assert funnel.graphql_spend()["calls"] == 3
    assert len([call for call in calls if call[:3] == ["gh", "api", "graphql"]]) == 3
    assert not any(
        "finish" in call and "budget-exhausted" in call for call in calls
    )


@pytest.mark.parametrize("gate", ["pace", "idle"])
def test_main_applies_begin_gates_before_loading_the_project(
    monkeypatch, capsys, gate
):
    """#655's 42-call/47-point before number is not paid by a refusal."""
    events = []

    monkeypatch.setattr(
        funnel,
        "_start_begin_heartbeat",
        lambda agent: events.append("heartbeat") or "run-id",
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: events.append("usage") or {
            "windows": {"five_hour": {"used_percent": 1.0}},
        },
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: (
            events.append("pace")
            or {"over_pace": gate == "pace"}
        ),
    )
    monkeypatch.setattr(
        usage,
        "idle_verdict",
        lambda agent, reading: events.append("idle") or {
            "over": gate == "idle",
            "why": "idle refusal",
        },
    )
    monkeypatch.setattr(
        funnel,
        "load_items",
        lambda: pytest.fail("a refused begin must not load the Project"),
    )

    argv = ["begin", "--agent", "codex", "--tier", "standard"]
    if gate == "idle":
        argv.append("--idle")
    assert funnel.main(argv) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["do"] == "stop"
    assert result["gate"] == "over"
    assert events == ["heartbeat", "usage", "pace"] + (
        ["idle"] if gate == "idle" else []
    )


def test_main_loads_the_project_after_begin_gates_pass(
    monkeypatch, capsys
):
    """A passing preflight still reaches the normal queue and WIP checks."""
    events = []
    monkeypatch.setattr(
        funnel,
        "_start_begin_heartbeat",
        lambda agent: events.append("heartbeat") or "run-id",
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: events.append("usage") or {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: events.append("pace") or {
            "over_pace": False,
        },
    )
    def member_repos(after_first_response=None):
        events.append("api")
        if after_first_response is not None:
            after_first_response({
                "rateLimit": {
                    "cost": 1,
                    "remaining": 5_000,
                    "resetAt": "later",
                },
            })
        return []

    monkeypatch.setattr(funnel, "member_repos", member_repos)
    monkeypatch.setattr(
        funnel,
        "load_items",
        lambda include_details=True: events.append("load") or [],
    )
    monkeypatch.setattr(funnel, "repo_readiness_for_items", lambda items: {})
    monkeypatch.setattr(
        funnel,
        "cmd_begin",
        lambda items, now, agent, tier, idle, breakdown=False,
        repo_readiness=None, caller_role=None, _detail_loader=None,
        _preflight=None, timings=None, _pr_facts=None,
        _pr_facts_error=None, _pr_facts_elapsed=None: (
            events.append(("begin", items, _preflight, timings)) or 0
        ),
    )

    assert funnel.main(["begin", "--agent", "codex", "--tier", "standard"]) == 0
    capsys.readouterr()
    assert [event for event in events if isinstance(event, str)] == [
        "heartbeat", "usage", "pace", "api", "load",
    ]
    assert events[-1][0] == "begin"
    assert events[-1][2][0]["gate"] == "ok"
    assert "begin_load.member_repos" in events[-1][3]


def test_main_stands_down_before_loading_the_project_when_reserve_is_low(
    monkeypatch, capsys
):
    """A low window is a clean stop before the expensive Project read."""
    import heartbeat

    events = []
    reserve_events = []
    monkeypatch.setattr(
        funnel,
        "_start_begin_heartbeat",
        lambda agent: events.append("heartbeat") or "run-id",
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: events.append("usage") or {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: events.append("pace") or {
            "over_pace": False,
        },
    )

    def first_member_query(query, **variables):
        events.append(("api", query))
        return {
            "rateLimit": {
                "cost": 1,
                "remaining": funnel.GRAPHQL_RESERVE_POINT_CEILING - 1,
                "resetAt": "later",
            },
        }

    monkeypatch.setattr(funnel, "OWNERS", [("user", "owner")])
    monkeypatch.setattr(funnel, "gh_graphql", first_member_query)
    monkeypatch.setattr(
        funnel,
        "_gh_api_json",
        lambda *args, **kwargs: pytest.fail(
            "begin reserve must not consult REST rate_limit"
        ),
    )
    monkeypatch.setattr(
        funnel,
        "load_items",
        lambda *args, **kwargs: pytest.fail("reserve stop must not load Project"),
    )
    monkeypatch.setattr(
        heartbeat,
        "record_event",
        lambda agent, run, outcome, **kwargs: reserve_events.append(
            (agent, run, outcome, kwargs)
        ) or "spooled",
    )

    assert funnel.main(["begin", "--agent", "codex", "--tier", "standard"]) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["gate"] == "reserve"
    assert result["do"] == "stop"
    assert events[:3] == ["heartbeat", "usage", "pace"]
    assert events[3][0] == "api"
    assert "rateLimit" in events[3][1]
    assert "repositories(first:" in events[3][1]
    assert "projectV2" not in events[3][1]
    assert reserve_events == [
        ("codex", "run-id", "skipped-api-reserve", {"note": result["why"]}),
    ]
    assert "funnel: skipped-api-reserve:" in captured.err


def test_begin_preflight_uses_the_capped_engineering_floor(monkeypatch):
    monkeypatch.setattr(
        funnel,
        "gh_graphql",
        lambda *args, **kwargs: pytest.fail(
            "reserve check must consume an existing GraphQL response"
        ),
    )
    assert funnel._begin_api_reserve_preflight(
        "codex", "standard", None,
        {"rateLimit": {"remaining": 826}},
    ) is None


def test_main_treats_a_structured_empty_window_as_a_clean_reserve_stop(
    monkeypatch, capsys
):
    """GitHub's partial zero-budget response must not load the Project."""
    import heartbeat

    reset_at = "2026-09-16T05:30:32Z"
    events = []
    reserve_events = []
    monkeypatch.setattr(
        funnel,
        "_start_begin_heartbeat",
        lambda agent: events.append("heartbeat") or "run-id",
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: events.append("usage") or {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: events.append("pace") or {
            "over_pace": False,
        },
    )

    def empty_window(query, **variables):
        events.append(("api", query))
        funnel._GRAPHQL_SPEND.update(
            {"calls": 1, "cost": 0, "remaining": 0, "reset_at": reset_at}
        )
        raise funnel.GitHubError("API rate limit already exceeded")

    monkeypatch.setattr(funnel, "gh_graphql", empty_window)
    monkeypatch.setattr(
        funnel,
        "load_items",
        lambda *args, **kwargs: pytest.fail("empty window must not load Project"),
    )
    monkeypatch.setattr(
        heartbeat,
        "record_event",
        lambda agent, run, outcome, **kwargs: reserve_events.append(
            (agent, run, outcome, kwargs)
        ) or "spooled",
    )

    assert funnel.main(["begin", "--agent", "codex", "--tier", "standard"]) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["gate"] == "reserve"
    assert result["do"] == "stop"
    assert result["timings"]["begin_load.member_repos"] >= 0
    assert events[3][0] == "api"
    assert reserve_events == [
        ("codex", "run-id", "skipped-api-reserve", {"note": result["why"]}),
    ]
    assert "funnel: skipped-api-reserve:" in captured.err


def test_begin_records_a_named_finish_for_a_structured_exhaustion(
    monkeypatch, capsys
):
    _allow_begin(monkeypatch)
    funnel.reset_route_state()
    funnel.reset_api_usage()
    reset_at = "2026-09-16T05:30:32Z"
    calls = []
    responses = [
        {
            "data": {
                "rateLimit": {
                    "cost": 1,
                    "remaining": 5_000,
                    "resetAt": reset_at,
                },
            },
        },
        {
            "data": {
                "rateLimit": {
                    "cost": 0,
                    "remaining": 0,
                    "resetAt": reset_at,
                },
            },
            "errors": [{"message": "API rate limit already exceeded"}],
        },
    ]

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(responses.pop(0)),
                stderr="",
            )
        if any("heartbeat.py" in str(part) for part in argv):
            if "finish" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="begin-run\n", stderr="")
        raise AssertionError("unexpected subprocess: {}".format(argv))

    monkeypatch.setattr(funnel.subprocess, "run", run)

    def load_exhausted_project():
        funnel.gh_graphql("{viewer{login}}")
        funnel.gh_graphql("{viewer{login}}")
        return []

    assert funnel.main(
        ["begin", "--agent", "codex", "--tier", "standard"],
        _items_loader=load_exhausted_project,
    ) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["run"] == "begin-run"
    assert result["gate"] == "unknown"
    assert result["do"] == "stop"
    finishes = [call for call in calls if "finish" in call]
    assert len(finishes) == 1
    finish = finishes[0]
    assert finish[finish.index("--outcome") + 1] == "budget-exhausted"
    assert finish[finish.index("--note") + 1] == (
        "budget-exhausted remaining=0 resetAt={}".format(reset_at)
    )
    funnel.reset_route_state()


@pytest.mark.parametrize(
    "response",
    [
        {
            "data": {
                "rateLimit": {
                    "cost": 0,
                    "remaining": 0,
                    "resetAt": "2026-09-16T05:30:32Z",
                },
            },
            "errors": [{"message": "API rate limit already exceeded"}],
        },
        {
            "data": {
                "viewer": {"login": "n"},
                "rateLimit": {
                    "cost": 0,
                    "remaining": 0,
                    "resetAt": "2026-09-16T05:30:32Z",
                },
            },
        },
    ],
)
def test_begin_exhaustion_shapes_produce_the_same_named_finish(
    monkeypatch, response
):
    funnel.reset_route_state()
    funnel.reset_api_usage()
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(response), stderr=""
            )
        if any("heartbeat.py" in str(part) for part in argv):
            if "finish" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="begin-run\n", stderr="")
        raise AssertionError("unexpected subprocess: {}".format(argv))

    monkeypatch.setattr(funnel.subprocess, "run", run)
    error = None
    try:
        funnel.gh_graphql("{viewer{login}}")
    except funnel.GitHubError as exc:
        error = exc
    if error is None:
        error = funnel.GitHubError("begin failed after a zero-budget response")

    funnel._begin_error_envelope("codex", error)

    finish = next(call for call in calls if "finish" in call)
    assert finish[finish.index("--outcome") + 1] == "budget-exhausted"
    assert finish[finish.index("--note") + 1] == (
        "budget-exhausted remaining=0 resetAt=2026-09-16T05:30:32Z"
    )
    funnel.reset_route_state()


def test_begin_error_after_heartbeat_start_does_not_start_a_second_run(
    monkeypatch, capsys
):
    """Reconcile no longer aborts begin (#823): the listing error is
    recorded, selection runs against the empty board and stops cleanly, and
    still only one run is started."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {
                        "rateLimit": {
                            "cost": 1,
                            "remaining": 5_000,
                            "resetAt": "later",
                        },
                    },
                }),
                stderr="",
            )
        if any("heartbeat.py" in str(part) for part in argv):
            return SimpleNamespace(
                returncode=0, stdout="already-started\n", stderr=""
            )
        raise AssertionError("unexpected subprocess: {}".format(argv))

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        usage, "read_agent", lambda agent, timestamp: {"windows": {}}
    )
    monkeypatch.setattr(
        usage, "pace", lambda reading, timestamp, provider: {"over_pace": False}
    )
    monkeypatch.setattr(
        funnel,
        "member_repos",
        lambda after_first_response=None: (
            after_first_response({
                "rateLimit": {"cost": 1, "remaining": 5_000}
            }) or []
        ),
    )
    monkeypatch.setattr(funnel, "repo_readiness_for_items", lambda items: {})
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda *args: (_ for _ in ()).throw(funnel.GitHubError("offline")),
    )

    assert funnel.main(
        ["begin", "--agent", "codex", "--tier", "standard"],
        _items=[],
    ) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["run"] == "already-started"
    assert result["gate"] == "ok"
    assert result["do"] == "stop"
    assert result["reconcile_errors"] == [{
        "step": "approved_merges",
        "error": "offline",
        "transient": False,
    }]
    assert len([
        call for call in calls
        if any("heartbeat.py" in str(part) for part in call)
    ]) == 1


def _reconcile_begin(monkeypatch, capsys, items, rows, verdicts, merge_result=0):
    _allow_begin(monkeypatch)
    def facts():
        by_ref = {}
        for raw in rows:
            row = dict(raw)
            row.setdefault("state", "OPEN")
            row["verdict"] = verdicts.get(row.get("number"))
            ref = funnel.ticket_ref_from_branch(
                row.get("repo", items[0].repo), row.get("headRefName") or ""
            )
            if ref:
                by_ref.setdefault(ref, []).append(row)
        primary = {ref: values[0] for ref, values in by_ref.items()}
        return funnel.TicketPRFacts(primary, rows_by_ref=by_ref)

    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda _items: facts())
    calls = []

    def merge(rows, now, repo, pr, confirmed):
        calls.append((repo, pr, confirmed))
        return merge_result

    monkeypatch.setattr(funnel, "cmd_merge", merge)
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)
    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    return json.loads(capsys.readouterr().out), calls


def test_begin_keeps_merge_and_status_reconciles_in_separate_keys(
    monkeypatch, capsys
):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda *args: [{"ref": "nateprich/example#70", "result": "merged"}],
    )
    monkeypatch.setattr(
        funnel,
        "reconcile_closed_items",
        lambda *args: ["nateprich/example#71"],
    )
    monkeypatch.setattr(funnel, "reconcile_auto_closeable_projects", lambda *args: [])
    monkeypatch.setattr(funnel, "clear_satisfied_blocks", lambda *args, **kwargs: [])
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(
        funnel, "next_ticket_for_tier", lambda *args, **kwargs: None
    )

    assert funnel.cmd_begin([], NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["reconciled_merges"] == [
        {"ref": "nateprich/example#70", "result": "merged"}
    ]
    assert result["reconciled_statuses"] == ["nateprich/example#71"]
    assert "reconciled" not in result


def test_begin_runs_parked_wakes_as_a_reconcile_step(monkeypatch, capsys):
    items = [
        _closed_project_item(
            234, status="Parked", state_reason="NOT_PLANNED"
        )
    ]
    calls = []

    def wake(rows, now):
        calls.append((rows, now))
        return [items[0].ref]

    monkeypatch.setattr(funnel, "reconcile_parked_wakes", wake)
    result, _, _ = _begin_with_reconcile_wired(monkeypatch, capsys, items)

    assert calls == [(items, NOW)]
    assert result["woke_parked"] == [items[0].ref]


def test_begin_retries_an_approval_at_the_current_head(monkeypatch, capsys):
    project, ticket = _ticket(7, 6)
    result, calls = _reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        [{"number": 70, "headRefName": "ticket/7", "headRefOid": "new-head"}],
        {70: {"verdict": "approved", "head_sha": "new-head"}},
    )

    assert calls == [(ticket.repo, 70, True)]
    assert result["reconciled_merges"] == [{
        "repo": ticket.repo,
        "pr": 70,
        "ref": ticket.ref,
        "result": "merged",
    }]
    assert ticket.state == "CLOSED"
    assert ticket.state_reason == "COMPLETED"


def test_begin_does_not_retry_an_old_or_rejected_verdict(monkeypatch, capsys):
    project, ticket = _ticket(8, 6)
    rows = [
        {"number": 80, "headRefName": "ticket/8", "headRefOid": "new-head"},
        {"number": 81, "headRefName": "ticket/8", "headRefOid": "new-head"},
    ]
    result, calls = _reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        rows,
        {
            80: {"verdict": "approved", "head_sha": "old-head"},
            81: {"verdict": "rejected", "head_sha": "new-head"},
        },
    )

    assert calls == []
    assert "reconciled_merges" not in result
    assert ticket.state == "OPEN"


def test_begin_reports_a_merge_gate_refusal_without_closing_the_ticket(
    monkeypatch, capsys
):
    project, ticket = _ticket(9, 6)
    result, calls = _reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        [{"number": 90, "headRefName": "ticket/9", "headRefOid": "head"}],
        {90: {"verdict": "approved", "head_sha": "head"}},
        merge_result=1,
    )

    assert calls == [(ticket.repo, 90, True)]
    assert result["reconciled_merges"] == [{
        "repo": ticket.repo,
        "pr": 90,
        "ref": ticket.ref,
        "result": "refused",
    }]
    assert ticket.state == "OPEN"


def test_begin_reconcile_runs_before_codex_queue_lookup(monkeypatch, capsys):
    project, ticket = _ticket(10, 6)
    events = []
    _allow_begin(monkeypatch)
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda *args: events.append("reconcile") or [],
    )
    monkeypatch.setattr(
        funnel,
        "clear_satisfied_blocks",
        lambda *args, **kwargs: events.append("clear") or [],
    )
    monkeypatch.setattr(
        funnel,
        "awaiting_review",
        lambda rows: events.append("awaiting") or set(),
    )
    monkeypatch.setattr(
        funnel,
        "next_ticket_for_tier",
        lambda *args, **kwargs: events.append("next") or None,
    )

    assert funnel.cmd_begin([project, ticket], NOW, "codex", "standard", False) == 0
    capsys.readouterr()
    assert events == ["reconcile", "clear", "awaiting", "next"]


def test_begin_reconcile_is_idempotent_when_the_pr_is_no_longer_open(
    monkeypatch, capsys
):
    project, ticket = _ticket(11, 6)
    rows = [
        {"number": 110, "headRefName": "ticket/11", "headRefOid": "head"}
    ]
    calls = []
    _allow_begin(monkeypatch)

    def facts(_items):
        current = [dict(row, state="OPEN", verdict={
            "verdict": "approved", "head_sha": "head"
        }) for row in rows]
        by_ref = {ticket.ref: current} if current else {}
        return funnel.TicketPRFacts(
            {ref: values[0] for ref, values in by_ref.items()},
            rows_by_ref=by_ref,
        )

    monkeypatch.setattr(funnel, "ticket_pr_facts", facts)

    def merge(items, now, repo, pr, confirmed):
        calls.append(pr)
        rows.clear()
        return 0

    monkeypatch.setattr(funnel, "cmd_merge", merge)
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)

    assert funnel.cmd_begin([project, ticket], NOW, "codex", "standard", False) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["reconciled_merges"][0]["result"] == "merged"

    assert funnel.cmd_begin([project, ticket], NOW, "codex", "standard", False) == 0
    second = json.loads(capsys.readouterr().out)
    assert calls == [110]
    assert "reconciled_merges" not in second


def _completed_project(number, *, klass="Improve", children_done=2,
                      carried_human_step=False, origin="agent", parent=None):
    repo = "nateprich/example"
    body = (
        funnel.origin_block(origin, at=NOW, run="reconcile-run", agent="codex")
        if origin in funnel.ORIGIN_VOICES else None
    )
    project = funnel.Item(
        repo=repo,
        number=number,
        title="Project {}".format(number),
        url="https://github.com/{}/issues/{}".format(repo, number),
        state="OPEN",
        body=body,
        status="Building",
        klass=klass,
        origin=("agent" if origin == "agent" else "Nate"),
        risk="standard",
        needs="none",
        parent=parent,
        item_id="project-{}".format(number),
        children_total=2,
        children_done=children_done,
        carried_human_step=carried_human_step,
    )
    children = [
        funnel.Item(
            repo=repo,
            number=number + offset,
            title="Ticket {}".format(number + offset),
            url="https://github.com/{}/issues/{}".format(repo, number + offset),
            state="CLOSED",
            parent=project.ref,
        )
        for offset in (1, 2)
    ]
    return [project] + children


def _closed_project_item(number, *, status, state_reason, labels=None):
    repo = "nateprich/example"
    return funnel.Item(
        repo=repo,
        number=number,
        title="Closed project {}".format(number),
        url="https://github.com/{}/issues/{}".format(repo, number),
        state="CLOSED",
        state_reason=state_reason,
        status=status,
        labels=list(labels or []),
        item_id="project-{}".format(number),
    )


def _begin_with_reconcile_wired(monkeypatch, capsys, items):
    calls = []
    graphql_calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--agent" in argv:
            return SimpleNamespace(stdout="run-id\n", returncode=0, stderr="")
        return SimpleNamespace(stdout="", returncode=0, stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        usage, "read_agent", lambda agent, timestamp: {"windows": {}}
    )
    monkeypatch.setattr(
        usage, "pace", lambda reading, timestamp, provider: {"over_pace": False}
    )
    monkeypatch.setattr(funnel, "drift_since_approval", lambda item: [])
    monkeypatch.setattr(funnel, "gh_graphql", lambda query, **variables: (
        graphql_calls.append((query, variables)) or {}
    ))
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: funnel.TicketPRFacts())
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "done-option")
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)

    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    return json.loads(capsys.readouterr().out), calls, graphql_calls


def test_begin_reconciles_a_completed_upkeep_project_and_records_marker(
    monkeypatch, capsys
):
    items = _completed_project(200)

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert result["auto_closed"] == [items[0].ref]
    assert items[0].state == "CLOSED"
    assert items[0].status == "Done"
    assert items[0].state_reason == "COMPLETED"
    assert (funnel.SET_FIELD, {
        "project": funnel.PROJECT_ID,
        "item": "project-200",
        "field": funnel.STATUS_FIELD_ID,
        "option": "done-option",
    }) in graphql_calls
    assert [
        "gh", "issue", "close", "200", "--repo", "nateprich/example",
        "--reason", "completed",
    ] in calls
    comments = [
        call[-1] for call in calls
        if call[:3] == ["gh", "issue", "comment"]
    ]
    assert len(comments) == 1
    assert comments[0].startswith(funnel.CLOSED_ITSELF_PREFIX)
    payload = json.loads(
        comments[0].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    assert payload == {
        "drift": [],
        "tickets": [
            {"ref": items[1].ref, "title": items[1].title},
            {"ref": items[2].ref, "title": items[2].title},
        ],
    }


def test_begin_reconciles_completed_parented_item_and_records_marker(
    monkeypatch, capsys
):
    items = _completed_project(205, parent="nateprich/example#204")

    result, calls, _graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert result["auto_closed"] == [items[0].ref]
    assert items[0].state == "CLOSED"
    assert items[0].status == "Done"
    comments = [
        call[-1] for call in calls
        if call[:3] == ["gh", "issue", "comment"]
    ]
    assert len(comments) == 1
    assert comments[0].startswith(funnel.CLOSED_ITSELF_PREFIX)
    payload = json.loads(
        comments[0].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    assert payload["tickets"] == [
        {"ref": items[1].ref, "title": items[1].title},
        {"ref": items[2].ref, "title": items[2].title},
    ]


def test_begin_preserves_parentless_project_reconciliation_and_marker(
    monkeypatch, capsys
):
    items = _completed_project(206, parent=None)

    result, calls, _graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert items[0].parent is None
    assert result["auto_closed"] == [items[0].ref]
    assert items[0].state == "CLOSED"
    assert items[0].status == "Done"
    comments = [
        call[-1] for call in calls
        if call[:3] == ["gh", "issue", "comment"]
    ]
    assert len(comments) == 1
    assert comments[0].startswith(funnel.CLOSED_ITSELF_PREFIX)


def test_begin_leaves_a_parented_leaf_ticket_for_finish_ticket(
    monkeypatch, capsys
):
    leaf = funnel.Item(
        repo="nateprich/example",
        number=230,
        title="Leaf ticket",
        url="https://github.com/nateprich/example/issues/230",
        state="OPEN",
        body=funnel.origin_block(
            "agent", at=NOW, run="reconcile-run", agent="codex"
        ),
        status="Building",
        klass="Broken",
        parent="nateprich/example#229",
        item_id="project-230",
        children_total=0,
        children_done=0,
    )

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, [leaf]
    )

    assert "auto_closed" not in result
    assert leaf.state == "OPEN"
    assert leaf.status == "Building"
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]
    assert not [
        call for call in graphql_calls
        if call[1].get("item") == "project-230"
    ]


@pytest.mark.parametrize("klass", ["New", "Replace"])
def test_begin_leaves_a_nonqualifying_parented_item_at_building_gate(
    monkeypatch, capsys, klass
):
    items = _completed_project(
        240, klass=klass, parent="nateprich/example#239"
    )

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert funnel.gate_question(items[0]) == "Accept it?"
    assert "auto_closed" not in result
    assert items[0].state == "OPEN"
    assert items[0].status == "Building"
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]
    assert not [
        call for call in graphql_calls
        if call[1].get("item") == "project-240"
    ]


@pytest.mark.parametrize(
    "klass,carried_human_step,children_done,origin",
    [
        ("New", False, 2, "agent"),
        ("Improve", True, 2, "nate-relayed"),
        ("Improve", False, 1, "agent"),
    ],
)
def test_begin_leaves_non_reconcilable_projects_untouched(
    monkeypatch, capsys, klass, carried_human_step, children_done, origin
):
    items = _completed_project(
        210,
        klass=klass,
        carried_human_step=carried_human_step,
        children_done=children_done,
        origin=origin,
    )

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "auto_closed" not in result
    assert items[0].state == "OPEN"
    assert items[0].status == "Building"
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]
    assert not [
        call for call in graphql_calls
        if call[1].get("item") == "project-210"
    ]


@pytest.mark.parametrize("klass", ["Broken", "Investigate", "Maintenance"])
@pytest.mark.parametrize("origin", ["agent", "nate-direct", "nate-relayed"])
@pytest.mark.parametrize("carried_human_step", [False, True])
def test_begin_reconciles_parented_upkeep_items_regardless_of_origin_or_human_step(
    monkeypatch, capsys, klass, origin, carried_human_step
):
    items = _completed_project(
        211,
        klass=klass,
        carried_human_step=carried_human_step,
        origin=origin,
        parent="nateprich/example#210",
    )

    result, calls, _graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert result["auto_closed"] == [items[0].ref]
    assert items[0].state == "CLOSED"
    assert items[0].status == "Done"
    assert [
        "gh", "issue", "close", str(items[0].number), "--repo",
        "nateprich/example",
        "--reason", "completed",
    ] in calls
    comments = [
        call[-1] for call in calls
        if call[:3] == ["gh", "issue", "comment"]
    ]
    assert len(comments) == 1
    assert comments[0].startswith(funnel.CLOSED_ITSELF_PREFIX)


@pytest.mark.parametrize(
    "origin", ["nate-direct", "nate-relayed", None, "malformed"]
)
def test_begin_keeps_parented_improve_items_without_agent_origin_at_accept_gate(
    monkeypatch, capsys, origin
):
    items = _completed_project(
        212,
        klass="Improve",
        origin=None if origin == "malformed" else origin,
        parent="nateprich/example#210",
    )
    if origin == "malformed":
        items[0].body = (
            funnel.ORIGIN_MARKER + "\n\n```json\nnot json\n```"
        )

    assert funnel.gate_question(items[0]) == "Accept it?"

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "auto_closed" not in result
    assert items[0].state == "OPEN"
    assert items[0].status == "Building"
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]
    assert not [
        call for call in graphql_calls
        if call[1].get("item") == "project-212"
    ]


def test_begin_reconcile_is_idempotent(monkeypatch, capsys):
    items = _completed_project(220)

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )
    assert result["auto_closed"] == [items[0].ref]

    calls.clear()
    graphql_calls.clear()
    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "auto_closed" not in result
    assert not graphql_calls
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]


def test_begin_repairs_closed_terminal_statuses_and_stale_shaping_labels(
    monkeypatch, capsys
):
    completed = _closed_project_item(
        230, status="Building", state_reason="COMPLETED"
    )
    parked = _closed_project_item(
        231, status="Ideas", state_reason="NOT_PLANNED",
        labels=["needs-shaping"],
    )
    already_done = _closed_project_item(
        232, status="Done", state_reason="COMPLETED"
    )
    already_parked = _closed_project_item(
        233, status="Parked", state_reason="NOT_PLANNED"
    )
    open_item = funnel.Item(
        repo="nateprich/example",
        number=234,
        title="Open idea",
        url="https://github.com/nateprich/example/issues/234",
        state="OPEN",
        state_reason="NOT_PLANNED",
        status="Ideas",
        labels=["needs-shaping"],
        item_id="project-234",
    )
    items = [completed, parked, already_done, already_parked, open_item]

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert result["reconciled_statuses"] == [completed.ref, parked.ref]
    assert completed.status == "Done"
    assert parked.status == "Parked"
    assert parked.labels == []
    assert already_done.status == "Done"
    assert already_parked.status == "Parked"
    assert open_item.status == "Ideas"
    assert open_item.labels == ["needs-shaping"]
    assert [variables for query, variables in graphql_calls] == [
        {
            "project": funnel.PROJECT_ID,
            "item": "project-230",
            "field": funnel.STATUS_FIELD_ID,
            "option": "done-option",
        },
        {
            "project": funnel.PROJECT_ID,
            "item": "project-231",
            "field": funnel.STATUS_FIELD_ID,
            "option": "done-option",
        },
    ]
    assert [
        call for call in calls
        if call[:3] == ["gh", "issue", "edit"]
    ] == [[
        "gh", "issue", "edit", "231", "--repo", "nateprich/example",
        "--remove-label", "needs-shaping",
    ]]

    calls.clear()
    graphql_calls.clear()
    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "reconciled_statuses" not in result
    assert not graphql_calls
    assert not [
        call for call in calls
        if call[:3] == ["gh", "issue", "edit"]
    ]


def test_begin_releases_every_claim_left_on_a_closed_ticket(
    monkeypatch, capsys
):
    first_project, first = _ticket(
        240, 239, in_motion_since=NOW - timedelta(minutes=5)
    )
    second_project, second = _ticket(
        242, 241, in_motion_since=NOW - timedelta(minutes=10)
    )
    first.state = "CLOSED"
    first.state_reason = "COMPLETED"
    second.state = "CLOSED"
    second.state_reason = "COMPLETED"

    result, _, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys,
        [first_project, first, second_project, second],
    )

    assert result["released_claims"] == [first.ref, second.ref]
    assert first.in_motion_since is None
    assert second.in_motion_since is None
    assert [
        variables for query, variables in graphql_calls
        if query == funnel.SET_LOCK
    ] == [
        {
            "project": funnel.PROJECT_ID,
            "item": first.item_id,
            "field": funnel.LOCK_FIELD_ID,
            "value": "",
        },
        {
            "project": funnel.PROJECT_ID,
            "item": second.item_id,
            "field": funnel.LOCK_FIELD_ID,
            "value": "",
        },
    ]


def test_codex_begin_records_heartbeat_before_selecting_and_claiming(
    monkeypatch, capsys
):
    project, ticket = _ticket(7, 6)
    items = [project, ticket]
    events = []

    def heartbeat(*args, **kwargs):
        events.append("heartbeat")
        return SimpleNamespace(stdout="run-id\n")

    monkeypatch.setattr(funnel.subprocess, "run", heartbeat)
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda *args: events.append("reconcile") or [],
    )
    monkeypatch.setattr(usage, "read_agent", lambda agent, timestamp: {"windows": {}})
    monkeypatch.setattr(
        usage, "pace", lambda reading, timestamp, provider: {"over_pace": False}
    )
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: {})
    monkeypatch.setattr(
        funnel,
        "clear_satisfied_blocks",
        lambda *args, **kwargs: events.append("clear") or [],
    )
    monkeypatch.setattr(
        funnel,
        "next_ticket_for_tier",
        lambda *args, **kwargs: events.append("next") or ticket,
    )
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: events.append("claim")
    )
    monkeypatch.setattr(
        funnel, "read_lock", lambda item: events.append("read-lock") or None
    )

    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert events == [
        "heartbeat", "reconcile", "clear", "next", "read-lock", "claim"
    ]


def test_codex_ticket_begin_carries_the_implementation_packet_and_vendor_block(
    monkeypatch, capsys
):
    project, ticket = _ticket(81, 80)
    packet = {
        "repo": ticket.repo,
        "ticket": {"number": ticket.number, "body": "Do the work."},
        "plan": {"number": project.number, "body": "# Plan"},
        "verdict": {"blocking": ["cover the empty case"]},
        "prior_run": {"session": "prior.jsonl"},
    }
    calls = []
    monkeypatch.setattr(
        funnel,
        "implementation_packet",
        lambda repo, number, agent: (
            calls.append((repo, number, agent)) or packet
        ),
    )

    result, _ = _implementing_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "ticket"
    assert result["packet"] == packet
    assert result["vendor"] == funnel.CODEX_IMPLEMENT_VENDOR
    assert "--answer-file PATH" in result["vendor"]["answer_handoff"]
    assert calls == [(ticket.repo, ticket.number, "codex")]


def test_codex_begin_binds_before_loading_the_implementation_packet(
    monkeypatch, capsys
):
    import heartbeat

    project, ticket = _ticket(89, 88)
    events = []
    monkeypatch.setattr(
        heartbeat,
        "record_binding",
        lambda agent, run, do, work, repo=None: (
            events.append(("bind", work)) or "pushed"
        ),
    )

    def packet(repo, number, agent):
        events.append(("packet", "{}#{}".format(repo, number)))
        return {"repo": repo, "ticket": {"number": number}}

    monkeypatch.setattr(funnel, "implementation_packet", packet)

    result, _ = _implementing_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "ticket"
    assert events == [("bind", ticket.ref), ("packet", ticket.ref)]


def test_codex_stop_and_non_codex_ticket_do_not_carry_the_vendor_packet(
    monkeypatch, capsys
):
    stopped, _ = _implementing_begin(monkeypatch, capsys, [])
    assert stopped["do"] == "stop"
    assert "packet" not in stopped and "vendor" not in stopped

    # No other agent implements since #1322; put Muse back on the roster, the
    # reversal path, to show a non-Codex ticket still carries no vendor block.
    monkeypatch.setitem(funnel.AGENTS_BY_ROLE["implement"], "muse",
                        frozenset(funnel.TIERS))
    project, ticket = _ticket(83, 82, body="Risk: escalated")
    muse, _ = _implementing_begin(
        monkeypatch, capsys, [project, ticket], agent="muse", tier="escalated"
    )
    assert muse["do"] == "ticket"
    assert "packet" not in muse and "vendor" not in muse


def test_codex_packet_failure_releases_the_claim_and_stops(
    monkeypatch, capsys
):
    project, ticket = _ticket(85, 84)
    monkeypatch.setattr(
        funnel,
        "implementation_packet",
        lambda *args: (_ for _ in ()).throw(
            funnel.GitHubError("packet source unavailable")
        ),
    )

    result, writes = _implementing_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "stop"
    assert result["gate"] == "error"
    assert "work" not in result
    assert "packet source unavailable" in result["why"]
    assert writes[-1] == (ticket.ref, None)


def test_ticket_branch_facts_failure_stops_with_an_error_gate(
    monkeypatch, capsys
):
    """A begin that stops on a GitHub failure must not look like an empty
    queue: the stop carries gate error (#1216)."""
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())

    def facts(rows):
        raise funnel.GitHubError("HTTP 502")

    monkeypatch.setattr(funnel, "ticket_pr_facts", facts)
    assert funnel.cmd_begin([], NOW, "muse", "escalated", False) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["do"] == "stop"
    assert result["gate"] == "error"
    assert "could not establish ticket branch facts" in result["why"]


def test_cmd_begin_uses_preloaded_branch_facts_and_records_parallel_time(
    monkeypatch, capsys
):
    _allow_begin(monkeypatch)
    project, ticket = _ticket(86, 87)
    facts = {ticket.ref: {"branch_exists": True}}
    merge_calls = []
    timings = {}
    monkeypatch.setattr(
        funnel,
        "ticket_pr_facts",
        lambda rows: (_ for _ in ()).throw(
            AssertionError("preloaded branch facts must be reused")
        ),
    )
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda items, now, pr_facts: merge_calls.append(pr_facts) or [],
    )
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())

    assert funnel.cmd_begin(
        [project, ticket], NOW, "zcode", "standard", False,
        _pr_facts=facts,
        _pr_facts_elapsed=0.25,
        timings=timings,
    ) == 0

    assert merge_calls == [facts]
    assert timings["begin_load.ticket_pr_facts"] == pytest.approx(0.25)
    capsys.readouterr()


def test_two_same_minute_begins_claim_different_tickets(monkeypatch, capsys):
    first_project, first = _ticket(8, 7)
    second_project, second = _ticket(10, 9)
    items = [first_project, first, second_project, second]
    current_claims = {}

    first_result, first_writes = _implementing_begin(
        monkeypatch,
        capsys,
        items,
        current_claims=current_claims,
    )
    current_claims[first.ref] = NOW
    second_result, second_writes = _implementing_begin(
        monkeypatch,
        capsys,
        items,
        current_claims=current_claims,
    )

    assert first_result["work"]["ref"] == first.ref
    assert second_result["work"]["ref"] == second.ref
    assert [ref for ref, value in first_writes if value] == [first.ref]
    assert [ref for ref, value in second_writes if value] == [second.ref]


def test_begin_passes_a_held_head_and_offers_the_next_startable_ticket(
    monkeypatch, capsys
):
    first_project, first = _ticket(
        8, 7, in_motion_since=NOW - timedelta(minutes=5)
    )
    second_project, second = _ticket(10, 9)

    result, writes = _implementing_begin(
        monkeypatch,
        capsys,
        [first_project, first, second_project, second],
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == second.ref
    assert result["held"] == [first.ref]
    assert [ref for ref, value in writes if value] == [second.ref]


def test_merge_releases_the_claim_on_the_ticket_it_finishes(monkeypatch):
    project, ticket = _ticket(12, 11, in_motion_since=NOW)
    writes = []

    monkeypatch.setattr(funnel, "resolve_repo", lambda repo: ticket.repo)
    monkeypatch.setattr(funnel, "merge_blockers", lambda *args: [])
    monkeypatch.setattr(
        funnel,
        "_gh_json",
        lambda *args: (
            {"headRefName": "ticket/{}".format(ticket.number)}
            if "pr" in args
            else {"state": "CLOSED"}
        ),
    )
    monkeypatch.setattr(
        funnel,
        "_run_gh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )
    monkeypatch.setattr(funnel, "_auto_close_parent", lambda *args: False)

    assert funnel.cmd_merge([project, ticket], NOW, ticket.repo, 70, True) == 0
    assert writes == [(ticket.ref, "")]
    assert ticket.in_motion_since is None


def test_begin_offers_a_ticket_whose_refreshed_claim_is_over_60_seconds_old(
    monkeypatch, capsys
):
    project, ticket = _ticket(12, 11)

    result, writes = _implementing_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        current_claims={
            ticket.ref: (
                NOW
                - funnel.BEGIN_CLAIM_COLLISION_WINDOW
                - timedelta(seconds=1)
            ),
        },
    )

    assert result["work"]["ref"] == ticket.ref
    assert [ref for ref, value in writes if value] == [ticket.ref]


def test_read_lock_reads_the_selected_project_item(monkeypatch):
    project, ticket = _ticket(14, 13)
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        return {"node": {"lock": {"text": "2026-09-05T12:00:00Z"}}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    assert funnel.read_lock(ticket) == NOW
    assert calls == [(funnel.ITEM_LOCK_QUERY, {"item": ticket.item_id})]


def test_codex_begin_skips_the_other_tier_before_claiming(monkeypatch, capsys):
    standard_project, standard = _ticket(8, 9, body="Risk: standard")
    escalated_project, escalated = _ticket(
        10, 11, body="Risk: escalated — concurrency"
    )
    result, writes = _implementing_begin(
        monkeypatch, capsys,
        [escalated_project, escalated, standard_project, standard],
        tier="standard",
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == standard.ref
    assert [ref for ref, value in writes if value] == [standard.ref]


def test_muse_escalated_begin_no_longer_claims_a_ticket(monkeypatch, capsys):
    """Muse judges and Codex implements (Nate, 2026-09-22, #1315, #1322):
    an escalated Muse begin with no role takes the review path."""
    project, ticket = _ticket(
        12, 13, body="Risk: escalated — concurrency"
    )

    result, writes = _implementing_begin(
        monkeypatch, capsys, [project, ticket], agent="muse", tier="escalated"
    )

    assert result["do"] != "ticket"
    assert [ref for ref, value in writes if value] == []


def test_muse_back_on_the_roster_claims_a_ticket_again(monkeypatch, capsys):
    """The reversal path: the roster is the switch."""
    monkeypatch.setitem(funnel.AGENTS_BY_ROLE["implement"], "muse",
                        frozenset(funnel.TIERS))
    project, ticket = _ticket(
        12, 13, body="Risk: escalated — concurrency"
    )

    result, writes = _implementing_begin(
        monkeypatch, capsys, [project, ticket], agent="muse", tier="escalated",
        caller_role="implement",
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert [ref for ref, value in writes if value] == [ticket.ref]


@pytest.mark.parametrize("tier", ["standard", "escalated"])
def test_an_implement_caller_off_the_roster_is_refused(monkeypatch, capsys,
                                                        tier):
    """`--role implement` used to route by the declaration alone, so taking
    Muse off the roster would not have stopped `scripts/muse-implement`
    (#1322). The refusal comes before any claim."""
    project, ticket = _ticket(
        14, 15, body="Risk: escalated — concurrency"
    )

    result, writes = _implementing_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        agent="muse",
        tier=tier,
        caller_role="implement",
    )

    assert result["do"] == "stop"
    assert result["gate"] == "role"
    assert "muse does not implement {} work".format(tier) in result["why"]
    assert writes == []


def test_the_role_refusal_comes_before_the_project_read(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent: "run-id")
    monkeypatch.setattr(usage, "read_agent", lambda *args: {"windows": {}})
    monkeypatch.setattr(usage, "pace", lambda *args, **kwargs: {
        "over_pace": False})
    monkeypatch.setattr(funnel, "load_items", lambda *args, **kwargs:
                        pytest.fail("a refused implementer reads no Project"))

    assert funnel.main(["begin", "--agent", "muse", "--tier", "standard",
                        "--role", "implement"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["gate"] == "role"


def test_codex_implements_both_tiers_and_muse_reviews():
    assert funnel.AGENTS_BY_ROLE["implement"] == {
        "codex": frozenset(funnel.TIERS),
        "claude": frozenset(funnel.TIERS + (None,)),
    }
    assert funnel._begin_role_refusal("codex", "standard", "implement") is None
    assert funnel._begin_role_refusal("codex", "escalated", "implement") is None
    assert funnel._begin_role_refusal("muse", "standard", "review") is None
    assert funnel._begin_role_refusal("muse", "escalated", None) is None


def test_codex_begin_respects_the_wip_limit(monkeypatch, capsys):
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            20 + index * 2, 21 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(40, 41)
    items.extend((project, ticket))

    result, writes = _implementing_begin(monkeypatch, capsys, items)

    assert result["do"] == "stop"
    assert "lock held" in result["why"]
    assert writes == []


def _capture_events(monkeypatch):
    import heartbeat

    events = []
    monkeypatch.setattr(
        heartbeat, "record_event",
        lambda agent, run, outcome, **fields: events.append(
            (agent, run, outcome, fields)))
    return events


def test_begin_records_an_empty_queue_itself(monkeypatch, capsys):
    """The routine files many stops as `nothing-to-do`; only this record
    says the queue was genuinely empty (#1320)."""
    events = _capture_events(monkeypatch)

    result, writes = _implementing_begin(monkeypatch, capsys, [])

    assert result["do"] == "stop"
    assert result["queue"] == "empty"
    assert "no standard work waiting" in result["why"]
    assert events == [("codex", "run-id", "nothing-to-do",
                       {"queue": "empty", "tier": "standard"})]
    assert writes == []


def test_backed_off_work_is_not_an_empty_queue(monkeypatch, capsys):
    """A ticket held back after repeated failures is work nobody has
    implemented. Near a cleared backlog the remainder is mostly failing
    work, and calling it empty would read "cleared" while the lane is
    stuck."""
    events = _capture_events(monkeypatch)
    project, ticket = _ticket(40, 41)
    monkeypatch.setattr(funnel, "_backed_off_work", lambda items, now: {
        ticket.ref: {"ref": ticket.ref, "failures": 3,
                     "until": NOW + timedelta(hours=6),
                     "reason": "errored three times"}})

    result, _ = _implementing_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "stop"
    assert result["backed_off"][0]["ref"] == ticket.ref
    assert "queue" not in result
    assert not [event for event in events
                if event[3].get("queue") == "empty"]


def test_a_claim_below_the_cap_does_not_hide_an_empty_queue(
        monkeypatch, capsys):
    """One run in flight is not a brake while the lane has room for more:
    with nothing else startable, the queue is empty."""
    events = _capture_events(monkeypatch)
    project, ticket = _ticket(20, 21, in_motion_since=NOW)

    result, _ = _implementing_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "stop"
    assert result["queue"] == "empty"
    assert [event[2] for event in events
            if event[3].get("queue") == "empty"] == ["nothing-to-do"]


def test_a_held_lock_is_not_recorded_as_an_empty_queue(monkeypatch, capsys):
    """At the WIP limit there is work; the lane just may not take it."""
    events = _capture_events(monkeypatch)
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            20 + index * 2, 21 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(40, 41)
    items.extend((project, ticket))

    result, _ = _implementing_begin(monkeypatch, capsys, items)

    assert "lock held" in result["why"]
    assert "queue" not in result
    assert not [event for event in events
                if event[3].get("queue") == "empty"]


def test_codex_begin_allows_broken_preemption_at_the_wip_limit(
    monkeypatch, capsys
):
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            50 + index * 2, 51 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(70, 71, klass="Broken")
    items.extend((project, ticket))

    result, writes = _implementing_begin(monkeypatch, capsys, items)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert [ref for ref, value in writes if value] == [ticket.ref]


def test_codex_begin_takes_over_the_five_branchless_claims(monkeypatch, capsys):
    items = []
    claims = []
    for index, number in enumerate((221, 214, 223, 224, 301)):
        project, claimed_ticket = _ticket(
            number,
            500 + index,
            in_motion_since=NOW - timedelta(minutes=31 + index),
        )
        items.extend((project, claimed_ticket))
        claims.append(claimed_ticket)
    facts = {item.ref: None for item in claims}

    result, writes = _implementing_begin(
        monkeypatch, capsys, items, pr_facts=facts
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] in facts
    assert {ref for ref, value in writes if not value} == (
        set(facts) - {result["work"]["ref"]}
    )
    assert [ref for ref, value in writes if value] == [result["work"]["ref"]]


def test_codex_begin_reports_reconciled_abandoned_claims(
    monkeypatch, capsys
):
    project, ticket = _ticket(302, 501)
    reconciled = [{
        "run": "abandoned",
        "agent": "codex",
        "ref": ticket.ref,
        "result": "released",
    }]
    calls = []
    monkeypatch.setattr(
        funnel,
        "reconcile_abandoned_claims",
        lambda items, now, pr_facts: (
            calls.append((items, now, pr_facts)) or reconciled
        ),
    )
    facts = {ticket.ref: None}

    result, _ = _implementing_begin(
        monkeypatch, capsys, [project, ticket], pr_facts=facts
    )

    assert result["reconciled_claims"] == reconciled
    assert calls == [([project, ticket], NOW, facts)]


def test_codex_begin_reports_repo_readiness_when_work_is_withheld(
    monkeypatch, capsys
):
    project, ticket = _ticket(72, 73)
    readiness = {
        ticket.repo: funnel.MemberRepoReadiness(
            ticket.repo, topic=True, ci_workflow=False,
            stock_labels=(), dependabot=False,
        ),
    }

    result, writes = _implementing_begin(
        monkeypatch, capsys, [project, ticket],
        repo_readiness=readiness,
    )

    assert result["do"] == "stop"
    assert "no CI workflow" in result["why"]
    assert result["withheld"] == [{
        "ref": ticket.ref,
        "repo": ticket.repo,
        "reasons": ["no CI workflow"],
    }]
    assert writes == []


@pytest.mark.parametrize("agent", sorted(funnel.AGENTS_BY_ROLE["implement"]))
def test_main_supplies_repo_readiness_to_an_implementing_begin_path(
    monkeypatch, agent
):
    _allow_begin(monkeypatch)
    # Claude's lane is closed outside Saturday morning (#1557).
    monkeypatch.setattr(funnel, "_local_time", lambda now: SATURDAY_0500)
    project, ticket = _ticket(74, 75)
    rows = [project, ticket]
    readiness = {
        ticket.repo: funnel.MemberRepoReadiness(
            ticket.repo, topic=True, ci_workflow=True,
            stock_labels=(), dependabot=True,
        ),
    }
    received = []
    monkeypatch.setattr(
        funnel, "load_items", lambda include_details=True: rows
    )
    monkeypatch.setattr(
        funnel,
        "repo_readiness_for_items",
        lambda items: received.append(items) or readiness,
    )
    monkeypatch.setattr(
        funnel,
        "cmd_begin",
        lambda items, now, agent, tier, idle, breakdown=False,
        repo_readiness=None, caller_role=None, _detail_loader=None,
        _preflight=None, _pr_facts=None, _pr_facts_error=None,
        _pr_facts_elapsed=None, **kwargs: (
            received.append(repo_readiness) or 0
        ),
    )

    tier = "escalated" if agent == "muse" else "standard"
    assert funnel.main(["begin", "--agent", agent, "--tier", tier]) == 0
    assert received == [rows, readiness]


def test_main_runs_begin_reads_in_a_bounded_pool_and_merges_by_repo(
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor

    _allow_begin(monkeypatch)
    rows = []
    repos = ["owner/repo{}".format(index) for index in range(6)]
    for number, repo in enumerate(repos, start=80):
        project, ticket = _ticket(number, number + 100)
        project.repo = repo
        ticket.repo = repo
        rows.extend((project, ticket))

    monkeypatch.setattr(
        funnel, "load_items", lambda include_details=True: rows
    )
    branch_started = threading.Event()
    faster_repo_finished = threading.Event()
    completion_order = []
    received = []
    pool_sizes = []

    def readiness_for_repo(repo):
        assert branch_started.wait(timeout=2)
        if repo == repos[0]:
            assert faster_repo_finished.wait(timeout=2)
        result = funnel.MemberRepoReadiness(
            repo, topic=True, ci_workflow=True,
            stock_labels=(), dependabot=True,
        )
        completion_order.append(repo)
        if repo == repos[1]:
            faster_repo_finished.set()
        return result

    def branch_facts(items):
        assert items is rows
        branch_started.set()
        return {"snapshot": "ready"}

    def tracking_pool(*, max_workers):
        pool_sizes.append(max_workers)
        return RealThreadPoolExecutor(max_workers=max_workers)

    def capture_begin(
        items, now, agent, tier, idle, breakdown=False,
        repo_readiness=None, caller_role=None, **kwargs
    ):
        received.append((repo_readiness, kwargs))
        return 0

    monkeypatch.setattr(funnel, "_begin_repo_readiness", readiness_for_repo)
    monkeypatch.setattr(funnel, "ticket_pr_facts", branch_facts)
    monkeypatch.setattr(funnel, "ThreadPoolExecutor", tracking_pool)
    monkeypatch.setattr(funnel, "cmd_begin", capture_begin)

    assert funnel.main(["begin", "--agent", "codex", "--tier", "standard"]) == 0

    assert pool_sizes == [funnel.BEGIN_FETCH_POOL_SIZE]
    assert completion_order.index(repos[1]) < completion_order.index(repos[0])
    readiness, kwargs = received[0]
    assert list(readiness) == repos
    assert list(readiness) == sorted(readiness)
    assert kwargs["_pr_facts"] == {"snapshot": "ready"}
    assert kwargs["_pr_facts_error"] is None
    assert kwargs["_pr_facts_elapsed"] >= 0


def test_begin_stop_reason_omits_breakdown_when_it_was_not_requested(
    monkeypatch, capsys
):
    calls = []

    def unexpected_breakdown_lookup(items):
        calls.append(items)
        raise AssertionError("breakdown queue was consulted without --breakdown")

    monkeypatch.setattr(funnel, "awaiting_breakdown", unexpected_breakdown_lookup)

    result = _begin(monkeypatch, capsys, breakdown=False)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review"
    assert "break down" not in result["why"]
    assert calls == []


def test_begin_stop_reason_names_both_empty_queues_when_both_were_consulted(
    monkeypatch, capsys
):
    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"


def test_begin_keeps_review_first_against_same_class_later_jobs(monkeypatch, capsys):
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    pending = SimpleNamespace(
        ref="nateprich-projects/command-center#20",
        url="https://github.com/nateprich-projects/command-center/issues/20",
        title="Break down project",
        repo="nateprich-projects/command-center",
        number=20,
        klass="Improve",
    )
    idea = SimpleNamespace(klass="Improve")
    breakdown_calls = []
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    monkeypatch.setattr(
        funnel,
        "awaiting_breakdown",
        lambda items: breakdown_calls.append(items) or [pending],
    )
    monkeypatch.setattr(
        funnel, "shapeable_idea", lambda items, tier, reading: idea
    )

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "review"
    assert result["work"] == work
    assert breakdown_calls == [[]]


def test_begin_offers_shape_when_needs_decision_blocks_breakdown(monkeypatch, capsys):
    blocked = funnel.Item(
        repo="nateprich/example",
        number=38,
        title="Blocked breakdown",
        url="https://github.com/nateprich/example/issues/38",
        state="OPEN",
        status="Ready",
        labels=["blocked"],
        needs_decision="Where should this connector live?",
    )
    idea = funnel.Item(
        repo="nateprich/example",
        number=39,
        title="A standard idea",
        url="https://github.com/nateprich/example/issues/39",
        state="OPEN",
        status="Ideas",
        klass="New",
        origin="agent",
        risk="standard",
        needs="none",
        labels=["needs-shaping"],
        body="Risk: standard",
    )

    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [])
    monkeypatch.setattr(funnel, "ideas", lambda items: [idea])
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    assert funnel.cmd_begin(
        [blocked, idea], NOW, "zcode", "standard", False, True
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "shape"
    assert result["work"]["ref"] == idea.ref


def _backoff_row(ref):
    until = NOW + funnel.BACKOFF_COOLDOWN
    return {"ref": ref, "failures": 11, "until": until,
            "reason": "backoff: 11 consecutive failed runs"}


def _shape_idea(number, risk="escalated"):
    return funnel.Item(
        repo="nateprich/example",
        number=number,
        title="Idea {}".format(number),
        url="https://github.com/nateprich/example/issues/{}".format(number),
        state="OPEN",
        status="Ideas",
        klass="Broken",
        origin="agent",
        risk=risk,
        needs="none",
        labels=["needs-shaping"],
        body="Risk: {}".format(risk),
    )


def test_review_lane_skips_a_backed_off_shape_and_says_so(monkeypatch, capsys):
    """#1581: #1195's shape failed eleven times in a row because the review
    lane never read the backoff the ticket path honours."""
    stuck = _shape_idea(1195)
    next_idea = _shape_idea(1196)

    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [])
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)
    monkeypatch.setattr(funnel, "_backed_off_work",
                        lambda items, now: {stuck.ref: _backoff_row(stuck.ref)})
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])

    assert funnel.cmd_begin(
        [stuck, next_idea], NOW, "muse", "escalated", False, True
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "shape"
    assert result["work"]["ref"] == next_idea.ref
    assert [row["ref"] for row in result["backed_off"]] == [stuck.ref]
    assert result["backed_off"][0]["failures"] == 11


def test_review_lane_skips_a_backed_off_breakdown(monkeypatch, capsys):
    project = funnel.Item(
        repo="nateprich/example",
        number=40,
        title="A Ready plan",
        url="https://github.com/nateprich/example/issues/40",
        state="OPEN",
        status="Ready",
        klass="Broken",
    )
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [])
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda items: [project])
    monkeypatch.setattr(funnel, "shapeable_idea", lambda items, tier, reading: None)
    monkeypatch.setattr(funnel, "_backed_off_work",
                        lambda items, now: {project.ref: _backoff_row(project.ref)})
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])

    assert funnel.cmd_begin(
        [project], NOW, "zcode", "standard", False, True
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "stop"
    assert [row["ref"] for row in result["backed_off"]] == [project.ref]


def test_review_lane_skips_the_backoff_read_with_no_issue_job(monkeypatch, capsys):
    """The heartbeat read costs seconds of the reply budget; a fire with no
    breakdown or shape candidate must not pay for it."""
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda items: [])

    def unexpected(*args, **kwargs):
        raise AssertionError("backoff read with nothing to filter")

    monkeypatch.setattr(funnel, "_backed_off_work", unexpected)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "review"


def test_breakdown_work_carries_plan_access_signals(monkeypatch, capsys):
    item = SimpleNamespace(
        ref="nateprich-projects/command-center#25",
        repo="nateprich-projects/command-center",
        number=25,
        url="https://github.com/nateprich-projects/command-center/issues/25",
        title="Reach the funnel from general chat",
    )
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda items: [item])
    monkeypatch.setattr(
        funnel,
        "_ticket_body",
        lambda repo, number: "Cloudflare Tunnel and a fine-grained token",
    )

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "breakdown"
    assert result["work"]["access_signals"] == ["token", "tunnel"]


def _idea(number, title, body, klass=None, labels=None):
    if labels is None:
        labels = ["needs-shaping"]
    return SimpleNamespace(
        ref="nateprich-projects/command-center#{}".format(number),
        repo="nateprich-projects/command-center",
        number=number,
        url="https://github.com/nateprich-projects/command-center/issues/{}".format(number),
        title=title,
        body=body,
        origin="agent",
        risk=("escalated" if "Risk: escalated" in body else "standard"),
        needs="none",
        klass=klass,
        labels=labels,
    )


def _review_job(ticket, pr=7):
    return {"pr": pr, "repo": ticket.repo, "ref": ticket.ref}


def _reviewer_begin(
    monkeypatch,
    capsys,
    items,
    *,
    review=None,
    breakdown_item=None,
    idea=None,
    breakdown=False,
    reviews=None,
    breakdown_items=None,
    band=None,
):
    """``reviews`` and ``breakdown_items`` give a whole queue, oldest first;
    ``review`` and ``breakdown_item`` remain the one-entry shorthand.
    ``band`` makes the usage gate report that #1198 band."""
    if reviews is None:
        reviews = [review] if review is not None else []
    if breakdown_items is None:
        breakdown_items = (
            [breakdown_item] if breakdown_item is not None else [])
    _allow_begin(monkeypatch)
    if band is not None:
        _tight_budget(monkeypatch, band)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(
        funnel,
        "review_queue",
        lambda rows, tier: list(reviews),
    )
    monkeypatch.setattr(
        funnel,
        "awaiting_breakdown",
        lambda rows: list(breakdown_items),
    )
    monkeypatch.setattr(
        funnel,
        "shapeable_idea",
        lambda rows, tier, reading: idea,
    )
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: "")

    assert funnel.cmd_begin(
        items, NOW, "zcode", "standard", False, breakdown
    ) == 0
    return json.loads(capsys.readouterr().out)


def test_broken_idea_preempts_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(101, 100, klass="Improve")
    idea = _idea(102, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "shape"
    assert result["work"]["ref"] == idea.ref


def test_improve_idea_does_not_preempt_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(103, 100, klass="Improve")
    idea = _idea(104, "Improve idea", "Risk: standard", klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_muse_escalated_begin_uses_the_explicit_reviewer_role(
    monkeypatch, capsys
):
    project, ticket = _ticket(
        113, 112, body="Risk: escalated — concurrency"
    )
    review = _review_job(ticket, pr=114)

    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(funnel, "review_queue", lambda rows, tier: [review])

    assert funnel.cmd_begin(
        [project, ticket],
        NOW,
        "muse",
        "escalated",
        False,
        caller_role="review",
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "review"
    assert result["work"] == review


def _recorded_conflict_fixture(verdict=None):
    recorded = json.loads(
        (ROOT / "tests/fixtures/conflict_begin_stdout_pr1501.json").read_text()
    )["pr"]
    if verdict is not None:
        recorded["verdict"] = verdict
    return recorded


def _recorded_conflict_ticket():
    repo = "nateprich-projects/command-center"
    return funnel.Item(
        repo=repo,
        number=703,
        title="Recorded conflicting ticket",
        url="https://github.com/{}/issues/703".format(repo),
        state="OPEN",
        status="Building",
        risk="standard",
        needs="none",
        parent=repo + "#1503",
    )


def test_begin_keeps_recorded_conflict_precheck_off_json_stdout(
    monkeypatch, capsys
):
    """The #1501 conflict precheck writes its confirmation before begin JSON."""
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda rows: [])
    monkeypatch.setattr(funnel, "shapeable_idea", lambda *args: None)
    monkeypatch.setattr(
        funnel,
        "_run_gh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )
    ticket = _recorded_conflict_ticket()
    fact = _recorded_conflict_fixture()
    facts = funnel.TicketPRFacts(rows_by_ref={ticket.ref: [fact]})

    assert funnel.cmd_begin(
        [ticket], NOW, "zcode", "standard", False,
        repo_readiness={}, caller_role="review", _pr_facts=facts,
    ) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["do"] == "stop"
    assert "recorded rejected on PR #1501 against 6389cc6961da" in captured.err


def test_begin_merge_reconcile_keeps_refusal_confirmation_off_json_stdout(
    monkeypatch, capsys
):
    """A refused merge in begin's reconcile still emits parseable JSON."""
    _allow_begin(monkeypatch)
    ticket = _recorded_conflict_ticket()
    fact = _recorded_conflict_fixture({
        "verdict": "approved",
        "ci": "green",
        "head_sha": "6389cc6961da",
        "blocking": [],
    })
    facts = funnel.TicketPRFacts(rows_by_ref={ticket.ref: [fact]})
    blocker = "branch 'ticket/703'" + funnel.CONFLICTING_BRANCH_SUFFIX
    monkeypatch.setattr(
        funnel, "merge_blockers",
        lambda repo, pr, items, now, pr_fact=None: [blocker],
    )
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda rows: [])
    monkeypatch.setattr(funnel, "review_queue", lambda rows, tier: [])
    monkeypatch.setattr(funnel, "shapeable_idea", lambda *args: None)
    monkeypatch.setattr(
        funnel,
        "_run_gh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="", stderr=""
        ),
    )

    assert funnel.cmd_begin(
        [ticket], NOW, "zcode", "standard", False,
        repo_readiness={}, caller_role="review", _pr_facts=facts,
    ) == 0

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["do"] == "stop"
    assert result["reconciled_merges"][0]["result"] == "refused"
    assert "recorded rejected on PR #1501 against 6389cc6961da" in captured.err


def test_broken_review_is_before_a_broken_idea(monkeypatch, capsys):
    project, ticket = _ticket(105, 100, klass="Broken")
    idea = _idea(106, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_unclassed_idea_does_not_preempt_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(107, 100, klass="Improve")
    idea = _idea(108, "Unclassed idea", "Risk: standard")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_same_class_candidates_keep_bottom_up_order(monkeypatch, capsys):
    project, ticket = _ticket(109, 100, klass="Improve")
    pending = funnel.Item(
        repo="nateprich/example",
        number=110,
        title="Project 110",
        url="https://github.com/nateprich/example/issues/110",
        state="OPEN",
        status="Ready",
        klass="Improve",
        children_total=0,
    )
    idea = _idea(111, "Improve idea", "Risk: standard", klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        breakdown_item=pending,
        idea=idea,
        breakdown=True,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def _ready_plan(number, klass):
    return funnel.Item(
        repo="nateprich/example",
        number=number,
        title="Project {}".format(number),
        url="https://github.com/nateprich/example/issues/{}".format(number),
        state="OPEN",
        status="Ready",
        klass=klass,
        children_total=0,
    )


def test_a_broken_review_is_not_hidden_behind_an_older_new_class_pr(
    monkeypatch, capsys
):
    """#1222: the review queue is oldest first, and its head used to speak for
    the whole queue. On 2026-09-21 one old New-class PR ranked below every
    Broken shape, and seven Broken reviews waited behind it for five hours."""
    new_project, new_ticket = _ticket(121, 120, klass="New")
    broken_project, broken_ticket = _ticket(123, 122, klass="Broken")
    idea = _idea(124, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [new_project, new_ticket, broken_project, broken_ticket],
        reviews=[_review_job(new_ticket, pr=7), _review_job(broken_ticket, pr=9)],
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(broken_ticket, pr=9)


def test_the_oldest_review_still_leads_when_nothing_waiting_preempts(
    monkeypatch, capsys
):
    older_project, older_ticket = _ticket(126, 125, klass="New")
    newer_project, newer_ticket = _ticket(128, 127, klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [older_project, older_ticket, newer_project, newer_ticket],
        reviews=[_review_job(older_ticket, pr=7), _review_job(newer_ticket, pr=9)],
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(older_ticket, pr=7)


def test_a_lone_new_class_review_still_yields_to_a_broken_idea(
    monkeypatch, capsys
):
    """The cross-stage key is unchanged: only which entry a queue puts
    forward moved. With no preempting review waiting, Broken shaping wins."""
    project, ticket = _ticket(130, 129, klass="New")
    idea = _idea(131, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        reviews=[_review_job(ticket)],
        idea=idea,
    )

    assert result["do"] == "shape"
    assert result["work"]["ref"] == idea.ref


def test_a_broken_breakdown_is_not_hidden_behind_an_older_improve_plan(
    monkeypatch, capsys
):
    """#1222, the breakdown half: `awaiting_breakdown` is oldest first too."""
    improve_plan = _ready_plan(132, "Improve")
    broken_plan = _ready_plan(133, "Broken")
    idea = _idea(134, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [improve_plan, broken_plan],
        breakdown_items=[improve_plan, broken_plan],
        idea=idea,
        breakdown=True,
    )

    assert result["do"] == "breakdown"
    assert result["work"]["ref"] == broken_plan.ref


def _tight_budget(monkeypatch, band="tight"):
    """Make the usage reader report a #1198 band with its numbers.

    The real `_begin_preflight` runs, because the stop it makes on `tight`
    (#1269) is the thing under test; only the reading and its verdict are
    stubbed. Applied after `_allow_begin`, which installs a bandless verdict,
    so the helpers take `band=` rather than calling this first."""
    window = {"used_percent": 62.0, "projected_percent": 118.0,
              "daily_rate_dollars": 31.0,
              "runs_out_at": NOW.timestamp() + 2 * 86400,
              "resets_at": NOW.timestamp() + 2 * 86400, "rolling": True}
    monkeypatch.setattr(
        usage, "read_agent", lambda agent, now: {"windows": {"seven_day": window}})
    monkeypatch.setattr(
        usage, "pace", lambda reading, now, provider=None: {
            "windows": [dict(window, window="seven_day", band=band, over=False)],
            "over_pace": False, "band": band, "known": True})


def test_a_tight_budget_stops_every_lane_before_reading_the_project(
    monkeypatch, capsys
):
    """#1269 (Nate, 2026-09-21): tight stops everything, like over. The
    ladder decides what goes first when the brake lifts, not what runs under
    it. #1199 had let Broken, Maintenance and pinned work through, and on a
    board that is mostly Broken that was no brake at all."""
    broken_project, broken_ticket = _ticket(141, 140, klass="Broken")
    pinned_project, pinned_ticket = _ticket(143, 142, klass="Replace")
    pinned_project.pinned = True
    maintenance_plan = _ready_plan(144, "Maintenance")
    idea = _idea(145, "Broken idea", "Risk: standard", klass="Broken")
    items = [broken_project, broken_ticket, pinned_project, pinned_ticket,
             maintenance_plan]

    result = _reviewer_begin(
        monkeypatch, capsys, items,
        reviews=[_review_job(broken_ticket, pr=7), _review_job(pinned_ticket, pr=9)],
        breakdown_items=[maintenance_plan], idea=idea, breakdown=True,
        band="tight",
    )
    assert result["do"] == "stop"
    assert result["gate"] == "tight"
    assert result["budget_band"] == "tight"
    for fragment in ("62% used", "projected 118%", "$31/day", "runs out",
                     "every lane waits"):
        assert fragment in result["why"], result["why"]
    assert "work" not in result

    result, writes = _implementing_begin(
        monkeypatch, capsys, items, band="tight")
    assert result["do"] == "stop"
    assert result["gate"] == "tight"
    assert writes == []
    assert "reconciled_claims" not in result and "held" not in result


def test_a_tight_budget_stops_an_empty_funnel_too(monkeypatch, capsys):
    """The stop is the budget's, not the queue's: it reads the same with
    nothing waiting, and it is recorded as a hold rather than nothing-to-do."""
    result = _reviewer_begin(monkeypatch, capsys, [], band="tight")
    assert result["do"] == "stop"
    assert result["gate"] == "tight"


def test_an_ok_band_offers_everything_as_before(monkeypatch, capsys):
    """Under `ok` nothing is held: a New project's PR review, an Improve
    ticket and an Improve idea are all offered exactly as without a band."""
    project, ticket = _ticket(185, 184, klass="New")
    result = _reviewer_begin(
        monkeypatch, capsys, [project, ticket],
        reviews=[_review_job(ticket)],
        idea=_idea(186, "Improve idea", "Risk: standard", klass="Improve"),
        band="ok",
    )
    assert result["budget_band"] == "ok"
    assert result["do"] == "review"
    assert "budget_held" not in result

    result = _reviewer_begin(
        monkeypatch, capsys, [],
        idea=_idea(187, "Improve idea", "Risk: standard", klass="Improve"),
        band="ok",
    )
    assert result["do"] == "shape"

    improve_project, improve_ticket = _ticket(189, 188, klass="Improve")
    result, _ = _implementing_begin(
        monkeypatch, capsys, [improve_project, improve_ticket], band="ok")
    assert result["do"] == "ticket"
    assert result["work"]["ref"] == improve_ticket.ref


def test_shape_is_not_offered_when_the_first_idea_is_the_other_tier(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: [_idea(31, "Risky idea", "Risk: escalated")],
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"


def test_shape_is_not_offered_without_headroom(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: calls.append(items) or [_idea(32, "An idea", "Risk: standard")],
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: False)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"
    assert calls == []


def test_shape_offers_only_the_first_idea_matching_the_run_tier(
    monkeypatch, capsys
):
    candidates = [
        _idea(33, "Escalated first", "Risk: escalated"),
        _idea(34, "Standard first", "Risk: standard"),
        _idea(35, "Standard second", "Risk: standard"),
    ]
    calls = []
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: calls.append(items) or candidates,
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "shape"
    assert result["work"] == {
        "ref": candidates[1].ref,
        "url": candidates[1].url,
        "title": candidates[1].title,
    }
    assert len(calls) == 1


@pytest.mark.parametrize(
    "state", ["review", "idea", "both", "breakdown", "empty"]
)
def test_escalated_reviewer_is_offered_shape_after_review(
    monkeypatch, capsys, state
):
    """#1135 reverses #1026: the escalated schedule shapes escalated ideas
    while the Claude routine is off, after review. Run d9de03bb1229's
    review-only suppression no longer applies; a Broken idea still never
    preempts a waiting review there."""
    project, ticket = _ticket(120, 121, body="Risk: escalated")
    review = _review_job(ticket, pr=122)
    pending = funnel.Item(
        repo="nateprich/example",
        number=123,
        title="Project 123",
        url="https://github.com/nateprich/example/issues/123",
        state="OPEN",
        status="Ready",
        klass="Broken",
        children_total=0,
    )
    idea = _idea(124, "Escalated idea", "Risk: escalated", klass="Broken")

    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(
        funnel, "review_queue",
        lambda rows, tier: [review] if state in ("review", "both") else [])
    monkeypatch.setattr(
        funnel, "awaiting_breakdown",
        lambda rows: [pending] if state == "breakdown" else [])
    monkeypatch.setattr(
        funnel, "ideas",
        lambda rows: [idea] if state in ("idea", "both") else [])
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: "")

    assert funnel.cmd_begin(
        [project, ticket], NOW, "muse", "escalated", False,
        caller_role="review",
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] != "breakdown"
    if state in ("review", "both"):
        assert result["do"] == "review"
        assert result["work"] == review
    elif state == "idea":
        assert result["do"] == "shape"
        assert result["work"]["ref"] == idea.ref
    else:
        assert result["do"] == "stop"


def test_standard_reviewer_is_still_offered_a_standard_idea(monkeypatch):
    idea = _idea(125, "Standard idea", "Risk: standard")
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)
    monkeypatch.setattr(funnel, "ideas", lambda items: [idea])

    assert funnel.shapeable_idea([], "standard", {}) is idea
    assert funnel.shapeable_idea([], "escalated", {}) is None


def test_escalated_reviewer_is_offered_an_escalated_idea(monkeypatch):
    """#1135 reverses #1026's escalated suppression in `shapeable_idea`."""
    idea = _idea(126, "Escalated idea", "Risk: escalated")
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)
    monkeypatch.setattr(funnel, "ideas", lambda items: [idea])

    assert funnel.shapeable_idea([], "escalated", {}) is idea
    assert funnel.shapeable_idea([], "standard", {}) is None


def test_shapeable_idea_requires_the_needs_shaping_label(monkeypatch):
    unflagged = _idea(
        38, "Unflagged idea", "Risk: standard", labels=[]
    )
    flagged = _idea(
        39, "Flagged idea", "Risk: standard", labels=["needs-shaping"]
    )

    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)
    monkeypatch.setattr(funnel, "ideas", lambda items: [unflagged, flagged])

    assert funnel.shapeable_idea([], "standard", {}) is flagged

    monkeypatch.setattr(funnel, "ideas", lambda items: [unflagged])
    assert funnel.shapeable_idea([], "standard", {}) is None


def test_muse_standard_schedule_is_offered_a_standard_idea_on_a_metered_reading(
    monkeypatch, capsys
):
    """Muse's standard schedule may shape while its metered pool has headroom."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"windows": {
            "five_hour": {"used_percent": 0.0},
            "seven_day": {"used_percent": 0.0, "rolling": True,
                           "resets_at": 1},
        }},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(36, "A standard idea", "Risk: standard")],
    )

    assert funnel.cmd_begin([], NOW, "muse", "standard", False, True) == 0
    result = json.loads(capsys.readouterr().out)

    assert "unmetered" not in result
    assert result["do"] == "shape"
    assert result["work"]["ref"] == "nateprich-projects/command-center#36"


def test_muse_escalated_schedule_is_never_offered_a_standard_idea(
    monkeypatch, capsys
):
    """#1135: the escalated schedule shapes escalated ideas, never standard
    ones. Tier isolation holds in both directions."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"windows": {
            "five_hour": {"used_percent": 0.0},
            "seven_day": {"used_percent": 0.0, "rolling": True,
                           "resets_at": 1},
        }},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(37, "A standard idea", "Risk: standard")],
    )

    assert funnel.cmd_begin(
        [], NOW, "muse", "escalated", False, False, caller_role="review"
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "stop"


def test_muse_escalated_schedule_is_offered_an_escalated_idea_on_a_metered_reading(
    monkeypatch, capsys
):
    """#1135 reverses #1026: the hourly escalated schedule shapes an
    escalated idea while the Claude routine is off."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"windows": {
            "five_hour": {"used_percent": 0.0},
            "seven_day": {"used_percent": 0.0, "rolling": True,
                           "resets_at": 1},
        }},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(40, "An escalated idea", "Risk: escalated")],
    )

    assert funnel.cmd_begin(
        [], NOW, "muse", "escalated", False, False, caller_role="review"
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "shape"
    assert result["work"]["ref"] == "nateprich-projects/command-center#40"


def test_begin_binds_the_ticket_it_issues_to_the_run(monkeypatch, capsys):
    """The run id alone let a stale finish land on the wrong run (#497)."""
    import heartbeat

    bound = []
    monkeypatch.setattr(
        heartbeat, "record_binding",
        lambda agent, run, do, work, repo=None: bound.append((agent, run, do, work)) or "pushed",
    )
    project, ticket = _ticket(7, 6)
    result, writes = _implementing_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "ticket"
    assert result["bound"] == {"do": "ticket", "work": ticket.ref}
    assert bound == [("codex", "run-id", "ticket", ticket.ref)]


def test_a_stop_run_binds_nothing(monkeypatch, capsys):
    import heartbeat

    bound = []
    monkeypatch.setattr(
        heartbeat, "record_binding",
        lambda *args: bound.append(args) or "pushed",
    )
    result, writes = _implementing_begin(monkeypatch, capsys, [])

    assert result["do"] == "stop"
    assert "bound" not in result and bound == []


def _selecting_reconcile_begin(monkeypatch, capsys, items, rows, verdicts,
                               merge):
    """Begin with a live merge reconcile and live ticket selection."""
    _allow_begin(monkeypatch)

    def facts(_items):
        by_ref = {}
        for raw in rows:
            row = dict(raw)
            row.setdefault("state", "OPEN")
            row["verdict"] = verdicts.get(row.get("number"))
            ref = funnel.ticket_ref_from_branch(
                row.get("repo", items[0].repo), row.get("headRefName") or ""
            )
            if ref:
                by_ref.setdefault(ref, []).append(row)
        return funnel.TicketPRFacts(
            {ref: values[0] for ref, values in by_ref.items()},
            rows_by_ref=by_ref,
        )

    monkeypatch.setattr(funnel, "ticket_pr_facts", facts)
    monkeypatch.setattr(funnel, "cmd_merge", merge)
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    bodies = {item.number: item.body for item in items}
    monkeypatch.setattr(
        funnel, "_ticket_body", lambda repo, number: bodies.get(number) or ""
    )
    monkeypatch.setattr(
        funnel, "read_lock", lambda item: item.in_motion_since
    )
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )
    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    return json.loads(capsys.readouterr().out), writes


def test_begin_records_a_reconcile_listing_error_and_still_selects(
    monkeypatch, capsys
):
    """A reconcile step that cannot reach GitHub is recorded, not fatal.

    The listing failure below is #732's transient GraphQL shape: selection
    proceeds, the gate stays ok instead of claiming unknown state, and the
    raw stdout stays one valid JSON document.
    """
    project, ticket = _ticket(12, 11)
    error = funnel.GitHubError(
        "Something went wrong",
        transient=True,
        request_id="D707:172A39:4F2E95A:53DD225:6AA6668B",
    )

    result, writes = _implementing_begin(
        monkeypatch, capsys, [project, ticket],
        reconcile=lambda *args: (_ for _ in ()).throw(error),
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert result["gate"] == "ok"
    assert result["reconcile_errors"] == [{
        "step": "approved_merges",
        "error": "transient GraphQL response: Something went wrong "
                 "(GraphQL request ID D707:172A39:4F2E95A:53DD225:6AA6668B)",
        "transient": True,
    }]
    assert [ref for ref, value in writes if value] == [ticket.ref]


@pytest.mark.parametrize("func,step", [
    ("reconcile_auto_closeable_projects", "auto_closeable_projects"),
    ("reconcile_closed_items", "closed_items"),
    ("reconcile_orphaned_starts", "orphaned_starts"),
])
def test_begin_records_other_reconcile_step_errors_and_still_selects(
    monkeypatch, capsys, func, step
):
    """Every reconcile step is non-fatal, not just the merge retry."""
    project, ticket = _ticket(30, 29)
    monkeypatch.setattr(
        funnel,
        func,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            funnel.GitHubError("offline")
        ),
    )

    result, writes = _implementing_begin(
        monkeypatch, capsys, [project, ticket]
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert result["reconcile_errors"] == [{
        "step": step,
        "error": "offline",
        "transient": False,
    }]
    assert [ref for ref, value in writes if value] == [ticket.ref]


def test_begin_withholds_a_ticket_whose_merge_reconcile_errored(
    monkeypatch, capsys
):
    """#732: the merge may have succeeded remotely, so the open-looking
    ticket is unknown state and must not be claimed; selection serves the
    healthy ticket instead."""
    bad_project, bad = _ticket(20, 19)
    good_project, good = _ticket(22, 21)

    def merge(items, now, repo, pr, confirmed):
        raise funnel.GitHubError("Something went wrong")

    result, writes = _selecting_reconcile_begin(
        monkeypatch,
        capsys,
        [bad_project, bad, good_project, good],
        [{"number": 200, "headRefName": "ticket/20", "headRefOid": "head"}],
        {200: {"verdict": "approved", "head_sha": "head"}},
        merge,
    )

    assert result["reconciled_merges"] == [{
        "repo": bad.repo,
        "pr": 200,
        "ref": bad.ref,
        "result": "error",
        "error": "Something went wrong",
    }]
    assert result["do"] == "ticket"
    assert result["work"]["ref"] == good.ref
    assert [ref for ref, value in writes if value] == [good.ref]


def test_begin_stops_without_claiming_when_only_the_errored_ticket_remains(
    monkeypatch, capsys
):
    """Withholding the unknown-state ticket can leave nothing startable;
    that is a clean stop, not an abort and not a claim."""
    project, ticket = _ticket(24, 23)

    def merge(items, now, repo, pr, confirmed):
        raise funnel.GitHubError("Something went wrong")

    result, writes = _selecting_reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        [{"number": 240, "headRefName": "ticket/24", "headRefOid": "head"}],
        {240: {"verdict": "approved", "head_sha": "head"}},
        merge,
    )

    assert result["reconciled_merges"][0]["result"] == "error"
    assert result["do"] == "stop"
    assert writes == []


def test_begin_sources_never_name_brief():
    """#823: selection after reconcile never loads a reporting section."""
    for func in (funnel.cmd_begin, funnel.reconcile_approved_merges):
        assert re.search(r"\bbrief\b",
                         inspect.getsource(func), re.IGNORECASE) is None


def test_begin_completes_without_loading_any_reporting_section(
    monkeypatch, capsys
):
    """#823: reconcile, merge record and selection run while every
    reporting-section loader explodes. The PR snapshot stays available:
    selection reads it as gate state, not as a reporting section."""
    def explode(*args, **kwargs):
        raise AssertionError("begin loaded a reporting section")

    monkeypatch.setattr(funnel, "cmd_brief", explode)
    monkeypatch.setattr(funnel, "_brief_timed", explode)
    monkeypatch.setattr(funnel.BriefCache, "get_pr_facts", explode)
    monkeypatch.setattr(funnel, "unattended_approvals", explode)
    monkeypatch.setattr(funnel, "closed_itself_json", explode)
    monkeypatch.setattr(funnel, "maintenance_load", explode)

    merged_project, merged_ticket = _ticket(40, 39)
    project, ticket = _ticket(42, 41)
    result, writes = _selecting_reconcile_begin(
        monkeypatch,
        capsys,
        [merged_project, merged_ticket, project, ticket],
        [{"number": 400, "headRefName": "ticket/40", "headRefOid": "head"}],
        {400: {"verdict": "approved", "head_sha": "head"}},
        lambda items, now, repo, pr, confirmed: 0,
    )

    assert result["reconciled_merges"] == [{
        "repo": merged_ticket.repo,
        "pr": 400,
        "ref": merged_ticket.ref,
        "result": "merged",
    }]
    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert [ref for ref, value in writes if value] == [ticket.ref]


def _shaped_plan(number, *, status="Shaped", open_need=False,
                 escalated=False, labels=None):
    body = "# Plan\n\nProposed class: Broken\n\n"
    if escalated:
        body += "Risk: escalated — destructive\n\n"
    needs = {
        "Exposure": "Which credentials should be used?"
        if open_need else "nothing outstanding. No new credentials.",
        "Gates": "nothing outstanding. No gate changes.",
        "Scope and priority": "nothing outstanding. Scope is bounded.",
        "Preference": "nothing outstanding. No user-facing choice.",
    }
    body += "## Needs Nate\n\n{}\n".format("\n".join(
        "- {}: {}".format(category, answer)
        for category, answer in needs.items()
    ))
    body += "\n" + funnel.origin_block(
        "agent", at=NOW, run="shape-run", agent="claude"
    )
    return funnel.Item(
        repo="nateprich/example",
        number=number,
        title="Project {}".format(number),
        url="https://github.com/nateprich/example/issues/{}".format(number),
        state="OPEN",
        body=body,
        status=status,
        klass="Broken",
        origin="agent",
        risk="escalated" if escalated else "standard",
        needs="human" if open_need else "none",
        labels=labels or [],
        item_id="project-item-{}".format(number),
    )


def test_shape_lane_rechecks_stranded_self_approvals_before_new_ideas(
    monkeypatch, capsys
):
    stranded = _shaped_plan(300)
    open_question = _shaped_plan(301, open_need=True)
    escalated = _shaped_plan(302, escalated=True)
    blocked_question = _shaped_plan(303, labels=["blocked"])
    already_ready = _shaped_plan(304, status="Ready")
    writes = []
    comments = []

    def write_status(item, status, now):
        writes.append((item.ref, status))
        item.status = status
        return None

    def run_gh(argv, **kwargs):
        if argv[:3] == ["gh", "issue", "comment"]:
            comments.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "_write_status", write_status)
    monkeypatch.setattr(funnel, "_run_gh", run_gh)

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [stranded, open_question, escalated, blocked_question, already_ready],
    )

    assert writes == [(stranded.ref, "Ready")]
    assert stranded.status == "Ready"
    assert open_question.status == "Shaped"
    assert escalated.status == "Shaped"
    assert blocked_question.status == "Shaped"
    assert already_ready.status == "Ready"
    assert funnel.awaiting_decision([open_question]) == [open_question]
    assert result["shaped_self_approvals"] == [
        {"ref": stranded.ref, "status": "Ready"}
    ]
    assert len(comments) == 1
    marker = funnel.parse_self_approval(comments[0])
    assert marker is not None
    assert "needs_nate all null; class Broken self-approvable" in marker
    assert "no escalated risk" in marker


def test_plan_needs_nate_ignores_omitted_null_categories():
    body = (
        "## Needs Nate\n\n"
        "- Exposure: nothing outstanding. No new credentials.\n"
        "- Gates: nothing outstanding. No gate changes.\n"
        "- Preference: nothing outstanding. No user-facing choice.\n"
    )

    assert funnel.plan_needs_nate(body) is False


def test_plan_needs_nate_reads_only_the_visible_open_categories():
    assert funnel.plan_needs_nate("# Plan\n") is False
    assert funnel.plan_needs_nate(
        "## Needs Nate\n\n- Gates: Who may write Ready?\n"
    ) is True
    assert funnel.plan_needs_nate(
        "## Needs Nate\n\n"
        "- Gates: answered 2026-09-24T06:00:00Z by Nate. Agents may.\n"
    ) is False


# Claude's Saturday-morning implement lane (#1557).

SATURDAY_0500 = datetime(2026, 9, 26, 5, 0)
SATURDAY_1114 = datetime(2026, 9, 26, 11, 14)
SATURDAY_1115 = datetime(2026, 9, 26, 11, 15)
FRIDAY_2300 = datetime(2026, 9, 25, 23, 0)


def test_claude_window_is_saturday_before_eleven_fifteen():
    assert funnel.claude_window_refusal(SATURDAY_0500) is None
    assert funnel.claude_window_refusal(SATURDAY_1114) is None
    assert "11:15" in funnel.claude_window_refusal(SATURDAY_1115)
    assert "Saturdays only" in funnel.claude_window_refusal(FRIDAY_2300)


def test_claude_implements_every_tier_and_untiered():
    for tier in funnel.TIERS + (None,):
        assert funnel._begin_role_refusal("claude", tier, "implement") is None


def test_claude_outside_the_window_stops_before_usage_or_project(
    monkeypatch, capsys
):
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent: "run-id")
    monkeypatch.setattr(funnel, "_local_time", lambda now: SATURDAY_1115)
    monkeypatch.setattr(usage, "read_agent", lambda *args: pytest.fail(
        "the Claude lane reads no budget"))
    monkeypatch.setattr(funnel, "load_items", lambda *args, **kwargs:
                        pytest.fail("a closed window reads no Project"))

    assert funnel.main(["begin", "--agent", "claude", "--role",
                        "implement"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["gate"] == "time"
    assert result["do"] == "stop"


def test_claude_inside_the_window_is_unmetered():
    import usage as usage_module

    original = funnel._local_time
    try:
        funnel._local_time = lambda now: SATURDAY_0500
        saved = usage_module.read_agent
        usage_module.read_agent = lambda *args: pytest.fail(
            "the Claude lane reads no budget")
        try:
            import heartbeat

            start = funnel._start_begin_heartbeat
            funnel._start_begin_heartbeat = lambda agent: "run-id"
            try:
                out, reading = funnel._begin_preflight(
                    NOW, "claude", False, None)
            finally:
                funnel._start_begin_heartbeat = start
        finally:
            usage_module.read_agent = saved
    finally:
        funnel._local_time = original
    assert out["gate"] == "ok"
    assert out["unmetered"] is True
    assert reading["unmetered"] is True


def test_claude_ticket_begin_carries_the_packet_and_claude_vendor_block(
    monkeypatch, capsys
):
    monkeypatch.setattr(funnel, "_local_time", lambda now: SATURDAY_0500)
    project, ticket = _ticket(81, 80)
    calls = []
    monkeypatch.setattr(
        funnel,
        "implementation_packet",
        lambda repo, number, agent: (
            calls.append((repo, number, agent)) or {"repo": repo}
        ),
    )

    result, _ = _implementing_begin(
        monkeypatch, capsys, [project, ticket], agent="claude", tier=None,
        caller_role="implement",
    )

    assert result["do"] == "ticket"
    assert result["vendor"] == funnel.CLAUDE_IMPLEMENT_VENDOR
    assert "finish-ticket --agent claude" in (
        result["vendor"]["answer_handoff"])
    assert calls == [(ticket.repo, ticket.number, "claude")]


def test_skipped_outside_window_is_a_heartbeat_outcome():
    import heartbeat

    assert "skipped-outside-window" in heartbeat.OUTCOMES
