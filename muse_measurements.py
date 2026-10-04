"""Validate owner-reported Muse panel readings and pair them with the meter.

GitHub issue history remains the durable source record. This module is a
versioned, display-independent measurement adapter: it does not acquire panel
data, write state, or change the pace meter. Report time is used as an
approximate observation time, with the owner's stated timing range retained.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import usage


SCHEMA_VERSION = 1
PANEL_USED_PERCENT = "panel_used_percent"
OBSERVATION_AGE_SECONDS = {"minimum": 60, "maximum": 120, "direction": "before_report"}
SIGNAL_CONTRACTS = {
    PANEL_USED_PERCENT: {
        "source": "owner-reported Muse account-panel reading",
        "unit": "percent",
        "paired_meter": "usage.read_muse own-card meter",
    }
}


def runtime_buffer_root(runtime_root: Optional[Path | str] = None) -> Path:
    """Return the only supported root for temporary Muse decode buffers."""
    root = (Path(runtime_root) if runtime_root is not None else
            Path.home() / ".claude" / "command-center-heartbeat")
    return root / "muse-estimate"


def _finite_number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_report_time(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _report_time_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _source_record_url(value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urlsplit(value.strip())
    except ValueError:
        return None
    if (url.scheme != "https" or url.hostname != "github.com"
            or not url.path or not url.fragment.startswith("issuecomment-")):
        return None
    return value.strip()


def _result(status: str, reason: Optional[str] = None, *,
            source: Optional[str] = None, reported_at: Optional[str] = None,
            measurement: Optional[dict] = None) -> dict:
    return {
        "status": status,
        "reason": reason,
        "source": source,
        "reported_at": reported_at,
        "measurement": measurement,
    }


def ingest_owner_report(
    report: object,
    *,
    meter_reader: Optional[Callable[[float], Optional[Mapping]]] = None,
) -> dict:
    """Validate one owner report and pair it with ``usage.read_muse``.

    ``report`` is a thin-adapter record from GitHub history with ``used_percent``,
    ``unit``, ``source``, ``provenance``, ``reported_at``, and
    ``source_record_url`` fields. The returned measurement deliberately has no
    provider sample timestamp. Missing input is a no-op; malformed input and an
    unavailable or mismatched meter return an internal rejection reason.
    """
    if report is None:
        return _result("absent", "no_owner_report")
    if not isinstance(report, Mapping):
        return _result("rejected", "report_must_be_a_mapping")

    raw_source = report.get("source")
    source = raw_source.strip() if isinstance(raw_source, str) else None
    if not source:
        source = None
    report_time = _parse_report_time(report.get("reported_at"))
    report_time_text = _report_time_text(report_time) if report_time else None
    if source is None:
        return _result("rejected", "source_must_be_nonempty",
                       reported_at=report_time_text)

    percent = _finite_number(report.get("used_percent"))
    if percent is None or not 0.0 <= percent <= 100.0:
        return _result("rejected", "used_percent_must_be_finite_percent",
                       source=source, reported_at=report_time_text)
    if report.get("unit") != "percent":
        return _result("rejected", "unit_must_be_percent",
                       source=source, reported_at=report_time_text)
    if report.get("provenance") != "owner-reported":
        return _result("rejected", "provenance_must_be_owner_reported",
                       source=source, reported_at=report_time_text)
    source_record_url = _source_record_url(report.get("source_record_url"))
    if source_record_url is None:
        return _result("rejected", "source_record_must_be_a_github_comment",
                       source=source, reported_at=report_time_text)
    if report_time is None:
        return _result("rejected", "reported_at_must_include_a_timezone",
                       source=source)

    as_of = report_time.timestamp()
    read_meter = meter_reader or usage.read_muse
    try:
        meter = read_meter(as_of)
    except Exception:
        meter = None
    if not isinstance(meter, Mapping) or meter.get("source") != "muse":
        return _result("rejected", "own_card_meter_unavailable",
                       source=source, reported_at=report_time_text)

    captured_at = _finite_number(meter.get("captured_at"))
    windows = meter.get("windows")
    window = windows.get("seven_day") if isinstance(windows, Mapping) else None
    if not isinstance(window, Mapping) or captured_at is None:
        return _result("rejected", "own_card_meter_window_unavailable",
                       source=source, reported_at=report_time_text)

    # read_muse is evaluated at report time, and its recorded reset stamp must
    # identify the same canonical Monday-UTC window as that report.
    reset_at = _finite_number(window.get("resets_at"))
    try:
        expected_reset_at = usage.muse_window_start(as_of) + usage.SEVEN_DAY
    except (OSError, OverflowError, ValueError):
        return _result("rejected", "reported_at_out_of_range",
                       source=source, reported_at=report_time_text)
    if (abs(captured_at - as_of) > 0.001 or reset_at is None
            or abs(reset_at - expected_reset_at) > 0.5):
        return _result("rejected", "own_card_meter_window_mismatch",
                       source=source, reported_at=report_time_text)

    own_card_percent = _finite_number(window.get("used_percent"))
    own_card_spent = _finite_number(window.get("spent_dollars"))
    own_card_cap = _finite_number(window.get("cap_dollars"))
    if (own_card_percent is None or own_card_percent < 0.0
            or own_card_spent is None or own_card_spent < 0.0
            or own_card_cap is None or own_card_cap <= 0.0):
        return _result("rejected", "own_card_meter_values_unusable",
                       source=source, reported_at=report_time_text)

    measurement = {
        "schema_version": SCHEMA_VERSION,
        "signal": PANEL_USED_PERCENT,
        "value": percent,
        "unit": "percent",
        "source": source,
        "provenance": "owner-reported",
        "source_record_url": source_record_url,
        "reported_at": report_time_text,
        "approximate_observation_time": report_time_text,
        "observation_time_uncertainty_seconds": dict(OBSERVATION_AGE_SECONDS),
        "paired_meter": {
            "source": "usage.read_muse own-card meter",
            "as_of": report_time_text,
            "window_resets_at": _report_time_text(
                datetime.fromtimestamp(reset_at, timezone.utc)
            ),
            "own_card_used_percent": own_card_percent,
            "own_card_spent_dollars": own_card_spent,
            "own_card_cap_dollars": own_card_cap,
        },
    }
    return _result("accepted", source=source, reported_at=report_time_text,
                   measurement=measurement)
