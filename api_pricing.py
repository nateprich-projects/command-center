#!/usr/bin/env python3
"""Price run token observations against an effective-dated public rate table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

import session_usage


RATE_TABLE_PATH = Path(__file__).with_name("model_rates.json")
BASIS = "notional_api_list_price"


class PricingError(ValueError):
    """The checked-in public rate table is malformed or unreadable."""


def _timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        found = datetime.fromisoformat(text)
    except ValueError:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.astimezone(timezone.utc)


def _decimal_rate(value: object) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        rate = Decimal(str(value))
    except InvalidOperation:
        return None
    if not rate.is_finite() or rate < 0:
        return None
    return rate


def _validated_rows(rows: object) -> List[Tuple[Mapping[str, object], datetime, Decimal]]:
    if not isinstance(rows, list):
        raise PricingError("rate table rates must be a list")
    validated = []
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise PricingError("rate table row {} is not an object".format(index))
        provider = row.get("provider")
        model = row.get("model")
        token_kind = row.get("token_kind")
        effective_from = _timestamp(row.get("effective_from"))
        rate = _decimal_rate(row.get("usd_per_million_tokens"))
        source_url = row.get("source_url")
        recorded_at = _timestamp(row.get("recorded_at"))
        if not all(isinstance(value, str) and value.strip() for value in (
            provider, model, token_kind, source_url,
        )):
            raise PricingError("rate table row {} is missing identifying data".format(index))
        if token_kind not in session_usage.TOKEN_KINDS:
            raise PricingError("rate table row {} has an unknown token kind".format(index))
        if effective_from is None or recorded_at is None or rate is None:
            raise PricingError("rate table row {} has an invalid rate or timestamp".format(index))
        key = (
            provider.casefold(), model.casefold(), token_kind,
            effective_from,
        )
        if key in seen:
            raise PricingError("rate table contains a duplicate effective rate")
        seen.add(key)
        validated.append((row, effective_from, rate))
    return validated


def load_rates(path: Optional[Path] = None) -> List[Mapping[str, object]]:
    """Load and validate the checked-in rate rows."""
    source = path or RATE_TABLE_PATH
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PricingError("could not read rate table {}".format(source)) from exc
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise PricingError("rate table has an unsupported schema")
    rows = document.get("rates")
    _validated_rows(rows)
    return rows


def _incomplete(reason: str, **details: object) -> Dict[str, object]:
    return {
        "value": None,
        "unit": "USD",
        "basis": BASIS,
        "status": "incomplete",
        "reason": reason,
        **details,
    }


def price_run(
    run: Mapping[str, object],
    rate_rows: Sequence[Mapping[str, object]],
) -> Dict[str, object]:
    """Price all four token kinds at the rates live when the run started.

    A run is priced only when every token count and every matching rate is
    known. This prevents a partial count, an unpriced model, or a missing
    timestamp from becoming an understated total.
    """
    validated = _validated_rows(list(rate_rows))
    for name in ("provider", "model"):
        start_observation = run.get("start_" + name)
        finish_observation = run.get("finish_" + name)
        if (
            isinstance(start_observation, str)
            and start_observation.strip()
            and isinstance(finish_observation, str)
            and finish_observation.strip()
            and start_observation.strip().casefold()
            != finish_observation.strip().casefold()
        ):
            return _incomplete("conflicting_model_observations")
    provider = run.get("provider")
    model = run.get("model")
    started_at = _timestamp(run.get("started_at"))
    if not isinstance(provider, str) or not provider.strip():
        return _incomplete("missing_provider")
    if not isinstance(model, str) or not model.strip():
        return _incomplete("missing_model")
    if started_at is None:
        return _incomplete("missing_run_timestamp")

    usage = run.get("token_usage")
    if not isinstance(usage, Mapping):
        return _incomplete(
            "missing_token_usage",
            missing_token_kinds=list(session_usage.TOKEN_KINDS),
        )
    counts: Dict[str, int] = {}
    missing_tokens = []
    for token_kind in session_usage.TOKEN_KINDS:
        value = usage.get(token_kind)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            missing_tokens.append(token_kind)
        else:
            counts[token_kind] = value
    if missing_tokens:
        return _incomplete("missing_token_counts", missing_token_kinds=missing_tokens)

    effective_rates: Dict[str, Dict[str, object]] = {}
    missing_rates = []
    total = Decimal("0")
    provider_key = provider.casefold()
    model_key = model.casefold()
    for token_kind in session_usage.TOKEN_KINDS:
        eligible = [
            (row, effective_from, rate)
            for row, effective_from, rate in validated
            if row.get("provider", "").casefold() == provider_key
            and row.get("model", "").casefold() == model_key
            and row.get("token_kind") == token_kind
            and effective_from <= started_at
        ]
        if not eligible:
            missing_rates.append(token_kind)
            continue
        row, effective_from, rate = max(eligible, key=lambda item: item[1])
        total += Decimal(counts[token_kind]) * rate / Decimal(1_000_000)
        effective_rates[token_kind] = {
            "tokens": counts[token_kind],
            "usd_per_million_tokens": float(rate),
            "effective_from": effective_from.isoformat().replace("+00:00", "Z"),
            "source_url": row["source_url"],
            "recorded_at": row["recorded_at"],
            "source_note": row.get("source_note"),
        }
    if missing_rates:
        return _incomplete(
            "missing_effective_rate",
            missing_rate_token_kinds=missing_rates,
            effective_rates=effective_rates,
        )

    return {
        "value": float(total),
        "unit": "USD",
        "basis": BASIS,
        "status": "priced",
        "effective_rates": effective_rates,
    }
