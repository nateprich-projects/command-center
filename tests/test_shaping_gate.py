"""The optional shaping job only uses the existing idle-rule headroom."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import usage  # noqa: E402


def reading(used):
    return {"windows": {"five_hour": {"used_percent": used}}}


def test_shaping_allows_genuine_headroom_under_the_idle_ceiling():
    """The 15% boundary is reused from usage.py's existing idle rule."""
    assert usage.shaping_allowed(
        reading(usage.IDLE_WINDOW_CEILING - 0.1)
    )


def test_shaping_allows_the_existing_idle_ceiling_boundary():
    assert usage.shaping_allowed(reading(usage.IDLE_WINDOW_CEILING))


def test_shaping_denies_a_tight_five_hour_window():
    assert not usage.shaping_allowed(
        reading(usage.IDLE_WINDOW_CEILING + 0.1)
    )


def test_shaping_denies_missing_or_malformed_usage():
    assert not usage.shaping_allowed({"windows": {}})
    assert not usage.shaping_allowed(reading(None))
    assert not usage.shaping_allowed(reading("not-a-number"))
    assert not usage.shaping_allowed(reading(float("nan")))



def muse_reading(used, rolling=True):
    """The shape `read_muse` returns: a rolling seven-day total, no five-hour window."""
    return {
        "source": "muse",
        "windows": {
            "seven_day": {
                "used_percent": used,
                "resets_at": 2_000_000_000.0,
                "rolling": rolling,
            }
        },
    }


def test_shaping_allows_a_rolling_week_under_its_ceiling():
    """#1129: Muse's reader has no five-hour window; its rolling week gates instead."""
    assert usage.shaping_allowed(muse_reading(46.06))


def test_shaping_denies_a_rolling_week_over_its_ceiling():
    assert not usage.shaping_allowed(muse_reading(99.9))
    assert not usage.shaping_allowed(muse_reading(120.0))


def test_shaping_denies_a_malformed_or_non_rolling_week():
    assert not usage.shaping_allowed(muse_reading(None))
    assert not usage.shaping_allowed(muse_reading("not-a-number"))
    assert not usage.shaping_allowed(muse_reading(float("nan")))
    assert not usage.shaping_allowed(muse_reading(True))
    assert not usage.shaping_allowed(muse_reading(10.0, rolling=False))


def test_a_five_hour_window_still_takes_the_idle_rule():
    tight = muse_reading(10.0)
    tight["windows"]["five_hour"] = {"used_percent": usage.IDLE_WINDOW_CEILING + 1}
    assert not usage.shaping_allowed(tight)
