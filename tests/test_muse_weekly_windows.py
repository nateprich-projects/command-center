"""Ticket #2384: provider weekly-window bounds across the DST transition.

All fixtures are synthetic epoch probes mirroring the field shapes of real
panel/reset evidence; no raw journals or live provider reads here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import muse_weekly_windows as windows  # noqa: E402


def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


# -- weekly_window_bounds: DST or window misalignment mixes unlike periods --


def test_bounds_match_sunday_1700_los_angeles_probes():
    for instant, expected in windows.DST_PROBES:
        bounds = windows.weekly_window_bounds(instant)
        assert bounds["start"] == expected
        assert bounds["window_id"] == windows.window_id(expected)
        assert bounds["end"] == windows.window_end(expected)
        assert bounds["reset_rule"] == windows.RESET_RULE


def test_dst_transition_weeks_are_169_and_167_hours():
    fall = windows.weekly_window_bounds(_utc(2026, 10, 27, 12, 0))
    assert fall["window_id"] == "2026-10-25T17:00:00-07:00"
    assert fall["length_hours"] == 169.0
    spring = windows.weekly_window_bounds(_utc(2026, 3, 4, 12, 0))
    assert spring["window_id"] == "2026-03-01T17:00:00-08:00"
    assert spring["length_hours"] == 167.0


def test_pst_boundary_events_file_into_different_windows():
    # Sun 2026-11-01 16:30 PST (old window) vs 17:30 PST (new window).
    before = _utc(2026, 11, 2, 0, 30)
    after = _utc(2026, 11, 2, 1, 30)
    old = windows.weekly_window_bounds(before)
    new = windows.weekly_window_bounds(after)
    assert old["window_id"] == "2026-10-25T17:00:00-07:00"
    assert new["window_id"] == "2026-11-01T17:00:00-08:00"
    # The Monday-UTC lattice files both under one Monday, mixing unlike
    # provider weeks into a single fit window.
    assert windows.monday_utc_window_start(before) == \
        windows.monday_utc_window_start(after) == _utc(2026, 11, 2, 0, 0)


def test_monday_utc_misalignment_is_one_hour_in_pst():
    # In PST the provider reset (Monday 01:00 UTC) sits one hour after the
    # local Monday-UTC assumption (Monday 00:00 UTC): at the reset instant
    # the zoned lattice opens a new window while Monday-UTC still files
    # under the hour-old one.
    at_reset = _utc(2026, 11, 9, 1, 0)
    assert windows.window_start(at_reset) == at_reset
    assert windows.monday_utc_window_start(at_reset) == _utc(2026, 11, 9, 0, 0)
    assert windows.window_start(at_reset) - \
        windows.monday_utc_window_start(at_reset) == 3600.0


def test_monday_utc_mirror_matches_live_usage_code():
    import usage
    for instant, _ in windows.DST_PROBES:
        assert windows.monday_utc_window_start(instant) == \
            usage.muse_window_start(instant)


def test_bounds_match_exposure_table_windows():
    import muse_exposure_table as exposure
    for instant, _ in windows.DST_PROBES:
        assert windows.window_start(instant) == exposure.window_start(instant)
        bounds = windows.weekly_window_bounds(instant)
        assert bounds["end"] == exposure.window_end(bounds["start"])
        assert bounds["window_id"] == exposure.window_id(bounds["start"])


# -- validate_dst_transition: fails UTC equivalence in PST, passes zoned ----


def test_validate_dst_transition_passes_zone_aware_bounds():
    report = windows.validate_dst_transition()
    assert report["checked"] == "window_start"
    assert report["probes"] == len(windows.DST_PROBES)
    assert report["passed"] is True
    assert report["failures"] == []


def test_validate_dst_transition_fails_utc_equivalence_in_pst():
    report = windows.validate_dst_transition(windows.monday_utc_window_start)
    assert report["passed"] is False
    instants = {f["instant_utc"] for f in report["failures"]}
    # The PST rows fail; the PDT agreement rows do not.
    assert "2026-11-09T00:30:00+00:00" in instants
    assert "2026-10-05T00:00:00+00:00" not in instants
