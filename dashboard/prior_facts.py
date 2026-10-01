"""Read prior dashboard PR facts for display-only carry-forward.

The publisher spool is a recoverable display buffer, never state of record.
Callers use these facts only to label board rows when the live PR fact for a
ticket could not be established.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple


MAX_PRIOR_AGE = timedelta(hours=24)
DISABLE_CARRY_FORWARD_FLAG = "disable-pr-carry-forward"
PR_STATES = frozenset({"approved", "changes requested", "merged", "submitted"})


def _timestamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _utc(value: object) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _prior_ticket_facts(payload: object) -> Dict[str, Dict[str, object]]:
    facts: Dict[str, Dict[str, object]] = {}
    if not isinstance(payload, Mapping):
        return facts
    board = payload.get("board")
    if not isinstance(board, Mapping):
        return facts
    columns = board.get("columns")
    if not isinstance(columns, list):
        return facts
    for column in columns:
        if not isinstance(column, Mapping):
            continue
        items = column.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            tickets = item.get("tickets")
            if not isinstance(tickets, list):
                continue
            for ticket in tickets:
                if not isinstance(ticket, Mapping):
                    continue
                ref = ticket.get("ref")
                stale = ticket.get("pr_stale") is True
                state = (
                    ticket.get("pr_stale_state") if stale
                    else ticket.get("pr")
                )
                if (
                    not isinstance(ref, str)
                    or not isinstance(state, str)
                    or state not in PR_STATES
                ):
                    continue
                fact: Dict[str, object] = {"pr": state}
                number = ticket.get(
                    "pr_stale_number" if stale else "pr_number"
                )
                if isinstance(number, int) and not isinstance(number, bool):
                    fact["pr_number"] = number
                if stale:
                    # A failed brief republishes the carried state. Keep its
                    # original live capture time so each consecutive failure
                    # ages the same fact instead of renewing it.
                    original_capture = _timestamp(
                        ticket.get("pr_stale_captured_at")
                    )
                    if original_capture is None:
                        continue
                    fact["_captured_at"] = original_capture
                facts[ref] = fact
    return facts


def read_prior_pr_facts(
    spool_dir: Path,
) -> Tuple[Optional[datetime], Dict[str, Dict[str, object]]]:
    """Return facts and capture time from the newest parseable spool entry."""
    try:
        paths = sorted(Path(spool_dir).glob("*.json"), key=lambda path: path.name)
    except OSError:
        return None, {}

    newest: Optional[Tuple[datetime, str, object]] = None
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, Mapping):
            continue
        captured_at = _timestamp(payload.get("generated_at"))
        if captured_at is None:
            continue
        candidate = (captured_at, path.name, payload)
        if newest is None or candidate[:2] > newest[:2]:
            newest = candidate

    if newest is None:
        return None, {}
    captured_at, _name, payload = newest
    return captured_at, _prior_ticket_facts(payload)


def prior_pr_carry_forward_enabled(spool_dir: Path) -> bool:
    """Default on unless the runtime spool contains its disable flag."""
    flag = Path(spool_dir) / DISABLE_CARRY_FORWARD_FLAG
    try:
        flag.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        # An unreadable switch must restore the established live/unknown board.
        return False
    return False


def _age_label(age: timedelta) -> str:
    minutes = max(0, int(age.total_seconds() // 60))
    hours, remaining_minutes = divmod(minutes, 60)
    if hours and remaining_minutes:
        return "{}h{}m".format(hours, remaining_minutes)
    if hours:
        return "{}h".format(hours)
    return "{}m".format(minutes)


def _live_fact_is_established(
    live_facts: Mapping[str, object], ref: str, facts_known: bool,
) -> bool:
    if not facts_known or ref not in live_facts:
        return False
    fact = live_facts[ref]
    # A present None means both PR and branch absence were established. A
    # non-empty mapping can also establish useful live facts without a PR
    # state (for example, branch_exists=True). Only the empty mapping is the
    # truncated-PR form that still permits carry-forward.
    return not isinstance(fact, Mapping) or bool(fact)


def carry_forward_display_facts(
    ticket_refs: Iterable[str],
    live_facts: Optional[Mapping[str, object]],
    *,
    live_facts_known: bool,
    captured_at: Optional[datetime],
    prior_facts: Optional[Mapping[str, Mapping[str, object]]],
    now: datetime,
) -> Dict[str, Dict[str, object]]:
    """Mark missing open-ticket facts stale, or unknown when no safe prior exists.

    A present live ``None`` is an established absence and is left alone. An
    omitted or incomplete per-ticket fact can use the prior board while it is
    younger than 24 hours; the caller passes only current open ticket refs.
    """
    live = live_facts or {}
    previous = prior_facts or {}
    current_at = _utc(now)
    default_prior_at = _utc(captured_at)
    overrides: Dict[str, Dict[str, object]] = {}

    for ref in ticket_refs:
        if _live_fact_is_established(live, ref, live_facts_known):
            continue
        prior = previous.get(ref)
        prior_at = (
            _utc(prior.get("_captured_at"))
            if isinstance(prior, Mapping) else None
        ) or default_prior_at
        age = (
            current_at - prior_at
            if current_at is not None and prior_at is not None else None
        )
        usable_prior = (
            age is not None
            and timedelta(0) <= age < MAX_PRIOR_AGE
        )
        prior = previous.get(ref) if usable_prior else None
        state = prior.get("pr") if isinstance(prior, Mapping) else None
        if isinstance(state, str) and state in PR_STATES:
            override: Dict[str, object] = {
                "status": "stale",
                "pr": state,
                "age": _age_label(age),
                "captured_at": prior_at.isoformat(),
            }
            number = prior.get("pr_number")
            if isinstance(number, int) and not isinstance(number, bool):
                override["pr_number"] = number
            overrides[ref] = override
        else:
            overrides[ref] = {"status": "unknown"}
    return overrides


def dashboard_pr_display_overrides(
    spool_dir: Path,
    ticket_refs: Iterable[str],
    live_facts: Optional[Mapping[str, object]],
    *,
    live_facts_known: bool,
    now: datetime,
) -> Dict[str, Dict[str, object]]:
    """Load display-only carry-forward unless its runtime flag is set."""
    if not prior_pr_carry_forward_enabled(spool_dir):
        return {}
    captured_at, prior_facts = read_prior_pr_facts(spool_dir)
    return carry_forward_display_facts(
        ticket_refs,
        live_facts,
        live_facts_known=live_facts_known,
        captured_at=captured_at,
        prior_facts=prior_facts,
        now=now,
    )
