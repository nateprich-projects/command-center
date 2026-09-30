"""Fixture-only tests for the effective-dated model price watch."""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import price_watch  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "price_watch.json"
NOW = datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)


def data():
    return json.loads(FIXTURE.read_text())


def write_table(path, rows):
    path.write_text(json.dumps({"schema_version": 1, "rates": rows}, indent=2) + "\n")


def test_changed_published_prices_append_rows_at_the_effective_time(tmp_path):
    fixture = data()
    original = copy.deepcopy(fixture["baseline_before_gpt_5_6_luna_update"])
    path = tmp_path / "model_rates.json"
    write_table(path, original)
    result = price_watch.watch(
        {"openai": ("gpt-5.6-luna",)},
        {"openai": fixture["captured_openai_update"]},
        path=path,
        now=NOW,
    )

    assert result.faults == ()
    assert [
        (change.token_kind, change.old_rate, change.new_rate, change.effective_date)
        for change in result.changes
    ] == [
        ("fresh_input_tokens", 1.0, 0.2, "2026-07-30T00:00:00Z"),
        ("cache_read_input_tokens", 0.1, 0.02, "2026-07-30T00:00:00Z"),
        ("cache_write_input_tokens", 1.25, 0.25, "2026-07-30T00:00:00Z"),
        ("output_tokens", 6.0, 1.2, "2026-07-30T00:00:00Z"),
    ]

    updated = json.loads(path.read_text())
    assert updated["rates"][:len(original)] == original
    assert [row["effective_from"] for row in updated["rates"][len(original):]] == [
        "2026-07-30T00:00:00Z",
    ] * 4
    assert [row["usd_per_million_tokens"] for row in updated["rates"][len(original):]] == [
        0.2, 0.02, 0.25, 1.2,
    ]


def test_unchanged_captured_prices_do_not_write_the_rate_table(tmp_path):
    fixture = data()
    original = copy.deepcopy(fixture["current_gpt_6_luna_rates"])
    path = tmp_path / "model_rates.json"
    write_table(path, original)
    before = path.read_bytes()

    result = price_watch.watch(
        {"openai": ("gpt-6-luna",)},
        {"openai": fixture["captured_gpt_6_luna_rates"]},
        path=path,
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )

    assert result.clean
    assert path.read_bytes() == before


def test_anthropic_capture_uses_default_five_minute_cache_write_price(tmp_path):
    fixture = data()
    original = copy.deepcopy(fixture["current_claude_opus_5_5_rates"])
    path = tmp_path / "model_rates.json"
    write_table(path, original)
    before = path.read_bytes()

    result = price_watch.watch(
        {"anthropic": ("claude-opus-5-5",)},
        {"anthropic": fixture["captured_anthropic_prices"]},
        path=path,
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )

    assert result.clean
    assert path.read_bytes() == before


def test_missing_effective_time_keeps_a_changed_rate_unknown(tmp_path):
    fixture = data()
    original = copy.deepcopy(fixture["baseline_before_gpt_5_6_luna_update"])
    path = tmp_path / "model_rates.json"
    write_table(path, original)
    before = path.read_bytes()
    response = {
        "source_url": "https://developers.openai.com/api/docs/pricing",
        "models": [{
            "model": "gpt-5.6-luna",
            "rates": {
                "input": 0.2,
                "cached_input": 0.02,
                "cache_write": 0.25,
                "output": 1.2,
            },
        }],
    }

    result = price_watch.watch(
        {"openai": ("gpt-5.6-luna",)},
        {"openai": response},
        path=path,
        now=NOW,
    )

    assert result.changes == ()
    assert result.faults[0].reason == "changed price has no effective time"
    assert path.read_bytes() == before


def test_unparseable_vendor_response_is_a_could_not_check_fault(tmp_path):
    fixture = data()
    original = copy.deepcopy(fixture["baseline_before_gpt_5_6_luna_update"])
    path = tmp_path / "model_rates.json"
    write_table(path, original)
    before = path.read_bytes()

    result = price_watch.watch(
        {"openai": ("gpt-5.6-luna",)},
        {"openai": fixture["unparseable_response"]},
        path=path,
        now=NOW,
    )

    assert result.changes == ()
    assert len(result.faults) == 1
    assert result.faults[0].kind == "could-not-check"
    assert result.faults[0].provider == "openai"
    assert path.read_bytes() == before


def test_watch_checks_models_detected_from_heartbeat_sources(tmp_path):
    fixture = data()
    path = tmp_path / "model_rates.json"
    write_table(path, fixture["current_gpt_6_luna_rates"])
    observed = {
        "codex": {"provider": "openai", "model": "gpt-6-luna"},
        "claude": {"provider": "anthropic", "model": None},
    }

    result = price_watch.watch_in_use(
        {"openai": fixture["captured_gpt_6_luna_rates"]},
        path=path,
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        sources={"codex": "unused", "claude": "unused"},
        detector=lambda agent: observed[agent],
        providers={"codex": "openai", "claude": "anthropic"},
    )

    assert result.clean
    assert len(json.loads(path.read_text())["rates"]) == 4


def test_recent_changes_expose_rates_for_seven_days_only(tmp_path):
    fixture = data()
    path = tmp_path / "model_rates.json"
    write_table(path, fixture["baseline_before_gpt_5_6_luna_update"])
    result = price_watch.watch(
        {"openai": ("gpt-5.6-luna",)},
        {"openai": fixture["captured_openai_update"]},
        path=path,
        now=NOW,
    )
    assert len(result.changes) == 4

    visible = price_watch.recent_changes(NOW, path=path)
    assert len(visible) == 4
    assert visible[0] == {
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "token_kind": "cache_read_input_tokens",
        "old_rate": 0.1,
        "new_rate": 0.02,
        "unit": "USD per million tokens",
        "effective_date": "2026-07-30T00:00:00Z",
    }
    expired = price_watch.recent_changes(
        datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc), path=path,
    )
    assert expired == []
