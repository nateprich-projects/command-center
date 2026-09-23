#!/usr/bin/env python3
"""Derive and append one hourly execution-metrics row.

``metrics.jsonl`` is an append-only projection on the heartbeat branch.  Rows
keep source facts (and numerator/denominator pairs for rates) so the later
series reader can roll up days without averaging rounded daily percentages.
Missing evidence stays ``null`` with a source and reason; an empty observation
is represented by a real zero only when the source was readable.
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPO = "nateprich-projects/command-center"
BRANCH = "heartbeat"
METRICS_PATH = "metrics.jsonl"
AGENTS = ("muse", "claude", "codex")
HOUR_SECONDS = 60 * 60
STORE_BACKOFF = (1, 3, 7)
FINISH_OUTCOMES = (
    "done", "nothing-to-do", "errored", "skipped-over-pace",
    "skipped-api-reserve", "skipped-provider-quota",
)


class MetricsError(RuntimeError):
    """An input could not be read safely, or an append was unsafe."""


def _timestamp(value: object) -> Optional[datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        found = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            found = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: object, allow_negative: bool = False) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (number < 0 and not allow_negative):
        return None
    return number


def _count(value: object, source: str, reason: str = "source value is missing") -> Dict:
    number = _number(value)
    if number is None:
        return {"value": None, "source": source, "gap": reason}
    rendered = int(number) if number.is_integer() else number
    return {"value": rendered, "source": source}


def _signed_count(
    value: object, source: str, reason: str = "source value is missing"
) -> Dict:
    number = _number(value, allow_negative=True)
    if number is None:
        return {"value": None, "source": source, "gap": reason}
    rendered = int(number) if number.is_integer() else number
    return {"value": rendered, "source": source}


def _sum_count(
    values: object,
    source: str,
    reason: str = "no observations are available",
) -> Dict:
    if not isinstance(values, list) or not values:
        return {
            "sum": None, "count": None, "source": source, "gap": reason,
        }
    numbers = [_number(value) for value in values]
    if any(value is None for value in numbers):
        return {
            "sum": None, "count": None, "source": source, "gap": reason,
        }
    total = sum(numbers)
    rendered = int(total) if total.is_integer() else round(total, 3)
    return {"sum": rendered, "count": len(numbers), "source": source}


def _fact(value: object, source: str, reason: str = "source value is missing") -> Dict:
    if value is None:
        return {"value": None, "source": source, "gap": reason}
    return {"value": value, "source": source}


def _pair(
    numerator: object,
    denominator: object,
    source: str,
    reason: str = "source numerator or denominator is missing",
) -> Dict:
    n = _number(numerator)
    d = _number(denominator)
    if n is None or d is None:
        return {
            "numerator": None, "denominator": None,
            "source": source, "gap": reason,
        }
    return {
        "numerator": int(n) if n.is_integer() else n,
        "denominator": int(d) if d.is_integer() else d,
        "source": source,
    }


def _rate_pair(
    numerator: object,
    denominator: object,
    source: str,
    reason: str = "rate numerator or denominator is missing",
) -> Dict:
    d = _number(denominator)
    if d is None or d == 0:
        return _pair(None, None, source, reason if d is None else "no denominator observations in this hour")
    return _pair(numerator, denominator, source, reason)


def _split_snapshot(snapshot: Mapping[str, object]) -> Tuple[Mapping, Mapping]:
    brief = snapshot.get("brief")
    if isinstance(brief, Mapping):
        return brief, snapshot
    # A raw ``funnel.py brief`` object is also useful for fixture callers.
    return snapshot, {}


def _section_gap(brief: Mapping[str, object], section: str) -> Optional[str]:
    for key in ("missing", "degraded"):
        rows = brief.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, str) and row == section:
                return "brief section {} was unreadable".format(section)
            if isinstance(row, Mapping) and row.get("section") == section:
                return str(row.get("reason") or "brief section {} degraded".format(section))
    return None


def _section(
    brief: Mapping[str, object], name: str, source: str
) -> Tuple[Optional[Mapping], Optional[str]]:
    problem = _section_gap(brief, name)
    value = brief.get(name)
    if problem:
        return None, problem
    if not isinstance(value, Mapping):
        return None, "{} is absent from the brief".format(source)
    return value, None


def _interval(now: datetime) -> Tuple[datetime, datetime, str]:
    current = now.astimezone(timezone.utc)
    start_epoch = int(current.timestamp()) // HOUR_SECONDS * HOUR_SECONDS
    start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
    end = start + timedelta(hours=1)
    return start, end, _iso(start)


def _in_interval(value: object, start: datetime, end: datetime) -> Optional[datetime]:
    found = _timestamp(value)
    return found if found is not None and start <= found < end else None


def _ticket_identity(record: Mapping[str, object]) -> Tuple[Optional[str], Optional[int]]:
    repo = record.get("repo") or record.get("repository")
    number = record.get("number")
    ticket = record.get("ticket")
    if isinstance(ticket, str) and "#" in ticket:
        ticket_repo, ticket_number = ticket.rsplit("#", 1)
        if not isinstance(repo, str) or not repo:
            repo = ticket_repo
        if not isinstance(number, int) and ticket_number.isdigit():
            number = int(ticket_number)
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        number = None
    return (repo if isinstance(repo, str) else None, number)


def _outcome_prs(
    records: Optional[Sequence[Mapping[str, object]]],
    start: datetime,
    end: datetime,
) -> Tuple[Optional[List[Dict]], Optional[str], Optional[str]]:
    if records is None or not records:
        reason = "outcomes.jsonl is unavailable or empty"
        return None, reason, reason
    found: List[Dict] = []
    unknown_merged_branch = False
    unknown_review_branch = False
    for record in records:
        if not isinstance(record, Mapping):
            continue
        repo, number = _ticket_identity(record)
        if not repo or number is None:
            continue
        prs = record.get("prs")
        if not isinstance(prs, list):
            continue
        for pr in prs:
            if not isinstance(pr, Mapping):
                continue
            merged_at = _in_interval(pr.get("merged_at") or pr.get("mergedAt"), start, end)
            created_at = _in_interval(pr.get("created_at") or pr.get("createdAt"), start, end)
            raw_reviewed_at = pr.get("first_reviewed_at")
            first_reviewed_at = _in_interval(raw_reviewed_at, start, end)
            branch = pr.get("head_ref_name") or pr.get("headRefName")
            if (
                pr.get("first_review_result") is not None
                and (raw_reviewed_at is None or _timestamp(raw_reviewed_at) is None)
            ):
                unknown_review_branch = True
            if not isinstance(branch, str):
                if merged_at is not None:
                    unknown_merged_branch = True
                if first_reviewed_at is not None:
                    unknown_review_branch = True
            if isinstance(branch, str) and branch == "ticket/{}".format(number):
                found.append({
                    "repo": repo,
                    "number": number,
                    "branch": branch,
                    "created_at": pr.get("created_at") or pr.get("createdAt"),
                    "merged_at": pr.get("merged_at") or pr.get("mergedAt"),
                    "first_review_result": pr.get("first_review_result"),
                    "first_reviewed_at": raw_reviewed_at,
                    "reopened_at": record.get("reopened_at"),
                    "in_hour": {
                        "created_at": created_at is not None,
                        "merged_at": merged_at is not None,
                        "first_reviewed_at": first_reviewed_at is not None,
                    },
                })
    merged_gap = (
        "a merged PR in this hour lacks its branch name"
        if unknown_merged_branch else None
    )
    review_gap = (
        "a reviewed PR in this hour lacks its branch or timestamp"
        if unknown_review_branch else None
    )
    return found, merged_gap, review_gap


def _finish_rows(
    ledgers: Optional[Mapping[str, Optional[Sequence[Mapping[str, object]]]]],
) -> Tuple[Optional[Dict[str, List[Dict]]], Optional[str]]:
    if ledgers is None:
        return None, "heartbeat ledgers are unavailable"
    rows_by_agent: Dict[str, List[Dict]] = {}
    missing = []
    for agent in AGENTS:
        rows = ledgers.get(agent)
        if rows is None:
            missing.append(agent)
            continue
        copied = [dict(row) for row in rows if isinstance(row, Mapping)]
        rows_by_agent[agent] = copied
    if missing:
        return rows_by_agent or None, "heartbeat ledger unreadable for {}".format(
            ", ".join(missing)
        )
    return rows_by_agent, None


def _run_job_kinds(rows: Sequence[Mapping[str, object]]) -> Dict[str, str]:
    kinds: Dict[str, str] = {}
    for row in rows:
        run = row.get("run")
        if not isinstance(run, str):
            continue
        if row.get("phase") == "bind":
            kind = row.get("do")
            if kind == "ticket":
                kinds[run] = "implement"
            elif isinstance(kind, str) and kind in ("implement", "review", "shape", "breakdown"):
                kinds[run] = kind
        for name in ("job_kind", "kind", "job"):
            value = row.get(name)
            if isinstance(value, str) and value in ("implement", "review", "shape", "breakdown"):
                kinds[run] = value
    return kinds


def _all_finishes(
    rows_by_agent: Optional[Mapping[str, Sequence[Mapping[str, object]]]],
) -> List[Dict]:
    found = []
    if rows_by_agent is None:
        return found
    for agent, rows in rows_by_agent.items():
        newest: Dict[str, Tuple[datetime, int, Dict]] = {}
        unattributed = []
        for index, row in enumerate(rows):
            if row.get("phase") == "finish":
                item = dict(row, agent=agent)
                run = item.get("run")
                if not isinstance(run, str) or not run:
                    unattributed.append(item)
                    continue
                stamp = _timestamp(item.get("ts")) or datetime.min.replace(
                    tzinfo=timezone.utc
                )
                current = newest.get(run)
                if current is None or (stamp, index) >= (current[0], current[1]):
                    newest[run] = (stamp, index, item)
        found.extend(item for _stamp, _index, item in newest.values())
        found.extend(unattributed)
    found.sort(key=lambda row: (_timestamp(row.get("ts")) or datetime.min.replace(
        tzinfo=timezone.utc
    ), str(row.get("run") or "")))
    return found


def _rate_map(
    counts: Mapping[Tuple[str, str], Mapping[str, int]],
    numerator_name: str,
    denominator_names: Sequence[str],
    source: str,
) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    for (agent, kind), values in sorted(counts.items()):
        denominator = sum(values.get(name, 0) for name in denominator_names)
        result.setdefault(agent, {})[kind] = _rate_pair(
            values.get(numerator_name, 0), denominator, source
        )
    return result


def _usage_window(reading: object, name: str) -> Optional[Mapping]:
    if not isinstance(reading, Mapping):
        return None
    windows = reading.get("windows")
    if not isinstance(windows, Mapping):
        return None
    window = windows.get(name)
    return window if isinstance(window, Mapping) else None


def read_codex_thread_source_usage(
    now: datetime, codex_reading: object
) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    """Attribute weekly Codex token usage to automation or other sessions."""
    window = _usage_window(codex_reading, "seven_day")
    reset_at = _timestamp(window.get("resets_at")) if window else None
    if reset_at is None:
        return None, "Codex weekly reset time is unavailable"
    start = reset_at - timedelta(days=7)
    if start > now:
        return None, "Codex weekly reset window is inconsistent"

    totals = {"funnel_tokens": 0, "personal_tokens": 0, "total_tokens": 0}
    incomplete = False
    seen_records = set()
    paths = glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/*.jsonl"))
    for path in paths:
        try:
            modified = os.path.getmtime(path)
        except OSError:
            incomplete = True
            continue
        if modified < start.timestamp():
            continue
        thread_source = None
        source_conflict = False
        events = []
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "token_usage_record" not in line and "session_meta" not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        if "token_usage_record" in line:
                            incomplete = True
                        continue
                    payload = record.get("payload")
                    payload = payload if isinstance(payload, Mapping) else record
                    if record.get("type") == "session_meta":
                        source = payload.get("thread_source") or payload.get("threadSource")
                        if isinstance(source, str) and source:
                            if thread_source is not None and thread_source != source:
                                source_conflict = True
                            thread_source = source
                    elif record.get("type") == "token_usage_record":
                        at = _timestamp(record.get("timestamp"))
                        if at is None:
                            incomplete = True
                        elif start <= at <= now:
                            events.append((record, payload))
        except OSError:
            incomplete = True
            continue

        if source_conflict and events:
            incomplete = True
        for record, payload in events:
            if not isinstance(thread_source, str) or not thread_source:
                incomplete = True
                continue
            identity = json.dumps(record, sort_keys=True, separators=(",", ":"))
            if identity in seen_records:
                continue
            seen_records.add(identity)
            raw_usage = payload.get("usage")
            usage = raw_usage if isinstance(raw_usage, Mapping) else {}
            quantity = usage.get("total_tokens")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                if (
                    isinstance(input_tokens, bool) or not isinstance(input_tokens, int)
                    or input_tokens < 0
                    or isinstance(output_tokens, bool) or not isinstance(output_tokens, int)
                    or output_tokens < 0
                ):
                    incomplete = True
                    continue
                quantity = input_tokens + output_tokens
            category = "funnel_tokens" if thread_source == "automation" else "personal_tokens"
            totals[category] += quantity
            totals["total_tokens"] += quantity

    if incomplete:
        return None, "one or more Codex session usage records are incomplete"
    return totals, None


def _count_in_hour(
    rows: object,
    start: datetime,
    end: datetime,
    fields: Sequence[str],
) -> Optional[int]:
    if not isinstance(rows, list):
        return None
    count = 0
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        stamp = None
        for field in fields:
            if row.get(field) is not None:
                stamp = _timestamp(row.get(field))
                break
        if stamp is None:
            return None
        if start <= stamp < end:
            count += 1
    return count


def _count_timestamps(
    values: object,
    start: datetime,
    end: datetime,
    source: str,
    reason: str,
) -> Dict:
    if not isinstance(values, list):
        return _count(None, source, reason)
    count = 0
    for value in values:
        stamp = _timestamp(value)
        if stamp is None:
            return _count(None, source, reason)
        if start <= stamp < end:
            count += 1
    return _count(count, source, reason)


def _hold_seconds(
    finishes: Sequence[Mapping[str, object]],
    start: datetime,
    end: datetime,
    now: datetime,
) -> Dict[str, Dict[str, float]]:
    reasons = {
        "skipped-over-pace": "over_pace",
        "skipped-api-reserve": "api_reserve",
        "skipped-provider-quota": "provider_quota",
    }
    by_agent: Dict[str, List[Mapping[str, object]]] = {}
    for row in finishes:
        at = _timestamp(row.get("ts"))
        agent = row.get("agent")
        if at is not None and isinstance(agent, str):
            by_agent.setdefault(agent, []).append(row)
    totals = {
        agent: {value: 0.0 for value in reasons.values()}
        for agent in AGENTS
    }
    for agent, rows in by_agent.items():
        rows.sort(key=lambda row: _timestamp(row.get("ts")) or start)
        for index, row in enumerate(rows):
            reason = reasons.get(str(row.get("outcome") or ""))
            if reason is None:
                continue
            began = _timestamp(row.get("ts"))
            if began is None:
                continue
            next_at = _timestamp(rows[index + 1].get("ts")) if index + 1 < len(rows) else now
            stopped = min(next_at or now, end, now)
            began = max(began, start)
            if stopped > began:
                totals.setdefault(agent, {value: 0.0 for value in reasons.values()})[
                    reason
                ] += (stopped - began).total_seconds()
    return {
        agent: {key: round(value, 3) for key, value in values.items()}
        for agent, values in totals.items()
    }


def _latency_sums(
    rows_by_agent: Optional[Mapping[str, Sequence[Mapping[str, object]]]],
    outcome_records: Optional[Sequence[Mapping[str, object]]],
    start: datetime,
    end: datetime,
) -> Tuple[Optional[Dict], Optional[str]]:
    if rows_by_agent is None or not outcome_records:
        return None, "claim or outcome source is unavailable"
    outcomes_by_ticket = {
        str(row.get("ticket")): row for row in outcome_records
        if isinstance(row, Mapping) and isinstance(row.get("ticket"), str)
    }
    bindings_by_ticket: Dict[str, List[Dict[str, object]]] = {}
    for agent, rows in rows_by_agent.items():
        job_kinds = _run_job_kinds(rows)
        for row in rows:
            run = row.get("run")
            ticket = row.get("work")
            bound_at = _timestamp(row.get("ts"))
            if (
                row.get("phase") != "bind"
                or row.get("do") != "ticket"
                or not isinstance(run, str)
                or not isinstance(ticket, str)
                or bound_at is None
            ):
                continue
            bindings_by_ticket.setdefault(ticket, []).append({
                "run": run,
                "agent": agent,
                "job": job_kinds.get(run, "implement"),
                "at": bound_at,
            })

    grouped: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {}
    observed = False
    for ticket, bindings in bindings_by_ticket.items():
        outcome = outcomes_by_ticket.get(ticket)
        prs = outcome.get("prs") if isinstance(outcome, Mapping) else None
        if not isinstance(prs, list):
            continue
        bindings.sort(key=lambda row: row["at"])
        number_text = ticket.rsplit("#", 1)[-1]
        number = int(number_text) if number_text.isdigit() else None
        for pr in prs:
            if not isinstance(pr, Mapping):
                continue
            branch = pr.get("head_ref_name") or pr.get("headRefName")
            created = _timestamp(pr.get("created_at") or pr.get("createdAt"))
            merged = _timestamp(pr.get("merged_at") or pr.get("mergedAt"))
            if not isinstance(branch, str) and (
                (created is not None and start <= created < end)
                or (merged is not None and start <= merged < end)
            ):
                return None, "ticket PR in this hour lacks its branch name"
            if number is None or branch != "ticket/{}".format(number):
                continue
            if created is None:
                continue
            eligible = [binding for binding in bindings if binding["at"] <= created]
            if not eligible:
                continue
            binding = eligible[-1]
            agent = str(binding["agent"])
            job = str(binding["job"])
            bucket = grouped.setdefault(agent, {}).setdefault(job, {
                "claim_to_pr": {"sum_seconds": 0.0, "count": 0},
                "pr_to_merge": {"sum_seconds": 0.0, "count": 0},
            })
            if start <= created < end:
                bucket["claim_to_pr"]["sum_seconds"] += (
                    created - binding["at"]
                ).total_seconds()
                bucket["claim_to_pr"]["count"] += 1
                observed = True
            if merged is not None and start <= merged < end and merged >= created:
                bucket["pr_to_merge"]["sum_seconds"] += (
                    merged - created
                ).total_seconds()
                bucket["pr_to_merge"]["count"] += 1
                observed = True
    if not observed:
        return None, "no claim-to-PR or PR-to-merge observations in this hour"
    for agents in grouped.values():
        for jobs in agents.values():
            for pair in jobs.values():
                if pair["count"]:
                    pair["sum_seconds"] = round(pair["sum_seconds"], 3)
                else:
                    pair["sum_seconds"] = None
                    pair["gap"] = "no observation in this UTC hour"
    return {"by_agent_and_job": grouped}, None


def derive_row(
    snapshot: Mapping[str, object],
    ledgers: Optional[Mapping[str, Optional[Sequence[Mapping[str, object]]]]],
    usage_readings: Optional[Mapping[str, object]],
    outcome_records: Optional[Sequence[Mapping[str, object]]],
    now: Optional[datetime] = None,
    commit_activity: Optional[Mapping[str, object]] = None,
    funnel_line_count: Optional[int] = None,
) -> Dict:
    """Build a JSON-safe UTC-hour observation from fixture or live inputs."""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    start, end, hour = _interval(observed_at)
    brief, wrapper = _split_snapshot(snapshot)
    snapshot_at = _timestamp(
        wrapper.get("generated_at") or brief.get("generated_at")
    )
    captured_at = _iso(snapshot_at) if snapshot_at is not None else None
    outcomes_prs, merged_pr_gap, review_pr_gap = _outcome_prs(
        outcome_records, start, end
    )
    rows_by_agent, heartbeat_gap = _finish_rows(ledgers)
    finishes = _all_finishes(rows_by_agent)
    in_hour_finishes = [
        row for row in finishes
        if _in_interval(row.get("ts"), start, end) is not None
    ]
    metrics: Dict[str, Dict] = {key: {} for key in "ABCDEF"}

    # A — Output
    if outcomes_prs is None or merged_pr_gap:
        metrics["A"]["A1"] = _fact(
            None, "outcomes.jsonl.prs", merged_pr_gap or "outcome records unavailable"
        )
    else:
        by_repo: Dict[str, int] = {}
        for row in outcomes_prs:
            if row["in_hour"]["merged_at"]:
                by_repo[row["repo"]] = by_repo.get(row["repo"], 0) + 1
        metrics["A"]["A1"] = {
            "value": {"total": sum(by_repo.values()), "by_repo": dict(sorted(by_repo.items()))},
            "source": "outcomes.jsonl PRs; merged_at within this UTC hour; head_ref_name=ticket/<n>",
        }
    cc_share, cc_gap = _section(brief, "command_center_ticket_pr_share", "brief.command_center_ticket_pr_share")
    if cc_share is None or cc_share.get("available") is not True:
        metrics["A"]["A2"] = _rate_pair(
            None, None, "brief.command_center_ticket_pr_share",
            (str(cc_share.get("reason")) if cc_share and cc_share.get("reason") else None)
            or cc_gap or "share is unavailable",
        )
    else:
        metrics["A"]["A2"] = _rate_pair(
            cc_share.get("ticket_merged_prs"), cc_share.get("merged_prs"),
            "brief.command_center_ticket_pr_share",
        )
    maintenance, maintenance_gap = _section(brief, "maintenance_load", "brief.maintenance_load")
    metrics["A"]["A3"] = _rate_pair(
        maintenance.get("upkeep_projects") if maintenance else None,
        maintenance.get("closed_in_window") if maintenance else None,
        "brief.maintenance_load.upkeep_projects/closed_in_window",
        maintenance_gap or "exact upkeep numerator is not present in this snapshot",
    )
    metrics["A"]["A4"] = {
        "new_projects_started": _count_timestamps(
            maintenance.get("new_started_at") if maintenance else None,
            start,
            end,
            "brief.maintenance_load.new_started_at",
            maintenance_gap or "new work start timestamps are unavailable",
        ),
        "days_since_last_new_started": _count(
            maintenance.get("days_since_anything_new_started") if maintenance else None,
            "brief.maintenance_load.days_since_anything_new_started",
            maintenance_gap or "maintenance_load is unavailable",
        ),
        "source": "brief.maintenance_load",
    }
    disposal, disposal_gap = _section(brief, "disposal", "brief.disposal")
    metrics["A"]["A5"] = {
        key: _count(
            disposal.get(key) if disposal else None,
            "brief.disposal.{}".format(key),
            disposal_gap or "disposal is unavailable",
        )
        for key in ("done", "parked")
    }
    metrics["A"]["A5"]["net_open_growth"] = _signed_count(
        disposal.get("net_open_growth") if disposal else None,
        "brief.disposal.net_open_growth",
        disposal_gap or "net open growth is unavailable",
    )
    reopens = [
        row for row in (outcome_records or [])
        if isinstance(row, Mapping)
        and _in_interval(row.get("reopened_at"), start, end) is not None
    ]
    reverts_by_repo = (
        commit_activity.get("reverts_by_repo")
        if isinstance(commit_activity, Mapping) else None
    )
    metrics["A"]["A6"] = {
        "reverts_by_repo": _fact(
            reverts_by_repo, "github.commits.main.revert_subjects",
            "member-repository commit activity is unavailable",
        ),
        "reopened_tickets": _count(
        len(reopens) if outcome_records else None,
            "outcomes.jsonl.reopened_at",
            "outcomes.jsonl is unavailable",
        ),
    }

    # B — Quality
    first_approvals = first_reviewed = 0
    first_review_problem = review_pr_gap
    for row in outcomes_prs or []:
        if not row["in_hour"]["first_reviewed_at"]:
            continue
        verdict = row.get("first_review_result")
        if verdict in ("approved", "rejected"):
            first_reviewed += 1
            first_approvals += int(verdict == "approved")
        else:
            first_review_problem = "a first verdict in this hour is missing or unrecognized"
    first_review_available = outcomes_prs is not None and first_review_problem is None
    metrics["B"]["B1"] = _rate_pair(
        first_approvals if first_review_available else None,
        first_reviewed if first_review_available else None,
        "outcomes.jsonl.prs.first_review_result/first_reviewed_at",
        first_review_problem or "first verdict evidence is unavailable",
    )
    signal, signal_gap = _section(brief, "outcome_signals", "brief.outcome_signals")
    rework = None
    if signal:
        signals = signal.get("signals")
        rework = signals.get("rework_rate") if isinstance(signals, Mapping) else None
    metrics["B"]["B2"] = _rate_pair(
        rework.get("rework_attempts") if isinstance(rework, Mapping) else None,
        rework.get("merged_prs") if isinstance(rework, Mapping) else None,
        "brief.outcome_signals.signals.rework_rate.rework_attempts/merged_prs",
        signal_gap or ((str(rework.get("reason")) if rework.get("reason") else None)
                       if isinstance(rework, Mapping) else None)
        or "rework signal unavailable",
    )
    causes, causes_gap = _section(brief, "recorded_cause_regressions", "brief.recorded_cause_regressions")
    metrics["B"]["B3"] = _rate_pair(
        causes.get("with_recorded_cause") if causes else None,
        causes.get("broken_projects") if causes else None,
        "brief.recorded_cause_regressions.with_recorded_cause/broken_projects",
        causes_gap or "recorded-cause signal unavailable",
    )
    main_ci = brief.get("main_ci")
    main_ci_problem = _section_gap(brief, "main_ci")
    if not isinstance(main_ci, list) or main_ci_problem:
        metrics["B"]["B4"] = _fact(
            None, "brief.main_ci", main_ci_problem or "main_ci is unavailable"
        )
    else:
        metrics["B"]["B4"] = {
            "value": {
                "infra": sum(1 for row in main_ci if isinstance(row, Mapping) and row.get("verdict") == "infra"),
                "real": sum(1 for row in main_ci if isinstance(row, Mapping) and row.get("verdict") == "real"),
                "current_red_mains": len(main_ci),
                "by_repo": [
                    {"repo": row.get("repo"), "sha": row.get("sha"), "verdict": row.get("verdict")}
                    for row in main_ci if isinstance(row, Mapping)
                ],
            },
            "source": "brief.main_ci (current state; not an incident history)",
        }

    # C — Runs.  These are event counts from the hour, grouped by lane inputs.
    if rows_by_agent is None or heartbeat_gap:
        metrics["C"]["C1"] = _fact(None, "heartbeat ledgers finish rows", heartbeat_gap or "ledgers unavailable")
        metrics["C"]["C2"] = _fact(None, "heartbeat ledgers finish rows", heartbeat_gap or "ledgers unavailable")
        metrics["C"]["C3"] = _fact(None, "heartbeat ledgers finish rows", heartbeat_gap or "ledgers unavailable")
        metrics["C"]["C4"] = _fact(
            None, "heartbeat.finish.error_class",
            heartbeat_gap or "error_class is not recorded yet",
        )
        metrics["C"]["C5"] = _fact(None, "heartbeat ledgers finish rows", heartbeat_gap or "ledgers unavailable")
    else:
        grouped: Dict[Tuple[str, str], Dict[str, int]] = {
            (agent, kind): {
                name: 0 for name in FINISH_OUTCOMES + ("other", "finishes")
            }
            for agent in AGENTS
            for kind in ("implement", "review", "shape", "breakdown", "unknown")
        }
        by_outcome: Dict[str, Dict[str, int]] = {
            agent: {name: 0 for name in FINISH_OUTCOMES + ("other",)}
            for agent in AGENTS
        }
        error_classes: Dict[str, Dict[str, int]] = {}
        error_classes_by_job: Dict[str, Dict[str, Dict[str, int]]] = {}
        error_class_gap = not in_hour_finishes
        claim_losses: Dict[str, Dict[str, int]] = {
            agent: {
                "reconciled_claims": 0, "wall_clock_kills": 0, "re_begins": 0,
            }
            for agent in AGENTS
        }
        claim_losses_by_job: Dict[str, Dict[str, Dict[str, int]]] = {}
        for row in in_hour_finishes:
            agent = str(row.get("agent") or "unknown")
            run = row.get("run")
            kind = "unknown"
            if isinstance(run, str):
                kind = _run_job_kinds(rows_by_agent.get(agent, ())).get(run, "unknown")
            outcome = str(row.get("outcome") or "unknown")
            bucket = grouped.setdefault(
                (agent, kind), {name: 0 for name in FINISH_OUTCOMES + ("finishes",)}
            )
            bucket["finishes"] += 1
            if outcome in bucket:
                bucket[outcome] += 1
            else:
                bucket["other"] += 1
            outcome_key = outcome if outcome in FINISH_OUTCOMES else "other"
            by_outcome[agent][outcome_key] += 1
            if outcome == "errored":
                error_class = row.get("error_class")
                if error_class not in ("floor", "regression"):
                    error_class = "unclassified"
                    error_class_gap = True
                error_classes.setdefault(agent, {})[error_class] = error_classes.setdefault(agent, {}).get(error_class, 0) + 1
                class_bucket = error_classes_by_job.setdefault(agent, {}).setdefault(
                    kind, {"floor": 0, "regression": 0, "unclassified": 0}
                )
                class_bucket[error_class] += 1
            losses = claim_losses.setdefault(agent, {
                "reconciled_claims": 0, "wall_clock_kills": 0, "re_begins": 0,
            })
            job_losses = claim_losses_by_job.setdefault(agent, {}).setdefault(
                kind, {"reconciled_claims": 0, "wall_clock_kills": 0, "re_begins": 0}
            )
            losses["reconciled_claims"] += int(bool(row.get("reconciled_claim")))
            job_losses["reconciled_claims"] += int(bool(row.get("reconciled_claim")))
            note = str(row.get("note") or "").lower()
            wall_clock_kill = int(
                outcome == "errored" and bool(re.search(
                    r"wall[- ]clock|killed after \d+ minutes|timed out after \d+ minutes",
                    note,
                ))
            )
            losses["wall_clock_kills"] += wall_clock_kill
            job_losses["wall_clock_kills"] += wall_clock_kill
            try:
                import heartbeat
                rebegin = int(heartbeat.is_rebegin_finish(dict(row)))
            except Exception:
                rebegin = int(bool(row.get("re_begun_by")))
            losses["re_begins"] += rebegin
            job_losses["re_begins"] += rebegin
        by_agent_and_job = {}
        for (agent, kind), values in sorted(grouped.items()):
            by_agent_and_job.setdefault(agent, {})[kind] = {
                name: values[name]
                for name in FINISH_OUTCOMES + ("other", "finishes")
            }
        metrics["C"]["C1"] = {
            "value": {
                "by_agent": {
                    agent: dict(sorted(values.items()))
                    for agent, values in sorted(by_outcome.items())
                },
                "by_agent_and_job": by_agent_and_job,
            },
            "source": "heartbeat ledgers finish.outcome within this UTC hour",
        }
        productive = _rate_map(grouped, "done", ("finishes",), "heartbeat finish.outcome")
        empty = _rate_map(grouped, "nothing-to-do", ("finishes",), "heartbeat finish.outcome")
        metrics["C"]["C2"] = {"productive_share": productive, "empty_fire_share": empty}
        engaged = _rate_map(grouped, "errored", ("done", "errored"), "heartbeat finish.outcome")
        metrics["C"]["C3"] = {"error_rate_by_agent_and_job": engaged}
        metrics["C"]["C4"] = {
            "value": {
                "by_agent": error_classes,
                "by_agent_and_job": error_classes_by_job,
            },
            "source": "heartbeat.finish.error_class",
            **({"gap": "one or more errors lack a recognized error_class"}
               if error_class_gap else {}),
        }
        metrics["C"]["C5"] = {
            "value": {
                "by_agent": claim_losses,
                "by_agent_and_job": claim_losses_by_job,
            },
            "source": "heartbeat.finish.reconciled_claim, re_begun_by, and note",
        }
    if heartbeat_gap:
        latency, latency_gap = None, heartbeat_gap
    else:
        latency, latency_gap = _latency_sums(rows_by_agent, outcome_records, start, end)
    metrics["C"]["C6"] = _fact(
        latency, "heartbeat.bind.ts and outcomes.jsonl.prs.created_at/merged_at by agent and job",
        latency_gap or "no claim-to-PR or PR-to-merge observations in this hour",
    )

    # D — Budget. Usage snapshots are gauges; token and API rates remain raw pairs.
    readings = usage_readings or {}
    muse = readings.get("muse") if isinstance(readings, Mapping) else None
    muse_week = _usage_window(muse, "seven_day")
    pace_band = muse.get("pace_band") if isinstance(muse, Mapping) else None
    if pace_band is None and isinstance(muse, Mapping):
        try:
            import usage
            pace_band = usage.pace(
                dict(muse), observed_at.timestamp(), provider=usage.provider_of("muse")
            ).get("band")
        except Exception:
            pace_band = None
    metrics["D"]["D1"] = {
        "dollars_per_day": _count(
            muse_week.get("daily_rate_dollars") if muse_week else None,
            "usage.py read_muse windows.seven_day.daily_rate_dollars",
            "Muse usage reading is unavailable",
        ),
        "window_used_percent": _count(
            muse_week.get("used_percent") if muse_week else None,
            "usage.py read_muse windows.seven_day.used_percent",
            "Muse usage reading is unavailable",
        ),
        "pace_band": _fact(
            pace_band,
            "usage.py read_muse windows.seven_day",
            "Muse pace band is not part of the usage reading",
        ),
    }
    codex = readings.get("codex") if isinstance(readings, Mapping) else None
    codex_week = _usage_window(codex, "seven_day")
    thread_reading = (
        readings.get("codex_thread_source_usage")
        if isinstance(readings, Mapping) else None
    )
    thread_totals = (
        thread_reading.get("value")
        if isinstance(thread_reading, Mapping) else None
    )
    thread_totals = thread_totals if isinstance(thread_totals, Mapping) else None
    thread_gap = (
        str(thread_reading.get("gap"))
        if isinstance(thread_reading, Mapping) and thread_reading.get("gap")
        else "Codex session source data is unavailable"
    )
    thread_source = (
        str(thread_reading.get("source"))
        if isinstance(thread_reading, Mapping) and thread_reading.get("source")
        else "~/.codex/sessions session_meta.thread_source and token_usage_record"
    )
    metrics["D"]["D2"] = {
        "weekly_used_percent": _count(
            codex_week.get("used_percent") if codex_week else None,
            "usage.py codex windows.seven_day.used_percent",
            "Codex usage reading is unavailable",
        ),
        "funnel_vs_personal": {
            "funnel_tokens": _count(
                thread_totals.get("funnel_tokens") if thread_totals else None,
                thread_source + ".automation.total_tokens",
                thread_gap,
            ),
            "personal_tokens": _count(
                thread_totals.get("personal_tokens") if thread_totals else None,
                thread_source + ".other_thread_sources.total_tokens",
                thread_gap,
            ),
            "total_tokens": _count(
                thread_totals.get("total_tokens") if thread_totals else None,
                thread_source + ".total_tokens",
                thread_gap,
            ),
            "funnel_share": _rate_pair(
                thread_totals.get("funnel_tokens") if thread_totals else None,
                thread_totals.get("total_tokens") if thread_totals else None,
                thread_source + ".automation/total_tokens",
                thread_gap,
            ),
            "personal_share": _rate_pair(
                thread_totals.get("personal_tokens") if thread_totals else None,
                thread_totals.get("total_tokens") if thread_totals else None,
                thread_source + ".other_thread_sources/total_tokens",
                thread_gap,
            ),
            "source": thread_source,
            **({"gap": thread_gap} if thread_totals is None else {}),
        },
    }
    claude = readings.get("claude") if isinstance(readings, Mapping) else None
    claude_windows = {}
    if isinstance(claude, Mapping) and isinstance(claude.get("windows"), Mapping):
        for name, window in claude["windows"].items():
            if isinstance(window, Mapping):
                claude_windows[name] = {
                    "used_percent": _count(
                        window.get("used_percent"),
                        "usage.py claude windows.{}.used_percent".format(name),
                        "Claude usage percentage is unavailable",
                    ),
                    "resets_at": _fact(
                        window.get("resets_at"),
                        "usage.py claude windows.{}.resets_at".format(name),
                        "Claude reset time is unavailable",
                    ),
                }
    metrics["D"]["D3"] = _fact(
        claude_windows if claude_windows else None,
        "usage.py claude windows",
        "Claude usage reading is unavailable",
    )
    cost = None
    if signal:
        signals = signal.get("signals")
        cost = signals.get("cost_per_merged_pr") if isinstance(signals, Mapping) else None
    cost_lanes = []
    if isinstance(cost, Mapping) and isinstance(cost.get("by_lane"), list):
        for lane in cost["by_lane"]:
            if not isinstance(lane, Mapping):
                continue
            cost_lanes.append({
                "lane": lane.get("lane"), "unit": lane.get("unit"),
                **_rate_pair(
                    lane.get("total_cost"), lane.get("merged_prs"),
                    "brief.outcome_signals.signals.cost_per_merged_pr.by_lane",
                    str(cost.get("reason") or "cost numerator or denominator is unavailable")
                    if isinstance(cost, Mapping) else "cost source is unavailable",
                ),
            })
    metrics["D"]["D4"] = _fact(
        cost_lanes if cost_lanes else None,
        "brief.outcome_signals.signals.cost_per_merged_pr.by_lane",
        str(cost.get("reason") if isinstance(cost, Mapping) else signal_gap or "cost join is unavailable"),
    )
    api_by_agent: Dict[str, Dict] = {}
    resend_by_agent: Dict[str, Dict] = {}
    for agent in AGENTS:
        agent_finishes = [
            row for row in in_hour_finishes if row.get("agent") == agent
        ]
        points = calls = total_input = fresh_input = 0
        points_complete = calls_complete = bool(agent_finishes)
        input_complete = bool(agent_finishes)
        by_run = {}
        for index, row in enumerate(agent_finishes, 1):
            run = row.get("run")
            run_id = run if isinstance(run, str) and run else "unattributed-{}".format(index)
            api = row.get("api_cost")
            point_value = api.get("graphql_points") if isinstance(api, Mapping) else None
            call_value = api.get("gh_calls") if isinstance(api, Mapping) else None
            by_run[run_id] = {
                "graphql_points": _count(
                    point_value,
                    "heartbeat.finish.api_cost.graphql_points",
                    "this run's GraphQL point count is unavailable",
                ),
                "gh_calls": _count(
                    call_value,
                    "heartbeat.finish.api_cost.gh_calls",
                    "this run's gh call count is unavailable",
                ),
            }
            if isinstance(point_value, int) and not isinstance(point_value, bool) and point_value >= 0:
                points += point_value
            else:
                points_complete = False
            if isinstance(call_value, int) and not isinstance(call_value, bool) and call_value >= 0:
                calls += call_value
            else:
                calls_complete = False
            usage = row.get("input_usage")
            if isinstance(usage, Mapping):
                total = usage.get("total_input_tokens")
                fresh = usage.get("fresh_input_tokens")
                if (
                    isinstance(total, int) and not isinstance(total, bool) and total >= 0
                    and isinstance(fresh, int) and not isinstance(fresh, bool)
                    and 0 <= fresh <= total
                ):
                    total_input += total
                    fresh_input += fresh
                else:
                    input_complete = False
            else:
                input_complete = False
        api_by_agent[agent] = {
            "graphql_points": _count(
                points if points_complete else None,
                "heartbeat.finish.api_cost.graphql_points",
                "one or more run point counts are unavailable",
            ),
            "gh_calls": _count(
                calls if calls_complete else None,
                "heartbeat.finish.api_cost.gh_calls",
                "one or more run call counts are unavailable",
            ),
            "by_run": by_run,
        }
        resend_by_agent[agent] = _pair(
            total_input if input_complete else None,
            fresh_input if input_complete else None,
            "heartbeat.finish.input_usage.total_input_tokens/fresh_input_tokens",
            "one or more run input-usage readings are unavailable",
        )
    metrics["D"]["D5"] = {
        "graphql_points_per_run": {
            "value": api_by_agent if rows_by_agent is not None else None,
            "source": "heartbeat.finish.api_cost.graphql_points/gh_calls",
            **({"gap": heartbeat_gap} if heartbeat_gap else {}),
        },
        "points_per_brief": _count(
            (brief.get("api_cost") or {}).get("graphql_points")
            if isinstance(brief.get("api_cost"), Mapping) else None,
            "brief.api_cost.graphql_points",
            "the brief did not record a complete GraphQL point total",
        ),
        "gh_calls_per_brief": _count(
            (brief.get("api_cost") or {}).get("gh_calls")
            if isinstance(brief.get("api_cost"), Mapping) else None,
            "brief.api_cost.gh_calls",
            "the brief did not record a gh call total",
        ),
        "api_reserve_skips": _count(
            sum(1 for row in in_hour_finishes if row.get("outcome") == "skipped-api-reserve")
            if rows_by_agent is not None else None,
            "heartbeat.finish.outcome=skipped-api-reserve",
            heartbeat_gap or "heartbeat ledgers are unavailable",
        ),
        "resend_ratio_by_agent": resend_by_agent if rows_by_agent is not None else _fact(
            None, "heartbeat.finish.input_usage", heartbeat_gap or "heartbeat ledgers are unavailable"
        ),
    }
    if heartbeat_gap:
        metrics["D"]["D5"]["graphql_points_per_run"] = _fact(
            None, "heartbeat.finish.api_cost", heartbeat_gap
        )
        metrics["D"]["D5"]["api_reserve_skips"] = _count(
            None, "heartbeat.finish.outcome=skipped-api-reserve", heartbeat_gap
        )
        metrics["D"]["D5"]["resend_ratio_by_agent"] = _fact(
            None, "heartbeat.finish.input_usage", heartbeat_gap
        )
    held = _hold_seconds(finishes, start, end, observed_at)
    metrics["D"]["D6"] = {
        "held_hours_by_agent_and_reason": {
            agent: {
                reason: _count(
                    seconds / 3600.0 if not heartbeat_gap else None,
                    "heartbeat finish timestamps; held interval until next finish",
                    heartbeat_gap or "held interval is unavailable",
                )
                for reason, seconds in values.items()
            }
            for agent, values in held.items()
        },
        **({"gap": heartbeat_gap} if heartbeat_gap else {}),
    }

    # E — Attention
    board = wrapper.get("board") if isinstance(wrapper, Mapping) else None
    if not isinstance(board, Mapping):
        board = snapshot.get("board")
    columns = board.get("columns") if isinstance(board, Mapping) else None
    gate_dwell = None
    if isinstance(columns, list):
        gate_dwell = {}
        seen_gates = set()
        for column in columns:
            if not isinstance(column, Mapping) or column.get("stage") not in ("Shaped", "Ready"):
                continue
            items = column.get("items")
            if not isinstance(items, list):
                gate_dwell = None
                break
            seen_gates.add(column["stage"])
            waited_seconds = [
                row.get("waited_seconds") if isinstance(row, Mapping) else None
                for row in items
            ]
            gate_dwell[column["stage"]] = _sum_count(
                waited_seconds,
                "snapshot.board.columns.items.waited_seconds.{}".format(column["stage"]),
                "gate wait seconds are missing or no items were observed",
            )
        if gate_dwell is not None and seen_gates != {"Shaped", "Ready"}:
            gate_dwell = None
    metrics["E"]["E1"] = {
        "total_needing_nate": _count(
            brief.get("total_needing_nate"), "brief.total_needing_nate",
            "brief.total_needing_nate is unavailable",
        ),
        "by_gate": _fact(
            brief.get("counts_by_gate"), "brief.counts_by_gate",
            "gate counts are unavailable",
        ),
        "gate_dwell": _fact(
            gate_dwell, "snapshot.board.columns.items.waited for Shaped and Ready",
            "the brief snapshot does not include numeric gate dwell values",
        ),
    }
    human_steps = brief.get("human_steps")
    blocked_human_steps = brief.get("blocked_human_steps")
    human_steps_gap = (
        _section_gap(brief, "human_steps")
        or _section_gap(brief, "blocked_human_steps")
    )
    metrics["E"]["E2"] = {
        "outstanding": _count(
            (len(human_steps) + len(blocked_human_steps))
            if (
                isinstance(human_steps, list)
                and isinstance(blocked_human_steps, list)
                and human_steps_gap is None
            )
            else None,
            "brief.human_steps and brief.blocked_human_steps",
            human_steps_gap or "human-step lists are unavailable",
        ),
        "opened_this_hour": _count(
            None,
            "brief.human_steps and brief.blocked_human_steps",
            human_steps_gap or "the brief lists outstanding steps but has no complete opening history",
        ),
    }
    approvals = brief.get("unattended_approvals")
    merges = brief.get("unattended_merges")
    approvals_gap = _section_gap(brief, "unattended_approvals")
    merges_gap = _section_gap(brief, "unattended_merges")
    metrics["E"]["E3"] = {
        "approvals_this_hour": _count(
            _count_in_hour(approvals, start, end, ("at", "created_at", "createdAt", "approved_at"))
            if isinstance(approvals, list) and approvals_gap is None else None,
            "brief.unattended_approvals timestamps",
            approvals_gap or "approval timestamps are unavailable",
        ),
        "merges_this_hour": _count(
            _count_in_hour(merges, start, end, ("at", "merged_at", "mergedAt"))
            if isinstance(merges, list) and merges_gap is None else None,
            "brief.unattended_merges timestamps",
            merges_gap or "merge timestamps are unavailable",
        ),
    }
    watch_actions = 0
    claude_available = rows_by_agent is not None and "claude" in rows_by_agent
    if claude_available:
        for row in in_hour_finishes:
            note = str(row.get("note") or "").lower()
            if row.get("agent") == "claude" and re.search(r"check[- ]in\s+#?684", note) and re.search(r"unwedge|override|reconcil", note):
                watch_actions += 1
    metrics["E"]["E4"] = _count(
        watch_actions if claude_available else None,
        "claude heartbeat finish notes for check-in #684 actions",
        "Claude heartbeat ledger is unavailable" if not claude_available else "no matching check-in action",
    )
    metrics["E"]["E5"] = {
        "stranded": _count(
            len(brief["stranded"]) if isinstance(brief.get("stranded"), list) else None,
            "brief.stranded", _section_gap(brief, "stranded") or "stranded is unavailable",
        ),
        "degraded_sections": _count(
            len(brief["degraded"]) if isinstance(brief.get("degraded"), list) else None,
            "brief.degraded", "degraded is unavailable",
        ),
        "status_state_mismatches": _count(
            len(brief["status_state_mismatches"])
            if isinstance(brief.get("status_state_mismatches"), list) else None,
            "brief.status_state_mismatches",
            _section_gap(brief, "status_state_mismatches") or "status mismatch data is unavailable",
        ),
        "stale_locks_taken_over": _count(
            len(brief["stale_locks_taken_over"])
            if isinstance(brief.get("stale_locks_taken_over"), list) else None,
            "brief.stale_locks_taken_over",
            _section_gap(brief, "stale_locks_taken_over") or "stale lock data is unavailable",
        ),
    }

    # F — Churn
    commits = commit_activity.get("commits_by_repo") if isinstance(commit_activity, Mapping) else None
    metrics["F"]["F1"] = _fact(
        commits, "GitHub commits on member-repository main branches in this UTC hour",
        "member-repository commit activity is unavailable",
    )
    metrics["F"]["F2"] = _count(
        funnel_line_count, "git show origin/main:funnel.py line count",
        "funnel.py on origin/main is unavailable",
    )
    timings, timings_gap = _section(brief, "timings", "brief.timings")
    degraded = brief.get("degraded")
    metrics["F"]["F3"] = {
        "project_load_seconds": _count(
            timings.get("project_load") if timings else None,
            "brief.timings.project_load", timings_gap or "project_load timing unavailable",
        ),
        "degraded_sections": _count(
            len(degraded) if isinstance(degraded, list) else None,
            "brief.degraded", "degraded section list unavailable",
        ),
    }

    return {
        "schema_version": 1,
        "hour": hour,
        "captured_at": captured_at,
        "derived_at": _iso(observed_at),
        "metrics": metrics,
    }


def _decode_rows(content: str) -> List[Dict]:
    rows = []
    seen = set()
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise MetricsError("{} line {} is invalid JSON".format(METRICS_PATH, line_number)) from exc
        if not isinstance(row, Mapping) or not isinstance(row.get("hour"), str):
            raise MetricsError("{} line {} is not an hourly metrics row".format(METRICS_PATH, line_number))
        if row["hour"] in seen:
            raise MetricsError("{} contains duplicate hour {}".format(METRICS_PATH, row["hour"]))
        seen.add(row["hour"])
        rows.append(dict(row))
    return rows


def _encode_rows(rows: Iterable[Mapping[str, object]]) -> str:
    return "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows)


def append_rows(
    existing: Sequence[Mapping[str, object]], row: Mapping[str, object]
) -> Tuple[List[Dict], int]:
    """Pure append policy used by the remote writer and fixture tests."""
    hour = row.get("hour")
    if not isinstance(hour, str):
        raise MetricsError("cannot append a row without its UTC hour")
    found = [dict(value) for value in existing]
    if any(value.get("hour") == hour for value in found):
        return found, 0
    found.append(dict(row))
    return found, 1


def _gh(args: Sequence[str], stdin: Optional[str] = None):
    try:
        import funnel
        kwargs = {"capture_output": True, "text": True}
        if stdin is not None:
            kwargs["input"] = stdin
        return funnel._run_gh(["gh"] + list(args), **kwargs)
    except Exception as exc:
        raise MetricsError("could not run GitHub CLI: {}".format(exc)) from exc


def _read_remote(repo: str = REPO, branch: str = BRANCH) -> Tuple[List[Dict], Optional[str]]:
    endpoint = "repos/{}/contents/{}?ref={}".format(repo, METRICS_PATH, branch)
    result = _gh(["api", endpoint])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").lower()
        if "404" in detail or "not found" in detail:
            return [], None
        raise MetricsError(result.stderr.strip() or "could not read {}".format(METRICS_PATH))
    try:
        payload = json.loads(result.stdout)
        content = base64.b64decode(payload.get("content") or "").decode("utf-8")
        sha, size = payload.get("sha"), payload.get("size")
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise MetricsError("invalid remote {} response".format(METRICS_PATH)) from exc
    if not content and isinstance(size, int) and size > 0:
        if not isinstance(sha, str) or not sha:
            raise MetricsError("remote metrics has a size but no blob sha")
        blob = _gh(["api", "repos/{}/git/blobs/{}".format(repo, sha)])
        if blob.returncode != 0:
            raise MetricsError(blob.stderr.strip() or "could not read metrics blob")
        try:
            content = base64.b64decode(json.loads(blob.stdout).get("content") or "").decode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise MetricsError("invalid metrics blob response") from exc
        if not content:
            raise MetricsError("remote metrics blob unexpectedly read empty")
    return _decode_rows(content), sha


def append_remote(row: Mapping[str, object], repo: str = REPO, branch: str = BRANCH) -> int:
    """Append by Contents API compare-and-swap, preserving unknown remote rows."""
    for attempt in range(len(STORE_BACKOFF) + 1):
        existing, sha = _read_remote(repo, branch)
        combined, appended = append_rows(existing, row)
        if not appended:
            return 0
        payload = {
            "message": "metrics: +1 hourly row ({})".format(row["hour"]),
            "branch": branch,
            "content": base64.b64encode(_encode_rows(combined).encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        result = _gh([
            "api", "-X", "PUT", "repos/{}/contents/{}".format(repo, METRICS_PATH),
            "--input", "-",
        ], stdin=json.dumps(payload))
        if result.returncode == 0:
            return 1
        detail = (result.stderr or result.stdout or "").lower()
        if "409" not in detail and "sha" not in detail and "conflict" not in detail:
            raise MetricsError(result.stderr.strip() or "could not write {}".format(METRICS_PATH))
        if attempt >= len(STORE_BACKOFF):
            raise MetricsError("could not append {} after compare-and-swap retries".format(METRICS_PATH))
        time.sleep(STORE_BACKOFF[attempt])
    return 0


def _read_snapshot(path: Optional[str]) -> Mapping[str, object]:
    if path:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MetricsError("could not read snapshot {}: {}".format(path, exc)) from exc
    else:
        command = [sys.executable, str(Path(__file__).resolve().parent / "funnel.py"), "snapshot"]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise MetricsError(result.stderr.strip() or "could not read newest brief snapshot")
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise MetricsError("newest brief snapshot is invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise MetricsError("snapshot root must be a JSON object")
    return value


def _read_jsonl(path: str) -> List[Dict]:
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MetricsError("could not read {}: {}".format(path, exc)) from exc
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise MetricsError("{} line {} is invalid JSON".format(path, line_number)) from exc
        if not isinstance(row, Mapping):
            raise MetricsError("{} line {} is not an object".format(path, line_number))
        rows.append(dict(row))
    return rows


def _live_inputs(now: datetime) -> Tuple[Dict, Dict, List, Dict, Optional[int]]:
    import heartbeat
    import outcomes
    import usage

    ledgers = {}
    for agent in AGENTS:
        try:
            rows = heartbeat.read(agent)
        except Exception:
            rows = None
        # An absent or empty ledger is unknown evidence, never a measured zero.
        ledgers[agent] = rows if rows else None
    usage_readings = {
        agent: _safe_usage_read(usage, agent, now.timestamp())
        for agent in ("muse", "claude", "codex")
    }
    thread_totals, thread_gap = read_codex_thread_source_usage(
        now, usage_readings.get("codex")
    )
    usage_readings["codex_thread_source_usage"] = {
        "value": thread_totals,
        "source": "~/.codex/sessions session_meta.thread_source and token_usage_record",
        **({"gap": thread_gap} if thread_gap else {}),
    }
    try:
        outcome_records = outcomes.read_records()
    except Exception:
        outcome_records = None
    try:
        commit_activity = read_commit_activity(now)
    except Exception:
        commit_activity = None
    line_count = read_funnel_line_count()
    return ledgers, usage_readings, outcome_records, commit_activity, line_count


def _safe_usage_read(usage_module, agent: str, now: float) -> object:
    try:
        return usage_module.read_agent(agent, now)
    except Exception:
        return None


def read_commit_activity(now: datetime) -> Dict:
    """Count commits and Revert subjects on opted-in repositories' main."""
    import funnel

    start, end, _ = _interval(now)
    since = _iso(start)
    until = _iso(end)
    repos = sorted(set(funnel.member_repos()) | {funnel.REPO})
    commits_by_repo: Dict[str, int] = {}
    reverts_by_repo: Dict[str, int] = {}
    for repo in repos:
        page = 1
        rows_for_repo: List[Mapping[str, object]] = []
        while page <= 100:
            endpoint = (
                "repos/{}/commits?sha=main&since={}&until={}&per_page=100&page={}"
            ).format(repo, since, until, page)
            result = _gh(["api", endpoint])
            if result.returncode != 0:
                raise MetricsError(result.stderr.strip() or "could not read main commits for {}".format(repo))
            try:
                payload = json.loads(result.stdout)
            except ValueError as exc:
                raise MetricsError("invalid commit response for {}".format(repo)) from exc
            if not isinstance(payload, list):
                raise MetricsError("invalid commit list for {}".format(repo))
            rows_for_repo.extend(row for row in payload if isinstance(row, Mapping))
            if len(payload) < 100:
                break
            page += 1
        if page > 100:
            raise MetricsError("commit scan for {} exceeded 10,000 rows".format(repo))
        commits_by_repo[repo] = len(rows_for_repo)
        reverts = 0
        for row in rows_for_repo:
            commit = row.get("commit")
            message = commit.get("message") if isinstance(commit, Mapping) else ""
            if re.search(
                r"(?im)^revert\s+['\"]|^this reverts commit\s+[0-9a-f]{7,}",
                str(message or ""),
            ):
                reverts += 1
        reverts_by_repo[repo] = reverts
    return {"commits_by_repo": commits_by_repo, "reverts_by_repo": reverts_by_repo}


def read_funnel_line_count() -> Optional[int]:
    repo_dir = Path(__file__).resolve().parent
    result = subprocess.run(
        ["git", "show", "origin/main:funnel.py"], cwd=str(repo_dir),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return len(result.stdout.splitlines())


def _load_fixture_inputs(args) -> Tuple[Dict, Optional[Dict], Optional[List], Optional[Dict], Optional[int]]:
    ledgers: Dict[str, Optional[List[Dict]]] = {}
    for item in args.ledger or []:
        if "=" not in item:
            raise MetricsError("--ledger needs AGENT=PATH")
        agent, path = item.split("=", 1)
        if agent not in AGENTS:
            raise MetricsError("unknown ledger agent {}".format(agent))
        ledgers[agent] = _read_jsonl(path)
    for agent in AGENTS:
        ledgers.setdefault(agent, None)
    readings = None
    if args.usage:
        try:
            readings = json.loads(Path(args.usage).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MetricsError("could not read usage fixture: {}".format(exc)) from exc
    outcome_records = _read_jsonl(args.outcomes) if args.outcomes else None
    commit_activity = None
    if args.commits:
        try:
            commit_activity = json.loads(Path(args.commits).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MetricsError("could not read commit fixture: {}".format(exc)) from exc
    line_count = args.line_count
    return ledgers, readings, outcome_records, commit_activity, line_count


def _parse_now(value: Optional[str]) -> Optional[datetime]:
    return _timestamp(value) if value else None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Derive and append one hourly metrics row")
    sub = parser.add_subparsers(dest="command", required=True)
    derive = sub.add_parser("derive", help="derive one UTC-hour metrics row")
    derive.add_argument("--snapshot", help="fixture snapshot JSON; default reads the newest brief")
    derive.add_argument("--ledger", action="append", help="fixture heartbeat JSONL as AGENT=PATH")
    derive.add_argument("--outcomes", help="fixture outcomes.jsonl")
    derive.add_argument("--usage", help="fixture usage readings JSON")
    derive.add_argument("--commits", help="fixture commit activity JSON")
    derive.add_argument("--line-count", type=int, help="fixture funnel.py line count")
    derive.add_argument("--now", help="UTC hour timestamp, for fixture reproduction")
    derive.add_argument("--dry-run", action="store_true", help="print the row without appending")
    args = parser.parse_args(argv)
    if args.command != "derive":
        parser.error("unknown command")
    now = _parse_now(args.now) or datetime.now(timezone.utc)
    try:
        snapshot = _read_snapshot(args.snapshot)
        if args.snapshot or args.ledger or args.outcomes or args.usage or args.commits or args.line_count is not None:
            ledgers, readings, records, activity, line_count = _load_fixture_inputs(args)
        else:
            ledgers, readings, records, activity, line_count = _live_inputs(now)
        row = derive_row(snapshot, ledgers, readings, records, now, activity, line_count)
        if args.dry_run:
            print(json.dumps(row, indent=2, sort_keys=True))
            return 0
        appended = append_remote(row)
        print(json.dumps({"hour": row["hour"], "appended": appended}, sort_keys=True))
        return 0
    except Exception as exc:
        print("metrics: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised as a command
    raise SystemExit(main())
