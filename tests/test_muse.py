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
