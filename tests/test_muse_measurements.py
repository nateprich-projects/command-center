"""Owner-reported Muse measurement validation and pairing."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import muse_measurements  # noqa: E402


REPORT_AT = 1_791_066_187.0  # 2026-10-03T22:23:07Z
REPORT_TEXT = "2026-10-03T22:23:07Z"
RESET_AT = 1_791_158_400.0  # 2026-10-05T00:00:00Z, the same weekly window
SOURCE_URL = (
    "https://github.com/nateprich-projects/command-center/issues/2123"
    "#issuecomment-5974149725"
)


def test_signal_contract_names_the_owner_panel_and_canonical_meter(tmp_path):
    assert muse_measurements.SIGNAL_CONTRACTS == {
        "panel_used_percent": {
            "source": "owner-reported Muse account-panel reading",
            "unit": "percent",
            "paired_meter": "usage.read_muse own-card meter",
        }
    }
    assert muse_measurements.runtime_buffer_root(tmp_path) == (
        tmp_path / "muse-estimate"
    )
    assert not (tmp_path / "muse-estimate").exists()


def owner_report(**changes):
    report = {
        "used_percent": 36,
        "unit": "percent",
        "source": "Nate's live reading at Meta",
        "provenance": "owner-reported",
        "reported_at": REPORT_TEXT,
        "source_record_url": SOURCE_URL,
    }
    report.update(changes)
    return report


def meter_reading(as_of):
    return {
        "source": "muse",
        "captured_at": as_of,
        "spent_dollars": 68.50,
        "cap_dollars": 200.0,
        "windows": {
            "seven_day": {
                "used_percent": 34.25,
                "spent_dollars": 68.50,
                "cap_dollars": 200.0,
                "resets_at": RESET_AT,
            }
        },
    }


def source_comment(**changes):
    comment = {
        "author": {"login": "nateprich"},
        "url": SOURCE_URL,
        "body": (
            "Owner-supplied panel calibration observation for #2123; not independently verified.\n\n"
            "Nate reported at 2026-10-03 22:23:07 UTC (15:23:07 PDT): "
            "live reading at Meta.\n\n"
            "Reported panel value: 36% used. Source: Nate's live reading at Meta. "
            "The timestamp above is the report time, not a verified sample time."
        ),
    }
    comment.update(changes)
    return comment


def test_latest_owner_report_reads_the_source_comment_and_pairs_at_report_time():
    calls = []

    def read_meter(as_of):
        calls.append(as_of)
        return meter_reading(as_of)

    measurement = muse_measurements.latest_usable_owner_measurement(
        [source_comment()], meter_reader=read_meter
    )

    assert calls == [REPORT_AT]
    assert measurement["value"] == 36.0
    assert measurement["reported_at"] == REPORT_TEXT
    assert measurement["approximate_observation_time"] == REPORT_TEXT
    assert measurement["source_record_url"] == SOURCE_URL
    assert measurement["provenance"] == "owner-reported"
    assert "sampled_at" not in measurement


def test_adjusted_estimate_anchors_panel_percent_then_adds_meter_delta():
    measurement = muse_measurements.latest_usable_owner_measurement(
        [source_comment()], meter_reader=meter_reading
    )
    current = meter_reading(REPORT_AT + 3600)
    current["spent_dollars"] = 72.0
    current["windows"]["seven_day"].update(
        used_percent=36.0, spent_dollars=72.0, calls=43
    )

    estimate = muse_measurements.adjusted_estimate(measurement, current)

    assert estimate["spent_dollars"] == 75.5
    assert estimate["cap_dollars"] == 200.0
    assert estimate["used_percent"] == 37.75
    assert estimate["captured_at"] == REPORT_AT + 3600
    assert estimate["measurement"]["reported_at"] == REPORT_TEXT
    assert "sampled_at" not in estimate


def test_pairing_record_links_the_owner_report_without_claiming_a_sample_time():
    measurement = muse_measurements.latest_usable_owner_measurement(
        [source_comment()], meter_reader=meter_reading
    )

    body = muse_measurements.pairing_record_comment(measurement)

    assert muse_measurements.PAIRING_RECORD_MARKER in body
    assert SOURCE_URL in body
    assert '"provenance": "owner-reported"' in body
    assert '"approximate_observation_time": "2026-10-03T22:23:07Z"' in body
    assert "sampled_at" not in body


def test_runtime_buffer_is_a_private_single_slot_recovery_copy(tmp_path):
    measurement = {"schema_version": 1, "reported_at": REPORT_TEXT}
    estimate = {"source": "Muse estimate", "spent_dollars": 75.5}

    assert muse_measurements.save_runtime_buffer(
        measurement, estimate, runtime_root=tmp_path
    )
    buffer = muse_measurements.load_runtime_buffer(tmp_path)
    path = tmp_path / "muse-estimate" / "latest.json"

    assert buffer["measurement"] == measurement
    assert buffer["estimate"] == estimate
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_runtime_buffer_records_a_failure_before_any_pairing(tmp_path):
    assert muse_measurements.record_runtime_failure(
        "command-center#2123",
        "owner_report_history_unavailable",
        REPORT_TEXT,
        runtime_root=tmp_path,
    )

    buffer = muse_measurements.load_runtime_buffer(tmp_path)
    path = tmp_path / "muse-estimate" / "latest.json"

    assert buffer["measurement"] is None
    assert buffer["estimate"] is None
    assert buffer["last_failure"] == {
        "source": "command-center#2123",
        "reason": "owner_report_history_unavailable",
        "observed_at": REPORT_TEXT,
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_valid_36_percent_report_preserves_provenance_and_pairs_at_report_time(
    monkeypatch,
):
    calls = []

    def read_meter(as_of):
        calls.append(as_of)
        return meter_reading(as_of)

    monkeypatch.setattr(muse_measurements.usage, "read_muse", read_meter)
    result = muse_measurements.ingest_owner_report(owner_report())

    assert result["status"] == "accepted"
    assert calls == [REPORT_AT]
    measurement = result["measurement"]
    assert measurement["schema_version"] == 1
    assert measurement["signal"] == "panel_used_percent"
    assert measurement["value"] == 36.0
    assert measurement["unit"] == "percent"
    assert measurement["source"] == "Nate's live reading at Meta"
    assert measurement["provenance"] == "owner-reported"
    assert measurement["source_record_url"] == SOURCE_URL
    assert measurement["reported_at"] == REPORT_TEXT
    assert measurement["approximate_observation_time"] == REPORT_TEXT
    assert measurement["observation_time_uncertainty_seconds"] == {
        "minimum": 60,
        "maximum": 120,
        "direction": "before_report",
    }
    assert "sampled_at" not in measurement
    assert measurement["paired_meter"] == {
        "source": "usage.read_muse own-card meter",
        "as_of": REPORT_TEXT,
        "window_resets_at": "2026-10-05T00:00:00Z",
        "own_card_used_percent": 34.25,
        "own_card_spent_dollars": 68.50,
        "own_card_cap_dollars": 200.0,
    }


def test_absent_report_is_a_noop_without_reading_or_fabricating_a_sample():
    calls = []
    result = muse_measurements.ingest_owner_report(
        None, meter_reader=lambda as_of: calls.append(as_of)
    )

    assert result == {
        "status": "absent",
        "reason": "no_owner_report",
        "source": None,
        "reported_at": None,
        "measurement": None,
    }
    assert calls == []


def test_malformed_percentage_fails_closed_with_an_internal_reason():
    calls = []
    result = muse_measurements.ingest_owner_report(
        owner_report(used_percent="36%"),
        meter_reader=lambda as_of: calls.append(as_of),
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "used_percent_must_be_finite_percent"
    assert result["source"] == "Nate's live reading at Meta"
    assert result["reported_at"] == REPORT_TEXT
    assert result["measurement"] is None
    assert calls == []


def test_meter_from_a_different_weekly_window_fails_closed():
    def read_wrong_window(as_of):
        reading = meter_reading(as_of)
        reading["windows"]["seven_day"]["resets_at"] = (
            RESET_AT - 7 * 24 * 60 * 60
        )
        return reading

    result = muse_measurements.ingest_owner_report(
        owner_report(), meter_reader=read_wrong_window
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "own_card_meter_window_mismatch"
    assert result["measurement"] is None
