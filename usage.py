#!/usr/bin/env python3
"""usage.py — read subscription usage for either agent, and gate on pace.

Both vendors expose the same two windows under different names, so this
normalises them to one shape and applies one pace rule. Neither routine
implements the gate itself, for the same reason neither ranks anything itself.

    usage.py claude          # the two windows, as JSON
    usage.py codex
    usage.py gate codex      # exit 0 to proceed, 1 if over pace, 2 if unknown

**The gate must be called from inside a live session**, after that session has
made at least one model call. Both sources are written *by* a running session,
so a reading taken before one is stale — and a stale reading always understates
usage, because a window's used percentage only rises. Gating on a stale value
systematically overestimates headroom, which is the exact failure the budget
exists to prevent. Hence: start, do a turn, read, then exit if over pace. A
no-op session costs nothing; the work is what costs.

Unknown fails closed. A run that cannot read its budget does not work.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import datetime
import sys
import time
from datetime import timezone
from typing import Dict, List, Optional

CLAUDE_CACHE = os.path.expanduser("~/.claude/command-center-usage.json")
CLAUDE_TRANSCRIPTS = os.path.expanduser("~/.claude/projects/*/*.jsonl")
CODEX_SESSIONS = os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl")

# -- The local token estimate -------------------------------------------------
#
# Claude's real usage percentages are only readable through the statusline
# cache, and a scheduled run never writes one — the desktop app renders no
# status line. So for scheduled work the real signal is never available, and a
# gate that fails closed on it is not a safety property but an off switch.
#
# The fallback measures what Claude Code itself actually spent, from the
# transcripts it writes anyway, and converts that to a percentage of an observed
# capacity.
#
# **This undercounts.** It cannot see claude.ai or mobile usage on the same
# subscription, which is exactly the objection plan.md raised against estimating
# from these files. That objection stands: this is an estimate, it errs low, and
# it carries an explicit haircut below to lean the other way. It is used only
# when the real reading is unavailable.

#: Only Opus is budgeted. It is what actually consumes a subscription window —
#: the 5-hour window that came closest to the limit on 2026-09-05 carried 713k
#: Opus output tokens and 44k of everything else. Sonnet is cheap enough that
#: counting it adds arithmetic without changing a decision.
BUDGETED_MODEL = "opus"

#: Base capacity in Opus output tokens, calibrated 2026-09-05 against Claude's
#: own usage panel: 1,667,023 tokens since the weekly reset reading 61% used,
#: and 262,413 in the trailing five hours reading 45%. The weekly figure has the
#: +50% promo of that day divided back out.
#:
#: An earlier pair of numbers was wrong in both directions, because the weekly
#: one was calibrated against a percentage read off the *ChatGPT* usage panel
#: rather than Claude's. Cross-wiring two providers' figures produces a
#: confidently wrong constant, so re-derive these only from Claude's own panel.
#: The five-hour figure is deliberately generous, and is the weaker of the two.
#: Its calibration is self-contradictory: 262,413 tokens read 45% on the panel,
#: implying 583k capacity, yet a measured 801,303-token five-hour stretch did not
#: hit the limit. Both cannot hold, so the five-hour limit is evidently not a
#: function of Opus output tokens alone — cache reads dwarf output in volume and
#: very likely carry weight. Set above the largest stretch actually observed, so
#: this window does not produce false refusals; the weekly window, which
#: calibrates cleanly, is the load-bearing gate.
FIVE_HOUR_CAPACITY = 900_000.0
WEEKLY_CAPACITY = 1_822_000.0

#: No inflation. The capacities above are calibrated from the real panel using
#: these same token counts, so any systematic blind spot is already absorbed
#: into them — a haircut on top would double-count the conservatism. The
#: reserves below carry the margin instead. Raise this only if the estimate is
#: observed reading low against the panel.
ESTIMATE_HAIRCUT = 1.0

#: When the weekly window resets, in local time. The estimate counts tokens
#: **since the most recent reset**, not over a trailing seven days.
#:
#: A trailing window seemed conservative and is in fact broken: at a reset the
#: real usage drops to zero while a trailing count keeps the previous week's
#: tokens for another seven days, so the gate would refuse for days against a
#: completely fresh budget. Knowing the anchor also gives the weekly window a
#: cycle position, so it can use the same proportional pace line as a real
#: reading instead of falling back to a flat ceiling.
WEEKLY_RESET_WEEKDAY = 5  # Monday is 0, so 5 is Saturday
WEEKLY_RESET_HOUR = 12    # local noon


def last_weekly_reset(now: float) -> float:
    """The most recent weekly reset at or before `now`, in local time."""
    here = datetime.datetime.fromtimestamp(now)
    candidate = here.replace(
        hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0
    ) - datetime.timedelta(days=(here.weekday() - WEEKLY_RESET_WEEKDAY) % 7)
    if candidate > here:
        candidate -= datetime.timedelta(days=7)
    return candidate.timestamp()


#: Anthropic runs limit promos regularly, so the boost is read at runtime rather
#: than written into this file with an expiry date. The app caches the notice it
#: displays; that is the source.
CLAUDE_APP_CONFIG = os.path.expanduser("~/.claude.json")


def promo_multiplier(window: str, now: float) -> float:
    """How much a current promo inflates a window's capacity, e.g. 1.5.

    **Only applied when the promo can be confirmed active**, meaning both the
    percentage and an end date in the future parse out of the notice. An
    unconfirmed boost is ignored, because assuming one that has lapsed would
    raise capacity, lower the apparent usage, and permit overspending — while
    ignoring a real one merely makes the gate stricter than it needs to be.
    """
    import re

    try:
        with open(CLAUDE_APP_CONFIG) as fh:
            notices = (
                json.load(fh)
                .get("cachedGrowthBookFeatures", {})
                .get("tengu_rate_limit_promo_notices", [])
            )
    except (OSError, ValueError, AttributeError):
        return 1.0

    for notice in notices if isinstance(notices, list) else []:
        if not isinstance(notice, dict) or notice.get("bar") != window:
            continue
        text = str(notice.get("text") or "")
        percent = re.search(r"\+\s*(\d{1,3})\s*%", text)
        through = re.search(
            r"through\s+([A-Z][a-z]{2})\w*\s+(\d{1,2})", text
        )
        if not percent or not through:
            continue
        month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        try:
            index = month.index(through.group(1)) + 1
        except ValueError:
            continue
        today = datetime.datetime.fromtimestamp(now, timezone.utc)
        ends = datetime.datetime(
            today.year, index, int(through.group(2)), 23, 59, tzinfo=timezone.utc
        )
        if ends < today:
            continue  # lapsed, or a stale cached notice
        return 1.0 + int(percent.group(1)) / 100.0
    return 1.0


def capacity(window: str, now: float) -> float:
    base = FIVE_HOUR_CAPACITY if window == "five_hour" else WEEKLY_CAPACITY
    return base * promo_multiplier(window, now)

#: A reading older than this is not trusted. Both sources are refreshed by the
#: reading session itself, so anything older than a few minutes means the gate
#: was called before the session did any work — or that the app is not running.
MAX_AGE = 15 * 60

#: The week's budget: aim to have used no more than this by the end of the
#: 7-day window, leaving ~10% for Nate.
WEEKLY_TARGET = 90.0

#: An early-cycle allowance. Without it the proportional line starts at zero, so
#: the reserve alone exceeds it and nothing can run for the first day or so of
#: every week — a dead zone at exactly the moment the budget is most free.
#:
#: Raised to 50.0 on 2026-09-06 and **reverted the same day**. The raise did let
#: the routines run — they reviewed and merged PR #36, the first end-to-end cycle
#: — but it did not create bandwidth. It moved Nate's own weekly budget to the
#: automations, which is the same contention pointed the other way. He does his
#: real work on this subscription; the routines are not entitled to it.
#:
#: The lesson is that this number cannot solve the problem. Routines competing
#: with Nate for one pool is a *capacity* problem, and the only fix that adds
#: capacity rather than reallocating it is a second provider with its own quota.
#: Until that exists, the routines are correctly starved. See MIGRATION.md P3b.
WEEKLY_FLOOR = 25.0

#: Never burn a 5-hour window past this. The rolling 5-hour window, not the
#: weekly one, is what actually locks Nate out of his own account.
FIVE_HOUR_CEILING = 80.0

#: What a run is assumed to cost, reserved before it is allowed to start.
#:
#: A gate that only asks "are we under the line *now*" is a start check, not a
#: bound on spend — it will wave through a run that then blows straight past the
#: line, because nothing can cap a session's consumption once it begins. So the
#: gate reserves the cost of the run it is authorising.
#:
#: Sized against what a run actually costs, now that we can measure it. A
#: Claude routine run with nothing to do cost 2,690 output tokens; even a run
#: ten times heavier is about 27,000. Against the capacities above that is
#: roughly 1.5% of a weekly window and 4.6% of a five-hour one.
#:
#: The reserves are therefore several times the largest plausible run, not the
#: order-of-magnitude guesses they started as — those were set before any run
#: had happened, and made the gate refuse work it had ample room for. Widen them
#: again if the heartbeat shows real runs costing more than this.
WEEKLY_RESERVE = 5.0
FIVE_HOUR_RESERVE = 10.0

# -- The idle rule, Codex only ------------------------------------------------
#
# The pace gate above answers "is there budget left". It permits Codex to climb
# to FIVE_HOUR_CEILING, which is fine at three in the morning and wrong at three
# in the afternoon: it lets a scheduled run eat the window Nate is actively
# working in. He asked for the opposite of a ceiling — a **presence test**.
#
# Five-hour utilisation is the proxy. If none of the window has been spent, Nate
# is not at the keyboard, and the machine may take a ticket. If any of it has, he
# is, and it may not start.
#
# The reading is account-wide — it comes from the `rate_limits` records ChatGPT
# writes into Codex's own session files — so **there is no way to tell his usage
# from the agent's**. A literal "must read zero" test would therefore self-block:
# the first run pushes the window above zero and every later run in that window
# refuses, capping the funnel at one ticket per five hours.
#
# So the test is on *entry*, not on every run. A window that was already at zero
# when a run started is a window Nate was absent for, and later runs may continue
# in it up to a low ceiling. He is never competed with at the moment that matters,
# and the funnel can still chain work overnight.
#
#: A window must read exactly this to be opened. Not "close to zero" — the point
#: is that he has not touched it, and any figure above zero means he has.
IDLE_WINDOW_START = 0.0

#: Once opened, how far a chain of runs may take that window. A guess, and
#: labelled as one: no Codex run has ever worked a funnel ticket, so nobody knows
#: what one costs as a share of a five-hour window. Recalibrate from the
#: heartbeat records once real numbers exist rather than defending this number.
IDLE_WINDOW_CEILING = 15.0

#: Two readings of the same window can report `resets_at` a little apart.
IDLE_RESET_TOLERANCE = 120.0

FIVE_HOUR = 300 * 60
SEVEN_DAY = 10080 * 60


def read_claude_local(now: Optional[float] = None) -> Optional[Dict]:
    """Estimate Claude usage from Claude Code's own transcripts.

    Counts weighted output tokens in the trailing 5-hour and 7-day windows and
    expresses them as a percentage of observed capacity. Sessions record their
    own consumption, so this works for a scheduled run — unlike the statusline
    cache, which such a run never writes.
    """
    now = time.time() if now is None else now
    reset = last_weekly_reset(now)
    cutoff = min(reset, now - FIVE_HOUR)
    five_hour = weekly = 0.0
    seen = False

    for path in glob.glob(CLAUDE_TRANSCRIPTS):
        try:
            # A file untouched since before the window cannot hold a record
            # inside it. Skipping those keeps this cheap as transcripts pile up.
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, errors="replace") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    if BUDGETED_MODEL not in (message.get("model") or "").lower():
                        continue
                    tokens = (message.get("usage") or {}).get("output_tokens")
                    stamp = _epoch(record.get("timestamp") or "")
                    if not tokens or stamp is None or stamp < cutoff:
                        continue
                    seen = True
                    if stamp >= reset:
                        weekly += tokens
                    if stamp >= now - FIVE_HOUR:
                        five_hour += tokens
        except OSError:
            continue

    if not seen:
        return None

    return {
        "source": "claude-local-estimate",
        "captured_at": now,
        "estimated": True,
        "windows": {
            "five_hour": {
                "used_percent": min(
                    100.0,
                    100.0 * five_hour * ESTIMATE_HAIRCUT
                    / capacity("five_hour", now),
                ),
                # The 5-hour window is anchored to first use, which is not
                # knowable here, so this one stays a trailing count — and the
                # five-hour rule is a flat ceiling anyway.
                "rolling": True,
                "resets_at": now + FIVE_HOUR,
            },
            "seven_day": {
                "used_percent": min(
                    100.0,
                    100.0 * weekly * ESTIMATE_HAIRCUT
                    / capacity("seven_day", now),
                ),
                "rolling": False,
                "resets_at": reset + SEVEN_DAY,
            },
        },
        "opus_output_tokens": {"five_hour": five_hour, "seven_day": weekly},
    }


def read_claude() -> Optional[Dict]:
    """Claude Code's statusline cache. See statusline.sh."""
    try:
        with open(CLAUDE_CACHE) as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    windows = {k: raw[k] for k in ("five_hour", "seven_day") if k in raw}
    if not windows:
        return None
    return {
        "source": "claude",
        "captured_at": raw.get("captured_at"),
        "windows": {
            name: {
                "used_percent": w.get("used_percentage"),
                "resets_at": w.get("resets_at"),
            }
            for name, w in windows.items()
        },
    }


def read_codex() -> Optional[Dict]:
    """Codex writes rate_limits into its session rollout JSONL.

    There is no single current-state file, so this takes the most recent
    reading across every session — which is the routine's own, when called from
    inside a run.
    """
    newest = None
    for path in glob.glob(CODEX_SESSIONS):
        try:
            with open(path) as fh:
                for line in fh:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    limits = _find_rate_limits(record)
                    stamp = record.get("timestamp") or ""
                    if limits and (newest is None or stamp > newest[0]):
                        newest = (stamp, limits)
        except OSError:
            continue

    if newest is None:
        return None
    stamp, limits = newest

    # Match windows by their length, not by their key. "primary" and
    # "secondary" are positional names a vendor is free to renumber, whereas
    # "300 minutes" is the durable fact. A window whose length matches neither
    # is dropped rather than guessed at.
    windows = {}
    for key, window in limits.items():
        if not isinstance(window, dict) or "window_minutes" not in window:
            continue
        seconds = (window.get("window_minutes") or 0) * 60
        for name, length in (("five_hour", FIVE_HOUR), ("seven_day", SEVEN_DAY)):
            if abs(seconds - length) <= length * 0.1:
                windows[name] = {
                    "used_percent": window.get("used_percent"),
                    "resets_at": window.get("resets_at"),
                }
                break

    return {
        "source": "codex",
        "captured_at": _epoch(stamp),
        "windows": windows,
    } if windows else None


def _find_rate_limits(node):
    if isinstance(node, dict):
        limits = node.get("rate_limits")
        if isinstance(limits, dict):
            return limits
        for value in node.values():
            found = _find_rate_limits(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_rate_limits(value)
            if found:
                return found
    return None


def _epoch(stamp: str) -> Optional[int]:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            import calendar
            import datetime

            return calendar.timegm(datetime.datetime.strptime(stamp, fmt).timetuple())
        except ValueError:
            continue
    return None


def opened_idle(agent: str, resets_at: Optional[float]) -> Optional[bool]:
    """Did a run start in *this* five-hour window while it still read zero?

    Read from the heartbeat records, which already carry each run's usage
    reading — no new state, and nothing to keep in step. `resets_at` is what
    identifies the window: two runs share one when their recorded reset times
    agree.

    Returns None when the records cannot be read at all. The caller treats that
    as "not opened" and skips: refusing to work is the healthy outcome here, and
    guessing the window was idle is the one mistake that competes with Nate.
    """
    if resets_at is None:
        return None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat

        records = heartbeat.read(agent)
    except Exception:
        # Only a genuine failure to read is "unknown". No records at all is a
        # definite answer — nothing has started in this window, so it was not
        # opened — and saying so beats reporting a diagnosable state as a mystery.
        return None
    if not records:
        return False

    for record in records:
        if record.get("phase") != "start":
            continue
        window = (record.get("usage") or {}).get("five_hour")
        # Older records stored a bare percentage with no reset time, so they
        # cannot be placed in a window and are ignored rather than guessed at.
        if not isinstance(window, dict):
            continue
        was = window.get("used_percent")
        when = window.get("resets_at")
        if was is None or when is None:
            continue
        if abs(float(when) - float(resets_at)) > IDLE_RESET_TOLERANCE:
            continue
        if float(was) <= IDLE_WINDOW_START:
            return True
    return False


def idle_verdict(agent: str, reading: Dict) -> Optional[Dict]:
    """Whether Codex may work right now, on the presence test rather than budget.

    `None` means the rule does not apply — it is Codex-only, because Claude has
    no trustworthy signal to test: a scheduled Claude run writes no statusline,
    so its reading falls back to a token estimate that cannot say "you spent
    none of this".
    """
    if agent != "codex":
        return None
    five = (reading.get("windows") or {}).get("five_hour") or {}
    used = five.get("used_percent")
    if used is None:
        return None

    if float(used) <= IDLE_WINDOW_START:
        return {"used_percent": float(used), "opened": True, "over": False,
                "why": "window untouched — Nate is away, take a ticket"}

    opened = opened_idle(agent, five.get("resets_at"))
    if opened and float(used) <= IDLE_WINDOW_CEILING:
        return {"used_percent": float(used), "opened": True, "over": False,
                "why": "window was opened idle; continuing under {:.0f}%".format(
                    IDLE_WINDOW_CEILING)}
    if opened:
        return {"used_percent": float(used), "opened": True, "over": True,
                "why": "window was opened idle but is now over {:.0f}%".format(
                    IDLE_WINDOW_CEILING)}
    if opened is None:
        return {"used_percent": float(used), "opened": None, "over": True,
                "why": "cannot tell whether this window was opened idle"}
    return {"used_percent": float(used), "opened": False, "over": True,
            "why": "window already in use when it began — Nate is working"}


def pace(reading: Dict, now: float, provider: Optional[str] = None) -> Dict:
    """Is this agent within its budget?

    The weekly line is proportional: by the time a fraction f of the 7-day
    window has elapsed, at most f x WEEKLY_TARGET should be spent. That keeps
    the burn even instead of letting a routine spend the week's capacity on
    Monday, and it needs no persistence — the window's own resets_at says how
    far through it we are.
    """
    verdicts = []
    windows = reading.get("windows", {})

    five = windows.get("five_hour") or {}
    if five.get("used_percent") is not None:
        verdicts.append(
            {
                "window": "five_hour",
                "used_percent": five["used_percent"],
                "reserve": FIVE_HOUR_RESERVE,
                "allowed_percent": policy(provider, "five_hour_ceiling",
                                          FIVE_HOUR_CEILING),
                "over": five["used_percent"] + FIVE_HOUR_RESERVE > policy(
                    provider, "five_hour_ceiling", FIVE_HOUR_CEILING),
            }
        )

    seven = windows.get("seven_day") or {}
    if seven.get("used_percent") is not None and seven.get("resets_at"):
        if seven.get("rolling"):
            # A trailing count has no cycle position, so the proportional line
            # is meaningless — it would read "0% allowed" forever. A rolling
            # total gets a flat ceiling instead.
            elapsed_fraction = None
            allowed = policy(provider, "weekly_target", WEEKLY_TARGET)
        else:
            remaining = max(0.0, float(seven["resets_at"]) - now)
            elapsed_fraction = max(0.0, min(1.0, 1.0 - remaining / SEVEN_DAY))
            allowed = max(policy(provider, "weekly_floor", WEEKLY_FLOOR),
                          policy(provider, "weekly_target", WEEKLY_TARGET)
                          * elapsed_fraction)
        verdicts.append(
            {
                "window": "seven_day",
                "used_percent": seven["used_percent"],
                "reserve": policy(provider, "weekly_reserve", WEEKLY_RESERVE),
                "allowed_percent": round(allowed, 1),
                "elapsed_fraction": (
                    round(elapsed_fraction, 3) if elapsed_fraction is not None else None
                ),
                "over": seven["used_percent"] + policy(
                    provider, "weekly_reserve", WEEKLY_RESERVE) > allowed,
            }
        )

    return {
        "windows": verdicts,
        "over_pace": any(v["over"] for v in verdicts),
        # No window read at all is not "under pace" — it is unknown.
        "known": bool(verdicts),
    }


# -- providers ----------------------------------------------------------------
#
# A budget belongs to a **provider**, not to an agent. Today the two look alike
# because each agent has a pool to itself, which is exactly why the distinction is
# easy to lose — and it stops being true the moment one subscription serves two
# harnesses. z.ai's Coding Plan would do that: GLM implementing in Codex and GLM
# reviewing in Claude Code draw the same 5-hour and weekly credits. Two agents,
# one budget.
#
# Keyed by agent because that is what a caller has; the value is the pool it
# spends. Adding a provider is this map plus a reader.

PROVIDERS = {"claude": "anthropic", "codex": "openai", "zcode": "zai"}

#: z.ai reports quota directly, so nothing here is estimated. Found by reading
#: z.ai's own `glm-plan-usage` plugin rather than guessing endpoints; the token
#: is sent bare, without a `Bearer` prefix (either works).
#:
#: **Do not compute credits from the published multipliers.** That was tried:
#: (fresh x 6.9 + cached x 1.7 + output x 24) / 10,000 estimated 23.5 credits for
#: a session the API scored at 3. Roughly 8x wrong, in the direction that would
#: have starved the pool for no reason. The API is authoritative; arithmetic from
#: documentation is not.
ZAI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"

#: Where the key lives. Never in the repo, never in a shell profile.
ZAI_KEYCHAIN_SERVICE = "ZAI_API_KEY"

#: Agents that spend a pool, keyed by the pool. The inverse of PROVIDERS, used to
#: say who else is drawing on the same credits.
def agents_on(provider: str) -> List[str]:
    return sorted(a for a, p in PROVIDERS.items() if p == provider)


#: Hold this share of a pool back from work that *creates* downstream work.
#:
#: An implementation run that spends the last of a shared pool leaves its own PR
#: unreviewable, and an unreviewed PR is worse than an unstarted ticket: the
#: quota is gone and nothing shipped. This is the same idea as the per-run
#: reserves above, one level out — reserve for the stage that has to follow, not
#: only for the run being authorised.
#:
#: Only bites when a provider serves more than one agent. With a pool to itself
#: there is no downstream stage on the same credits to protect.
DOWNSTREAM_RESERVE = 20.0

# -- pacing policy, per provider ----------------------------------------------
#
# The constants above were written for a pool Nate **shares**. `WEEKLY_FLOOR` in
# particular exists to leave him room on his own subscription, and applying it to
# a pool he never touches caps the automations at 25% of a budget bought for them.
#
# So policy is per provider, not global. State was already per provider (P0.2);
# this is the half that was missed.

PROVIDER_POLICY = {
    # Bought for the automations and used for nothing else, so there is no
    # interactive share to protect. Nate set the floor to 90 on 2026-09-06: he
    # has no plans to use the app by hand, and 90% still leaves 1,000 credits.
    #
    # At 90 the floor equals `WEEKLY_TARGET`, so the proportional line is inert
    # and this is a flat cap. That is deliberate and it is the trade: no early-week
    # smoothing, so a runaway could spend the week by Tuesday. Acceptable while the
    # queue is small and a run costs ~3 credits; revisit if a retry storm ever
    # empties it.
    #
    # The reserve moves with it. 5% of a weekly window is calibrated for Anthropic,
    # where one run is ~1.5% of the budget. Measured here, a whole session cost
    # **3 credits of 10,000** — 0.03% — so the shared reserve would hold back 500
    # credits against a run that costs three, and the cap would really bite at 85%.
    "zai": {"weekly_floor": 90.0, "weekly_reserve": 0.5},
}


def policy(provider: Optional[str], name: str, default: float) -> float:
    return (PROVIDER_POLICY.get(provider or "", {}) or {}).get(name, default)


def provider_of(agent: str) -> Optional[str]:
    return PROVIDERS.get(agent)


def shares_a_pool(agent: str) -> bool:
    """Whether another agent spends the same credits as this one."""
    provider = provider_of(agent)
    return bool(provider) and len(agents_on(provider)) > 1


def _zai_key() -> Optional[str]:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", ZAI_KEYCHAIN_SERVICE,
             "-a", os.environ.get("USER", ""), "-w"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def read_zai(now: float) -> Optional[Dict]:
    """Remaining z.ai Coding Plan quota, read rather than estimated.

    Two windows come back as `CREDIT_LIMIT` entries carrying `usage` (the limit),
    `currentValue` (spent) and `nextResetTime` in epoch milliseconds. They are
    told apart by how far away that reset is, not by the `unit`/`number` enum,
    whose meaning z.ai does not document — a shape that survives them renumbering
    it.
    """
    key = _zai_key()
    if not key:
        return None
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "20", ZAI_QUOTA_URL,
             "-H", "Authorization: " + key,
             "-H", "Accept-Language: en-US,en",
             "-H", "Content-Type: application/json"],
            capture_output=True, text=True, timeout=30)
        payload = json.loads(out.stdout)
    except Exception:
        return None
    if not payload.get("success"):
        return None

    windows = {}
    for limit in (payload.get("data") or {}).get("limits", []):
        cap = limit.get("usage")
        spent = limit.get("currentValue")
        reset = limit.get("nextResetTime")
        if not cap or spent is None or not reset:
            continue
        resets_at = float(reset) / 1000.0
        name = "five_hour" if (resets_at - now) <= 86400 else "seven_day"
        windows[name] = {
            "used_percent": round(100.0 * float(spent) / float(cap), 2),
            "resets_at": resets_at,
        }
    if not windows:
        return None
    return {"source": "zai", "captured_at": now, "windows": windows}


def read_agent(agent: str, now: float) -> Optional[Dict]:
    """Best available reading for an agent, preferring a real measurement.

    Codex records its own rate limits, so it always has one. Claude does not in
    a scheduled run, so a fresh statusline cache is used when it exists — which
    it will on days Nate has worked in a terminal — and the local token estimate
    fills in otherwise. The estimate is never preferred over a real reading.
    """
    provider = provider_of(agent)
    if provider is None:
        # An agent with no pool is a configuration error, not an empty budget.
        # AGENTS.md: missing usage data fails closed.
        return None
    if provider == "openai":
        return read_codex()
    if provider == "zai":
        return read_zai(now)
    if provider != "anthropic":
        # A registered provider with no reader yet. Returning None fails the gate
        # closed rather than waving work through on a budget nobody can see —
        # which is the failure mode a provider registry invites.
        return None

    cached = read_claude()
    age = now - cached["captured_at"] if cached and cached.get("captured_at") else None
    if age is not None and -60 <= age <= MAX_AGE:
        return cached
    return read_claude_local(now) or cached


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    # Driven by the registry, so adding a provider adds its CLI surface too.
    for name in sorted(PROVIDERS):
        sub.add_parser(name, help="read {}'s usage".format(name))
    gate = sub.add_parser("gate", help="exit 1 if over pace, 2 if unknown")
    gate.add_argument("agent", choices=sorted(PROVIDERS))
    args = parser.parse_args(argv)

    now = time.time()
    agent = args.agent if args.command == "gate" else args.command
    reading = read_agent(agent, now)

    if reading is None:
        print("usage: no reading available for {}".format(agent), file=sys.stderr)
        return 2

    age = now - reading["captured_at"] if reading.get("captured_at") else None
    reading["age_seconds"] = int(age) if age is not None else None

    if args.command != "gate":
        print(json.dumps(reading, indent=2))
        return 0

    # A reading from the future is as untrustworthy as an old one — clock skew
    # or a malformed timestamp would otherwise sail straight through the gate,
    # and every failure here reads *low*, which is the direction that burns the
    # week.
    if age is None or age > MAX_AGE or age < -60:
        if age is None:
            why = "undated"
        elif age < 0:
            why = "dated {:.0f}s in the future".format(-age)
        else:
            why = "{:.0f}s old".format(age)
        print(
            "usage: {} reading is {} — refusing to guess".format(agent, why),
            file=sys.stderr,
        )
        return 2

    verdict = pace(reading, now, provider=provider_of(agent))
    idle = idle_verdict(agent, reading)

    # A pool shared with another agent keeps headroom for the stage that follows.
    if shares_a_pool(agent) and verdict.get("known"):
        for window in verdict["windows"]:
            allowed = window["allowed_percent"] - DOWNSTREAM_RESERVE
            if window["used_percent"] + window["reserve"] > allowed:
                window["over"] = True
                window["allowed_percent"] = round(allowed, 1)
                window["shared_with"] = [
                    a for a in agents_on(provider_of(agent)) if a != agent]
        verdict["over_pace"] = any(v["over"] for v in verdict["windows"])
    if not verdict["known"]:
        print("usage: no usable window for {}".format(agent), file=sys.stderr)
        return 2

    if reading.get("estimated"):
        print(
            "usage: no real reading for {}; using the local token estimate "
            "(undercounts claude.ai and mobile)".format(agent),
            file=sys.stderr,
        )

    for window in verdict["windows"]:
        print(
            "{:<10} {:>5.1f}% used + {:>4.1f}% reserved = {:>5.1f}%, "
            "{:>5.1f}% allowed  {}".format(
                window["window"],
                window["used_percent"],
                window["reserve"],
                window["used_percent"] + window["reserve"],
                window["allowed_percent"],
                "OVER" if window["over"] else "ok",
            ),
            file=sys.stderr,
        )
    if idle is not None:
        print(
            "idle       {:>5.1f}% of the five-hour window spent  {}  — {}".format(
                idle["used_percent"],
                "OVER" if idle["over"] else "ok",
                idle["why"],
            ),
            file=sys.stderr,
        )
    return 1 if (verdict["over_pace"] or (idle or {}).get("over")) else 0


if __name__ == "__main__":
    sys.exit(main())
