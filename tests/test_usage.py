"""usage.py — normalising two vendors' shapes, and the pace gate.

The gate's failure mode that matters is reading *low*: every stale or partial
reading understates usage, because a window's used percentage only rises. So
every ambiguous case here must resolve to "unknown", never to "under pace".
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import usage  # noqa: E402

NOW = 1_788_600_000.0
WEEK = usage.SEVEN_DAY


def seven_day(used, elapsed_fraction):
    """A 7-day window that is `elapsed_fraction` of the way through."""
    return {
        "source": "test",
        "captured_at": NOW,
        "windows": {
            "seven_day": {
                "used_percent": used,
                "resets_at": NOW + WEEK * (1 - elapsed_fraction),
            }
        },
    }


# -- the weekly pace line ---------------------------------------------------


def test_spending_the_week_on_monday_is_over_pace():
    """The point of a proportional line: capacity refills on a schedule Nate
    does not control, so an even burn beats a sprint."""
    verdict = usage.pace(seven_day(60.0, 0.15), NOW)
    assert verdict["over_pace"]


def test_an_even_burn_is_under_pace():
    # allowed = 45%; 25 used + 15 reserved = 40
    verdict = usage.pace(seven_day(25.0, 0.5), NOW)
    assert not verdict["over_pace"]


def test_the_gate_reserves_the_cost_of_the_run_it_authorises():
    """Without this the gate is a start check, not a bound on spend: it waves
    through a run that then blows straight past the line."""
    # allowed = 45%, and 44% used is under it — but not with a run's cost to come
    assert usage.pace(seven_day(44.0, 0.5), NOW)["over_pace"]


def test_the_five_hour_reserve_applies_too():
    under = {"windows": {"five_hour": {"used_percent": 45.0, "resets_at": NOW}}}
    over = {"windows": {"five_hour": {"used_percent": 55.0, "resets_at": NOW}}}
    assert not usage.pace(under, NOW)["over_pace"]   # 45 + 30 = 75 < 80
    assert usage.pace(over, NOW)["over_pace"]        # 55 + 30 = 85 > 80


def test_the_target_leaves_headroom_at_the_end_of_the_week():
    """At the very end of the window the line is WEEKLY_TARGET, not 100 —
    the remainder is Nate's."""
    verdict = usage.pace(seven_day(95.0, 1.0), NOW)
    assert verdict["over_pace"]
    assert verdict["windows"][0]["allowed_percent"] == usage.WEEKLY_TARGET


def test_a_fresh_window_allows_almost_nothing():
    verdict = usage.pace(seven_day(5.0, 0.0), NOW)
    assert verdict["over_pace"]


# -- the five-hour ceiling --------------------------------------------------


def test_the_five_hour_window_has_a_flat_ceiling():
    """The rolling 5-hour window, not the weekly one, is what actually locks
    Nate out of his own account."""
    reading = {"source": "test", "captured_at": NOW,
               "windows": {"five_hour": {"used_percent": 85.0, "resets_at": NOW + 600}}}
    assert usage.pace(reading, NOW)["over_pace"]


def test_either_window_alone_can_stop_a_run():
    reading = {"source": "test", "captured_at": NOW, "windows": {
        "five_hour": {"used_percent": 95.0, "resets_at": NOW + 600},
        "seven_day": {"used_percent": 1.0, "resets_at": NOW + WEEK * 0.02},
    }}
    verdict = usage.pace(reading, NOW)
    assert verdict["over_pace"]
    assert [v["over"] for v in verdict["windows"]] == [True, False]


# -- unknown is never "under pace" -----------------------------------------


def test_no_windows_is_unknown_not_healthy():
    assert not usage.pace({"windows": {}}, NOW)["known"]


def test_a_window_with_no_percentage_is_ignored_rather_than_read_as_zero():
    reading = {"windows": {"five_hour": {"used_percent": None, "resets_at": NOW}}}
    verdict = usage.pace(reading, NOW)
    assert not verdict["known"]
    assert not verdict["over_pace"]


def test_a_seven_day_window_with_no_reset_cannot_be_paced():
    """Without resets_at there is no way to know how far through the window we
    are, so the line is unknowable — and guessing would guess generously."""
    reading = {"windows": {"seven_day": {"used_percent": 99.0, "resets_at": None}}}
    assert not usage.pace(reading, NOW)["known"]


# -- reading the two vendors' shapes ---------------------------------------


def test_codex_windows_are_matched_by_length_not_by_position(tmp_path, monkeypatch):
    """primary/secondary are positional names. A vendor may renumber them; the
    window length is the durable fact."""
    session = tmp_path / "2026" / "09" / "05"
    session.mkdir(parents=True)
    record = {"timestamp": "2026-09-05T08:00:00.000Z", "payload": {"rate_limits": {
        "primary": {"used_percent": 11.0, "window_minutes": 10080, "resets_at": 1},
        "secondary": {"used_percent": 22.0, "window_minutes": 300, "resets_at": 2},
    }}}
    (session / "rollout-x.jsonl").write_text(json.dumps(record) + "\n")
    monkeypatch.setattr(usage, "CODEX_SESSIONS", str(tmp_path / "*/*/*/*.jsonl"))

    reading = usage.read_codex()
    assert reading["windows"]["seven_day"]["used_percent"] == 11.0
    assert reading["windows"]["five_hour"]["used_percent"] == 22.0


def test_codex_takes_the_most_recent_reading_across_sessions(tmp_path, monkeypatch):
    session = tmp_path / "2026" / "09" / "05"
    session.mkdir(parents=True)
    for stamp, used in (("2026-09-05T07:00:00.000Z", 10.0),
                        ("2026-09-05T09:00:00.000Z", 40.0),
                        ("2026-09-05T08:00:00.000Z", 20.0)):
        (session / ("rollout-%s.jsonl" % used)).write_text(json.dumps({
            "timestamp": stamp,
            "rate_limits": {"primary": {"used_percent": used, "window_minutes": 300,
                                        "resets_at": 1}},
        }) + "\n")
    monkeypatch.setattr(usage, "CODEX_SESSIONS", str(tmp_path / "*/*/*/*.jsonl"))
    assert usage.read_codex()["windows"]["five_hour"]["used_percent"] == 40.0


def test_no_codex_sessions_reads_as_none_not_as_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CODEX_SESSIONS", str(tmp_path / "nothing/*.jsonl"))
    assert usage.read_codex() is None


def test_claude_cache_is_normalised_to_the_same_shape(tmp_path, monkeypatch):
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({
        "captured_at": 1788600000,
        "five_hour": {"used_percentage": 23.5, "resets_at": 1788601000},
    }))
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(cache))
    reading = usage.read_claude()
    assert reading["windows"]["five_hour"]["used_percent"] == 23.5
    assert "seven_day" not in reading["windows"]


def test_an_empty_claude_cache_reads_as_none(tmp_path, monkeypatch):
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({"captured_at": 1788600000}))
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(cache))
    assert usage.read_claude() is None


def test_a_missing_claude_cache_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(tmp_path / "absent.json"))
    assert usage.read_claude() is None


# -- the gate's exit codes --------------------------------------------------


def test_gate_exits_2_when_nothing_can_be_read(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(tmp_path / "absent.json"))
    assert usage.main(["gate", "claude"]) == 2


def test_gate_exits_2_on_a_stale_reading(tmp_path, monkeypatch):
    """Called before the session did any work, or the app is not running.
    Either way the number is too low to trust."""
    import time as _time
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({
        "captured_at": int(_time.time()) - usage.MAX_AGE - 60,
        "five_hour": {"used_percentage": 1.0, "resets_at": int(_time.time()) + 600},
    }))
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(cache))
    assert usage.main(["gate", "claude"]) == 2


def test_gate_exits_2_on_a_reading_from_the_future(tmp_path, monkeypatch):
    """Clock skew must not buy free headroom."""
    import time as _time
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({
        "captured_at": int(_time.time()) + 3600,
        "five_hour": {"used_percentage": 1.0, "resets_at": int(_time.time()) + 600},
    }))
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(cache))
    assert usage.main(["gate", "claude"]) == 2
