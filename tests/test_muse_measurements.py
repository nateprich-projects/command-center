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
