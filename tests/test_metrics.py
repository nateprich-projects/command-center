"""Fixture-driven hourly metrics row and append-contract tests."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metrics  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 9, 23, 4, 30, tzinfo=timezone.utc)


def _json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _jsonl(name):
    return metrics._read_jsonl(str(FIXTURES / name))


def _inputs():
    ledgers = {
        "codex": _jsonl("metrics_codex.jsonl"),
        "claude": _jsonl("metrics_claude.jsonl"),
        "muse": _jsonl("metrics_muse.jsonl"),
    }
    return (
        _json("metrics_snapshot.json"),
        ledgers,
        _json("metrics_usage.json"),
        _jsonl("metrics_outcomes.jsonl"),
        _json("metrics_commits.json"),
        1234,
    )


def test_derive_row_covers_plan_metrics_and_preserves_rate_pairs():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()

    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )

    assert row["schema_version"] == 1
    assert row["hour"] == "2026-09-23T04:00:00Z"
    assert set(row["metrics"]) == set("ABCDEF")
    assert set(row["metrics"]["A"]) == {"A1", "A2", "A3", "A4", "A5", "A6"}
    assert set(row["metrics"]["B"]) == {"B1", "B2", "B3", "B4"}
    assert set(row["metrics"]["C"]) == {"C1", "C2", "C3", "C4", "C5", "C6"}
    assert set(row["metrics"]["D"]) == {"D1", "D2", "D3", "D4", "D5", "D6"}
    assert set(row["metrics"]["E"]) == {"E1", "E2", "E3", "E4", "E5"}
    assert set(row["metrics"]["F"]) == {"F1", "F2", "F3"}

    assert row["metrics"]["A"]["A1"]["value"] == {
        "total": 1, "by_repo": {"owner/repo": 1}
    }
    assert row["metrics"]["A"]["A2"]["numerator"] is None
    assert row["metrics"]["A"]["A2"]["denominator"] is None
    assert row["metrics"]["A"]["A2"]["gap"]
    assert row["metrics"]["A"]["A3"]["numerator"] == 3
    assert row["metrics"]["A"]["A3"]["denominator"] == 4
    assert row["metrics"]["A"]["A4"]["new_projects_started"]["value"] == 1
    assert row["metrics"]["B"]["B1"]["numerator"] == 1
    assert row["metrics"]["B"]["B1"]["denominator"] == 1
    assert row["metrics"]["B"]["B2"]["numerator"] == 2
    assert row["metrics"]["B"]["B2"]["denominator"] == 8
    assert row["metrics"]["C"]["C1"]["value"]["by_agent"]["codex"]["done"] == 1
    assert row["metrics"]["C"]["C1"]["value"]["by_agent_and_job"]["codex"]["implement"]["finishes"] == 1
    assert row["metrics"]["C"]["C2"]["productive_share"]["codex"]["implement"] == {
        "numerator": 1, "denominator": 1, "source": "heartbeat finish.outcome"
    }
    assert row["metrics"]["C"]["C3"]["error_rate_by_agent_and_job"]["codex"]["implement"]["numerator"] == 0
    assert row["metrics"]["C"]["C4"]["gap"]
    assert row["metrics"]["C"]["C4"]["value"]["by_agent_and_job"]["muse"]["review"]["unclassified"] == 1
    assert row["metrics"]["C"]["C6"]["value"]["by_agent_and_job"]["codex"]["implement"]["claim_to_pr"] == {
        "sum_seconds": 480.0, "count": 1
    }
    assert row["metrics"]["A"]["A6"]["reverts_by_repo"]["value"][
        "nateprich-projects/command-center"
    ] == 1
    assert row["metrics"]["D"]["D4"]["value"] is None
    assert row["metrics"]["D"]["D4"]["gap"]
    assert row["metrics"]["D"]["D5"]["graphql_points_per_run"]["value"]["codex"]["graphql_points"]["value"] == 5
    assert row["metrics"]["D"]["D6"]["held_hours_by_agent_and_reason"]["muse"]["over_pace"]["value"] == 0
    assert row["metrics"]["D"]["D2"]["funnel_vs_personal"]["funnel_share"]["numerator"] == 600
    assert row["metrics"]["D"]["D2"]["funnel_vs_personal"]["funnel_share"]["denominator"] == 1000
    assert row["metrics"]["E"]["E2"]["outstanding"]["value"] == 0
    assert row["metrics"]["E"]["E2"]["opened_this_hour"]["value"] is None
    assert row["metrics"]["E"]["E2"]["opened_this_hour"]["gap"]
    assert row["metrics"]["D"]["D5"]["points_per_brief"]["value"] == 21
    gate_dwell = row["metrics"]["E"]["E1"]["gate_dwell"]["value"]
    assert gate_dwell["Shaped"]["sum"] == 259200
    assert gate_dwell["Shaped"]["count"] == 1
    assert gate_dwell["Ready"]["sum"] is None
    assert gate_dwell["Ready"]["gap"]
    assert row["metrics"]["F"]["F2"]["value"] == 1234


def test_missing_inputs_remain_gaps_instead_of_becoming_zero():
    snapshot, ledgers, _, outcomes, commits, lines = _inputs()
    del snapshot["brief"]["maintenance_load"]["upkeep_projects"]

    row = metrics.derive_row(
        snapshot, ledgers, None, outcomes, NOW, commits, lines
    )

    upkeep = row["metrics"]["A"]["A3"]
    assert upkeep["numerator"] is None
    assert upkeep["denominator"] is None
    assert upkeep["gap"]
    assert row["metrics"]["D"]["D1"]["window_used_percent"]["value"] is None
    assert row["metrics"]["D"]["D1"]["window_used_percent"]["gap"]
    assert row["metrics"]["D"]["D2"]["funnel_vs_personal"]["funnel_share"]["gap"]


def test_derive_command_accepts_committed_snapshot_and_ledgers(capsys):
    result = metrics.main([
        "derive",
        "--snapshot", str(FIXTURES / "metrics_snapshot.json"),
        "--ledger", "codex={}".format(FIXTURES / "metrics_codex.jsonl"),
        "--ledger", "claude={}".format(FIXTURES / "metrics_claude.jsonl"),
        "--ledger", "muse={}".format(FIXTURES / "metrics_muse.jsonl"),
        "--outcomes", str(FIXTURES / "metrics_outcomes.jsonl"),
        "--usage", str(FIXTURES / "metrics_usage.json"),
        "--commits", str(FIXTURES / "metrics_commits.json"),
        "--line-count", "1234",
        "--now", "2026-09-23T04:30:00Z",
        "--dry-run",
    ])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["hour"] == "2026-09-23T04:00:00Z"
    assert output["metrics"]["C"]["C1"]["value"]["by_agent"]["codex"]["done"] == 1


def test_append_is_idempotent_and_jsonl_round_trips():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )

    first, appended = metrics.append_rows([], row)
    second, duplicate_appended = metrics.append_rows(first, row)

    assert appended == 1
    assert duplicate_appended == 0
    assert second == first
    assert metrics._decode_rows(metrics._encode_rows(second)) == second
