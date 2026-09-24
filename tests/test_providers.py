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

import pytest

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
    """The quota endpoint's answer. Named for the curl call it replaced."""
    monkeypatch.setattr(usage, "_zai_quota_payload", lambda key: payload)
    monkeypatch.setattr(usage, "_zai_key", lambda: "k")


class _QuotaServer:
    """A local quota endpoint that records the headers it was sent."""

    def __init__(self, status=200, body=None, location=None):
        import http.server
        import threading

        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append({k.lower(): v for k, v in self.headers.items()})
                raw = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/api/monitor/usage/quota/limit".format(
            self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_zai_quota_is_read_from_the_api(monkeypatch):
    fake_curl(monkeypatch, zai_payload(200, 500, NOW))
    r = usage.read_zai(NOW)
    assert r["source"] == "zai"
    assert r["windows"]["five_hour"]["used_percent"] == 10.0   # 200 / 2000
    assert r["windows"]["seven_day"]["used_percent"] == 5.0    # 500 / 10000


def test_the_zai_key_travels_in_process_never_on_a_command_line(monkeypatch):
    """#1411: `curl -H "Authorization: <key>"` put the key where any local
    account could read it through `ps`. The request is now made in process,
    so no subprocess is started for it at all."""
    monkeypatch.setattr(usage, "_zai_key", lambda: "the-secret-key")
    monkeypatch.setattr(usage.subprocess, "run", lambda *a, **k: pytest.fail(
        "the quota read must not start a process: {}".format(a)))
    with _QuotaServer(body=zai_payload(200, 500, NOW)) as server:
        monkeypatch.setattr(usage, "ZAI_QUOTA_URL", server.url)
        reading = usage.read_zai(NOW)
    assert reading["windows"]["five_hour"]["used_percent"] == 10.0
    assert server.seen[0]["authorization"] == "the-secret-key"


@pytest.mark.parametrize("status", (401, 500))
def test_an_http_error_from_the_quota_endpoint_fails_closed(monkeypatch,
                                                            status):
    monkeypatch.setattr(usage, "_zai_key", lambda: "k")
    with _QuotaServer(status=status, body={"success": False}) as server:
        monkeypatch.setattr(usage, "ZAI_QUOTA_URL", server.url)
        assert usage.read_zai(NOW) is None


def test_the_quota_read_never_follows_a_redirect_with_the_key(monkeypatch):
    monkeypatch.setattr(usage, "_zai_key", lambda: "k")
    with _QuotaServer(body=zai_payload(0, 0, NOW)) as elsewhere:
        with _QuotaServer(status=302, location=elsewhere.url) as server:
            monkeypatch.setattr(usage, "ZAI_QUOTA_URL", server.url)
            assert usage.read_zai(NOW) is None
    assert elsewhere.seen == []


def test_zai_without_a_key_fails_closed(monkeypatch):
    monkeypatch.setattr(usage, "_zai_key", lambda: None)
    assert usage.read_zai(NOW) is None


def test_zai_rejects_an_unsuccessful_payload(monkeypatch):
    fake_curl(monkeypatch, {"success": False, "msg": "nope"})
    assert usage.read_zai(NOW) is None


def test_zcode_spends_the_zai_pool():
    assert usage.provider_of("zcode") == "zai"


@pytest.mark.parametrize(("five_spent", "week_spent", "gate"), (
    (0, 0, "ok"),
    (1500, 9000, "ok"),
    (2000, 0, "over"),
    (0, 10000, "over"),
))
def test_begin_for_zcode_gates_on_zais_own_windows(monkeypatch, five_spent,
                                                   week_spent, gate):
    """The engine's z.ai lane opens with `begin --agent zcode`. Its preflight
    must read the z.ai quota endpoint — not Muse's journal, not Claude's
    cache — and stop before any Project read once a window is spent."""
    from datetime import datetime, timezone

    import funnel

    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent, tier=None: "run-id")
    fake_curl(monkeypatch, zai_payload(five_spent, week_spent, NOW))
    monkeypatch.setattr(usage, "read_muse", lambda now: pytest.fail(
        "zcode must not read Muse's meter"))

    out, reading = funnel._begin_preflight(
        datetime.fromtimestamp(NOW, timezone.utc), "zcode", False, "standard")

    assert out["gate"] == gate
    if gate == "ok":
        assert reading["source"] == "zai"
        assert "do" not in out
    else:
        assert out["do"] == "stop"
        assert reading is None


def test_begin_for_zcode_fails_closed_without_a_zai_reading(monkeypatch):
    from datetime import datetime, timezone

    import funnel

    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent, tier=None: "run-id")
    monkeypatch.setattr(usage, "_zai_key", lambda: None)

    out, reading = funnel._begin_preflight(
        datetime.fromtimestamp(NOW, timezone.utc), "zcode", False, "standard")

    assert out["gate"] == "unknown" and out["do"] == "stop"
    assert reading is None


# -- pacing policy is per provider, not global --------------------------------

def test_the_lapsing_zai_pool_is_spendable_all_week_not_paced():
    """The cancelled plan's credits lapse when it expires on 2026-10-07
    (Nate, 2026-09-23), so no line holds any of the week back: the allowance
    is 100% from the first hour to the last, on z.ai's own policy rather than
    the shared default."""
    allowed = [
        usage.pace(seven_day(10.0, fraction), NOW, provider="zai")["windows"][0]
        for fraction in (0.0, 0.05, 0.5, 1.0)
    ]
    assert [window["allowed_percent"] for window in allowed] == [100.0] * 4
    assert allowed[1]["allowed_percent"] != usage.WEEKLY_FLOOR  # not the shared line
    assert allowed[1]["reserve"] == 0.5                         # its own reserve, not 5
    assert allowed[1]["reserve"] != usage.WEEKLY_RESERVE
    # 90% on Monday is not "ahead": nothing later is worth saving it for.
    assert not usage.pace(seven_day(90.0, 0.05), NOW, provider="zai")["over_pace"]


@pytest.mark.parametrize(("five_spent", "week_spent", "over"), (
    (0, 0, False),
    (1900, 5000, False),       # 95% + 2.5 reserve = 97.5, under 100
    (1960, 5000, True),        # 98% + 2.5 reserve: one run would not finish
    (2000, 5000, True),        # the five-hour window is spent
    (0, 9940, False),          # 99.4% + 0.5 reserve = 99.9, under 100
    (0, 9960, True),           # 99.6% + 0.5 reserve: one run would not finish
    (0, 10000, True),          # the week is spent
))
def test_a_spent_zai_window_still_stops_begin(monkeypatch, five_spent,
                                              week_spent, over):
    """Unpaced is not unbounded: either window z.ai reports stops the lane
    once one more run could not finish inside it."""
    fake_curl(monkeypatch, zai_payload(five_spent, week_spent, NOW))
    verdict = usage.pace(usage.read_zai(NOW), NOW,
                         provider=usage.provider_of("zcode"))
    assert verdict["known"]
    week = next(w for w in verdict["windows"] if w["window"] == "seven_day")
    five = next(w for w in verdict["windows"] if w["window"] == "five_hour")
    assert five["allowed_percent"] == 100.0 and five["reserve"] == 2.5
    assert week["allowed_percent"] == 100.0 and week["reserve"] == 0.5
    assert verdict["over_pace"] is over


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


# -- shaping on the lapsing z.ai pool (#1411) ----------------------------------

def _zai_reading(five_spent, week_spent):
    now = usage.time.time()
    return {"source": "zai", "captured_at": now, "windows": {
        "five_hour": {"used_percent": 100.0 * five_spent / 2000,
                      "resets_at": now + 3 * 3600},
        "seven_day": {"used_percent": 100.0 * week_spent / 10000,
                      "resets_at": now + 5 * 86400},
    }}


def test_zai_shapes_past_the_idle_boundary_while_the_lane_runs(monkeypatch):
    """The 15% five-hour boundary keeps shaping off a window someone may be
    working in. Nobody else spends the cancelled z.ai plan, and what it does
    not spend lapses, so it shapes up to its own stop."""
    import heartbeat

    monkeypatch.setattr(heartbeat, "ZAI_STANDARD_UNTIL",
                        usage.time.time() + 86400)
    assert usage.shaping_allowed(_zai_reading(1200, 4000))       # 60% used
    assert not usage.shaping_allowed(_zai_reading(1980, 4000))   # spent
    assert not usage.shaping_allowed(_zai_reading(0, 10000))     # week spent
    # Other pools keep the idle boundary.
    other = dict(_zai_reading(1200, 4000), source="openai")
    assert not usage.shaping_allowed(other)


def test_zai_shaping_returns_to_the_idle_boundary_after_the_cutoff(
        monkeypatch):
    import heartbeat

    monkeypatch.setattr(heartbeat, "ZAI_STANDARD_UNTIL",
                        usage.time.time() - 1)
    assert not usage.shaping_allowed(_zai_reading(1200, 4000))
    assert usage.shaping_allowed(_zai_reading(200, 4000))        # 10% used
