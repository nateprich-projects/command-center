#!/usr/bin/env python3
"""Derive one append-only outcome record for each closed GitHub ticket.

The record is evidence, not telemetry supplied by a run.  Pull requests,
checks, review verdict comments, issue events and direct comments are all read
from GitHub after the event.  Durable records live in ``outcomes.jsonl`` on the
``heartbeat`` branch, alongside the heartbeat history but outside the code
checkout.  The file is an append-only projection: every line can be rebuilt
from GitHub, and a ticket is appended at most once.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import funnel
import session_usage


REPO = "nateprich-projects/command-center"
HEARTBEAT_BRANCH = "heartbeat"
OUTCOMES_PATH = "outcomes.jsonl"
NATE_LOGIN = "nateprich"

# ``funnel.ticket_pr_index`` defaults to the brief's 100-row diagnostic bound.
# Outcome derivation asks for a larger whole-repository scan, and refuses to
# write if even that scan is truncated rather than silently under-counting
# attempts.  Backfill has its own ticket; this bound keeps the normal job
# finite while covering the current repository history.
PR_SCAN_LIMIT = 1000

# Keep the retired zcode records readable: a historical outcome may still have
# been produced by it even though no new run is scheduled.
HEARTBEAT_AGENTS = ("claude", "codex", "muse", "zcode")

STORE_BACKOFF = (1, 3, 7)
SUCCESS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
FAILURE_CONCLUSIONS = frozenset({
    "ACTION_REQUIRED", "CANCELLED", "FAILURE", "STALE", "STARTUP_FAILURE",
    "TIMED_OUT", "ERROR",
})


class OutcomeError(RuntimeError):
    """The evidence could not be read or the append-only contract was unsafe."""


def _timestamp(value: object) -> Optional[datetime]:
    """Parse GitHub's UTC timestamp fields without guessing on bad input."""
    if isinstance(value, datetime):
        found = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
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


def _timestamp_text(value: object) -> Optional[str]:
    """Return a stable ISO value for a valid timestamp, or ``None``."""
    found = _timestamp(value)
    return found.isoformat().replace("+00:00", "Z") if found else None


def _number(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _login(value: object) -> Optional[str]:
    if not isinstance(value, Mapping):
        return None
    found = value.get("login")
    return found if isinstance(found, str) and found else None


def _author_login(row: Mapping[str, object]) -> Optional[str]:
    return _login(row.get("author")) or _login(row.get("user"))


def _body(row: Mapping[str, object]) -> str:
    value = row.get("body")
    return value if isinstance(value, str) else ""


def _pr_number(row: Mapping[str, object]) -> Optional[int]:
    return _number(row.get("number"))


def _pr_sort_key(row: Mapping[str, object]) -> Tuple[float, int]:
    """Order PRs by their outcome timestamp, then by number."""
    for name in ("mergedAt", "closedAt", "createdAt"):
        stamp = _timestamp(row.get(name))
        if stamp is not None:
            return stamp.timestamp(), _pr_number(row) or 0
    return -math.inf, _pr_number(row) or 0


def _is_merged(row: Mapping[str, object]) -> bool:
    return bool(row.get("mergedAt")) or str(row.get("state") or "").upper() == "MERGED"


def ci_green(checks: object) -> Optional[bool]:
    """Return the aggregate CI result for one PR.

    A missing or incomplete check list is unknown.  A red check wins over all
    other checks; only a complete list of successful, neutral or skipped checks
    is green.  This mirrors the merge gate's fail-closed vocabulary without
    treating an unreadable check as a pass.
    """
    if not isinstance(checks, list) or not checks:
        return None

    unknown = False
    for check in checks:
        if not isinstance(check, Mapping):
            unknown = True
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        if conclusion in FAILURE_CONCLUSIONS:
            return False
        if conclusion not in SUCCESS_CONCLUSIONS:
            unknown = True
    if unknown:
        return None
    return True


def _ci_result(value: Optional[bool]) -> str:
    if value is True:
        return "green"
    if value is False:
        return "red"
    return "unknown"


def _provenance(body: str) -> Optional[Dict]:
    try:
        return funnel.parse_provenance(body)
    except Exception:
        # A malformed provenance block must not make the evidence job fail.
        # It is handled as an unattributed direct comment below.
        return None


def _direct_nate_comment(row: object) -> bool:
    """Whether a GitHub comment is evidence of Nate's own intervention.

    Agent comments are often posted through Nate's GitHub login.  A valid
    ``voice: agent`` provenance marker therefore excludes those comments; an
    unmarked or ``nate-direct``/``nate-relayed`` comment is treated as human
    involvement.  This is intentionally conservative: ambiguity counts as
    intervention rather than being mistaken for unattended work.
    """
    if not isinstance(row, Mapping) or _author_login(row) != NATE_LOGIN:
        return False
    provenance = _provenance(_body(row))
    return provenance is None or provenance.get("voice") != "agent"


def _verdicts(comments: object) -> List[Dict[str, object]]:
    """Read structured verdicts from one PR's comments, oldest first."""
    if not isinstance(comments, list):
        return []
    found: List[Dict[str, object]] = []
    for index, comment in enumerate(comments):
        if not isinstance(comment, Mapping):
            continue
        parsed = funnel.parse_verdict(_body(comment))
        if not isinstance(parsed, Mapping):
            continue
        verdict = parsed.get("verdict")
        if verdict not in funnel.VERDICTS:
            continue
        at = _timestamp(comment.get("createdAt"))
        if at is None:
            at = _timestamp(parsed.get("reviewed_at"))
        found.append({
            "verdict": verdict,
            "at": at,
            "index": index,
            "head_sha": parsed.get("head_sha"),
        })
    found.sort(key=lambda row: (
        row["at"] is None,
        row["at"] or datetime.min.replace(tzinfo=timezone.utc),
        int(row["index"]),
    ))
    return found


def _comment_rows(detail: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    comments = detail.get("comments")
    if not isinstance(comments, list):
        return ()
    return (row for row in comments if isinstance(row, Mapping))


def _merged_without_approval(
    row: Mapping[str, object], verdicts: Sequence[Mapping[str, object]]
) -> bool:
    if not _is_merged(row):
        return False
    return not any(verdict.get("verdict") == "approved" for verdict in verdicts)


def _reopened_after_merge(
    events: object, merged_prs: Sequence[Mapping[str, object]]
) -> bool:
    merge_times = [
        _timestamp(row.get("mergedAt"))
        for row in merged_prs
        if _timestamp(row.get("mergedAt")) is not None
    ]
    if not merge_times or not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, Mapping) or event.get("event") != "reopened":
            continue
        reopened_at = _timestamp(event.get("created_at") or event.get("createdAt"))
        if reopened_at is not None and any(reopened_at > merged for merged in merge_times):
            return True
    return False


def _record_pr(row: Mapping[str, object]) -> Dict[str, object]:
    """Keep the PR identity and lifecycle facts useful to a later reader."""
    number = _pr_number(row)
    result: Dict[str, object] = {
        "number": number,
        "state": row.get("state"),
        "url": row.get("url"),
        "created_at": _timestamp_text(row.get("createdAt")),
        "closed_at": _timestamp_text(row.get("closedAt")),
        "merged_at": _timestamp_text(row.get("mergedAt")),
    }
    return result


def _record_time(value: object) -> Optional[datetime]:
    """Parse either GitHub ISO timestamps or heartbeat epoch seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return _timestamp(value)


def _heartbeat_remote_path(repo: str, agent: str, branch: str) -> str:
    return "repos/{}/contents/{}.jsonl?ref={}".format(repo, agent, branch)


def read_heartbeat_records(
    repo: str = REPO,
    branch: str = HEARTBEAT_BRANCH,
) -> Dict[str, List[Dict[str, object]]]:
    """Read durable heartbeat rows, best effort, without using quota readings.

    The heartbeat branch supplies the run-to-ticket binding and the exact
    harness session id.  A missing file, malformed row or temporarily
    unreadable agent stream leaves that agent without usage rather than making
    a GitHub-derived outcome look free.
    """
    found: Dict[str, List[Dict[str, object]]] = {}
    for agent in HEARTBEAT_AGENTS:
        result = _run_gh(["api", _heartbeat_remote_path(repo, agent, branch)])
        if result.returncode != 0:
            found[agent] = []
            continue
        try:
            payload = json.loads(result.stdout)
            content = base64.b64decode(payload.get("content", "")).decode("utf-8")
        except (TypeError, ValueError, UnicodeDecodeError):
            found[agent] = []
            continue
        rows = []
        for line in content.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, Mapping):
                rows.append(dict(row))
        found[agent] = rows
    return found


def _latest_by_run(
    rows: Iterable[Mapping[str, object]],
    phase: str,
) -> Dict[str, Dict[str, object]]:
    found: Dict[str, Dict[str, object]] = {}
    for row in rows:
        if row.get("phase") != phase or not isinstance(row.get("run"), str):
            continue
        run = row["run"]
        old = found.get(run)
        if old is None or (_record_time(row.get("ts")) or datetime.min.replace(
            tzinfo=timezone.utc
        )) >= (_record_time(old.get("ts")) or datetime.min.replace(
            tzinfo=timezone.utc
        )):
            found[run] = dict(row)
    return found


def _run_metadata(
    start: Mapping[str, object],
    finish: Optional[Mapping[str, object]],
    name: str,
) -> object:
    if finish is not None and finish.get(name) is not None:
        return finish.get(name)
    return start.get(name)


def _ticket_runs(
    ticket_ref: str,
    heartbeat_records: Mapping[str, Sequence[Mapping[str, object]]],
) -> List[Dict[str, object]]:
    """Join one ticket to all of its bound implementation runs."""
    found: List[Dict[str, object]] = []
    for agent, rows in heartbeat_records.items():
        starts = _latest_by_run(rows, "start")
        finishes = _latest_by_run(rows, "finish")
        bindings = _latest_by_run(rows, "bind")
        for run, binding in bindings.items():
            if binding.get("do") != "ticket" or str(binding.get("work")) != ticket_ref:
                continue
            start = starts.get(run, {})
            finish = finishes.get(run)
            started_at = _record_time(start.get("ts"))
            finished_at = _record_time(finish.get("ts")) if finish else None
            session_id = start.get("session_id") or (
                finish.get("session_id") if finish else None
            )
            usage = session_usage.usage_for_session(
                agent,
                session_id if isinstance(session_id, str) else None,
                started_at=started_at,
                finished_at=finished_at,
            )
            found.append({
                "run": run,
                "agent": agent,
                "started_at": _timestamp_text(started_at),
                "finished_at": _timestamp_text(finished_at),
                "outcome": finish.get("outcome") if finish else None,
                "provider": _run_metadata(start, finish, "provider"),
                "harness": _run_metadata(start, finish, "harness"),
                "model": _run_metadata(start, finish, "model"),
                "reasoning_effort": _run_metadata(start, finish, "reasoning_effort"),
                "model_source": _run_metadata(start, finish, "model_source"),
                "token_usage": usage,
            })
    found.sort(key=lambda row: (
        row.get("started_at") is None,
        row.get("started_at") or "",
        row.get("run") or "",
    ))
    return found


def _aggregate_token_usage(
    runs: Sequence[Mapping[str, object]],
) -> Optional[Dict[str, Optional[int]]]:
    if not runs:
        return None
    totals = {kind: 0 for kind in session_usage.TOKEN_KINDS}
    for run in runs:
        usage = run.get("token_usage")
        if not isinstance(usage, Mapping):
            return None
        for kind in session_usage.TOKEN_KINDS:
            value = usage.get(kind)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            totals[kind] += value
    return totals


def _top_level_run_metadata(
    runs: Sequence[Mapping[str, object]],
    name: str,
) -> object:
    # The Project display copy is singular.  Use the final implementation run
    # that observed a value; the per-run list remains authoritative when a
    # ticket changed models during rework.
    for run in reversed(runs):
        value = run.get(name)
        if value is not None:
            return value
    return None


def derive_outcome(
    ticket: Mapping[str, object],
    prs: Iterable[Mapping[str, object]] = (),
    pr_details: Optional[Mapping[int, Mapping[str, object]]] = None,
    issue_comments: object = (),
    issue_events: object = (),
    now: Optional[datetime] = None,
    run_observations: object = (),
) -> Dict[str, object]:
    """Derive one JSON-safe outcome record from supplied GitHub observations.

    The function is pure apart from the default clock, which makes the
    acceptance cases fixture-driven while the command-line job only supplies
    observations fetched from GitHub.
    """
    repo = ticket.get("repo") or ticket.get("repository")
    number = _pr_number(ticket)
    if not isinstance(repo, str) or not repo or number is None:
        raise OutcomeError("a ticket needs a repository and positive number")

    details = pr_details or {}
    merged_rows: List[Mapping[str, object]] = []
    all_verdicts: List[Dict[str, object]] = []
    verdicts_by_pr: Dict[int, List[Dict[str, object]]] = {}
    pr_rows: List[Mapping[str, object]] = []
    direct_human = any(
        _direct_nate_comment(row) for row in (issue_comments or [])
        if isinstance(row, Mapping)
    )

    for raw in prs:
        if not isinstance(raw, Mapping):
            continue
        pr_number = _pr_number(raw)
        if pr_number is None:
            continue
        combined = dict(raw)
        detail = details.get(pr_number)
        if isinstance(detail, Mapping):
            combined.update(detail)
        pr_rows.append(combined)
        if _is_merged(combined):
            merged_rows.append(combined)

    pr_rows.sort(key=_pr_sort_key)
    merged_rows.sort(key=_pr_sort_key)
    for combined in pr_rows:
        pr_number = _pr_number(combined) or 0
        verdicts = _verdicts(combined.get("comments"))
        verdicts_by_pr[pr_number] = verdicts
        all_verdicts.extend(
            dict(verdict, pr_number=pr_number) for verdict in verdicts
        )
        if any(_direct_nate_comment(comment) for comment in _comment_rows(combined)):
            direct_human = True
        if any(
            _direct_nate_comment(review)
            for review in (combined.get("reviews") or [])
            if isinstance(review, Mapping)
        ):
            direct_human = True
    all_verdicts.sort(key=lambda row: (
        row["at"] is None,
        row["at"] or datetime.min.replace(tzinfo=timezone.utc),
        int(row.get("pr_number") or 0),
        int(row.get("index") or 0),
    ))
    final_pr = pr_rows[-1] if pr_rows else None
    final_ci = ci_green(final_pr.get("statusCheckRollup")) if final_pr else None
    merged_pr_numbers = [
        _pr_number(row) for row in merged_rows if _pr_number(row) is not None
    ]
    intervention = direct_human or any(
        _merged_without_approval(row, verdicts_by_pr.get(_pr_number(row), []))
        for row in merged_rows
    )

    # The latest structured verdict is the ticket's final review result. Turns
    # deliberately stays null when no PR has a parseable verdict comment: a
    # pre-marker PR did not take zero turns, its history is unknown.
    latest_verdict = all_verdicts[-1]["verdict"] if all_verdicts else None
    closed_at = _timestamp_text(ticket.get("closedAt") or ticket.get("closed_at"))
    recorded_at = now or datetime.now(timezone.utc)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)

    runs = [
        dict(row) for row in (run_observations or [])
        if isinstance(row, Mapping)
    ]

    return {
        "schema_version": 1,
        "source": "github",
        "ticket": "{}#{}".format(repo, number),
        "repo": repo,
        "number": number,
        "title": ticket.get("title"),
        "url": ticket.get("url"),
        "closed_at": closed_at,
        "derived_at": recorded_at.astimezone(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "attempts": len(pr_rows),
        "turns": len(all_verdicts) if all_verdicts else None,
        "ci_green": final_ci,
        "ci_result": _ci_result(final_ci),
        "review_result": latest_verdict,
        "merged": bool(merged_rows),
        "merged_prs": merged_pr_numbers,
        "reopened_after_merge": _reopened_after_merge(issue_events, merged_rows),
        "human_intervention_required": bool(intervention),
        "prs": [_record_pr(row) for row in pr_rows],
        # Raw per-run usage is intentionally retained beside the aggregate.
        # #165 prices these timestamped observations; this ticket does not
        # invent a dollar value or reuse heartbeat's provider quota meter.
        "runs": runs,
        "token_usage": _aggregate_token_usage(runs),
        "provider": _top_level_run_metadata(runs, "provider"),
        "harness": _top_level_run_metadata(runs, "harness"),
        "model": _top_level_run_metadata(runs, "model"),
        "reasoning_effort": _top_level_run_metadata(runs, "reasoning_effort"),
        "model_source": _top_level_run_metadata(runs, "model_source"),
    }


def _run_gh(args: Sequence[str]):
    """Run a GitHub CLI read through funnel's route guard.

    Outcome derivation is a consumer of the same GraphQL-backed ``gh`` route
    as the funnel. Reusing its launcher means an exhausted route is refused
    after the first observed failure instead of producing one failing call per
    historical ticket (#513). REST calls, including the repository-wide issue
    event scan, keep their own route as funnel does.
    """
    return funnel._run_gh(
        ["gh"] + list(args), capture_output=True, text=True
    )


def gh_json(*args: str):
    """Run one read-only gh command and decode its JSON response."""
    result = _run_gh(args)
    if result.returncode != 0:
        if funnel.route_exhausted() is not None:
            raise funnel.GitHubError(
                result.stderr.strip()
                or "GitHub GraphQL route exhausted during outcome derivation"
            )
        raise OutcomeError(result.stderr.strip() or "gh exited {}".format(result.returncode))
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise OutcomeError("gh returned invalid JSON: {}".format(exc)) from exc


def list_closed_tickets(repo: str, limit: int = PR_SCAN_LIMIT) -> List[Dict[str, object]]:
    """Read closed issues, excluding pull requests via the ``gh issue`` API."""
    rows = gh_json(
        "issue", "list", "--repo", repo, "--state", "closed",
        "--limit", str(limit + 1),
        "--json", "number,title,url,closedAt,stateReason,comments",
    )
    if not isinstance(rows, list):
        raise OutcomeError("invalid closed-ticket response for {}".format(repo))
    if len(rows) > limit:
        raise OutcomeError(
            "closed-ticket scan for {} exceeded {}; refusing partial outcome records".format(
                repo, limit
            )
        )
    found = [dict(row) for row in rows if isinstance(row, Mapping) and _pr_number(row)]
    found.sort(key=lambda row: (_timestamp(row.get("closedAt")) or datetime.max.replace(
        tzinfo=timezone.utc), _pr_number(row) or 0))
    for row in found:
        row["repo"] = repo
    return found


def _flatten_pages(payload: object) -> List[Dict[str, object]]:
    if not isinstance(payload, list):
        return []
    rows: List[Dict[str, object]] = []
    for page in payload:
        values = page if isinstance(page, list) else [page]
        rows.extend(dict(row) for row in values if isinstance(row, Mapping))
    return rows


def _issue_observations(repo: str, number: int) -> Tuple[List[Dict], List[Dict]]:
    issue = gh_json(
        "issue", "view", str(number), "--repo", repo, "--json", "comments"
    )
    comments = issue.get("comments") if isinstance(issue, Mapping) else None
    if not isinstance(comments, list):
        raise OutcomeError("invalid comments response for {}#{}".format(repo, number))
    events = gh_json(
        "api", "--paginate", "--slurp",
        "repos/{}/issues/{}/events?per_page=100".format(repo, number),
    )
    return comments, _flatten_pages(events)


def _repository_issue_events(repo: str) -> Dict[int, List[Dict[str, object]]]:
    """Read repository issue events once and group them by issue number.

    The per-issue endpoint is correct for a small sample but turns a full
    history walk into one REST request per closed ticket. GitHub's repository
    event endpoint is paginated, so it gives the same evidence in one bounded
    route walk and remains safe when an issue or its head ref has disappeared.
    """
    payload = gh_json(
        "api", "--paginate", "--slurp",
        "repos/{}/issues/events?per_page=100".format(repo),
    )
    grouped: Dict[int, List[Dict[str, object]]] = {}
    for row in _flatten_pages(payload):
        issue = row.get("issue")
        number = issue.get("number") if isinstance(issue, Mapping) else None
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            number = row.get("issue_number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            continue
        grouped.setdefault(number, []).append(row)
    return grouped


def _index_with_outcome_details(
    repo: str, limit: int,
) -> Tuple[Mapping[str, Mapping[str, object]], bool, Sequence[Mapping[str, object]]]:
    """Use the shared PR index and request detail fields only for backfill."""
    try:
        index, truncated = funnel.ticket_pr_index(
            repo, limit=limit, include_comments=True
        )
    except TypeError as exc:
        # Keep fixture-era callers and downstream consumers that provide the
        # pre-#101 two-argument test double compatible. The real funnel helper
        # always accepts the opt-in detail flag.
        if "include_comments" not in str(exc):
            raise
        index, truncated = funnel.ticket_pr_index(repo, limit=limit)
    rows = getattr(index, "all_rows", tuple(index.values()))
    return index, truncated, tuple(
        row for row in rows if isinstance(row, Mapping)
    )


def _closing_ticket_numbers(
    row: Mapping[str, object], repo: str,
) -> Iterable[int]:
    """Yield ticket numbers explicitly linked by a PR when its head ref is gone."""
    references = row.get("closingIssuesReferences")
    if not isinstance(references, list):
        return ()
    found: List[int] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        repository = reference.get("repository")
        ref_repo = (
            repository.get("nameWithOwner")
            if isinstance(repository, Mapping) else None
        )
        if ref_repo is not None and ref_repo != repo:
            continue
        number = reference.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number > 0:
            found.append(number)
    return found


def _null_field_counts(
    records: Iterable[Mapping[str, object]],
) -> Dict[str, int]:
    """Count null top-level fields so a backfill reports unknown history plainly."""
    counts: Dict[str, int] = {}
    for record in records:
        for name, value in record.items():
            if value is None:
                counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def outcome_summary(
    records: Sequence[Mapping[str, object]], appended: int,
) -> Dict[str, object]:
    """Return the mechanical counts printed by ``derive`` and used in its PR."""
    null_fields = _null_field_counts(records)
    return {
        "derived": len(records),
        "appended": appended,
        "null_fields": sum(null_fields.values()),
        "null_fields_by_name": null_fields,
        "storage": "{}:{}".format(HEARTBEAT_BRANCH, OUTCOMES_PATH),
    }


def _pr_observation(repo: str, number: int) -> Dict[str, object]:
    detail = gh_json(
        "pr", "view", str(number), "--repo", repo,
        "--json",
        "number,state,url,headRefName,headRefOid,createdAt,closedAt,mergedAt,"
        "statusCheckRollup,comments,reviews,commits",
    )
    if not isinstance(detail, Mapping):
        raise OutcomeError("invalid PR response for {} PR #{}".format(repo, number))
    return dict(detail)


def derive_repository(
    repo: str, limit: int = PR_SCAN_LIMIT, now: Optional[datetime] = None,
    ticket_numbers: Optional[Iterable[int]] = None,
    heartbeat_records: Optional[Mapping[str, Sequence[Mapping[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Derive every closed-ticket record in one repository.

    The PR scan is repository-wide and shared across all tickets. A truncated
    scan is an error: writing partial attempt counts would make the data look
    complete and would be harder to notice than a failed run.
    """
    tickets = list_closed_tickets(repo, limit=limit)
    wanted_numbers = None
    if ticket_numbers is not None:
        wanted_numbers = {int(number) for number in ticket_numbers}
        tickets = [
            ticket for ticket in tickets
            if _pr_number(ticket) in wanted_numbers
        ]
    heartbeat_rows = (
        dict(heartbeat_records)
        if heartbeat_records is not None
        else read_heartbeat_records()
    )
    index, truncated, indexed_rows = _index_with_outcome_details(repo, limit)
    if truncated:
        raise OutcomeError(
            "PR scan for {} exceeded {}; refusing partial outcome records".format(
                repo, limit
            )
        )
    issue_events = _repository_issue_events(repo)
    by_branch: Dict[str, List[Dict[str, object]]] = {}
    by_ticket_number: Dict[int, List[Dict[str, object]]] = {}
    for row in indexed_rows:
        branch = row.get("headRefName")
        if isinstance(branch, str) and branch.startswith("ticket/"):
            by_branch.setdefault(branch, []).append(dict(row))
        # A deleted head ref can leave the PR without a usable branch name.
        # Preserve the attempt when GitHub still exposes the PR's explicit
        # closing-issue relationship.
        for closing_number in _closing_ticket_numbers(row, repo):
            by_ticket_number.setdefault(closing_number, []).append(dict(row))

    records: List[Dict[str, object]] = []
    for ticket in tickets:
        number = _pr_number(ticket)
        if number is None:
            continue
        prs = by_branch.get("ticket/{}".format(number), [])
        if not prs:
            prs = by_ticket_number.get(number, [])
        details: Dict[int, Dict[str, object]] = {}
        for row in prs:
            pr_number = _pr_number(row)
            # The outcome-aware index carries comments, reviews and checks in
            # one repository-wide read. Fixture-era rows without comments use
            # the old detail reader, keeping the pure test seam and the
            # unreachable-ref fallback intact.
            if pr_number is not None and "comments" not in row:
                details[pr_number] = _pr_observation(repo, pr_number)
        comments = ticket.get("comments")
        if not isinstance(comments, list):
            comments, events = _issue_observations(repo, number)
        else:
            events = issue_events.get(number, [])
        records.append(derive_outcome(
            ticket,
            prs,
            details,
            comments,
            events,
            now=now,
            run_observations=_ticket_runs(
                "{}#{}".format(repo, number), heartbeat_rows
            ),
        ))
    return records


def _decode_records(content: str) -> List[Dict[str, object]]:
    records: List[Dict[str, object]] = []
    seen = set()
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise OutcomeError(
                "{} line {} is not valid JSON".format(OUTCOMES_PATH, line_number)
            ) from exc
        if not isinstance(row, Mapping) or not isinstance(row.get("ticket"), str):
            raise OutcomeError(
                "{} line {} is not an outcome record".format(OUTCOMES_PATH, line_number)
            )
        if row["ticket"] in seen:
            raise OutcomeError(
                "{} contains duplicate ticket {}".format(OUTCOMES_PATH, row["ticket"])
            )
        seen.add(row["ticket"])
        records.append(dict(row))
    return records


def _encode_records(records: Iterable[Mapping[str, object]]) -> str:
    return "".join(
        json.dumps(dict(row), sort_keys=True) + "\n" for row in records
    )


def _remote_path(repo: str, branch: str) -> str:
    return "repos/{}/contents/{}?ref={}".format(repo, OUTCOMES_PATH, branch)


def _read_remote(repo: str = REPO, branch: str = HEARTBEAT_BRANCH) -> Tuple[List[Dict], Optional[str]]:
    result = _run_gh(["api", _remote_path(repo, branch)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").lower()
        if "404" in detail or "not found" in detail:
            return [], None
        raise OutcomeError(result.stderr.strip() or "could not read {}".format(OUTCOMES_PATH))
    try:
        payload = json.loads(result.stdout)
        content = base64.b64decode(payload.get("content", "")).decode("utf-8")
        sha = payload.get("sha")
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise OutcomeError("invalid remote {} response: {}".format(OUTCOMES_PATH, exc)) from exc
    if not isinstance(sha, str) and sha is not None:
        raise OutcomeError("remote {} response has an invalid sha".format(OUTCOMES_PATH))
    return _decode_records(content), sha


def read_records(
    repo: str = REPO, branch: str = HEARTBEAT_BRANCH
) -> List[Dict[str, object]]:
    """Read the durable derived records from the heartbeat branch."""
    return _read_remote(repo, branch)[0]


def _new_records(
    existing: Sequence[Mapping[str, object]], additions: Iterable[Mapping[str, object]]
) -> List[Dict[str, object]]:
    seen = {row.get("ticket") for row in existing}
    found: List[Dict[str, object]] = []
    for row in additions:
        if not isinstance(row, Mapping) or not isinstance(row.get("ticket"), str):
            raise OutcomeError("cannot append a record without a ticket ref")
        ticket = row["ticket"]
        if ticket in seen:
            continue
        seen.add(ticket)
        found.append(dict(row))
    return found


def append_records(
    additions: Iterable[Mapping[str, object]],
    repo: str = REPO,
    branch: str = HEARTBEAT_BRANCH,
) -> int:
    """Append unseen records with Contents-API compare-and-swap retries."""
    pending = [dict(row) for row in additions]
    if not pending:
        return 0

    for attempt in range(len(STORE_BACKOFF) + 1):
        existing, sha = _read_remote(repo, branch)
        fresh = _new_records(existing, pending)
        if not fresh:
            return 0
        body = _encode_records(existing + fresh).encode("utf-8")
        args = [
            "api", "-X", "PUT",
            "repos/{}/contents/{}".format(repo, OUTCOMES_PATH),
            "-f", "message=outcomes: +{} record(s)".format(len(fresh)),
            "-f", "branch=" + branch,
            "-f", "content=" + base64.b64encode(body).decode("ascii"),
        ]
        if sha:
            args.extend(["-f", "sha=" + sha])
        result = _run_gh(args)
        if result.returncode == 0:
            return len(fresh)
        detail = (result.stderr or result.stdout or "").lower()
        if "409" not in detail and "sha" not in detail and "conflict" not in detail:
            raise OutcomeError(result.stderr.strip() or "could not write {}".format(OUTCOMES_PATH))
        if attempt >= len(STORE_BACKOFF):
            raise OutcomeError(
                "could not append {} after compare-and-swap retries".format(OUTCOMES_PATH)
            )
        time.sleep(STORE_BACKOFF[attempt])
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    derive = sub.add_parser("derive", help="derive closed-ticket records")
    derive.add_argument(
        "--repo", action="append", default=None,
        help="repository to scan; repeat for more than one (default: command-center)",
    )
    derive.add_argument("--limit", type=int, default=PR_SCAN_LIMIT)
    derive.add_argument(
        "--ticket", action="append", type=int, default=None,
        help="only derive these closed issue numbers; repeat to sample a run",
    )
    derive.add_argument(
        "--dry-run", action="store_true",
        help="print records without writing the heartbeat branch",
    )

    show = sub.add_parser("read", help="print durable outcome records")
    show.add_argument("--repo", default=REPO)
    show.add_argument("--branch", default=HEARTBEAT_BRANCH)

    args = parser.parse_args(argv)
    try:
        if args.command == "read":
            print(json.dumps(read_records(args.repo, args.branch), indent=2, sort_keys=True))
            return 0

        repos = args.repo or [REPO]
        records: List[Dict[str, object]] = []
        for repo in repos:
            records.extend(derive_repository(
                repo, limit=args.limit, ticket_numbers=args.ticket
            ))
        if args.dry_run:
            print(json.dumps(records, indent=2, sort_keys=True))
            return 0
        appended = append_records(records)
        print(json.dumps(outcome_summary(records, appended), sort_keys=True))
        return 0
    except (OutcomeError, funnel.GitHubError) as exc:
        print("outcomes: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
