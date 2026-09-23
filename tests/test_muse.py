"""Muse's local cost reader."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import usage  # noqa: E402

NOW = 1_788_800_000.0


def _muse_record(at, *, input_tokens=0, cached_tokens=0, output_tokens=0,
                 family="provider", usage_id=None, run_id=None):
    record = {
        "recorded_at": int(at * 1_000_000),
        "payload": {"event": {
            "kind": "goal_usage_attribution",
            "record": {
                "usage_family": family,
                "quantity": {
                    "input_tokens": input_tokens,
                    "cached_tokens": cached_tokens,
                    "output_tokens": output_tokens,
                    "reported": True,
                },
            },
        }},
    }
    if usage_id is not None:
        record["payload"]["event"]["record"]["usage_id"] = usage_id
    if run_id is not None:
        record["payload"]["event"]["record"]["owner"] = {"run_id": run_id}
    return record


def _muse_model_record(at, run_id, model_id):
    """The `run.model.configured` event a usage record joins to (#1302).

    A provider usage record carries no model id of its own, only
    `owner.run_id`; this is where the model name lives.
    """
    return {
        "recorded_at": int(at * 1_000_000),
        "payload_type": "run.model.configured",
        "payload": {"record": {
            "run_stream": {"kind": "run", "id": run_id},
            "model_id": model_id,
            "provider_id": "meta",
        }},
    }


def _muse_fixture(tmp_path, monkeypatch, records, mtime=None):
    session = tmp_path / "2026" / "09" / "18" / "session"
    session.mkdir(parents=True)
    journal = session / "session.jsonl"
    journal.write_text(
        "".join(json.dumps(record) + "\n" for record in records))
    if mtime is not None:
        # The reader skips a journal last written before the window opened,
        # so a reading dated after today needs a journal dated with it.
        os.utime(journal, (mtime, mtime))
    monkeypatch.setattr(
        usage, "MUSE_SESSIONS", str(tmp_path / "*/*/*/*/session.jsonl")
    )


def test_the_two_registries_agree_about_muse():
    """`heartbeat.py` and `usage.py` each keep a provider map, and a routine
    that is registered in one but not the other fails halfway through its own
    opening — `funnel begin` accepts the agent, then heartbeat rejects it."""
    assert heartbeat.PROVIDERS["muse"] == "meta"
    assert usage.PROVIDERS["muse"] == "meta"
    assert heartbeat.HARNESSES["muse"] == "muse-code"


def test_muse_reader_prices_provider_calls(tmp_path, monkeypatch):
    """Provider attribution is priced; non-provider events are not."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            NOW - 3600, input_tokens=3_000_000, cached_tokens=2_000_000,
            output_tokens=3_000_000, usage_id="provider-1"
        ),
        _muse_record(
            NOW - 3600, input_tokens=9_000_000, output_tokens=9_000_000,
            family="tool", usage_id="tool-1"
        ),
        _muse_record(
            NOW - usage.SEVEN_DAY - 1, input_tokens=9_000_000,
            output_tokens=9_000_000, usage_id="old-1"
        ),
    ])
    reading = usage.read_agent("muse", NOW)
    assert reading["source"] == "muse"
    assert reading["spent_dollars"] == pytest.approx(14.30)
    assert reading["cap_dollars"] == 200.0
    window = reading["windows"]["seven_day"]
    assert window["spent_dollars"] == pytest.approx(14.30)
    assert window["used_percent"] == pytest.approx(7.15)
    assert window["rolling"] is True
    assert window["calls"] == 1

    # An agent nobody registered has no pool: a configuration error, not an
    # empty budget.
    assert usage.read_agent("nonesuch", NOW) is None


def test_muse_reader_counts_only_the_current_provider_window(tmp_path, monkeypatch):
    """#1182: the total is anchored to the provider's reset, not to now - 7d.

    A trailing seven days straddles two weekly windows, so it can hold two
    windows' spend against a cap calibrated from one. Measured 2026-09-20 the
    rolling reading was 112% of the cap while the live window held 0.26% of it.
    """
    window_start = usage.muse_window_start(NOW)
    assert window_start < NOW
    assert NOW - window_start < usage.SEVEN_DAY
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            window_start + 60, input_tokens=1_000_000, usage_id="inside"
        ),
        _muse_record(
            window_start - 60, input_tokens=100_000_000, usage_id="last-week"
        ),
    ])
    reading = usage.read_agent("muse", NOW)
    window = reading["windows"]["seven_day"]
    assert window["calls"] == 1
    assert reading["spent_dollars"] == pytest.approx(1.25)
    assert window["window_start"] == window_start
    assert window["resets_at"] == window_start + usage.SEVEN_DAY


def test_muse_window_start_is_the_monday_utc_lattice():
    """The observed resets all sit on Monday 00:00 UTC — Sunday 17:00 PDT."""
    observed = (
        datetime.datetime(2026, 9, 14, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 9, 21, tzinfo=datetime.timezone.utc),
        datetime.datetime(2026, 9, 28, tzinfo=datetime.timezone.utc),
    )
    for reset in observed:
        just_after = reset.timestamp() + 3600
        assert usage.muse_window_start(just_after) == reset.timestamp()
        just_before = reset.timestamp() - 3600
        assert usage.muse_window_start(just_before) == (
            reset.timestamp() - usage.SEVEN_DAY
        )


def test_muse_reader_uses_the_flat_cap_path(tmp_path, monkeypatch):
    """A rolling total uses the flat cap rather than the proportional line."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            NOW - 3600, input_tokens=19_000_000, output_tokens=1_000_000,
            usage_id="provider-1"
        )
    ])
    reading = usage.read_agent("muse", NOW)
    verdict = usage.pace(reading, NOW, provider="meta")
    assert verdict["windows"][0]["elapsed_fraction"] is None
    assert verdict["windows"][0]["allowed_percent"] == 100.0
    assert not verdict["over_pace"]
    over = dict(reading)
    over["windows"] = {
        "seven_day": dict(reading["windows"]["seven_day"]),
    }
    over["windows"]["seven_day"]["used_percent"] = 100.0
    assert usage.pace(over, NOW, provider="meta")["over_pace"]


def test_muse_reader_reports_spend_over_the_weekly_cap(tmp_path, monkeypatch):
    """The rolling reader leaves an over-cap percentage visible to the gate."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(
            NOW - 3600, input_tokens=161_600_000, usage_id="provider-1"
        )
    ])
    reading = usage.read_agent("muse", NOW)
    window = reading["windows"]["seven_day"]
    assert reading["spent_dollars"] == pytest.approx(202.0)
    assert window["used_percent"] == pytest.approx(101.0)
    assert usage.pace(reading, NOW, provider="meta")["over_pace"]


def _projected(tmp_path, monkeypatch, *, spent, trailing, days_left, at=NOW):
    """A Muse reading with `spent` dollars in the window, `trailing` dollars in
    the last 72 hours, read `days_left` days before the reset of the window
    holding `at`."""
    window_start = usage.muse_window_start(at)
    now = window_start + usage.SEVEN_DAY - days_left * 86400.0
    records = []
    older = spent - min(spent, trailing)
    in_window_recent = min(spent, trailing)
    if older:
        records.append(_muse_record(
            window_start + 60, input_tokens=int(older / 1.25 * 1_000_000),
            usage_id="older"))
    if in_window_recent:
        records.append(_muse_record(
            now - 3600, input_tokens=int(in_window_recent / 1.25 * 1_000_000),
            usage_id="recent"))
    before_window = trailing - in_window_recent
    if before_window:
        records.append(_muse_record(
            now - usage.MUSE_RATE_LOOKBACK + 3600,
            input_tokens=int(before_window / 1.25 * 1_000_000),
            usage_id="before-window"))
    _muse_fixture(tmp_path, monkeypatch, records, mtime=now)
    reading = usage.read_agent("muse", now)
    return reading, usage.pace(reading, now, provider="meta"), now


def test_a_burst_in_a_light_week_is_not_throttled(tmp_path, monkeypatch):
    """#1198 (a): $44 spent with six days left and $45 in the last 72 hours
    projects 67 percent. A proportional line would have refused this."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=44.0, trailing=45.0, days_left=6.0)
    window = reading["windows"]["seven_day"]
    assert window["trailing_72h_dollars"] == pytest.approx(45.0)
    assert window["daily_rate_dollars"] == pytest.approx(15.0)
    assert window["projected_percent"] == pytest.approx(67.0)
    assert verdict["band"] == "ok"
    assert not verdict["over_pace"]
    assert verdict["windows"][0]["runs_out_at"] is None


def test_a_rate_that_outruns_the_window_is_tight_not_a_stop(tmp_path, monkeypatch):
    """#1198 (b): the same spend at $31 a day projects 115 percent."""
    reading, verdict, now = _projected(
        tmp_path, monkeypatch, spent=44.0, trailing=93.0, days_left=6.0)
    assert reading["windows"]["seven_day"]["projected_percent"] == pytest.approx(115.0)
    assert verdict["band"] == "tight"
    assert not verdict["over_pace"]
    runs_out_at = verdict["windows"][0]["runs_out_at"]
    assert runs_out_at == pytest.approx(now + (200.0 - 44.0) / 31.0 * 86400.0, abs=1)


def test_tight_is_read_from_the_rate_not_from_an_even_line(tmp_path, monkeypatch):
    """#1198 (c): with two days left, 65 percent used is behind an even line
    (71 percent), and at $40 a day it still ends at 105. The ticket's own
    numbers for this case could not occur: once a window is older than 72
    hours its trailing spend cannot exceed what the window has spent."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=130.0, trailing=120.0, days_left=2.0)
    assert reading["windows"]["seven_day"]["used_percent"] == pytest.approx(65.0)
    assert reading["windows"]["seven_day"]["projected_percent"] == pytest.approx(105.0)
    assert verdict["band"] == "tight"
    assert not verdict["over_pace"]


def test_the_tickets_own_case_c_is_tight_at_110_percent():
    """#1198 (c), with the ticket's exact numbers: $100 spent of $200 with two
    days left and $180 in the trailing 72 hours is $60 a day, so the window
    projects 50 + 60 = 110 percent. `tight`, although 50 percent used is behind
    an even line at day five. Fed to `pace` as a reading, because no journal
    can hold these numbers: on day five the trailing 72 hours lie inside the
    window, so they cannot exceed what the window has spent. The journal-built
    test above covers the same claim with numbers that can occur."""
    days_left = 2.0
    reading = {"windows": {"seven_day": {
        "used_percent": 50.0,
        "resets_at": NOW + days_left * 86400.0,
        "window_start": NOW + days_left * 86400.0 - usage.SEVEN_DAY,
        "rolling": True,
        "spent_dollars": 100.0,
        "cap_dollars": 200.0,
        "trailing_72h_dollars": 180.0,
        "daily_rate_dollars": 60.0,
        "projected_percent": 50.0 + 100.0 * 60.0 * days_left / 200.0,
    }}}
    verdict = usage.pace(reading, NOW, provider="meta")
    weekly = verdict["windows"][0]
    assert weekly["projected_percent"] == pytest.approx(110.0)
    assert verdict["band"] == "tight"
    assert not verdict["over_pace"]
    even_line = 100.0 * (usage.SEVEN_DAY - days_left * 86400.0) / usage.SEVEN_DAY
    assert weekly["used_percent"] < even_line
    assert weekly["runs_out_at"] == pytest.approx(
        NOW + (200.0 - 100.0) / 60.0 * 86400.0, abs=1)


def test_the_flat_ceiling_still_stops_whatever_the_projection(tmp_path, monkeypatch):
    """#1198 (d): $196 plus the session reserve passes the cap."""
    _, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=196.0, trailing=0.0, days_left=1.0)
    assert verdict["band"] == "over"
    assert verdict["over_pace"]


# A literal, not read from the override: emptying the override to end it
# early should fail the tests below, not the collection of this module.
OVERRIDE_RESET = 1790553600.0  # 2026-09-28 00:00 UTC
IN_OVERRIDE = OVERRIDE_RESET - 3 * 86400.0


def test_the_override_names_the_window_resetting_sunday_2026_09_27():
    """#1341: Sunday 17:00 PDT is Monday 00:00 UTC, on the provider's lattice."""
    assert usage.MUSE_PACE_OVERRIDE["resets_at"] == OVERRIDE_RESET
    reset = datetime.datetime.fromtimestamp(
        OVERRIDE_RESET, datetime.timezone.utc)
    assert (reset.year, reset.month, reset.day, reset.hour) == (2026, 9, 28, 0)
    assert usage.muse_window_start(IN_OVERRIDE) + usage.SEVEN_DAY == OVERRIDE_RESET


def test_the_override_prices_the_window_from_the_panel(tmp_path, monkeypatch):
    """#1341: the panel read 70% while the meter held $89.38, so the same
    spend reads 70% here. At the implement-era rate the projection passes
    100%, and is reported rather than banded."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=89.38, trailing=89.38, days_left=4.8,
        at=IN_OVERRIDE)
    window = reading["windows"]["seven_day"]
    assert reading["cap_dollars"] == pytest.approx(127.69)
    assert window["cap_dollars"] == pytest.approx(127.69)
    assert window["used_percent"] == pytest.approx(70.0, abs=0.01)
    # 70 + 100 * $29.79/day * 4.8 days / $127.69: priced against the panel
    # cap, not the $200 one, which would read 141.5.
    assert window["projected_percent"] == pytest.approx(182.0, abs=0.05)
    assert window["override"] == {"issue": 1341, "until": OVERRIDE_RESET}

    weekly = verdict["windows"][0]
    assert verdict["band"] == "ok"
    assert not verdict["over_pace"]
    assert weekly["allowed_percent"] == 95.0
    assert weekly["reserve"] == pytest.approx(3.52)
    assert weekly["runs_out_at"] is not None
    assert weekly["override"]["issue"] == 1341


@pytest.mark.parametrize("spent, over", [(116.0, False), (117.0, True)])
def test_the_override_stops_at_95_less_one_session(tmp_path, monkeypatch,
                                                    spent, over):
    """Used plus $4.50 of $127.69 (3.52%) against 95: $116 reads 90.85% and
    is admitted, $117 reads 91.63% and is not."""
    _, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=spent, trailing=0.0, days_left=2.0,
        at=IN_OVERRIDE)
    assert verdict["over_pace"] is over
    assert verdict["band"] == ("over" if over else "ok")


def test_the_override_lapses_at_the_reset(tmp_path, monkeypatch):
    """The next window reads against $200, and `tight` is back."""
    assert usage.muse_pace_override(OVERRIDE_RESET, OVERRIDE_RESET) is None
    after = OVERRIDE_RESET + 3 * 86400.0
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=44.0, trailing=93.0, days_left=6.0,
        at=after)
    window = reading["windows"]["seven_day"]
    assert reading["cap_dollars"] == 200.0
    assert "policy" not in window and "override" not in window
    assert window["projected_percent"] == pytest.approx(115.0)
    assert verdict["band"] == "tight"
    assert verdict["windows"][0]["allowed_percent"] == 100.0
    assert "override" not in verdict["windows"][0]


def test_the_window_before_the_override_is_untouched(tmp_path, monkeypatch):
    before = OVERRIDE_RESET - usage.SEVEN_DAY - 86400.0
    assert usage.muse_pace_override(
        OVERRIDE_RESET - usage.SEVEN_DAY, before) is None
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=44.0, trailing=93.0, days_left=6.0,
        at=before)
    assert reading["cap_dollars"] == 200.0
    assert verdict["band"] == "tight"


def test_begin_runs_on_the_reading_that_stopped_it_tight(tmp_path, monkeypatch):
    """#1341 through `begin`'s own preflight. The 21:32 PDT reading on
    2026-09-22 — $89.38 in the window, all of it in the last 72 hours, 4.8
    days left — stopped every lane as `tight` at 116% projected. Under the
    override it passes; the same reading a window later stops again."""
    import funnel

    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent, tier=None: "run-id")

    def preflight(root, at):
        _, _, now = _projected(root, monkeypatch, spent=89.38,
                               trailing=89.38, days_left=4.8, at=at)
        return funnel._begin_preflight(
            datetime.datetime.fromtimestamp(now, datetime.timezone.utc),
            "muse", False)

    out, reading = preflight(tmp_path / "override", IN_OVERRIDE)
    assert out["gate"] == "ok" and out["budget_band"] == "ok"
    assert reading is not None

    out, reading = preflight(tmp_path / "after", OVERRIDE_RESET + 3 * 86400.0)
    assert out["gate"] == "tight" and out["do"] == "stop"
    assert out["budget"]["projected_percent"] == pytest.approx(116.2, abs=0.1)
    assert reading is None


def test_a_window_policy_is_read_before_the_providers():
    """`pace` takes a ceiling or reserve the window carries; without one the
    provider's policy stands, as it does for every other reader."""
    window = {"used_percent": 60.0, "resets_at": NOW + 86400.0,
              "rolling": True}
    carried = dict(window, policy={"weekly_target": 62.0,
                                   "weekly_reserve": 3.0})
    assert usage.pace({"windows": {"seven_day": carried}}, NOW,
                      provider="meta")["over_pace"]
    plain = usage.pace({"windows": {"seven_day": window}}, NOW,
                       provider="meta")
    assert not plain["over_pace"]
    assert plain["windows"][0]["allowed_percent"] == 100.0
    assert plain["windows"][0]["reserve"] == pytest.approx(2.25)


def test_no_recent_spend_is_ok_with_no_run_out_time(tmp_path, monkeypatch):
    """#1198 (e)."""
    reading, verdict, _ = _projected(
        tmp_path, monkeypatch, spent=50.0, trailing=0.0, days_left=3.0)
    assert reading["windows"]["seven_day"]["daily_rate_dollars"] == 0.0
    assert verdict["band"] == "ok"
    assert verdict["windows"][0]["runs_out_at"] is None


def test_a_window_with_no_spend_yet_is_a_reading_of_zero(tmp_path, monkeypatch):
    """Every window opens with no calls in it, and after a wall the last 72
    hours are empty too. That is a budget of zero used, not an unreadable one:
    read as unknown it would stop every lane at the reset, with nothing left
    to make the first call."""
    window_start = usage.muse_window_start(NOW)
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(window_start - 5 * 86400.0, input_tokens=1_000_000,
                     usage_id="long-ago"),
    ])
    just_after_reset = window_start + 60
    reading = usage.read_agent("muse", just_after_reset)
    assert reading is not None
    assert reading["spent_dollars"] == 0.0
    verdict = usage.pace(reading, just_after_reset, provider="meta")
    assert verdict["known"] and not verdict["over_pace"]
    assert verdict["band"] == "ok"


def test_no_journal_at_all_still_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        usage, "MUSE_SESSIONS", str(tmp_path / "*/*/*/*/session.jsonl"))
    assert usage.read_agent("muse", NOW) is None


def test_other_providers_carry_no_band():
    """#1198 (f): a reading without a projection behaves exactly as before."""
    reading = {"windows": {"seven_day": {
        "used_percent": 10.0, "resets_at": NOW + 3 * 86400.0}}}
    verdict = usage.pace(reading, NOW, provider="anthropic")
    assert verdict["band"] is None
    assert "band" not in verdict["windows"][0]
    assert "projected_percent" not in verdict["windows"][0]


def test_muse_is_metered_with_a_measured_session_reserve():
    """Muse uses the measured worst-case session reserve under the cap."""
    assert "meta" not in usage.UNMETERED_PROVIDERS
    assert "anthropic" not in usage.UNMETERED_PROVIDERS
    assert "openai" not in usage.UNMETERED_PROVIDERS
    assert "zai" not in usage.UNMETERED_PROVIDERS
    assert usage.PROVIDER_POLICY["meta"]["weekly_target"] == 100.0
    assert usage.PROVIDER_POLICY["meta"]["weekly_reserve"] == pytest.approx(2.25)


def test_muse_model_detection_reads_snapshots_not_jsonl():
    """Every other harness writes JSONL; Muse writes whole-file JSON snapshots,
    and its `HEAD.json` carries no model at all. A glob that picks up HEAD
    reports no model and the routing dataset silently loses its most expensive
    lane."""
    source = heartbeat.MODEL_SOURCES["muse"]
    assert source.endswith("snapshot-*.json"), source
    assert "HEAD" not in source


# --- the per-model breakdown, and the card the gate keeps using (#1302) ---

CONTRIBUTOR = "muse-spark-1.3-contributor"
STANDARD = "muse-spark-1.3"


def _mixed_window(tmp_path, monkeypatch):
    """One window holding a standard run and a contributor run."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_model_record(NOW - 100, "run-std", STANDARD),
        _muse_model_record(NOW - 90, "run-con", CONTRIBUTOR),
        _muse_record(NOW - 80, input_tokens=1_000_000,
                     cached_tokens=800_000, output_tokens=100_000,
                     usage_id="u1", run_id="run-std"),
        _muse_record(NOW - 70, input_tokens=1_000_000,
                     cached_tokens=800_000, output_tokens=100_000,
                     usage_id="u2", run_id="run-con"),
    ])
    return usage.read_muse(NOW)


def test_the_gated_total_is_the_standard_card_whatever_model_ran(
        tmp_path, monkeypatch):
    """The $200 ceiling was calibrated in a window where every session was
    on the standard model. Re-pricing contributor calls cheaply assumes a
    price-shaped window, which nothing has established; if it is
    token-shaped the brake never trips and the lanes walk into a refusal.
    Same card means the brake does not move."""
    reading = _mixed_window(tmp_path, monkeypatch)

    card = usage.muse_model.RATE_CARDS[STANDARD]
    one_call = ((200_000 * card["input"]
                 + 800_000 * card["cached_input"]
                 + 100_000 * card["output"]) / 1_000_000)
    assert reading["spent_dollars"] == pytest.approx(2 * one_call)
    assert reading["windows"]["seven_day"]["spent_dollars"] == \
        pytest.approx(2 * one_call)


def test_the_breakdown_counts_calls_and_dollars_per_model(
        tmp_path, monkeypatch):
    reading = _mixed_window(tmp_path, monkeypatch)

    assert set(reading["by_model"]) == {STANDARD, CONTRIBUTOR}
    assert reading["by_model"][STANDARD]["calls"] == 1
    assert reading["by_model"][CONTRIBUTOR]["calls"] == 1
    assert all(row["has_rate_card"] for row in reading["by_model"].values())


def test_each_model_is_priced_both_ways(tmp_path, monkeypatch):
    """The difference between the two is the size of the open question."""
    reading = _mixed_window(tmp_path, monkeypatch)

    standard_row = reading["by_model"][STANDARD]
    contributor_row = reading["by_model"][CONTRIBUTOR]

    # A standard call costs the same either way; there is one card for it.
    assert standard_row["dollars_at_standard"] == \
        pytest.approx(standard_row["dollars_at_own_card"])
    # A contributor call does not, and by a lot: this workload is
    # cache-heavy, and the cards discount a cache read to 12% and 2%.
    assert contributor_row["dollars_at_own_card"] < \
        contributor_row["dollars_at_standard"] / 10
    assert reading["own_card_dollars"] == pytest.approx(
        standard_row["dollars_at_own_card"]
        + contributor_row["dollars_at_own_card"])
    assert reading["own_card_dollars"] < reading["spent_dollars"]


def test_an_unrecognised_model_prices_at_standard_and_is_named(
        tmp_path, monkeypatch):
    """A provider version bump would otherwise be mispriced in silence."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_model_record(NOW - 100, "run-next", "muse-spark-1.4"),
        _muse_record(NOW - 80, input_tokens=1_000_000,
                     cached_tokens=800_000, output_tokens=100_000,
                     usage_id="u1", run_id="run-next"),
    ])
    reading = usage.read_muse(NOW)

    row = reading["by_model"]["muse-spark-1.4"]
    assert row["has_rate_card"] is False
    assert row["dollars_at_own_card"] == pytest.approx(
        row["dollars_at_standard"])
    assert reading["models_without_a_card"] == ["muse-spark-1.4"]
    assert reading["spent_dollars"] == pytest.approx(
        row["dollars_at_standard"])


def test_a_record_with_no_model_event_is_named_not_dropped(
        tmp_path, monkeypatch):
    """A window full of these means the join has broken. The dollars
    still count; what must not happen is the records vanishing from the
    breakdown while the total stays right."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(NOW - 80, input_tokens=1_000, output_tokens=100,
                     usage_id="u1", run_id="run-orphan"),
    ])
    reading = usage.read_muse(NOW)

    assert list(reading["by_model"]) == [usage.MUSE_MODEL_UNKNOWN]
    assert reading["by_model"][usage.MUSE_MODEL_UNKNOWN]["calls"] == 1
    assert reading["spent_dollars"] > 0


def test_one_model_in_a_journal_covers_a_record_that_did_not_join(
        tmp_path, monkeypatch):
    """The run id is the join, but a session that configured exactly one
    model ran on that model whatever the id says."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_model_record(NOW - 100, "run-a", CONTRIBUTOR),
        _muse_record(NOW - 80, input_tokens=1_000, output_tokens=100,
                     usage_id="u1", run_id="some-other-run"),
    ])
    reading = usage.read_muse(NOW)

    assert list(reading["by_model"]) == [CONTRIBUTOR]


def test_the_ceiling_and_the_reserve_are_untouched():
    """This ticket corrects what is counted, not what it is counted
    against. Moving either would change the brake, which is the thing it
    deliberately leaves alone."""
    assert usage.MUSE_WEEKLY_CAP_DOLLARS == 200.0
    assert usage.MUSE_SESSION_RESERVE_DOLLARS == 4.50


def test_the_rate_names_still_read_the_standard_card():
    """The dashboard and the brief import these.

    The literals are spelled out rather than derived from
    `muse_model.RATE_CARDS`: comparing the implementation to itself
    cannot fail, and would not notice the standard card moving. These
    three numbers are the ones the $200 ceiling was calibrated against.
    """
    assert usage.MUSE_INPUT_RATE == 1.25 / 1_000_000
    assert usage.MUSE_CACHED_INPUT_RATE == 0.15 / 1_000_000
    assert usage.MUSE_OUTPUT_RATE == 4.25 / 1_000_000


def test_the_breakdown_sums_to_the_gated_total(tmp_path, monkeypatch):
    """The invariant that catches a record counted in one place and not
    the other — which is how an under-read would look from outside."""
    reading = _mixed_window(tmp_path, monkeypatch)

    assert sum(row["dollars_at_standard"]
               for row in reading["by_model"].values()) == \
        pytest.approx(reading["spent_dollars"])
    assert sum(row["calls"] for row in reading["by_model"].values()) == \
        reading["windows"]["seven_day"]["calls"]


def test_a_usage_line_mentioning_the_model_event_is_still_counted(
        tmp_path, monkeypatch):
    """`usage.py` contains the string `run.model.configured`, so a Muse
    session that reads this file puts it in its own journal. A usage
    record on such a line must not be filtered out as a model event:
    dropping it under-reads the window, which is the direction that ends
    at the provider's refusal."""
    record = _muse_record(NOW - 80, input_tokens=1_000, output_tokens=100,
                          usage_id="u1", run_id="run-a")
    record["payload"]["event"]["record"]["note"] = "run.model.configured"
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_model_record(NOW - 100, "run-a", STANDARD),
        record,
    ])
    reading = usage.read_muse(NOW)

    assert reading["windows"]["seven_day"]["calls"] == 1
    assert reading["spent_dollars"] > 0
    assert reading["by_model"][STANDARD]["calls"] == 1


def test_the_join_break_sentinel_is_not_reported_as_a_missing_card(
        tmp_path, monkeypatch):
    """Two different alarms. #1304 reads `models_without_a_card` for a
    provider version bump; `by_model` already announces a broken join."""
    _muse_fixture(tmp_path, monkeypatch, [
        _muse_record(NOW - 80, input_tokens=1_000, output_tokens=100,
                     usage_id="u1", run_id="run-orphan"),
    ])
    reading = usage.read_muse(NOW)

    assert usage.MUSE_MODEL_UNKNOWN in reading["by_model"]
    assert reading["models_without_a_card"] == []
