#!/usr/bin/env python3
"""Assemble one read-only review packet for a PR (#798, Phase 1 of #794).

The review runner shows the model this packet and nothing else: the ticket
body, plan.md, the diff, CI state, the newest verdict and its head, the
changed-file overlap with every other open PR, protected-path touches, and
the stop-auto-merging counter. Pre-checks and the model call come later;
this module only assembles the evidence.

Read-only by construction: every GitHub call here is a view, list, diff, or
content read. Anything that changes remote state belongs in a later
``review-apply`` step, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402

#: Entries ending in "/" match a directory prefix; the rest match exactly.
#: From routines/muse.md: a ticket PR touching any of these fails review
#: unless its own ticket asked for the change.
PROTECTED_PATHS = (
    ".claude/settings.json",
    "routines/",
    "skills/",
    "AGENTS.md",
    "plan.md",
)

#: The resolved external-volume spelling. It belongs only in sandbox
#: configuration; in a file an agent runs commands from it breaks the
#: permission rule, which matches the canonical path literally.
RESOLVED_PATH_MARKER = "/Volumes/"

#: Conclusions GitHub reports for checks that passed or were excused.
CI_SUCCESS = ("SUCCESS", "NEUTRAL", "SKIPPED")

#: States that mean a check has not reported a conclusion yet.
CI_PENDING = ("PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING",
              "REQUESTED", "STALE")


def ci_state(checks: Sequence[dict]) -> str:
    """Derive green/red/unknown from a statusCheckRollup list.

    The red rule is merge_blockers' rule: any reported conclusion outside
    the success set. Anything unfinished, or no checks at all, is unknown
    rather than green — an absent signal must never read as a passing one.
    Tolerates both wire shapes: CheckRun (conclusion/status) and Status
    (state/context).
    """
    if not checks:
        return "unknown"
    pending = False
    for check in checks:
        if not isinstance(check, dict):
            continue
        result = check.get("conclusion") or check.get("state")
        if result not in CI_SUCCESS + (None, ""):
            return "red"
        if result in (None, ""):
            status = str(check.get("status") or "").upper()
            if status and status != "COMPLETED":
                pending = True
            elif not status:
                # A bare entry with neither conclusion nor status carries
                # no signal yet.
                pending = True
        elif str(result).upper() in CI_PENDING:
            pending = True
    return "unknown" if pending else "green"


def summarize_checks(rollup: Sequence[dict]) -> List[Dict[str, Optional[str]]]:
    """Keep the per-check fields a reviewer needs, in a stable shape."""
    summarized = []
    for check in rollup or []:
        if not isinstance(check, dict):
            continue
        summarized.append({
            "name": check.get("name") or check.get("context"),
            "conclusion": check.get("conclusion"),
            "state": check.get("state"),
            "status": check.get("status"),
        })
    return summarized


def protected_touches(changed_files: Sequence[str], diff: str) -> Dict:
    """Which protected paths the diff touches, and any path respelling.

    ``touched`` names the changed files that hit a protected rule; ``rules``
    names the rules they hit. ``resolved_path_spelling`` is true when the
    diff text itself introduces the resolved external-volume spelling that
    test_guardrails.py forbids in executed files.
    """
    touched: List[str] = []
    rules: List[str] = []
    for path in changed_files or []:
        for rule in PROTECTED_PATHS:
            if rule.endswith("/"):
                hit = path.startswith(rule)
            else:
                hit = path == rule
            if hit:
                if path not in touched:
                    touched.append(path)
                if rule not in rules:
                    rules.append(rule)
    return {
        "touched": sorted(touched),
        "rules": sorted(rules),
        "resolved_path_spelling": bool(diff) and RESOLVED_PATH_MARKER in diff,
    }


def file_overlap(candidate_files: Sequence[str],
                 open_prs: Sequence[dict],
                 candidate_pr: int) -> List[Dict]:
    """Changed-file overlap between the candidate and every other open PR.

    A merge changes main underneath every other open PR, so an overlap is
    the early warning that the candidate may have gone stale. Only PRs
    with a non-empty intersection are listed.
    """
    mine = set(candidate_files or [])
    overlapping = []
    for row in open_prs or []:
        if not isinstance(row, dict) or row.get("number") == candidate_pr:
            continue
        theirs = {
            entry.get("path") for entry in (row.get("files") or [])
            if isinstance(entry, dict) and entry.get("path")
        }
        shared = sorted(mine & theirs)
        if shared:
            overlapping.append({
                "pr": row.get("number"),
                "branch": row.get("headRefName"),
                "files": shared,
            })
    overlapping.sort(key=lambda entry: entry.get("pr") or 0)
    return overlapping


def build_packet(*, repo: str, pr_number: int, pr_view: dict, diff: str,
                 ticket: Optional[dict], plan_md: str,
                 plan_md_missing: bool, open_prs: Sequence[dict],
                 verdict: Optional[dict], stop_counter: dict,
                 collected_at: str) -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO.

    Every field the review question needs, in one JSON-serialisable dict.
    ``ticket`` is None when the branch is not a ticket/<n> branch; the
    verdict's head_sha sits beside it so a reader can compare it with the
    current head without parsing the verdict.
    """
    pr_view = pr_view or {}
    changed_files = sorted({
        entry.get("path") for entry in (pr_view.get("files") or [])
        if isinstance(entry, dict) and entry.get("path")
    })
    checks = summarize_checks(pr_view.get("statusCheckRollup") or [])
    if ticket is None:
        ticket_packet: Dict[str, Optional[object]] = {
            "ref": None, "number": None, "title": None, "url": None,
            "body": None,
        }
    else:
        ticket_packet = {
            "ref": ticket.get("ref"),
            "number": ticket.get("number"),
            "title": ticket.get("title"),
            "url": ticket.get("url"),
            "body": ticket.get("body"),
        }
    return {
        "repo": repo,
        "pr": pr_number,
        "pr_title": pr_view.get("title"),
        "branch": pr_view.get("headRefName"),
        "base": pr_view.get("baseRefName"),
        "state": pr_view.get("state"),
        "mergeable": pr_view.get("mergeable"),
        "head_sha": pr_view.get("headRefOid"),
        "ticket": ticket_packet,
        "plan_md": plan_md,
        "plan_md_missing": plan_md_missing,
        "diff": diff,
        "changed_files": changed_files,
        "ci": {"state": ci_state(pr_view.get("statusCheckRollup") or []),
               "checks": checks},
        "verdict": verdict,
        "verdict_head_sha": (verdict or {}).get("head_sha"),
        "overlap": file_overlap(changed_files, open_prs, pr_number),
        "protected": protected_touches(changed_files, diff),
        "stop_auto_merging": stop_counter,
        "collected_at": collected_at,
    }


def fetch_pr(repo: str, pr_number: int) -> dict:
    """One PR view: identity, head, CI rollup, and changed files."""
    data = funnel._gh_json(
        "gh", "pr", "view", str(pr_number), "--repo", repo, "--json",
        "number,title,headRefName,headRefOid,baseRefName,state,mergeable,"
        "statusCheckRollup,files")
    if not data:
        raise funnel.GitHubError(
            "could not read PR #{} in {}".format(pr_number, repo))
    return data


def fetch_diff(repo: str, pr_number: int) -> str:
    """The unified diff of the PR."""
    proc = funnel._run_gh(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not read diff for PR #{} in {}: {}".format(
                pr_number, repo, (proc.stderr or "").strip()))
    return proc.stdout or ""


def fetch_ticket(repo: str, number: int) -> dict:
    """The ticket body behind a ticket/<n> branch, with its identity."""
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo, "--json",
        "number,title,url,body")
    if not data:
        raise funnel.GitHubError(
            "could not read ticket {}#{}".format(repo, number))
    data["ref"] = "{}#{}".format(repo, number)
    return data


def fetch_plan_md(repo: str) -> Tuple[str, bool]:
    """The repo's plan.md at its default branch, or ("", True) if absent.

    Missing is a fact about the repo, not a fetch failure: the packet
    stays valid and says so, because the review question is still
    answerable without it.
    """
    proc = funnel._run_gh(
        ["gh", "api", "repos/{}/contents/plan.md".format(repo),
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        if "404" in (proc.stderr or ""):
            return "", True
        raise funnel.GitHubError(
            "could not read plan.md in {}: {}".format(
                repo, (proc.stderr or "").strip()))
    return proc.stdout or "", False


def fetch_open_prs(repo: str) -> List[dict]:
    """Every open PR's number, branch, and changed files, in one read."""
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "open",
        "--json", "number,headRefName,files", "--limit", "100")
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError(
            "could not list open PRs in {}".format(repo))
    return [row for row in rows if isinstance(row, dict)]


def fetch_verdict(repo: str, pr_number: int) -> Optional[dict]:
    """The newest review verdict on the PR, or None. Newest wins."""
    return funnel.latest_verdict(repo, pr_number)


def fetch_stop_counter(
        items_loader: Optional[Callable[[], list]] = None,
        now: Optional[datetime] = None) -> dict:
    """The rejected-merges counter funnel.rejected_merges computes.

    Reused rather than re-derived: two definitions of "stop" would
    disagree exactly when the bar has failed.
    """
    loader = items_loader or funnel.load_items
    return dict(funnel.rejected_merges(
        loader(), now or datetime.now(timezone.utc)))


def collect(repo: Optional[str], pr_number: int, *,
            items_loader: Optional[Callable[[], list]] = None,
            now: Optional[datetime] = None) -> Dict:
    """Fetch every piece and build the packet. Reads only, no writes."""
    resolved = funnel.resolve_repo(repo)
    pr_view = fetch_pr(resolved, pr_number)
    ref = funnel.ticket_ref_from_branch(
        resolved, pr_view.get("headRefName") or "")
    if ref is not None:
        ticket = fetch_ticket(resolved, int(ref.split("#", 1)[1]))
    else:
        ticket = None
    plan_md, plan_md_missing = fetch_plan_md(resolved)
    return build_packet(
        repo=resolved,
        pr_number=pr_number,
        pr_view=pr_view,
        diff=fetch_diff(resolved, pr_number),
        ticket=ticket,
        plan_md=plan_md,
        plan_md_missing=plan_md_missing,
        open_prs=fetch_open_prs(resolved),
        verdict=fetch_verdict(resolved, pr_number),
        stop_counter=fetch_stop_counter(items_loader, now),
        collected_at=(now or datetime.now(timezone.utc)).isoformat(),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the review packet for one PR as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only review packet for a PR")
    parser.add_argument("pr", type=int, help="PR number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    args = parser.parse_args(argv)
    try:
        packet = collect(args.repo, args.pr)
    except funnel.GitHubError as exc:
        print("review-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
