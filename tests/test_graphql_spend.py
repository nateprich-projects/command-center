"""Per-run GraphQL spend, accumulated from responses and reported to stderr (#295)."""

from __future__ import annotations

import io
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
import heartbeat  # noqa: E402


class Proc:
    returncode = 0

    def __init__(self, payload):
        self.stdout = json.dumps(payload)
        self.stderr = ""


def reset():
    funnel.reset_api_usage()


def test_spend_is_accumulated_from_the_response(monkeypatch):
    reset()
    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"viewer": {"login": "n"},
                  "rateLimit": {"cost": 7, "remaining": 4993,
                                "resetAt": "2026-09-09T04:52:19Z"}}}))

    funnel.gh_graphql("{viewer{login}}")

    spend = funnel.graphql_spend()
    assert spend == {"calls": 1, "cost": 7, "remaining": 4993,
                     "reset_at": "2026-09-09T04:52:19Z"}


def test_cost_is_summed_not_differenced(monkeypatch):
    """A shared token means a `remaining` delta charges us other agents' spend.

    Measured 2026-09-08: 1,826 points on this token belonged to something other
    than the measuring session.
    """
    reset()
    responses = [
        {"data": {"a": 1, "rateLimit": {"cost": 2, "remaining": 100, "resetAt": "z"}}},
        {"data": {"a": 1, "rateLimit": {"cost": 2, "remaining": 50, "resetAt": "z"}}},
    ]
    monkeypatch.setattr(funnel.subprocess, "run",
                        lambda *a, **k: Proc(responses.pop(0)))

    funnel.gh_graphql("{a}")
    funnel.gh_graphql("{a}")

    assert funnel.graphql_spend()["cost"] == 4, "sum reported cost, never a delta"
    assert funnel.graphql_spend()["remaining"] == 50


def test_cost_and_remaining_accumulate_in_separate_caller_buckets(monkeypatch):
    reset()
    responses = [
        {"data": {"rateLimit": {"cost": 2, "remaining": 100}}},
        {"data": {"rateLimit": {"cost": 3, "remaining": 80}}},
        {"data": {"rateLimit": {"cost": 5, "remaining": 70}}},
    ]

    def run(args, **kwargs):
        assert args[:3] == ["gh", "api", "graphql"]
        assert "rate_limit" not in args
        return Proc(responses.pop(0))

    monkeypatch.setattr(funnel.subprocess, "run", run)
    with funnel.graphql_caller("standard"):
        funnel.gh_graphql("{first}")
        funnel.gh_graphql("{second}")
    with funnel.graphql_caller("publisher"):
        funnel.gh_graphql("{third}")

    spend = funnel.graphql_caller_spend()
    assert {
        caller: {key: reading[key] for key in ("calls", "points", "remaining")}
        for caller, reading in spend.items()
    } == {
        "standard": {"calls": 2, "points": 5, "remaining": 80},
        "publisher": {"calls": 1, "points": 5, "remaining": 70},
    }
    assert [reading["cost"] for reading in spend["standard"]["readings"]] == [2, 3]
    assert [reading["remaining"] for reading in spend["standard"]["readings"]] == [
        100, 80,
    ]


def test_response_reading_keeps_reset_stamp_and_receipt_time(monkeypatch):
    reset()
    response_at = 1_790_425_555.125
    monkeypatch.setattr(funnel.time, "time", lambda: response_at)
    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"viewer": {}, "rateLimit": {
            "cost": 7, "remaining": 4993,
            "resetAt": "2026-09-26T10:49:20Z",
        }}}
    ))

    with funnel.graphql_caller("standard"):
        funnel.gh_graphql("{viewer{login}}")

    assert funnel.graphql_caller_spend()["standard"]["readings"] == [{
        "cost": 7,
        "remaining": 4993,
        "reset_at": "2026-09-26T10:49:20Z",
        "received_at": response_at,
    }]


def test_response_readings_reconcile_from_graphql_through_heartbeat(
        monkeypatch):
    reset()
    response_at = 1_790_425_555.125
    reset_at = "2026-09-26T10:49:20Z"
    responses = [
        {"data": {"viewer": {}, "rateLimit": {
            "cost": 7, "remaining": 4993, "resetAt": reset_at,
        }}},
        {"data": {"viewer": {}}},
    ]
    monkeypatch.setattr(funnel.time, "time", lambda: response_at)
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: Proc(responses.pop(0)),
    )
    monkeypatch.setattr(
        funnel, "_heartbeat_context",
        lambda run, agent: (run or "run-id", agent or "codex"),
    )
    written = []
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: written.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    with funnel.graphql_caller("standard"):
        funnel.gh_graphql("{viewer{login}}")
        funnel.gh_graphql("{viewer{login}}")
    funnel.report_api_cost(run="run-id", agent="codex")

    assert len(written) == 1
    readings = written[0]["graphql_by_caller"]
    assert readings["standard"]["readings"] == [{
        "cost": 7,
        "remaining": 4993,
        "reset_at": reset_at,
        "received_at": response_at,
    }]
    assert readings["unattributed"]["readings"] == [{
        "cost": None,
        "remaining": None,
        "reset_at": None,
        "received_at": response_at,
    }]
    assert heartbeat.graphql_points_by_reset_at(
        written, response_at - 1, response_at + 1,
    ) == {
        "by_reset_at": {
            reset_at: {
                "buckets": 1,
                "graphql_points": 7,
                "unknown_buckets": 0,
            },
        },
        "unattributed_unknown_buckets": 1,
        "untimed_unknown_buckets": 0,
    }


def test_response_without_rate_limit_keeps_unknown_values_null(monkeypatch):
    reset()
    response_at = 1_790_425_556.25
    monkeypatch.setattr(funnel.time, "time", lambda: response_at)
    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"viewer": {}}}
    ))

    with funnel.graphql_caller("standard"):
        funnel.gh_graphql("{viewer{login}}")

    assert funnel.graphql_caller_spend()["unattributed"]["readings"] == [{
        "cost": None,
        "remaining": None,
        "reset_at": None,
        "received_at": response_at,
    }]


def test_unreadable_cost_is_kept_under_unattributed(monkeypatch):
    reset()
    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"viewer": {}, "rateLimit": {"remaining": 42}}}
    ))

    with funnel.graphql_caller("escalated"):
        funnel.gh_graphql("{viewer{login}}")

    spend = funnel.graphql_caller_spend()
    assert {
        caller: {key: reading[key] for key in ("calls", "points", "remaining")}
        for caller, reading in spend.items()
    } == {
        "unattributed": {"calls": 1, "points": None, "remaining": 42},
    }
    assert spend["unattributed"]["readings"][0]["reset_at"] is None
    assert spend["unattributed"]["readings"][0]["remaining"] == 42


def test_partial_error_response_still_contributes_its_rate_limit_reading(
    monkeypatch,
):
    reset()
    response = {
        "data": {"rateLimit": {"cost": 4, "remaining": 20}},
        "errors": [{"message": "partial result"}],
    }
    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(
                            returncode=1, stdout=json.dumps(response),
                            stderr="partial result",
                        ))

    with funnel.graphql_caller("watch"):
        with pytest.raises(funnel.GitHubError, match="partial result"):
            funnel.gh_graphql("{viewer{login}}")

    spend = funnel.graphql_caller_spend()
    assert {
        caller: {key: reading[key] for key in ("calls", "points", "remaining")}
        for caller, reading in spend.items()
    } == {
        "watch": {"calls": 1, "points": 4, "remaining": 20},
    }
    assert spend["watch"]["readings"][0]["cost"] == 4
    assert spend["watch"]["readings"][0]["received_at"] is not None


def test_command_caller_mapping_covers_the_named_paths():
    assert funnel.graphql_caller_for_command(
        ["begin", "--tier", "standard"]
    ) == "begin"
    assert funnel.graphql_caller_for_command(["brief"]) == "publisher"
    assert funnel.graphql_caller_for_command(["main-ci"]) == "watch"
    assert funnel.graphql_caller_for_command(
        ["next-review", "--tier", "escalated"]
    ) == "escalated"
    assert funnel.graphql_caller_for_command(["breakdown-packet"]) == "breakdown"


def test_run_lane_comes_from_the_existing_heartbeat_spool(tmp_path, monkeypatch):
    (tmp_path / "codex.jsonl").write_text(json.dumps({
        "run": "run-id", "agent": "codex", "phase": "start",
        "tier": "standard",
    }) + "\n")
    monkeypatch.setenv("COMMAND_CENTER_HEARTBEAT_SPOOL", str(tmp_path))

    assert funnel.graphql_caller_for_run("run-id", "codex") == "standard"


def test_a_missing_or_malformed_block_does_not_break_the_call(monkeypatch):
    reset()
    monkeypatch.setattr(funnel.subprocess, "run",
                        lambda *a, **k: Proc({"data": {"a": 1}}))
    assert funnel.gh_graphql("{a}") == {"a": 1}

    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"a": 1, "rateLimit": "nonsense"}}))
    assert funnel.gh_graphql("{a}") == {"a": 1, "rateLimit": "nonsense"}
    assert funnel.graphql_spend()["calls"] == 2


def test_the_report_goes_to_a_stream_and_names_the_numbers():
    reset()
    funnel._GRAPHQL_SPEND.update(
        {"calls": 3, "cost": 41, "remaining": 130, "reset_at": "04:52:19Z"})
    out = io.StringIO()

    funnel.report_graphql_spend(out)

    text = out.getvalue()
    assert "3 call(s)" in text and "41 point(s)" in text
    assert "130 remaining" in text and "04:52:19Z" in text


def test_a_run_that_made_no_graphql_call_reports_nothing():
    """A row of zeros on every local command trains the reader to ignore it."""
    reset()
    out = io.StringIO()
    funnel.report_graphql_spend(out)
    assert out.getvalue() == ""


def test_unknown_remaining_is_said_rather_than_guessed():
    reset()
    funnel._GRAPHQL_SPEND.update({"calls": 1, "cost": 0})
    out = io.StringIO()
    funnel.report_graphql_spend(out)
    assert "unknown remaining" in out.getvalue()


def test_api_usage_counts_cli_calls_separately_from_graphql(monkeypatch):
    reset()

    def run(args, **kwargs):
        if args[:3] == ["gh", "api", "graphql"]:
            return Proc({
                "data": {
                    "viewer": {"login": "n"},
                    "rateLimit": {
                        "cost": 7, "remaining": 4993, "resetAt": "z",
                    },
                }
            })
        return Proc({"items": []})

    monkeypatch.setattr(funnel.subprocess, "run", run)

    funnel.gh_graphql("{viewer{login}}")
    assert funnel._gh_json("gh", "pr", "list") == {"items": []}

    assert funnel.api_usage() == {
        "calls": 2,
        "graphql_calls": 1,
        "cli_calls": 1,
        "refused_exhausted": 0,
        "cost": 7,
        "remaining": 4993,
        "reset_at": "z",
    }


def test_api_cost_keeps_points_and_all_gh_calls_separate(monkeypatch):
    reset()

    def run(args, **kwargs):
        if args[:3] == ["gh", "api", "graphql"]:
            return Proc({
                "data": {
                    "rateLimit": {"cost": 7, "remaining": 4993,
                                   "resetAt": "z"},
                }
            })
        return Proc({"items": []})

    monkeypatch.setattr(funnel.subprocess, "run", run)

    funnel.gh_graphql("{viewer{login}}")
    funnel._gh_json("gh", "pr", "list")

    assert funnel.api_cost() == {"graphql_points": 7, "gh_calls": 2}


def test_api_cost_marks_unreadable_graphql_points_null(monkeypatch):
    reset()
    monkeypatch.setattr(funnel.subprocess, "run",
                        lambda *a, **k: Proc({"data": {"viewer": {}}}))

    funnel.gh_graphql("{viewer{login}}")

    assert funnel.api_cost() == {"graphql_points": None, "gh_calls": 1}


def test_report_api_cost_attaches_measurement_to_the_resolved_run(monkeypatch):
    reset()
    funnel._API_USAGE["graphql_calls"] = 1
    funnel._API_USAGE["cli_calls"] = 2
    funnel._GRAPHQL_SPEND.update({"calls": 1, "cost": 7})
    funnel._GRAPHQL_COST_READS = 1
    funnel._GRAPHQL_CALLER_SPEND["standard"] = {
        "calls": 1, "points": 7, "remaining": 90,
    }
    captured = []

    monkeypatch.setattr(
        funnel, "_heartbeat_context",
        lambda run, agent: (run or "run-id", agent or "codex"),
    )
    import heartbeat
    monkeypatch.setattr(
        heartbeat, "record_api_cost",
        lambda agent, run, cost: captured.append((agent, run, cost)),
    )

    funnel.report_api_cost(run="run-id", agent="codex")

    assert captured == [(
        "codex", "run-id", {
            "graphql_points": 7,
            "gh_calls": 3,
            "graphql_by_caller": {
                "standard": {
                    "calls": 1, "points": 7, "remaining": 90,
                    "readings": [],
                },
            },
        }
    )]


def test_doctor_api_usage_uses_graphql_remaining_and_never_rest(monkeypatch, capsys):
    def run(args, **kwargs):
        assert args[:3] == ["gh", "api", "graphql"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "data": {
                    "viewer": {"login": "n"},
                    "rateLimit": {
                        "cost": 4, "remaining": 321, "resetAt": "later",
                    },
                }
            }),
            stderr="",
        )

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: pytest.fail(
            "doctor API usage must not consult REST /rate_limit"
        ),
    )
    monkeypatch.setattr(
        funnel, "load_items",
        lambda: (funnel.gh_graphql("{viewer{login}}"), [])[1],
    )
    monkeypatch.setattr(
        funnel, "doctor_checks",
        lambda items=None, merged_pr_facts=None: [
            funnel.Check("local", True, "ok", ""),
        ],
    )

    assert funnel.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert (
        "API usage: funnel doctor made 1 API call(s) (1 GraphQL, 0 gh CLI); "
        "cost 4 GraphQL point(s); 321 remaining, resets later"
    ) in output


def test_doctor_api_usage_fails_when_remaining_is_unreadable():
    reset()
    funnel._API_USAGE["cli_calls"] = 1
    funnel._GRAPHQL_SPEND.update({"calls": 1, "cost": 4})

    result = funnel.check_api_usage()

    assert not result.ok
    assert "unknown remaining" in result.found
    assert "rerun funnel doctor" in result.fix
    reset()
