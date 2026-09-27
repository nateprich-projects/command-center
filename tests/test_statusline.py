"""statusline.sh — rendering, and the usage cache it writes.

The interesting cases are all absences: `rate_limits` does not exist until the
first API response of a session, each window is independently optional, and
Claude Code drops a window once its `resets_at` has passed. All three look the
same to a naive reader, and getting them wrong overstates headroom.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "statusline.sh"


def run(payload: dict, cache: pathlib.Path):
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={"HOME": str(cache.parent), "PATH": "/usr/bin:/bin:/usr/local/bin",
             "COMMAND_CENTER_USAGE_CACHE": str(cache)},
    )
    assert proc.returncode == 0, proc.stderr
    return re.sub(r"\033\[[0-9;]*m", "", proc.stdout)  # strip colour


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "usage.json"


def session(**rate_limits):
    payload = {"model": {"display_name": "Opus 5"}, "workspace": {"current_dir": "/tmp"}}
    if rate_limits:
        payload["rate_limits"] = rate_limits
    return payload


def soon(seconds):
    return int(time.time()) + seconds


def test_both_windows_are_rendered_and_cached(cache):
    week = soon(86400 * 2)
    out = run(
        session(
            five_hour={"used_percentage": 23.5, "resets_at": soon(3600)},
            seven_day={"used_percentage": 41.2, "resets_at": week},
        ),
        cache,
    )
    assert "5h 24%" in out and "7d 41%" in out

    written = json.loads(cache.read_text())
    assert written["five_hour"]["used_percentage"] == 23.5
    # statusline.sh copies this reset value into the cache unchanged.
    assert written["seven_day"]["resets_at"] == week
    assert written["captured_at"] > 0


def test_a_missing_window_is_omitted_not_rendered_as_zero(cache):
    """Absent means unknown. Unknown reading as 'plenty left' is the exact
    failure the budget exists to prevent."""
    out = run(session(seven_day={"used_percentage": 88.0, "resets_at": soon(60)}), cache)
    assert "7d 88%" in out
    assert "5h" not in out
    assert "0%" not in out
    assert "five_hour" not in json.loads(cache.read_text())


def test_an_absent_field_does_not_shift_the_later_ones(cache):
    """The fields are read positionally, so an empty one must stay empty rather
    than letting every later value slide left. Getting this wrong labelled the
    seven-day number as the five-hour one."""
    payload = {"workspace": {"current_dir": "/tmp"},
               "rate_limits": {"seven_day": {"used_percentage": 88.0,
                                             "resets_at": soon(86400)}}}
    out = run(payload, cache)          # no model, no five_hour
    assert "7d 88%" in out
    assert "5h" not in out
    assert "tmp" in out                # the directory did not slide into a percentage


def test_percentages_round_up_not_down(cache):
    """Truncation always understates usage, and understating is what burns the
    week."""
    out = run(session(five_hour={"used_percentage": 89.6, "resets_at": soon(600)}), cache)
    assert "5h 90%" in out


def test_absent_rate_limits_says_so_rather_than_looking_healthy(cache):
    out = run(session(), cache)
    assert "usage unknown" in out
    assert "%" not in out


def test_absent_rate_limits_does_not_erase_a_previous_reading(cache):
    """'Not reported yet' is not the same as 'no usage'."""
    run(session(five_hour={"used_percentage": 50.0, "resets_at": soon(3600)}), cache)
    before = cache.read_text()
    run(session(), cache)
    assert cache.read_text() == before


def test_a_present_payload_replaces_the_file_wholesale(cache):
    """A window that vanished has genuinely expired and must not linger."""
    run(
        session(
            five_hour={"used_percentage": 50.0, "resets_at": soon(3600)},
            seven_day={"used_percentage": 10.0, "resets_at": soon(86400)},
        ),
        cache,
    )
    run(session(seven_day={"used_percentage": 12.0, "resets_at": soon(86400)}), cache)
    written = json.loads(cache.read_text())
    assert "five_hour" not in written
    assert written["seven_day"]["used_percentage"] == 12.0


def test_an_already_passed_reset_renders_no_countdown(cache):
    out = run(session(five_hour={"used_percentage": 5.0, "resets_at": soon(-60)}), cache)
    assert "5h 5%" in out
    assert "↻" not in out


def test_the_cache_is_valid_json_and_leaves_no_temp_files(cache):
    run(session(five_hour={"used_percentage": 1.0, "resets_at": soon(600)}), cache)
    json.loads(cache.read_text())
    assert list(cache.parent.glob("usage.json.*")) == []


def test_it_survives_junk_on_stdin(cache):
    """A malformed payload must not take the status line down with it."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        input="not json at all",
        capture_output=True,
        text=True,
        env={"HOME": str(cache.parent), "PATH": "/usr/bin:/bin:/usr/local/bin",
             "COMMAND_CENTER_USAGE_CACHE": str(cache)},
    )
    assert proc.returncode == 0
    assert not cache.exists()
