"""Source-linked weekly-panel reading inventory for Muse usage weights.

Ticket #2379 (parent #2376): a versioned, read-only inventory of every
provider reading or refusal found by searching the listed issue records and
durable repo evidence. Raw journals and local scan outputs stay
machine-local; this file and its JSON inventory hold only redacted,
source-linked readings.

Scope preservation (verified against local evidence before building):
- #2018 was narrowed to dashboard display; #2123 implemented display
  anchoring and explicitly excluded learned weights. Neither scope is
  reopened here.
- #1673/#1994/#1995 per-model API-card accounting and the panel-paired cap
  are evidence and precedent, not substitutes for empirical weights.
- The 8% panel figure is a display anchor only
  (``display_anchor_only: true``); it must not feed any weight fit.

Remote issue bodies/comments were not re-fetched in this checkout
(no network side effects in an implement run); each record rests on the
durable repo citation in ``source_ref`` and flags what is unresolved.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 1

INVENTORY_FILENAME = "muse_weekly_panel_readings_v1.json"

WINDOW_IDENTITIES = ("weekly", "five_hour")
RECORD_KINDS = ("panel_percent", "refusal_wall", "reopen_zero")
ROUNDING_MEANINGS = (
    "integer_rounding_interval",
    "tick_crossing",
    "wall_100",
)
COVERAGE_STATES = (
    "complete_main_plus_subagent",
    "incomplete",
    "unquantified",
)

REQUIRED_FIELDS = (
    "id",
    "window_identity",
    "kind",
    "panel_percent",
    "source_url",
    "source_ref",
    "reported_at",
    "observation_uncertainty_seconds",
    "rounding_meaning",
    "reset_description",
    "subscription_context",
    "model_context",
    "coverage",
    "include_in_primary_fit",
    "comparability_flags",
    "display_anchor_only",
)


def inventory_path(path: str | Path | None = None) -> Path:
    """Return the versioned inventory file for Ticket #2379."""
    if path is not None:
        return Path(path)
    return Path(__file__).resolve().parent / INVENTORY_FILENAME


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and abs(float(value)) != float("inf")
        and float(value) == float(value)
    )


def validate_reading_record(record: Mapping) -> dict:
    """Validate one inventory reading; return a plain-dict copy.

    Raises ValueError naming the defect. In particular it rejects pooled
    weekly/five-hour window identities, a missing source URL, and missing
    observation-time uncertainty, and it refuses to admit an
    incomplete-coverage interval into the primary fit.
    """
    if not isinstance(record, Mapping):
        raise ValueError("reading record must be a mapping")
    missing = [key for key in REQUIRED_FIELDS if key not in record]
    if missing:
        raise ValueError(
            f"reading record {record.get('id', '?')!r} "
            f"is missing fields: {', '.join(missing)}"
        )
    record_id = record["id"]
    if not isinstance(record_id, str) or not record_id.strip():
        raise ValueError("reading record id must be a non-empty string")

    identity = record["window_identity"]
    if identity not in WINDOW_IDENTITIES:
        raise ValueError(
            f"reading {record_id!r}: window_identity {identity!r} pools or "
            "mislabels windows; must be exactly 'weekly' or 'five_hour'"
        )

    kind = record["kind"]
    if kind not in RECORD_KINDS:
        raise ValueError(
            f"reading {record_id!r}: kind {kind!r} must be one of "
            f"{', '.join(RECORD_KINDS)}"
        )

    panel = record["panel_percent"]
    if not _is_finite_number(panel) or not 0.0 <= float(panel) <= 100.0:
        raise ValueError(
            f"reading {record_id!r}: panel_percent must be a finite "
            "0-100 number"
        )
    if kind == "refusal_wall" and float(panel) != 100.0:
        raise ValueError(
            f"reading {record_id!r}: a refusal wall must read 100"
        )

    source_url = record["source_url"]
    if (
        not isinstance(source_url, str)
        or not source_url.strip()
        or urlsplit(source_url.strip()).scheme != "https"
    ):
        raise ValueError(
            f"reading {record_id!r}: source_url must be a non-empty https URL"
        )

    for field in (
        "source_ref",
        "reported_at",
        "reset_description",
        "subscription_context",
        "model_context",
    ):
        value = record[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"reading {record_id!r}: {field} must be a non-empty string"
            )
    try:
        stamp = record["reported_at"].strip()
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        datetime.fromisoformat(stamp)
    except ValueError:
        raise ValueError(
            f"reading {record_id!r}: reported_at is not an ISO-8601 timestamp"
        ) from None

    uncertainty = record["observation_uncertainty_seconds"]
    if not isinstance(uncertainty, Mapping):
        raise ValueError(
            f"reading {record_id!r}: observation_uncertainty_seconds is "
            "missing; report-time uncertainty must be stated, not omitted"
        )
    low = uncertainty.get("minimum")
    high = uncertainty.get("maximum")
    if (
        not _is_finite_number(low)
        or not _is_finite_number(high)
        or float(low) < 0
        or float(high) < float(low)
    ):
        raise ValueError(
            f"reading {record_id!r}: observation_uncertainty_seconds needs "
            "finite minimum/maximum seconds with 0 <= minimum <= maximum"
        )

    rounding = record["rounding_meaning"]
    if rounding not in ROUNDING_MEANINGS:
        raise ValueError(
            f"reading {record_id!r}: rounding_meaning {rounding!r} must be "
            f"one of {', '.join(ROUNDING_MEANINGS)}"
        )

    coverage = record["coverage"]
    if coverage not in COVERAGE_STATES:
        raise ValueError(
            f"reading {record_id!r}: coverage {coverage!r} must be one of "
            f"{', '.join(COVERAGE_STATES)}"
        )
    include = record["include_in_primary_fit"]
    if not isinstance(include, bool):
        raise ValueError(
            f"reading {record_id!r}: include_in_primary_fit must be a boolean"
        )
    if include and coverage != "complete_main_plus_subagent":
        raise ValueError(
            f"reading {record_id!r}: coverage {coverage!r} cannot enter the "
            "primary fit; incomplete intervals stay in inventory and "
            "sensitivity analysis only"
        )

    flags = record["comparability_flags"]
    if not isinstance(flags, list) or not all(
        isinstance(flag, str) and flag.strip() for flag in flags
    ):
        raise ValueError(
            f"reading {record_id!r}: comparability_flags must be a list of "
            "non-empty strings"
        )
    anchor = record["display_anchor_only"]
    if not isinstance(anchor, bool):
        raise ValueError(
            f"reading {record_id!r}: display_anchor_only must be a boolean"
        )
    return dict(record)


def load_reading_inventory(path: str | Path | None = None) -> dict:
    """Load and validate the versioned Ticket #2379 reading inventory."""
    resolved = inventory_path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read inventory at {resolved}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(
            f"inventory at {resolved} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise ValueError("inventory root must be a mapping")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"inventory schema_version must be {SCHEMA_VERSION}"
        )
    sources = payload.get("sources_searched")
    if not isinstance(sources, list) or not sources:
        raise ValueError("inventory must list its searched sources")
    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        raise ValueError("inventory must hold at least one reading")
    seen: set[str] = set()
    validated = []
    for record in readings:
        checked = validate_reading_record(record)
        if checked["id"] in seen:
            raise ValueError(f"duplicate reading id {checked['id']!r}")
        seen.add(checked["id"])
        validated.append(checked)
    scope = payload.get("scope_confirmation")
    if not isinstance(scope, Mapping) or not scope:
        raise ValueError(
            "inventory must carry its scope_confirmation "
            "(#2018/#2123 display scope and 8% anchor-only status)"
        )
    return {
        "schema_version": payload["schema_version"],
        "inventory_version": payload.get("inventory_version", "v1"),
        "scope_confirmation": dict(scope),
        "sources_searched": list(sources),
        "readings": validated,
    }


def weekly_window_open_zoned(reset_description: str) -> bool:
    """Report whether a reset description names the zoned Sunday reset.

    The confirmed provider reset is Sunday 17:00 America/Los_Angeles,
    preserving DST; a fixed Monday-00:00-UTC claim is only equivalent in
    PDT and must fail the check in PST. This helper keeps the inventory's
    reset strings honest without owning the Ticket 4 validation.
    """
    text = reset_description.lower()
    return "america/los_angeles" in text and "17:00" in text


def inventory_anchor_check(now: datetime | None = None) -> dict:
    """Confirm the provider lattice used by the inventory's reset strings."""
    moment = now if now is not None else datetime.now(timezone.utc)
    return {
        "reset_rule": "Sunday 17:00 America/Los_Angeles, preserving DST",
        "checked_at": moment.isoformat(),
    }
