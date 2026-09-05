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


def test_the_weekly_window_is_anchored_to_a_known_reset():
    """A trailing count keeps last week's tokens past a reset, refusing work
    against a completely fresh budget."""
    import datetime as _dt
    for probe in (NOW, NOW + 86400 * 3, NOW + 86400 * 6):
        reset = usage.last_weekly_reset(probe)
        assert reset <= probe
        assert probe - reset < 7 * 86400
        assert _dt.datetime.fromtimestamp(reset).weekday() == usage.WEEKLY_RESET_WEEKDAY
        assert _dt.datetime.fromtimestamp(reset).hour == usage.WEEKLY_RESET_HOUR


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
    under = {"windows": {"five_hour": {"used_percent": 65.0, "resets_at": NOW}}}
    over = {"windows": {"five_hour": {"used_percent": 75.0, "resets_at": NOW}}}
    assert not usage.pace(under, NOW)["over_pace"]   # 65 + 10 = 75 < 80
    assert usage.pace(over, NOW)["over_pace"]        # 75 + 10 = 85 > 80


def test_the_reserves_are_sized_against_a_real_run_not_a_guess():
    """A run with nothing to do cost 2,690 output tokens; ten times that is
    ~1.5% of a weekly window. Reserves an order of magnitude above that refused
    work the budget had ample room for."""
    biggest_plausible_run = 27_000.0
    assert usage.WEEKLY_RESERVE > 100 * biggest_plausible_run / usage.WEEKLY_CAPACITY
    assert usage.FIVE_HOUR_RESERVE > 100 * biggest_plausible_run / usage.FIVE_HOUR_CAPACITY


def test_the_target_leaves_headroom_at_the_end_of_the_week():
    """At the very end of the window the line is WEEKLY_TARGET, not 100 —
    the remainder is Nate's."""
    verdict = usage.pace(seven_day(95.0, 1.0), NOW)
    assert verdict["over_pace"]
    assert verdict["windows"][0]["allowed_percent"] == usage.WEEKLY_TARGET


def test_a_fresh_window_allows_the_floor_not_zero():
    """Without a floor the line starts at zero, the reserve alone exceeds it,
    and nothing runs for the first day of every week — a dead zone at exactly
    the moment the budget is most free."""
    assert not usage.pace(seven_day(5.0, 0.0), NOW)["over_pace"]
    assert usage.pace(seven_day(5.0, 0.0), NOW)["windows"][0]["allowed_percent"] == \
        usage.WEEKLY_FLOOR


def test_the_floor_does_not_license_spending_the_week_on_day_one():
    """It permits a little early work, not a sprint."""
    assert usage.pace(seven_day(40.0, 0.02), NOW)["over_pace"]


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
    """Unknown still fails closed — but unknown now means neither the real
    reading nor the local estimate is available."""
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "none/*.jsonl"))
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
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "none/*.jsonl"))
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
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "none/*.jsonl"))
    assert usage.main(["gate", "claude"]) == 2


# -- the local token estimate ----------------------------------------------


def transcript(path, entries, ts="2026-09-05T08:00:00.000Z"):
    """entries: (model, output_tokens) or (model, output_tokens, timestamp)."""
    lines = []
    for e in entries:
        model, out = e[0], e[1]
        stamp = e[2] if len(e) > 2 else ts
        lines.append(json.dumps({
            "type": "assistant", "timestamp": stamp,
            "message": {"role": "assistant", "model": model,
                        "usage": {"output_tokens": out}},
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


def _at(now, hours_ago):
    import datetime
    t = datetime.datetime.utcfromtimestamp(now - hours_ago * 3600)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_only_opus_is_budgeted(tmp_path, monkeypatch):
    """Opus is what consumes a window. The 5-hour window that came closest to
    the limit carried 713k Opus tokens and 44k of everything else."""
    import time as _time
    now = _time.time()
    transcript(tmp_path / "a.jsonl", [
        ("claude-opus-5", 1000, _at(now, 1)),
        ("claude-sonnet-5", 50000, _at(now, 1)),
        ("claude-haiku-4-5", 50000, _at(now, 1)),
    ])
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "*.jsonl"))
    assert usage.read_claude_local(now)["opus_output_tokens"]["five_hour"] == 1000


def test_a_sonnet_only_session_reads_as_no_usage(tmp_path, monkeypatch):
    """Not zero-but-known — genuinely nothing budgeted happened."""
    import time as _time
    now = _time.time()
    transcript(tmp_path / "a.jsonl", [("claude-sonnet-5", 90000, _at(now, 1))])
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "*.jsonl"))
    assert usage.read_claude_local(now) is None


def test_records_outside_the_window_are_not_counted(tmp_path, monkeypatch):
    import time as _time
    now = _time.time()
    # Pin the weekly anchor a full week back, so this exercises the window
    # boundaries rather than accidentally testing where today falls relative
    # to Saturday noon.
    monkeypatch.setattr(usage, "last_weekly_reset", lambda n: n - 7 * 86400)
    transcript(tmp_path / "a.jsonl", [
        ("claude-opus-5", 1000, _at(now, 1)),
        ("claude-opus-5", 500, _at(now, 20)),
        ("claude-opus-5", 9999, _at(now, 24 * 9)),
    ])
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "*.jsonl"))
    w = usage.read_claude_local(now)["opus_output_tokens"]
    assert w["five_hour"] == 1000
    assert w["seven_day"] == 1500


def test_no_transcripts_reads_as_none_not_as_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "none/*.jsonl"))
    assert usage.read_claude_local(NOW) is None


# -- promos are read at runtime, never written into the file ----------------


def promo_config(tmp_path, monkeypatch, text, bar="seven_day"):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"cachedGrowthBookFeatures": {
        "tengu_rate_limit_promo_notices": [{"bar": bar, "text": text}]}}))
    monkeypatch.setattr(usage, "CLAUDE_APP_CONFIG", str(cfg))
    return cfg


def test_a_live_promo_raises_capacity(tmp_path, monkeypatch):
    promo_config(tmp_path, monkeypatch, "+50% weekly limits promo through Dec 31")
    assert usage.promo_multiplier("seven_day", NOW) == 1.5
    assert usage.capacity("seven_day", NOW) == usage.WEEKLY_CAPACITY * 1.5


def test_a_lapsed_promo_is_ignored(tmp_path, monkeypatch):
    """A stale cached notice must not keep inflating capacity."""
    promo_config(tmp_path, monkeypatch, "+50% weekly limits promo through Jan 2")
    assert usage.promo_multiplier("seven_day", NOW) == 1.0


def test_an_unparseable_promo_is_ignored(tmp_path, monkeypatch):
    """Assuming a boost that is not real permits overspending; ignoring a real
    one only makes the gate stricter. Fail toward strict."""
    promo_config(tmp_path, monkeypatch, "bigger limits for a while!")
    assert usage.promo_multiplier("seven_day", NOW) == 1.0


def test_a_promo_applies_only_to_its_own_window(tmp_path, monkeypatch):
    promo_config(tmp_path, monkeypatch, "+50% weekly limits through Dec 31",
                 bar="seven_day")
    assert usage.promo_multiplier("five_hour", NOW) == 1.0


def test_a_missing_app_config_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CLAUDE_APP_CONFIG", str(tmp_path / "absent.json"))
    assert usage.promo_multiplier("seven_day", NOW) == 1.0


# -- the estimate is a fallback, never a preference -------------------------


def test_a_rolling_estimate_gets_a_flat_ceiling_not_a_pace_line():
    """A trailing window has no cycle position, so the proportional line would
    read 0% allowed forever — the failure that made the gate unusable."""
    reading = {"estimated": True, "windows": {
        "seven_day": {"used_percent": 50.0, "rolling": True,
                      "resets_at": NOW + WEEK}}}
    verdict = usage.pace(reading, NOW)
    assert verdict["windows"][0]["allowed_percent"] == usage.WEEKLY_TARGET
    assert verdict["windows"][0]["elapsed_fraction"] is None


def test_a_real_reading_still_gets_the_proportional_line():
    verdict = usage.pace(seven_day(50.0, 0.5), NOW)
    assert verdict["windows"][0]["allowed_percent"] < usage.WEEKLY_TARGET


def test_a_fresh_real_reading_beats_the_estimate(tmp_path, monkeypatch):
    import time as _time
    now = _time.time()
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({"captured_at": int(now),
                                 "five_hour": {"used_percentage": 3.0,
                                               "resets_at": int(now + 600)}}))
    transcript(tmp_path / "a.jsonl", [("claude-opus-5", 99999, _at(now, 1))])
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(cache))
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "*.jsonl"))
    assert usage.read_agent("claude", now)["source"] == "claude"


def test_a_stale_real_reading_falls_back_to_the_estimate(tmp_path, monkeypatch):
    """The actual failure: no scheduled run ever writes the cache."""
    import time as _time
    now = _time.time()
    cache = tmp_path / "usage.json"
    cache.write_text(json.dumps({"captured_at": int(now) - usage.MAX_AGE - 600,
                                 "five_hour": {"used_percentage": 3.0,
                                               "resets_at": int(now)}}))
    transcript(tmp_path / "a.jsonl", [("claude-opus-5", 1000, _at(now, 1))])
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(cache))
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "*.jsonl"))
    assert usage.read_agent("claude", now)["source"] == "claude-local-estimate"


def test_the_gate_can_now_pass_for_claude(tmp_path, monkeypatch):
    """The whole point: a scheduled run must be able to proceed."""
    import time as _time
    now = _time.time()
    monkeypatch.setattr(usage, "CLAUDE_CACHE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(usage, "CLAUDE_APP_CONFIG", str(tmp_path / "absent.json"))
    transcript(tmp_path / "a.jsonl", [("claude-opus-5", 100, _at(now, 1))])
    monkeypatch.setattr(usage, "CLAUDE_TRANSCRIPTS", str(tmp_path / "*.jsonl"))
    assert usage.main(["gate", "claude"]) == 0
