#!/usr/bin/env python3
"""Deduplicated main-plus-subagent Muse exposure table.

Ticket #2380 (parent #2376). Read-only analysis support: it reads the Muse
session journals on this Mac, main **and** nested subagent, and builds one
row per provider ``goal_usage_attribution`` event, deduplicated by
``usage_id`` across both and joined to the run's ``run.model.configured``
model. Each row keeps fresh input, cached input, output, model, event time
and provenance, and is filed under its provider weekly window (Sunday 17:00
America/Los_Angeles, preserving DST).

Why nested journals matter: ``usage.read_muse`` globs ``MUSE_SESSIONS``
(``sessions/*/*/*/*/session.jsonl``), a fixed depth that never reaches
``sessions/<y>/<m>/<d>/<session>/subagent/<child>/session.jsonl``. Main-only
data is therefore never labelled complete here.

Coverage is reported per window with the #2379 inventory's states:
``complete_main_plus_subagent``, ``incomplete`` (a known gap: main-only
read, a linked child journal missing, malformed or conflicting records,
journal retention starting after the window opened) or ``unquantified``
(a gap whose size the journals cannot give: unknown model, child sessions
linked with no journal path). Other-device use is never excluded by local
journals and is always flagged as not excluded.

Raw journals and the table this writes stay machine-local under
``<runtime-root>/muse-weekly-weights/`` (default runtime root
``~/.claude/command-center-heartbeat``, the ``muse_measurements``
convention), created 0700 with 0600 files. Deleting that directory is the
rollback. Nothing here changes pacing, admission or display.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Optional
from zoneinfo import ZoneInfo

import session_logs


SCHEMA_VERSION = 1
TABLE_VERSION = "v1"
RUNTIME_SUBDIR = "muse-weekly-weights"
RUNTIME_ROOT_ENV = "COMMAND_CENTER_MUSE_WEIGHTS_RUNTIME_ROOT"
TABLE_FILENAME = "exposure_table_v1.json"
COVERAGE_FILENAME = "coverage_report_v1.json"

PROVIDER_ZONE = ZoneInfo("America/Los_Angeles")
RESET_WEEKDAY = 6  # Sunday
RESET_HOUR = 17

MAIN = "main"
SUBAGENT = "subagent"
MODEL_UNKNOWN = "unknown"

COMPLETE = "complete_main_plus_subagent"
INCOMPLETE = "incomplete"
UNQUANTIFIED = "unquantified"

_USAGE_MARKER = "goal_usage_attribution"
_MODEL_MARKER = "run.model.configured"
_CHILD_MARKER = "child_session_id"


# -- Windows ---------------------------------------------------------------

def window_start(epoch: float) -> float:
    """Epoch of the Sunday 17:00 America/Los_Angeles reset at or before it.

    Zone-aware, so the reset stays at 17:00 local across PDT and PST. DST
    changes at 02:00 local on a Sunday, so 17:00 is never ambiguous.
    """
    local = datetime.fromtimestamp(epoch, PROVIDER_ZONE)
    back = (local.weekday() - RESET_WEEKDAY) % 7
    day = (local - timedelta(days=back)).date()
    candidate = datetime(day.year, day.month, day.day, RESET_HOUR,
                         tzinfo=PROVIDER_ZONE)
    if candidate.timestamp() > epoch:
        day = day - timedelta(days=7)
        candidate = datetime(day.year, day.month, day.day, RESET_HOUR,
                             tzinfo=PROVIDER_ZONE)
    return candidate.timestamp()


def window_end(start: float) -> float:
    """The next Sunday 17:00 local after a window that opened at ``start``."""
    local = datetime.fromtimestamp(start, PROVIDER_ZONE)
    day = local.date() + timedelta(days=7)
    return datetime(day.year, day.month, day.day, RESET_HOUR,
                    tzinfo=PROVIDER_ZONE).timestamp()


def window_id(start: float) -> str:
    """Stable window label: its opening instant in provider-local time."""
    return datetime.fromtimestamp(start, PROVIDER_ZONE).isoformat()


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# -- Journal parsing -------------------------------------------------------

def _epoch(stamp: object) -> Optional[float]:
    """A Muse envelope ``recorded_at`` in epoch seconds (us/ms/s scales)."""
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    stamp = float(stamp)
    if stamp != stamp or stamp < 0 or stamp == float("inf"):
        return None
    if stamp >= 1e14:
        return stamp / 1_000_000.0
    if stamp >= 1e11:
        return stamp / 1_000.0
    return stamp


def _tokens(quantity: object) -> tuple[Optional[dict], Optional[str]]:
    """Validated token fields, or a malformed-reason string."""
    if not isinstance(quantity, Mapping):
        return None, "quantity_missing"
    if quantity.get("reported") is False:
        return None, "quantity_unreported"
    values = {}
    for name in ("input_tokens", "cached_tokens", "output_tokens"):
        value = quantity.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "token_field_missing"
        if value < 0 or value != value or value == float("inf"):
            return None, "token_field_invalid"
        values[name] = int(value) if float(value).is_integer() else value
    if values["cached_tokens"] > values["input_tokens"]:
        return None, "cached_exceeds_input"
    return {
        "input_tokens": values["input_tokens"],
        "fresh_input_tokens": values["input_tokens"] - values["cached_tokens"],
        "cached_input_tokens": values["cached_tokens"],
        "output_tokens": values["output_tokens"],
    }, None


class JournalRef:
    """One logical journal: its kind, ids and the readable copy."""

    def __init__(self, logical: str, readable: str, kind: str,
                 session_id: str, parent_session_id: str, relative: str,
                 source: str):
        self.logical = logical
        self.readable = readable
        self.kind = kind
        self.session_id = session_id
        self.parent_session_id = parent_session_id
        self.relative = relative
        self.source = source


def _relative(path: str, root: str) -> Optional[str]:
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    try:
        if os.path.commonpath((path, root)) != root:
            return None
    except ValueError:
        return None
    return os.path.relpath(path, root)


def discover_journals(store_root: str, archive_base: Optional[str] = None,
                      include_nested: bool = True) -> list[JournalRef]:
    """Main (and, unless disabled, nested subagent) journals, one per log.

    Uses ``session_logs.paths`` so an archived ``.gz`` copy stands in for a
    live journal that was moved off the internal disk.
    """
    archive = session_logs.archive_root("muse", archive_base)
    patterns = [(MAIN, os.path.join(store_root, "*", "*", "*", "*",
                                    "session.jsonl"))]
    if include_nested:
        patterns.append((SUBAGENT, os.path.join(
            store_root, "*", "*", "*", "*", "subagent", "*",
            "session.jsonl")))
    refs = []
    for kind, pattern in patterns:
        for readable in session_logs.paths(
                pattern, "muse", store_root=store_root,
                archive_base=archive_base):
            relative = _relative(readable, store_root)
            source = "live"
            if relative is None:
                relative = _relative(readable, archive)
                source = "archive"
            if relative is None:
                continue
            if relative.endswith(".gz"):
                relative = relative[:-3]
                if source == "live":
                    source = "live_gz"
            parts = relative.split(os.sep)
            if kind == MAIN:
                session_id = parent = parts[3]
            else:
                session_id, parent = parts[5], parts[3]
            refs.append(JournalRef(
                os.path.join(store_root, relative), readable, kind,
                session_id, parent, relative, source))
    return refs


def _scan_journal(ref: JournalRef, counters: Counter) -> dict:
    """Read one journal's provider usage, model and child-link events."""
    usage = []
    models = []
    links = []
    malformed = []
    with session_logs.open_text(ref.readable, errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            is_usage = _USAGE_MARKER in line
            if not is_usage and _MODEL_MARKER not in line and (
                    _CHILD_MARKER not in line):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                if is_usage:
                    malformed.append({"reason": "unparseable_json",
                                      "journal": ref.relative,
                                      "line": line_number})
                continue
            if not isinstance(record, Mapping):
                continue
            payload = record.get("payload")
            if not isinstance(payload, Mapping):
                continue
            event = payload.get("event")
            if isinstance(event, Mapping) and event.get("kind") == _USAGE_MARKER:
                parsed = _parse_usage(record, event, ref, line_number)
                if parsed is None:
                    counters["non_provider_attributions"] += 1
                elif "reason" in parsed and "usage_id" not in parsed:
                    malformed.append(parsed)
                else:
                    usage.append(parsed)
                continue
            if isinstance(event, Mapping) and _CHILD_MARKER in event:
                child = event.get(_CHILD_MARKER)
                log_path = event.get("child_session_log_path")
                links.append({
                    "child_session_id": child if isinstance(child, str) else None,
                    "log_path": log_path if isinstance(log_path, str) and log_path else None,
                    "agent": (event.get("reminder_agent_id")
                              if isinstance(event.get("reminder_agent_id"), str)
                              else "unrecorded"),
                    "recorded_at": _epoch(record.get("recorded_at")),
                    "journal": ref.relative,
                })
                continue
            configured = payload.get("record")
            if (record.get("payload_type") == _MODEL_MARKER
                    or payload.get("kind") == _MODEL_MARKER
                    or (isinstance(configured, Mapping)
                        and "model_id" in configured
                        and "run_stream" in configured)):
                if not isinstance(configured, Mapping):
                    continue
                stream = configured.get("run_stream")
                run_id = stream.get("id") if isinstance(stream, Mapping) else None
                model_id = configured.get("model_id")
                if isinstance(run_id, str) and isinstance(model_id, str) and model_id:
                    models.append({
                        "run_id": run_id,
                        "model": model_id,
                        "recorded_at": _epoch(record.get("recorded_at")),
                        "journal": ref.relative,
                    })
    return {"usage": usage, "models": models, "links": links,
            "malformed": malformed}


def _parse_usage(record: Mapping, event: Mapping, ref: JournalRef,
                 line_number: int) -> Optional[dict]:
    """One provider attribution as a row, a malformed entry, or None."""
    attribution = event.get("record")
    where = {"journal": ref.relative, "line": line_number}
    if not isinstance(attribution, Mapping):
        return dict(where, reason="attribution_missing")
    if attribution.get("usage_family") != "provider":
        return None
    recorded_at = _epoch(record.get("recorded_at"))
    if recorded_at is None:
        return dict(where, reason="event_time_invalid")
    tokens, problem = _tokens(attribution.get("quantity"))
    if tokens is None:
        return dict(where, reason=problem, recorded_at=recorded_at)
    usage_id = attribution.get("usage_id")
    if not isinstance(usage_id, str) or not usage_id:
        return dict(where, reason="usage_id_missing", recorded_at=recorded_at)
    owner = attribution.get("owner")
    owner = owner if isinstance(owner, Mapping) else {}
    run_id = owner.get("run_id")
    stream = record.get("stream")
    row = {
        "usage_id": usage_id,
        "run_id": run_id if isinstance(run_id, str) else None,
        "event_time": recorded_at,
        "event_time_utc": _iso_utc(recorded_at),
        **tokens,
        "provenance": {
            "journal_kind": ref.kind,
            "journal": ref.relative,
            "journal_source": ref.source,
            "session_id": ref.session_id,
            "parent_session_id": ref.parent_session_id,
            "record_id": record.get("id"),
            "stream_id": stream.get("id") if isinstance(stream, Mapping) else None,
            "sequence": record.get("sequence"),
            "line": line_number,
            "owner_session_id": owner.get("session_id"),
            "requester_kind": owner.get("requester_kind"),
            "owner_type": owner.get("owner_type"),
        },
    }
    return row


# -- The three named steps -------------------------------------------------

def _token_key(event: Mapping) -> tuple:
    return (event.get("input_tokens"), event.get("cached_input_tokens"),
            event.get("output_tokens"))


def deduplicate_usage_ids(events: Iterable[Mapping]) -> dict:
    """Keep each ``usage_id`` once across main and nested journals.

    The first occurrence in the given order is kept (callers pass main
    journals before nested ones, each sorted by path). Every later copy is
    dropped, never summed, and listed with its provenance; a copy whose
    token fields disagree with the kept one is also listed as a conflict,
    because which copy is right cannot be told from the journals.
    """
    kept: list[dict] = []
    first: dict[str, dict] = {}
    duplicates: list[dict] = []
    conflicts: list[dict] = []
    for event in events:
        usage_id = event.get("usage_id")
        if not isinstance(usage_id, str) or not usage_id:
            raise ValueError("every event passed to deduplicate_usage_ids "
                             "needs a usage_id")
        if usage_id in first:
            original = first[usage_id]
            entry = {
                "usage_id": usage_id,
                "kept_journal": original["provenance"]["journal"],
                "kept_journal_kind": original["provenance"]["journal_kind"],
                "dropped_journal": event["provenance"]["journal"],
                "dropped_journal_kind": event["provenance"]["journal_kind"],
            }
            duplicates.append(entry)
            if _token_key(original) != _token_key(event):
                conflicts.append(entry)
            continue
        copy = dict(event)
        first[usage_id] = copy
        kept.append(copy)
    cross = sum(1 for entry in duplicates
                if entry["kept_journal_kind"] != entry["dropped_journal_kind"])
    return {"events": kept, "duplicates": duplicates, "conflicts": conflicts,
            "cross_journal_duplicates": cross}


def join_model_identity(events: Iterable[Mapping],
                        configured: Iterable[Mapping]) -> list[dict]:
    """Attach each event's ``run.model.configured`` model, or flag unknown.

    The join key is the event's ``run_id`` against the configured event's
    ``run_stream.id``, across every journal read. A run configured with one
    model gets it. A run configured with several gets the latest one at or
    before the event; none before it is ambiguous and unknown. No run, or
    no configured event for it, is unknown: there is deliberately no
    "only model in the file" fallback, because guessing would put a
    misclassified row into a per-model total.
    """
    by_run: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for entry in configured:
        run_id = entry.get("run_id")
        model = entry.get("model")
        if isinstance(run_id, str) and isinstance(model, str) and model:
            stamp = entry.get("recorded_at")
            by_run[run_id].append(
                (float(stamp) if isinstance(stamp, (int, float)) else
                 float("-inf"), model))
    joined = []
    for event in events:
        row = dict(event)
        run_id = row.get("run_id")
        choices = sorted(by_run.get(run_id, ())) if run_id else []
        models = {model for _, model in choices}
        if not run_id:
            row.update(model=MODEL_UNKNOWN, model_status="unknown",
                       model_join="no_run_id")
        elif not choices:
            row.update(model=MODEL_UNKNOWN, model_status="unknown",
                       model_join="run_not_configured")
        elif len(models) == 1:
            row.update(model=choices[0][1], model_status="configured",
                       model_join="run_configured")
        else:
            before = [model for stamp, model in choices
                      if stamp <= row.get("event_time", float("inf"))]
            if before:
                row.update(model=before[-1], model_status="configured",
                           model_join="latest_configured_before_event")
            else:
                row.update(model=MODEL_UNKNOWN, model_status="unknown",
                           model_join="ambiguous_reconfigured_run")
        joined.append(row)
    return joined


def build_exposure_table(store_root: Optional[str] = None,
                         archive_base: Optional[str] = None,
                         now: Optional[float] = None,
                         include_nested: bool = True,
                         since: Optional[float] = None) -> dict:
    """Build the deduplicated, model-joined table and its coverage report.

    ``include_nested=False`` reproduces the ``usage.read_muse`` main-only
    read; every window it produces is ``incomplete``, never complete.
    """
    store_root = os.path.abspath(os.path.expanduser(
        store_root or session_logs.STORE_ROOTS["muse"]))
    now = time.time() if now is None else float(now)
    journals = discover_journals(store_root, archive_base, include_nested)
    journals.sort(key=lambda ref: (ref.kind != MAIN, ref.relative))
    counters: Counter = Counter()
    usage: list[dict] = []
    configured: list[dict] = []
    links: list[dict] = []
    malformed: list[dict] = []
    unreadable: list[str] = []
    for ref in journals:
        try:
            scanned = _scan_journal(ref, counters)
        except OSError:
            unreadable.append(ref.relative)
            continue
        usage.extend(scanned["usage"])
        configured.extend(scanned["models"])
        malformed.extend(scanned["malformed"])
        for link in scanned["links"]:
            link["parent_dir"] = os.path.dirname(ref.logical)
            links.append(link)

    read_logical = {ref.logical for ref in journals}
    for link in links:
        if link["log_path"] is None:
            link["state"] = "no_journal_path"
            continue
        logical = os.path.normpath(os.path.join(link["parent_dir"],
                                                link["log_path"]))
        if logical in read_logical:
            link["state"] = "read"
        elif session_logs.resolve_path(
                logical, "muse", store_root=store_root,
                archive_base=archive_base) is not None:
            link["state"] = "present_not_read"
        else:
            link["state"] = "missing"

    in_range = [event for event in usage
                if event["event_time"] <= now
                and (since is None or event["event_time"] >= since)]
    future = len(usage) - len(in_range)
    deduped = deduplicate_usage_ids(in_range)
    rows = join_model_identity(deduped["events"], configured)
    for row in rows:
        start = window_start(row["event_time"])
        row["window_id"] = window_id(start)
    rows.sort(key=lambda row: (row["event_time"], row["usage_id"]))

    earliest = min((event["event_time"] for event in usage), default=None)
    coverage = _coverage(rows, deduped, malformed, links, unreadable,
                         journals, include_nested, earliest, now, since)
    coverage["events_outside_range"] = future
    return {
        "schema_version": SCHEMA_VERSION,
        "table_version": TABLE_VERSION,
        "generated_at": _iso_utc(now),
        "reset_rule": "Sunday 17:00 America/Los_Angeles, preserving DST",
        "include_nested": include_nested,
        "rows": rows,
        "coverage": coverage,
    }


def _blank_window(start: float, now: float) -> dict:
    end = window_end(start)
    return {
        "window_id": window_id(start),
        "start_utc": _iso_utc(start),
        "end_utc": _iso_utc(end),
        "in_progress": end > now,
        "events": 0,
        "events_by_journal_kind": {MAIN: 0, SUBAGENT: 0},
        "duplicates_dropped": 0,
        "cross_journal_duplicates": 0,
        "conflicting_duplicates": 0,
        "malformed": Counter(),
        "unknown_model_events": 0,
        "unknown_model_joins": Counter(),
        "child_links": Counter(),
        "pathless_child_agents": Counter(),
        "other_device_signals": Counter(),
        "by_model": {},
    }


def _coverage(rows, deduped, malformed, links, unreadable, journals,
              include_nested, earliest, now, since) -> dict:
    windows: dict[float, dict] = {}

    def slot(epoch: float) -> dict:
        start = window_start(epoch)
        if start not in windows:
            windows[start] = _blank_window(start, now)
        return windows[start]

    for row in rows:
        window = slot(row["event_time"])
        window["events"] += 1
        window["events_by_journal_kind"][row["provenance"]["journal_kind"]] += 1
        if row["model_status"] != "configured":
            window["unknown_model_events"] += 1
            window["unknown_model_joins"][row["model_join"]] += 1
        prov = row["provenance"]
        if prov.get("requester_kind") not in (None, "main"):
            window["other_device_signals"]["unexpected_requester_kind"] += 1
        if prov.get("owner_session_id") not in (None, prov["session_id"]):
            window["other_device_signals"]["owner_session_not_journal_session"] += 1
        model = window["by_model"].setdefault(row["model"], {
            "events": 0, "fresh_input_tokens": 0, "cached_input_tokens": 0,
            "output_tokens": 0, MAIN: 0, SUBAGENT: 0})
        model["events"] += 1
        model[prov["journal_kind"]] += 1
        for field in ("fresh_input_tokens", "cached_input_tokens",
                      "output_tokens"):
            model[field] += row[field]

    by_usage = {row["usage_id"]: row for row in rows}
    for entry in deduped["duplicates"]:
        window = slot(by_usage[entry["usage_id"]]["event_time"])
        window["duplicates_dropped"] += 1
        if entry["kept_journal_kind"] != entry["dropped_journal_kind"]:
            window["cross_journal_duplicates"] += 1
    for entry in deduped["conflicts"]:
        slot(by_usage[entry["usage_id"]]["event_time"])[
            "conflicting_duplicates"] += 1

    undated_malformed = Counter()
    for entry in malformed:
        stamp = entry.get("recorded_at")
        if isinstance(stamp, (int, float)) and stamp <= now and (
                since is None or stamp >= since):
            slot(stamp)["malformed"][entry["reason"]] += 1
        elif not isinstance(stamp, (int, float)):
            undated_malformed[entry["reason"]] += 1

    undated_links = Counter()
    for link in links:
        stamp = link.get("recorded_at")
        if isinstance(stamp, (int, float)) and stamp <= now and (
                since is None or stamp >= since):
            window = slot(stamp)
            window["child_links"][link["state"]] += 1
            if link["state"] == "no_journal_path":
                window["pathless_child_agents"][link["agent"]] += 1
        elif not isinstance(stamp, (int, float)):
            undated_links[link["state"]] += 1

    report_windows = []
    for start in sorted(windows):
        window = windows[start]
        incomplete = []
        unquantified = []
        if not include_nested:
            incomplete.append("main_only_nested_journals_not_read")
        if window["child_links"]["missing"]:
            incomplete.append("linked_child_journal_missing")
        if window["child_links"]["present_not_read"]:
            incomplete.append("child_journal_present_not_read")
        if window["malformed"]:
            incomplete.append("malformed_provider_records")
        if window["conflicting_duplicates"]:
            incomplete.append("conflicting_duplicate_usage_ids")
        if earliest is None or earliest > start:
            incomplete.append("journal_retention_starts_after_window_open")
        if since is not None and since > start:
            incomplete.append("scan_starts_after_window_open")
        if undated_malformed or unreadable:
            incomplete.append("undated_malformed_or_unreadable_journals")
        if window["unknown_model_events"]:
            unquantified.append("unknown_model_events")
        if window["child_links"]["no_journal_path"]:
            unquantified.append("child_sessions_without_journal_path")
        if incomplete:
            state = INCOMPLETE
        elif unquantified:
            state = UNQUANTIFIED
        else:
            state = COMPLETE
        window.update(
            coverage=state,
            incomplete_reasons=incomplete,
            unquantified_reasons=unquantified,
            possible_other_device_use={
                "excluded": False,
                "reason": ("local journals record this Mac only; Muse use "
                           "on another device would be absent from them"),
                "signals": dict(window["other_device_signals"]),
            },
            malformed=dict(window["malformed"]),
            unknown_model_joins=dict(window["unknown_model_joins"]),
            child_links=dict(window["child_links"]),
            pathless_child_agents=dict(window["pathless_child_agents"]),
        )
        window.pop("other_device_signals")
        report_windows.append(window)

    kinds = Counter(ref.kind for ref in journals)
    sources = Counter(ref.source for ref in journals)
    return {
        "coverage_states": [COMPLETE, INCOMPLETE, UNQUANTIFIED],
        "include_nested": include_nested,
        "journals_read": {MAIN: kinds[MAIN], SUBAGENT: kinds[SUBAGENT]},
        "journal_sources": dict(sources),
        "unreadable_journals": len(unreadable),
        "undated_malformed": dict(undated_malformed),
        "undated_child_links": dict(undated_links),
        "earliest_event_utc": _iso_utc(earliest) if earliest else None,
        "events": len(rows),
        "duplicates_dropped": len(deduped["duplicates"]),
        "cross_journal_duplicates": deduped["cross_journal_duplicates"],
        "conflicting_duplicates": len(deduped["conflicts"]),
        "windows": report_windows,
    }


# -- Output ----------------------------------------------------------------

def output_root(runtime_root: Optional[Path | str] = None) -> Path:
    """``<runtime-root>/muse-weekly-weights``; same default root as
    ``muse_measurements.runtime_buffer_root``."""
    configured = runtime_root or os.environ.get(RUNTIME_ROOT_ENV)
    root = (Path(configured).expanduser() if configured else
            Path.home() / ".claude" / "command-center-heartbeat")
    return root / RUNTIME_SUBDIR


def _write_private(directory: Path, name: str, payload: object) -> Path:
    target = directory / name
    fd, temporary = tempfile.mkstemp(prefix="." + name + "-", suffix=".tmp",
                                     dir=str(directory))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=1, sort_keys=True,
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
        os.chmod(target, 0o600)
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def write_outputs(result: Mapping,
                  runtime_root: Optional[Path | str] = None) -> dict:
    """Write the table and coverage report owner-only under the root."""
    directory = output_root(runtime_root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    table = _write_private(directory, TABLE_FILENAME, result)
    coverage = _write_private(directory, COVERAGE_FILENAME, {
        "schema_version": result["schema_version"],
        "table_version": result["table_version"],
        "generated_at": result["generated_at"],
        "reset_rule": result["reset_rule"],
        "coverage": result["coverage"],
    })
    return {"directory": str(directory), "table": str(table),
            "coverage": str(coverage)}


def summary(result: Mapping) -> dict:
    """Counts only — no ids, paths or content — for a public PR comment."""
    coverage = result["coverage"]
    windows = []
    for window in coverage["windows"]:
        windows.append({
            "window_id": window["window_id"],
            "in_progress": window["in_progress"],
            "coverage": window["coverage"],
            "incomplete_reasons": window["incomplete_reasons"],
            "unquantified_reasons": window["unquantified_reasons"],
            "events": window["events"],
            "events_by_journal_kind": window["events_by_journal_kind"],
            "events_by_model": {model: values["events"] for model, values
                                in sorted(window["by_model"].items())},
            "duplicates_dropped": window["duplicates_dropped"],
            "cross_journal_duplicates": window["cross_journal_duplicates"],
            "unknown_model_events": window["unknown_model_events"],
            "malformed": window["malformed"],
            "child_links": window["child_links"],
            "pathless_child_agents": window["pathless_child_agents"],
        })
    return {
        "include_nested": coverage["include_nested"],
        "journals_read": coverage["journals_read"],
        "journal_sources": coverage["journal_sources"],
        "unreadable_journals": coverage["unreadable_journals"],
        "events": coverage["events"],
        "duplicates_dropped": coverage["duplicates_dropped"],
        "cross_journal_duplicates": coverage["cross_journal_duplicates"],
        "conflicting_duplicates": coverage["conflicting_duplicates"],
        "windows": windows,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-root", default=None,
                        help="runtime root (default "
                             "~/.claude/command-center-heartbeat, or $"
                             + RUNTIME_ROOT_ENV + ")")
    parser.add_argument("--store-root", default=None,
                        help="Muse session store (default the live store)")
    parser.add_argument("--archive-base", default=None,
                        help="session-log archive base (session_logs default)")
    parser.add_argument("--since", default=None,
                        help="ISO-8601 instant; ignore events before it")
    parser.add_argument("--main-only", action="store_true",
                        help="reproduce the usage.read_muse main-only read")
    args = parser.parse_args(argv)
    since = None
    if args.since:
        stamp = args.since.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            parser.error("--since needs a timezone offset")
        since = parsed.timestamp()
    result = build_exposure_table(
        store_root=args.store_root, archive_base=args.archive_base,
        include_nested=not args.main_only, since=since)
    written = write_outputs(result, args.runtime_root)
    print(json.dumps({"written": written, "summary": summary(result)},
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
