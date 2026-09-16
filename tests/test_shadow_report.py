"""Fixture coverage for the review-engine shadow-period report."""

from __future__ import annotations

import json
import pathlib

from engine import shadow_report


ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "shadow_heartbeat.json"


def _records():
    return json.loads(FIXTURE.read_text())["records"]


def test_same_muse_stream_splits_shadow_and_live_and_pairs_prs():
    report = shadow_report.build_report(_records(), now=1000, window_seconds=200)

    assert report["jobs"] == {
        "shadow": 3,
        "live": 2,
        "matched": 2,
        "compared": 2,
    }
    assert report["agreement"] == {
        "agree": 2,
        "disagree": 0,
        "rate": 1.0,
        "approved": {"shadow": 1, "live": 1},
        "rejected": {"shadow": 1, "live": 1},
    }


def test_malformed_rate_is_separate_and_duration_percentiles_are_measured():
    report = shadow_report.build_report(_records(), now=1000, window_seconds=200)

    assert report["malformed_output"] == {
        "shadow": {"count": 1, "rate": 1 / 3},
        "live": {"count": 0, "rate": 0.0},
    }
    assert report["time_per_job"]["shadow"] == {
        "jobs": 3,
        "measured": 3,
        "median_seconds": 30,
        "p90_seconds": 38.0,
    }
    assert report["time_per_job"]["live"] == {
        "jobs": 2,
        "measured": 2,
        "median_seconds": 27.5,
        "p90_seconds": 33.5,
    }


def test_window_reports_full_data_floor_without_changing_nominal_window():
    report = shadow_report.build_report(_records(), now=1000, window_seconds=200)

    assert report["window"] == {
        "since": 800.0,
        "until": 1000.0,
        "seconds": 200.0,
        "data_since": 790.0,
        "truncated": False,
    }


def test_window_reports_truncated_data_floor():
    records = [row for row in _records() if row["ts"] >= 850]

    report = shadow_report.build_report(records, now=1000, window_seconds=200)

    assert report["window"] == {
        "since": 800.0,
        "until": 1000.0,
        "seconds": 200.0,
        "data_since": 850.0,
        "truncated": True,
    }


def test_empty_window_data_shape_is_not_marked_truncated():
    report = shadow_report.build_report([], now=1000, window_seconds=200)

    assert report["window"] == {
        "since": 800.0,
        "until": 1000.0,
        "seconds": 200.0,
        "data_since": None,
        "truncated": False,
    }


def test_separate_streams_accept_structured_review_results():
    shadow = [
        {"run": "s", "phase": "start", "ts": 10},
        {"run": "s", "phase": "bind", "ts": 11, "do": "review", "work": "7"},
        {"run": "s", "phase": "finish", "ts": 20,
         "review_result": "approved", "outcome": "done"},
    ]
    live = [
        {"run": "l", "phase": "start", "ts": 12},
        {"run": "l", "phase": "bind", "ts": 13, "do": "review", "work": "7"},
        {"run": "l", "phase": "finish", "ts": 30,
         "review_result": "approved", "outcome": "done"},
    ]

    report = shadow_report.build_report(
        shadow, live, now=40, window_seconds=40
    )

    assert report["jobs"]["matched"] == 1
    assert report["agreement"]["rate"] == 1.0


def test_recorded_live_verdict_recovers_a_finish_without_note_prose():
    shadow = [
        {"run": "shadow", "phase": "start", "ts": 10},
        {"run": "shadow", "phase": "bind", "ts": 11,
         "do": "review", "work": "42", "repo": "owner/repo"},
        {"run": "shadow", "phase": "finish", "ts": 20,
         "note": "shadow review of PR #42 in owner/repo at abc123: approved",
         "outcome": "done"},
    ]
    live = [
        {"run": "live", "phase": "start", "ts": 12},
        {"run": "live", "phase": "bind", "ts": 13,
         "do": "review", "work": "42", "repo": "owner/repo"},
        {"run": "live", "phase": "finish", "ts": 30,
         "note": "merged PR #42", "outcome": "done"},
    ]

    report = shadow_report.build_report(
        shadow, live, now=40, window_seconds=40,
        live_verdicts={"live": {"verdict": "approved",
                                "head_sha": "abc123"}},
    )

    assert report["jobs"] == {
        "shadow": 1, "live": 1, "matched": 1, "compared": 1,
    }
    assert report["agreement"] == {
        "agree": 1,
        "disagree": 0,
        "rate": 1.0,
        "approved": {"shadow": 1, "live": 1},
        "rejected": {"shadow": 0, "live": 0},
    }


def test_missing_or_wrong_head_live_verdict_stays_out_of_denominator():
    shadow = [
        {"run": "shadow", "phase": "start", "ts": 10},
        {"run": "shadow", "phase": "bind", "ts": 11,
         "do": "review", "work": "42", "repo": "owner/repo"},
        {"run": "shadow", "phase": "finish", "ts": 20,
         "note": "shadow review of PR #42 at abc123: approved",
         "outcome": "done"},
    ]
    live = [
        {"run": "live", "phase": "start", "ts": 12},
        {"run": "live", "phase": "bind", "ts": 13,
         "do": "review", "work": "42", "repo": "owner/repo"},
        {"run": "live", "phase": "finish", "ts": 30,
         "head_sha": "abc123", "note": "merged PR #42", "outcome": "done"},
    ]

    report = shadow_report.build_report(
        shadow, live, now=40, window_seconds=40,
        live_verdicts={"live": {"verdict": "approved",
                                "head_sha": "different"}},
    )
    assert report["jobs"]["matched"] == 1
    assert report["jobs"]["compared"] == 0
    assert report["agreement"]["rate"] is None


def test_fetch_live_verdicts_requires_a_verdict_at_the_current_head():
    jobs = [{"run": "live", "key": "pr#42", "repo": "owner/repo",
             "pr": 42}]
    seen = []

    def read_verdict(repo, pr):
        seen.append(("verdict", repo, pr))
        return {"verdict": "rejected", "head_sha": "full-head"}

    def read_head(repo, pr):
        seen.append(("head", repo, pr))
        return "full-head"

    found = shadow_report.fetch_live_verdicts(
        jobs, latest_verdict=read_verdict, current_head=read_head,
    )

    assert found == {
        "live": {"verdict": "rejected", "head_sha": "full-head"},
    }
    assert seen == [
        ("verdict", "owner/repo", 42), ("head", "owner/repo", 42),
    ]


def test_window_bounds_are_inclusive_and_inverted_windows_fail():
    rows = [
        {"run": "s", "phase": "start", "ts": 10},
        {"run": "s", "phase": "bind", "ts": 11, "do": "review", "work": "7"},
        {"run": "s", "phase": "finish", "ts": 20,
         "review_result": "rejected", "outcome": "done"},
    ]

    report = shadow_report.build_report(rows, list(rows), since=20, until=20)
    assert report["jobs"] == {"shadow": 1, "live": 1, "matched": 1, "compared": 1}

    try:
        shadow_report.window_bounds(since=3, until=2)
    except ValueError as exc:
        assert "starts after" in str(exc)
    else:
        raise AssertionError("an inverted report window must fail")


def _issue_job(run, kind, finished, note, *, target="owner/repo#103"):
    return [
        {"run": run, "phase": "start", "ts": finished - 20},
        {"run": run, "phase": "bind", "ts": finished - 19,
         "do": kind, "work": target},
        {"run": run, "phase": "finish", "ts": finished,
         "note": note, "outcome": "done"},
    ]


def test_breakdown_shape_mode_compares_breakdowns_and_keeps_shapes_population_only():
    records = (
        _issue_job(
            "shadow-breakdown", "breakdown", 820,
            "shadow breakdown of owner/repo#103: 2 tickets; answer: "
            + json.dumps({"needs_decision": None,
                          "tickets": [{}, {}]}),
        )
        + _issue_job(
            "live-breakdown", "breakdown", 830,
            "broke down owner/repo#103: created 2 tickets",
        )
        + _issue_job(
            "shadow-shape", "shape", 840,
            "shadow shape of owner/repo#104: Ready",
            target="owner/repo#104",
        )
        + _issue_job(
            "live-shape", "shape", 850,
            "shaped owner/repo#104: Ready",
            target="owner/repo#104",
        )
    )

    report = shadow_report.build_report(
        records, now=1000, window_seconds=200, mode="breakdown-shape"
    )

    assert report["window"] == {
        "since": 800.0,
        "until": 1000.0,
        "seconds": 200.0,
        "data_since": 800.0,
        "truncated": False,
    }
    assert report["breakdown_shape_agreement"] == {
        "agree": 1,
        "disagree": 0,
        "rate": 1.0,
        "jobs": {"shadow": 2, "live": 2, "matched": 2, "compared": 1},
        "malformed_output": {
            "shadow": {"count": 0, "rate": 0.0},
            "live": {"count": 0, "rate": 0.0},
        },
        "time_per_job": {
            "shadow": {
                "jobs": 2, "measured": 2,
                "median_seconds": 20, "p90_seconds": 20.0,
            },
            "live": {
                "jobs": 2, "measured": 2,
                "median_seconds": 20, "p90_seconds": 20.0,
            },
        },
    }


def _breakdown_shadow_job(run, finished, count, question=None,
                          *, target="owner/repo#103", answer=True):
    note = "shadow breakdown of {}: {} tickets".format(target, count)
    if answer:
        note += "; answer: " + json.dumps({
            "needs_decision": question,
            "tickets": [{} for _ in range(count)],
        })
    return _issue_job(run, "breakdown", finished, note, target=target)


def _breakdown_live_job(run, finished, count, question=None,
                        *, target="owner/repo#103", structured=False):
    rows = _issue_job(
        run, "breakdown", finished,
        "broke down {}: created {} ticket{}".format(
            target, count, "" if count == 1 else "s"),
        target=target,
    )
    if structured:
        rows[-1]["created"] = [{} for _ in range(count)]
        rows[-1]["needs_decision"] = question
    return rows


def test_breakdown_ticket_counts_allow_one_and_disagree_past_one():
    records = []
    for index, live_count in enumerate((2, 1, 0), 1):
        target = "owner/repo#{}".format(100 + index)
        records += _breakdown_shadow_job(
            "shadow-{}".format(index), 810 + index, 2, target=target,
        )
        records += _breakdown_live_job(
            "live-{}".format(index), 820 + index, live_count,
            target=target,
        )

    report = shadow_report.build_report(
        records, now=900, window_seconds=200, mode="breakdown-shape",
    )

    assert report["breakdown_shape_agreement"]["jobs"] == {
        "shadow": 3, "live": 3, "matched": 3, "compared": 3,
    }
    assert report["breakdown_shape_agreement"]["agree"] == 2
    assert report["breakdown_shape_agreement"]["disagree"] == 1
    assert report["breakdown_shape_agreement"]["rate"] == 2 / 3


def test_breakdown_needs_decision_matches_normalized_text_and_presence():
    cases = (
        (None, None, True),
        ("  Which repo owns this?  ", "which repo owns this?", True),
        (None, "Which repo owns this?", False),
        ("Which repo owns this?", "Which project owns this?", False),
    )
    records = []
    for index, (shadow_question, live_question, expected) in enumerate(cases, 1):
        target = "owner/repo#{}".format(200 + index)
        records += _breakdown_shadow_job(
            "shadow-question-{}".format(index), 810 + index, 1,
            shadow_question, target=target,
        )
        records += _breakdown_live_job(
            "live-question-{}".format(index), 820 + index, 1,
            live_question, target=target, structured=True,
        )

    report = shadow_report.build_report(
        records, now=900, window_seconds=200, mode="breakdown-shape",
    )
    agreement = report["breakdown_shape_agreement"]

    assert agreement["jobs"]["compared"] == len(cases)
    assert agreement["agree"] == sum(expected for _, _, expected in cases)
    assert agreement["disagree"] == 2
    assert agreement["rate"] == 0.5


def test_unreadable_breakdown_payload_and_missing_live_map_fail_closed():
    shadow = _breakdown_shadow_job(
        "shadow-malformed", 820, 1, target="owner/repo#301", answer=False,
    )
    live = _breakdown_live_job(
        "live-malformed", 830, 1, target="owner/repo#301",
    )
    report = shadow_report.build_report(
        shadow + live, now=900, window_seconds=200, mode="breakdown-shape",
    )
    agreement = report["breakdown_shape_agreement"]
    assert agreement["jobs"] == {
        "shadow": 1, "live": 1, "matched": 1, "compared": 0,
    }
    assert agreement["agree"] == 0
    assert agreement["disagree"] == 0
    assert agreement["rate"] is None
    assert agreement["malformed_output"]["shadow"] == {
        "count": 1, "rate": 1.0,
    }

    valid_shadow = _breakdown_shadow_job(
        "shadow-missing-live", 820, 1, target="owner/repo#302",
    )
    valid_live = _breakdown_live_job(
        "live-missing-live", 830, 1, target="owner/repo#302",
    )
    missing = shadow_report.build_report(
        valid_shadow + valid_live, now=900, window_seconds=200,
        mode="breakdown-shape", live_issue_data={},
    )["breakdown_shape_agreement"]
    assert missing["jobs"]["compared"] == 0
    assert missing["rate"] is None
    assert missing["malformed_output"]["live"] == {
        "count": 1, "rate": 1.0,
    }


def test_issue_mode_uses_binding_kind_and_pairs_duplicate_targets_in_order():
    shadow = (
        _issue_job(
            "shadow-one", "shape", 810,
            "shadow shape of owner/repo#103: Ready",
        )
        + _issue_job(
            "shadow-two", "shape", 820,
            "shadow shape of owner/repo#103: Shaped",
        )
    )
    live = _issue_job(
        "live-one", "shape", 830,
        "shaped owner/repo#103: Ready",
    )

    report = shadow_report.build_report(
        shadow, live, now=900, window_seconds=200,
        mode="breakdown-shape",
    )

    assert report["breakdown_shape_agreement"]["jobs"] == {
        "shadow": 2, "live": 1, "matched": 1, "compared": 0,
    }


def test_issue_mode_falls_back_to_narrow_finish_markers_without_bindings():
    rows = [
        {"run": "shadow", "phase": "start", "ts": 10},
        {"run": "shadow", "phase": "finish", "ts": 20,
         "note": "shadow breakdown of owner/repo#7: 1 ticket"},
        {"run": "live", "phase": "start", "ts": 11},
        {"run": "live", "phase": "finish", "ts": 30,
         "note": "shaped owner/repo#8: Ready"},
    ]

    jobs = shadow_report.issue_jobs_from_records(rows)
    assert [(job["kind"], job["key"]) for job in jobs] == [
        ("breakdown", "owner/repo#7"),
        ("shape", "owner/repo#8"),
    ]
