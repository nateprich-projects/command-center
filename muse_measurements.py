"""Validate owner-reported Muse panel readings and pair them with the meter.

GitHub issue history remains the durable source record. This module is a
versioned, display-independent measurement adapter: it does not acquire panel
data, write state, or change the pace meter. Report time is used as an
approximate observation time, with the owner's stated timing range retained.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
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
PAIRING_RECORD_MARKER = "<!-- command-center-muse-measurement-pairing-v1 -->"
FEED_DISABLED_ENV = "COMMAND_CENTER_MUSE_ESTIMATE_FEED_DISABLED"
_REPORTED_AT = re.compile(
    r"^Nate reported at (?P<time>\d{4}-\d{2}-\d{2} "
    r"\d{2}:\d{2}:\d{2}) UTC(?:\s|\()",
    re.MULTILINE,
)
_PANEL_READING = re.compile(
    r"^Reported panel value:\s*(?P<percent>\d+(?:\.\d+)?)% used\. "
    r"Source:\s*(?P<source>[^.\r\n]+)\.",
    re.MULTILINE,
)


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


def owner_reports_from_comments(comments: object) -> list[dict]:
    """Read only Nate's explicitly formatted panel reports from issue history.

    Comment text is treated as data. A report needs both the report-time line
    and the labeled percentage line, plus the comment's own GitHub URL.
    """
    if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
        return []
    reports = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        author = comment.get("author")
        if not isinstance(author, Mapping) or author.get("login") != "nateprich":
            continue
        body = comment.get("body")
        url = comment.get("url")
        if not isinstance(body, str):
            continue
        time_match = _REPORTED_AT.search(body)
        panel_match = _PANEL_READING.search(body)
        if time_match is None or panel_match is None:
            continue
        try:
            report_time = datetime.fromisoformat(
                time_match.group("time").replace(" ", "T") + "+00:00"
            )
        except ValueError:
            continue
        reports.append({
            "used_percent": float(panel_match.group("percent")),
            "unit": "percent",
            "source": panel_match.group("source").strip(),
            "provenance": "owner-reported",
            "reported_at": _report_time_text(report_time),
            "source_record_url": url,
        })
    return reports


def latest_usable_owner_measurement(
    comments: object,
    *,
    meter_reader: Optional[Callable[[float], Optional[Mapping]]] = None,
) -> Optional[dict]:
    """Choose the newest report that validates against its own-card window."""
    reports = owner_reports_from_comments(comments)
    reports.sort(
        key=lambda report: _parse_report_time(report["reported_at"])
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for report in reports:
        result = ingest_owner_report(report, meter_reader=meter_reader)
        if result.get("status") == "accepted":
            return result.get("measurement")
    return None


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


def adjusted_estimate(measurement: object, current_meter: object) -> Optional[dict]:
    """Anchor the estimate to the panel reading, then add own-card spend delta.

    The panel percentage is converted to dollars using the paired meter cap.
    Subsequent local-meter dollars are added only when the current reading is
    still in that same weekly window. The returned `measurement` preserves the
    owner report and its paired observation time; `captured_at` is the current
    own-card meter observation, not a provider sample timestamp.
    """
    if not isinstance(measurement, Mapping) or not isinstance(current_meter, Mapping):
        return None
    if (measurement.get("schema_version") != SCHEMA_VERSION
            or measurement.get("signal") != PANEL_USED_PERCENT
            or measurement.get("provenance") != "owner-reported"):
        return None
    report_time = _parse_report_time(measurement.get("reported_at"))
    if (report_time is None
            or measurement.get("approximate_observation_time")
            != _report_time_text(report_time)
            or _source_record_url(measurement.get("source_record_url")) is None):
        return None
    panel_percent = _finite_number(measurement.get("value"))
    if panel_percent is None or not 0.0 <= panel_percent <= 100.0:
        return None

    paired = measurement.get("paired_meter")
    windows = current_meter.get("windows")
    current_window = windows.get("seven_day") if isinstance(windows, Mapping) else None
    if not isinstance(paired, Mapping) or not isinstance(current_window, Mapping):
        return None
    paired_at = _parse_report_time(paired.get("as_of"))
    paired_reset = _parse_report_time(paired.get("window_resets_at"))
    if paired_at is None or paired_reset is None or paired_at != report_time:
        return None

    captured_at = _finite_number(current_meter.get("captured_at"))
    current_spent = _finite_number(current_meter.get("spent_dollars"))
    current_cap = _finite_number(current_meter.get("cap_dollars"))
    paired_spent = _finite_number(paired.get("own_card_spent_dollars"))
    paired_cap = _finite_number(paired.get("own_card_cap_dollars"))
    current_reset = _finite_number(current_window.get("resets_at"))
    calls = current_window.get("calls")
    if (current_meter.get("source") != "muse"
            or captured_at is None or captured_at < report_time.timestamp()
            or current_spent is None or current_spent < 0.0
            or current_cap is None or current_cap <= 0.0
            or paired_spent is None or paired_spent < 0.0
            or paired_cap is None or paired_cap <= 0.0
            or current_reset is None
            or abs(current_reset - paired_reset.timestamp()) > 0.5
            or abs(current_cap - paired_cap) > 0.000001
            or not isinstance(calls, int) or isinstance(calls, bool) or calls < 0):
        return None

    delta_spent = current_spent - paired_spent
    if delta_spent < -0.000001:
        return None
    estimate_spent = round(
        panel_percent * current_cap / 100.0 + max(0.0, delta_spent), 6
    )
    return {
        "source": "Muse estimate anchored to owner-reported account-panel reading",
        "captured_at": captured_at,
        "spent_dollars": estimate_spent,
        "cap_dollars": current_cap,
        "used_percent": round(100.0 * estimate_spent / current_cap, 2),
        "calls": calls,
        "measurement": dict(measurement),
    }


def pairing_record_comment(measurement: Mapping) -> str:
    """Render one durable, source-linked pairing record for GitHub history."""
    payload = json.dumps(dict(measurement), indent=2, sort_keys=True)
    return (
        "Validated Muse account-panel measurement pairing (schema v1).\n\n"
        "This records the owner-reported percentage with its approximate "
        "observation time and same-window own-card meter pairing. The source "
        "comment remains the authority for the reported value. No provider "
        "sample timestamp is asserted.\n\n"
        + PAIRING_RECORD_MARKER
        + "\n\n```json\n"
        + payload
        + "\n```\n"
    )


def pairing_record_exists(comments: object, measurement: Mapping) -> bool:
    """Return whether issue history already records this source pairing."""
    source_url = measurement.get("source_record_url")
    if not isinstance(source_url, str) or not source_url:
        return False
    if not isinstance(comments, Sequence) or isinstance(comments, (str, bytes)):
        return False
    return any(
        isinstance(comment, Mapping)
        and isinstance(comment.get("body"), str)
        and PAIRING_RECORD_MARKER in comment["body"]
        and source_url in comment["body"]
        for comment in comments
    )


def feed_enabled() -> bool:
    """Allow the display-only feed to be disabled without changing the meter."""
    value = os.environ.get(FEED_DISABLED_ENV, "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def _runtime_buffer_path(runtime_root: Optional[Path | str] = None) -> Path:
    return runtime_buffer_root(runtime_root) / "latest.json"


def load_runtime_buffer(runtime_root: Optional[Path | str] = None) -> Optional[dict]:
    """Read the one-slot local recovery buffer; it is never a source of truth."""
    try:
        with _runtime_buffer_path(runtime_root).open(encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    measurement = value.get("measurement")
    estimate = value.get("estimate")
    failure = value.get("last_failure")
    if ((measurement is None) != (estimate is None)
            or (measurement is not None and not isinstance(measurement, dict))
            or (estimate is not None and not isinstance(estimate, dict))
            or (measurement is None and estimate is None
                and not isinstance(failure, dict))):
        return None
    return value


def _write_runtime_buffer(value: Mapping, runtime_root: Optional[Path | str]) -> bool:
    path = _runtime_buffer_path(runtime_root)
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, name = tempfile.mkstemp(prefix=".latest-", suffix=".tmp", dir=path.parent)
        temporary_path = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(value), stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_path, path)
        path.chmod(0o600)
        return True
    except OSError:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
        return False


def save_runtime_buffer(
    measurement: Mapping,
    estimate: Mapping,
    *,
    runtime_root: Optional[Path | str] = None,
) -> bool:
    """Keep only the latest accepted pairing and estimate for read failures."""
    return _write_runtime_buffer({
        "schema_version": SCHEMA_VERSION,
        "measurement": dict(measurement),
        "estimate": dict(estimate),
        "last_failure": None,
    }, runtime_root)


def record_runtime_failure(
    source: str,
    reason: str,
    observed_at: str,
    *,
    runtime_root: Optional[Path | str] = None,
) -> bool:
    """Record a bounded failure reason internally without creating a log."""
    buffer = load_runtime_buffer(runtime_root)
    if buffer is None:
        buffer = {
            "schema_version": SCHEMA_VERSION,
            "measurement": None,
            "estimate": None,
        }
    buffer["last_failure"] = {
        "source": source,
        "reason": reason,
        "observed_at": observed_at,
    }
    return _write_runtime_buffer(buffer, runtime_root)
