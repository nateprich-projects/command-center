#!/usr/bin/env python3
"""Capture provider data, run both model watches, and publish brief records.

The comparisons stay in :mod:`price_watch` and :mod:`release_watch`. This
entry point only fetches their inputs and keeps a local, append-only record of
new model releases and check faults for ``funnel brief``.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import heartbeat
import price_watch
import publisher
import release_watch


PRICE_URLS = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/pricing",
    "meta": "https://dev.meta.ai/docs/pricing-rate-limits",
}

# Public, provider-authored model pages avoid API-key requirements and give
# every check a durable source to carry into its result.
MODEL_CATALOGUES = {
    "openai": {
        "url": "https://developers.openai.com/api/docs/models/all.md",
        "format": "openai-markdown",
    },
    "anthropic": {
        "url": "https://platform.claude.com/docs/en/models/overview.md",
        "format": "anthropic-markdown",
    },
    "meta": {
        "url": "https://dev.meta.ai/docs/models",
        "format": "meta-html",
    },
}
ZAI_MODEL_CATALOGUE = {
    "url": "https://api.z.ai/api/anthropic/v1/models",
    "key": "ZAI_API_KEY",
    "header": "x-api-key",
    "prefix": "",
    "extra_headers": {"anthropic-version": "2023-06-01"},
    "cursor": "after_id",
}

ENV_FILE = Path.home() / ".claude" / "command-center" / ".env"
WATCH_RECORD_PATH = Path(heartbeat.SPOOL_DIR) / "model-watch.jsonl"
WATCH_WINDOW = timedelta(days=7)
REQUEST_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CATALOG_PAGES = 20


class WatchRecordError(ValueError):
    """The local watch record cannot be read or safely appended."""


@dataclass(frozen=True)
class RunOutcome:
    price_result: price_watch.WatchResult
    release_result: release_watch.WatchResult
    model_releases: Tuple[Dict[str, object], ...]
    watch_faults: Tuple[Dict[str, object], ...]
    recorded: bool


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Do not resend an authenticated request to a redirected host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _utc(value: Optional[datetime] = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


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


def _credentials(
    environ: Optional[Mapping[str, str]] = None,
    env_file: Optional[Path] = None,
) -> Dict[str, str]:
    """Read credentials for the legacy z.ai catalogue route only."""
    values = publisher.parse_dotenv(env_file or ENV_FILE)
    values.update(dict(os.environ if environ is None else environ))
    return values


def _zai_key() -> Optional[str]:
    """Reuse the existing Keychain lookup for the legacy z.ai route."""
    try:
        import usage

        return usage._zai_key() or None
    except Exception:
        return None


def _zai_headers(values: Mapping[str, str]) -> Optional[Dict[str, str]]:
    key = values.get(str(ZAI_MODEL_CATALOGUE["key"]), "").strip()
    if not key:
        key = _zai_key() or ""
    if not key:
        return None
    result = dict(ZAI_MODEL_CATALOGUE["extra_headers"])
    result[str(ZAI_MODEL_CATALOGUE["header"])] = (
        str(ZAI_MODEL_CATALOGUE["prefix"]) + key
    )
    result["Accept"] = "application/json"
    return result


def _fetch_url(
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> Optional[bytes]:
    """Fetch one bounded response; authenticated redirects fail closed."""
    request_headers = {"User-Agent": "command-center-nightly-watch/1"}
    request_headers.update(dict(headers or {}))
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        if headers:
            opener = urllib.request.build_opener(_RefuseRedirect())
            response = opener.open(request, timeout=timeout)
        else:
            response = urllib.request.urlopen(request, timeout=timeout)
        with response:
            content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            return None
        return content
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        return None


def _safe_fetch(
    fetcher: Callable[[str, Optional[Mapping[str, str]]], Optional[bytes]],
    url: str,
    headers: Optional[Mapping[str, str]],
) -> Optional[bytes]:
    try:
        return fetcher(url, headers)
    except Exception:
        return None


def _decode_json(raw: bytes) -> Optional[object]:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _catalog_entries(payload: object) -> Tuple[Optional[str], Optional[List[object]]]:
    if isinstance(payload, list):
        return None, payload
    if not isinstance(payload, Mapping):
        return None, None
    for key in release_watch.CATALOG_KEYS:
        if key in payload:
            entries = payload[key]
            return key, entries if isinstance(entries, list) else None
    return None, None


def _with_cursor(url: str, name: str, value: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, item) for key, item in query if key != name]
    query.append((name, value))
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query), parts.fragment,
    ))


def _fetch_zai_catalogue(
    headers: Mapping[str, str],
    fetcher: Callable[[str, Optional[Mapping[str, str]]], Optional[bytes]],
) -> Optional[object]:
    """Preserve the existing authenticated z.ai pagination path."""
    url = str(ZAI_MODEL_CATALOGUE["url"])
    cursor_name = str(ZAI_MODEL_CATALOGUE["cursor"])
    collected: List[object] = []
    first_payload: Optional[Mapping[str, object]] = None
    entries_key: Optional[str] = None

    for page_number in range(MAX_CATALOG_PAGES):
        raw = _safe_fetch(fetcher, url, headers)
        if raw is None:
            return None
        payload = _decode_json(raw)
        if isinstance(payload, list):
            if page_number:
                return None
            return payload
        if not isinstance(payload, Mapping):
            return payload
        key, entries = _catalog_entries(payload)
        if key is None or entries is None:
            return payload
        if first_payload is None:
            first_payload = dict(payload)
            entries_key = key
        elif key != entries_key:
            return None
        collected.extend(entries)
        if payload.get("has_more") is not True:
            result = dict(first_payload)
            result[entries_key] = collected
            result["has_more"] = False
            return result
        cursor = payload.get("last_id") or payload.get("last")
        if not isinstance(cursor, str) or not cursor.strip():
            return None
        if page_number + 1 == MAX_CATALOG_PAGES:
            return None
        url = _with_cursor(
            str(ZAI_MODEL_CATALOGUE["url"]), cursor_name, cursor.strip(),
        )
    return None


def _page_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("public model page is not UTF-8") from exc


def _openai_model_ids(raw: bytes) -> Tuple[str, ...]:
    """Read model slugs from OpenAI's published Markdown model index."""
    text = _page_text(raw)
    ids = re.findall(
        r"\]\(/api/docs/models/([A-Za-z0-9][A-Za-z0-9._-]*)\.md\)", text,
    )
    if not ids:
        raise ValueError("OpenAI model index has no recognized model links")
    return tuple(sorted(set(ids)))


def _anthropic_model_ids(raw: bytes) -> Tuple[str, ...]:
    """Read the API IDs row from Anthropic's public models overview."""
    text = _page_text(raw)
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0].casefold() == "claude api id":
            ids = re.findall(r"`(claude-[A-Za-z0-9._-]+)`", line)
            if ids:
                return tuple(sorted(set(ids)))
            raise ValueError("Anthropic API ID row contains no model IDs")
    raise ValueError("Anthropic models page has no Claude API ID row")


class _MetaModelIdsParser(HTMLParser):
    """Extract model IDs from the Muse Spark section of Meta's docs page."""

    _MODEL_ID = re.compile(r"muse-spark-\d+(?:\.\d+)*(?:-contributor)?")

    def __init__(self) -> None:
        super().__init__()
        self._in_heading = False
        self._heading: List[str] = []
        self._in_spark_section = False
        self._in_code = False
        self._code: List[str] = []
        self.model_ids: List[str] = []

    def handle_starttag(
        self, tag: str, _attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        if tag == "h2":
            self._in_heading = True
            self._heading = []
        elif tag == "code" and self._in_spark_section:
            self._in_code = True
            self._code = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading.append(data)
        if self._in_code:
            self._code.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._in_heading:
            heading = "".join(self._heading).strip().casefold()
            self._in_spark_section = heading == "muse spark"
            self._in_heading = False
            self._heading = []
        elif tag == "code" and self._in_code:
            model_id = "".join(self._code).strip()
            if self._MODEL_ID.fullmatch(model_id):
                self.model_ids.append(model_id)
            self._in_code = False
            self._code = []


def _meta_model_ids(raw: bytes) -> Tuple[str, ...]:
    """Read model IDs from Meta's public Muse Spark model documentation."""
    parser = _MetaModelIdsParser()
    try:
        parser.feed(_page_text(raw))
        parser.close()
    except Exception as exc:
        raise ValueError("Meta models page could not be parsed") from exc
    if not parser.model_ids:
        raise ValueError("Meta models page has no Muse Spark model IDs")
    return tuple(sorted(set(parser.model_ids)))


_PUBLIC_MODEL_PARSERS = {
    "openai-markdown": _openai_model_ids,
    "anthropic-markdown": _anthropic_model_ids,
    "meta-html": _meta_model_ids,
}


def capture_price_responses(
    current_models: Mapping[str, Iterable[str]],
    fetcher: Callable[[str, Optional[Mapping[str, str]]], Optional[bytes]] = _fetch_url,
) -> Dict[str, object]:
    """Fetch only pricing pages for providers with observed models."""
    responses: Dict[str, object] = {}
    for provider in sorted(current_models):
        url = PRICE_URLS.get(provider)
        if url is None:
            continue
        raw = _safe_fetch(fetcher, url, None)
        if raw is None:
            continue
        if raw.lstrip().startswith(b"<"):
            responses[provider] = {"body": raw, "source_url": url}
        else:
            responses[provider] = raw
    return responses


def capture_release_responses(
    current_models: Mapping[str, Iterable[str]],
    values: Optional[Mapping[str, str]] = None,
    fetcher: Callable[[str, Optional[Mapping[str, str]]], Optional[bytes]] = _fetch_url,
) -> Dict[str, object]:
    """Use public catalogues, retaining z.ai's existing keyed API route."""
    # The public providers never consult credentials or send request headers.
    # Retain the already-supported z.ai API path for that separate provider.
    values = {} if values is None else values
    responses: Dict[str, object] = {}
    for provider in sorted(current_models):
        if provider == "zai":
            url = str(ZAI_MODEL_CATALOGUE["url"])
            headers = _zai_headers(values)
            if headers is None:
                responses[provider] = {
                    "source_url": url,
                    "error": "z.ai API credential is unavailable",
                }
                continue
            response = _fetch_zai_catalogue(headers, fetcher)
            if response is None:
                responses[provider] = {
                    "source_url": url,
                    "error": "z.ai model API fetch failed",
                }
                continue
            try:
                models = release_watch.parse_model_catalog(response)
            except release_watch.CouldNotCheck as exc:
                responses[provider] = {
                    "source_url": url,
                    "error": str(exc),
                }
                continue
            responses[provider] = {
                "source_url": url,
                "models": list(models),
            }
            continue
        spec = MODEL_CATALOGUES.get(provider)
        if spec is None:
            responses[provider] = {
                "source_url": None,
                "error": "no public model source is configured",
            }
            continue
        url = str(spec["url"])
        raw = _safe_fetch(fetcher, url, None)
        if raw is None:
            responses[provider] = {
                "source_url": url,
                "error": "public model page fetch failed",
            }
            continue
        parser = _PUBLIC_MODEL_PARSERS[str(spec["format"])]
        try:
            models = parser(raw)
        except ValueError as exc:
            responses[provider] = {
                "source_url": url,
                "error": str(exc),
            }
            continue
        responses[provider] = {
            "source_url": url,
            "models": list(models),
        }
    return responses


def _failed_price_check(exc: Exception) -> price_watch.WatchResult:
    return price_watch.WatchResult(faults=(price_watch.WatchFault(
        "unknown", "pricing check failed ({})".format(type(exc).__name__),
    ),))


def _failed_release_check(exc: Exception) -> release_watch.WatchResult:
    return release_watch.WatchResult(faults=(release_watch.WatchFault(
        "unknown", "release check failed ({})".format(type(exc).__name__),
    ),))


def _record_events(
    price_result: price_watch.WatchResult,
    release_result: release_watch.WatchResult,
    now: datetime,
    path: Path,
) -> Tuple[Tuple[Dict[str, object], ...], Tuple[Dict[str, object], ...], bool]:
    observed_at = _utc(now).isoformat().replace("+00:00", "Z")
    existing: List[Dict[str, object]] = []
    if path.exists():
        existing = _read_records(path)
    seen = {
        (
            str(event.get("provider", "")),
            str(event.get("current_model", "")),
            str(event.get("newer_model", "")),
        )
        for record in existing
        for event in record.get("model_releases", [])
        if isinstance(event, Mapping)
    }

    releases: List[Dict[str, object]] = []
    for task in release_result.notifications:
        key = (task.provider, task.current_model, task.newer_model)
        if key in seen:
            continue
        seen.add(key)
        release: Dict[str, object] = {
            "provider": task.provider,
            "current_model": task.current_model,
            "newer_model": task.newer_model,
        }
        if task.source_url:
            release["source_url"] = task.source_url
        releases.append(release)

    faults: List[Dict[str, object]] = []
    for source, result in (("pricing", price_result), ("release", release_result)):
        for fault in result.faults:
            entry: Dict[str, object] = {
                "source": source,
                "provider": fault.provider,
                "kind": fault.kind,
                "reason": str(fault),
            }
            source_url = getattr(fault, "source_url", None)
            if source_url:
                entry["source_url"] = source_url
            model = getattr(fault, "model", None)
            if isinstance(model, str) and model:
                entry["model"] = model
            faults.append(entry)

    if not releases and not faults:
        return (), (), False

    record = {
        "recorded_at": observed_at,
        "model_releases": releases,
        "watch_faults": faults,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            # Recheck release identities under the append lock so concurrent
            # manual and scheduled runs cannot duplicate the same notice.
            locked_records = _read_handle(handle)
            locked_seen = {
                (
                    str(event.get("provider", "")),
                    str(event.get("current_model", "")),
                    str(event.get("newer_model", "")),
                )
                for prior in locked_records
                for event in prior.get("model_releases", [])
                if isinstance(event, Mapping)
            }
            fresh = [
                event for event in releases
                if (
                    str(event["provider"]),
                    str(event["current_model"]),
                    str(event["newer_model"]),
                ) not in locked_seen
            ]
            record["model_releases"] = fresh
            if not fresh and not faults:
                return (), (), False
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return tuple(fresh), tuple(faults), True
    except OSError as exc:
        raise WatchRecordError("could not append watch record ({})".format(
            type(exc).__name__)) from exc


def _read_handle(handle) -> List[Dict[str, object]]:
    handle.seek(0)
    text = handle.read()
    return _decode_records(text)


def _read_records(path: Path) -> List[Dict[str, object]]:
    try:
        return _decode_records(path.read_text(encoding="utf-8"))
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            return []
        raise WatchRecordError("could not read watch record ({})".format(
            type(exc).__name__)) from exc


def _decode_records(text: str) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise WatchRecordError(
                "watch record line {} is invalid JSON".format(line_number)
            ) from exc
        if not isinstance(value, dict) or _timestamp(value.get("recorded_at")) is None:
            raise WatchRecordError(
                "watch record line {} has an invalid shape".format(line_number)
            )
        for key in ("model_releases", "watch_faults"):
            if not isinstance(value.get(key), list):
                raise WatchRecordError(
                    "watch record line {} omits {}".format(line_number, key)
                )
        records.append(value)
    return records


def recent_entries(
    now: datetime,
    *,
    path: Optional[Path] = None,
) -> Dict[str, List[Dict[str, object]]]:
    """Return unique release notices and faults recorded in the last seven days."""
    current = _utc(now)
    cutoff = current - WATCH_WINDOW
    releases: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    faults: List[Dict[str, object]] = []
    for record in _read_records(path or WATCH_RECORD_PATH):
        recorded_at = _timestamp(record.get("recorded_at"))
        if recorded_at is None or recorded_at < cutoff or recorded_at > current:
            continue
        stamp = recorded_at.isoformat().replace("+00:00", "Z")
        for event in record["model_releases"]:
            if not isinstance(event, Mapping):
                continue
            entry = dict(event)
            entry["recorded_at"] = stamp
            key = (
                str(entry.get("provider", "")),
                str(entry.get("current_model", "")),
                str(entry.get("newer_model", "")),
            )
            prior = releases.get(key)
            if prior is None or str(prior.get("recorded_at", "")) < stamp:
                releases[key] = entry
        for event in record["watch_faults"]:
            if isinstance(event, Mapping):
                entry = dict(event)
                entry["recorded_at"] = stamp
                faults.append(entry)

    release_rows = sorted(
        releases.values(),
        key=lambda row: (
            str(row.get("recorded_at", "")),
            str(row.get("provider", "")),
            str(row.get("newer_model", "")),
        ),
        reverse=True,
    )
    fault_rows = sorted(
        faults,
        key=lambda row: (
            str(row.get("recorded_at", "")),
            str(row.get("source", "")),
            str(row.get("provider", "")),
        ),
        reverse=True,
    )
    return {"model_releases": release_rows, "watch_faults": fault_rows}


def run(
    *,
    now: Optional[datetime] = None,
    record_path: Optional[Path] = None,
    env_file: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
    current_models: Optional[Mapping[str, Iterable[str]]] = None,
    fetcher: Callable[[str, Optional[Mapping[str, str]]], Optional[bytes]] = _fetch_url,
    price_path: Optional[Path] = None,
) -> RunOutcome:
    """Run both existing checks even when capture or one check fails."""
    observed_at = _utc(now)
    if current_models is None:
        try:
            current_models = release_watch.models_in_use()
        except Exception:
            # Each watch is still called below and reports its own read fault.
            current_models = {}
    values = (
        _credentials(environ=environ, env_file=env_file)
        if "zai" in current_models else {}
    )

    try:
        price_responses = capture_price_responses(current_models, fetcher)
    except Exception:
        price_responses = {}
    try:
        release_responses = capture_release_responses(
            current_models, values, fetcher,
        )
    except Exception:
        release_responses = {}

    try:
        price_result = price_watch.watch_in_use(
            price_responses,
            path=price_path,
            now=observed_at,
        )
    except Exception as exc:
        price_result = _failed_price_check(exc)

    try:
        release_result = release_watch.watch_in_use(release_responses)
    except Exception as exc:
        release_result = _failed_release_check(exc)

    releases, faults, recorded = _record_events(
        price_result,
        release_result,
        observed_at,
        record_path or WATCH_RECORD_PATH,
    )
    return RunOutcome(
        price_result=price_result,
        release_result=release_result,
        model_releases=releases,
        watch_faults=faults,
        recorded=recorded,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="capture provider data and update the local watch record")
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.error("unknown command")
    try:
        result = run()
    except WatchRecordError as exc:
        print("nightly watch: {}".format(exc), file=sys.stderr)
        return 1
    if result.recorded:
        print(
            "nightly watch: recorded {} release(s) and {} fault(s)".format(
                len(result.model_releases), len(result.watch_faults),
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
