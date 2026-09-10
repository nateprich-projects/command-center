"""The idle rule: Codex works when Nate is away, and never competes with him.

The pace gate answers "is there budget left" and permits Codex up to 80% of the
five-hour window. That is fine overnight and wrong during the day. This rule is a
presence test instead — five-hour utilisation as a proxy for whether Nate is at
the keyboard.

The reading is account-wide, so it cannot separate his usage from the agent's. A
literal "must read zero every run" test would self-block after the first run, so
the test is on *entry*: a window that read zero when a run started is one he was
absent for, and later runs may continue in it up to a low ceiling.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import usage  # noqa: E402


@pytest.fixture(autouse=True)
def _rule_active(monkeypatch):
    """Exercise the rule even while it is suspended in production.

    `IDLE_RULE_SUSPENDED` was set on 2026-09-07 to force a backlog through and
    made permanent on 2026-09-09, when the Luna lane it guarded measured at
    ~0.1% of the week per run (see `usage.py` beside the switch). It makes
    `idle_verdict` return `None` for everything. Left alone, every test below
    would pass by doing nothing — the rule would be untested for as long as it
    is off, which is exactly when it is most likely to be changed by someone
    who cannot see it working.
    """
    monkeypatch.setattr(usage, "IDLE_RULE_SUSPENDED", False)


def test_the_suspension_suppresses_the_rule_entirely(monkeypatch):
    """The switch has to work regardless of what a run asks for. It lives in
    `usage.py` rather than in the automation prompt because the Codex app
    rewrites that prompt from its own copy — it did so at 18:14:50 on
    2026-09-07, three minutes after the flag was removed there, and the next run
    refused on the proxy again. A switch something else can overwrite is not a
    switch."""
    monkeypatch.setattr(usage, "IDLE_RULE_SUSPENDED", True)
    reading = {"windows": {"five_hour": {"used_percent": 99.0,
                                         "resets_at": 2_000_000_000}}}
    assert usage.idle_verdict("codex", reading) is None

RESETS = 1_788_700_000.0


def reading(used, resets_at=RESETS):
    return {"source": "codex", "windows": {"five_hour": {
        "used_percent": used, "resets_at": resets_at}}}


def start(used, resets_at=RESETS):
    return {"phase": "start", "agent": "codex",
            "usage": {"five_hour": {"used_percent": used, "resets_at": resets_at}}}


def records(monkeypatch, rows):
    import heartbeat
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)


def test_untouched_window_opens(monkeypatch):
    records(monkeypatch, [])
    v = usage.idle_verdict("codex", reading(0.0))
    assert v["over"] is False and v["opened"] is True


def test_window_already_in_use_refuses(monkeypatch):
    """Nate is at the keyboard. This is the case the rule exists for."""
    records(monkeypatch, [])
    v = usage.idle_verdict("codex", reading(1.0))
    assert v["over"] is True and v["opened"] is False
    assert "Nate is working" in v["why"]


def test_a_window_opened_idle_may_continue(monkeypatch):
    """Otherwise the first run would block every later one, since the agent's own
    spend counts against the same account-wide number."""
    records(monkeypatch, [start(0.0)])
    v = usage.idle_verdict("codex", reading(8.0))
    assert v["over"] is False


def test_continuing_stops_at_the_ceiling(monkeypatch):
    records(monkeypatch, [start(0.0)])
    v = usage.idle_verdict("codex", reading(usage.IDLE_WINDOW_CEILING + 0.1))
    assert v["over"] is True
    assert "over" in v["why"]


def test_a_previous_window_does_not_open_this_one(monkeypatch):
    """The idle start must be in *this* window, identified by resets_at."""
    records(monkeypatch, [start(0.0, resets_at=RESETS - 6 * 3600)])
    v = usage.idle_verdict("codex", reading(4.0))
    assert v["over"] is True and v["opened"] is False


def test_reset_times_may_differ_slightly(monkeypatch):
    records(monkeypatch, [start(0.0, resets_at=RESETS + 30)])
    assert usage.idle_verdict("codex", reading(4.0))["over"] is False


def test_a_non_idle_start_does_not_open_the_window(monkeypatch):
    records(monkeypatch, [start(3.0)])
    assert usage.idle_verdict("codex", reading(6.0))["over"] is True


def test_legacy_records_are_ignored_not_guessed(monkeypatch):
    """Older records stored a bare percentage with no reset time."""
    records(monkeypatch, [{"phase": "start", "agent": "codex",
                           "usage": {"five_hour": 0.0}}])
    assert usage.idle_verdict("codex", reading(4.0))["over"] is True


def test_unreadable_records_refuse_rather_than_assume(monkeypatch):
    import heartbeat

    def boom(agent):
        raise RuntimeError("github down")

    monkeypatch.setattr(heartbeat, "read", boom)
    v = usage.idle_verdict("codex", reading(4.0))
    assert v["over"] is True and v["opened"] is None


def test_the_rule_does_not_apply_to_claude():
    """A scheduled Claude run writes no statusline, so its reading is a token
    estimate that cannot say 'you spent none of this'."""
    assert usage.idle_verdict("claude", reading(0.0)) is None


def test_no_five_hour_reading_means_the_rule_is_silent():
    assert usage.idle_verdict("codex", {"windows": {}}) is None
