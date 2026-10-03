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


def test_partly_known_token_kinds_are_valued_at_observed_kinds_only(
    complete_run, complete_rate_rows
):
    # #2178: 100 fresh x $1 + 300 written x $3 + 400 out x $4 per million is
    # $0.0026. The unknown cache reads are listed, never priced as zero.
    complete_run["token_usage"]["cache_read_input_tokens"] = None

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["status"] == "partial"
    assert result["value"] == pytest.approx(.0026)
    assert result["missing_token_kinds"] == ["cache_read_input_tokens"]
    assert "cache_read_input_tokens" not in result["effective_rates"]


def test_no_known_token_kind_stays_unpriced(complete_run, complete_rate_rows):
    complete_run["token_usage"] = {
        kind: None for kind in session_usage.TOKEN_KINDS
    }

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["status"] == "incomplete"
    assert result["value"] is None
    assert result["reason"] == "missing_token_counts"
    assert result["missing_token_kinds"] == list(session_usage.TOKEN_KINDS)


def test_partial_call_coverage_is_valued_at_observed_tokens_only(
    complete_run, complete_rate_rows
):
    # #2178: three calls made, two journals read. The two observed calls'
    # tokens are valued as they are; the third is listed, not estimated.
    complete_run["token_usage_coverage"] = {
        "status": "partial",
        "captured_calls": 3,
        "made_calls": 3,
        "readable_journals": 2,
        "unreadable_journals": 1,
        "uncaptured_calls": 0,
        "reasons": ["unreadable_session_journals_or_usage"],
    }

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["status"] == "partial"
    assert result["value"] == pytest.approx(.003)
    assert result["missing_calls"] == 1
    assert result["missing_call_reasons"] == [
        "unreadable_session_journals_or_usage"
    ]


def test_complete_call_coverage_is_priced(complete_run, complete_rate_rows):
    complete_run["token_usage_coverage"] = {
        "status": "complete",
        "captured_calls": 2,
        "made_calls": 2,
        "readable_journals": 2,
        "unreadable_journals": 0,
        "uncaptured_calls": 0,
    }

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["status"] == "priced"
    assert result["value"] == pytest.approx(.003)
    assert "missing_calls" not in result


def test_faulted_call_coverage_stays_unpriced(complete_run, complete_rate_rows):
    complete_run["token_usage_coverage"] = {
        "status": "fault",
        "reasons": ["call_count_mismatch"],
    }

    result = api_pricing.price_run(complete_run, complete_rate_rows)

    assert result["status"] == "incomplete"
    assert result["value"] is None


def test_partial_usage_with_an_unpriced_observed_kind_stays_unpriced(
    complete_run
):
    complete_run["token_usage"]["cache_read_input_tokens"] = None
    rows = [
        row for row in rate_rows([("2026-07-01T00:00:00Z", [1, 2, 3, 4])])
        if row["token_kind"] != "output_tokens"
    ]

    result = api_pricing.price_run(complete_run, rows)

    assert result["status"] == "incomplete"
    assert result["value"] is None
    assert result["reason"] == "missing_effective_rate"
    assert result["missing_rate_token_kinds"] == ["output_tokens"]


def test_run_before_first_effective_rate_stays_unpriced():
    result = api_pricing.price_run(
        run("2026-06-30T23:59:59Z"),
        rate_rows([("2026-07-01T00:00:00Z", [1, 2, 3, 4])]),
    )

    assert result["value"] is None
    assert result["reason"] == "missing_effective_rate"
    assert result["missing_rate_token_kinds"] == list(session_usage.TOKEN_KINDS)
