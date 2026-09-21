"""Muse's local cost reader."""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import usage  # noqa: E402

NOW = 1_788_800_000.0


def _muse_record(at, *, input_tokens=0, cached_tokens=0, output_tokens=0,
                 family="provider", usage_id=None):
    record = {
        "recorded_at": int(at * 1_000_000),
        "payload": {"event": {
            "kind": "goal_usage_attribution",
            "record": {
                "usage_family": family,
                "quantity": {
                    "input_tokens": input_tokens,
                    "cached_tokens": cached_tokens,
                    "output_tokens": output_tokens,
                    "reported": True,
                },
            },
        }},
    }
    if usage_id is not None:
        record["payload"]["event"]["record"]["usage_id"] = usage_id
    return record


def _muse_fixture(tmp_path, monkeypatch, records):
    session = tmp_path / "2026" / "09" / "18" / "session"
    session.mkdir(parents=True)
    (session / "session.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setattr(
        usage, "MUSE_SESSIONS", str(tmp_path / "*/*/*/*/session.jsonl")
    )


def test_the_two_registries_agree_about_muse():
    """`heartbeat.py` and `usage.py` each keep a provider map, and a routine
    that is registered in one but not the other fails halfway through its own
    opening — `funnel begin` accepts the agent, then heartbeat rejects it."""
    assert heartbeat.PROVIDERS["muse"] == "meta"
    assert usage.PROVIDERS["muse"] == "meta"
    assert heartbeat.HARNESSES["muse"] == "muse-code"


def test_muse_reader_prices_provider_calls(tmp_path, monkeypatch):
    """Provider attribution is priced; non-provider events are not."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            NOW - 3600, input_tokens=3_000_000, cached_tokens=2_000_000,
            output_tokens=3_000_000, usage_id="provider-1"
        ),
        _muse_record(
            NOW - 3600, input_tokens=9_000_000, output_tokens=9_000_000,
            family="tool", usage_id="tool-1"
        ),
        _muse_record(
            NOW - usage.SEVEN_DAY - 1, input_tokens=9_000_000,
            output_tokens=9_000_000, usage_id="old-1"
        ),
    ])
    reading = usage.read_agent("muse", NOW)
    assert reading["source"] == "muse"
    assert reading["spent_dollars"] == pytest.approx(14.30)
    assert reading["cap_dollars"] == 200.0
    window = reading["windows"]["seven_day"]
    assert window["spent_dollars"] == pytest.approx(14.30)
    assert window["used_percent"] == pytest.approx(7.15)
    assert window["rolling"] is True
    assert window["calls"] == 1

    # An agent nobody registered has no pool: a configuration error, not an
    # empty budget.
    assert usage.read_agent("nonesuch", NOW) is None


def test_muse_reader_counts_only_the_current_provider_window(tmp_path, monkeypatch):
    """#1182: the total is anchored to the provider's reset, not to now - 7d.

    A trailing seven days straddles two weekly windows, so it can hold two
    windows' spend against a cap calibrated from one. Measured 2026-09-20 the
    rolling reading was 112% of the cap while the live window held 0.26% of it.
    """
    window_start = usage.muse_window_start(NOW)
    assert window_start < NOW
    assert NOW - window_start < usage.SEVEN_DAY
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            window_start + 60, input_tokens=1_000_000, usage_id="inside"
        ),
        _muse_record(
            window_start - 60, input_tokens=100_000_000, usage_id="last-week"
        ),
    ])
    reading = usage.read_agent("muse", NOW)
    window = reading["windows"]["seven_day"]
    assert window["calls"] == 1
    assert reading["spent_dollars"] == pytest.approx(1.25)
    assert window["window_start"] == window_start
    assert window["resets_at"] == window_start + usage.SEVEN_DAY


def test_muse_window_start_is_the_monday_utc_lattice():
    """The observed resets all sit on Monday 00:00 UTC — Sunday 17:00 PDT."""
    observed = (
        datetime.datetime(2026, 9, 14, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 9, 21, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc),
    )
    for reset in observed:
        just_after = reset.timestamp() + 3600
        assert usage.muse_window_start(just_after) == reset.timestamp()
        just_before = reset.timestamp() - 3600
        assert usage.muse_window_start(just_before) == (
            reset.timestamp() - usage.SEVEN_DAY
        )


def test_muse_reader_uses_the_flat_cap_path(tmp_path, monkeypatch):
    """A rolling total uses the flat cap rather than the proportional line."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            NOW - 3600, input_tokens=19_000_000, output_tokens=1_000_000,
            usage_id="provider-1"
        )
    ])
    reading = usage.read_agent("muse", NOW)
    verdict = usage.pace(reading, NOW, provider="meta")
    assert verdict["windows"][0]["elapsed_fraction"] is None
    assert verdict["windows"][0]["allowed_percent"] == 100.0
    assert not verdict["over_pace"]
    over = dict(reading)
    over["windows"] = {
        "seven_day": dict(reading["windows"]["seven_day"]),
    }
    over["windows"]["seven_day"]["used_percent"] = 100.0
    assert usage.pace(over, NOW, provider="meta")["over_pace"]


def test_muse_reader_reports_spend_over_the_weekly_cap(tmp_path, monkeypatch):
    """The rolling reader leaves an over-cap percentage visible to the gate."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            NOW - 3600, input_tokens=161_600_000, usage_id="provider-1"
        )
    ])
    reading = usage.read_agent("muse", NOW)
    window = reading["windows"]["seven_day"]
    assert reading["spent_dollars"] == pytest.approx(202.0)
    assert window["used_percent"] == pytest.approx(101.0)
    assert usage.pace(reading, NOW, provider="meta")["over_pace"]


def _projected(tmp_path, monkeypatch, *, spent, trailing, days_left):
    """A Muse reading with `spent` dollars in the window, `trailing` dollars in
    the last 72 hours, read `days_left` days before the reset."""
    window_start = usage.muse_window_start(NOW)
    now = window_start + usage.SEVEN_DAY - days_left * 86400.0
    records = []
    older = spent - min(spent, trailing)
    in_window_recent = min(spent, trailing)
    if older:
        records.append(_muse_record(
            window_start + 60, input_tokens=int(older / 1.25 * 1_000_000),
            usage_id="older"))
    if in_window_recent:
        records.append(_muse_record(
            now - 3600, input_tokens=int(in_window_recent / 1.25 * 1_000_000),
            usage_id="recent"))
    before_window = trailing - in_window_recent
    if before_window:
        records.append(_muse_record(
            now - usage.MUSE_RATE_LOOKBACK + 3600,
            input_tokens=int(before_window / 1.25 * 1_000_000),
            usage_id="before-window"))
    _muse_fixture(tmp_path, monkeypatch, records)
    reading = usage.read_agent("muse", now)
    return reading, usage.pace(reading, now, provider="meta"), now


def test_a_burst_in_a_light_week_is_not_throttled(tmp_path, monkeypatch):
    """#1198 (a): $44 spent with six days left and $45 in the last 72 hours
    projects 67 percent. A proportional line would have refused this."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=44.0, trailing=45.0, days_left=6.0)
    window = reading["windows"]["seven_day"]
    assert window["trailing_72h_dollars"] == pytest.approx(45.0)
    assert window["daily_rate_dollars"] == pytest.approx(15.0)
    assert window["projected_percent"] == pytest.approx(67.0)
    assert verdict["band"] == "ok"
    assert not verdict["over_pace"]
    assert verdict["windows"][0]["runs_out_at"] is None


def test_a_rate_that_outruns_the_window_is_tight_not_a_stop(tmp_path, monkeypatch):
    """#1198 (b): the same spend at $31 a day projects 115 percent."""
    reading, verdict, now = _projected(
        tmp_path, monkeypatch, spent=44.0, trailing=93.0, days_left=6.0)
    assert reading["windows"]["seven_day"]["projected_percent"] == pytest.approx(115.0)
    assert verdict["band"] == "tight"
    assert not verdict["over_pace"]
    runs_out_at = verdict["windows"][0]["runs_out_at"]
    assert runs_out_at == pytest.approx(now + (200.0 - 44.0) / 31.0 * 86400.0, abs=1)


def test_tight_is_read_from_the_rate_not_from_an_even_line(tmp_path, monkeypatch):
    """#1198 (c): with two days left, 65 percent used is behind an even line
    (71 percent), and at $40 a day it still ends at 105. The ticket's own
    numbers for this case could not occur: once a window is older than 72
    hours its trailing spend cannot exceed what the window has spent."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=130.0, trailing=120.0, days_left=2.0)
    assert reading["windows"]["seven_day"]["used_percent"] == pytest.approx(65.0)
    assert reading["windows"]["seven_day"]["projected_percent"] == pytest.approx(105.0)
    assert verdict["band"] == "tight"
    assert not verdict["over_pace"]


def test_the_tickets_own_case_c_is_tight_at_110_percent():
    """#1198 (c), with the ticket's exact numbers: $100 spent of $200 with two
    days left and $180 in the trailing 72 hours is $60 a day, so the window
    projects 50 + 60 = 110 percent. `tight`, although 50 percent used is behind
    an even line at day five. Fed to `pace` as a reading, because no journal
    can hold these numbers: on day five the trailing 72 hours lie inside the
    window, so they cannot exceed what the window has spent. The journal-built
    test above covers the same claim with numbers that can occur."""
    days_left = 2.0
    reading = {"windows": {"seven_day": {
        "used_percent": 50.0,
        "resets_at": NOW + days_left * 86400.0,
        "window_start": NOW + days_left * 86400.0 - usage.SEVEN_DAY,
        "rolling": True,
        "spent_dollars": 100.0,
        "cap_dollars": 200.0,
        "trailing_72h_dollars": 180.0,
        "daily_rate_dollars": 60.0,
        "projected_percent": 50.0 + 100.0 * 60.0 * days_left / 200.0,
    }}}
    verdict = usage.pace(reading, NOW, provider="meta")
    weekly = verdict["windows"][0]
    assert weekly["projected_percent"] == pytest.approx(110.0)
    assert verdict["band"] == "tight"
    assert not verdict["over_pace"]
    even_line = 100.0 * (usage.SEVEN_DAY - days_left * 86400.0) / usage.SEVEN_DAY
    assert weekly["used_percent"] < even_line
    assert weekly["runs_out_at"] == pytest.approx(
        NOW + (200.0 - 100.0) / 60.0 * 86400.0, abs=1)


def test_the_flat_ceiling_still_stops_whatever_the_projection(tmp_path, monkeypatch):
    """#1198 (d): $196 plus the session reserve passes the cap."""
    _, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=196.0, trailing=0.0, days_left=1.0)
    assert verdict["band"] == "over"
    assert verdict["over_pace"]


def test_no_recent_spend_is_ok_with_no_run_out_time(tmp_path, monkeypatch):
    """#1198 (e)."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=50.0, trailing=0.0, days_left=3.0)
    assert reading["windows"]["seven_day"]["daily_rate_dollars"] == 0.0
    assert verdict["band"] == "ok"
    assert verdict["windows"][0]["runs_out_at"] is None


def test_a_window_with_no_spend_yet_is_a_reading_of_zero(tmp_path, monkeypatch):
    """Every window opens with no calls in it, and after a wall the last 72
    hours are empty too. That is a budget of zero used, not an unreadable one:
    read as unknown it would stop every lane at the reset, with nothing left
    to make the first call."""
    window_start = usage.muse_window_start(NOW)
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(window_start - 5 * 86400.0, input_tokens=1_000_000,
                     usage_id="long-ago"),
    ])
    just_after_reset = window_start + 60
    reading = usage.read_agent("muse", just_after_reset)
    assert reading is not None
    assert reading["spent_dollars"] == 0.0
    verdict = usage.pace(reading, just_after_reset, provider="meta")
    assert verdict["known"] and not verdict["over_pace"]
    assert verdict["band"] == "ok"


def test_no_journal_at_all_still_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        usage, "MUSE_SESSIONS", str(tmp_path / "*/*/*/*/session.jsonl"))
    assert usage.read_agent("muse", NOW) is None


def test_other_providers_carry_no_band():
    """#1198 (f): a reading without a projection behaves exactly as before."""
    reading = {"windows": {"seven_day": {
        "used_percent": 10.0, "resets_at": NOW + 3 * 86400.0}}}
    verdict = usage.pace(reading, NOW, provider="anthropic")
    assert verdict["band"] is None
    assert "band" not in verdict["windows"][0]
    assert "projected_percent" not in verdict["windows"][0]


def test_muse_is_metered_with_a_measured_session_reserve():
    """Muse uses the measured worst-case session reserve under the cap."""
    assert "meta" not in usage.UNMETERED_PROVIDERS
    assert "anthropic" not in usage.UNMETERED_PROVIDERS
    assert "openai" not in usage.UNMETERED_PROVIDERS
    assert "zai" not in usage.UNMETERED_PROVIDERS
    assert usage.PROVIDER_POLICY["meta"]["weekly_target"] == 100.0
    assert usage.PROVIDER_POLICY["meta"]["weekly_reserve"] == pytest.approx(2.25)


def test_muse_model_detection_reads_snapshots_not_jsonl():
    """Every other harness writes JSONL; Muse writes whole-file JSON snapshots,
    and its `HEAD.json` carries no model at all. A glob that picks up HEAD
    reports no model and the routing dataset silently loses its most expensive
    lane."""
    source = heartbeat.MODEL_SOURCES["muse"]
    assert source.endswith("snapshot-*.json"), source
    assert "HEAD" not in source
