#!/usr/bin/env python3
"""Provider weekly-window bounds with DST-transition validation.

Ticket #2380-blocked #2384 (parent #2376). Read-only analysis support: no
live pacing, admission, display, scheduler, or spending-brake change.

First verification (recorded here, then built contingent on it):

- Local Monday-UTC assumption: ``usage.py:509-520`` anchors the provider
  week at Monday 00:00 UTC (``MUSE_WINDOW_ANCHOR_WEEKDAY = 0``, line 520),
  implemented by ``muse_window_start`` (``usage.py:532``). Its comment
  notes three observed resets on that lattice (2026-09-14 and 2026-09-21,
  both named by 429 refusals, plus the panel's "Resets Sep 27 at 5:00 PM").
  That lattice equals Sunday 17:00 America/Los_Angeles only while PDT
  (UTC-7) holds; in PST (UTC-8) the provider reset is Monday 01:00 UTC.
- Verified provider reset: owner confirmation Sundays at 5pm
  (https://github.com/nateprich-projects/command-center/issues/2376,
  2026-10-07T06:02:57Z, interpreted per plan as Sunday 17:00
  America/Los_Angeles preserving DST); the #2379 inventory
  (``muse_weekly_panel_readings_v1.json``, ``reset_rule`` "Sunday 17:00
  America/Los_Angeles, preserving DST") files each weekly record under
  that zoned reset and flags the local Monday-UTC code as distinct from
  it on weekly records.

Contingent build: ``weekly_window_bounds`` files an instant under the
zoned Sunday-17:00 reset with zone-aware datetimes, so the autumn
transition week is 169 hours and the spring week 167; filing PST
instants under Monday 00:00 UTC mixes unlike weekly periods into one
fit. ``validate_dst_transition`` passes those bounds and fails the UTC
equivalence in PST.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo


METHOD_VERSION = "v1"
RESET_RULE = "Sunday 17:00 America/Los_Angeles, preserving DST"

PROVIDER_ZONE = ZoneInfo("America/Los_Angeles")
RESET_WEEKDAY = 6  # Monday is 0, so 6 is Sunday
RESET_HOUR = 17


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


def weekly_window_bounds(epoch: float) -> dict:
    """Bounds of the provider weekly window containing ``epoch``.

    Returns the opening/closing instants as epochs, UTC ISO strings, and
    the provider-local window id. A DST or window misalignment that files
    an event under the wrong bounds mixes unlike weekly periods into one
    fit, which is what the Ticket 4 holdout and residual checks guard.
    """
    start = window_start(epoch)
    end = window_end(start)
    return {
        "method_version": METHOD_VERSION,
        "reset_rule": RESET_RULE,
        "window_id": window_id(start),
        "start": start,
        "end": end,
        "start_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "length_hours": (end - start) / 3600.0,
    }


def monday_utc_window_start(now: float) -> float:
    """Mirror of ``usage.muse_window_start``: the Monday 00:00 UTC lattice.

    Kept here (rather than imported) so the DST comparison has no runtime
    dependency; a test asserts it still matches ``usage.muse_window_start``
    on the probe instants, keeping the two from drifting apart silently.
    """
    moment = datetime.fromtimestamp(now, timezone.utc)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    back = (midnight.weekday() - 0) % 7
    return (midnight - timedelta(days=back)).timestamp()


def _utc(*parts: int) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


#: (instant, expected zone-aware window start) probes spanning PDT, PST,
#: and both DST transitions. PST rows are the ones Monday 00:00 UTC misses.
DST_PROBES: tuple[tuple[float, float], ...] = (
    # PDT: Sunday 17:00 local is Monday 00:00 UTC; both lattices agree.
    (_utc(2026, 10, 5, 0, 0), _utc(2026, 10, 5, 0, 0)),
    (_utc(2026, 10, 4, 23, 59, 59), _utc(2026, 9, 28, 0, 0)),
    # PST: Sunday 17:00 local is Monday 01:00 UTC, not 00:00.
    (_utc(2026, 11, 9, 0, 30), _utc(2026, 11, 2, 1, 0)),
    (_utc(2026, 11, 9, 1, 0), _utc(2026, 11, 9, 1, 0)),
    # Week spanning the PDT->PST change (Sun 2026-11-01 02:00 local).
    (_utc(2026, 11, 2, 0, 30), _utc(2026, 10, 26, 0, 0)),
    # Week spanning the PST->PDT change (Sun 2026-03-08 02:00 local).
    (_utc(2026, 3, 9, 0, 0), _utc(2026, 3, 9, 0, 0)),
    (_utc(2026, 3, 8, 23, 59), _utc(2026, 3, 2, 1, 0)),
)


def validate_dst_transition(
    start_fn: Callable[[float], float] | None = None,
) -> dict:
    """Check a window-start function against the DST probe lattice.

    Defaults to the zone-aware ``window_start``. Returns a pass/fail
    report; endpoint-style sampling here illustrates sensitivity only and
    is not a certified bound. Pass ``monday_utc_window_start`` to see the
    UTC equivalence fail in PST while passing in PDT.
    """
    fn = start_fn or window_start
    failures = []
    for instant, expected in DST_PROBES:
        got = fn(instant)
        if got != expected:
            failures.append({
                "instant_utc": datetime.fromtimestamp(
                    instant, timezone.utc).isoformat(),
                "expected_start_utc": datetime.fromtimestamp(
                    expected, timezone.utc).isoformat(),
                "got_start_utc": datetime.fromtimestamp(
                    got, timezone.utc).isoformat(),
            })
    return {
        "method_version": METHOD_VERSION,
        "reset_rule": RESET_RULE,
        "checked": getattr(fn, "__name__", "window_start"),
        "probes": len(DST_PROBES),
        "passed": not failures,
        "failures": failures,
    }
