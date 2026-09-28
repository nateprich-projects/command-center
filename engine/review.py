#!/usr/bin/env python3
"""Assemble one read-only review packet for a PR (Phase 1 of #794).

The review runner shows the model this packet and nothing else: the ticket
body and its comments, the parent project's comments, PR review and issue
comments, the PR description and its Departures as the implementer's own
claims (#1720), plan.md, the diff, CI state, the newest verdict and its head,
the changed-file overlap with every other open PR, protected-path touches, the
stop-auto-merging counter, and the pull_request CI runs on the head. #798
assembled the evidence; #799 adds
the deterministic pre-check rows the runner evaluates before any model is
called. The model call itself comes later.

Read-only by construction: every GitHub call here is a view, list, diff, or
content read. Anything that changes remote state belongs in a later
``review-apply`` step, not here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402
from engine.shape import PREMISE_LABELS  # noqa: E402

#: Entries ending in "/" match a directory prefix; the rest match exactly.
#: A ticket PR touching any of these fails review unless its own ticket
#: asked for the change.
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

#: Path prefixes the #794 freeze covered. The review-side freeze row was
#: retired when #794 closed (#1362); these lists stay only because
#: ``funnel._canonical_freeze_lists`` still parses them, and go with it.
FROZEN_PATHS = ("routines/", "skills/")

#: Parser constants scheduled for deletion by the #794 project: the union of
#: the Phase 2 prose list and #814's operative deletion list, minus RISK_LINE,
#: which stays as a code-read of a code-written line. The Human step marker
#: (#826) is already deleted with its parser. A diff that adds or removes a
#: line naming one of these fails the freeze row unless the ticket's parent
#: is #794 or #1044.
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

#: The plans whose own tickets may touch frozen ground: #794, which owns
#: the freeze, and #1044, which exists only to measure #794's cutover (Nate,
#: 2026-09-18: #1044's routine edits record shadow-comparison instructions,
#: not behaviour changes, so holding them to the freeze would block the
#: measurement of the freeze's own project).
FREEZE_PARENT_NUMBERS = (794, 1044)

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

# The parent issue body is rendered by engine.shape.render_plan. Keep the
# reader on that stable line shape so review never has to rediscover the
# premises from free-form plan prose.
PLAN_PREMISES_HEADING_RE = re.compile(r"^## Premises[ \t]*$", re.MULTILINE)
PLAN_PREMISES_END_RE = re.compile(
    r"^(?:#{1,6}[ \t]+\S|Proposed class: )", re.MULTILINE)
PLAN_PREMISE_ROW_RE = re.compile(
    r"^- (?P<claim>.+?) \(label: (?P<label>[^;]+); "
    r"evidence: (?P<evidence>.+)\)$")

# Evidence pointers are prose, but ticket references inside them have the
# same forms GitHub renders elsewhere: owner/repo#n, #n, or an issue URL.
EVIDENCE_ISSUE_REF_RE = re.compile(
    r"(?P<url>https?://github\.com/(?P<url_owner>[A-Za-z0-9_.-]+)/"
    r"(?P<url_repo>[A-Za-z0-9_.-]+)/issues/"
    r"(?P<url_number>[1-9][0-9]*)/?)"
    r"|(?<![A-Za-z0-9_.-])(?P<full>(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*))"
    r"|(?<![A-Za-z0-9_/])#(?P<bare_number>[1-9][0-9]*)")
ISSUE_REF_RE = re.compile(
    r"\A(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"#(?P<number>[1-9][0-9]*)\Z")
ISSUE_URL_RE = re.compile(
    r"\Ahttps?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)/?\Z")

EVIDENCE_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      state
      blockedBy(first: 100, after: $after) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}
"""

PLAN_SUBISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}
"""

#: How many of the ticket's comments the packet carries, and how much of
#: each. The newest comments win: a decision recorded late in a long ticket
#: (#821 retiring the drift checks, which the shadow review of #985 missed
#: in #806) is exactly what the reviewer must see. Bodies past the cap are
#: cut with the same ``…[truncated N chars]`` mark review-apply uses, so a
#: long ticket cannot flood the prompt.
TICKET_COMMENT_LIMIT = 30
TICKET_COMMENT_BODY_LIMIT = 4000

# PR comments are durable run evidence. Keep each body bounded while carrying
# every comment, newest last, so a reviewer can see the complete conversation.
PR_COMMENT_BODY_LIMIT = 4000

# The PR description, and the Departures section engine/implement.py writes
# into it (#1720). Tickets ask for records "in the PR description" (#1659,
# #1660), and a packet without the description rejected PR #1667 at one head
# over and over for records its description held. The body is bounded like
# every other free text in the packet; the departures are parsed from the
# whole body, so a section past the cut still arrives. Both are the
# implementer's own claims: the note travels beside them in the packet so a
# judge reading the JSON alone still sees what they are.
PR_BODY_LIMIT = 20000
PR_CLAIMS_NOTE = (
    "pr_body and pr_departures are the implementer's own claims, not "
    "verified facts: pr_body is the PR description as its author wrote it, "
    "and pr_departures lists the entries of its Departures section. Cite "
    "them as evidence of what the author recorded and why; weigh every "
    "claim against the diff, and never count a departure as meeting its "
    "requirement by itself.")

# A Departures header is a label line (``Departures:``, bold or not, with or
# without text after the colon) or a Markdown heading (``## Departures``).
# Prose that merely starts with the word, such as "Departures were none",
# has neither the colon nor a line of its own and is not a header.
DEPARTURES_HEADER_RE = re.compile(
    r"^[ \t]{0,3}(?:(?P<heading>#{1,6})[ \t]+)?(?:\*\*|__)?Departures"
    r"(?:(?:\*\*|__)?[ \t]*:(?:\*\*|__)?|(?:\*\*|__)?[ \t]*$)"
    r"[ \t]*(?P<rest>.*?)[ \t]*$",
    re.IGNORECASE)
DEPARTURE_BULLET_RE = re.compile(
    r"^[ \t]{0,3}(?:[-*+]|[0-9]{1,3}[.)])[ \t]+(?P<text>.*)$")
# The next section ends the list: any Markdown heading, and after a
# ``Departures:`` label, the template's next unindented short label with a
# colon (``Branch:``, ``Local: ...``).
MARKDOWN_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]")
DEPARTURES_LABEL_END_RE = re.compile(
    r"^(?:\*\*|__)?[A-Z][A-Za-z0-9 /()'-]{0,40}?(?:\*\*|__)?:"
    r"(?:\*\*|__)?(?:[ \t]|$)")
# "- None." is how implement.py renders no departures; people write the
# same thing as "none", "n/a" or "no departures". "None of the tests ran"
# is a departure, so the word must stand alone or end at punctuation.
NO_DEPARTURE_RE = re.compile(
    r"^(?:none|n/a|no departures?(?: from the ticket)?)"
    r"[ \t]*(?:[.;:,!(—–-].*)?$",
    re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"^[ \t]{0,3}(?:```|~~~)")

# A canonical run-evidence comment is a marked, fenced JSON block. Parsing its
# shape helps the reviewer find the reported facts; it does not judge whether
# those facts satisfy a ticket requirement.
RUN_EVIDENCE_MARKER = "**Run evidence:**"
RUN_EVIDENCE_FIELDS = (
    "command", "exit_status", "output_summary", "environment_note",
)
RUN_EVIDENCE_FENCE_RE = re.compile(
    r"\A[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*```json[ \t]*\r?\n"
    r"(?P<payload>.*?)\r?\n[ \t]*```[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)

PR_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!,
      $issueCursor: String, $threadCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      issueComments: comments(first: 100, after: $issueCursor) {
        nodes { author { login } body createdAt }
        pageInfo { hasNextPage endCursor }
      }
      reviewThreads(first: 100, after: $threadCursor) {
        nodes {
          id
          comments(first: 100) {
            nodes { author { login } body createdAt }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

PR_REVIEW_THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $cursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        nodes { author { login } body createdAt }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
  rateLimit { cost remaining resetAt }
}
"""

#: How many pull_request runs the merged-overlap row can see. Newest first,
#: so a head whose runs fall outside this window reads as uncovered — the
#: fail-closed direction.
CI_RUN_SCAN_LIMIT = 20

#: The workflow event whose runs test the merge commit GitHub built for the
#: run rather than the branch head. Only these runs can cover an overlap:
#: a push run on the head never saw main at all.
CI_COVERING_EVENT = "pull_request"

# The escalated reviewer keeps each max call small enough to finish. The
# lister's output is canonical; judges receive slices of this list and never
# derive requirements of their own (#1233, #1242).
MAX_JUDGE_REQUIREMENTS = 3
JUDGE_REQUIREMENT_STATUSES = ("met", "unmet", "unsure")


class ReviewJudgeError(ValueError):
    """A judge answer could not be tied safely to its assigned requirements."""


def _canonical_requirements(requirements: Sequence[str]) -> List[str]:
    if isinstance(requirements, (str, bytes)) or not isinstance(
            requirements, (list, tuple)):
        raise ReviewJudgeError("requirements must be a list of strings")
    shaped = []
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, str) or not requirement.strip():
            raise ReviewJudgeError(
                "requirements[{}] must be a non-empty string".format(index))
        shaped.append(requirement.strip())
    return shaped


def chunk_requirements(requirements: Sequence[str],
                       chunk_size: int = MAX_JUDGE_REQUIREMENTS
                       ) -> List[List[str]]:
    """Split the lister's canonical requirements into bounded judge calls."""
    if (not isinstance(chunk_size, int) or isinstance(chunk_size, bool)
            or not 1 <= chunk_size <= MAX_JUDGE_REQUIREMENTS):
        raise ReviewJudgeError(
            "chunk_size must be between 1 and {}".format(
                MAX_JUDGE_REQUIREMENTS))
    canonical = _canonical_requirements(requirements)
    return [canonical[start:start + chunk_size]
            for start in range(0, len(canonical), chunk_size)]


def parse_judge_answer(raw: str,
                       expected_requirements: Sequence[str]
                       ) -> List[Dict[str, str]]:
    """Validate one judge's statuses and return them in canonical order.

    A judge may answer only the requirements assigned to it. Missing,
    duplicated, extra, or malformed entries make the whole chunk unusable so
    the runner can retry once and then fail closed for that chunk.
    """
    expected = _canonical_requirements(expected_requirements)
    if not expected:
        raise ReviewJudgeError("a judge must receive at least one requirement")
    if len(expected) > MAX_JUDGE_REQUIREMENTS:
        raise ReviewJudgeError(
            "a judge may receive at most {} requirements".format(
                MAX_JUDGE_REQUIREMENTS))
    if not (raw or "").strip():
        raise ReviewJudgeError("empty answer: expected a JSON object")
    try:
        answer = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReviewJudgeError("invalid JSON: {}".format(exc))
    if not isinstance(answer, dict):
        raise ReviewJudgeError("answer must be a JSON object")
    if set(answer) != {"requirements"}:
        raise ReviewJudgeError(
            "answer must contain only the 'requirements' key")
    entries = answer["requirements"]
    if not isinstance(entries, list):
        raise ReviewJudgeError("'requirements' must be a list")
    shaped = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {
                "requirement", "status", "evidence"}:
            raise ReviewJudgeError(
                "'requirements'[{}] must contain requirement, status, and "
                "evidence only".format(index))
        requirement = entry["requirement"]
        status = entry["status"]
        evidence = entry["evidence"]
        if not isinstance(requirement, str) or not requirement.strip():
            raise ReviewJudgeError(
                "'requirements'[{}].requirement must be a non-empty string"
                .format(index))
        if status not in JUDGE_REQUIREMENT_STATUSES:
            raise ReviewJudgeError(
                "'requirements'[{}].status must be one of {}".format(
                    index, list(JUDGE_REQUIREMENT_STATUSES)))
        if not isinstance(evidence, str) or not evidence.strip():
            raise ReviewJudgeError(
                "'requirements'[{}].evidence must be a non-empty string"
                .format(index))
        shaped.append({"requirement": requirement,
                       "status": status,
                       "evidence": evidence.strip()})

    if len(shaped) != len(expected):
        raise ReviewJudgeError(
            "judge returned {} requirement result(s) for {} assigned"
            .format(len(shaped), len(expected)))
    remaining = list(shaped)
    ordered = []
    for requirement in expected:
        matches = [entry for entry in remaining
                   if entry["requirement"] == requirement]
        if len(matches) != 1:
            raise ReviewJudgeError(
                "judge must answer {!r} exactly once".format(requirement))
        entry = matches[0]
        remaining.remove(entry)
        ordered.append(entry)
    if remaining:
        raise ReviewJudgeError("judge returned an unassigned requirement")
    return ordered


def uncertain_judge_results(requirements: Sequence[str],
                             evidence: str) -> List[Dict[str, str]]:
    """Represent a failed judge call as an unsure result for its whole chunk."""
    canonical = _canonical_requirements(requirements)
    detail = ((evidence or "").strip()
              or "judge call failed without diagnostic details")
    return [{"requirement": requirement, "status": "unsure",
             "evidence": detail}
            for requirement in canonical]


def derive_judge_answer(requirements: Sequence[str],
                        results: Sequence[dict]) -> Dict[str, object]:
    """Build the apply answer in code; any uncertainty rejects the review.

    Results arrive in chunk order. If the runner ever loses or corrupts an
    entry, that requirement becomes unsure instead of disappearing from the
    review. Extra entries also force rejection.
    """
    canonical = _canonical_requirements(requirements)
    if not canonical:
        raise ReviewJudgeError("cannot derive a verdict without requirements")
    if isinstance(results, (str, bytes)) or not isinstance(results, (list, tuple)):
        results = []
    records = list(results)
    shaped = []
    for index, requirement in enumerate(canonical):
        entry = records[index] if index < len(records) else None
        if (not isinstance(entry, dict)
                or set(entry) != {"requirement", "status", "evidence"}
                or entry.get("requirement") != requirement
                or entry.get("status") not in JUDGE_REQUIREMENT_STATUSES
                or not isinstance(entry.get("evidence"), str)
                or not entry.get("evidence", "").strip()):
            shaped.append({
                "requirement": requirement,
                "status": "unsure",
                "evidence": (
                    "No valid judge result was recorded for this requirement"
                ),
            })
        else:
            shaped.append({
                "requirement": requirement,
                "status": entry["status"],
                "evidence": entry["evidence"].strip(),
            })
    extra_count = max(0, len(records) - len(canonical))
    blocking = [
        "requirement {}: {} -- {}".format(
            entry["status"], entry["requirement"], entry["evidence"])
        for entry in shaped if entry["status"] != "met"
    ]
    if extra_count:
        blocking.append(
            "unexpected extra judge result(s): {}".format(extra_count))
    return {
        "verdict": "rejected" if blocking else "approved",
        "blocking": blocking,
        "unsure": [],
        "requirements": shaped,
    }


def ci_state(checks: Sequence[dict]) -> str:
    """Derive the shared CI state from a statusCheckRollup list.

    The reading itself lives in ``funnel.ci_rollup_state``, so the packet, the
    merge gate and the review queue cannot disagree about what CI said. The
    rules are unchanged: any reported conclusion outside the success set is
    red, and anything unfinished — or no checks at all — is unknown rather
    than green, because an absent signal must never read as a passing one.
    """
    return funnel.ci_rollup_state(checks)


def _rollup_with_actions_evidence(
        rollup: Sequence[dict], ci_runs: Sequence[dict],
        head_sha: Optional[str]) -> List[dict]:
    """Attach newest-run startup evidence to the PR's failed checks."""
    shaped = [dict(check) for check in rollup if isinstance(check, dict)]
    for run in ci_runs or []:
        if not isinstance(run, dict) or run.get("headSha") != head_sha:
            continue
        if funnel.ci_could_not_run_reason([run]) is None:
            continue
        evidence = {
            key: run[key]
            for key in ("annotations", "annotation", "completed_steps",
                        "completedSteps", "steps", "jobs")
            if key in run
        }
        if not evidence:
            continue
        attached = False
        for index, check in enumerate(shaped):
            result = check.get("conclusion") or check.get("state")
            if str(result or "").upper() in (
                "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"
            ):
                updated = dict(check)
                updated.update(evidence)
                shaped[index] = updated
                attached = True
        if not attached:
            shaped.append({
                "name": run.get("name") or "Actions",
                "conclusion": run.get("conclusion"),
                "status": run.get("status"),
                **evidence,
            })
        break
    return shaped


def summarize_checks(rollup: Sequence[dict]) -> List[Dict[str, object]]:
    """Keep the per-check fields a reviewer needs, in a stable shape."""
    summarized = []
    for check in rollup or []:
        if not isinstance(check, dict):
            continue
        row = {
            "name": check.get("name") or check.get("context"),
            "conclusion": check.get("conclusion"),
            "state": check.get("state"),
            "status": check.get("status"),
        }
        # The normal PR view has only the four stable fields above.  Preserve
        # optional Actions evidence when a read adapter supplies it so the
        # shared funnel classifier can distinguish a startup stop from red
        # test output without changing the ordinary packet shape.
        for key in ("annotations", "annotation", "completed_steps",
                    "completedSteps", "steps", "jobs"):
            if key in check:
                row[key] = check[key]
        summarized.append(row)
    return summarized


def ticket_comments(rows: Optional[Sequence[dict]]) -> List[Dict]:
    """The ticket's comments as the reviewer reads them, oldest first.

    Each row keeps ``author``, ``created_at``, ``voice`` and ``body``.
    ``voice`` is the ``command-center-provenance`` voice
    (``nate-direct``, ``nate-relayed``, ``agent``), or ``unknown`` when
    the comment carries no parseable marker — the GitHub login cannot
    establish it, since agents comment under Nate's account. The marker
    block is stripped from ``body``: it is machine text the reviewer must
    not re-read as prose. Only the newest TICKET_COMMENT_LIMIT rows are
    kept, and each body is capped at TICKET_COMMENT_BODY_LIMIT characters
    with the cut marked, so a long ticket cannot flood the prompt.
    """
    shaped = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        author = row.get("author")
        if isinstance(author, dict):
            author = author.get("login")
        elif not isinstance(author, str):
            author = None
        body = row.get("body") or ""
        provenance = funnel.parse_provenance(body)
        voice = provenance.get("voice") if provenance else "unknown"
        for _, block in funnel._marked_json_blocks(
                body, funnel.PROVENANCE_MARKER):
            body = body.replace(block, "")
        body = body.strip()
        if len(body) > TICKET_COMMENT_BODY_LIMIT:
            body = body[:TICKET_COMMENT_BODY_LIMIT] + (
                "\n…[truncated {} chars]".format(
                    len(body) - TICKET_COMMENT_BODY_LIMIT))
        shaped.append({
            "author": author,
            "created_at": row.get("createdAt") or row.get("created_at"),
            "voice": voice,
            "body": body,
        })
    shaped.sort(key=lambda entry: entry.get("created_at") or "")
    return shaped[-TICKET_COMMENT_LIMIT:]


def parse_ci_time(value: Optional[str]) -> Optional[datetime]:
    """Parse a workflow-run timestamp, tolerantly. None when unreadable.

    ``funnel.parse_time`` reads only whole-second ``Z`` stamps, which is
    all ``mergedAt`` ever carries. Run timestamps may carry fractional
    seconds, so this accepts those (and numeric offsets) rather than
    failing closed on a well-formed time. Unparseable still reads as
    missing, and a run with no readable start takes no part in coverage.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def summarize_runs(
        ci_runs: Sequence[dict],
        head_sha: Optional[str]) -> List[Dict[str, Optional[object]]]:
    """The pull_request runs on this head, newest first, in a stable shape.

    ``id`` is the workflow-run database id a re-run targets; ``started_at``
    is the latest attempt's start, which is what orders a run against a
    merge: GitHub builds the run's merge commit against then-current main,
    so a start after the merge means the tested base contains it. Runs on
    other heads, other events, and rubbish rows are dropped — a green push
    run never saw main, and another head's runs say nothing about this one.
    A packet without a head keeps no runs: unattributable runs must never
    cover an overlap.
    """
    if not head_sha:
        return []
    summarized = []
    for run in ci_runs or []:
        if not isinstance(run, dict):
            continue
        if str(run.get("event") or "").lower() != CI_COVERING_EVENT:
            continue
        if run.get("headSha") != head_sha:
            continue
        summarized.append({
            "id": run.get("databaseId"),
            "conclusion": run.get("conclusion"),
            "status": run.get("status"),
            "started_at": run.get("startedAt"),
        })

    def _started(entry: Dict[str, Optional[object]]) -> datetime:
        raw = entry.get("started_at")
        return (parse_ci_time(raw)  # type: ignore[arg-type]
                or datetime.min.replace(tzinfo=timezone.utc))

    summarized.sort(key=_started, reverse=True)
    return summarized


def green_run_at(runs: Sequence[dict]) -> Optional[str]:
    """The newest green run's start, or None. The coverage fact row 5 reads.

    Only completed success runs count, and only with a readable start: an
    unreadable start cannot be ordered against a merge, so the run takes no
    part rather than covering by assertion.
    """
    best: Optional[str] = None
    best_dt: Optional[datetime] = None
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        if str(run.get("status") or "").upper() != "COMPLETED":
            continue
        if str(run.get("conclusion") or "").upper() != "SUCCESS":
            continue
        started = parse_ci_time(run.get("started_at"))
        if started is None:
            continue
        if best_dt is None or started > best_dt:
            best_dt = started
            best = run.get("started_at")
    return best


def latest_completed_run_id(runs: Sequence[dict]) -> Optional[int]:
    """The newest completed run's id: the seed a CI re-run targets.

    Any conclusion seeds a re-run — ``gh run rerun`` rebuilds the whole
    run — but the run must be finished and its start must read, so the
    wait path can see the new attempt next time. None when no run
    qualifies; row 5 then rejects rather than queuing blind.
    """
    best_id: Optional[int] = None
    best_dt: Optional[datetime] = None
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        if str(run.get("status") or "").upper() != "COMPLETED":
            continue
        started = parse_ci_time(run.get("started_at"))
        if started is None:
            continue
        run_id = run.get("id")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            continue
        if best_dt is None or started > best_dt:
            best_dt = started
            best_id = run_id
    return best_id


def _run_newer_and_open(run: dict, merged_dt: datetime) -> bool:
    """A covering attempt already in flight: started after the merge,
    unfinished. The review waits for it instead of queuing another
    re-run — which is what keeps the re-run to once."""
    if not run.get("status"):
        return False
    if str(run.get("status")).upper() == "COMPLETED":
        return False
    started = parse_ci_time(run.get("started_at"))
    return started is not None and started > merged_dt


def _newest_open_run_id(runs: Sequence[dict]) -> Optional[int]:
    """The in-flight run to name in the wait note: newest start wins, an
    unorderable but identifiable run beats silence."""
    open_runs = [run for run in runs or []
                 if isinstance(run, dict)
                 and run.get("status")
                 and str(run.get("status")).upper() != "COMPLETED"
                 and isinstance(run.get("id"), int)
                 and not isinstance(run.get("id"), bool)]
    if not open_runs:
        return None

    def _started(run: dict) -> datetime:
        return (parse_ci_time(run.get("started_at"))
                or datetime.min.replace(tzinfo=timezone.utc))

    return max(open_runs, key=_started).get("id")


def _overlap_facts(packet: dict) -> Dict[str, object]:
    """The shared inputs row 5 and the re-run decision both read."""
    ci = packet.get("ci") or {}
    seed = ci.get("latest_run_id")
    return {
        "clean": str(packet.get("mergeable") or "").upper() == "MERGEABLE",
        "green_dt": parse_ci_time(ci.get("green_run_at")),
        "runs": [run for run in (ci.get("runs") or [])
                 if isinstance(run, dict)],
        "seed": (seed if isinstance(seed, int)
                 and not isinstance(seed, bool) else None),
    }


def _overlap_hold(entry: dict, facts: Dict[str, object]) -> str:
    """One overlap's standing: covered, rerun, wait, or blocked.

    Covered needs a green run newer than the merge. A clean branch with
    older green runs is rerun when a finished run seeds it, wait when a
    newer attempt is already in flight. Everything else blocks: a
    conflicting or uncomputed branch, an unorderable merge, and an
    uncovered overlap with no run to re-run all reject as stale.
    """
    if not facts["clean"]:
        return "blocked"
    merged_dt = parse_ci_time(entry.get("merged_at"))
    if merged_dt is None:
        return "blocked"
    green_dt = facts["green_dt"]
    assert green_dt is None or isinstance(green_dt, datetime)
    if green_dt is not None and green_dt > merged_dt:
        return "covered"
    runs = facts["runs"]
    assert isinstance(runs, list)
    if any(_run_newer_and_open(run, merged_dt) for run in runs):
        return "wait"
    if facts["seed"] is not None:
        return "rerun"
    return "blocked"


def _stale_reason(entry: dict) -> str:
    return "merged-overlap: PR #{} merged at {} touches {}".format(
        entry.get("pr"), entry.get("merged_at"),
        ", ".join(entry.get("files") or []))


def decide_ci_rerun(packet: dict) -> Optional[Dict[str, object]]:
    """The CI re-run the merged-overlap row asks for, if any. Pure.

    Set only when no overlap blocks yet some overlap is uncovered: every
    merge is either covered by a green run newer than it or clean with a
    re-runnable or in-flight run. ``rerun`` names the finished run to
    rebuild; ``wait`` names the attempt already in flight. ``overlaps``
    names the uncovered PRs either way. None covers the rest: no overlap,
    all covered, or any overlap stale — the last is a rejection, not a
    re-run. The runner acts on this only when the whole precheck passes;
    a failing row rejects first and no re-run is requested.
    """
    overlaps = [entry for entry in (packet.get("merged_overlap") or [])
                if isinstance(entry, dict)]
    if not overlaps:
        return None
    facts = _overlap_facts(packet)
    holds = [(entry, _overlap_hold(entry, facts)) for entry in overlaps]
    if any(hold == "blocked" for _, hold in holds):
        return None
    uncovered = [(entry, hold) for entry, hold in holds if hold != "covered"]
    if not uncovered:
        return None
    prs = [entry.get("pr") for entry, _ in uncovered]
    if any(hold == "rerun" for _, hold in uncovered):
        return {"action": "rerun", "run_id": facts["seed"], "overlaps": prs}
    runs = facts["runs"]
    assert isinstance(runs, list)
    return {"action": "wait", "run_id": _newest_open_run_id(runs),
            "overlaps": prs}


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


def ticket_prior_prs(branch: Optional[str],
                     merged_prs: Sequence[dict],
                     candidate_pr: int) -> List[Dict]:
    """Already-merged PRs delivering the same ticket, oldest first.

    A ticket is often delivered in several PRs off one ``ticket/<n>``
    branch: #902 took three. Each later PR carries only its own slice, so a
    reviewer handed the whole ticket body and one slice will reject it for
    the work the earlier slices already landed — which is what happened to
    #907 (see #908). These rows are what lets the review question ask what
    the diff *adds*.

    Matched on the head branch rather than on closing keywords: the branch
    is what the runner derives the ticket from in the first place, and it is
    already in every row. ``merged_overlap`` cannot serve here — it looks
    only at merges strictly newer than the candidate's head, and a prior
    slice is by definition older.
    """
    if not branch:
        return []
    prior = []
    for row in merged_prs or []:
        if not isinstance(row, dict) or row.get("number") == candidate_pr:
            continue
        if (row.get("headRefName") or "") != branch:
            continue
        prior.append({
            "pr": row.get("number"),
            "title": row.get("title"),
            "merged_at": row.get("mergedAt"),
            "files": sorted({
                entry.get("path") for entry in (row.get("files") or [])
                if isinstance(entry, dict) and entry.get("path")
            }),
        })
    prior.sort(key=lambda entry: (entry.get("merged_at") or "",
                                  entry.get("pr") or 0))
    return prior


def precheck_pr_open(packet: dict) -> List[str]:
    """Row 1: only an open PR can receive a review verdict.

    A merged or closed PR has no live judgement left to make. Keep the
    reason stable and distinguishable from a model rejection so downstream
    shadow reporting can omit this scheduling race from agreement.
    """
    state = str(packet.get("state") or "").upper()
    if state == "OPEN":
        return []
    if not state:
        state = "UNKNOWN"
    merged_at = packet.get("merged_at") or packet.get("mergedAt")
    if merged_at:
        timestamp_name = "merged_at"
        timestamp = merged_at
    else:
        timestamp_name = "closed_at"
        timestamp = (
            packet.get("closed_at") or packet.get("closedAt") or "unknown"
        )
    return ["pr_not_open state={} {}={}".format(
        state, timestamp_name, timestamp)]


def precheck_ci(packet: dict) -> List[str]:
    """Row 3: green passes; pending/red fail; startup stops stand down."""
    ci = packet.get("ci") or {}
    if ci.get("state") in ("green", funnel.CI_COULD_NOT_RUN):
        # ``could-not-run`` is not approval evidence, but it is also not a
        # review judgement.  The runner sees the state and stands down without
        # recording a rejection, so the account/startup cause can recover.
        return []
    failed = [check.get("name") for check in ci.get("checks") or []
              if isinstance(check, dict) and
              (check.get("conclusion") or check.get("state"))
              not in CI_SUCCESS + (None, "")]
    detail = ": {}".format(", ".join(failed)) if failed else ""
    return ["ci: CI not green (state {}){}".format(ci.get("state"), detail)]


def precheck_verdict(packet: dict) -> List[str]:
    """Row 4: a verdict already covering this head needs no new review."""
    comments_section = packet.get("pr_comments")
    comments = (
        comments_section.get("comments")
        if isinstance(comments_section, dict)
        and isinstance(comments_section.get("comments"), list)
        else None
    )
    if not funnel.verdict_covers_head(
        packet.get("verdict"), packet.get("head_sha"), comments
    ):
        return []
    return ["verdict: a verdict already covers head {}".format(
        str(packet.get("head_sha"))[:12])]


def precheck_merged_overlap(packet: dict) -> List[str]:
    """Row 5: a merge since the head may have made this PR stale.

    A newer overlapping merge blocks unless the PR proves it harmless:
    GitHub reports the branch MERGEABLE and a green pull_request run
    started after the merge, so its tested merge commit was built against
    a main containing the overlap (#1019, Nate 2026-09-17). A clean branch
    whose green runs all predate the merge is not rejected here: the
    runner re-runs CI once and waits (see ``decide_ci_rerun``), so the
    engineer needs no rebase. Anything else — conflicting, uncomputed, or
    uncovered with no run to re-run — rejects as stale, as before.
    """
    overlaps = [entry for entry in (packet.get("merged_overlap") or [])
                if isinstance(entry, dict)]
    facts = _overlap_facts(packet)
    return [_stale_reason(entry) for entry in overlaps
            if _overlap_hold(entry, facts) == "blocked"]


def precheck_protected(packet: dict) -> List[str]:
    """Row 6: protected paths need the ticket to ask for them.

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
    """Row 7: the rejected-merges bar stops every review while set."""
    counter = packet.get("stop_auto_merging") or {}
    if not counter.get("stop_auto_merging"):
        return []
    refs = ", ".join(counter.get("refs") or [])
    return ["stop: stop_auto_merging set ({} rejected in {}d{})".format(
        counter.get("count"), counter.get("window_days"),
        ": {}".format(refs) if refs else "")]


def precheck_repo_rules(packet: dict) -> List[str]:
    """Row 8: per-repo path rules that outrank the ticket's Risk field."""
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
        tier = ticket.get("risk")
        if tier not in funnel.RISK_OPTIONS:
            tier = "unset"
        if tier != rule["tier"]:
            reasons.append(
                "repo-rules: {} in {} is {}-only but the ticket is {} ({})"
                .format(rule["path"], repo, rule["tier"], tier, rule["ref"]))
    return reasons


def precheck(packet: dict) -> Dict[str, object]:
    """All seven rows in ticket order. Any reason fails the packet.

    The freeze row was retired when #794 closed (#1362); the queue-side
    predicate had already gone inert with it.
    """
    reasons: List[str] = []
    for row in (precheck_pr_open, precheck_ci,
                precheck_verdict,
                precheck_merged_overlap, precheck_protected, precheck_stop,
                precheck_repo_rules):
        reasons.extend(row(packet or {}))
    return {"pass": not reasons, "reasons": reasons}


def parse_plan_premises(body: object) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Read the fixed premises section rendered into a parent plan body.

    Plans written before #1422 have no section; that is a valid empty list.
    A present but malformed section is reported to the reviewer instead of
    silently dropping a premise.
    """
    if not isinstance(body, str):
        return [], "parent plan body is unavailable"
    headings = list(PLAN_PREMISES_HEADING_RE.finditer(body))
    if not headings:
        return [], None
    if len(headings) != 1:
        return [], "parent plan has multiple Premises sections"

    section = body[headings[0].end():]
    end = PLAN_PREMISES_END_RE.search(section)
    if end:
        section = section[:end.start()]

    premises: List[Dict[str, str]] = []
    empty_marker = False
    for line_number, line in enumerate(section.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        if text == "None recorded.":
            empty_marker = True
            continue
        match = PLAN_PREMISE_ROW_RE.fullmatch(text)
        if not match:
            return [], "parent plan Premises line {} is not in the rendered format".format(
                line_number)
        label = match.group("label")
        if label not in PREMISE_LABELS:
            return [], "parent plan Premises line {} has an unknown label".format(
                line_number)
        claim = match.group("claim").strip()
        evidence = match.group("evidence").strip()
        if not claim or not evidence:
            return [], "parent plan Premises line {} is incomplete".format(
                line_number)
        premises.append({"claim": claim, "evidence": evidence,
                         "label": label})

    if empty_marker and premises:
        return [], "parent plan Premises section mixes entries with None recorded"
    return premises, None


def packet_plan_premises(tickets: Sequence[Optional[dict]]) -> List[Dict]:
    """Group structured premises by ticket parent for a fixed packet section.

    Multiple tickets can share a project plan, or one PR can close tickets
    from different plans. Keep those sources explicit and preserve an
    unavailable state distinct from a legacy plan with no premises.
    """
    grouped: Dict[object, Dict] = {}
    for ticket in tickets:
        if not isinstance(ticket, dict):
            continue
        parent = ticket.get("parent")
        if not isinstance(parent, dict):
            continue
        ticket_ref = ticket.get("ref")
        parent_number = parent.get("number")
        parent_repo = parent_repo_from_row(parent)
        parent_ref = parent.get("ref")
        if not isinstance(parent_ref, str):
            if parent_repo and isinstance(parent_number, int):
                parent_ref = "{}#{}".format(parent_repo, parent_number)
            else:
                parent_ref = parent.get("url")
        if not isinstance(parent_ref, str):
            parent_ref = None
        key: object = parent_ref or ("unresolved", parent_number, ticket_ref)

        body = parent.get("body")
        if parent.get("body_unavailable"):
            body = None
        premises, error = parse_plan_premises(body)
        group = grouped.get(key)
        if group is None:
            group = {
                "parent_ref": parent_ref,
                "ticket_refs": [],
                "available": error is None,
                "premises": premises if error is None else [],
            }
            if error is not None:
                group["error"] = error
            grouped[key] = group
        elif error is None and not group["available"]:
            # Another ticket in the same PR may have supplied the same
            # parent body successfully after an earlier read degraded.
            group["available"] = True
            group["premises"] = premises
            group.pop("error", None)
        elif (error is None and group["available"]
              and group["premises"] != premises):
            group["available"] = False
            group["premises"] = []
            group["error"] = "parent plan changed during packet collection"
        if isinstance(ticket_ref, str) and ticket_ref not in group["ticket_refs"]:
            group["ticket_refs"].append(ticket_ref)
    return list(grouped.values())


def _issue_ref_parts(value: object, default_repo: Optional[str] = None
                     ) -> Optional[Tuple[str, int]]:
    """Split a GitHub issue ref or URL, with bare #n scoped to a repo."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = ISSUE_REF_RE.fullmatch(text)
    if match:
        return "{}/{}".format(match.group("owner"), match.group("repo")), int(
            match.group("number"))
    match = ISSUE_URL_RE.fullmatch(text)
    if match:
        return "{}/{}".format(match.group("owner"), match.group("repo")), int(
            match.group("number"))
    if default_repo:
        match = re.fullmatch(r"#([1-9][0-9]*)", text)
        if match:
            return default_repo, int(match.group(1))
    return None


def _issue_ref(repo: str, number: int) -> str:
    return "{}#{}".format(repo, number)


def _issue_ref_key(ref: str) -> Tuple[str, int]:
    parts = _issue_ref_parts(ref)
    if parts is None:
        raise funnel.GitHubError("GitHub returned an invalid issue ref")
    repo, number = parts
    return repo.casefold(), number


def _evidence_issue_refs(evidence_pointer: object,
                         default_repo: str) -> List[str]:
    """Find explicit and repo-local issue refs in one evidence pointer."""
    if not isinstance(evidence_pointer, str):
        return []
    refs: List[str] = []
    seen = set()
    for match in EVIDENCE_ISSUE_REF_RE.finditer(evidence_pointer):
        if match.group("url_number"):
            repo = "{}/{}".format(match.group("url_owner"),
                                  match.group("url_repo"))
            number = int(match.group("url_number"))
        elif match.group("number"):
            repo = "{}/{}".format(match.group("owner"),
                                  match.group("repo"))
            number = int(match.group("number"))
        else:
            repo = default_repo
            number = int(match.group("bare_number"))
        ref = _issue_ref(repo, number)
        key = _issue_ref_key(ref)
        if key not in seen:
            seen.add(key)
            refs.append(ref)
    return refs


def _read_evidence_issue(ref: str) -> Optional[Dict[str, object]]:
    """Read one issue's current state and complete native blocked-by edges."""
    parts = _issue_ref_parts(ref)
    if parts is None:
        raise funnel.GitHubError("cannot read an invalid evidence ticket ref")
    repo, number = parts
    owner, name = repo.split("/", 1)
    after: Optional[str] = None
    state: Optional[str] = None
    blockers: List[str] = []
    blocker_keys = set()
    total_count: Optional[int] = None
    while True:
        variables = {"owner": owner, "name": name, "number": number}
        if after is not None:
            variables["after"] = after
        payload = funnel.gh_graphql(EVIDENCE_ISSUE_QUERY, **variables)
        repository = payload.get("repository") if isinstance(payload, dict) else None
        issue = repository.get("issue") if isinstance(repository, dict) else None
        if issue is None:
            if after is None:
                return None
            raise funnel.GitHubError(
                "evidence ticket changed during dependency read")
        page_state = issue.get("state")
        if page_state not in ("OPEN", "CLOSED"):
            raise funnel.GitHubError(
                "evidence ticket has an unreadable state")
        if state is None:
            state = page_state
        elif state != page_state:
            raise funnel.GitHubError(
                "evidence ticket changed during dependency read")
        connection = issue.get("blockedBy")
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
        count = connection.get("totalCount") if isinstance(connection, dict) else None
        if (not isinstance(nodes, list)
                or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)
                or not isinstance(count, int) or isinstance(count, bool)
                or count < 0):
            raise funnel.GitHubError(
                "evidence ticket has an incomplete blocked-by connection")
        if total_count is None:
            total_count = count
        elif total_count != count:
            raise funnel.GitHubError(
                "evidence ticket dependencies changed during read")
        for blocker in nodes:
            if not isinstance(blocker, dict):
                raise funnel.GitHubError(
                    "evidence ticket has an invalid blocked-by edge")
            blocker_repo = (blocker.get("repository") or {}).get(
                "nameWithOwner")
            blocker_number = blocker.get("number")
            if (not isinstance(blocker_repo, str)
                    or not isinstance(blocker_number, int)
                    or isinstance(blocker_number, bool)
                    or blocker_number < 1):
                raise funnel.GitHubError(
                    "evidence ticket has an invalid blocked-by edge")
            blocker_ref = _issue_ref(blocker_repo, blocker_number)
            blocker_key = _issue_ref_key(blocker_ref)
            if blocker_key in blocker_keys:
                raise funnel.GitHubError(
                    "evidence ticket repeated a blocked-by edge")
            blocker_keys.add(blocker_key)
            blockers.append(blocker_ref)
        if not page_info["hasNextPage"]:
            if total_count != len(blockers):
                raise funnel.GitHubError(
                    "evidence ticket has a partial blocked-by connection")
            return {"state": state, "blocked_by": blockers}
        next_after = page_info.get("endCursor")
        if not isinstance(next_after, str) or not next_after or next_after == after:
            raise funnel.GitHubError(
                "evidence ticket has an invalid blocked-by cursor")
        after = next_after


def _read_issue_parent_ref(ref: str) -> Optional[str]:
    """Read an issue's live parent, if it has one."""
    parts = _issue_ref_parts(ref)
    if parts is None:
        raise funnel.GitHubError("cannot read an invalid ticket ref")
    repo, number = parts
    parent = funnel._gh_json(
        "gh", "api", "repos/{}/issues/{}/parent".format(repo, number))
    if not isinstance(parent, dict):
        return None
    parent_number = parent.get("number")
    repository = parent.get("repository")
    parent_repo = (repository.get("full_name")
                   if isinstance(repository, dict) else None)
    if not isinstance(parent_repo, str) or "/" not in parent_repo:
        parent_repo = repo
    if (not isinstance(parent_number, int)
            or isinstance(parent_number, bool)
            or parent_number < 1):
        return None
    return _issue_ref(parent_repo, parent_number)


def _read_plan_ticket_order(plan_ref: str) -> List[str]:
    """Read the plan's sub-issues in GitHub's returned order."""
    parts = _issue_ref_parts(plan_ref)
    if parts is None:
        raise funnel.GitHubError("cannot read an invalid plan ref")
    repo, number = parts
    owner, name = repo.split("/", 1)
    after: Optional[str] = None
    refs: List[str] = []
    seen = set()
    while True:
        variables = {"owner": owner, "name": name, "number": number}
        if after is not None:
            variables["after"] = after
        payload = funnel.gh_graphql(PLAN_SUBISSUES_QUERY, **variables)
        repository = payload.get("repository") if isinstance(payload, dict) else None
        issue = repository.get("issue") if isinstance(repository, dict) else None
        if issue is None:
            if after is None:
                return []
            raise funnel.GitHubError("plan changed during sub-issue read")
        connection = issue.get("subIssues")
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
        if (not isinstance(nodes, list)
                or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)):
            raise funnel.GitHubError("plan has an incomplete sub-issue connection")
        for child in nodes:
            if not isinstance(child, dict):
                raise funnel.GitHubError("plan has an invalid sub-issue")
            child_repo = (child.get("repository") or {}).get("nameWithOwner")
            child_number = child.get("number")
            if (not isinstance(child_repo, str)
                    or not isinstance(child_number, int)
                    or isinstance(child_number, bool)
                    or child_number < 1):
                raise funnel.GitHubError("plan has an invalid sub-issue")
            child_ref = _issue_ref(child_repo, child_number)
            child_key = _issue_ref_key(child_ref)
            if child_key in seen:
                raise funnel.GitHubError("plan repeated a sub-issue")
            seen.add(child_key)
            refs.append(child_ref)
        if not page_info["hasNextPage"]:
            return refs
        next_after = page_info.get("endCursor")
        if not isinstance(next_after, str) or not next_after or next_after == after:
            raise funnel.GitHubError("plan has an invalid sub-issue cursor")
        after = next_after


def _ticket_is_later_sibling(reviewed_ref: str, evidence_ref: str,
                             parent_cache: Dict[Tuple[str, int], Optional[str]],
                             order_cache: Dict[Tuple[str, int], List[str]]) -> bool:
    """Whether an open evidence ticket follows the reviewed ticket in its plan."""
    reviewed_key = _issue_ref_key(reviewed_ref)
    evidence_key = _issue_ref_key(evidence_ref)
    if reviewed_key not in parent_cache:
        parent_cache[reviewed_key] = _read_issue_parent_ref(reviewed_ref)
    if evidence_key not in parent_cache:
        parent_cache[evidence_key] = _read_issue_parent_ref(evidence_ref)
    reviewed_parent = parent_cache[reviewed_key]
    evidence_parent = parent_cache[evidence_key]
    if (not reviewed_parent or not evidence_parent
            or _issue_ref_key(reviewed_parent) != _issue_ref_key(evidence_parent)):
        return False
    parent_key = _issue_ref_key(reviewed_parent)
    if parent_key not in order_cache:
        order_cache[parent_key] = _read_plan_ticket_order(reviewed_parent)
    positions = {
        _issue_ref_key(ticket_ref): position
        for position, ticket_ref in enumerate(order_cache[parent_key])
    }
    reviewed_position = positions.get(reviewed_key)
    evidence_position = positions.get(evidence_key)
    return (reviewed_position is not None and evidence_position is not None
            and evidence_position > reviewed_position)


def _has_dependency_path(evidence: Dict[str, object], reviewed_ref: str,
                         issue_cache: Dict[Tuple[str, int], Optional[Dict[str, object]]]
                         ) -> bool:
    """Walk native blocked-by edges from evidence toward its prerequisites."""
    reviewed_key = _issue_ref_key(reviewed_ref)
    queue = list(evidence.get("blocked_by") or [])
    visited = set()
    while queue:
        blocker_ref = queue.pop(0)
        if not isinstance(blocker_ref, str):
            continue
        blocker_key = _issue_ref_key(blocker_ref)
        if blocker_key == reviewed_key:
            return True
        if blocker_key in visited:
            continue
        visited.add(blocker_key)
        if blocker_key not in issue_cache:
            issue_cache[blocker_key] = _read_evidence_issue(blocker_ref)
        blocker = issue_cache[blocker_key]
        if blocker is not None:
            queue.extend(blocker.get("blocked_by") or [])
    return False


def evidence_ticket_is_unrunnable(evidence_pointer: object,
                                  reviewed_ticket_ref: str) -> bool:
    """Whether an open ticket named by evidence cannot run before this review.

    Bare ``#n`` pointers are scoped to the reviewed ticket's repository.
    An open evidence ticket is deferred only when GitHub's live sub-issue
    order puts it later in the same plan, or its native blocked-by graph
    reaches the ticket under review. A closed or missing issue does not defer
    the premise, so genuinely checkable evidence remains a review requirement.
    """
    reviewed_parts = _issue_ref_parts(reviewed_ticket_ref)
    if reviewed_parts is None:
        raise ValueError("reviewed_ticket_ref must be a GitHub issue ref")
    reviewed_repo, reviewed_number = reviewed_parts
    reviewed_ref = _issue_ref(reviewed_repo, reviewed_number)
    reviewed_key = _issue_ref_key(reviewed_ref)
    issue_cache: Dict[Tuple[str, int], Optional[Dict[str, object]]] = {}
    parent_cache: Dict[Tuple[str, int], Optional[str]] = {}
    order_cache: Dict[Tuple[str, int], List[str]] = {}
    for evidence_ref in _evidence_issue_refs(evidence_pointer, reviewed_repo):
        evidence_key = _issue_ref_key(evidence_ref)
        if evidence_key == reviewed_key:
            continue
        if evidence_key not in issue_cache:
            issue_cache[evidence_key] = _read_evidence_issue(evidence_ref)
        evidence = issue_cache[evidence_key]
        if evidence is None or evidence.get("state") != "OPEN":
            continue
        if _ticket_is_later_sibling(reviewed_ref, evidence_ref,
                                    parent_cache, order_cache):
            return True
        if _has_dependency_path(evidence, reviewed_ref, issue_cache):
            return True
    return False


def annotate_unrunnable_premises(packet: Dict) -> Dict:
    """Record live-verified deferrals and premise-label errors in a packet.

    ``build_packet`` stays pure. ``collect`` calls this after assembling its
    packet so only live GitHub state can add ``deferred_answer`` for inferred
    premises or ``label_error`` for measured/documented premises. Unavailable
    plan groups and unresolved issue reads keep the existing review path.
    """
    ticket = packet.get("ticket")
    reviewed_ref = ticket.get("ref") if isinstance(ticket, dict) else None
    if (not isinstance(reviewed_ref, str)
            or _issue_ref_parts(reviewed_ref) is None):
        return packet

    groups = packet.get("plan_premises")
    if not isinstance(groups, list):
        return packet

    checked: Dict[str, bool] = {}
    for group in groups:
        if not isinstance(group, dict) or group.get("available") is not True:
            continue
        premises = group.get("premises")
        if not isinstance(premises, list):
            continue
        for premise in premises:
            if not isinstance(premise, dict):
                continue
            label = premise.get("label")
            if label not in {"inferred", "measured", "documented"}:
                continue
            evidence = premise.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                continue
            if evidence not in checked:
                try:
                    checked[evidence] = evidence_ticket_is_unrunnable(
                        evidence, reviewed_ref)
                except funnel.GitHubError:
                    # No verified forward pointer: preserve the probe path.
                    checked[evidence] = False
            if checked[evidence]:
                if label == "inferred":
                    premise["deferred_answer"] = {
                        "status": "deferred",
                        "evidence_pointer": evidence,
                        "reviewed_ticket": reviewed_ref,
                        "reason": (
                            "live issue state shows the named evidence ticket "
                            "is open and cannot run before the reviewed ticket "
                            "is complete"
                        ),
                    }
                else:
                    premise["label_error"] = {
                        "status": "verified",
                        "label": label,
                        "evidence_pointer": evidence,
                        "reviewed_ticket": reviewed_ref,
                        "reason": (
                            "live issue state shows the named evidence ticket "
                            "is open and cannot run before the reviewed ticket "
                            "is complete"
                        ),
                    }
    return packet


def _verified_deferred_premises(packet: Dict) -> List[Dict[str, str]]:
    """Return only deferrals whose packet fields match the live premise."""
    ticket = packet.get("ticket")
    reviewed_ref = ticket.get("ref") if isinstance(ticket, dict) else None
    if not isinstance(reviewed_ref, str):
        return []
    groups = packet.get("plan_premises")
    if not isinstance(groups, list):
        return []
    verified: List[Dict[str, str]] = []
    for group in groups:
        if not isinstance(group, dict) or group.get("available") is not True:
            continue
        premises = group.get("premises")
        if not isinstance(premises, list):
            continue
        for premise in premises:
            if (not isinstance(premise, dict)
                    or premise.get("label") != "inferred"):
                continue
            claim = premise.get("claim")
            evidence = premise.get("evidence")
            deferred = premise.get("deferred_answer")
            if (not isinstance(claim, str) or not claim
                    or not isinstance(evidence, str) or not evidence
                    or not isinstance(deferred, dict)
                    or deferred.get("status") != "deferred"
                    or deferred.get("evidence_pointer") != evidence
                    or deferred.get("reviewed_ticket") != reviewed_ref):
                continue
            verified.append({
                "claim": claim,
                "evidence": evidence,
                "reviewed_ticket": reviewed_ref,
            })
    return verified


def _deferred_premise_requirement(premise: Dict[str, str]) -> str:
    parts = _issue_ref_parts(premise["reviewed_ticket"])
    reviewed_ticket = ("#{}".format(parts[1]) if parts
                       else premise["reviewed_ticket"])
    return ("Defer the inferred premise '{}' to its evidence pointer '{}' "
            "until ticket {} is complete.").format(
        premise["claim"], premise["evidence"], reviewed_ticket)


def _verified_label_error_premises(packet: Dict) -> List[Dict[str, str]]:
    """Return only live-verified measured/documented forward-pointer errors."""
    ticket = packet.get("ticket")
    reviewed_ref = ticket.get("ref") if isinstance(ticket, dict) else None
    if not isinstance(reviewed_ref, str):
        return []
    groups = packet.get("plan_premises")
    if not isinstance(groups, list):
        return []
    verified: List[Dict[str, str]] = []
    for group in groups:
        if not isinstance(group, dict) or group.get("available") is not True:
            continue
        premises = group.get("premises")
        if not isinstance(premises, list):
            continue
        for premise in premises:
            if not isinstance(premise, dict):
                continue
            label = premise.get("label")
            claim = premise.get("claim")
            evidence = premise.get("evidence")
            error = premise.get("label_error")
            if (label not in {"measured", "documented"}
                    or not isinstance(claim, str) or not claim
                    or not isinstance(evidence, str) or not evidence
                    or not isinstance(error, dict)
                    or error.get("status") != "verified"
                    or error.get("label") != label
                    or error.get("evidence_pointer") != evidence
                    or error.get("reviewed_ticket") != reviewed_ref):
                continue
            verified.append({
                "claim": claim,
                "evidence": evidence,
                "label": label,
                "reviewed_ticket": reviewed_ref,
            })
    return verified


def _label_error_premise_requirement(premise: Dict[str, str]) -> str:
    parts = _issue_ref_parts(premise["reviewed_ticket"])
    reviewed_ticket = ("#{}".format(parts[1]) if parts
                       else premise["reviewed_ticket"])
    return ("Reject the {} premise '{}' as a labeling error because its "
            "evidence pointer '{}' names an open ticket that cannot run "
            "before ticket {} is complete.").format(
                premise["label"], premise["claim"], premise["evidence"],
                reviewed_ticket)


def normalize_plan_premise_requirements(
        packet: Dict, requirements: Sequence[str]) -> List[str]:
    """Replace model probe requirements with verified canonical premise rows.

    The runner verifies these fields itself, so a lister wording lapse cannot
    turn a verified deferral into an unsure probe or hide a verified label
    error behind one.
    """
    deferred = _verified_deferred_premises(packet)
    label_errors = _verified_label_error_premises(packet)
    verified = deferred + label_errors
    if not verified:
        return list(requirements)

    kept: List[str] = []
    for requirement in requirements:
        text = requirement.casefold()
        if any(premise["claim"].casefold() in text
               or (premise["evidence"].casefold() in text
                   and "premise" in text)
               for premise in verified):
            continue
        kept.append(requirement)

    for premise, canonical in (
            [(row, _deferred_premise_requirement(row)) for row in deferred]
            + [(row, _label_error_premise_requirement(row))
               for row in label_errors]):
        if canonical not in kept:
            kept.append(canonical)
    return kept


def mark_verified_premise_requirements(
        packet: Dict, results: Sequence[Dict]) -> List[Dict]:
    """Resolve canonical premise checks from matching verified packet fields."""
    verified = {
        _deferred_premise_requirement(premise): (
            "met",
            "Verified packet deferral: the inferred premise's evidence pointer "
            "and reviewed ticket match its live deferred_answer.")
        for premise in _verified_deferred_premises(packet)
    }
    verified.update({
        _label_error_premise_requirement(premise): (
            "unmet",
            "Verified labeling error: the measured/documented premise's "
            "evidence pointer names an open ticket that cannot run before "
            "the reviewed ticket is complete.")
        for premise in _verified_label_error_premises(packet)
    })
    marked: List[Dict] = []
    for result in results:
        if not isinstance(result, dict):
            marked.append(result)
            continue
        requirement = result.get("requirement")
        resolution = verified.get(requirement)
        if resolution is None:
            marked.append(result)
            continue
        status, evidence = resolution
        resolved = dict(result)
        resolved["status"] = status
        resolved["evidence"] = evidence
        marked.append(resolved)
    return marked


def _comment_connection_page(connection: object, label: str
                             ) -> Tuple[List[dict], bool, Optional[str]]:
    """Read one GraphQL comment page, failing closed on an incomplete shape."""
    if not isinstance(connection, dict):
        raise funnel.GitHubError("{} comment list was unreadable".format(label))
    nodes = connection.get("nodes")
    page_info = connection.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise funnel.GitHubError("{} comment list was unreadable".format(label))
    has_next = page_info.get("hasNextPage")
    if not isinstance(has_next, bool):
        raise funnel.GitHubError("{} comment pagination was unreadable".format(label))
    cursor = page_info.get("endCursor")
    if ((has_next or nodes)
            and (not isinstance(cursor, str) or not cursor)):
        raise funnel.GitHubError("{} comment pagination was unreadable".format(label))
    if any(not isinstance(row, dict) for row in nodes):
        raise funnel.GitHubError("{} comment row was unreadable".format(label))
    return nodes, has_next, cursor if isinstance(cursor, str) else None


def parse_run_evidence_comment(body: str) -> Optional[Dict[str, object]]:
    """Read a canonical Run evidence payload without judging its claims.

    The generic marked-block reader owns the fenced JSON parsing and the
    marker family convention. This layer requires the Run evidence comment's
    immediate fenced form and its four reported fields. A missing, malformed,
    or incomplete form remains ordinary prose in the unchanged comment body.
    """
    if not isinstance(body, str):
        return None
    for payload, block in funnel._marked_json_blocks(
            body, RUN_EVIDENCE_MARKER):
        remainder = block[len(RUN_EVIDENCE_MARKER):]
        if RUN_EVIDENCE_FENCE_RE.match(remainder) is None:
            continue
        if any(field not in payload for field in RUN_EVIDENCE_FIELDS):
            continue
        if (not isinstance(payload.get("command"), str)
                or not payload["command"].strip()
                or isinstance(payload.get("exit_status"), bool)
                or not isinstance(payload.get("exit_status"), int)
                or payload["exit_status"] < 0
                or not isinstance(payload.get("output_summary"), str)
                or not payload["output_summary"].strip()
                or not isinstance(payload.get("environment_note"), str)
                or not payload["environment_note"].strip()):
            continue
        return {field: payload[field] for field in RUN_EVIDENCE_FIELDS}
    return None


def _shape_pr_comment(row: dict, kind: str) -> Dict[str, Any]:
    """Return one reviewer-visible PR comment with a capped body."""
    body = row.get("body")
    created_at = row.get("createdAt")
    if not isinstance(body, str) or not isinstance(created_at, str) or not created_at:
        raise funnel.GitHubError("{} comment fields were unreadable".format(kind))
    author = row.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(login, str) or not login:
        login = "unknown"
    body = body.strip()
    if len(body) > PR_COMMENT_BODY_LIMIT:
        body = body[:PR_COMMENT_BODY_LIMIT] + (
            "\n…[truncated {} chars]".format(len(body) - PR_COMMENT_BODY_LIMIT))
    shaped: Dict[str, Any] = {
        "kind": kind,
        "author": login,
        "created_at": created_at,
        "body": body,
    }
    if RUN_EVIDENCE_MARKER in body:
        payload = parse_run_evidence_comment(body)
        shaped["run_evidence"] = (
            {"format": "canonical", "fields": payload}
            if payload is not None else {"format": "prose"}
        )
    return shaped


def parse_departures(body: object) -> List[str]:
    """Return the entries of a PR body's Departures section (#1720).

    The section is the first ``Departures:`` label or ``## Departures``
    heading outside a code fence. Each bullet is one entry, and so is a
    paragraph of prose; indented and wrapped lines continue the entry above
    them. A label section ends at a blank line followed by a new paragraph,
    at the template's next label (``Branch:``), or at a heading; a heading
    section ends only at the next heading. A code fence ends either.

    "None" entries are the template's way of saying there were none, so
    they are dropped: a body without the section and a section reading
    ``- None.`` both give an empty list. Anything else is kept as written,
    because what a departure claims is for the judge to weigh.
    """
    if not isinstance(body, str):
        return []
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header = None
    in_fence = False
    start = 0
    for index, line in enumerate(lines):
        if CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            header = DEPARTURES_HEADER_RE.match(line)
            if header is not None:
                start = index + 1
                break
    if header is None:
        return []
    heading_form = header.group("heading") is not None

    entries: List[str] = []
    current: Optional[List[str]] = None
    if header.group("rest"):
        current = [header.group("rest")]

    def close() -> None:
        nonlocal current
        if current:
            entries.append(" ".join(current))
        current = None

    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            close()
            continue
        if CODE_FENCE_RE.match(line) or MARKDOWN_HEADING_RE.match(line):
            break
        if not heading_form and DEPARTURES_LABEL_END_RE.match(line):
            break
        bullet = DEPARTURE_BULLET_RE.match(line)
        if bullet is not None:
            close()
            current = [bullet.group("text").strip()]
            continue
        if current is not None:
            current.append(stripped)
        elif heading_form or not entries:
            current = [stripped]
        elif line[:1] in (" ", "\t"):
            # An indented paragraph after a blank line belongs to the
            # bullet above it, as it does in Markdown.
            entries[-1] = entries[-1] + " " + stripped
        else:
            break
    close()
    return [entry for entry in entries
            if not NO_DEPARTURE_RE.match(entry.strip("*_` \t"))]


def pr_body_section(pr_view: dict) -> Dict[str, object]:
    """The packet's PR-description fields, labelled as claims (#1720).

    ``pr_body`` is the stripped description cut at ``PR_BODY_LIMIT`` with
    the packet's usual ``…[truncated N chars]`` mark, and
    ``pr_body_truncated`` says so without parsing the text. A view that
    carried no body reads as None, not as an empty description.
    ``pr_departures`` comes from the whole body, not the cut one.
    """
    body = pr_view.get("body")
    text: Optional[str] = None
    truncated = False
    if isinstance(body, str):
        text = body.strip()
        if len(text) > PR_BODY_LIMIT:
            truncated = True
            text = text[:PR_BODY_LIMIT] + (
                "\n…[truncated {} chars]".format(len(text) - PR_BODY_LIMIT))
    return {
        "pr_body": text,
        "pr_body_truncated": truncated,
        "pr_departures": parse_departures(body),
        "pr_claims_note": PR_CLAIMS_NOTE,
    }


def _pr_comments_section(comments: List[Dict[str, Any]]) -> Dict:
    """Give the packet an explicit available or empty PR-comments section."""
    comments.sort(key=lambda entry: (entry["created_at"], entry["kind"],
                                     entry["author"], entry["body"]))
    return {
        "status": "available" if comments else "empty",
        "message": None if comments else "No PR comments.",
        "comments": comments,
    }


def fetch_pr_comments(repo: str, pr_number: int) -> Dict:
    """Fetch every PR issue and inline review comment through shared GraphQL.

    The comments appear oldest first, newest last. A failed or malformed read
    becomes an explicit could-not-read section instead of looking like an
    empty PR.
    """
    try:
        owner, separator, name = repo.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise funnel.GitHubError("repository name was unreadable")

        issue_cursor: Optional[str] = None
        thread_cursor: Optional[str] = None
        comments: List[Dict[str, Any]] = []
        while True:
            variables = {"owner": owner, "name": name, "number": pr_number}
            if issue_cursor is not None:
                variables["issueCursor"] = issue_cursor
            if thread_cursor is not None:
                variables["threadCursor"] = thread_cursor
            data = funnel.gh_graphql(PR_COMMENTS_QUERY, **variables)
            repository = data.get("repository") if isinstance(data, dict) else None
            pull_request = (repository.get("pullRequest")
                            if isinstance(repository, dict) else None)
            if not isinstance(pull_request, dict):
                raise funnel.GitHubError("PR comment list was unreadable")

            issue_rows, issue_more, next_issue_cursor = _comment_connection_page(
                pull_request.get("issueComments"), "issue")
            for row in issue_rows:
                comments.append(_shape_pr_comment(row, "issue"))

            thread_rows, thread_more, next_thread_cursor = _comment_connection_page(
                pull_request.get("reviewThreads"), "review")
            for thread in thread_rows:
                thread_id = thread.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise funnel.GitHubError("review comment thread was unreadable")
                review_rows, review_more, next_review_cursor = (
                    _comment_connection_page(thread.get("comments"), "review"))
                for row in review_rows:
                    comments.append(_shape_pr_comment(row, "review"))
                while review_more:
                    review_data = funnel.gh_graphql(
                        PR_REVIEW_THREAD_COMMENTS_QUERY,
                        threadId=thread_id, cursor=next_review_cursor)
                    node = review_data.get("node") if isinstance(review_data, dict) else None
                    review_connection = (node.get("comments")
                                         if isinstance(node, dict) else None)
                    review_rows, review_more, next_review_cursor = (
                        _comment_connection_page(review_connection, "review"))
                    for row in review_rows:
                        comments.append(_shape_pr_comment(row, "review"))

            # Carry the last cursor even after a connection is exhausted.
            # The other connection may still have another page, and omitting
            # an exhausted cursor would make GraphQL repeat its first page.
            issue_cursor = next_issue_cursor
            thread_cursor = next_thread_cursor
            if not issue_more and not thread_more:
                break
        return _pr_comments_section(comments)
    except (funnel.GitHubError, OSError, TypeError, ValueError) as exc:
        reason = str(exc).strip() or "the response was unreadable"
        return {
            "status": "could_not_read",
            "message": "Could not read PR comments: {}".format(reason),
            "comments": [],
        }


def build_packet(*, repo: str, pr_number: int, pr_view: dict, diff: str,
                 ticket: Optional[dict], plan_md: str,
                 plan_md_missing: bool, open_prs: Sequence[dict],
                 verdict: Optional[dict], stop_counter: dict,
                 collected_at: str, pr_comments: dict,
                 merged_prs: Optional[Sequence[dict]] = None,
                 ci_runs: Optional[Sequence[dict]] = None,
                 tickets: Optional[Sequence[Optional[dict]]] = None,
                 changed_files: Optional[Sequence[str]] = None,
                 merge_base: Optional[str] = None,
                 scope_source: str = "pr") -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO.

    Every field the review question needs, in one JSON-serialisable dict.
    ``ticket`` is None when the branch is not a ticket/<n> branch; the
    verdict's head_sha sits beside it so a reader can compare it with the
    current head without parsing the verdict. ``merged_prs`` rows carry
    number, title, mergedAt, headRefName and files [{path}]; None reads as
    no merges scanned. Rows on the candidate's own head branch become
    ``ticket_prior_prs``: the slices of this ticket that already merged.
    ``ticket.comments`` carries the ticket's newest comments with their
    recorded voices, shaped by ``ticket_comments``; a ticketless branch
    gets an empty list. ``tickets`` is every ticket the PR closes —
    the branch ticket plus the closing references (#1088) — each shaped
    like ``ticket``; None reads as the branch ticket alone, so a
    single-ticket PR's packet keeps its shape.
    ``pr_body`` and ``pr_departures`` carry the PR description and its
    Departures entries (#1720), shaped by ``pr_body_section``. They were
    kept out once because they are the author's own claims and a reviewer
    that trusts them can be argued into approving; that left every ticket
    asking for a record in the description unmergeable. They now enter
    beside ``pr_claims_note``, which labels both as the implementer's
    claims, and the verdict rules are unchanged: a claim is weighed against
    the diff, and a departure never meets its requirement by itself.
    ``plan_premises`` groups the fixed, structured premises section from
    each ticket's parent plan. An available empty list means that plan
    recorded none; ``available: false`` means its body could not be read
    or its section could not be parsed.
    ``pr_comments`` is a fixed section for issue and inline review comments;
    its explicit empty and could-not-read states are distinct.
    ``ci_runs`` rows are ``gh run list`` JSON; None reads as no runs
    scanned, which fails closed — an overlap then rejects as stale, as
    before #1019.
    A ``diff`` rebuilt from the files API (an ``AssembledDiff``, #1114) adds
    ``diff_truncated: true`` and ``diff_omitted_files``, the count of files
    GitHub listed without a patch; an ordinary diff adds neither key.
    ``changed_files`` overrides the PR view's file list with the live
    compare scope (#1043); None derives it from the view as before, and the
    overlap and protected rows read whichever list the packet carries.
    ``merge_base`` is the compare's ``merge_base_commit.sha`` (None on the
    PR fallback), ``pr_base_sha`` is the PR's recorded base, and
    ``scope_source`` names which scope the packet carries (``"compare"`` or
    ``"pr"``), so a reviewer can see when the two bases differ.
    """
    pr_view = pr_view or {}
    if changed_files is None:
        changed_files = sorted({
            entry.get("path") for entry in (pr_view.get("files") or [])
            if isinstance(entry, dict) and entry.get("path")
        })
    else:
        changed_files = sorted({path for path in changed_files if path})
    raw_rollup = pr_view.get("statusCheckRollup") or []
    rollup = _rollup_with_actions_evidence(
        raw_rollup, ci_runs or [], pr_view.get("headRefOid")
    )
    checks = summarize_checks(rollup)
    runs = summarize_runs(ci_runs or [], pr_view.get("headRefOid"))
    green_at = green_run_at(runs)
    could_not_run = funnel.ci_could_not_run_reasons(rollup)
    if tickets is None:
        ticket_rows: List[Optional[dict]] = [ticket] if ticket is not None else []
    else:
        ticket_rows = list(tickets)
    plan_premises = packet_plan_premises(ticket_rows)
    ticket_packet = shape_ticket(ticket)
    tickets_packet = [shape_ticket(row) for row in ticket_rows
                      if isinstance(row, dict)]
    head = head_date(pr_view)
    assembled = {
        "repo": repo,
        "pr": pr_number,
        "pr_title": pr_view.get("title"),
        **pr_body_section(pr_view),
        "branch": pr_view.get("headRefName"),
        "base": pr_view.get("baseRefName"),
        "state": pr_view.get("state"),
        "merged_at": pr_view.get("mergedAt") or pr_view.get("merged_at"),
        "closed_at": pr_view.get("closedAt") or pr_view.get("closed_at"),
        "mergeable": pr_view.get("mergeable"),
        "head_sha": pr_view.get("headRefOid"),
        "head_date": head,
        "ticket": ticket_packet,
        "tickets": tickets_packet,
        "plan_premises": plan_premises,
        "pr_comments": pr_comments,
        "plan_md": plan_md,
        "plan_md_missing": plan_md_missing,
        "diff": diff,
        "changed_files": changed_files,
        "merge_base": merge_base,
        "pr_base_sha": pr_view.get("baseRefOid"),
        "scope_source": scope_source,
        "ci": {"state": ci_state(rollup),
               "checks": checks,
               "annotation": could_not_run[0] if could_not_run else None,
               "annotations": could_not_run,
               "runs": runs,
               "green_run_at": green_at,
               "latest_run_id": latest_completed_run_id(runs)},
        "verdict": verdict,
        "verdict_head_sha": (verdict or {}).get("head_sha"),
        "overlap": file_overlap(changed_files, open_prs, pr_number),
        "merged_overlap": merged_overlap(changed_files, merged_prs, head,
                                         pr_number),
        "ticket_prior_prs": ticket_prior_prs(
            pr_view.get("headRefName"), merged_prs, pr_number),
        "protected": protected_touches(changed_files, diff),
        "stop_auto_merging": stop_counter,
        "collected_at": collected_at,
    }
    if isinstance(diff, AssembledDiff):
        assembled["diff_truncated"] = True
        assembled["diff_omitted_files"] = diff.omitted_patches
    assembled["precheck"] = precheck(assembled)
    # The re-run is the runner's next action, not a fact about the overlap:
    # it is set only when the whole precheck passes, so a failing row
    # rejects first and no re-run is requested for a doomed packet.
    if assembled["precheck"]["pass"]:
        assembled["ci_rerun"] = decide_ci_rerun(assembled)
    else:
        assembled["ci_rerun"] = None
    return assembled


def fetch_pr(repo: str, pr_number: int) -> dict:
    """One PR view: identity, head, merge result, CI rollup, changed files.

    ``closingIssuesReferences`` rides the same read so the packet can
    carry every ticket the PR closes (#1088); it costs no extra call.
    ``baseRefOid`` is the PR's recorded base, the ``pr_base_sha`` the
    packet compares the live merge base against (#1043). ``body`` is the
    description the packet carries as the implementer's claims (#1720).
    """
    data = funnel._gh_json(
        "gh", "pr", "view", str(pr_number), "--repo", repo, "--json",
        "number,title,body,headRefName,headRefOid,baseRefName,baseRefOid,"
        "state,mergeable,"
        "mergedAt,mergedBy,closedAt,"
        "statusCheckRollup,commits,files,closingIssuesReferences")
    if not data:
        raise funnel.GitHubError(
            "could not read PR #{} in {}".format(pr_number, repo))
    return data


class AssembledDiff(str):
    """A diff rebuilt from the PR files API because ``gh pr diff`` refused.

    ``omitted_patches`` counts the files GitHub listed without a ``patch``
    (large or binary entries): they are in the PR but not in this text.
    """

    omitted_patches = 0


#: GitHub serves at most 3,000 files per PR, so 30 pages of 100 is the end.
_FILES_PAGE_SIZE = 100
_FILES_MAX_PAGES = 30


def _diff_too_large(stderr: str) -> bool:
    """Whether ``gh pr diff`` failed only because the diff is over the cap."""
    return "too_large" in stderr or "HTTP 406" in stderr


def fetch_files_diff(repo: str, pr_number: int) -> AssembledDiff:
    """Rebuild a unified diff from ``pulls/<n>/files``, page by page (#1114).

    Each entry's ``patch`` goes under a ``diff --git a/<path> b/<path>``
    header, in API order. Entries without a patch add nothing to the text
    and are counted in ``omitted_patches``.
    """
    entries: List[dict] = []
    for page in range(1, _FILES_MAX_PAGES + 1):
        endpoint = "repos/{}/pulls/{}/files?per_page={}&page={}".format(
            repo, pr_number, _FILES_PAGE_SIZE, page)
        proc = funnel._run_gh(["gh", "api", endpoint],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise funnel.GitHubError(
                "could not read files for PR #{} in {}: {}".format(
                    pr_number, repo, (proc.stderr or "").strip()))
        try:
            rows = json.loads(proc.stdout or "[]")
        except ValueError:
            raise funnel.GitHubError(
                "unreadable files page {} for PR #{} in {}".format(
                    page, pr_number, repo))
        if not isinstance(rows, list):
            raise funnel.GitHubError(
                "unexpected files page {} for PR #{} in {}".format(
                    page, pr_number, repo))
        entries.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < _FILES_PAGE_SIZE:
            break
    parts: List[str] = []
    omitted = 0
    for entry in entries:
        path = entry.get("filename") or ""
        patch = entry.get("patch")
        if not isinstance(patch, str) or not patch:
            omitted += 1
            continue
        old_path = entry.get("previous_filename") or path
        parts.append("diff --git a/{} b/{}\n--- a/{}\n+++ b/{}\n{}\n".format(
            old_path, path, old_path, path, patch.rstrip("\n")))
    diff = AssembledDiff("".join(parts))
    diff.omitted_patches = omitted
    return diff


def fetch_diff(repo: str, pr_number: int) -> str:
    """The unified diff of the PR.

    A diff over GitHub's 20,000-line cap (HTTP 406 ``too_large``) is rebuilt
    from the files API instead (#1114); every other failure still raises.
    """
    proc = funnel._run_gh(
        ["gh", "pr", "diff", str(pr_number), "--repo", repo],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if _diff_too_large(stderr):
            return fetch_files_diff(repo, pr_number)
        raise funnel.GitHubError(
            "could not read diff for PR #{} in {}: {}".format(
                pr_number, repo, stderr))
    return proc.stdout or ""


def fetch_scope(repo: str, base_ref: str,
                head_sha: str) -> Tuple[List[str], str, Optional[str]]:
    """Scope the branch against the live merge base (#1043).

    Reads ``gh api repos/<repo>/compare/<base_ref>...<head_sha>``. The
    three-dot compare diffs the merge base against the head, so files main
    gained after the branch merged it are not reported as the branch's own
    (#194: 49 PR files against 6 real). Returns the changed-file list, the
    unified diff assembled from the entries' patches, and
    ``merge_base_commit.sha`` (None when the answer omits it). Entries
    without a patch (large or binary files) stay in the scope but out of
    the text, counted exactly as the #1114 files-API rebuild counts them.
    Raises ``funnel.GitHubError`` when the call fails or the answer is
    unreadable; the caller falls back to the PR reads.
    """
    data = funnel._gh_json(
        "gh", "api", "repos/{}/compare/{}...{}".format(
            repo, base_ref, head_sha))
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise funnel.GitHubError(
            "could not read compare {}...{} in {}".format(
                base_ref, head_sha, repo))
    entries = [row for row in data["files"] if isinstance(row, dict)]
    changed = sorted({row.get("filename") for row in entries
                      if row.get("filename")})
    parts: List[str] = []
    omitted = 0
    for row in entries:
        path = row.get("filename") or ""
        patch = row.get("patch")
        if not isinstance(patch, str) or not patch:
            omitted += 1
            continue
        old_path = row.get("previous_filename") or path
        parts.append("diff --git a/{} b/{}\n--- a/{}\n+++ b/{}\n{}\n".format(
            old_path, path, old_path, path, patch.rstrip("\n")))
    diff: str = "".join(parts)
    if omitted:
        assembled = AssembledDiff(diff)
        assembled.omitted_patches = omitted
        diff = assembled
    merge_base = data.get("merge_base_commit")
    merge_sha = (merge_base.get("sha") if isinstance(merge_base, dict)
                 else None)
    return (changed, diff, merge_sha if isinstance(merge_sha, str) else None)


_ISSUE_URL = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/issues/\d+")
_ISSUE_NUMBER = re.compile(r"/issues/(\d+)")


def closing_ticket_refs(pr_view: dict,
                       default_repo: str) -> List[Tuple[str, int]]:
    """Every ticket the PR closes, as (repo, number) pairs, deduplicated.

    Read from the PR view's ``closingIssuesReferences`` (#1088): a PR
    closing several tickets is judged against the union, not the branch
    ticket alone — jeffy PR #132 was rejected for the change jeffy#129
    asked for because the packet showed only #131. Each entry keeps its
    own repository (GraphQL ``nameWithOwner``, REST ``full_name``, or
    the issue URL); entries without a readable number are dropped, and
    entries without a repository read as the PR's own. Rubbish rows
    never break the parse: an unreadable closing list reads as empty,
    and the branch ticket still carries the spec.
    """
    raw = (pr_view or {}).get("closingIssuesReferences")
    if isinstance(raw, dict) and isinstance(raw.get("nodes"), list):
        raw = raw["nodes"]
    if not isinstance(raw, list):
        return []
    refs: List[Tuple[str, int]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        number = entry.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            match = _ISSUE_NUMBER.search(str(entry.get("url") or ""))
            if not match:
                continue
            number = int(match.group(1))
        if number <= 0:
            continue
        repo = default_repo
        repository = entry.get("repository")
        if isinstance(repository, dict):
            for key in ("nameWithOwner", "full_name"):
                full = repository.get(key)
                if isinstance(full, str) and "/" in full:
                    repo = full
                    break
        if repo == default_repo:
            match = _ISSUE_URL.search(str(entry.get("url") or ""))
            if match:
                repo = match.group(1)
        ref = entry.get("ref")
        if isinstance(ref, str) and "#" in ref:
            owner_repo = ref.split("#", 1)[0]
            if "/" in owner_repo:
                repo = owner_repo
        pair = (repo, number)
        if pair not in refs:
            refs.append(pair)
    return refs


def shape_ticket(ticket: Optional[dict]) -> Dict[str, Optional[object]]:
    """One ticket as the reviewer reads it: identity, body, parent, comments.

    Shared by the single branch ``ticket`` and every entry of the
    ``tickets`` list, so both carry the same fields: the parent with its
    comments shaped, and the ticket's newest comments with their recorded
    voices. None reads as the empty ticket a ticketless branch gets.
    """
    if ticket is None:
        return {
            "ref": None, "number": None, "title": None, "url": None,
            "body": None, "risk": None, "parent": None, "comments": [],
        }
    parent = ticket.get("parent")
    if isinstance(parent, dict):
        parent = dict(parent)
        parent["comments"] = ticket_comments(parent.get("comments"))
        # The full project plan is read only to render its structured
        # premises in the packet's dedicated section.
        parent.pop("body", None)
        parent.pop("body_unavailable", None)
    return {
        "ref": ticket.get("ref"),
        "number": ticket.get("number"),
        "title": ticket.get("title"),
        "url": ticket.get("url"),
        "body": ticket.get("body"),
        "risk": ticket.get("risk"),
        "parent": parent,
        "comments": ticket_comments(ticket.get("comments")),
    }


def parent_repo_from_row(parent: dict) -> Optional[str]:
    """The parent's repository when the relationship row already names it.

    The ticket read's ``parent`` field usually carries only the parent's
    number, but some rows name the repository too: a ``repository`` object
    (REST ``full_name`` or GraphQL ``nameWithOwner``), a ``repo`` or ``ref``
    field, or the issue ``url`` (#1062). Any of those resolves the parent
    without spending a read; None means the caller must ask the API.
    """
    repository = parent.get("repository")
    if isinstance(repository, dict):
        for key in ("full_name", "nameWithOwner"):
            full = repository.get(key)
            if isinstance(full, str) and "/" in full:
                return full
    direct = parent.get("repo")
    if isinstance(direct, str) and "/" in direct:
        return direct
    ref = parent.get("ref")
    if isinstance(ref, str) and "#" in ref:
        owner_repo = ref.split("#", 1)[0]
        if "/" in owner_repo:
            return owner_repo
    match = _ISSUE_URL.search(str(parent.get("url") or ""))
    if match:
        return match.group(1)
    return None


def resolve_parent_repo(repo: str, ticket_number: int,
                        parent: dict) -> Optional[str]:
    """The parent's own repository, never assumed to be the ticket's.

    A member-repo ticket's parent usually lives in command-center, so
    reading the parent in the ticket's repo 404s (#1066). Prefer a
    repository the relationship row already names; otherwise one
    sub-issue relationship read supplies it. None means the parent is
    unreadable and the caller degrades, rather than guessing the
    ticket's repo and risking another issue's comments.
    """
    from_row = parent_repo_from_row(parent)
    if from_row is not None:
        return from_row
    relationship = funnel._gh_json(
        "gh", "api", "repos/{}/issues/{}/parent".format(repo, ticket_number))
    if isinstance(relationship, dict):
        repository = relationship.get("repository")
        if isinstance(repository, dict):
            full = repository.get("full_name")
            if isinstance(full, str) and "/" in full:
                return full
    return None


def degraded_parent(parent: dict, parent_number: object,
                    parent_repo: Optional[str]) -> dict:
    """A partial parent row that names which plan/comment reads failed.

    A 404 on either parent read degrades instead of raising: the review
    proceeds without that parent data. The ``comments_unavailable`` and
    ``body_unavailable`` markers keep missing data distinct from an empty
    comments list or a plan with no premises. The ``ref`` names the parent
    only when its repository resolved, so a degraded row never names the
    wrong repository.
    """
    enriched = dict(parent)
    if not isinstance(enriched.get("comments"), list):
        enriched["comments"] = []
        enriched["comments_unavailable"] = True
    if not isinstance(enriched.get("body"), str):
        enriched["body_unavailable"] = True
    if parent_repo is not None:
        enriched["repo"] = parent_repo
        enriched["ref"] = "{}#{}".format(parent_repo, parent_number)
    return enriched


def fetch_parent_comments(repo: str, ticket_number: int,
                          parent: Optional[dict]) -> Optional[dict]:
    """Attach a parent's body and comments, resolved against its repository.

    GitHub CLI currently returns only the parent's identity in the ticket's
    ``parent`` field. Some fixtures and future CLI versions may include its
    body and comments there already, so preserve that fast path. Otherwise
    the parent's repository resolves from the row or from one sub-issue
    relationship read, and one parent issue view in that repository
    supplies both. Either read failing degrades to a marked partial row,
    never a raise: an unreadable parent must not block a review.
    """
    if not isinstance(parent, dict):
        return parent
    if (isinstance(parent.get("comments"), list)
            and isinstance(parent.get("body"), str)
            and not parent.get("body_unavailable")):
        return parent
    parent_number = parent.get("number")
    if parent_number is None:
        enriched = dict(parent)
        if not isinstance(enriched.get("comments"), list):
            enriched["comments"] = []
        if not isinstance(enriched.get("body"), str):
            enriched["body_unavailable"] = True
        return enriched
    parent_repo = resolve_parent_repo(repo, ticket_number, parent)
    if parent_repo is None:
        return degraded_parent(parent, parent_number, None)
    parent_view = funnel._gh_json(
        "gh", "issue", "view", str(parent_number), "--repo", parent_repo,
        "--json", "body,comments")
    if not isinstance(parent_view, dict) \
            or not isinstance(parent_view.get("comments"), list):
        return degraded_parent(parent, parent_number, parent_repo)
    enriched = dict(parent)
    enriched["comments"] = parent_view["comments"]
    if isinstance(parent_view.get("body"), str):
        enriched["body"] = parent_view["body"]
        enriched.pop("body_unavailable", None)
    elif not isinstance(enriched.get("body"), str):
        enriched["body_unavailable"] = True
    enriched["repo"] = parent_repo
    enriched["ref"] = "{}#{}".format(parent_repo, parent_number)
    return enriched


def fetch_ticket(repo: str, number: int) -> dict:
    """The ticket behind a ticket/<n> branch: body, identity, parent, comments.

    Comments ride the ticket read, so the reviewer sees decisions recorded
    there at no extra GitHub call. The parent is enriched from one sub-issue
    relationship read for its repository and one parent issue view for its
    plan body and comments when either is not already included.
    """
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo, "--json",
        "number,title,url,body,parent,comments")
    if not data:
        raise funnel.GitHubError(
            "could not read ticket {}#{}".format(repo, number))
    data["parent"] = fetch_parent_comments(repo, number, data.get("parent"))
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
    """Recently merged PRs with their merge time, branch and changed files.

    ``headRefName`` is what ``ticket_prior_prs`` matches on; ``title`` is
    for the reviewer to read. Both ride the call ``merged_overlap`` already
    makes, so neither costs a GitHub read.
    """
    rows = funnel._gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "merged",
        "--json", "number,title,mergedAt,files,headRefName",
        "--limit", str(limit))
    if rows is None or not isinstance(rows, list):
        raise funnel.GitHubError(
            "could not list merged PRs in {}".format(repo))
    return [row for row in rows if isinstance(row, dict)]


def fetch_ci_runs(repo: str, branch: str,
                limit: int = CI_RUN_SCAN_LIMIT) -> List[dict]:
    """Newest pull_request runs on this branch: id, head, verdict, times.

    The merged-overlap row's coverage evidence: a green run on this head
    that started after an overlapping merge tested a merge commit built
    against a main containing it. An unreadable answer — Actions off, a
    transient API failure — reads as no runs, which fails closed: an
    overlap then rejects as stale exactly as before #1019, and a PR with
    no overlap is unaffected. Event and head are filtered again in
    ``summarize_runs`` so a surprising server answer cannot smuggle a push
    run, or another head's runs, into coverage.
    """
    if not branch:
        return []
    rows = funnel._gh_json(
        "gh", "run", "list", "--repo", repo, "--branch", branch,
        "--event", CI_COVERING_EVENT, "--limit", str(limit),
        "--json", "databaseId,event,headSha,headBranch,conclusion,status,"
                  "createdAt,startedAt,updatedAt")
    if rows is None or not isinstance(rows, list):
        return []
    shaped = []
    newest = True
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            if newest and str(row.get("conclusion") or "").upper() == "FAILURE":
                row = funnel.enrich_actions_run(repo, row)
        except (funnel.GitHubError, OSError, TypeError, ValueError):
            # The run list remains useful for overlap coverage even when the
            # optional startup annotation read is unavailable.
            pass
        shaped.append(row)
        newest = False
    return shaped


def fetch_verdict(repo: str, pr_number: int) -> Optional[dict]:
    """The newest review verdict on the PR, or None. Newest wins."""
    return funnel.latest_verdict(repo, pr_number)


def load_board_items() -> list:
    """The full Project board without item history.

    ``collect`` uses the PR's ticket refs and filtered regression
    connection instead. The full history read cost about a minute and a
    hundred GraphQL points per packet.
    """
    return funnel.load_items(include_details=False)


def fetch_stop_counter(
        items_loader: Optional[Callable[[], list]] = None,
        now: Optional[datetime] = None) -> dict:
    """The rejected-merges counter funnel.rejected_merges computes.

    Reused rather than re-derived: two definitions of "stop" would
    disagree exactly when the bar has failed. Regression items that
    arrive without history are read here; an unreadable history raises
    and fails the packet rather than counting zero.
    """
    items = (items_loader or load_board_items)()
    funnel.hydrate_regression_history(items)
    return dict(funnel.rejected_merges(
        items, now or datetime.now(timezone.utc)))


def collect(repo: Optional[str], pr_number: int, *,
            items_loader: Optional[Callable[[], list]] = None,
            now: Optional[datetime] = None) -> Dict:
    """Fetch every piece and build the packet. Reads only, no writes.

    The ``tickets`` list is the branch ticket plus every closing
    reference (#1088), deduplicated with the branch ticket first; the
    read budget is bounded by the PR's own closing list, never a scan.
    The changed-file scope comes from the live compare against the merge
    base (#1043); when that call fails or the base and head are unknown,
    the packet falls back to the PR reads with ``scope_source`` ``"pr"``.
    """
    resolved = funnel.resolve_repo(repo)
    pr_view = fetch_pr(resolved, pr_number)
    ref = funnel.ticket_ref_from_branch(
        resolved, pr_view.get("headRefName") or "")
    if ref is not None:
        ticket = fetch_ticket(resolved, int(ref.split("#", 1)[1]))
    else:
        ticket = None
    tickets: List[dict] = []
    seen = set()
    if ticket is not None:
        tickets.append(ticket)
        seen.add((resolved, ticket.get("number")))
    for closing_repo, closing_number in closing_ticket_refs(
            pr_view, resolved):
        if (closing_repo, closing_number) in seen:
            continue
        seen.add((closing_repo, closing_number))
        tickets.append(fetch_ticket(closing_repo, closing_number))

    # The PR supplies the bounded ticket refs. Reuse the existing by-ref
    # Project loader, then read the filtered regression set for the packet's
    # stop counter. A by-ref miss falls back to the history-free full board,
    # preserving the previous packet when a ticket is absent from the Project.
    # Keep the loader injection point for parity fixtures and callers that
    # intentionally provide a complete board snapshot.
    if items_loader is None:
        members = funnel.member_repos()
        refs = [row["ref"] for row in tickets
                if isinstance(row.get("ref"), str)]
        loaded_items = funnel.load_project_items_by_refs(
            refs, member_repo_names=members,
        )
        if loaded_items is None:
            loaded_items = funnel.load_items(include_details=False)
        else:
            by_ref = {item.ref: item for item in loaded_items}
            for item in funnel.load_regression_items(
                    member_repo_names=members):
                by_ref.setdefault(item.ref, item)
            loaded_items = list(by_ref.values())
    else:
        loaded_items = items_loader()
    project_rows = {item.ref: item for item in loaded_items}
    for row in tickets:
        project_item = project_rows.get(row.get("ref"))
        row["risk"] = getattr(project_item, "risk", None)
    plan_md, plan_md_missing = fetch_plan_md(resolved)
    branch = pr_view.get("headRefName") or ""
    base_ref = pr_view.get("baseRefName") or ""
    head_sha = pr_view.get("headRefOid") or ""
    scope_files: Optional[List[str]] = None
    scope_diff: Optional[str] = None
    merge_base: Optional[str] = None
    scope_source = "pr"
    if base_ref and head_sha:
        try:
            scope_files, scope_diff, merge_base = fetch_scope(
                resolved, base_ref, head_sha)
        except (funnel.GitHubError, OSError, TypeError, ValueError):
            # The compare is advisory scope: any failure falls back to
            # today's PR reads, which raise exactly as before.
            scope_files, scope_diff, merge_base = None, None, None
        else:
            scope_source = "compare"
    if scope_diff is None:
        scope_diff = fetch_diff(resolved, pr_number)
    packet = build_packet(
        repo=resolved,
        pr_number=pr_number,
        pr_view=pr_view,
        diff=scope_diff,
        changed_files=scope_files,
        merge_base=merge_base,
        scope_source=scope_source,
        ticket=ticket,
        tickets=tickets,
        plan_md=plan_md,
        plan_md_missing=plan_md_missing,
        open_prs=fetch_open_prs(resolved),
        merged_prs=fetch_merged_prs(resolved),
        ci_runs=fetch_ci_runs(resolved, branch) if branch else [],
        verdict=fetch_verdict(resolved, pr_number),
        pr_comments=fetch_pr_comments(resolved, pr_number),
        stop_counter=fetch_stop_counter(lambda: loaded_items, now),
        collected_at=(now or datetime.now(timezone.utc)).isoformat(),
    )
    # Keep build_packet pure; unrunnability depends on fresh GitHub state.
    packet = annotate_unrunnable_premises(packet)
    return packet


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
