"""Acceptance coverage for effective-dated model API prices."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import api_pricing  # noqa: E402
import session_usage  # noqa: E402


def rate_rows(periods):
    rows = []
    for effective_from, values in periods:
        for token_kind, rate in zip(session_usage.TOKEN_KINDS, values):
            rows.append({
                "provider": "test-provider",
                "model": "test-model",
                "token_kind": token_kind,
                "usd_per_million_tokens": rate,
                "effective_from": effective_from,
                "source_url": "https://example.test/pricing",
                "recorded_at": "2026-09-10T12:00:00Z",
            })
    return rows


def run(started_at="2026-08-01T00:00:00Z"):
    return {
        "provider": "test-provider",
        "model": "test-model",
        "started_at": started_at,
        "token_usage": {
            "fresh_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "cache_write_input_tokens": 300,
            "output_tokens": 400,
        },
    }


@pytest.fixture
def complete_rate_rows():
    return rate_rows([("2026-07-01T00:00:00Z", [1, 2, 3, 4])])


@pytest.fixture
def complete_run():
    return run()


@pytest.fixture
def checked_in_rate_rows():
    return api_pricing.load_rates()


@pytest.fixture
def checked_in_model_run():
    return {
        "provider": "openai",
        "model": "gpt-6-luna",
        "started_at": "2026-09-29T08:00:00Z",
        "token_usage": {
            "fresh_input_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_write_input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        },
    }


def test_historical_run_selects_the_rate_effective_at_its_start():
    rows = rate_rows([
        ("2026-07-01T00:00:00Z", [1, 2, 3, 4]),
        ("2026-08-15T00:00:00Z", [2, 3, 4, 5]),
    ])

    before_change = api_pricing.price_run(run(), rows)
    after_change = api_pricing.price_run(
        run("2026-09-01T00:00:00Z"), rows
    )

    assert before_change["value"] == pytest.approx(.003)
    assert after_change["value"] == pytest.approx(.004)
    assert before_change["effective_rates"]["fresh_input_tokens"][
        "effective_from"
    ] == "2026-07-01T00:00:00Z"
    assert after_change["effective_rates"]["fresh_input_tokens"][
        "effective_from"
    ] == "2026-08-15T00:00:00Z"


def test_all_four_token_kinds_use_their_own_rates():
    result = api_pricing.price_run(
        run(),
        rate_rows([("2026-07-01T00:00:00Z", [1, 2, 3, 4])]),
    )

    assert result["status"] == "priced"
    assert result["value"] == pytest.approx(.003)
    assert {
        kind: result["effective_rates"][kind]["usd_per_million_tokens"]
        for kind in session_usage.TOKEN_KINDS
    } == dict(zip(session_usage.TOKEN_KINDS, [1, 2, 3, 4]))


def test_checked_in_rate_card_prices_usage_for_its_model(
    checked_in_model_run, checked_in_rate_rows
):
    result = api_pricing.price_run(checked_in_model_run, checked_in_rate_rows)

    # model_rates.json lists $0.10, $0.01, $0.125, and $0.50 per million
    # tokens for these four token kinds; one million of each totals $0.735.
    assert api_pricing.RATE_TABLE_PATH.name == "model_rates.json"
    assert result["status"] == "priced"
    assert result["unit"] == "USD"
    assert result["value"] == pytest.approx(0.735)


def test_missing_usage_stays_unpriced(complete_run, complete_rate_rows):
    complete_run["token_usage"] = None

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["value"] is None
    assert result["reason"] == "missing_token_usage"


def test_partial_usage_stays_unpriced(complete_run, complete_rate_rows):
    complete_run["token_usage"]["cache_read_input_tokens"] = None

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["value"] is None
    assert result["reason"] == "missing_token_counts"
    assert result["missing_token_kinds"] == ["cache_read_input_tokens"]


def test_run_before_first_effective_rate_stays_unpriced():
    result = api_pricing.price_run(
        run("2026-06-30T23:59:59Z"),
        rate_rows([("2026-07-01T00:00:00Z", [1, 2, 3, 4])]),
    )

    assert result["value"] is None
    assert result["reason"] == "missing_effective_rate"
    assert result["missing_rate_token_kinds"] == list(session_usage.TOKEN_KINDS)
