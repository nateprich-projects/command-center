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
