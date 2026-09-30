"""Fixture-driven hourly metrics row and append-contract tests."""

from __future__ import annotations

import base64
import copy
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import metrics  # noqa: E402
import outcomes  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 9, 23, 5, 30, tzinfo=timezone.utc)


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
    assert row["metrics"]["A"]["A6"]["reopened_tickets"]["value"] == 0
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
    assert row["metrics"]["D"]["D1"]["dollars_per_day"]["numerator"] == 21
    assert row["metrics"]["D"]["D1"]["dollars_per_day"]["denominator"] == 3
    assert row["metrics"]["D"]["D1"]["dollars_per_day"]["source"] == (
        "usage.py read_muse windows.seven_day.trailing_72h_dollars / "
        "(MUSE_RATE_LOOKBACK / 86400)"
    )
    assert row["metrics"]["D"]["D1"]["window_resets_at"]["value"] == metrics._iso(
        datetime.fromtimestamp(1790308800, tz=timezone.utc)
    )
    assert row["metrics"]["D"]["D2"]["funnel_vs_personal"]["value"]["funnel_tokens"] == 140
    assert row["metrics"]["D"]["D2"]["funnel_vs_personal"]["funnel_share"]["numerator"] == 140
    assert row["metrics"]["D"]["D2"]["funnel_vs_personal"]["funnel_share"]["denominator"] == 170
    assert row["metrics"]["D"]["D3"]["value"]["estimated"]["value"] is False
    assert row["metrics"]["D"]["D5"]["graphql_points_per_run"]["value"]["codex"]["graphql_points"]["value"] == 5
    assert row["metrics"]["D"]["D5"]["graphql_points_per_run"]["value"]["codex"]["points_per_run"] == {
        "numerator": 5,
        "denominator": 1,
        "source": "heartbeat.finish.api_cost.graphql_points / finished runs",
    }
    assert row["metrics"]["D"]["D6"]["held_hours_by_agent_and_reason"]["muse"]["over_pace"]["value"] == 1200 / 3600.0
    assert row["metrics"]["D"]["D5"]["points_per_brief"]["value"] == 21
    assert row["metrics"]["D"]["D5"]["gh_calls_per_brief"]["value"] == 8
    gate_dwell = row["metrics"]["E"]["E1"]["gate_dwell"]["value"]
    assert gate_dwell["Shaped"]["sum"] == 259200
    assert gate_dwell["Shaped"]["count"] == 1
    assert gate_dwell["Ready"]["sum"] is None
    assert gate_dwell["Ready"]["gap"]
    assert row["metrics"]["E"]["E2"]["outstanding"]["value"] == 0
    assert row["metrics"]["E"]["E2"]["opened_this_hour"]["value"] is None
    assert row["metrics"]["E"]["E2"]["opened_this_hour"]["gap"]
    assert row["metrics"]["F"]["F2"]["value"] == 1234


def test_d4_uses_priced_run_usage_and_excludes_unpriced_runs():
    token_kinds = (
        "fresh_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
    )
    rates = [
        {
            "provider": "openai",
            "model": "gpt-test",
            "token_kind": kind,
            "usd_per_million_tokens": amount,
            "effective_from": "2026-09-01T00:00:00Z",
            "source_url": "https://example.test/pricing",
            "recorded_at": "2026-09-22T00:00:00Z",
        }
        for kind, amount in zip(token_kinds, (1, 2, 3, 4))
    ]
    # Independent expected rate: (100*1 + 200*2 + 300*3 + 400*4) / 1M = $0.003.
    run_fields = {
        "agent": "codex",
        "provider": "openai",
        "model": "gpt-test",
        "reasoning_effort": "high",
        "started_at": "2026-09-23T04:10:00Z",
    }
    record = outcomes.derive_outcome(
        {
            "repo": "owner/repo",
            "number": 42,
            "title": "Ticket 42",
            "closedAt": "2026-09-23T04:26:00Z",
        },
        prs=[{
            "number": 7,
            "state": "MERGED",
            "mergedAt": "2026-09-23T04:26:00Z",
            "headRefName": "ticket/42",
        }],
        run_observations=[
            {
                **run_fields,
                "run": "priced-run",
                "token_usage": {
                    "fresh_input_tokens": 100,
                    "cache_read_input_tokens": 200,
                    "cache_write_input_tokens": 300,
                    "output_tokens": 400,
                },
            },
            {
                **run_fields,
                "run": "incomplete-run",
                "model": "gpt-unpriced",
                "token_usage": None,
            },
        ],
        rate_rows=rates,
        now=NOW,
    )
    assert outcomes._cost_observation({
        "token_usage": record["runs"][0]["token_usage"],
    }) is None
    assert outcomes._cost_observation(record["runs"][0]) == (
        pytest.approx(0.003), "USD"
    )
    missing_record = outcomes.derive_outcome(
        {
            "repo": "owner/repo",
            "number": 43,
            "title": "Ticket 43",
            "closedAt": "2026-09-23T04:30:00Z",
        },
        prs=[{
            "number": 8,
            "state": "MERGED",
            "mergedAt": "2026-09-23T04:30:00Z",
            "headRefName": "ticket/43",
        }],
        run_observations=[{
            **run_fields,
            "run": "missing-run",
            "token_usage": None,
        }],
        rate_rows=rates,
        now=NOW,
    )
    snapshot = _json("metrics_snapshot.json")
    signal_summary = outcomes.signal_summary([record, missing_record], now=NOW)
    cost_signal = signal_summary["signals"]["cost_per_merged_pr"]
    assert cost_signal["status"] == "partial"
    assert cost_signal["sample_size"] == 1
    assert cost_signal["merged_records"] == 2
    assert cost_signal["missing_records"] == 2
    assert cost_signal["by_lane"][0]["merged_prs"] == 1
    assert cost_signal["by_lane"][0]["cost_per_merged_pr"] == pytest.approx(0.003)
    snapshot["brief"]["outcome_signals"] = signal_summary

    row = metrics.derive_row(
        snapshot,
        _inputs()[1],
        _json("metrics_usage.json"),
        [record, missing_record],
        NOW,
        _json("metrics_commits.json"),
        1234,
    )

    d4 = row["metrics"]["D"]["D4"]
    assert len(d4["value"]) == 1
    assert d4["value"][0]["lane"] == "codex/gpt-test/high"
    assert d4["value"][0]["unit"] == "USD"
    assert d4["value"][0]["numerator"] == pytest.approx(0.003)
    assert d4["value"][0]["denominator"] == 1
    assert d4["gap"] == (
        "outcomes cost join is partial; some merged tickets or runs lack priced usage"
    )


def test_d4_shared_cost_join_fills_execution_panel_for_representative_window():
    """#1272 D4, made concrete by #1284's Panel D contract.

    #1272 sources cost per merged PR by lane from outcomes.signal_summary;
    #1284 requires missing token_usage to remain a labelled gap, not zero.
    """
    window = _jsonl("d4_representative_window.jsonl")
    summary = outcomes.signal_summary(window, now=NOW)
    cost = summary["signals"]["cost_per_merged_pr"]

    assert cost["status"] == "partial"
    assert cost["sample_size"] == 6
    assert cost["merged_records"] == 7
    assert cost["missing_records"] == 2
    # Independent check for the seven-day fixture: $0.15 / 6 merged PRs.
    assert cost["by_lane"] == [{
        "lane": "codex/gpt-test/high",
        "unit": "USD",
        "merged_prs": 6,
        "total_cost": 0.15,
        "cost_per_merged_pr": 0.025,
    }]
    snapshot = _json("metrics_snapshot.json")
    snapshot["brief"]["outcome_signals"] = summary
    _, ledgers, usage, _, commits, lines = _inputs()
    row = metrics.derive_row(
        snapshot, ledgers, usage, window, NOW, commits, lines
    )

    d4 = row["metrics"]["D"]["D4"]
    assert d4["source"] == "brief.outcome_signals.signals.cost_per_merged_pr.by_lane"
    assert d4["value"] == [
        {
            "lane": lane["lane"],
            "unit": lane["unit"],
            "numerator": lane["total_cost"],
            "denominator": lane["merged_prs"],
            "source": "brief.outcome_signals.signals.cost_per_merged_pr.by_lane",
        }
        for lane in cost["by_lane"]
    ]
    # #1284 specifies a labelled gap when the panel's usage input is absent.
    assert d4["gap"] == (
        "outcomes cost join is partial; some merged tickets or runs lack priced usage"
    )
    series = metrics.series_from_rows([row], NOW)
    lane = series["metrics"]["D"]["D4"]["lane"]["codex/gpt-test/high"]
    assert lane["daily"][-1] == 0.025
    assert lane["numerators"][-1] == 0.15
    assert lane["denominators"][-1] == 6
    assert lane["gap"] == d4["gap"]


@pytest.mark.parametrize("estimated", [False, True])
def test_claude_estimated_flag_reaches_d3_metrics_series(estimated):
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    usage["claude"]["estimated"] = estimated
    usage["claude"]["source"] = "claude-local-estimate" if estimated else "claude"

    row = metrics.derive_row(snapshot, ledgers, usage, outcomes, NOW, commits, lines)
    fact = row["metrics"]["D"]["D3"]["value"]["estimated"]
    series = metrics.series_from_rows([row], NOW)

    assert fact["value"] is estimated
    assert series["metrics"]["D"]["D3"]["estimated"]["daily"][-1] is estimated


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
    assert row["metrics"]["D"]["D1"]["dollars_per_day"]["numerator"] is None
    assert row["metrics"]["D"]["D1"]["dollars_per_day"]["denominator"] is None
    assert row["metrics"]["D"]["D1"]["dollars_per_day"]["gap"]


def test_unreadable_brief_sections_remain_gaps_in_metrics():
    snapshot, ledgers, _, outcomes, commits, lines = _inputs()
    brief = snapshot["brief"]
    unreadable = (
        "maintenance_load", "disposal", "total_needing_nate",
        "counts_by_gate", "human_steps", "blocked_human_steps",
        "status_state_mismatches",
    )
    for section in unreadable:
        brief[section] = None
        brief.setdefault("missing", []).append({
            "section": section,
            "error": "brief budget exhausted",
        })

    row = metrics.derive_row(
        snapshot, ledgers, None, outcomes, NOW, commits, lines
    )

    assert row["metrics"]["A"]["A3"]["numerator"] is None
    assert row["metrics"]["A"]["A3"]["gap"]
    assert row["metrics"]["A"]["A5"]["done"]["value"] is None
    assert row["metrics"]["A"]["A5"]["done"]["gap"]
    assert row["metrics"]["E"]["E1"]["total_needing_nate"]["gap"]
    assert row["metrics"]["E"]["E1"]["by_gate"]["gap"]
    assert row["metrics"]["E"]["E2"]["outstanding"]["value"] is None
    assert row["metrics"]["E"]["E2"]["outstanding"]["gap"]
    assert row["metrics"]["E"]["E5"]["status_state_mismatches"]["gap"]


def test_muse_dollar_rate_gaps_without_raw_trailing_spend():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    del usage["muse"]["windows"]["seven_day"]["trailing_72h_dollars"]

    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )

    rate = row["metrics"]["D"]["D1"]["dollars_per_day"]
    assert rate["numerator"] is None
    assert rate["denominator"] is None
    assert rate["gap"]


def test_negative_net_open_growth_is_preserved():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    snapshot["brief"]["disposal"]["net_open_growth"] = -2

    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )

    assert row["metrics"]["A"]["A5"]["net_open_growth"]["value"] == -2


def test_codex_rollout_split_groups_automation_and_personal_tokens():
    split = metrics.read_codex_thread_source_split(
        {"resets_at": 1790308800},
        NOW,
        [
            str(FIXTURES / "metrics_codex_automation_rollout.jsonl"),
            str(FIXTURES / "metrics_codex_personal_rollout.jsonl"),
        ],
    )

    assert split["value"]["funnel_tokens"] == 140
    assert split["value"]["personal_tokens"] == 30
    assert split["value"]["unit"] == "tokens"


def test_codex_rollout_without_thread_source_is_a_gap(tmp_path, monkeypatch):
    rollout = tmp_path / "unknown-source.jsonl"
    rollout.write_text(
        json.dumps({
            "type": "token_usage_record",
            "timestamp": "2026-09-23T04:10:00Z",
            "payload": {"turn_token_usage": {"total_tokens": 30}},
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(metrics.os.path, "getmtime", lambda _path: NOW.timestamp())

    split = metrics.read_codex_thread_source_split(
        {"resets_at": (NOW + timedelta(days=1)).timestamp()},
        NOW,
        [str(rollout)],
    )

    assert split["value"] is None
    assert "thread_source" in split["gap"]


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
        "--now", "2026-09-23T05:30:00Z",
        "--dry-run",
    ])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["hour"] == "2026-09-23T04:00:00Z"
    assert output["metrics"]["C"]["C1"]["value"]["by_agent"]["codex"]["done"] == 1


def test_stale_outcomes_leave_hourly_counts_as_gaps():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    outcomes[0]["derived_at"] = "2026-09-23T04:59:59Z"

    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )

    metrics_by_group = row["metrics"]
    assert metrics_by_group["A"]["A1"]["value"] is None
    assert metrics_by_group["A"]["A1"]["gap"]
    assert metrics_by_group["A"]["A6"]["reopened_tickets"]["value"] is None
    assert metrics_by_group["A"]["A6"]["reopened_tickets"]["gap"]
    assert metrics_by_group["B"]["B1"]["numerator"] is None
    assert metrics_by_group["B"]["B1"]["denominator"] is None
    assert metrics_by_group["B"]["B1"]["gap"]
    assert metrics_by_group["C"]["C6"]["value"] is None
    assert metrics_by_group["C"]["C6"]["gap"]


def _stale_outcome_rows():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    stale_outcomes = copy.deepcopy(outcomes)
    stale_outcomes[0]["derived_at"] = "2026-09-23T04:59:59Z"
    old_hour = datetime(2026, 9, 23, 4, tzinfo=timezone.utc)
    stale_row = metrics.derive_row(
        snapshot, ledgers, usage, stale_outcomes, NOW, commits, lines,
        hour_start=old_hour,
    )

    fresh_outcomes = copy.deepcopy(outcomes)
    fresh_outcomes[0]["derived_at"] = "2026-09-23T06:00:00Z"
    current_now = NOW + timedelta(hours=1)
    current_row = metrics.derive_row(
        snapshot, ledgers, usage, fresh_outcomes, current_now, commits, lines,
    )
    return (
        stale_row, current_row, snapshot, ledgers, usage, fresh_outcomes,
        commits, lines, old_hour, current_now,
    )


def _without_outcome_tiles(row):
    preserved = copy.deepcopy(row)
    for path in metrics.OUTCOME_TILE_PATHS:
        parent = preserved
        for key in ("metrics",) + path[:-1]:
            parent = parent[key]
        parent[path[-1]] = "<outcome tile>"
    return preserved


def _nested(row, path):
    value = row
    for key in ("metrics",) + path:
        value = value[key]
    return value


def test_remeasure_recent_outcome_tiles_and_preserve_the_current_append():
    (
        stale_row, current_row, snapshot, ledgers, usage, fresh_outcomes,
        commits, lines, old_hour, current_now,
    ) = _stale_outcome_rows()
    previous = copy.deepcopy(stale_row)
    previous_d4 = copy.deepcopy(stale_row["metrics"]["D"]["D4"])
    assert previous_d4["value"] is None
    fresh_outcomes.append({
        "ticket": "owner/repo#43",
        "derived_at": "2026-09-23T06:00:00Z",
        "merged": True,
        "merged_prs": [8],
        "runs": [{
            "agent": "codex",
            "model": "gpt-test",
            "reasoning_effort": "high",
            "notional_api_cost": {
                "value": 0.003,
                "unit": "USD",
                "basis": "notional_api_list_price",
                "status": "priced",
            },
        }],
    })
    expected = metrics.derive_row(
        snapshot, ledgers, usage, fresh_outcomes, current_now, commits, lines,
        hour_start=old_hour,
    )

    refreshed, updated_count = metrics.remeasure_stale_outcome_rows(
        [stale_row, current_row], ledgers, fresh_outcomes, current_now,
    )

    assert updated_count == 1
    for path in metrics.OUTCOME_TILE_PATHS:
        assert _nested(refreshed[0], path) == _nested(expected, path)
    assert refreshed[0]["metrics"]["A"]["A1"]["value"] is not None
    assert refreshed[0]["metrics"]["A"]["A6"]["reopened_tickets"]["value"] is not None
    assert refreshed[0]["metrics"]["B"]["B1"]["numerator"] is not None
    assert refreshed[0]["metrics"]["C"]["C6"]["value"] is not None
    assert refreshed[0]["metrics"]["D"]["D4"] == previous_d4
    assert refreshed[0]["metrics"]["D"]["D4"]["value"] is None
    assert _without_outcome_tiles(refreshed[0]) == _without_outcome_tiles(previous)
    assert refreshed[1] == current_row


def test_remeasure_keeps_uncovered_and_over_48_hour_rows_unchanged():
    stale_row, _, _, ledgers, _, fresh_outcomes, _, _, _, current_now = _stale_outcome_rows()
    uncovered = copy.deepcopy(stale_row)
    uncovered_records = copy.deepcopy(fresh_outcomes)
    uncovered_records[0]["derived_at"] = "2026-09-23T04:59:59Z"

    unchanged, count = metrics.remeasure_stale_outcome_rows(
        [uncovered], ledgers, uncovered_records, current_now,
    )
    assert count == 0
    assert unchanged == [uncovered]

    old = copy.deepcopy(stale_row)
    beyond_window = current_now + timedelta(hours=49)
    unchanged, count = metrics.remeasure_stale_outcome_rows(
        [old], ledgers, fresh_outcomes, beyond_window,
    )
    assert count == 0
    assert unchanged == [old]


def test_remote_remeasure_writes_rows_with_compare_and_swap(monkeypatch):
    (
        stale_row, current_row, _, ledgers, _, fresh_outcomes,
        _, _, _, current_now,
    ) = _stale_outcome_rows()
    source_rows = [stale_row, current_row]
    writes = []

    monkeypatch.setattr(
        metrics, "_read_remote",
        lambda repo, branch: (copy.deepcopy(source_rows), "blob-sha"),
    )

    def fake_gh(args, stdin=None):
        writes.append((args, stdin))
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(metrics, "_gh", fake_gh)

    updated_count = metrics.remeasure_remote_stale_outcome_rows(
        ledgers, fresh_outcomes, current_now, repo="owner/repo", branch="heartbeat",
    )

    assert updated_count == 1
    assert len(writes) == 1
    args, stdin = writes[0]
    assert "repos/owner/repo/contents/metrics.jsonl" in args
    payload = json.loads(stdin)
    assert payload["sha"] == "blob-sha"
    written_rows = metrics._decode_rows(
        base64.b64decode(payload["content"]).decode("utf-8")
    )
    assert written_rows[1] == current_row
    assert written_rows[0]["metrics"]["A"]["A1"]["value"] is not None


def test_old_snapshot_leaves_in_hour_counts_as_gaps():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    snapshot["generated_at"] = "2026-09-23T04:59:59Z"
    snapshot["brief"]["generated_at"] = "2026-09-23T04:59:59Z"
    snapshot["brief"]["unattended_approvals"] = [{"at": "2026-09-23T04:20:00Z"}]
    snapshot["brief"]["unattended_merges"] = [{"merged_at": "2026-09-23T04:30:00Z"}]

    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )

    new_starts = row["metrics"]["A"]["A4"]["new_projects_started"]
    approvals = row["metrics"]["E"]["E3"]["approvals_this_hour"]
    merges = row["metrics"]["E"]["E3"]["merges_this_hour"]
    assert new_starts["value"] is None and new_starts["gap"]
    assert approvals["value"] is None and approvals["gap"]
    assert merges["value"] is None and merges["gap"]


def test_fixture_inputs_imply_dry_run(capsys, monkeypatch):
    monkeypatch.setattr(
        metrics, "append_remote",
        lambda _row: pytest.fail("fixture mode must not append"),
    )

    result = metrics.main([
        "derive",
        "--snapshot", str(FIXTURES / "metrics_snapshot.json"),
        "--outcomes", str(FIXTURES / "metrics_outcomes.jsonl"),
        "--now", "2026-09-23T05:30:00Z",
    ])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["hour"] == "2026-09-23T04:00:00Z"


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


def _git_history_commit(repo, committed_at, run_id, *, fixture_inputs=False):
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(
            ["git", "init", "-b", "heartbeat"], cwd=repo, check=True,
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Metrics Fixture"], cwd=repo,
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "metrics@example.test"], cwd=repo,
            check=True, capture_output=True, text=True,
        )
    for agent in metrics.AGENTS:
        rows = _jsonl("metrics_{}.jsonl".format(agent)) if fixture_inputs else []
        rows.append({"agent": agent, "run": run_id, "phase": "start", "ts": int(NOW.timestamp()) - 7200})
        (repo / "{}.jsonl".format(agent)).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    if fixture_inputs:
        (repo / "snapshot.json").write_text(
            json.dumps(_json("metrics_snapshot.json")), encoding="utf-8"
        )
        (repo / "usage.json").write_text(
            json.dumps(_json("metrics_usage.json")), encoding="utf-8"
        )
        (repo / "outcomes.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in _jsonl("metrics_outcomes.jsonl")),
            encoding="utf-8",
        )
        (repo / "commits.json").write_text(
            json.dumps(_json("metrics_commits.json")), encoding="utf-8"
        )
        (repo / "funnel.py").write_text("line\n", encoding="utf-8")
    else:
        (repo / ".keep").write_text("fixture\n", encoding="utf-8")
    (repo / "sample.txt").write_text(
        "{} {}\n".format(run_id, committed_at.isoformat()), encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
    stamp = committed_at.astimezone(timezone.utc).isoformat()
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = stamp
    env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(
        ["git", "commit", "-m", "fixture {}".format(run_id)], cwd=repo,
        env=env, check=True, capture_output=True, text=True,
    )
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def test_backfill_fixture_history_reuses_derive_and_appends_idempotently(tmp_path, monkeypatch):
    repo = tmp_path / "heartbeat"
    _git_history_commit(
        repo, datetime(2026, 9, 23, 4, 30, tzinfo=timezone.utc), "overlap",
        fixture_inputs=True,
    )
    first_sample_sha = _git_history_commit(
        repo, datetime(2026, 9, 23, 5, 10, tzinfo=timezone.utc), "overlap",
        fixture_inputs=True,
    )
    _git_history_commit(
        repo, datetime(2026, 9, 23, 6, 10, tzinfo=timezone.utc), "overlap",
        fixture_inputs=True,
    )
    monkeypatch.setattr(
        metrics, "_gh",
        lambda *_args, **_kwargs: pytest.fail("C-F history reads must not call the GitHub API"),
    )

    rows, overlap_count = metrics._build_backfill_rows(
        repo,
        "heartbeat",
        datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc),
        datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc),
        derived_at=NOW,
    )

    sample = metrics._load_history_sample(
        repo,
        datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc),
        metrics._HistoryCommit(
            first_sample_sha, datetime(2026, 9, 23, 5, 10, tzinfo=timezone.utc)
        ),
    )
    expected = metrics.derive_row(
        sample.snapshot,
        sample.ledgers,
        sample.usage_readings,
        sample.outcome_records,
        now=sample.sampled_at,
        commit_activity=sample.commit_activity,
        funnel_line_count=sample.funnel_line_count,
        hour_start=sample.hour_start,
        derived_at=NOW,
    )
    assert rows[0] == expected
    assert len(rows) == 2
    assert overlap_count == 1

    appended, count = metrics.append_rows_many([], rows)
    repeated, repeated_count = metrics.append_rows_many(appended, rows)
    assert count == 2
    assert repeated_count == 0
    assert repeated == appended


def test_backfill_rejects_history_stride_that_loses_ledger_overlap(tmp_path):
    repo = tmp_path / "heartbeat-gap"
    _git_history_commit(
        repo, datetime(2026, 9, 23, 4, 30, tzinfo=timezone.utc), "root"
    )
    _git_history_commit(
        repo, datetime(2026, 9, 23, 5, 10, tzinfo=timezone.utc), "first-window"
    )
    _git_history_commit(
        repo, datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc), "skipped-window"
    )

    with pytest.raises(metrics.MetricsError, match="do not overlap"):
        metrics._build_backfill_rows(
            repo,
            "heartbeat",
            datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc),
            derived_at=NOW,
        )


def test_backfill_rejects_unparseable_boundaries():
    with pytest.raises(metrics.MetricsError, match="--start"):
        metrics._parse_backfill_boundary("not-a-time", "start")
    with pytest.raises(metrics.MetricsError, match="--until"):
        metrics._parse_backfill_boundary("not-a-time", "until")


def test_backfill_snapshot_counts_bug_beside_broken_as_funnel_does():
    """The hourly rebuild mirrors funnel.maintenance_load and
    recorded_cause_regressions, which count Bug as a defect (#1845)."""
    import funnel

    start = NOW.replace(minute=0)
    inside = start + timedelta(minutes=10)

    def project(number, klass, **values):
        return funnel.Item(
            repo="nateprich-projects/command-center", number=number,
            title="Project {}".format(number), url="", klass=klass,
            created_at=inside, **values)

    items = [
        project(1, "Bug", state="CLOSED", status="Done", closed_at=inside),
        project(2, "Broken", state="OPEN", status="Ready"),
        project(3, "New", state="CLOSED", status="Done", closed_at=inside),
    ]

    brief = metrics._github_snapshot(
        items, None, start, start + timedelta(hours=1), NOW)["brief"]

    assert brief["maintenance_load"]["closed_in_window"] == 2
    assert brief["maintenance_load"]["upkeep_projects"] == 1
    assert brief["recorded_cause_regressions"]["broken_projects"] == 2


def test_series_builds_daily_rollups_and_weighted_rate_windows(capsys):
    fixture = FIXTURES / "metrics_series.jsonl"

    result = metrics.main([
        "series", "--rows", str(fixture), "--now", "2026-09-24T23:00:00Z",
    ])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["as_of"] == "2026-09-24"
    assert payload["start_date"] == "2026-06-27"
    assert len(payload["days"]) == 90

    tickets = payload["metrics"]["A"]["A1"]["total"]
    assert tickets["daily"][-28:] == list(range(1, 29))
    assert tickets["r7"][-1] == 25
    assert tickets["r28"][-1] == 14.5
    assert tickets["delta"][-1] == 10.5

    share = payload["metrics"]["A"]["A2"]
    assert share["numerators"][-7:] == [1] * 7
    assert share["denominators"][-7:] == [1, 1, 1, 1, 1, 1, 10]
    assert share["r7"][-1] == 0.4375
    assert share["r28"][-1] == 0.49
    assert share["delta"][-1] == -0.0525

    gapped = payload["metrics"]["A"]["A3"]
    gap_index = payload["days"].index("2026-09-20")
    assert gapped["daily"][gap_index] is None
    assert gapped["r7"][-1] is None


def test_series_accepts_the_complete_derived_hourly_schema():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    row = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines
    )
    newer_row = dict(row, hour="2026-09-23T05:00:00Z")
    newer_metrics = {group: dict(values) for group, values in row["metrics"].items()}
    newer_metrics["D"].pop("D4")
    newer_row["metrics"] = newer_metrics

    payload = metrics.series_from_rows([row, newer_row], NOW)

    assert payload["metrics"]["A"]["A2"]["kind"] == "rate"
    done = payload["metrics"]["C"]["C1"]["by_agent"]["codex"]["done"]
    assert done["daily"][-1] == 2
    reset = payload["metrics"]["D"]["D1"]["window_resets_at"]
    assert reset["kind"] == "category"
    assert reset["daily"][-1] == row["metrics"]["D"]["D1"]["window_resets_at"]["value"]
    cost = payload["metrics"]["D"]["D4"]
    assert cost["daily"][-1] is None
    assert cost["gap"] == row["metrics"]["D"]["D4"]["gap"]
    points_per_run = payload["metrics"]["D"]["D5"]["graphql_points_per_run"]["codex"]["points_per_run"]
    assert points_per_run["kind"] == "rate"
    assert points_per_run["daily"][-1] == 5


def test_b3_reads_the_fix_recurrence_measurement():
    """#1685: Panel B3 is fix-on-fix from git, not capture markers."""
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    result = {
        "numerator": 15, "denominator": 88, "share": 0.17,
        "hotspots": [{"path": "funnel.py", "function": "cmd_begin",
                      "count": 9, "projects": [1, 2], "extra": "dropped"}],
    }

    b3 = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines,
        fix_recurrence_result=result,
    )["metrics"]["B"]["B3"]

    assert (b3["numerator"], b3["denominator"]) == (15, 88)
    assert "gap" not in b3
    assert b3["source"] == metrics.FIX_RECURRENCE_SOURCE
    assert b3["hotspots"] == [{"path": "funnel.py", "function": "cmd_begin",
                               "count": 9, "projects": [1, 2]}]


@pytest.mark.parametrize("result, reason", [
    (None, "not measured"),
    ({"gap": "fix recurrence could not be measured: no broken_fix_tickets"},
     "no broken_fix_tickets"),
    ({"numerator": 0, "denominator": 0}, "no denominator"),
])
def test_b3_is_a_gap_never_zero_without_a_measurement(result, reason):
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()

    b3 = metrics.derive_row(
        snapshot, ledgers, usage, outcomes, NOW, commits, lines,
        fix_recurrence_result=result,
    )["metrics"]["B"]["B3"]

    assert b3["numerator"] is None and b3["denominator"] is None
    assert reason in b3["gap"]


def test_measure_fix_recurrence_reports_a_missing_field_as_a_gap():
    result = metrics.measure_fix_recurrence(
        {"brief": {"recorded_cause_regressions": {}}}, NOW)

    assert "broken_fix_tickets" in result["gap"]


def test_derive_cli_reads_a_fix_recurrence_fixture(tmp_path, capsys):
    fixture = tmp_path / "fix.json"
    fixture.write_text(json.dumps({"numerator": 1, "denominator": 4}))

    code = metrics.main([
        "derive", "--snapshot", str(FIXTURES / "metrics_snapshot.json"),
        "--fix-recurrence", str(fixture), "--now", NOW.isoformat(),
    ])

    assert code == 0
    row = json.loads(capsys.readouterr().out)
    assert (row["metrics"]["B"]["B3"]["numerator"],
            row["metrics"]["B"]["B3"]["denominator"]) == (1, 4)


def _git_repo_with_fix_on_fix(root):
    """Two Broken fixes for different projects, the second rewriting the first."""
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")

    def commit(text, subject, when):
        (root / "app.py").write_text("def run():\n    a = {}\n".format(text))
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S+0000")
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        subprocess.run(["git", "-C", str(root), "add", "app.py"], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", subject],
                       check=True, env=env)

    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    commit(1, "Initial", NOW - timedelta(days=30))
    commit(2, "Fix (#11) (#101)", NOW - timedelta(days=2))
    commit(3, "Fix again (#21) (#102)", NOW - timedelta(days=1))


def test_measure_fix_recurrence_reads_the_snapshot_tickets_against_git(tmp_path):
    """#1685 Accept: a fixture hour with the field yields the expected pair."""
    repo = tmp_path / "repo"
    _git_repo_with_fix_on_fix(repo)
    snapshot = {"brief": {"recorded_cause_regressions": {"broken_fix_tickets": [
        {"ticket": 11, "project": 10}, {"ticket": 21, "project": 20}]}}}

    result = metrics.measure_fix_recurrence(snapshot, NOW, repo=repo)
    b3 = metrics._fix_recurrence_pair(result)

    assert (b3["numerator"], b3["denominator"]) == (1, 2)


def test_live_derive_measures_at_the_hour_end_with_the_snapshot(monkeypatch, capsys):
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    calls = []

    def fake_measure(snap, at, repo=None):
        calls.append((snap, at))
        return {"numerator": 2, "denominator": 5}

    monkeypatch.setattr(metrics, "_read_snapshot", lambda path: snapshot)
    monkeypatch.setattr(metrics, "_live_inputs",
                        lambda now: (ledgers, usage, outcomes, commits, lines))
    monkeypatch.setattr(metrics, "measure_fix_recurrence", fake_measure)

    assert metrics.main(["derive", "--dry-run", "--now", NOW.isoformat()]) == 0

    row = json.loads(capsys.readouterr().out)
    assert calls == [(snapshot, metrics._interval(NOW)[1])]
    assert (row["metrics"]["B"]["B3"]["numerator"],
            row["metrics"]["B"]["B3"]["denominator"]) == (2, 5)


def test_live_derive_appends_before_remeasuring_recent_rows(monkeypatch, capsys):
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    calls = []

    monkeypatch.setattr(metrics, "_read_snapshot", lambda path: snapshot)
    monkeypatch.setattr(
        metrics, "_live_inputs",
        lambda now: (ledgers, usage, outcomes, commits, lines),
    )
    monkeypatch.setattr(metrics, "measure_fix_recurrence", lambda snapshot, at: None)

    def fake_append(row):
        calls.append(("append", row))
        return 1

    def fake_remeasure(actual_ledgers, records, now):
        calls.append(("remeasure", actual_ledgers, records, now))
        return 2

    monkeypatch.setattr(metrics, "append_remote", fake_append)
    monkeypatch.setattr(metrics, "remeasure_remote_stale_outcome_rows", fake_remeasure)

    assert metrics.main(["derive", "--now", NOW.isoformat()]) == 0

    output = json.loads(capsys.readouterr().out)
    assert [call[0] for call in calls] == ["append", "remeasure"]
    assert calls[0][1]["hour"] == "2026-09-23T04:00:00Z"
    assert calls[1][1:] == (ledgers, outcomes, NOW)
    assert output == {
        "hour": "2026-09-23T04:00:00Z", "appended": 1, "remeasured": 2,
    }


def test_series_never_blends_b3_rows_from_the_retired_definition():
    snapshot, ledgers, usage, outcomes, commits, lines = _inputs()
    day = datetime(2026, 9, 20, tzinfo=timezone.utc)

    def row(hour, b3):
        built = metrics.derive_row(snapshot, ledgers, usage, outcomes,
                                   day + timedelta(hours=hour, minutes=30),
                                   commits, lines, fix_recurrence_result=b3)
        return built

    old_style = [row(h, None) for h in range(1, 25)]
    for item in old_style:
        item["metrics"]["B"]["B3"] = {
            "numerator": 3, "denominator": 237,
            "source": "brief.recorded_cause_regressions.with_recorded_cause/broken_projects",
        }
    new_day = [row(h, {"numerator": 16, "denominator": 93})
               for h in range(25, 49)]

    series = metrics.series_from_rows(old_style + new_day,
                                      day + timedelta(days=2, hours=1))
    b3 = series["metrics"]["B"]["B3"]
    days = series["days"]

    assert b3["daily"][days.index("2026-09-20")] is None
    assert b3["daily"][days.index("2026-09-21")] == pytest.approx(16 / 93)
