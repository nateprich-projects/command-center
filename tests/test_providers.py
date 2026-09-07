"""A budget belongs to a provider, not to an agent.

Today the two look identical — Claude has Anthropic to itself, Codex has OpenAI
— which is exactly why the distinction is easy to lose. It stops being true the
moment one subscription serves two harnesses. A z.ai Coding Plan would do that:
GLM implementing in Codex and GLM reviewing in Claude Code draw the same 5-hour
and weekly credits.

The failure that creates is specific. An implementation run that spends the last
of a shared pool leaves its own PR unreviewable, and an unreviewed PR is worse
than an unstarted ticket — the quota is gone and nothing shipped.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import usage  # noqa: E402

NOW = 1_788_600_000.0
WEEK = 7 * 86400


def seven_day(used, elapsed):
    return {"source": "test", "captured_at": NOW, "windows": {"seven_day": {
        "used_percent": used, "resets_at": NOW + WEEK * (1 - elapsed)}}}


def test_each_agent_names_the_pool_it_spends():
    assert usage.provider_of("claude") == "anthropic"
    assert usage.provider_of("codex") == "openai"


def test_an_unregistered_agent_fails_closed():
    """A missing pool is a configuration error, not an empty budget."""
    assert usage.read_agent("nobody", NOW) is None


def test_a_registered_provider_with_no_reader_fails_closed(monkeypatch):
    """The failure a provider registry invites: a budget nobody can see waving
    work through. z.ai was this case until it got a reader on 2026-09-06; the
    next provider added will be, until someone writes one."""
    monkeypatch.setitem(usage.PROVIDERS, "someagent", "notyetimplemented")
    assert usage.read_agent("someagent", NOW) is None


def test_today_neither_agent_shares_a_pool():
    assert not usage.shares_a_pool("claude")
    assert not usage.shares_a_pool("codex")


def test_two_agents_on_one_provider_share_it(monkeypatch):
    monkeypatch.setitem(usage.PROVIDERS, "one", "sharedpool")
    monkeypatch.setitem(usage.PROVIDERS, "two", "sharedpool")
    assert usage.shares_a_pool("one")
    assert usage.agents_on("sharedpool") == ["one", "two"]


def test_the_downstream_reserve_only_bites_on_a_shared_pool(monkeypatch):
    """With a pool to itself there is no following stage on the same credits."""
    reading = seven_day(60.0, 0.9)          # allowed = 81%, 60 + 5 = 65 — fine
    assert not usage.pace(reading, NOW)["over_pace"]

    monkeypatch.setitem(usage.PROVIDERS, "solo", "lonely")
    assert not usage.shares_a_pool("solo")


def test_review_headroom_is_held_back_when_the_pool_is_shared():
    """65% of a pool allowed to 81% is fine alone, and not fine when the reviewer
    still has to run on the same credits: 81 - 20 = 61."""
    windows = usage.pace(seven_day(60.0, 0.9), NOW)["windows"]
    assert windows[0]["allowed_percent"] == 81.0
    assert 60.0 + windows[0]["reserve"] > 81.0 - usage.DOWNSTREAM_RESERVE


# -- z.ai: read, never computed ----------------------------------------------

def zai_payload(five_spent, week_spent, now):
    return {"success": True, "data": {"level": "lite", "limits": [
        {"type": "CREDIT_LIMIT", "usage": 2000, "currentValue": five_spent,
         "nextResetTime": (now + 3 * 3600) * 1000},
        {"type": "CREDIT_LIMIT", "usage": 10000, "currentValue": week_spent,
         "nextResetTime": (now + 5 * 86400) * 1000},
    ]}}


def fake_curl(monkeypatch, payload):
    class Out:
        stdout = json.dumps(payload)
    monkeypatch.setattr(usage.subprocess, "run", lambda *a, **k: Out())
    monkeypatch.setattr(usage, "_zai_key", lambda: "k")


def test_zai_quota_is_read_from_the_api(monkeypatch):
    fake_curl(monkeypatch, zai_payload(200, 500, NOW))
    r = usage.read_zai(NOW)
    assert r["source"] == "zai"
    assert r["windows"]["five_hour"]["used_percent"] == 10.0   # 200 / 2000
    assert r["windows"]["seven_day"]["used_percent"] == 5.0    # 500 / 10000


def test_zai_without_a_key_fails_closed(monkeypatch):
    monkeypatch.setattr(usage, "_zai_key", lambda: None)
    assert usage.read_zai(NOW) is None


def test_zai_rejects_an_unsuccessful_payload(monkeypatch):
    fake_curl(monkeypatch, {"success": False, "msg": "nope"})
    assert usage.read_zai(NOW) is None


def test_zcode_spends_the_zai_pool():
    assert usage.provider_of("zcode") == "zai"


# -- pacing policy is per provider, not global --------------------------------

def test_the_dedicated_pool_has_its_own_policy_not_the_shared_default():
    """`WEEKLY_FLOOR` exists to leave Nate room on a subscription he also works
    on. A pool bought for the automations needs no such protection — what it
    needs is pacing, so a week's credits are not spendable on Monday."""
    early = usage.pace(seven_day(10.0, 0.05), NOW, provider="zai")["windows"][0]
    # Asserted against the policy rather than a literal: the property under test
    # is that this pool reads *its own* entry, not that the entry holds any
    # particular number. Pinning the literal made this fail on 2026-09-07 when
    # Nate deliberately raised the floor to 22 to unstick zcode — a config
    # decision, not a regression. `test_the_dedicated_pool_is_paced_rather_than_
    # flat` below still pins the behaviour that actually matters.
    assert early["allowed_percent"] == usage.PROVIDER_POLICY["zai"]["weekly_floor"]
    assert early["allowed_percent"] != usage.WEEKLY_FLOOR    # not the shared 25
    assert early["reserve"] == 0.5                           # its own reserve, not 5
    assert early["reserve"] != usage.WEEKLY_RESERVE          # not the shared 5


def test_the_dedicated_pool_is_paced_rather_than_flat():
    """At 90 the floor equalled the target, the rising line never applied, and
    the whole week was spendable on day one. At 15 the line governs from about
    day one onward."""
    day_one = usage.pace(seven_day(40.0, 0.05), NOW, provider="zai")
    assert day_one["over_pace"]                      # 40% on Monday is ahead

    late = usage.pace(seven_day(40.0, 0.9), NOW, provider="zai")
    assert not late["over_pace"]                     # the same 40% is fine by Sunday


def test_the_dedicated_pool_reserve_is_sized_to_its_own_runs():
    """5% is calibrated for Anthropic, where a run is ~1.5% of the window. A
    measured z.ai run costs ~43 credits of 10,000 — 0.43% — so the shared
    reserve would hold back 500 credits against a run costing forty."""
    window = usage.pace(seven_day(44.0, 0.5), NOW, provider="zai")["windows"][0]
    assert window["reserve"] == 0.5
    assert not window["over"]                        # 44.5 < 45 allowed
    # The shared default would have refused the same reading.
    assert usage.pace(seven_day(44.0, 0.5), NOW)["windows"][0]["over"]


def test_an_unknown_provider_gets_the_shared_defaults():
    a = usage.pace(seven_day(40.0, 0.02), NOW)["windows"][0]
    b = usage.pace(seven_day(40.0, 0.02), NOW, provider="nosuchpool")["windows"][0]
    assert a["allowed_percent"] == b["allowed_percent"] == usage.WEEKLY_FLOOR


  # shared: 94 > 45
