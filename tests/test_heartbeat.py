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


def start(run, at, ticket=1):
    return {"run": run, "agent": "claude", "phase": "start",
            "ts": int(at), "ticket": ticket}


def finish(run, at, outcome="done"):
    return {"run": run, "agent": "claude", "phase": "finish",
            "ts": int(at), "outcome": outcome}


def unresolved(at, candidates, outcome="done"):
    return {"run": None, "agent": "claude", "phase": "finish", "ts": int(at),
            "outcome": outcome, "unresolved": True, "candidates": candidates}


# -- resolution ---------------------------------------------------------------

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
        records.append(start("run{}".format(i), old + i * MIN))
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
