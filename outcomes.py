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
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import funnel


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


def derive_outcome(
    ticket: Mapping[str, object],
    prs: Iterable[Mapping[str, object]] = (),
    pr_details: Optional[Mapping[int, Mapping[str, object]]] = None,
    issue_comments: object = (),
    issue_events: object = (),
    now: Optional[datetime] = None,
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
        verdicts = _verdicts(combined.get("comments"))
        all_verdicts.extend(verdicts)
        if any(_direct_nate_comment(comment) for comment in _comment_rows(combined)):
            direct_human = True
        if any(
            _direct_nate_comment(review)
            for review in (combined.get("reviews") or [])
            if isinstance(review, Mapping)
        ):
            direct_human = True
        if _is_merged(combined):
            merged_rows.append(combined)

    pr_rows.sort(key=_pr_sort_key)
    merged_rows.sort(key=_pr_sort_key)
    final_pr = pr_rows[-1] if pr_rows else None
    final_ci = ci_green(final_pr.get("statusCheckRollup")) if final_pr else None
    merged_pr_numbers = [
        _pr_number(row) for row in merged_rows if _pr_number(row) is not None
    ]
    intervention = direct_human or any(
        _merged_without_approval(
            row,
            _verdicts((details.get(_pr_number(row)) or {}).get("comments"))
            if isinstance(details.get(_pr_number(row)), Mapping)
            else _verdicts(row.get("comments")),
        )
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
    }


def _run_gh(args: Sequence[str]):
    return subprocess.run(["gh"] + list(args), capture_output=True, text=True)


def gh_json(*args: str):
    """Run one read-only gh command and decode its JSON response."""
    result = _run_gh(args)
    if result.returncode != 0:
        raise OutcomeError(result.stderr.strip() or "gh exited {}".format(result.returncode))
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise OutcomeError("gh returned invalid JSON: {}".format(exc)) from exc


def list_closed_tickets(repo: str, limit: int = PR_SCAN_LIMIT) -> List[Dict[str, object]]:
    """Read closed issues, excluding pull requests via the ``gh issue`` API."""
    rows = gh_json(
        "issue", "list", "--repo", repo, "--state", "closed",
        "--limit", str(limit),
        "--json", "number,title,url,closedAt,stateReason",
    )
    if not isinstance(rows, list):
        raise OutcomeError("invalid closed-ticket response for {}".format(repo))
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
    repo: str, limit: int = PR_SCAN_LIMIT, now: Optional[datetime] = None
) -> List[Dict[str, object]]:
    """Derive every closed-ticket record in one repository.

    The PR scan is repository-wide and shared across all tickets. A truncated
    scan is an error: writing partial attempt counts would make the data look
    complete and would be harder to notice than a failed run.
    """
    tickets = list_closed_tickets(repo, limit=limit)
    index, truncated = funnel.ticket_pr_index(repo, limit=limit)
    if truncated:
        raise OutcomeError(
            "PR scan for {} exceeded {}; refusing partial outcome records".format(
                repo, limit
            )
        )
    rows = getattr(index, "all_rows", tuple(index.values()))
    by_branch: Dict[str, List[Dict[str, object]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        branch = row.get("headRefName")
        if isinstance(branch, str) and branch.startswith("ticket/"):
            by_branch.setdefault(branch, []).append(dict(row))

    records: List[Dict[str, object]] = []
    for ticket in tickets:
        number = _pr_number(ticket)
        if number is None:
            continue
        prs = by_branch.get("ticket/{}".format(number), [])
        details: Dict[int, Dict[str, object]] = {}
        for row in prs:
            pr_number = _pr_number(row)
            if pr_number is not None:
                details[pr_number] = _pr_observation(repo, pr_number)
        comments, events = _issue_observations(repo, number)
        records.append(derive_outcome(
            ticket, prs, details, comments, events, now=now
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
            records.extend(derive_repository(repo, limit=args.limit))
        if args.dry_run:
            print(json.dumps(records, indent=2, sort_keys=True))
            return 0
        appended = append_records(records)
        print(json.dumps({
            "derived": len(records),
            "appended": appended,
            "storage": "{}:{}".format(HEARTBEAT_BRANCH, OUTCOMES_PATH),
        }, sort_keys=True))
        return 0
    except (OutcomeError, funnel.GitHubError) as exc:
        print("outcomes: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
