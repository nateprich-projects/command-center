"""Which run a `finish` belongs to.

The bug these pin: `start` wrote the run id to one file per agent and `finish`
read it back, so two overlapping runs clobbered the pointer and the first run's
finish was recorded against the second run's id. That makes one run look
finished while it is still going, and leaves the other looking like a start that
never came back — the exact signature the watchdog reports as a dying run.

So the tests are as much about refusing to guess as about resolving correctly.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "watchdog", ROOT / ".github" / "scripts" / "watchdog.py"
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

NOW = 1_788_600_000.0
MIN = 60
ERROR_CLASS_FIXTURE = (
    pathlib.Path(__file__).parent / "fixtures" / "error_classification.json"
)


def start(run, at, ticket=1):
    return {"run": run, "agent": "claude", "phase": "start",
            "ts": int(at), "ticket": ticket}


def bind(run, at, ticket=1):
    return {"run": run, "agent": "claude", "phase": "bind",
            "ts": int(at), "do": "ticket",
            "work": "nateprich-projects/command-center#{}".format(ticket)}


def finish(run, at, outcome="done"):
    return {"run": run, "agent": "claude", "phase": "finish",
            "ts": int(at), "outcome": outcome}


def api_event(run, graphql_points, gh_calls, at=NOW):
    return {
        "run": run, "agent": "claude", "phase": "api_cost", "ts": int(at),
        "api_cost": {
            "graphql_points": graphql_points,
            "gh_calls": gh_calls,
        },
    }


def unresolved(at, candidates, outcome="done"):
    return {"run": None, "agent": "claude", "phase": "finish", "ts": int(at),
            "outcome": outcome, "unresolved": True, "candidates": candidates}


# -- resolution ---------------------------------------------------------------

def test_read_github_excludes_records_only_in_the_local_spool(monkeypatch):
    remote = {"run": "remote", "agent": "codex", "phase": "finish"}
    spooled = {"run": "local", "agent": "codex", "phase": "finish"}
    monkeypatch.setattr(
        heartbeat, "_fetch", lambda agent: (json.dumps(remote), "blob-sha")
    )
    monkeypatch.setattr(heartbeat, "_spooled", lambda agent: [spooled])

    assert heartbeat.read_github("codex") == [remote]
    assert heartbeat.read("codex") == [remote, spooled]


def test_one_open_start_resolves_without_a_run_id():
    records = [start("aaa", NOW)]
    assert heartbeat.resolve_run(records, None) == ("aaa", None)


def test_two_open_starts_refuse_to_guess():
    """The incident: A starts, B starts, A finishes. There is no safe guess.

    'Most recent unfinished start' would pick B and reproduce the original bug,
    since the run that finishes is usually the one that started earlier.
    """
    records = [start("aaa", NOW), start("bbb", NOW + 3 * MIN)]
    run_id, candidates = heartbeat.resolve_run(records, None)
    assert run_id is None
    assert candidates == ["aaa", "bbb"]


def test_no_open_starts_refuses_to_guess():
    records = [start("aaa", NOW), finish("aaa", NOW + MIN)]
    assert heartbeat.resolve_run(records, None) == (None, [])


def test_explicit_run_id_wins_over_ambiguity():
    records = [start("aaa", NOW), start("bbb", NOW + 3 * MIN)]
    assert heartbeat.resolve_run(records, "aaa") == ("aaa", None)


def test_unknown_run_id_is_refused():
    records = [start("aaa", NOW)]
    with pytest.raises(heartbeat.HeartbeatError, match="never began"):
        heartbeat.resolve_run(records, "zzz")


def test_already_finished_run_id_is_refused():
    records = [start("aaa", NOW), finish("aaa", NOW + MIN)]
    with pytest.raises(heartbeat.HeartbeatError, match="already finished"):
        heartbeat.resolve_run(records, "aaa")


def test_unreadable_records_do_not_refuse_an_explicit_id():
    """GitHub unreachable and nothing spooled. Instrumentation must not gate the
    thing it instruments, so an explicit id is trusted rather than refused."""
    assert heartbeat.resolve_run([], "aaa") == ("aaa", None)


def test_finish_accepts_skipped_blocked(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id",
        "--outcome", "skipped-blocked",
    ]) == 0
    assert records[0]["outcome"] == "skipped-blocked"


def test_finish_records_structured_issue_outcomes(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "muse", "--run", "run-id",
        "--outcome", "done", "--ticket-count", "0", "--needs-decision",
        "", "--shape-status", "Shaped",
    ]) == 0
    assert records[0]["ticket_count"] == 0
    assert records[0]["needs_decision"] is None
    assert records[0]["shape_status"] == "Shaped"
    assert "token_usage" not in records[0]


def test_finish_records_every_muse_call_id_and_keeps_uncaptured_slots(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    call_record = {"session_ids": ["session-1", None, "session-3"],
                   "calls_made": 3}
    assert heartbeat.main([
        "finish", "--agent", "muse", "--run", "run-id",
        "--outcome", "done", "--muse-call-record", json.dumps(call_record),
    ]) == 0

    assert records[0]["muse_session_ids"] == ["session-1", None, "session-3"]
    assert records[0]["muse_calls_made"] == 3
    assert "token_usage" not in records[0]


def test_finish_keeps_single_id_bound_and_omits_token_snapshot(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "muse", "--run", "run-id",
        "--outcome", "done", "--muse-call-record",
        json.dumps({"session_ids": ["session-1"], "calls_made": 1}),
    ]) == 0

    assert "muse_session_ids" not in records[0]
    assert records[0]["muse_calls_made"] == 1
    assert "token_usage" not in records[0]


@pytest.mark.parametrize("call_record", [
    {"session_ids": ["session-1"], "calls_made": 2},
    {"session_ids": ["session-1", ""], "calls_made": 2},
    {"session_ids": ["session-1"], "calls_made": True},
])
def test_finish_rejects_malformed_muse_call_records(call_record):
    with pytest.raises(SystemExit):
        heartbeat.main([
            "finish", "--agent", "muse", "--run", "run-id",
            "--outcome", "done", "--muse-call-record", json.dumps(call_record),
        ])


def test_finish_rejects_negative_ticket_count():
    with pytest.raises(SystemExit):
        heartbeat.main([
            "finish", "--agent", "muse", "--run", "run-id",
            "--outcome", "done", "--ticket-count", "-1",
        ])


def test_start_records_the_runtime_checkout(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(
        heartbeat, "runtime_state",
        lambda: {"root": "/runtime/checkout", "head": "0123456789ab"},
    )

    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main(["start", "--agent", "codex", "--ticket", "360"]) == 0
    assert records[0]["runtime"] == {
        "root": "/runtime/checkout", "head": "0123456789ab"
    }


def test_start_and_finish_record_separate_usage_readings(monkeypatch):
    records = []
    readings = [
        {"five_hour": {"used_percent": 10.0, "resets_at": NOW + 100}},
        {"five_hour": {"used_percent": 12.0, "resets_at": NOW + 100}},
    ]
    seen_agents = []

    def usage_read(agent):
        seen_agents.append(agent)
        return readings.pop(0)

    monkeypatch.setattr(heartbeat, "read", lambda agent: records)
    monkeypatch.setattr(heartbeat, "session_id", lambda agent: "session-1")
    monkeypatch.setattr(heartbeat, "usage_snapshot", usage_read)
    monkeypatch.setattr(heartbeat, "close_rebegun_starts", lambda *args: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "runtime_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "token_usage_for_run", lambda *args: None)
    monkeypatch.setattr(heartbeat, "api_cost_for_run", lambda *args: None)
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main(["start", "--agent", "codex"]) == 0
    run = records[0]["run"]
    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", run, "--outcome", "done",
    ]) == 0

    assert [record["phase"] for record in records] == ["start", "finish"]
    assert [record["usage"] for record in records] == [
        {"five_hour": {"used_percent": 10.0, "resets_at": NOW + 100}},
        {"five_hour": {"used_percent": 12.0, "resets_at": NOW + 100}},
    ]
    assert seen_agents == ["codex", "codex"]


def test_usage_snapshot_reads_the_agent_own_provider(monkeypatch):
    import usage

    now = 1_700_000_000.0
    readings = {
        "claude": {
            "source": "anthropic", "captured_at": now,
            "windows": {"five_hour": {
                "used_percent": 11.0, "resets_at": now + 11,
            }},
        },
        "codex": {
            "source": "openai", "captured_at": now,
            "windows": {"five_hour": {
                "used_percent": 22.0, "resets_at": now + 22,
            }},
        },
        "zcode": {
            "source": "zai", "captured_at": now,
            "windows": {"five_hour": {
                "used_percent": 33.0, "resets_at": now + 33,
            }},
        },
    }
    monkeypatch.setattr(heartbeat.time, "time", lambda: now)
    monkeypatch.setattr(usage, "read_claude", lambda: readings["claude"])
    monkeypatch.setattr(usage, "read_codex", lambda: readings["codex"])
    monkeypatch.setattr(usage, "read_zai", lambda timestamp: readings["zcode"])

    assert heartbeat.usage_snapshot("claude") == {
        "five_hour": {"used_percent": 11.0, "resets_at": now + 11}
    }
    assert heartbeat.usage_snapshot("codex") == {
        "five_hour": {"used_percent": 22.0, "resets_at": now + 22}
    }
    zcode = heartbeat.usage_snapshot("zcode")
    assert zcode == {
        "five_hour": {"used_percent": 33.0, "resets_at": now + 33}
    }
    assert str(now + 22) not in str(zcode)


def test_usage_snapshot_records_the_muse_cost_window(monkeypatch):
    import usage

    now = 1_700_000_000.0
    reading = {
        "source": "muse", "captured_at": now,
        "windows": {"seven_day": {
            "used_percent": 12.5, "resets_at": now + usage.SEVEN_DAY,
            "spent_dollars": 3.75,
            "rolling": True,
        }},
    }
    monkeypatch.setattr(heartbeat.time, "time", lambda: now)
    monkeypatch.setattr(usage, "read_muse", lambda timestamp: reading)

    assert heartbeat.usage_snapshot("muse") == {
        "seven_day": {
            "used_percent": 12.5,
            "resets_at": now + usage.SEVEN_DAY,
            "spent_dollars": 3.75,
        }
    }


def test_muse_window_consumption_anchors_to_reset_and_counts_runs_once():
    reset_a = 1_800_000_000.0
    reset_b = reset_a + 7 * 86400

    def reading(run, phase, at, reset, spent):
        return {
            "run": run,
            "agent": "muse",
            "phase": phase,
            "ts": at,
            "usage": {"seven_day": {
                "resets_at": reset,
                "spent_dollars": spent,
            }},
        }

    first_start = reading("first", "start", 10, reset_a, 4.0)
    first_finish = reading("first", "finish", 20, reset_a, 5.25)
    second_start = reading("second", "start", 30, reset_a, 5.25)
    second_finish = reading("second", "finish", 40, reset_a, 8.0)
    next_window_start = reading("next", "start", 50, reset_b, 1.0)
    next_window_finish = reading("next", "finish", 60, reset_b, 4.5)
    crossed_start = reading("crossed", "start", 70, reset_a, 8.0)
    crossed_finish = reading("crossed", "finish", 80, reset_b, 2.0)

    rows = [
        first_start, first_finish,
        second_start, second_finish,
        next_window_start, next_window_finish,
        crossed_start, crossed_finish,
        # Re-reading rows, including repeated copies of one run, must not
        # multiply either its dollars or its sample count.
        first_start, first_finish, second_start, second_finish,
    ]

    assert heartbeat.muse_window_consumption(rows) == [
        {"resets_at": reset_b, "consumed_dollars": 3.5, "runs": 1},
        {"resets_at": reset_a, "consumed_dollars": 4.0, "runs": 2},
    ]


def test_usage_snapshot_fails_closed_for_unreadable_or_unknown_usage(
        monkeypatch):
    import usage

    def boom():
        raise RuntimeError("reader failed")

    monkeypatch.setattr(usage, "read_codex", boom)

    assert heartbeat.usage_snapshot("codex") is None
    assert heartbeat.usage_snapshot("nobody") is None


def test_finish_accepts_skipped_human_step(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id",
        "--outcome", "skipped-human-step",
    ]) == 0
    assert records[0]["outcome"] == "skipped-human-step"


def test_error_classification_fixture_fails_unmatched_notes_to_unclassified():
    fixture = json.loads(ERROR_CLASS_FIXTURE.read_text())
    runtime = fixture["runtime"]

    for case in fixture["cases"]:
        assert heartbeat.classify_error(case["note"], runtime) == case["expected"], (
            case["name"]
        )

    assert heartbeat.classify_error(
        "API rate limit exceeded", None
    ) == "unclassified"


def test_errored_finish_records_its_class_and_runtime_head(monkeypatch):
    records = []
    runtime = {"root": "/runtime/checkout", "head": "0123456789ab"}
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "runtime_state", lambda: runtime)
    monkeypatch.setattr(heartbeat, "token_usage_for_run", lambda *args: None)
    monkeypatch.setattr(heartbeat, "api_cost_for_run", lambda *args: None)
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id",
        "--outcome", "errored", "--note", "tests failed: test_checkout",
    ]) == 0

    assert records[0]["error_class"] == "regression"
    assert records[0]["runtime"] == runtime


def test_finish_records_input_usage_when_harness_exposes_both_counts(monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: {
        "total_input_tokens": 100,
        "fresh_input_tokens": 25,
        "ratio": 4.0,
    })
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(
        heartbeat, "runtime_state",
        lambda: {"root": "/runtime/checkout", "head": "0123456789ab"},
    )
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id",
        "--outcome", "done",
    ]) == 0
    assert records[0]["input_usage"] == {
        "total_input_tokens": 100,
        "fresh_input_tokens": 25,
        "ratio": 4.0,
    }
    assert records[0]["runtime"]["head"] == "0123456789ab"
    assert "error_class" not in records[0]


def test_finish_omits_input_usage_when_harness_does_not_expose_both_counts(
        monkeypatch):
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id",
        "--outcome", "done",
    ]) == 0
    assert "input_usage" not in records[0]


def test_finish_records_four_token_kinds_from_the_bound_session(monkeypatch):
    records = [start("run-id", NOW)]
    records[0]["agent"] = "codex"
    records[0]["session_id"] = "session-1"
    written = []
    seen = {}

    def usage_for_session(agent, session_id, started_at=None, finished_at=None):
        seen.update({
            "agent": agent,
            "session_id": session_id,
            "started_at": started_at,
            "finished_at": finished_at,
        })
        return {
            "fresh_input_tokens": 40,
            "cache_read_input_tokens": 60,
            "cache_write_input_tokens": 2,
            "output_tokens": 8,
        }

    monkeypatch.setattr(
        heartbeat.session_usage, "usage_for_session", usage_for_session
    )
    monkeypatch.setattr(heartbeat, "read", lambda agent: records)
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "runtime_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: written.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)
    monkeypatch.setattr(heartbeat.time, "time", lambda: NOW + 60)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id", "--outcome", "done",
    ]) == 0

    assert written[0]["token_usage"] == {
        "fresh_input_tokens": 40,
        "cache_read_input_tokens": 60,
        "cache_write_input_tokens": 2,
        "output_tokens": 8,
    }
    assert seen["agent"] == "codex"
    assert seen["session_id"] == "session-1"
    assert seen["started_at"].timestamp() == NOW
    assert seen["finished_at"].timestamp() == NOW + 60


def test_unreadable_token_usage_is_four_nulls_and_does_not_raise(
        monkeypatch):
    monkeypatch.setattr(
        heartbeat.session_usage,
        "usage_for_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )

    assert heartbeat.token_usage_for_run(
        "codex",
        [{"run": "run-id", "phase": "start", "ts": NOW,
          "session_id": "missing"}],
        "run-id",
        NOW + 60,
    ) == {
        "fresh_input_tokens": None,
        "cache_read_input_tokens": None,
        "cache_write_input_tokens": None,
        "output_tokens": None,
    }


def test_finish_sums_api_cost_events_from_two_funnel_commands(monkeypatch):
    records = [
        start("run-id", NOW),
        api_event("run-id", 7, 2),
        api_event("run-id", 5, 3),
    ]
    written = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: records)
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: written.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "claude", "--run", "run-id",
        "--outcome", "done",
    ]) == 0

    assert written[0]["api_cost"] == {
        "graphql_points": 12,
        "gh_calls": 5,
    }
    assert written[0]["graphql_by_caller"] == {
        "unattributed": {
            "calls": None, "points": 12, "remaining": None, "readings": [],
        },
    }


def test_api_cost_event_preserves_reset_metadata_and_null_unknowns(monkeypatch):
    written = []
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: written.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    heartbeat.record_api_cost("claude", "run-id", {
        "graphql_points": 7,
        "gh_calls": 2,
        "graphql_by_caller": {
            "standard": {
                "calls": 2,
                "points": 7,
                "remaining": 4993,
                "readings": [
                    {
                        "cost": 7,
                        "remaining": 4993,
                        "reset_at": "2026-09-26T10:49:20Z",
                        "received_at": NOW + 0.25,
                    },
                    {
                        "cost": None,
                        "remaining": None,
                        "reset_at": None,
                        "received_at": NOW + 0.5,
                    },
                ],
            },
        },
    })

    assert written[0]["graphql_by_caller"]["standard"]["readings"] == [
        {
            "cost": 7,
            "remaining": 4993,
            "reset_at": "2026-09-26T10:49:20Z",
            "received_at": NOW + 0.25,
        },
        {
            "cost": None,
            "remaining": None,
            "reset_at": None,
            "received_at": NOW + 0.5,
        },
    ]


def test_api_cost_reader_ignores_additive_graphql_readings():
    records = [{
        "run": "run-id",
        "phase": "api_cost",
        "api_cost": {"graphql_points": 7, "gh_calls": 2},
        "graphql_by_caller": {
            "standard": {
                "calls": 2,
                "points": 7,
                "remaining": 4993,
                "readings": [{
                    "cost": 7,
                    "remaining": 4993,
                    "reset_at": "2026-09-26T10:49:20Z",
                    "received_at": NOW + 0.25,
                }],
            },
        },
    }]

    assert heartbeat.api_cost_for_run(records, "run-id") == {
        "graphql_points": 7,
        "gh_calls": 2,
    }


def test_finish_aggregates_caller_costs_and_keeps_unknowns_unattributed(
    monkeypatch,
):
    records = [
        start("run-id", NOW),
        {
            "run": "run-id", "agent": "claude", "phase": "api_cost",
            "ts": int(NOW) + 1,
            "api_cost": {"graphql_points": 5, "gh_calls": 2},
            "graphql_by_caller": {
                "standard": {
                    "calls": 2, "points": 5, "remaining": 100,
                    "readings": [
                        {"cost": 2, "remaining": 100, "reset_at": "z",
                         "received_at": NOW + 1.1},
                        {"cost": 3, "remaining": 100, "reset_at": "z",
                         "received_at": NOW + 1.2},
                    ],
                },
                "unattributed": {
                    "calls": 1, "points": None, "remaining": 90,
                    "readings": [
                        {"cost": None, "remaining": 90, "reset_at": None,
                         "received_at": NOW + 1.3},
                    ],
                },
            },
        },
        {
            "run": "run-id", "agent": "claude", "phase": "api_cost",
            "ts": int(NOW) + 2,
            "api_cost": {"graphql_points": 7, "gh_calls": 1},
            "graphql_by_caller": {
                "standard": {
                    "calls": 1, "points": 7, "remaining": 80,
                    "readings": [
                        {"cost": 7, "remaining": 80, "reset_at": "later",
                         "received_at": NOW + 2.1},
                    ],
                },
                "publisher": {
                    "calls": 1, "points": 3, "remaining": 75,
                    "readings": [
                        {"cost": 3, "remaining": 75, "reset_at": "later",
                         "received_at": NOW + 2.2},
                    ],
                },
                "unattributed": {
                    "calls": 1, "points": None, "remaining": 70,
                    "readings": [
                        {"cost": None, "remaining": 70, "reset_at": None,
                         "received_at": NOW + 2.3},
                    ],
                },
            },
        },
    ]

    result = heartbeat.graphql_by_caller_for_run(records, "run-id")

    assert result == {
        "publisher": {
            "calls": 1, "points": 3, "remaining": 75,
            "readings": [
                {"cost": 3, "remaining": 75, "reset_at": "later",
                 "received_at": NOW + 2.2},
            ],
        },
        "standard": {
            "calls": 3, "points": 12, "remaining": 80,
            "readings": [
                {"cost": 2, "remaining": 100, "reset_at": "z",
                 "received_at": NOW + 1.1},
                {"cost": 3, "remaining": 100, "reset_at": "z",
                 "received_at": NOW + 1.2},
                {"cost": 7, "remaining": 80, "reset_at": "later",
                 "received_at": NOW + 2.1},
            ],
        },
        "unattributed": {
            "calls": 2, "points": None, "remaining": 70,
            "readings": [
                {"cost": None, "remaining": 90, "reset_at": None,
                 "received_at": NOW + 1.3},
                {"cost": None, "remaining": 70, "reset_at": None,
                 "received_at": NOW + 2.3},
            ],
        },
    }


def test_graphql_points_reconcile_into_the_recorded_reset_window():
    record = finish("run-id", NOW + 10)
    record["graphql_by_caller"] = {
        "standard": {
            "calls": 1,
            "points": 12,
            "remaining": 4988,
            "readings": [{
                "cost": 12,
                "remaining": 4988,
                "reset_at": "2026-09-26T13:00:00Z",
                "received_at": NOW + 5,
            }],
        },
    }

    event = dict(record, phase="api_cost", ts=int(NOW + 6))
    assert heartbeat.graphql_points_by_reset_at(
        [record, event], NOW, NOW + 10,
    ) == {
        "by_reset_at": {
            "2026-09-26T13:00:00Z": {
                "buckets": 1,
                "graphql_points": 12,
                "unknown_buckets": 0,
            },
        },
        "unattributed_unknown_buckets": 0,
        "untimed_unknown_buckets": 0,
    }


def test_graphql_points_count_null_reset_bucket_as_unknown_not_zero():
    record = api_event("run-id", None, 1, at=NOW + 10)
    record["graphql_by_caller"] = {
        "unattributed": {
            "calls": 1,
            "points": None,
            "remaining": None,
            "readings": [{
                "cost": None,
                "remaining": None,
                "reset_at": None,
                "received_at": NOW + 5,
            }],
        },
    }

    result = heartbeat.graphql_points_by_reset_at(
        [record], NOW, NOW + 10,
    )

    assert result == {
        "by_reset_at": {},
        "unattributed_unknown_buckets": 1,
        "untimed_unknown_buckets": 0,
    }


def test_graphql_points_keep_mixed_reset_windows_across_hour_boundary():
    record = finish("run-id", NOW + 7200)
    record["graphql_by_caller"] = {
        "standard": {
            "calls": 2,
            "points": 19,
            "remaining": 4981,
            "readings": [
                {
                    "cost": 8,
                    "remaining": 4992,
                    "reset_at": "2026-09-26T14:00:00Z",
                    "received_at": NOW + 3599,
                },
                {
                    "cost": 11,
                    "remaining": 4981,
                    "reset_at": "2026-09-26T15:00:00Z",
                    "received_at": NOW + 3601,
                },
            ],
        },
    }

    assert heartbeat.graphql_points_by_reset_at(
        [record], NOW, NOW + 7200,
    ) == {
        "by_reset_at": {
            "2026-09-26T14:00:00Z": {
                "buckets": 1,
                "graphql_points": 8,
                "unknown_buckets": 0,
            },
            "2026-09-26T15:00:00Z": {
                "buckets": 1,
                "graphql_points": 11,
                "unknown_buckets": 0,
            },
        },
        "unattributed_unknown_buckets": 0,
        "untimed_unknown_buckets": 0,
    }


def test_graphql_points_with_known_reset_and_null_cost_stay_unknown():
    record = finish("run-id", NOW + 10)
    record["graphql_by_caller"] = {
        "unattributed": {
            "calls": 1,
            "points": None,
            "remaining": 4990,
            "readings": [{
                "cost": None,
                "remaining": 4990,
                "reset_at": "2026-09-26T13:00:00Z",
                "received_at": NOW + 5,
            }],
        },
    }

    assert heartbeat.graphql_points_by_reset_at(
        [record], NOW, NOW + 10,
    ) == {
        "by_reset_at": {
            "2026-09-26T13:00:00Z": {
                "buckets": 1,
                "graphql_points": None,
                "unknown_buckets": 1,
            },
        },
        "unattributed_unknown_buckets": 0,
        "untimed_unknown_buckets": 0,
    }


def test_finish_reports_null_api_cost_without_funnel_commands(monkeypatch):
    records = [start("run-id", NOW)]
    written = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: records)
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: written.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "claude", "--run", "run-id",
        "--outcome", "done",
    ]) == 0

    assert written[0]["api_cost"] == {
        "graphql_points": None,
        "gh_calls": None,
    }


def test_finish_keeps_only_unreadable_api_cost_field_null(monkeypatch):
    records = [start("run-id", NOW), api_event("run-id", None, 2)]
    written = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: records)
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(
        heartbeat, "append",
        lambda agent, record: written.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "claude", "--run", "run-id",
        "--outcome", "done",
    ]) == 0

    assert written[0]["api_cost"] == {
        "graphql_points": None,
        "gh_calls": 2,
    }


def test_heartbeat_read_includes_api_cost_events(monkeypatch, capsys):
    monkeypatch.setattr(
        heartbeat, "read",
        lambda agent: [api_event("run-id", 4, 1)],
    )

    assert heartbeat.main(["read", "--agent", "claude"]) == 0
    assert '"api_cost"' in capsys.readouterr().out


def test_finish_rejects_an_unknown_outcome():
    with pytest.raises(SystemExit) as exc:
        heartbeat.main([
            "finish", "--agent", "codex", "--run", "run-id",
            "--outcome", "not-a-real-outcome",
        ])
    assert exc.value.code == 2


# -- open starts --------------------------------------------------------------

def test_the_incident_replayed():
    """A 10:42, B 10:45, A finishes 10:51 naming itself. Only B stays open."""
    records = [
        start("aaa", NOW),
        start("bbb", NOW + 3 * MIN),
        finish("aaa", NOW + 9 * MIN),
    ]
    assert [r["run"] for r in heartbeat.open_starts(records)] == ["bbb"]


def test_unresolved_finish_clears_the_oldest_candidate_only():
    """One of them ended, so exactly one start should stay open.

    Clearing both would hide a run that really did die; clearing neither would
    report a completed run as dying.
    """
    records = [
        start("aaa", NOW),
        start("bbb", NOW + 3 * MIN),
        unresolved(NOW + 9 * MIN, ["aaa", "bbb"]),
    ]
    assert [r["run"] for r in heartbeat.open_starts(records)] == ["bbb"]


def test_two_unresolved_finishes_clear_both():
    records = [
        start("aaa", NOW),
        start("bbb", NOW + 3 * MIN),
        unresolved(NOW + 9 * MIN, ["aaa", "bbb"]),
        unresolved(NOW + 12 * MIN, ["bbb"]),
    ]
    assert heartbeat.open_starts(records) == []


def test_unfinished_still_applies_its_ttl():
    records = [start("aaa", NOW)]
    assert heartbeat.unfinished(records, NOW + MIN, 2 * 3600) == []
    assert len(heartbeat.unfinished(records, NOW + 3 * 3600, 2 * 3600)) == 1


def test_run_summary_separates_same_session_rebegins_from_finishes():
    records = [
        start("old", NOW),
        {
            "run": "old",
            "agent": "claude",
            "phase": "finish",
            "ts": NOW + MIN,
            "outcome": "skipped-blocked",
            "re_begun_by": "fresh",
        },
        start("fresh", NOW + 2 * MIN),
        finish("fresh", NOW + 3 * MIN),
    ]

    assert heartbeat.run_summary(records, now=NOW + 4 * MIN) == {
        "starts": 2,
        "finishes": 1,
        "re_begins": 1,
    }


def test_normal_skipped_blocked_finish_is_not_a_rebegin():
    records = [finish("blocked", NOW, "skipped-blocked")]

    assert heartbeat.run_summary(records, now=NOW + MIN) == {
        "starts": 0,
        "finishes": 1,
        "re_begins": 0,
    }


# -- the watchdog reads it the same way ---------------------------------------

def test_watchdog_does_not_report_an_unattributable_finish_as_dying():
    """The false alarm this whole change exists to prevent."""
    old = NOW - 5 * 3600
    records = [
        start("aaa", old),
        start("bbb", old + 3 * MIN),
        unresolved(old + 9 * MIN, ["aaa", "bbb"]),
        finish("bbb", old + 20 * MIN),
        # Keep the silence check out of this test: its subject is the
        # unattributable finish, not whether five hours have passed since the
        # last record.
        finish("recent", NOW - 20 * MIN),
    ]
    assert watchdog.assess("claude", records, NOW) == []


def test_watchdog_still_reports_genuinely_dying_runs():
    old = NOW - 5 * 3600
    records = []
    for i in range(watchdog.DYING_THRESHOLD):
        at = old + i * MIN
        records.extend([
            start("run{}".format(i), at),
            bind("run{}".format(i), at + 1, i),
        ])
    found = watchdog.assess("claude", records, NOW)
    assert any("never finished" in p for p in found)


def test_finish_accepts_skipped_api_reserve(monkeypatch):
    """A GraphQL reserve decline is a refusal, not a failure (#273).

    Recording it as `errored` would make the watchdog alarm on the system
    working correctly.
    """
    records = []
    monkeypatch.setattr(heartbeat, "read", lambda agent: [])
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append(record) or "spooled",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "run-id",
        "--outcome", "skipped-api-reserve",
    ]) == 0
    assert records[0]["outcome"] == "skipped-api-reserve"


def test_the_reserve_decline_is_in_the_skipped_family():
    """The watchdog ignores `skipped-*` by prefix, so the name carries the
    healthy classification. A name outside that family would alarm."""
    assert "skipped-api-reserve" in heartbeat.OUTCOMES
    assert "skipped-api-reserve".startswith("skipped-")


def test_budget_exhausted_is_a_named_non_skipped_finish():
    assert "budget-exhausted" in heartbeat.OUTCOMES
    assert not "budget-exhausted".startswith("skipped-")


def test_heartbeat_retains_about_three_days_of_records():
    assert heartbeat.KEEP == 10000
