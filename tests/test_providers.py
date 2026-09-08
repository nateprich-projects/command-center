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
    """65% of a pool allowed to 83.5% is fine alone, and not fine when the
    reviewer still has to run on the same credits: 83.5 - 20 = 63.5."""
    windows = usage.pace(seven_day(60.0, 0.9), NOW)["windows"]
    expected = round(
        usage.WEEKLY_FLOOR
        + (usage.WEEKLY_TARGET - usage.WEEKLY_FLOOR) * 0.9,
        1,
    )
    assert windows[0]["allowed_percent"] == expected
    assert 60.0 + windows[0]["reserve"] > expected - usage.DOWNSTREAM_RESERVE


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
    floor = usage.PROVIDER_POLICY["zai"]["weekly_floor"]
    target = usage.PROVIDER_POLICY["zai"].get("weekly_target", usage.WEEKLY_TARGET)
    allowed = [
        usage.pace(seven_day(10.0, fraction), NOW, provider="zai")["windows"][0]
        for fraction in (0.0, 0.05, 0.5, 1.0)
    ]
    assert allowed[0]["allowed_percent"] == floor
    assert allowed[-1]["allowed_percent"] == target
    assert all(
        left["allowed_percent"] < right["allowed_percent"]
        for left, right in zip(allowed, allowed[1:])
    )
    assert allowed[1]["allowed_percent"] == round(
        floor + (target - floor) * 0.05, 1
    )
    assert allowed[1]["allowed_percent"] != usage.WEEKLY_FLOOR  # not the shared line
    assert allowed[1]["reserve"] == 0.5                         # its own reserve, not 5
    assert allowed[1]["reserve"] != usage.WEEKLY_RESERVE


def test_the_dedicated_pool_is_paced_rather_than_flat():
    """The dedicated pool rises from its own floor rather than staying flat
    until the target line catches up."""
    day_one = usage.pace(seven_day(40.0, 0.05), NOW, provider="zai")
    assert day_one["over_pace"]                      # 40% on Monday is ahead

    late = usage.pace(seven_day(40.0, 0.9), NOW, provider="zai")
    assert not late["over_pace"]                     # the same 40% is fine by Sunday


def test_the_dedicated_pool_reserve_is_sized_to_its_own_runs():
    """The z.ai pool reserves 0.5%, while the shared default reserves 5%."""
    window = usage.pace(seven_day(54.0, 0.5), NOW, provider="zai")["windows"][0]
    assert window["reserve"] == 0.5
    assert not window["over"]                        # 54.5 < 56 allowed
    # The shared default refuses the same reading because its reserve is 5.
    assert usage.pace(seven_day(54.0, 0.5), NOW)["windows"][0]["over"]


def test_an_unknown_provider_gets_the_shared_defaults():
    a = usage.pace(seven_day(40.0, 0.02), NOW)["windows"][0]
    b = usage.pace(seven_day(40.0, 0.02), NOW, provider="nosuchpool")["windows"][0]
    expected = round(
        usage.WEEKLY_FLOOR
        + (usage.WEEKLY_TARGET - usage.WEEKLY_FLOOR) * 0.02,
        1,
    )
    assert a["allowed_percent"] == b["allowed_percent"] == expected
    assert expected > usage.WEEKLY_FLOOR


  # shared: 94 > 45
