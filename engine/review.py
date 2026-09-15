#!/usr/bin/env python3
"""Assemble one read-only review packet for a PR (Phase 1 of #794).

The review runner shows the model this packet and nothing else: the ticket
body, plan.md, the diff, CI state, the newest verdict and its head, the
changed-file overlap with every other open PR, protected-path touches, and
the stop-auto-merging counter. #798 assembled the evidence; #799 adds the
deterministic pre-check rows the runner evaluates before any model is
called. The model call itself comes later.

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

#: Conclusions GitHub reports for checks that passed or were excused, and the
#: states that mean a check has not reported one yet. Both come from
#: ``funnel``, which owns the single rollup reader the packet, the merge gate
#: and the review queue all share (#900).
CI_SUCCESS = funnel.CI_SUCCESS_CONCLUSIONS
CI_PENDING = funnel.CI_PENDING_STATES

#: Path prefixes the #794 freeze covers. A diff touching these fails the
#: freeze row unless the ticket's parent is #794.
FROZEN_PATHS = ("routines/", "skills/")

#: Parser constants scheduled for deletion by the #794 project: the union of
#: the Phase 2 prose list and #814's operative deletion list, minus the two
#: #814 explicitly keeps (RISK_LINE stays as a code-read of a code-written
#: line; HUMAN_STEP_LINE stays as the dual-read fallback until Phase 6). A
#: diff that adds or removes a line naming one of these fails the freeze row
#: unless the ticket's parent is #794.
FROZEN_PARSERS = (
    "SELF_APPROVED_LINE",
    "NEEDS_NATE_CLAUSE_END",
    "PROSE_DEPENDENCY_RE",
    "PLAN_HEADING",
    "PLAN_FUNCTION_RE",
    "PLAN_PATH_RE",
    "PLAN_ISSUE_RE",
    "PLAN_REF_RE",
    "OVERLAP_CHECK_SECTION_RE",
    "PROPOSED_CLASS_RE",
)

#: The plan whose own tickets may touch frozen ground.
FREEZE_PARENT_NUMBER = 794

#: Per-repo path rules that outrank the ticket's own Risk marker (#724,
#: enforced as a pre-check row per #794's disposition table, which superseded
#: the funnel.py routing change). Table shape, not an inline literal, so
#: later rules reuse it: a diff touching ``path`` in ``repo`` requires the
#: ticket to need ``tier``.
REPO_PATH_RULES = (
    {"repo": "nateprich-projects/workbench",
     "path": "chatgpt-messages-connector/",
     "tier": "escalated",
     "ref": "#724"},
)

#: How many merged PRs the merged-overlap row can see. Mirror of the open-PR
#: scan bound: a head older than this window may hide an overlap.
MERGED_PR_SCAN_LIMIT = 100


def ci_state(checks: Sequence[dict]) -> str:
    """Derive green/red/unknown from a statusCheckRollup list.

    The reading itself lives in ``funnel.ci_rollup_state``, so the packet, the
    merge gate and the review queue cannot disagree about what CI said. The
    rules are unchanged: any reported conclusion outside the success set is
    red, and anything unfinished — or no checks at all — is unknown rather
    than green, because an absent signal must never read as a passing one.
    """
    return funnel.ci_rollup_state(checks)


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


def head_date(pr_view: dict) -> Optional[str]:
    """The head commit's committed date, or None when it cannot be read.

    Prefer the commit matching the head SHA; fall back to the newest commit
    date when the head SHA is absent from the list. None only when the view
    carries no commit dates at all — the merged-overlap row then treats
    every fetched merge as newer, the fail-closed direction.
    """
    view = pr_view or {}
    commits = [entry for entry in (view.get("commits") or [])
               if isinstance(entry, dict)]
    if not commits:
        return None
    head = view.get("headRefOid") or ""
    for entry in commits:
        if entry.get("oid") == head and entry.get("committedDate"):
            return entry["committedDate"]
    dated = sorted(entry.get("committedDate") or "" for entry in commits)
    return dated[-1] or None


def changed_diff_lines(diff: str) -> List[str]:
    """Added and removed diff lines, without the +++ / --- headers."""
    lines = []
    for line in (diff or "").splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            lines.append(line[1:])
    return lines


def freeze_touches(changed_files: Sequence[str],
                   diff: str) -> Dict[str, List[str]]:
    """Frozen ground the diff touches: routine/skill paths, doomed parsers.

    Paths come from the changed-file list; parsers from added or removed diff
    lines naming a FROZEN_PARSERS constant. Unchanged context lines do not
    count — touching a parser means changing a line that names it.
    """
    paths = sorted({path for path in (changed_files or [])
                    if path.startswith(FROZEN_PATHS)})
    changed = changed_diff_lines(diff)
    parsers = sorted({name for name in FROZEN_PARSERS
                      if any(name in line for line in changed)})
    return {"paths": paths, "parsers": parsers}


def protected_rule_for(path: str) -> Optional[str]:
    """The PROTECTED_PATHS rule a path hits, or None."""
    for rule in PROTECTED_PATHS:
        if rule.endswith("/"):
            if path.startswith(rule):
                return rule
        elif path == rule:
            return rule
    return None


def ticket_asks(ticket: Optional[dict], path: str) -> bool:
    """Whether the ticket's title or body names the touched protected path.

    Naming the exact file or its rule both count: a ticket that says it will
    rewrite skills/shape asked for skills/shape/SKILL.md. No ticket, or a
    ticket silent on the path, means not asked.
    """
    if not ticket:
        return False
    text = "{}\n{}".format(ticket.get("title") or "",
                           ticket.get("body") or "")
    if path in text:
        return True
    rule = protected_rule_for(path)
    return bool(rule) and rule in text


def merged_overlap(candidate_files: Sequence[str],
                   merged_prs: Sequence[dict],
                   head_date_value: Optional[str],
                   candidate_pr: int) -> List[Dict]:
    """Merged PRs newer than the head that share a changed file.

    ``merged_prs`` rows carry number, mergedAt, and files [{path}]. A merge
    counts as newer when its mergedAt is strictly after the head date; when
    either timestamp is missing or unparseable it counts as newer, because an
    unorderable merge must block review rather than slip through. The
    candidate itself is excluded: reviewing an already-merged PR must not
    fail on its own files.
    """
    mine = set(candidate_files or [])
    head = funnel.parse_time(head_date_value) if head_date_value else None
    overlapping = []
    for row in merged_prs or []:
        if not isinstance(row, dict) or row.get("number") == candidate_pr:
            continue
        merged_at = row.get("mergedAt")
        if head is not None and merged_at:
            other = funnel.parse_time(merged_at)
            if other is not None and other <= head:
                continue
        theirs = {
            entry.get("path") for entry in (row.get("files") or [])
            if isinstance(entry, dict) and entry.get("path")
        }
        shared = sorted(mine & theirs)
        if shared:
            overlapping.append({
                "pr": row.get("number"),
                "merged_at": merged_at,
                "files": shared,
            })
    overlapping.sort(key=lambda entry: entry.get("pr") or 0)
    return overlapping


def precheck_freeze(packet: dict) -> List[str]:
    """Row 1: frozen ground needs a ticket under #794."""
    touches = freeze_touches(packet.get("changed_files"), packet.get("diff"))
    frozen = touches["paths"] + touches["parsers"]
    if not frozen:
        return []
    ticket = packet.get("ticket") or {}
    parent = ticket.get("parent") or {}
    if parent.get("number") == FREEZE_PARENT_NUMBER:
        return []
    if ticket.get("number") is None:
        where = "the PR has no ticket"
    elif parent.get("number") is None:
        where = "ticket {} has no parent".format(ticket.get("ref"))
    else:
        where = "ticket {} is under #{}, not #{}".format(
            ticket.get("ref"), parent.get("number"), FREEZE_PARENT_NUMBER)
    return ["freeze: {} touched but {}; frozen while #794 lands".format(
        ", ".join(frozen), where)]


def precheck_ci(packet: dict) -> List[str]:
    """Row 2: CI must be green — pending or absent checks fail, not pass."""
    ci = packet.get("ci") or {}
    if ci.get("state") == "green":
        return []
    failed = [check.get("name") for check in ci.get("checks") or []
              if isinstance(check, dict) and
              (check.get("conclusion") or check.get("state"))
              not in CI_SUCCESS + (None, "")]
    detail = ": {}".format(", ".join(failed)) if failed else ""
    return ["ci: CI not green (state {}){}".format(ci.get("state"), detail)]


def precheck_verdict(packet: dict) -> List[str]:
    """Row 3: a verdict already covering this head needs no new review."""
    if packet.get("verdict") is None:
        return []
    if packet.get("verdict_head_sha") != packet.get("head_sha"):
        return []
    return ["verdict: a verdict already covers head {}".format(
        str(packet.get("head_sha"))[:12])]


def precheck_merged_overlap(packet: dict) -> List[str]:
    """Row 4: a merge since the head may have made this PR stale."""
    return ["merged-overlap: PR #{} merged at {} touches {}".format(
                entry.get("pr"), entry.get("merged_at"),
                ", ".join(entry.get("files") or []))
            for entry in packet.get("merged_overlap") or []]


def precheck_protected(packet: dict) -> List[str]:
    """Row 5: protected paths need the ticket to ask for them.

    The resolved-path spelling the packet flags stays out of this row: the
    plan's pre-check table lists only touched paths, and the spelling stays
    visible in the packet for the model to read.
    """
    ticket = packet.get("ticket")
    reasons = []
    touched = (packet.get("protected") or {}).get("touched") or []
    for path in touched:
        if not ticket_asks(ticket, path):
            reasons.append(
                "protected: {} touched but the ticket does not ask for it"
                .format(path))
    return reasons


def precheck_stop(packet: dict) -> List[str]:
    """Row 6: the rejected-merges bar stops every review while set."""
    counter = packet.get("stop_auto_merging") or {}
    if not counter.get("stop_auto_merging"):
        return []
    refs = ", ".join(counter.get("refs") or [])
    return ["stop: stop_auto_merging set ({} rejected in {}d{})".format(
        counter.get("count"), counter.get("window_days"),
        ": {}".format(refs) if refs else "")]


def precheck_repo_rules(packet: dict) -> List[str]:
    """Row 7: per-repo path rules that outrank the ticket's Risk marker."""
    repo = packet.get("repo")
    changed = packet.get("changed_files") or []
    ticket = packet.get("ticket") or {}
    reasons = []
    for rule in REPO_PATH_RULES:
        if rule["repo"] != repo:
            continue
        if rule["path"].endswith("/"):
            touched = [path for path in changed
                       if path.startswith(rule["path"])]
        else:
            touched = [path for path in changed if path == rule["path"]]
        if not touched:
            continue
        tier = funnel.required_tier(ticket.get("title") or "",
                                    ticket.get("body") or "")
        if tier != rule["tier"]:
            reasons.append(
                "repo-rules: {} in {} is {}-only but the ticket is {} ({})"
                .format(rule["path"], repo, rule["tier"], tier, rule["ref"]))
    return reasons


def precheck(packet: dict) -> Dict[str, object]:
    """All seven rows in ticket order. Any reason fails the packet."""
    reasons: List[str] = []
    for row in (precheck_freeze, precheck_ci, precheck_verdict,
                precheck_merged_overlap, precheck_protected, precheck_stop,
                precheck_repo_rules):
        reasons.extend(row(packet or {}))
    return {"pass": not reasons, "reasons": reasons}


def build_packet(*, repo: str, pr_number: int, pr_view: dict, diff: str,
                 ticket: Optional[dict], plan_md: str,
                 plan_md_missing: bool, open_prs: Sequence[dict],
                 verdict: Optional[dict], stop_counter: dict,
                 collected_at: str,
                 merged_prs: Optional[Sequence[dict]] = None) -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO.

    Every field the review question needs, in one JSON-serialisable dict.
    ``ticket`` is None when the branch is not a ticket/<n> branch; the
    verdict's head_sha sits beside it so a reader can compare it with the
    current head without parsing the verdict. ``merged_prs`` rows carry
    number, mergedAt, and files [{path}]; None reads as no merges scanned.
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
            "body": None, "parent": None,
        }
    else:
        ticket_packet = {
            "ref": ticket.get("ref"),
            "number": ticket.get("number"),
            "title": ticket.get("title"),
            "url": ticket.get("url"),
            "body": ticket.get("body"),
            "parent": ticket.get("parent"),
        }
    head = head_date(pr_view)
    assembled = {
        "repo": repo,
        "pr": pr_number,
        "pr_title": pr_view.get("title"),
        "branch": pr_view.get("headRefName"),
        "base": pr_view.get("baseRefName"),
        "state": pr_view.get("state"),
        "mergeable": pr_view.get("mergeable"),
        "head_sha": pr_view.get("headRefOid"),
        "head_date": head,
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
        "merged_overlap": merged_overlap(changed_files, merged_prs, head,
                                         pr_number),
        "protected": protected_touches(changed_files, diff),
        "stop_auto_merging": stop_counter,
        "collected_at": collected_at,
    }
    assembled["precheck"] = precheck(assembled)
    return assembled


def fetch_pr(repo: str, pr_number: int) -> dict:
    """One PR view: identity, head, CI rollup, commits, changed files."""
    data = funnel._gh_json(
        "gh", "pr", "view", str(pr_number), "--repo", repo, "--json",
        "number,title,headRefName,headRefOid,baseRefName,state,mergeable,"
        "statusCheckRollup,commits,files")
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
    """The ticket body behind a ticket/<n> branch, with identity and parent."""
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo, "--json",
        "number,title,url,body,parent")
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


def fetch_merged_prs(repo: str,
                     limit: int = MERGED_PR_SCAN_LIMIT) -> List[dict]:
    """Recently merged PRs with their merge time and changed files."""
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "merged",
        "--json", "number,mergedAt,files", "--limit", str(limit))
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError(
            "could not list merged PRs in {}".format(repo))
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
        merged_prs=fetch_merged_prs(resolved),
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
