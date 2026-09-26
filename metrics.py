#!/usr/bin/env python3
"""Derive and append one completed-hour execution-metrics row.

``metrics.jsonl`` is an append-only projection on the heartbeat branch. Rows
keep source facts (and numerator/denominator pairs for rates) so ``series`` can
roll up days without averaging rounded daily percentages.
Missing evidence stays ``null`` with a source and reason; an empty observation
is represented by a real zero only when the source was readable.

Each row has ``schema_version``, the start of its completed UTC hour, the time
it was derived, and metric groups ``A`` through ``F`` containing the plan's
stable ``A1``–``F3`` keys. Every leaf names its source; rates retain their raw
numerator and denominator. Outcome and brief inputs must be fresh through the
hour's end before in-hour counts can be recorded.
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


REPO = "nateprich-projects/command-center"
BRANCH = "heartbeat"
METRICS_PATH = "metrics.jsonl"
AGENTS = ("muse", "claude", "codex")
HOUR_SECONDS = 60 * 60
SERIES_DAYS = 90
STORE_BACKOFF = (1, 3, 7)
FINISH_OUTCOMES = (
    "done", "nothing-to-do", "errored", "skipped-over-pace",
    "skipped-api-reserve", "skipped-provider-quota",
)
HISTORY_AGENT_PATHS = {agent: "{}.jsonl".format(agent) for agent in AGENTS}
HISTORY_OVERLAP_GRACE = HOUR_SECONDS


class MetricsError(RuntimeError):
    """An input could not be read safely, or an append was unsafe."""


@dataclass(frozen=True)
class _HistoryCommit:
    sha: str
    committed_at: datetime


@dataclass(frozen=True)
class _HistorySample:
    hour_start: datetime
    commit: Optional[str]
    sampled_at: Optional[datetime]
    ledgers: Mapping[str, Optional[Sequence[Mapping[str, object]]]]
    snapshot: Optional[Mapping[str, object]] = None
    usage_readings: Optional[Mapping[str, object]] = None
    outcome_records: Optional[Sequence[Mapping[str, object]]] = None
    commit_activity: Optional[Mapping[str, object]] = None
    funnel_line_count: Optional[int] = None
    run_ids: frozenset = frozenset()


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


def _number(value: object) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
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
    """Keep signed count-like values such as net open growth intact."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"value": None, "source": source, "gap": reason}
    number = float(value)
    if not math.isfinite(number):
        return {"value": None, "source": source, "gap": reason}
    rendered = int(number) if number.is_integer() else number
    return {"value": rendered, "source": source}


def _sum_count(
    values: object,
    source: str,
    reason: str = "no observations are available",
) -> Dict:
    if not isinstance(values, list) or not values:
        return {"sum": None, "count": None, "source": source, "gap": reason}
    numbers = [_number(value) for value in values]
    if any(value is None for value in numbers):
        return {"sum": None, "count": None, "source": source, "gap": reason}
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
    end_epoch = int(current.timestamp()) // HOUR_SECONDS * HOUR_SECONDS
    end = datetime.fromtimestamp(end_epoch, tz=timezone.utc)
    start = end - timedelta(hours=1)
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


def _outcomes_freshness_gap(
    records: Optional[Sequence[Mapping[str, object]]],
    hour_end: datetime,
) -> Optional[str]:
    """Fail closed unless outcomes were derived after the measured hour."""
    if not records:
        return "outcomes.jsonl is unavailable or empty"
    derived_at = [
        parsed
        for row in records
        if isinstance(row, Mapping)
        for parsed in [_timestamp(row.get("derived_at"))]
        if parsed is not None
    ]
    if not derived_at:
        return "outcomes.jsonl has no parseable derived_at timestamp"
    if max(derived_at) < hour_end:
        return "outcomes.jsonl is stale; newest derived_at precedes this hour's end"
    return None


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
        seen = set()
        for row in rows:
            if row.get("phase") == "finish":
                item = dict(row, agent=agent)
                identity = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                if identity not in seen:
                    seen.add(identity)
                    found.append(item)
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
    hour_start: Optional[datetime] = None,
    derived_at: Optional[datetime] = None,
) -> Dict:
    """Build a JSON-safe UTC-hour observation from fixture or live inputs."""
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    recorded_at = observed_at
    if derived_at is not None:
        parsed_derived_at = _timestamp(derived_at)
        if parsed_derived_at is None:
            raise MetricsError("derived_at must be a parseable timestamp")
        recorded_at = parsed_derived_at
    if hour_start is None:
        start, end, hour = _interval(observed_at)
    else:
        start_at = _timestamp(hour_start)
        if start_at is None:
            raise MetricsError("hour_start must be a parseable timestamp")
        start = start_at.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)
        hour = _iso(start)
    brief, wrapper = _split_snapshot(snapshot)
    snapshot_at = _timestamp(
        wrapper.get("generated_at") or brief.get("generated_at")
    )
    captured_at = _iso(snapshot_at) if snapshot_at is not None else None
    snapshot_gap = (
        None
        if snapshot_at is not None and snapshot_at >= end
        else "brief snapshot does not cover the complete UTC hour"
    )
    outcomes_gap = _outcomes_freshness_gap(outcome_records, end)
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
    if outcomes_gap or outcomes_prs is None or merged_pr_gap:
        metrics["A"]["A1"] = _fact(
            None,
            "outcomes.jsonl.prs",
            outcomes_gap or merged_pr_gap or "outcome records unavailable",
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
            maintenance.get("new_started_at")
            if maintenance and snapshot_gap is None else None,
            start,
            end,
            "brief.maintenance_load.new_started_at",
            snapshot_gap or maintenance_gap or "new work start timestamps are unavailable",
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
        disposal_gap or "disposal is unavailable",
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
            len(reopens) if outcomes_gap is None else None,
            "outcomes.jsonl.reopened_at",
            outcomes_gap or "outcomes.jsonl is unavailable",
        ),
    }

    # B — Quality
    first_approvals = first_reviewed = 0
    first_review_problem = outcomes_gap or review_pr_gap
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
        metrics["C"]["C4"] = _fact(None, "heartbeat.finish.error_class", "error_class is not recorded yet")
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
                (agent, kind), {
                    name: 0 for name in FINISH_OUTCOMES + ("other", "finishes")
                }
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
                error_classes.setdefault(agent, {})[error_class] = error_classes.setdefault(agent, {}).get(error_class, 0) + 1
                error_class_bucket = error_classes_by_job.setdefault(agent, {}).setdefault(
                    kind, {"floor": 0, "regression": 0, "unclassified": 0}
                )
                error_class_bucket[error_class] += 1
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
            "gap": "error_class is not recorded yet; missing classes remain unclassified",
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
    elif outcomes_gap:
        latency, latency_gap = None, outcomes_gap
    else:
        latency, latency_gap = _latency_sums(rows_by_agent, outcome_records, start, end)
    metrics["C"]["C6"] = _fact(
        latency, "heartbeat.bind.ts and outcomes.jsonl.prs.created_at/merged_at by agent and job",
        latency_gap or "no claim-to-PR or PR-to-merge observations in this hour",
    )

    # D — Budget. Usage snapshots are gauges; rates retain raw numerator/denominator pairs.
    readings = usage_readings or {}
    muse = readings.get("muse") if isinstance(readings, Mapping) else None
    muse_week = _usage_window(muse, "seven_day")
    muse_reset_at = _timestamp(muse_week.get("resets_at") if muse_week else None)
    pace_band = muse.get("pace_band") if isinstance(muse, Mapping) else None
    muse_rate_days = None
    try:
        import usage
        muse_rate_days = usage.MUSE_RATE_LOOKBACK / 86400.0
    except Exception:
        pass
    if pace_band is None and isinstance(muse, Mapping):
        try:
            import usage
            pace_band = usage.pace(
                dict(muse), observed_at.timestamp(), provider=usage.provider_of("muse")
            ).get("band")
        except Exception:
            pace_band = None
    metrics["D"]["D1"] = {
        "dollars_per_day": _rate_pair(
            muse_week.get("trailing_72h_dollars") if muse_week else None,
            muse_rate_days,
            "usage.py read_muse windows.seven_day.trailing_72h_dollars / (MUSE_RATE_LOOKBACK / 86400)",
            "Muse trailing-spend amount or lookback window is unavailable",
        ),
        "window_used_percent": _count(
            muse_week.get("used_percent") if muse_week else None,
            "usage.py read_muse windows.seven_day.used_percent",
            "Muse usage reading is unavailable",
        ),
        "window_resets_at": _fact(
            _iso(muse_reset_at) if muse_reset_at else None,
            "usage.py read_muse windows.seven_day.resets_at",
            "Muse seven-day reset time is unavailable",
        ),
        "pace_band": _fact(
            pace_band,
            "usage.py read_muse windows.seven_day",
            "Muse pace band is not part of the usage reading",
        ),
    }
    codex = readings.get("codex") if isinstance(readings, Mapping) else None
    codex_week = _usage_window(codex, "seven_day")
    codex_split = (
        codex.get("thread_source_split") if isinstance(codex, Mapping) else None
    )
    thread_split = (
        codex_split.get("value") if isinstance(codex_split, Mapping) else None
    )
    thread_source = (
        str(codex_split.get("source"))
        if isinstance(codex_split, Mapping) and codex_split.get("source")
        else "~/.codex/sessions token_usage_record grouped by session_meta.thread_source"
    )
    thread_gap = (
        str(codex_split.get("gap"))
        if isinstance(codex_split, Mapping) and codex_split.get("gap")
        else "Codex rollout usage split is unavailable"
    )
    funnel_tokens = thread_split.get("funnel_tokens") if isinstance(thread_split, Mapping) else None
    personal_tokens = thread_split.get("personal_tokens") if isinstance(thread_split, Mapping) else None
    total_tokens = (
        _number(funnel_tokens) + _number(personal_tokens)
        if _number(funnel_tokens) is not None and _number(personal_tokens) is not None
        else None
    )
    metrics["D"]["D2"] = {
        "weekly_used_percent": _count(
            codex_week.get("used_percent") if codex_week else None,
            "usage.py codex windows.seven_day.used_percent",
            "Codex usage reading is unavailable",
        ),
        "funnel_vs_personal": {
            "value": thread_split,
            "funnel_tokens": _count(
                funnel_tokens, thread_source + ".automation.total_tokens", thread_gap
            ),
            "personal_tokens": _count(
                personal_tokens, thread_source + ".other_thread_sources.total_tokens", thread_gap
            ),
            "total_tokens": _count(
                total_tokens, thread_source + ".total_tokens", thread_gap
            ),
            "funnel_share": _rate_pair(
                funnel_tokens, total_tokens, thread_source + ".automation/total_tokens", thread_gap
            ),
            "personal_share": _rate_pair(
                personal_tokens, total_tokens,
                thread_source + ".other_thread_sources/total_tokens", thread_gap,
            ),
            "source": thread_source,
            **({"gap": thread_gap} if thread_split is None else {}),
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
                "numerator": lane.get("total_cost"),
                "denominator": lane.get("merged_prs"),
                "source": "brief.outcome_signals.signals.cost_per_merged_pr.by_lane",
            })
    cost_gap = (
        cost.get("reason") if isinstance(cost, Mapping) else None
    ) or signal_gap or "outcomes cost join is unavailable"
    if isinstance(cost, Mapping) and cost.get("status") == "partial":
        cost_gap = "outcomes cost join is partial; some merged tickets lack priced usage"
    metrics["D"]["D4"] = _fact(
        cost_lanes if cost_lanes else None,
        "brief.outcome_signals.signals.cost_per_merged_pr.by_lane",
        str(cost_gap),
    )
    if cost_lanes and isinstance(cost, Mapping) and cost.get("status") == "partial":
        metrics["D"]["D4"]["gap"] = str(cost_gap)
    api_by_agent: Dict[str, Dict] = {}
    resend_by_agent: Dict[str, Dict] = {}
    for agent in (rows_by_agent or {}):
        agent_finishes = [row for row in in_hour_finishes if row.get("agent") == agent]
        points = calls = total_input = fresh_input = 0
        points_complete = calls_complete = bool(agent_finishes)
        input_complete = bool(agent_finishes)
        by_run = {}
        for index, row in enumerate(agent_finishes, 1):
            run = row.get("run")
            run_id = run if isinstance(run, str) and run else "unattributed-{}".format(index)
            api = row.get("api_cost")
            api = api if isinstance(api, Mapping) else {}
            point_value = api.get("graphql_points")
            call_value = api.get("gh_calls")
            by_run[run_id] = {
                "graphql_points": _count(
                    point_value, "heartbeat.finish.api_cost.graphql_points",
                    "this run's GraphQL point count is unavailable",
                ),
                "gh_calls": _count(
                    call_value, "heartbeat.finish.api_cost.gh_calls",
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
            "points_per_run": _rate_pair(
                points if points_complete else None,
                len(agent_finishes) if points_complete else None,
                "heartbeat.finish.api_cost.graphql_points / finished runs",
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
    brief_timings = brief.get("timings")
    metrics["D"]["D5"] = {
        "graphql_points_per_run": {
            "value": api_by_agent if rows_by_agent is not None else None,
            "source": "heartbeat.finish.api_cost.graphql_points/gh_calls",
            **({"gap": heartbeat_gap} if heartbeat_gap else {}),
        },
        "points_per_brief": _count(
            brief_timings.get("api_cost.graphql_points")
            if isinstance(brief_timings, Mapping) else None,
            "brief.timings.api_cost.graphql_points",
            "the brief did not record a complete GraphQL point total",
        ),
        "gh_calls_per_brief": _count(
            brief_timings.get("api_cost.gh_calls")
            if isinstance(brief_timings, Mapping) else None,
            "brief.timings.api_cost.gh_calls",
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
            if isinstance(approvals, list) and approvals_gap is None and snapshot_gap is None else None,
            "brief.unattended_approvals timestamps",
            approvals_gap or snapshot_gap or "approval timestamps are unavailable",
        ),
        "merges_this_hour": _count(
            _count_in_hour(merges, start, end, ("at", "merged_at", "mergedAt"))
            if isinstance(merges, list) and merges_gap is None and snapshot_gap is None else None,
            "brief.unattended_merges timestamps",
            merges_gap or snapshot_gap or "merge timestamps are unavailable",
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
            len(brief["stranded"])
            if isinstance(brief.get("stranded"), list) and _section_gap(brief, "stranded") is None
            else None,
            "brief.stranded", _section_gap(brief, "stranded") or "stranded is unavailable",
        ),
        "degraded_sections": _count(
            len(brief["degraded"]) if isinstance(brief.get("degraded"), list) else None,
            "brief.degraded", "degraded is unavailable",
        ),
        "status_state_mismatches": _count(
            len(brief["status_state_mismatches"])
            if isinstance(brief.get("status_state_mismatches"), list)
            and _section_gap(brief, "status_state_mismatches") is None else None,
            "brief.status_state_mismatches",
            _section_gap(brief, "status_state_mismatches") or "status mismatch data is unavailable",
        ),
        "stale_locks_taken_over": _count(
            len(brief["stale_locks_taken_over"])
            if isinstance(brief.get("stale_locks_taken_over"), list)
            and _section_gap(brief, "stale_locks_taken_over") is None else None,
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
        "derived_at": _iso(recorded_at),
        "metrics": metrics,
    }


def _run_git(repo_dir: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["git"] + list(args), cwd=str(repo_dir), capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise MetricsError("could not run git: {}".format(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git exited {}".format(
            result.returncode
        )).strip()
        raise MetricsError("git {} failed: {}".format(" ".join(args), detail))
    return result.stdout


def _git_file(repo_dir: Path, commit: str, path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "show", "{}:{}".format(commit, path)],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
    except OSError as exc:
        raise MetricsError("could not read git history: {}".format(exc)) from exc
    if result.returncode == 0:
        return result.stdout
    detail = (result.stderr or "").strip()
    if "does not exist in" in detail or "exists on disk, but not in" in detail:
        return None
    raise MetricsError("could not read {} at {}: {}".format(path, commit, detail))


def _decode_jsonl_content(content: str, source: str) -> List[Dict]:
    rows = []
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise MetricsError("{} line {} is invalid JSON".format(
                source, line_number
            )) from exc
        if not isinstance(row, Mapping):
            raise MetricsError("{} line {} is not an object".format(
                source, line_number
            ))
        rows.append(dict(row))
    return rows


def _history_commits(repo_dir: Path, ref: str) -> List[_HistoryCommit]:
    output = _run_git(repo_dir, [
        "log", "--first-parent", "--reverse", "--format=%H%x09%ct", ref,
    ])
    commits = []
    previous = None
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            raise MetricsError("heartbeat history has a malformed commit row")
        try:
            committed_at = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise MetricsError("heartbeat history has an invalid commit time") from exc
        if previous is not None and committed_at < previous:
            raise MetricsError("heartbeat commit times are not chronological")
        commits.append(_HistoryCommit(parts[0], committed_at))
        previous = committed_at
    if not commits:
        raise MetricsError("heartbeat history is empty")
    return commits


def _floor_hour(value: datetime) -> datetime:
    found = _timestamp(value)
    if found is None:
        raise MetricsError("backfill boundary must be a parseable timestamp")
    return found.replace(minute=0, second=0, microsecond=0)


def _select_history_commits(
    commits: Sequence[_HistoryCommit],
    start: datetime,
    until: datetime,
    *,
    stride_hours: int = 1,
    max_delay: timedelta = timedelta(seconds=HISTORY_OVERLAP_GRACE),
) -> List[Tuple[datetime, Optional[_HistoryCommit]]]:
    """Select one post-hour heartbeat snapshot for every complete UTC hour."""
    if stride_hours != 1:
        raise MetricsError(
            "backfill sampling must cover every UTC hour; a wider stride can skip a window"
        )
    first = _floor_hour(start)
    end = _floor_hour(until)
    if end <= first:
        return []
    selected: List[Tuple[datetime, Optional[_HistoryCommit]]] = []
    root_time = commits[0].committed_at
    index = 0
    previous_sha = None
    hour = first
    while hour < end:
        hour_end = hour + timedelta(hours=1)
        if hour_end <= root_time:
            # These are pre-launch hours. They will be written as explicit gaps.
            selected.append((hour, None))
            hour += timedelta(hours=1)
            continue
        while index < len(commits) and commits[index].committed_at < hour_end:
            index += 1
        if index >= len(commits):
            raise MetricsError(
                "heartbeat history has no sample after {}".format(_iso(hour_end))
            )
        candidate = commits[index]
        if candidate.committed_at - hour_end > max_delay:
            raise MetricsError(
                "heartbeat history has a sampling gap after {}; reduce the stride or inspect the branch".format(
                    _iso(hour_end)
                )
            )
        if candidate.sha == previous_sha:
            raise MetricsError(
                "heartbeat sampling reused a commit across adjacent UTC hours"
            )
        selected.append((hour, candidate))
        previous_sha = candidate.sha
        hour += timedelta(hours=1)
    return selected


def _optional_json(content: Optional[str], source: str) -> Optional[Mapping[str, object]]:
    if content is None:
        return None
    try:
        value = json.loads(content)
    except ValueError as exc:
        raise MetricsError("{} is invalid JSON".format(source)) from exc
    if not isinstance(value, Mapping):
        raise MetricsError("{} must contain an object".format(source))
    return dict(value)


def _sample_run_ids(
    ledgers: Mapping[str, Optional[Sequence[Mapping[str, object]]]],
) -> frozenset:
    found = set()
    for agent, rows in ledgers.items():
        for row in rows or ():
            run = row.get("run")
            phase = row.get("phase")
            timestamp = row.get("ts")
            if isinstance(run, str) and run and isinstance(phase, str):
                found.add((agent, run, phase, str(timestamp)))
    return frozenset(found)


def _usage_from_ledgers(
    ledgers: Mapping[str, Optional[Sequence[Mapping[str, object]]]],
    hour_end: datetime,
) -> Optional[Dict[str, object]]:
    readings: Dict[str, object] = {}
    for agent, rows in ledgers.items():
        observed = []
        for row in rows or ():
            stamp = _timestamp(row.get("ts"))
            usage = row.get("usage")
            if stamp is not None and stamp <= hour_end and isinstance(usage, Mapping):
                observed.append((stamp, usage))
        if not observed:
            continue
        raw = max(observed, key=lambda pair: pair[0])[1]
        if raw.get("unmetered"):
            readings[agent] = dict(raw)
        else:
            windows = raw.get("windows")
            if not isinstance(windows, Mapping):
                windows = raw
            readings[agent] = {"windows": dict(windows)}
    return readings or None


def _load_history_sample(
    repo_dir: Path,
    hour_start: datetime,
    commit: Optional[_HistoryCommit],
) -> _HistorySample:
    if commit is None:
        return _HistorySample(
            hour_start=hour_start,
            commit=None,
            sampled_at=None,
            ledgers={agent: None for agent in AGENTS},
        )
    ledgers: Dict[str, Optional[Sequence[Mapping[str, object]]]] = {}
    for agent, path in HISTORY_AGENT_PATHS.items():
        content = _git_file(repo_dir, commit.sha, path)
        rows = _decode_jsonl_content(content, "{}:{}".format(commit.sha, path)) if content is not None else []
        ledgers[agent] = rows or None
    snapshot = _optional_json(
        _git_file(repo_dir, commit.sha, "snapshot.json"),
        "{}:snapshot.json".format(commit.sha),
    )
    readings = _optional_json(
        _git_file(repo_dir, commit.sha, "usage.json"),
        "{}:usage.json".format(commit.sha),
    )
    activity = _optional_json(
        _git_file(repo_dir, commit.sha, "commits.json"),
        "{}:commits.json".format(commit.sha),
    )
    raw_outcomes = _git_file(repo_dir, commit.sha, "outcomes.jsonl")
    outcomes = (
        _decode_jsonl_content(raw_outcomes, "{}:outcomes.jsonl".format(commit.sha))
        if raw_outcomes is not None else None
    )
    funnel_source = _git_file(repo_dir, commit.sha, "funnel.py")
    return _HistorySample(
        hour_start=hour_start,
        commit=commit.sha,
        sampled_at=commit.committed_at,
        ledgers=ledgers,
        snapshot=snapshot,
        usage_readings=readings,
        outcome_records=outcomes,
        commit_activity=activity,
        funnel_line_count=(len(funnel_source.splitlines()) if funnel_source is not None else None),
        run_ids=_sample_run_ids(ledgers),
    )


def _check_history_overlap(samples: Sequence[_HistorySample]) -> int:
    """Fail closed unless adjacent populated hourly snapshots share records."""
    checked = 0
    previous: Optional[_HistorySample] = None
    for sample in samples:
        if sample.commit is None:
            continue
        if previous is not None:
            if sample.hour_start - previous.hour_start != timedelta(hours=1):
                raise MetricsError("heartbeat samples do not cover consecutive UTC hours")
            if previous.run_ids and not sample.run_ids:
                raise MetricsError("heartbeat ledger rows disappeared between samples")
            if previous.run_ids and sample.run_ids:
                overlap = previous.run_ids.intersection(sample.run_ids)
                if not overlap:
                    raise MetricsError(
                        "consecutive heartbeat samples do not overlap; a sampling stride may have skipped a window"
                    )
                checked += 1
        previous = sample
    return checked


def _main_line_history(repo_dir: Path, ref: str = "origin/main") -> List[Tuple[datetime, int]]:
    try:
        output = _run_git(repo_dir, [
            "log", "--first-parent", "--format=%H%x09%ct", ref, "--", "funnel.py",
        ])
    except MetricsError:
        return []
    commits = []
    for line in output.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            stamp = datetime.fromtimestamp(int(parts[1]), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        commits.append((parts[0], stamp))
    counts: List[Tuple[datetime, int]] = []
    seen = set()
    for sha, stamp in sorted(commits, key=lambda row: (row[1], row[0])):
        if sha in seen:
            continue
        seen.add(sha)
        source = _git_file(repo_dir, sha, "funnel.py")
        if source is not None:
            counts.append((stamp, len(source.splitlines())))
    return counts


def _line_count_at(history: Sequence[Tuple[datetime, int]], when: datetime) -> Optional[int]:
    values = [count for stamp, count in history if stamp <= when]
    return values[-1] if values else None


def _item_timestamp(item: object, field: str) -> Optional[datetime]:
    return _timestamp(getattr(item, field, None))


def _building_times(items: Sequence[object]) -> List[datetime]:
    found = []
    for item in items:
        if getattr(item, "parent", None) is not None or getattr(item, "klass", None) != "New":
            continue
        transitions = []
        for event in getattr(item, "status_events", ()) or ():
            if not isinstance(event, Mapping) or event.get("status") != "Building":
                continue
            previous = event.get("previous_status") or event.get("previousStatus")
            if previous == "Building":
                continue
            stamp = _timestamp(event.get("at") or event.get("created_at") or event.get("createdAt"))
            if stamp is not None:
                transitions.append(stamp)
        if not transitions and getattr(item, "status", None) == "Building":
            stamp = _item_timestamp(item, "status_since")
            if stamp is not None:
                transitions.append(stamp)
        found.extend(transitions)
    return found


def _project_has_prior_cause(item: object, by_ref: Mapping[str, object], funnel_module) -> bool:
    created = _item_timestamp(item, "created_at")
    body = getattr(item, "body", None)
    for raw in funnel_module.parse_caused_by(body if isinstance(body, str) else ""):
        ref = funnel_module._normalise_cause_reference(raw)
        cause = by_ref.get(ref or "")
        cause_created = _item_timestamp(cause, "created_at") if cause is not None else None
        if created is not None and cause_created is not None and cause_created < created:
            return True
    return False


def _hourly_rework_signal(
    records: Optional[Sequence[Mapping[str, object]]],
    start: datetime,
    end: datetime,
) -> Mapping[str, object]:
    if not records:
        return {"rework_attempts": None, "merged_prs": None, "reason": "GitHub outcome history is unavailable"}
    rework_attempts = 0
    merged_prs = 0
    complete = True
    for record in records:
        repo, number = _ticket_identity(record)
        if not repo or number is None:
            continue
        prs = record.get("prs")
        if not isinstance(prs, list):
            continue
        merged_in_hour = []
        unknown_branch_in_hour = False
        all_merged = []
        for pr in prs:
            if not isinstance(pr, Mapping):
                continue
            merged_at = _timestamp(pr.get("merged_at") or pr.get("mergedAt"))
            if merged_at is None:
                continue
            branch = pr.get("head_ref_name") or pr.get("headRefName")
            if not isinstance(branch, str):
                if start <= merged_at < end:
                    unknown_branch_in_hour = True
                continue
            if branch != "ticket/{}".format(number):
                continue
            all_merged.append(merged_at)
            if start <= merged_at < end:
                merged_in_hour.append(merged_at)
        if unknown_branch_in_hour:
            complete = False
        if not merged_in_hour:
            continue
        attempts = record.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            complete = False
            continue
        if max(all_merged) in merged_in_hour:
            rework_attempts += max(0, attempts - 1)
        merged_prs += len(merged_in_hour)
    if not complete:
        return {"rework_attempts": None, "merged_prs": None, "reason": "one or more merged ticket records are incomplete"}
    return {"rework_attempts": rework_attempts, "merged_prs": merged_prs}


def _github_snapshot(
    items: Optional[Sequence[object]],
    outcome_records: Optional[Sequence[Mapping[str, object]]],
    start: datetime,
    end: datetime,
    captured_at: datetime,
) -> Mapping[str, object]:
    """Rebuild hourly A/B source facts from durable issue and PR observations."""
    import funnel

    brief: Dict[str, object] = {}
    if outcome_records is not None:
        prs, _, _ = _outcome_prs(outcome_records, start, end)
        if prs is not None:
            merged = [row for row in prs if row["in_hour"]["merged_at"]]
            brief["command_center_ticket_pr_share"] = {
                "available": True,
                "ticket_merged_prs": sum(1 for row in merged if row["repo"] == REPO),
                "merged_prs": len(merged),
            }
        brief["outcome_signals"] = {
            "signals": {"rework_rate": dict(_hourly_rework_signal(outcome_records, start, end))}
        }
    if items is not None:
        projects = [item for item in items if getattr(item, "parent", None) is None]
        closed = [
            item for item in projects
            if (stamp := _item_timestamp(item, "closed_at")) is not None
            and start <= stamp < end
            and getattr(item, "state_reason", None) != "NOT_PLANNED"
        ]
        if all(getattr(item, "klass", None) is not None for item in closed):
            brief["maintenance_load"] = {
                "closed_in_window": len(closed),
                "upkeep_projects": sum(
                    1 for item in closed
                    if getattr(item, "klass", None) in {"Broken", "Maintenance", "Investigate"}
                ),
            }
        start_times = _building_times(projects)
        past_starts = [stamp for stamp in start_times if stamp < end]
        brief["maintenance_load"] = dict(brief.get("maintenance_load") or {})
        brief["maintenance_load"].update({
            "new_started_at": [_iso(stamp) for stamp in past_starts],
            "days_since_anything_new_started": (
                (end - max(past_starts)).days if past_starts else None
            ),
        })
        closed_projects = [
            item for item in projects
            if _item_timestamp(item, "closed_at") is not None
            and start <= _item_timestamp(item, "closed_at") < end
        ]
        created_projects = [
            item for item in projects
            if (stamp := _item_timestamp(item, "created_at")) is not None
            and start <= stamp < end
        ]
        status_known = all(getattr(item, "status", None) is not None for item in closed_projects)
        brief["disposal"] = {
            "done": sum(1 for item in closed_projects if getattr(item, "status", None) == "Done") if status_known else None,
            "parked": sum(1 for item in closed_projects if getattr(item, "status", None) == "Parked") if status_known else None,
            "net_open_growth": len(created_projects) - len(closed_projects),
        }
        broken_created = [
            item for item in created_projects if getattr(item, "klass", None) == "Broken"
        ]
        unknown_created_class = any(
            getattr(item, "klass", None) is None for item in created_projects
        )
        by_ref = {getattr(item, "ref", ""): item for item in items}
        recorded = {
            getattr(item, "ref", "") for item in broken_created
            if _project_has_prior_cause(item, by_ref, funnel)
        }
        broken_refs = {getattr(item, "ref", "") for item in broken_created}
        for item in items:
            if not getattr(item, "title", "").startswith(funnel.REGRESSION_PREFIX):
                continue
            created = _item_timestamp(item, "created_at")
            if created is None or not (start <= created < end):
                continue
            ticket_ref = funnel._regression_ticket_ref(getattr(item, "body", None))
            ticket = by_ref.get(ticket_ref or "")
            parent = by_ref.get(getattr(ticket, "parent", None) or "") if ticket else None
            if parent is not None and getattr(parent, "ref", None) in broken_refs:
                recorded.add(getattr(parent, "ref"))
        brief["recorded_cause_regressions"] = {
            "with_recorded_cause": None if unknown_created_class else len(recorded),
            "broken_projects": None if unknown_created_class else len(broken_created),
        }
    if not brief:
        return {}
    return {"generated_at": _iso(captured_at), "brief": brief}


def _build_backfill_rows(
    repo_dir: Path,
    history_ref: str,
    start: Optional[datetime] = None,
    until: Optional[datetime] = None,
    *,
    outcome_records: Optional[Sequence[Mapping[str, object]]] = None,
    project_items: Optional[Sequence[object]] = None,
    derived_at: Optional[datetime] = None,
) -> Tuple[List[Dict], int]:
    commits = _history_commits(repo_dir, history_ref)
    launch = start or commits[0].committed_at.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = min(_floor_hour(until or datetime.now(timezone.utc)), _floor_hour(commits[-1].committed_at))
    selected = _select_history_commits(commits, launch, cutoff)
    samples = [
        _load_history_sample(repo_dir, hour, commit)
        for hour, commit in selected
    ]
    overlap_count = _check_history_overlap(samples)
    line_history = _main_line_history(repo_dir)
    records_at = derived_at or datetime.now(timezone.utc)
    rows = []
    for sample in samples:
        start_at = sample.hour_start
        end_at = start_at + timedelta(hours=1)
        records = outcome_records if outcome_records is not None else sample.outcome_records
        snapshot = (
            sample.snapshot
            if sample.snapshot is not None
            else _github_snapshot(project_items, records, start_at, end_at, records_at)
        )
        readings = sample.usage_readings
        if readings is None:
            readings = _usage_from_ledgers(sample.ledgers, end_at)
        line_count = sample.funnel_line_count
        if line_count is None:
            line_count = _line_count_at(line_history, end_at)
        observation = sample.sampled_at or (start_at + timedelta(minutes=30))
        rows.append(derive_row(
            snapshot,
            sample.ledgers,
            readings,
            records,
            now=observation,
            commit_activity=sample.commit_activity,
            funnel_line_count=line_count,
            hour_start=start_at,
            derived_at=records_at,
        ))
    return rows, overlap_count


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


def append_rows_many(
    existing: Sequence[Mapping[str, object]],
    rows: Iterable[Mapping[str, object]],
) -> Tuple[List[Dict], int]:
    """Append only previously unseen hours, preserving every stored row."""
    found = [dict(value) for value in existing]
    seen = {value.get("hour") for value in found}
    appended = 0
    for row in rows:
        hour = row.get("hour")
        if not isinstance(hour, str):
            raise MetricsError("cannot append a row without its UTC hour")
        if hour in seen:
            continue
        found.append(dict(row))
        seen.add(hour)
        appended += 1
    return found, appended

_SERIES_SUM_PREFIXES = (
    ("A", "A1"),
    ("A", "A4", "new_projects_started"),
    ("A", "A5", "done"),
    ("A", "A5", "parked"),
    ("A", "A5", "net_open_growth"),
    ("A", "A6", "reverts_by_repo"),
    ("A", "A6", "reopened_tickets"),
    ("C", "C1"),
    ("C", "C4"),
    ("C", "C5"),
    ("D", "D5", "points_per_brief"),
    ("D", "D5", "gh_calls_per_brief"),
    ("D", "D5", "api_reserve_skips"),
    ("D", "D5", "graphql_points_per_run"),
    ("D", "D6", "held_hours_by_agent_and_reason"),
    ("E", "E2", "opened_this_hour"),
    ("E", "E3"),
    ("E", "E4"),
    ("F", "F1"),
)
_SERIES_IDENTITY_KEYS = ("lane", "repo", "agent", "job", "reason", "stage", "name")
_SERIES_MISSING = object()


def _series_number(value: object, *, signed: bool = False) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (not signed and number < 0):
        return None
    return number


def _series_render_number(value: float) -> object:
    return int(value) if value.is_integer() else round(value, 9)


def _series_kind(path: Tuple[str, ...]) -> str:
    if any(path[:len(prefix)] == prefix for prefix in _SERIES_SUM_PREFIXES):
        return "sum"
    return "mean"


def _flatten_series_row(row: Mapping[str, object]):
    """Return numeric/categorical leaves plus code-level gap information."""
    root = row.get("metrics")
    if not isinstance(root, Mapping):
        raise MetricsError("hourly row metrics must be an object")
    leaves: Dict[Tuple[str, ...], Dict[str, object]] = {}
    present_codes = set()
    code_gaps = set()

    def emit(path: Tuple[str, ...], kind: str, source: Optional[str],
             gap: Optional[str], value: object = _SERIES_MISSING,
             numerator: object = _SERIES_MISSING,
             denominator: object = _SERIES_MISSING) -> None:
        valid = not bool(gap)
        if kind in ("rate", "weighted_mean"):
            top = _series_number(numerator, signed=(kind == "weighted_mean"))
            bottom = _series_number(denominator)
            valid = valid and top is not None and bottom is not None and bottom > 0
            numerator, denominator = top, bottom
        elif kind == "category":
            valid = valid and isinstance(value, str)
        else:
            number = _series_number(
                value,
                signed=(path[:3] == ("A", "A5", "net_open_growth")),
            )
            valid = valid and number is not None
            value = number
        leaves[path] = {
            "kind": kind,
            "source": source,
            "gap": gap if isinstance(gap, str) and gap else None,
            "valid": valid,
            "value": value,
            "numerator": numerator,
            "denominator": denominator,
        }

    def walk(value: object, path: Tuple[str, ...], source: Optional[str] = None,
             inherited_gap: Optional[str] = None) -> None:
        if isinstance(value, Mapping):
            current_source = value.get("source")
            if not isinstance(current_source, str) or not current_source:
                current_source = source
            current_gap = value.get("gap")
            if not isinstance(current_gap, str) or not current_gap:
                current_gap = inherited_gap
            if len(path) == 2 and current_gap:
                code_gaps.add(path)

            if "numerator" in value or "denominator" in value:
                emit(path, "rate", current_source, current_gap,
                     numerator=value.get("numerator"),
                     denominator=value.get("denominator"))
                return
            if "sum_seconds" in value and "count" in value:
                emit(path, "weighted_mean", current_source, current_gap,
                     numerator=value.get("sum_seconds"), denominator=value.get("count"))
                return
            if "sum" in value or "count" in value:
                emit(path, "weighted_mean", current_source, current_gap,
                     numerator=value.get("sum"), denominator=value.get("count"))
                return
            if "value" in value and ("source" in value or "gap" in value):
                payload = value.get("value")
                if payload is None:
                    emit(path, _series_kind(path), current_source,
                         current_gap or "hourly value is unavailable")
                else:
                    walk(payload, path, current_source, current_gap)
                return

            children = [
                (str(key), child) for key, child in value.items()
                if key not in ("source", "gap")
            ]
            if not children:
                if current_gap:
                    emit(path, _series_kind(path), current_source, current_gap)
                return
            for key, child in children:
                walk(child, path + (key,), current_source, current_gap)
            return

        if isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, Mapping):
                    identity = next(
                        ((key, child[key]) for key in _SERIES_IDENTITY_KEYS
                         if isinstance(child.get(key), (str, int))),
                        None,
                    )
                    if identity is not None:
                        walk(child, path + (identity[0], str(identity[1])),
                             source, inherited_gap)
                        continue
                walk(child, path + (str(index),), source, inherited_gap)
            return

        if isinstance(value, str):
            emit(path, "category", source, inherited_gap, value=value)
        else:
            emit(path, _series_kind(path), source,
                 inherited_gap or ("hourly value is unavailable" if value is None else None),
                 value=value)

    for group, group_metrics in root.items():
        if not isinstance(group, str) or not isinstance(group_metrics, Mapping):
            continue
        for code, value in group_metrics.items():
            if not isinstance(code, str):
                continue
            code_path = (group, code)
            present_codes.add(code_path)
            walk(value, code_path)
    return leaves, present_codes, code_gaps


def _daily_series_leaf(path: Tuple[str, ...], kind: str,
                       hourly: Sequence[Tuple[Dict, set, set]]):
    """Aggregate one leaf across a UTC day; any explicit gap fails closed."""
    code = path[:2]
    values: List[object] = []
    numerators: List[float] = []
    denominators: List[float] = []
    sources: List[str] = []
    for leaves, present_codes, code_gaps in hourly:
        if code not in present_codes or code in code_gaps:
            return None, None, None, None
        observation = leaves.get(path)
        if observation is None:
            if kind == "sum":
                values.append(0.0)
                continue
            return None, None, None, None
        if observation.get("kind") != kind or not observation.get("valid"):
            return None, None, None, None
        source = observation.get("source")
        if isinstance(source, str) and source:
            sources.append(source)
        if kind in ("rate", "weighted_mean"):
            numerator = observation.get("numerator")
            denominator = observation.get("denominator")
            if not isinstance(numerator, (int, float)) or not isinstance(denominator, (int, float)):
                return None, None, None, None
            numerators.append(float(numerator))
            denominators.append(float(denominator))
        else:
            values.append(observation.get("value"))

    if not hourly:
        return None, None, None, None
    if kind == "sum":
        value = sum(values)
        numerator = denominator = None
    elif kind == "mean":
        if not values or any(not isinstance(item, (int, float)) for item in values):
            return None, None, None, None
        value = sum(values) / len(values)
        numerator = denominator = None
    elif kind in ("rate", "weighted_mean"):
        numerator, denominator = sum(numerators), sum(denominators)
        if denominator <= 0:
            return None, None, None, None
        value = numerator / denominator
    elif kind == "category":
        if not values or not isinstance(values[-1], str):
            return None, None, None, None
        value = values[-1]
        numerator = denominator = None
    else:
        return None, None, None, None
    return value, numerator, denominator, (sources[-1] if sources else None)


def _rolling_values(kind: str, daily: Sequence[object],
                    numerators: Sequence[Optional[float]],
                    denominators: Sequence[Optional[float]], window: int
                    ) -> List[Optional[float]]:
    result: List[Optional[float]] = []
    for index in range(len(daily)):
        start = index - window + 1
        if start < 0:
            result.append(None)
            continue
        values = daily[start:index + 1]
        if any(value is None or not isinstance(value, (int, float)) for value in values):
            result.append(None)
            continue
        if kind in ("rate", "weighted_mean"):
            top = numerators[start:index + 1]
            bottom = denominators[start:index + 1]
            if any(value is None for value in top + bottom):
                result.append(None)
                continue
            denominator = sum(bottom)  # type: ignore[arg-type]
            result.append(sum(top) / denominator if denominator > 0 else None)  # type: ignore[arg-type]
        elif kind == "category":
            result.append(None)
        else:
            result.append(sum(values) / window)  # type: ignore[arg-type]
    return result


def _set_series_leaf(tree: Dict[str, object], path: Tuple[str, ...], value: Dict[str, object]) -> None:
    node: Dict[str, object] = tree
    for part in path[:-1]:
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            raise MetricsError("metrics series has conflicting paths at {}".format(part))
        node = child
    if not path or path[-1] in node:
        raise MetricsError("metrics series has a duplicate or empty leaf path")
    node[path[-1]] = value


def series_from_rows(rows: Sequence[Mapping[str, object]],
                     now: Optional[datetime] = None) -> Dict[str, object]:
    """Build the 90-day daily series and its R7, R28 and signed delta arrays.

    ``days`` aligns with each leaf's ``daily``, ``r7``, ``r28`` and ``delta``
    arrays. Event totals sum hourly facts, gauges average them, and rate leaves
    retain numerator/denominator parts so rolling rates are weighted correctly.
    A missing observation or source gap remains JSON ``null`` through rollup.
    """
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    as_of = observed_at.astimezone(timezone.utc).date()
    start_day = as_of - timedelta(days=SERIES_DAYS - 1)
    day_names = [
        (start_day + timedelta(days=index)).isoformat()
        for index in range(SERIES_DAYS)
    ]
    flattened_by_day: Dict[str, List[Tuple[Dict, set, set]]] = {}
    latest_observations: Dict[Tuple[str, ...], Dict[str, object]] = {}
    kinds: Dict[Tuple[str, ...], str] = {}
    seen_hours = set()
    latest_source_at: Optional[datetime] = None
    in_window: List[Tuple[datetime, str, Tuple[Dict, set, set]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise MetricsError("metrics series contains a non-object row")
        hour = _timestamp(row.get("hour"))
        if hour is None:
            raise MetricsError("metrics series contains a row without a valid hour")
        hour_key = _iso(hour.replace(minute=0, second=0, microsecond=0))
        if hour_key in seen_hours:
            raise MetricsError("metrics series contains duplicate hour {}".format(hour_key))
        seen_hours.add(hour_key)
        day_name = hour.date().isoformat()
        if day_name < day_names[0] or day_name > day_names[-1]:
            continue
        flattened = _flatten_series_row(row)
        for path, observation in flattened[0].items():
            kind = observation["kind"]
            prior = kinds.setdefault(path, kind)
            if prior != kind:
                raise MetricsError("metrics series changed aggregation kind for {}".format(".".join(path)))
        in_window.append((hour, day_name, flattened))
        source_at = _timestamp(row.get("derived_at")) or hour
        if latest_source_at is None or source_at > latest_source_at:
            latest_source_at = source_at
    in_window.sort(key=lambda item: item[0])
    for _, day_name, flattened in in_window:
        flattened_by_day.setdefault(day_name, []).append(flattened)
        latest_observations.update(flattened[0])

    # Some fields are unavailable for an hour as one code-level gap, while
    # their normal shape has nested leaves. Keep those gaps on the code state
    # so every nested leaf stays null, but do not emit a colliding root leaf.
    for path in list(kinds):
        if len(path) == 2 and any(
            other[:2] == path and len(other) > 2 for other in kinds
        ):
            del kinds[path]

    series_tree: Dict[str, object] = {}
    for path in sorted(kinds):
        kind = kinds[path]
        daily: List[object] = []
        daily_numerators: List[Optional[float]] = []
        daily_denominators: List[Optional[float]] = []
        sources: List[str] = []
        for day_name in day_names:
            hourly = flattened_by_day.get(day_name, [])
            value, numerator, denominator, source = _daily_series_leaf(path, kind, hourly)
            daily.append(value)
            daily_numerators.append(numerator)
            daily_denominators.append(denominator)
            if isinstance(source, str) and source:
                sources.append(source)

        r7 = _rolling_values(kind, daily, daily_numerators, daily_denominators, 7)
        r28 = _rolling_values(kind, daily, daily_numerators, daily_denominators, 28)
        delta = [
            left - right if left is not None and right is not None else None
            for left, right in zip(r7, r28)
        ]
        leaf: Dict[str, object] = {
            "kind": kind,
            "daily": [
                item if item is None or kind == "category" else _series_render_number(float(item))
                for item in daily
            ],
            "r7": [_series_render_number(item) if item is not None else None for item in r7],
            "r28": [_series_render_number(item) if item is not None else None for item in r28],
            "delta": [_series_render_number(item) if item is not None else None for item in delta],
        }
        latest_observation = latest_observations.get(path)
        latest_gap = latest_observation.get("gap") if latest_observation else None
        if isinstance(latest_gap, str) and latest_gap:
            leaf["gap"] = latest_gap
        if sources:
            leaf["source"] = sources[-1]
        if kind in ("rate", "weighted_mean"):
            leaf["numerators"] = [
                _series_render_number(item) if item is not None else None
                for item in daily_numerators
            ]
            leaf["denominators"] = [
                _series_render_number(item) if item is not None else None
                for item in daily_denominators
            ]
        _set_series_leaf(series_tree, path, leaf)

    return {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "start_date": day_names[0],
        "days": day_names,
        "source_updated_at": _iso(latest_source_at) if latest_source_at else None,
        "metrics": series_tree,
    }


def append_rows(
    existing: Sequence[Mapping[str, object]], row: Mapping[str, object]
) -> Tuple[List[Dict], int]:
    """Pure append policy used by the remote writer and fixture tests."""
    return append_rows_many(existing, [row])


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


def append_remote_rows(
    rows: Sequence[Mapping[str, object]],
    repo: str = REPO,
    branch: str = BRANCH,
) -> int:
    """Append a batch with Contents API compare-and-swap and hour idempotency."""
    if not rows:
        return 0
    first_hour = rows[0].get("hour")
    last_hour = rows[-1].get("hour")
    for attempt in range(len(STORE_BACKOFF) + 1):
        existing, sha = _read_remote(repo, branch)
        combined, appended = append_rows_many(existing, rows)
        if not appended:
            return 0
        payload = {
            "message": "metrics: backfill {} through {}".format(first_hour, last_hour),
            "branch": branch,
            "content": base64.b64encode(
                _encode_rows(combined).encode("utf-8")
            ).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        result = _gh([
            "api", "-X", "PUT", "repos/{}/contents/{}".format(repo, METRICS_PATH),
            "--input", "-",
        ], stdin=json.dumps(payload))
        if result.returncode == 0:
            return appended
        detail = (result.stderr or result.stdout or "").lower()
        if "409" not in detail and "sha" not in detail and "conflict" not in detail:
            raise MetricsError(result.stderr.strip() or "could not write {}".format(METRICS_PATH))
        if attempt >= len(STORE_BACKOFF):
            raise MetricsError("could not append backfill after compare-and-swap retries")
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


def read_codex_thread_source_split(
    weekly_window: Optional[Mapping],
    now: datetime,
    paths: Optional[Sequence[str]] = None,
) -> Dict:
    """Sum Codex turn tokens in its current weekly window by rollout source."""
    source = "~/.codex/sessions token_usage_record.turn_token_usage grouped by session_meta.thread_source"
    reset = _timestamp(weekly_window.get("resets_at")) if isinstance(weekly_window, Mapping) else None
    if reset is None:
        return {"value": None, "source": source, "gap": "Codex weekly reset time is unavailable"}
    observed_at = now.astimezone(timezone.utc)
    start = reset - timedelta(days=7)
    end = min(observed_at, reset)
    if paths is None:
        try:
            import usage
            paths = glob.glob(usage.CODEX_SESSIONS)
        except Exception as exc:
            return {"value": None, "source": source, "gap": "could not locate Codex rollouts: {}".format(exc)}
    if not paths:
        return {"value": None, "source": source, "gap": "no Codex session rollouts are readable"}

    totals = {"funnel": 0, "personal": 0}
    sessions = {"funnel": 0, "personal": 0}
    readable = 0
    unreadable = 0
    incomplete = 0
    unclassified = 0
    for path in paths:
        try:
            if os.path.getmtime(path) < start.timestamp():
                readable += 1
                continue
            token_total = 0
            has_tokens = False
            thread_sources = set()
            with open(path, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        incomplete += 1
                        continue
                    if not isinstance(record, Mapping):
                        continue
                    record_type = record.get("type")
                    payload = record.get("payload")
                    if record_type == "session_meta":
                        value = payload.get("thread_source") if isinstance(payload, Mapping) else None
                        if isinstance(value, str) and value:
                            thread_sources.add(value)
                        continue
                    if record_type != "token_usage_record":
                        continue
                    if not isinstance(payload, Mapping):
                        incomplete += 1
                        continue
                    stamp = _timestamp(record.get("timestamp"))
                    if stamp is None:
                        incomplete += 1
                        continue
                    if not start <= stamp < end:
                        continue
                    usage = payload.get("turn_token_usage") or payload.get("usage")
                    amount = _number(usage.get("total_tokens")) if isinstance(usage, Mapping) else None
                    if amount is None or not amount.is_integer():
                        incomplete += 1
                        continue
                    token_total += int(amount)
                    has_tokens = True
            readable += 1
        except OSError:
            unreadable += 1
            continue
        if has_tokens and len(thread_sources) != 1:
            unclassified += 1
            continue
        if has_tokens:
            group = "funnel" if next(iter(thread_sources)) == "automation" else "personal"
            totals[group] += token_total
            sessions[group] += 1
    if unreadable or incomplete or unclassified:
        reason = []
        if unreadable:
            reason.append("{} candidate rollout(s) could not be read".format(unreadable))
        if incomplete:
            reason.append("{} token record(s) were incomplete".format(incomplete))
        if unclassified:
            reason.append("{} rollout(s) with token use lacked one thread_source".format(unclassified))
        return {"value": None, "source": source, "gap": "; ".join(reason)}
    if readable == 0:
        return {"value": None, "source": source, "gap": "no Codex session rollouts are readable"}
    return {
        "value": {
            "funnel_tokens": totals["funnel"],
            "personal_tokens": totals["personal"],
            "funnel_sessions": sessions["funnel"],
            "personal_sessions": sessions["personal"],
            "unit": "tokens",
            "window_start": _iso(start),
            "window_end": _iso(end),
        },
        "source": source,
    }


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
    codex = usage_readings.get("codex")
    codex_split = read_codex_thread_source_split(
        _usage_window(codex, "seven_day"), now
    )
    codex = dict(codex) if isinstance(codex, Mapping) else {}
    codex["thread_source_split"] = codex_split
    usage_readings["codex"] = codex
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
        in_hour_rows = []
        for row in rows_for_repo:
            commit = row.get("commit")
            committer = commit.get("committer") if isinstance(commit, Mapping) else None
            author = commit.get("author") if isinstance(commit, Mapping) else None
            stamp = _timestamp(
                (committer.get("date") if isinstance(committer, Mapping) else None)
                or (author.get("date") if isinstance(author, Mapping) else None)
            )
            if stamp is None:
                raise MetricsError("commit timestamp is missing for {}".format(repo))
            if start <= stamp < end:
                in_hour_rows.append(row)
        commits_by_repo[repo] = len(in_hour_rows)
        reverts = 0
        for row in in_hour_rows:
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


def _parse_backfill_boundary(value: Optional[str], name: str) -> Optional[datetime]:
    if value is None:
        return None
    parsed = _timestamp(value)
    if parsed is None:
        raise MetricsError("--{} must be a parseable UTC timestamp".format(name))
    return parsed


def _collect_github_backfill_inputs(
    now: datetime,
) -> Tuple[List[object], List[Dict[str, object]]]:
    """Read durable PR, issue, and Project history for the A/B metric groups."""
    import funnel
    import outcomes

    items = funnel.load_items(include_details=True)
    repos = sorted(set(funnel.member_repos()) | {funnel.REPO})
    records: List[Dict[str, object]] = []
    for repo in repos:
        # The heartbeat branch is read locally and separately for C-F. Passing
        # an empty mapping prevents outcome derivation from making another
        # ledger read while it scans durable GitHub issue and PR history.
        records.extend(outcomes.derive_repository(
            repo, now=now, heartbeat_records={},
        ))
    return items, records


def _backfill_command(args) -> int:
    repo_dir = Path(args.repo_dir).resolve()
    history_ref = args.history_ref
    now = datetime.now(timezone.utc)
    try:
        start = _parse_backfill_boundary(args.start, "start")
        until = _parse_backfill_boundary(args.until, "until")
        if history_ref == "origin/heartbeat":
            _run_git(repo_dir, [
                "fetch", "origin",
                "refs/heads/heartbeat:refs/remotes/origin/heartbeat",
            ])
            # F2 is a local main-branch line-count series. If main is not
            # available, those cells remain gaps while heartbeat metrics can
            # still be rebuilt.
            try:
                _run_git(repo_dir, [
                    "fetch", "origin", "refs/heads/main:refs/remotes/origin/main",
                ])
            except MetricsError:
                pass
        project_items, outcomes = _collect_github_backfill_inputs(now)
        rows, overlap_count = _build_backfill_rows(
            repo_dir,
            history_ref,
            start,
            until,
            outcome_records=outcomes,
            project_items=project_items,
            derived_at=now,
        )
        if not rows:
            raise MetricsError("heartbeat history contains no complete hourly windows")
        if args.dry_run:
            appended = None
        else:
            appended = append_remote_rows(rows)
        print(json.dumps({
            "first_hour": rows[0]["hour"],
            "last_hour": rows[-1]["hour"],
            "rows": len(rows),
            "overlapping_sample_pairs": overlap_count,
            "appended": appended,
            "storage": "{}:{}".format(BRANCH, METRICS_PATH),
        }, sort_keys=True))
        return 0
    except Exception as exc:
        print("metrics backfill: {}".format(exc), file=sys.stderr)
        return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Derive and report execution metrics")
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
    backfill = sub.add_parser(
        "backfill", help="rebuild hourly metrics from heartbeat and GitHub history"
    )
    backfill.add_argument(
        "--repo-dir", default=str(Path(__file__).resolve().parent),
        help="local checkout containing the heartbeat and main git history",
    )
    backfill.add_argument(
        "--history-ref", default="origin/heartbeat",
        help="heartbeat history ref to sample (default: origin/heartbeat)",
    )
    backfill.add_argument("--start", help="first UTC hour; default is launch day")
    backfill.add_argument("--until", help="exclusive UTC-hour cutoff; default is latest complete hour")
    backfill.add_argument("--dry-run", action="store_true", help="derive rows without writing metrics.jsonl")
    series = sub.add_parser("series", help="roll hourly facts into 90 daily values")
    series.add_argument("--rows", help="fixture hourly metrics JSONL; default reads the heartbeat branch")
    series.add_argument("--now", help="UTC timestamp anchoring the 90-day window")
    args = parser.parse_args(argv)
    try:
        if args.command == "backfill":
            return _backfill_command(args)
        if args.command == "series":
            now = _parse_now(args.now) or datetime.now(timezone.utc)
            rows = _read_jsonl(args.rows) if args.rows else _read_remote()[0]
            result = series_from_rows(rows, now)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        now = _parse_now(args.now) or datetime.now(timezone.utc)
        snapshot = _read_snapshot(args.snapshot)
        fixture_mode = bool(
            args.snapshot or args.ledger or args.outcomes or args.usage
            or args.commits or args.line_count is not None
        )
        if fixture_mode:
            ledgers, readings, records, activity, line_count = _load_fixture_inputs(args)
        else:
            ledgers, readings, records, activity, line_count = _live_inputs(now)
        row = derive_row(snapshot, ledgers, readings, records, now, activity, line_count)
        if args.dry_run or fixture_mode:
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
