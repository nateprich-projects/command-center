#!/usr/bin/env python3
"""Shared assessment of heartbeat conditions worth surfacing.

The local funnel and the Actions watchdog read the same heartbeat rows. Keeping
the thresholds and wording here prevents the two readers from drifting apart.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Tuple

#: Calibrated by the offline replay in tests/test_watchdog_calibration.py against
#: the heartbeat branch snapshot in tests/fixtures/heartbeat_history.json.
NORMAL_PERCENTILE = 90
NORMAL_MULTIPLE = 5
SILENCE_FLOOR_SECONDS = 3600
MINIMUM_HISTORY = 8

#: The normal rhythm is learned from the trailing fortnight. This is deliberately
#: separate from the four provisional calibration parameters above: the ticket
#: calls for the window shown in the alarm evidence, "p90 over 14 days".
HISTORY_WINDOW_SECONDS = 14 * 86400

#: A run still unfinished after this long is presumed dead. Matches the lock TTL
#: in funnel.py — the same two hours after which its claim becomes takeable.
UNFINISHED_SECONDS = 2 * 3600

#: One dying run is noise. Three in a week means runs are dying.
DYING_THRESHOLD = 3
#: A single open start is normal while it is within this floor and the agent's
#: own typical completed-run duration. The floor avoids alarming on short runs
#: when the history contains only very fast no-op sessions.
OPEN_START_FLOOR_SECONDS = 15 * 60
OPEN_START_MULTIPLE = 10
ERROR_THRESHOLD = 3
WEEK = 7 * 86400
PROMPT_DRIFT_OUTCOME = "prompt-drift"


def _history(
    rows: List[Dict],
    now: float,
    *,
    history_window_seconds: int = HISTORY_WINDOW_SECONDS,
) -> Tuple[List[float], List[float]]:
    """Return recent record timestamps and their positive consecutive gaps."""
    cutoff = now - history_window_seconds
    timestamps = sorted(
        float(row["ts"])
        for row in rows
        if isinstance(row.get("ts"), (int, float))
        and cutoff <= float(row["ts"]) <= now
    )
    gaps = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])
            if later > earlier]
    return timestamps, gaps


def _percentile(values: List[float], percentile: float) -> float:
    """Return a linearly interpolated percentile without a third-party library."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _normal_gap(
    rows: List[Dict],
    now: float,
    *,
    normal_percentile: float = NORMAL_PERCENTILE,
    minimum_history: int = MINIMUM_HISTORY,
    history_window_seconds: int = HISTORY_WINDOW_SECONDS,
) -> Optional[Tuple[float, float, int]]:
    """Return ``(normal gap, latest record, record count)`` when inferable."""
    timestamps, gaps = _history(
        rows, now, history_window_seconds=history_window_seconds
    )
    if len(gaps) < minimum_history:
        return None
    return _percentile(gaps, normal_percentile), timestamps[-1], len(timestamps)


def _duration(seconds: float) -> str:
    """Render a duration compactly enough to scan in an issue body."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return "{}s".format(total)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append("{}d".format(days))
    if hours:
        parts.append("{}h".format(hours))
    if minutes:
        parts.append("{}m".format(minutes))
    if not parts:
        parts.append("0m")
    return "".join(parts)


def _window_label(history_window_seconds: int = HISTORY_WINDOW_SECONDS) -> str:
    days = history_window_seconds / 86400
    if days.is_integer():
        return "{} days".format(int(days))
    return _duration(history_window_seconds)


def _completed_run_durations(
    rows: List[Dict],
    now: float,
    *,
    history_window_seconds: int = HISTORY_WINDOW_SECONDS,
) -> List[float]:
    """Return named start-to-finish durations completed in the history window."""
    cutoff = now - history_window_seconds
    starts = {
        row.get("run"): float(row["ts"])
        for row in rows
        if row.get("phase") == "start"
        and row.get("run")
        and isinstance(row.get("ts"), (int, float))
        and float(row["ts"]) <= now
    }
    durations = []
    for row in rows:
        if (
            row.get("phase") != "finish"
            or not row.get("run")
            or not isinstance(row.get("ts"), (int, float))
        ):
            continue
        finished_at = float(row["ts"])
        started_at = starts.get(row.get("run"))
        if (
            started_at is None
            or finished_at < cutoff
            or finished_at < started_at
            or finished_at > now
        ):
            continue
        durations.append(finished_at - started_at)
    return durations


def assess(
    agent: str,
    rows: List[Dict],
    now: float,
    *,
    normal_percentile: Optional[float] = None,
    normal_multiple: Optional[int] = None,
    silence_floor_seconds: Optional[int] = None,
    minimum_history: Optional[int] = None,
    history_window_seconds: Optional[int] = None,
    unfinished_seconds: Optional[int] = None,
    dying_threshold: Optional[int] = None,
    week: Optional[int] = None,
    prompt_drift_outcome: Optional[str] = None,
    error_threshold: Optional[int] = None,
) -> List[str]:
    """Return raised conditions in the watchdog's existing wording.

    The optional keyword arguments keep the Actions wrapper's historical test
    hooks intact while leaving one implementation of the assessment rules.
    """
    normal_percentile = (
        NORMAL_PERCENTILE if normal_percentile is None else normal_percentile
    )
    normal_multiple = NORMAL_MULTIPLE if normal_multiple is None else normal_multiple
    silence_floor_seconds = (
        SILENCE_FLOOR_SECONDS
        if silence_floor_seconds is None else silence_floor_seconds
    )
    minimum_history = MINIMUM_HISTORY if minimum_history is None else minimum_history
    history_window_seconds = (
        HISTORY_WINDOW_SECONDS
        if history_window_seconds is None else history_window_seconds
    )
    unfinished_seconds = (
        UNFINISHED_SECONDS if unfinished_seconds is None else unfinished_seconds
    )
    dying_threshold = DYING_THRESHOLD if dying_threshold is None else dying_threshold
    week = WEEK if week is None else week
    prompt_drift_outcome = (
        PROMPT_DRIFT_OUTCOME
        if prompt_drift_outcome is None else prompt_drift_outcome
    )
    error_threshold = ERROR_THRESHOLD if error_threshold is None else error_threshold

    problems: List[str] = []

    if not rows:
        return []

    inferred = _normal_gap(
        rows,
        now,
        normal_percentile=normal_percentile,
        minimum_history=minimum_history,
        history_window_seconds=history_window_seconds,
    )
    if inferred is not None:
        normal, latest, record_count = inferred
        quiet_for = now - latest
        threshold = max(silence_floor_seconds, normal_multiple * normal)
        if quiet_for > threshold:
            ratio = quiet_for / normal if normal else float("inf")
            ratio_text = "{:.0f}x normal".format(ratio) if ratio != float("inf") else "unbounded"
            problems.append(
                "`{}`: normal gap {} (p{} over {}, {} records). "
                "Nothing recorded for {} — {} (alarm threshold {}x normal; "
                "last at <t:{}:f>).".format(
                    agent,
                    _duration(normal),
                    normal_percentile,
                    _window_label(history_window_seconds),
                    record_count,
                    _duration(quiet_for),
                    ratio_text,
                    normal_multiple,
                    int(latest),
                )
            )

    # An unresolved finish counts as a finish for one of its candidates. A run
    # that completed but could not name itself must not be reported as dying —
    # that false alarm is the failure this signal exists to avoid.
    import heartbeat

    open_starts = heartbeat.open_starts(rows)
    completed_durations = _completed_run_durations(
        rows, now, history_window_seconds=history_window_seconds
    )
    if len(open_starts) == 1 and completed_durations:
        open_start = open_starts[0]
        started_at = open_start.get("ts")
        if isinstance(started_at, (int, float)) and started_at <= now:
            age = now - started_at
            median = statistics.median(completed_durations)
            threshold = max(
                OPEN_START_FLOOR_SECONDS,
                OPEN_START_MULTIPLE * median,
            )
            if age > threshold:
                problems.append(
                    "`{}` has one open start `{}` aged {} — over the {} "
                    "threshold (10x median completed-run length {}, floor {}). "
                    "Started at <t:{}:f>.".format(
                        agent,
                        open_start.get("run"),
                        _duration(age),
                        _duration(threshold),
                        _duration(median),
                        _duration(OPEN_START_FLOOR_SECONDS),
                        int(started_at),
                    )
                )

    dying = [
        r for r in open_starts
        if now - (r.get("ts") or 0) > unfinished_seconds
        and now - (r.get("ts") or 0) < week
    ]
    if len(dying) >= dying_threshold:
        problems.append(
            "`{}` has {} runs this week that started and never finished — "
            "tickets {}. That is what a session killed mid-work by a rate limit "
            "looks like. Check whether the reserves in `usage.py` are too low.".format(
                agent,
                len(dying),
                ", ".join(str(r.get("ticket")) for r in dying[-5:]),
            )
        )

    prompt_drift = [
        r for r in rows
        if r.get("outcome") == prompt_drift_outcome
        and now - (r.get("ts") or 0) < week
    ]
    if prompt_drift:
        problems.append(
            "`{}` reported prompt drift {} time(s) this week. The routine "
            "prompt differs from the checked-in file; sync it before relying "
            "on scheduled work. Most recent at <t:{}:f>.".format(
                agent,
                len(prompt_drift),
                int(prompt_drift[-1].get("ts") or 0),
            )
        )

    errored = [
        r for r in rows
        if r.get("outcome") == "errored" and now - (r.get("ts") or 0) < week
    ]
    if len(errored) >= error_threshold:
        notes = [r.get("note") for r in errored[-3:] if r.get("note")]
        problems.append(
            "`{}` errored {} times this week.{}".format(
                agent, len(errored),
                (" Most recent: " + "; ".join(notes)) if notes else "",
            )
        )
    return problems
