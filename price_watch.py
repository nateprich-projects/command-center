#!/usr/bin/env python3
"""Track published model-price changes in the effective-dated rate table.

The scheduled caller supplies captured, normalized vendor responses. This
module does not fetch pages: it fails closed when a response cannot be read,
and only persists a change when the vendor's effective time and source are
known. Existing rows are append-only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import api_pricing
import release_watch
import session_usage


RATE_TABLE_PATH = api_pricing.RATE_TABLE_PATH
PRICE_CHANGE_WINDOW = timedelta(days=7)


class CouldNotCheck(ValueError):
    """A captured price response or its effective time is not trustworthy."""


@dataclass(frozen=True)
class PriceChange:
    provider: str
    model: str
    token_kind: str
    old_rate: float
    new_rate: float
    effective_date: str

    def brief_entry(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "token_kind": self.token_kind,
            "old_rate": self.old_rate,
            "new_rate": self.new_rate,
            "unit": "USD per million tokens",
            "effective_date": self.effective_date,
        }


@dataclass(frozen=True)
class WatchFault:
    provider: str
    reason: str
    kind: str = "could-not-check"
    model: Optional[str] = None

    def __str__(self) -> str:
        subject = self.provider
        if self.model:
            subject += "/" + self.model
        return "{}: {} ({})".format(self.kind, subject, self.reason)


@dataclass(frozen=True)
class WatchResult:
    changes: Tuple[PriceChange, ...] = ()
    faults: Tuple[WatchFault, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.changes and not self.faults


_RATE_ALIASES = {
    "fresh_input_tokens": "fresh_input_tokens",
    "input": "fresh_input_tokens",
    "base_input": "fresh_input_tokens",
    "cache_read_input_tokens": "cache_read_input_tokens",
    "cached_input": "cache_read_input_tokens",
    "cache_hits": "cache_read_input_tokens",
    "hits and refreshes": "cache_read_input_tokens",
    "cache_write_input_tokens": "cache_write_input_tokens",
    "cache_write": "cache_write_input_tokens",
    "5m_cache_write": "cache_write_input_tokens",
    "5m writes": "cache_write_input_tokens",
    "output_tokens": "output_tokens",
    "output": "output_tokens",
}


def _payload(response: Any) -> Mapping[str, object]:
    if isinstance(response, bytes):
        try:
            response = response.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CouldNotCheck("response is not UTF-8") from exc
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except (TypeError, ValueError) as exc:
            raise CouldNotCheck("response is not valid JSON") from exc
    if not isinstance(response, Mapping):
        raise CouldNotCheck("response is not an object")
    return response


def _timestamp(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        found = value
    elif isinstance(value, date):
        found = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            found = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _rate(value: object) -> Optional[Decimal]:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        return None
    text = str(value).strip()
    # Captured tables often spell out the unit. The snapshot normalizer keeps
    # the numeric amount and this parser rejects other currencies or units.
    match = re.fullmatch(
        r"\$?\s*([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:/\s*(?:MTok|1M tokens|million tokens))?",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def _normalized_rates(provider: str, raw: object) -> Dict[str, Decimal]:
    if not isinstance(raw, Mapping):
        raise CouldNotCheck("model pricing is not an object")
    rates: Dict[str, Decimal] = {}
    for name, value in raw.items():
        if not isinstance(name, str):
            raise CouldNotCheck("rate kind is not text")
        token_kind = _RATE_ALIASES.get(name.strip().casefold())
        amount = _rate(value)
        if token_kind is None or amount is None:
            raise CouldNotCheck("a rate kind or amount is not recognized")
        if token_kind in rates:
            raise CouldNotCheck("response repeats a rate kind")
        rates[token_kind] = amount

    # The Meta rate card has no separate cache-write price. The existing rate
    # table records the settled assumption that its published input rate also
    # applies to cache-write tokens.
    if provider.casefold() == "meta" and "cache_write_input_tokens" not in rates:
        if "fresh_input_tokens" in rates:
            rates["cache_write_input_tokens"] = rates["fresh_input_tokens"]

    missing = [kind for kind in session_usage.TOKEN_KINDS if kind not in rates]
    if missing:
        raise CouldNotCheck("response omits rate kinds: {}".format(", ".join(missing)))
    return rates


class _HTMLTables(HTMLParser):
    """Collect a page's ordinary HTML table text for the vendor adapters."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []
        self._table: Optional[List[List[str]]] = None
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag == "table" and self._table is None:
            self._table = []
        elif self._table is not None and tag == "tr":
            self._row = []
        elif self._row is not None and tag in ("th", "td"):
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _table_label(value: object) -> str:
    return " ".join(str(value).strip().casefold().split())


def _model_matches(cell: str, model: str) -> bool:
    cell_key = re.sub(r"[^a-z0-9]+", "-", cell.casefold()).strip("-")
    model_key = re.sub(r"[^a-z0-9]+", "-", model.casefold()).strip("-")
    return (
        cell_key == model_key
        or cell_key.startswith(model_key + "-")
        or cell_key.startswith(model_key + "for")
    )


def _html_catalog(
    provider: str,
    payload: Mapping[str, object],
    target_models: Sequence[str],
) -> Dict[str, Dict[str, object]]:
    body = payload.get("body")
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CouldNotCheck("response is not UTF-8") from exc
    if not isinstance(body, str):
        raise CouldNotCheck("response body is not text")
    parser = _HTMLTables()
    try:
        parser.feed(body)
        parser.close()
    except Exception as exc:
        raise CouldNotCheck("response HTML could not be read") from exc

    provider_key = provider.casefold()
    aliases = {
        "input": "fresh_input_tokens",
        "cached input": "cache_read_input_tokens",
        "cache writes": "cache_write_input_tokens",
        "5m writes": "cache_write_input_tokens",
        "hits and refreshes": "cache_read_input_tokens",
        "output": "output_tokens",
    }
    found: Dict[str, Dict[str, object]] = {}
    for table in parser.tables:
        header_index = None
        header_cells: List[str] = []
        for index, row in enumerate(table):
            labels = [_table_label(cell) for cell in row]
            if {"input", "output"} <= set(labels):
                if provider_key == "openai" and not {
                    "cached input", "cache writes",
                } <= set(labels):
                    continue
                if provider_key == "anthropic" and not {
                    "5m writes", "hits and refreshes",
                } <= set(labels):
                    continue
                header_index = index
                header_cells = labels
                break
        if header_index is None:
            continue

        columns = {}
        for index, label in enumerate(header_cells):
            token_kind = aliases.get(label)
            if token_kind is not None and token_kind not in columns:
                columns[token_kind] = index
        if any(kind not in columns for kind in session_usage.TOKEN_KINDS):
            continue

        for row in table[header_index + 1:]:
            if not row:
                continue
            for model in target_models:
                if not _model_matches(row[0], model):
                    continue
                try:
                    raw_rates = {
                        kind: row[index]
                        for kind, index in columns.items()
                    }
                    rates = _normalized_rates(provider, raw_rates)
                except (IndexError, CouldNotCheck):
                    raise CouldNotCheck("model pricing row is incomplete")
                key = model.casefold()
                if key not in found:
                    found[key] = {
                        "model": model,
                        "rates": rates,
                        "effective_from": _timestamp(payload.get("effective_from")),
                        "source_url": payload.get("source_url"),
                        "source_note": payload.get("source_note"),
                    }
        if len(found) == len(target_models):
            break

    if not parser.tables or not found:
        raise CouldNotCheck("response has no recognizable pricing table for models in use")
    effective_value = payload.get("effective_from")
    if effective_value is not None and _timestamp(effective_value) is None:
        raise CouldNotCheck("effective time is invalid")
    return found


def _parse_catalog(
    provider: str,
    response: Any,
    target_models: Sequence[str],
) -> Dict[str, Dict[str, object]]:
    if isinstance(response, str) and response.lstrip().startswith("<"):
        payload = {"body": response}
    elif isinstance(response, bytes) and response.lstrip().startswith(b"<"):
        payload = {"body": response}
    else:
        payload = _payload(response)
    if "body" in payload:
        return _html_catalog(provider, payload, target_models)

    entries = None
    for key in ("models", "prices", "data"):
        if key in payload:
            entries = payload[key]
            break
    if not isinstance(entries, list) or not entries:
        raise CouldNotCheck("response has no model pricing list")

    target_keys = {model.casefold() for model in target_models}
    catalog: Dict[str, Dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CouldNotCheck("model pricing entry is not an object")
        model = next(
            (entry.get(key) for key in ("model", "id", "name")
             if isinstance(entry.get(key), str) and entry.get(key).strip()),
            None,
        )
        if not isinstance(model, str):
            raise CouldNotCheck("model pricing entry has no model id")
        key = model.strip().casefold()
        if key not in target_keys:
            continue
        effective_value = entry.get("effective_from", payload.get("effective_from"))
        effective_from = None
        if effective_value is not None:
            effective_from = _timestamp(effective_value)
            if effective_from is None:
                raise CouldNotCheck("effective time is invalid")
        source_url = entry.get("source_url", payload.get("source_url"))
        source_note = entry.get("source_note", payload.get("source_note"))
        rates = _normalized_rates(provider, entry.get("rates"))
        if key in catalog:
            raise CouldNotCheck("response repeats model {!r}".format(model))
        catalog[key] = {
            "model": model.strip(),
            "rates": rates,
            "effective_from": effective_from,
            "source_url": source_url,
            "source_note": source_note,
        }
    return catalog


def _latest_rows(
    rows: Sequence[Mapping[str, object]],
    provider: str,
    model: str,
    at: datetime,
) -> Dict[str, Tuple[Mapping[str, object], datetime, Decimal]]:
    found: Dict[str, Tuple[Mapping[str, object], datetime, Decimal]] = {}
    for row in rows:
        if (
            str(row.get("provider", "")).casefold() != provider.casefold()
            or str(row.get("model", "")).casefold() != model.casefold()
        ):
            continue
        effective_from = _timestamp(row.get("effective_from"))
        amount = _rate(row.get("usd_per_million_tokens"))
        token_kind = row.get("token_kind")
        if effective_from is None or amount is None or not isinstance(token_kind, str):
            continue
        if effective_from > at:
            continue
        current = found.get(token_kind)
        if current is None or effective_from > current[1]:
            found[token_kind] = (row, effective_from, amount)
    return found


def _existing_at(
    rows: Sequence[Mapping[str, object]],
    provider: str,
    model: str,
    token_kind: str,
    at: datetime,
) -> Optional[Tuple[Mapping[str, object], datetime, Decimal]]:
    matches = []
    for row in rows:
        if (
            str(row.get("provider", "")).casefold() != provider.casefold()
            or str(row.get("model", "")).casefold() != model.casefold()
            or row.get("token_kind") != token_kind
        ):
            continue
        effective_from = _timestamp(row.get("effective_from"))
        amount = _rate(row.get("usd_per_million_tokens"))
        if effective_from == at and amount is not None:
            matches.append((row, effective_from, amount))
    if len(matches) > 1:
        raise CouldNotCheck("rate table repeats an effective time")
    return matches[0] if matches else None


def _document(path: Path) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CouldNotCheck("could not read the rate table") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise CouldNotCheck("rate table has an unsupported schema")
    rows = value.get("rates")
    if not isinstance(rows, list):
        raise CouldNotCheck("rate table rates are not a list")
    try:
        api_pricing.load_rates(path)
    except api_pricing.PricingError as exc:
        raise CouldNotCheck(str(exc)) from exc
    return value


def _write_document(path: Path, document: Mapping[str, object]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(path.parent),
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _current_models(
    current_models: Mapping[str, Iterable[str]],
) -> Dict[str, Tuple[str, ...]]:
    if not isinstance(current_models, Mapping):
        raise CouldNotCheck("current model map is not an object")
    result = {}
    for provider, model_ids in current_models.items():
        if not isinstance(provider, str) or not provider.strip():
            raise CouldNotCheck("provider name is invalid")
        if isinstance(model_ids, str):
            raise CouldNotCheck("current model list is invalid")
        try:
            found_by_key = {}
            for model in model_ids:
                if isinstance(model, str) and model.strip():
                    found_by_key.setdefault(model.strip().casefold(), model.strip())
            found = tuple(sorted(found_by_key.values(), key=str.casefold))
        except TypeError as exc:
            raise CouldNotCheck("current model list is invalid") from exc
        if found:
            result[provider.strip()] = found
    return result


def watch(
    current_models: Mapping[str, Iterable[str]],
    responses: Mapping[str, Any],
    *,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> WatchResult:
    """Append changed rates for models in use, returning faults explicitly.

    ``responses`` contains captured and normalized vendor pricing payloads,
    keyed by provider. No network access occurs here. A provider response may
    be unchanged without an effective time; a changed price requires one.
    """
    destination = path or RATE_TABLE_PATH
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    try:
        in_use = _current_models(current_models)
    except CouldNotCheck as exc:
        return WatchResult(faults=(WatchFault("unknown", str(exc)),))
    if not in_use:
        return WatchResult()
    try:
        document = _document(destination)
    except CouldNotCheck as exc:
        return WatchResult(faults=(WatchFault("rate-table", str(exc)),))
    rows = document["rates"]
    assert isinstance(rows, list)

    staged_rows: List[Dict[str, object]] = []
    staged_changes: List[PriceChange] = []
    faults: List[WatchFault] = []

    for provider, models in sorted(in_use.items()):
        if provider not in responses:
            faults.append(WatchFault(provider, "no pricing response"))
            continue
        try:
            catalog = _parse_catalog(provider, responses[provider], models)
        except CouldNotCheck as exc:
            faults.append(WatchFault(provider, str(exc)))
            continue

        for model in models:
            price = catalog.get(model.casefold())
            if price is None:
                faults.append(WatchFault(provider, "model has no published pricing", model=model))
                continue
            effective_from = price["effective_from"]
            prior = _latest_rows(rows, provider, model, observed_at)
            if any(kind not in prior for kind in session_usage.TOKEN_KINDS):
                faults.append(WatchFault(
                    provider, "no complete previous effective rate", model=model,
                ))
                continue
            rates = price["rates"]
            assert isinstance(rates, Mapping)
            changes = [
                token_kind for token_kind in session_usage.TOKEN_KINDS
                if rates[token_kind] != prior[token_kind][2]
            ]
            if not changes:
                continue
            if not isinstance(effective_from, datetime):
                faults.append(WatchFault(
                    provider, "changed price has no effective time", model=model,
                ))
                continue
            source_url = price.get("source_url")
            if not isinstance(source_url, str) or not source_url.strip():
                faults.append(WatchFault(
                    provider, "changed price has no source URL", model=model,
                ))
                continue

            model_rows: List[Dict[str, object]] = []
            model_changes: List[PriceChange] = []
            conflict = None
            for token_kind in changes:
                existing = _existing_at(
                    rows + staged_rows, provider, model, token_kind, effective_from,
                )
                if existing is not None:
                    if existing[2] == rates[token_kind]:
                        continue
                    conflict = "a different rate is already recorded at the effective time"
                    break
                if effective_from <= prior[token_kind][1]:
                    conflict = "effective time is not later than the current rate"
                    break
                _old_row, _old_effective, old_rate = prior[token_kind]
                new_rate = rates[token_kind]
                raw_source_note = price.get("source_note")
                row: Dict[str, object] = {
                    "provider": provider,
                    "model": model,
                    "token_kind": token_kind,
                    "usd_per_million_tokens": float(new_rate),
                    "effective_from": _timestamp_text(effective_from),
                    "source_url": source_url.strip(),
                    "recorded_at": _timestamp_text(observed_at),
                }
                if isinstance(raw_source_note, str) and raw_source_note.strip():
                    row["source_note"] = raw_source_note.strip()
                model_rows.append(row)
                model_changes.append(PriceChange(
                    provider=provider,
                    model=model,
                    token_kind=token_kind,
                    old_rate=float(old_rate),
                    new_rate=float(new_rate),
                    effective_date=_timestamp_text(effective_from),
                ))
            if conflict:
                faults.append(WatchFault(provider, conflict, model=model))
                continue
            staged_rows.extend(model_rows)
            staged_changes.extend(model_changes)

    if staged_rows:
        updated = dict(document)
        updated["rates"] = rows + staged_rows
        try:
            _write_document(destination, updated)
        except OSError as exc:
            return WatchResult(faults=tuple(faults + [
                WatchFault("rate-table", "could not append rates: {}".format(exc)),
            ]))

    return WatchResult(tuple(staged_changes), tuple(faults))


def watch_in_use(
    responses: Mapping[str, Any],
    *,
    path: Optional[Path] = None,
    now: Optional[datetime] = None,
    sources: Optional[Mapping[str, str]] = None,
    detector: Optional[Any] = None,
    providers: Optional[Mapping[str, str]] = None,
) -> WatchResult:
    """Run :func:`watch` for the models detected in heartbeat sources."""
    try:
        current = release_watch.models_in_use(
            sources=sources, detector=detector, providers=providers,
        )
    except (CouldNotCheck, release_watch.CouldNotCheck) as exc:
        return WatchResult(faults=(WatchFault("unknown", str(exc)),))
    return watch(current, responses, path=path, now=now)


def recent_changes(
    now: datetime,
    *,
    path: Optional[Path] = None,
    rows: Optional[Sequence[Mapping[str, object]]] = None,
) -> List[Dict[str, object]]:
    """Return rate changes whose effective times fall in the last seven days."""
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    if rows is None:
        source = path or RATE_TABLE_PATH
        rows = api_pricing.load_rates(source)

    indexed: List[Tuple[Mapping[str, object], datetime, Decimal]] = []
    for row in rows:
        effective = _timestamp(row.get("effective_from"))
        rate = _rate(row.get("usd_per_million_tokens"))
        if effective is None or rate is None:
            continue
        indexed.append((row, effective, rate))

    recent = []
    cutoff = current - PRICE_CHANGE_WINDOW
    for row, effective, rate in indexed:
        if effective < cutoff or effective > current:
            continue
        prior = [
            (candidate, candidate_effective, candidate_rate)
            for candidate, candidate_effective, candidate_rate in indexed
            if candidate_effective < effective
            and str(candidate.get("provider", "")).casefold()
            == str(row.get("provider", "")).casefold()
            and str(candidate.get("model", "")).casefold()
            == str(row.get("model", "")).casefold()
            and candidate.get("token_kind") == row.get("token_kind")
        ]
        if not prior:
            continue
        old_rate = max(prior, key=lambda item: item[1])[2]
        if old_rate == rate:
            continue
        recent.append(PriceChange(
            provider=str(row.get("provider", "")),
            model=str(row.get("model", "")),
            token_kind=str(row.get("token_kind", "")),
            old_rate=float(old_rate),
            new_rate=float(rate),
            effective_date=_timestamp_text(effective),
        ).brief_entry())
    return sorted(
        recent,
        key=lambda entry: (
            str(entry["effective_date"]), str(entry["provider"]),
            str(entry["model"]), str(entry["token_kind"]),
        ),
    )


__all__ = [
    "CouldNotCheck",
    "PriceChange",
    "WatchFault",
    "WatchResult",
    "recent_changes",
    "watch",
    "watch_in_use",
]
