"""Fixture-driven hourly metrics row and append-contract tests."""

from __future__ import annotations

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
