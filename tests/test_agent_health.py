"""The funnel's watchdog condition reader uses the shared assessment."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from agent_health import assess  # noqa: E402


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
SILENCE_FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "heartbeat_silence_window.json"
ALREADY_MERGED_FINISH = (
    pathlib.Path(__file__).parent / "fixtures" /
    "already_merged_done_finish.json"
)
NO_DIFF_FINISH = (
    pathlib.Path(__file__).parent / "fixtures" /
    "no_diff_done_finish.json"
)


def _silence_fixture():
    payload = json.loads(SILENCE_FIXTURE.read_text())
    now = datetime.fromisoformat(payload["now"].replace("Z", "+00:00"))
    rows = {
        agent: [
            dict(row, ts=datetime.fromisoformat(
                row["ts"].replace("Z", "+00:00")
            ).timestamp())
            for row in agent_rows
        ]
        for agent, agent_rows in payload["agents"].items()
    }
    return now, rows


def _start(run, minutes_ago):
    return {
        "run": run,
        "phase": "start",
        "ts": NOW.timestamp() - minutes_ago * 60,
        "agent": "codex",
    }


def _finish(run, minutes_ago):
    return {
        "run": run,
        "phase": "finish",
        "ts": NOW.timestamp() - minutes_ago * 60,
        "agent": "codex",
        "outcome": "done",
    }


def _error_rows():
    return [
        {
            "run": str(i),
            "phase": "finish",
            "ts": NOW.timestamp() - (i + 1) * 3600,
            "agent": "muse",
            "outcome": "errored",
            "error_class": "regression",
            "note": "boom {}".format(i),
            "runtime": {"head": "0123456789ab"},
        }
        for i in range(3)
    ]


def test_brief_lists_regressions_with_the_runtime_head(monkeypatch):
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"muse": "meta"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: _error_rows())

    assert funnel.agent_health(NOW) == [{
        "agent": "muse",
        "condition": (
            "`muse` had 3 regression errors this week. Latest: "
            "head 0123456789ab: boom 0; head 0123456789ab: boom 1; "
            "head 0123456789ab: boom 2"
        ),
    }]


def test_brief_omits_floor_and_unclassified_errors(monkeypatch):
    rows = [
        dict(row, error_class="floor")
        for row in _error_rows()[:2]
    ] + [dict(_error_rows()[2], error_class="unclassified")]
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"muse": "meta"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)

    assert funnel.agent_health(NOW) == []


def test_healthy_heartbeat_rows_render_no_agent_health(monkeypatch):
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"muse": "meta"})
    monkeypatch.setattr(
        heartbeat,
        "read",
        lambda agent: [{
            "run": "healthy",
            "phase": "finish",
            "ts": NOW.timestamp() - 3600,
            "agent": "muse",
            "outcome": "nothing-to-do",
        }],
    )

    assert funnel.agent_health(NOW) == []


def test_already_merged_done_finish_stays_healthy_with_its_observation(
        monkeypatch):
    finish = json.loads(ALREADY_MERGED_FINISH.read_text())
    now = datetime.fromtimestamp(finish["ts"] + 60, timezone.utc)
    monkeypatch.setattr(heartbeat, "PROVIDERS", {finish["agent"]: "zai"})
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset())
    monkeypatch.setattr(heartbeat, "read", lambda agent: [finish])

    assert finish["outcome"] == "done"
    assert finish["review_result"] == "approved"
    assert "observed already-merged PR" in finish["note"]
    assert (
        "reviewed PR #7 in nateprich-projects/command-center at "
        "abc123def456: approved"
    ) in finish["note"]
    observed = json.loads(
        finish["note"].split("observed already-merged PR: ", 1)[1]
    )
    assert observed == {
        "actor": "nate",
        "head": "abc123def456",
        "merged_at": "2026-09-25T02:43:19Z",
        "pr": 7,
    }
    assert finish["merged"] == observed["pr"]
    assert funnel.agent_health(now) == []


def test_no_diff_done_finish_stays_done_and_healthy_with_its_note(monkeypatch):
    finish = json.loads(NO_DIFF_FINISH.read_text())
    start = {
        "run": finish["run"],
        "phase": "start",
        "ts": finish["ts"] - 60,
        "agent": finish["agent"],
    }
    rows = [start, finish]
    now = datetime.fromtimestamp(finish["ts"] + 60, timezone.utc)
    monkeypatch.setattr(heartbeat, "PROVIDERS", {finish["agent"]: "openai"})
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset())
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)

    assert finish["outcome"] == "done"
    assert finish["note"].startswith(
        "no-diff close as completed; verified evidence: "
    )
    assert funnel.agent_run_summary(now) == [{
        "agent": "codex",
        "starts": 1,
        "finishes": 1,
        "re_begins": 0,
    }]
    assert funnel.agent_health(now) == []


def test_a_lane_held_by_a_tight_budget_raises_no_condition():
    """#1199: a tight-budget stop finishes every fire as `skipped-over-pace`
    with begin's numbers in the note, so the lane is neither silent nor
    erroring. #1160 is what it looks like when a hold writes nothing."""
    rows = []
    for index in range(12):
        minutes_ago = 5 + index * 10
        rows.append(_start("hold-{}".format(index), minutes_ago + 1))
        finish = _finish("hold-{}".format(index), minutes_ago)
        finish["outcome"] = "skipped-over-pace"
        finish["note"] = "tight: 62% used, projected 118% at $31/day"
        rows.append(finish)
    rows.sort(key=lambda row: row["ts"])

    assert assess("muse", rows, NOW.timestamp()) == []


def test_brief_run_summary_separates_rebegins_from_finishes(monkeypatch):
    rows = [
        _start("old", 4),
        {
            "run": "old",
            "phase": "finish",
            "ts": NOW.timestamp() - 3 * 60,
            "agent": "codex",
            "outcome": "skipped-blocked",
            "re_begun_by": "fresh",
        },
        _start("fresh", 2),
        _finish("fresh", 1),
    ]
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"muse": "meta"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)

    assert funnel.agent_run_summary(NOW) == [{
        "agent": "muse",
        "starts": 2,
        "finishes": 1,
        "re_begins": 1,
    }]


def test_brief_run_summary_includes_muse_window_consumption(monkeypatch):
    reset = 1_800_000_000.0
    rows = [
        {
            "run": "muse-run",
            "phase": "start",
            "ts": NOW.timestamp() - 120,
            "agent": "muse",
            "usage": {"seven_day": {
                "resets_at": reset,
                "spent_dollars": 7.5,
            }},
        },
        {
            "run": "muse-run",
            "phase": "finish",
            "ts": NOW.timestamp() - 60,
            "agent": "muse",
            "outcome": "done",
            "usage": {"seven_day": {
                "resets_at": reset,
                "spent_dollars": 9.0,
            }},
        },
    ]
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"muse": "meta"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)

    assert funnel.agent_run_summary(NOW) == [{
        "agent": "muse",
        "starts": 1,
        "finishes": 1,
        "re_begins": 0,
        "window_consumption": [{
            "resets_at": reset,
            "consumed_dollars": 1.5,
            "runs": 1,
        }],
    }]


def test_retired_prompt_events_raise_no_condition():
    """`prompt-drift` and `prompt-mismatch` left the outcome vocabulary with
    `--routine-sha` (#821). Old rows may still sit in the heartbeat history;
    they raise nothing."""
    rows = [
        {
            "run": "drift",
            "phase": "event",
            "ts": NOW.timestamp() - 2 * 3600,
            "agent": "codex",
            "outcome": "prompt-drift",
        },
        {
            "run": "mismatch",
            "phase": "event",
            "ts": NOW.timestamp() - 3600,
            "agent": "codex",
            "outcome": "prompt-mismatch",
        },
    ]

    assert assess("codex", rows, NOW.timestamp()) == []


def test_sparse_history_uses_the_absolute_silence_floor_and_reaches_the_brief(
    monkeypatch, capsys,
):
    now, rows = _silence_fixture()
    silent = rows["codex"]

    conditions = assess("muse", silent, now.timestamp())

    assert len(conditions) == 1
    assert "absolute silence floor 6h exceeded" in conditions[0]
    assert "Nothing recorded for 1d8h1m" in conditions[0]
    assert "normal gap" not in conditions[0]
    # The retired agent stays quiet on the same silence. zcode, since Codex
    # came back off the retired list (#1325). zcode is live again as the z.ai
    # standard tier until `heartbeat.ZAI_STANDARD_UNTIL`, so the control is
    # pinned rather than read from the clock.
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset({"zcode"}))
    assert assess("zcode", silent, now.timestamp()) == []

    monkeypatch.setattr(heartbeat, "PROVIDERS", {
        "zcode": heartbeat.PROVIDERS["zcode"],
        "muse": "meta",
    })
    monkeypatch.setattr(
        funnel, "_brief_heartbeat_rows",
        lambda agent: silent if agent in ("zcode", "muse") else [],
    )
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    item = funnel.Item(
        repo="nateprich/beta", number=60, title="A quiet project",
        url="https://example.invalid/60", state="OPEN", status="Building",
        klass="Improve",
    )
    assert funnel.cmd_brief([item], now) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["agent_health"] == [{
        "agent": "muse",
        "condition": conditions[0],
    }]


def test_one_open_start_older_than_ten_times_median_is_reported():
    rows = [
        _start("completed", 2),
        _finish("completed", 1.7),
        _start("hung", 16),
    ]

    conditions = assess("codex", rows, NOW.timestamp())

    assert len(conditions) == 1
    assert "one open start `hung`" in conditions[0]
    assert "aged 16m" in conditions[0]
    assert "median completed-run length 18s" in conditions[0]


def test_one_open_start_under_the_floor_is_not_reported():
    rows = [
        _start("completed", 2),
        _finish("completed", 1.7),
        _start("working", 5),
    ]

    assert assess("codex", rows, NOW.timestamp()) == []


def test_finished_start_is_not_reported_as_open():
    rows = [
        _start("completed", 16),
        _finish("completed", 15.7),
    ]

    assert assess("codex", rows, NOW.timestamp()) == []


def test_three_open_starts_keep_the_dying_condition():
    rows = [
        _start("one", 180),
        _start("two", 150),
        _start("three", 130),
    ]

    conditions = assess("codex", rows, NOW.timestamp())

    assert len(conditions) == 1
    assert "started and never finished" in conditions[0]
    assert "Check whether the reserves in `usage.py` are too low." in conditions[0]


def test_begin_timeout_finishes_are_classified_from_the_record():
    rows = [
        {
            "run": "begin-timeout-{}".format(index),
            "phase": "finish",
            "ts": NOW.timestamp() - index * 60,
            "agent": "muse",
            "outcome": "errored",
            "error_class": "begin-timeout",
            "note": (
                "funnel begin failed (exit 2): reply-timeout: FUNNEL_SESSION "
                "busy past the 180s reply budget (slow command: begin)"
            ),
        }
        for index in range(1, 4)
    ]

    conditions = assess("muse", rows, NOW.timestamp())

    assert len(conditions) == 1
    assert "3 begin-timeout errors this week" in conditions[0]
    assert "slow command: begin" in conditions[0]
    assert "usage.py" not in conditions[0]


# -- provider park (#1172) --------------------------------------------------


def _muse_rows(count, first_minutes_ago, gap_minutes):
    """A cadence history dense enough for the p90 inference to fire."""
    return [
        {
            "run": "r{}".format(i),
            "phase": "finish",
            "ts": NOW.timestamp() - (first_minutes_ago + i * gap_minutes) * 60,
            "agent": "muse",
            "outcome": "done",
        }
        for i in range(count)
    ]


def _write_hold(tmp_path, moment):
    path = tmp_path / "muse-quota-hold"
    path.write_text(moment.isoformat().replace("+00:00", "Z") + "\n")
    return str(path)


def test_a_future_hold_reports_parked_instead_of_silence():
    """The lane is not dead, it is waiting for a window to reset."""
    rows = _muse_rows(12, first_minutes_ago=600, gap_minutes=10)
    hold_until = NOW.timestamp() + 3600

    conditions = assess("muse", rows, NOW.timestamp(), hold_until=hold_until)

    silence = [c for c in conditions if "Nothing recorded for" in c]
    assert silence == []
    parked = [c for c in conditions if "parked until" in c]
    assert len(parked) == 1
    assert "provider quota" in parked[0]
    # The last record before the park stays visible.
    assert "<t:{}:f>".format(int(rows[0]["ts"])) in parked[0]


def test_an_expired_hold_returns_the_normal_alarm():
    rows = _muse_rows(12, first_minutes_ago=600, gap_minutes=10)
    hold_until = NOW.timestamp() - 10 * 3600

    conditions = assess("muse", rows, NOW.timestamp(), hold_until=hold_until)

    assert [c for c in conditions if "parked until" in c] == []
    assert [c for c in conditions if "Nothing recorded for" in c]


def test_a_just_expired_hold_measures_silence_from_the_reset():
    """A lane that could not run was not quiet: the gap starts at the reset."""
    rows = _muse_rows(12, first_minutes_ago=600, gap_minutes=10)
    hold_until = NOW.timestamp() - 60

    conditions = assess("muse", rows, NOW.timestamp(), hold_until=hold_until)

    assert [c for c in conditions if "Nothing recorded for" in c] == []


def test_no_hold_reads_exactly_as_before():
    rows = _muse_rows(12, first_minutes_ago=600, gap_minutes=10)

    assert assess("muse", rows, NOW.timestamp(), hold_until=None) == assess(
        "muse", rows, NOW.timestamp()
    )
    assert [
        c for c in assess("muse", rows, NOW.timestamp())
        if "Nothing recorded for" in c
    ]


def test_a_future_hold_also_silences_the_absolute_floor():
    """Sparse history has its own alarm; a park explains that gap too."""
    rows = [{
        "run": "only",
        "phase": "finish",
        "ts": NOW.timestamp() - 30 * 3600,
        "agent": "muse",
        "outcome": "done",
    }]
    hold_until = NOW.timestamp() + 1800

    conditions = assess("muse", rows, NOW.timestamp(), hold_until=hold_until)

    assert [c for c in conditions if "absolute silence floor" in c] == []
    assert [c for c in conditions if "parked until" in c]


def test_a_park_does_not_silence_the_errored_runs_condition():
    """It explains a gap in records, not runs that failed."""
    conditions = assess(
        "muse", _error_rows(), NOW.timestamp(),
        hold_until=NOW.timestamp() + 3600,
    )

    assert [c for c in conditions if "errored" in c]


def test_the_hold_reader_returns_the_stamp_and_never_removes_the_file(tmp_path):
    from datetime import timedelta

    import agent_health

    moment = NOW + timedelta(hours=2)
    path = _write_hold(tmp_path, moment)

    assert agent_health.quota_hold_until(path) == moment.timestamp()
    assert pathlib.Path(path).exists()


def test_an_expired_stamp_is_returned_rather_than_dropped(tmp_path):
    from datetime import timedelta

    import agent_health

    moment = NOW - timedelta(hours=2)
    path = _write_hold(tmp_path, moment)

    assert agent_health.quota_hold_until(path) == moment.timestamp()


def test_a_missing_or_corrupt_hold_reads_as_no_hold(tmp_path):
    import agent_health

    assert agent_health.quota_hold_until(str(tmp_path / "absent")) is None

    empty = tmp_path / "empty"
    empty.write_text("   \n")
    assert agent_health.quota_hold_until(str(empty)) is None

    corrupt = tmp_path / "corrupt"
    corrupt.write_text("not a timestamp\n")
    assert agent_health.quota_hold_until(str(corrupt)) is None
    # Unreadable is not a reason to destroy the evidence.
    assert corrupt.exists()


def test_the_hold_path_follows_the_runners_environment(monkeypatch, tmp_path):
    import agent_health

    monkeypatch.setenv(agent_health.QUOTA_HOLD_ENV, str(tmp_path / "elsewhere"))
    assert agent_health.quota_hold_path() == str(tmp_path / "elsewhere")

    monkeypatch.delenv(agent_health.QUOTA_HOLD_ENV, raising=False)
    assert agent_health.quota_hold_path().endswith(
        "command-center-muse-quota-hold"
    )


def test_the_brief_reads_the_hold_only_for_muse(monkeypatch):
    import agent_health

    seen = []

    def reader(path=None):
        seen.append(path)
        return NOW.timestamp() + 3600

    monkeypatch.setattr(agent_health, "quota_hold_until", reader)
    monkeypatch.setattr(funnel, "_brief_heartbeat_rows", lambda agent: (
        _muse_rows(12, first_minutes_ago=600, gap_minutes=10)
        if agent == "muse" else []
    ))

    rows = funnel.agent_health(NOW)

    assert len(seen) == 1
    assert [row["agent"] for row in rows] == ["muse"]
    assert "parked until" in rows[0]["condition"]


# -- counts are of runs, not of rows (#1225, #1239) -------------------------


def _duplicated(row, times=3):
    """The measured duplicate shape: byte-identical, same ts."""
    return [dict(row) for _ in range(times)]


def test_an_errored_run_written_three_times_counts_once():
    """agent_health read 67 muse errors against 55 true runs (2026-09-21)."""
    rows = []
    for index in range(3):
        rows += _duplicated({
            "run": "r{}".format(index),
            "phase": "finish",
            "ts": NOW.timestamp() - (index + 1) * 3600,
            "agent": "muse",
            "outcome": "errored",
            "note": "boom",
        })

    conditions = assess("muse", rows, NOW.timestamp())

    errored = [c for c in conditions if "errored" in c]
    assert len(errored) == 1
    assert "3" in errored[0]
    assert "9" not in errored[0]


def test_two_errored_runs_stay_under_the_threshold_however_often_written():
    rows = []
    for index in range(2):
        rows += _duplicated({
            "run": "r{}".format(index),
            "phase": "finish",
            "ts": NOW.timestamp() - (index + 1) * 3600,
            "agent": "muse",
            "outcome": "errored",
        }, times=4)

    assert [c for c in assess("muse", rows, NOW.timestamp())
            if "errored" in c] == []


def test_duplicate_records_do_not_tighten_the_cadence_inference():
    """A record written twice with the same ts is a gap of zero that never
    happened, and it drags the p90 down."""
    clean = _muse_rows(12, first_minutes_ago=600, gap_minutes=10)
    doubled = [dict(row) for row in clean for _ in range(2)]

    assert assess("muse", doubled, NOW.timestamp()) == assess(
        "muse", clean, NOW.timestamp()
    )


def test_one_record_per_run_keeps_the_newest_and_the_unattributed():
    import heartbeat

    rows = [
        {"run": "a", "phase": "finish", "ts": 10, "outcome": "errored"},
        {"run": "a", "phase": "finish", "ts": 20, "outcome": "done"},
        {"phase": "event", "ts": 30, "note": "no run id"},
    ]

    kept = heartbeat.one_record_per_run(rows)

    assert len(kept) == 2
    assert kept[0]["outcome"] == "done"
    assert kept[1]["note"] == "no run id"


def test_distinct_records_keeps_genuinely_different_events():
    import heartbeat

    rows = [
        {"run": "a", "phase": "api_cost", "ts": 10,
         "api_cost": {"gh_calls": 5}},
        {"run": "a", "phase": "api_cost", "ts": 10,
         "api_cost": {"gh_calls": 5}},
        {"run": "a", "phase": "api_cost", "ts": 20,
         "api_cost": {"gh_calls": 7}},
    ]

    assert len(heartbeat.distinct_records(rows)) == 2


def test_a_run_cost_is_not_multiplied_by_duplicate_writes():
    """Per-run costs in #685 and #1125 read up to ~45% high from raw records."""
    import heartbeat

    event = {"run": "a", "phase": "api_cost", "ts": 10,
             "api_cost": {"gh_calls": 12, "graphql_points": 30}}
    once = heartbeat.api_cost_for_run([event], "a")
    thrice = heartbeat.api_cost_for_run([dict(event) for _ in range(3)], "a")

    assert once == thrice
    assert once["gh_calls"] == 12


def test_two_real_commands_in_one_run_still_add_up():
    import heartbeat

    rows = [
        {"run": "a", "phase": "api_cost", "ts": 10,
         "api_cost": {"gh_calls": 12, "graphql_points": 30}},
        {"run": "a", "phase": "api_cost", "ts": 20,
         "api_cost": {"gh_calls": 8, "graphql_points": 10}},
    ]

    assert heartbeat.api_cost_for_run(rows, "a")["gh_calls"] == 20
