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

