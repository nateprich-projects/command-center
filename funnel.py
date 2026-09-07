#!/usr/bin/env python3
"""funnel.py — the shared ranking program.

Both Claude and Codex call this; neither ranks anything itself. See AGENTS.md.

GitHub is the state. There is no cache, no state file and no lock file here, and
adding one is a wrong turn.

Authentication is delegated entirely to the `gh` CLI, so no token is ever read,
stored or passed by this program.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set

# --------------------------------------------------------------------------
# Configuration. These are the only knobs; everything else is derived.
# --------------------------------------------------------------------------

PROJECT_OWNER = "nateprich"
PROJECT_NUMBER = 2
TOPIC = "command-center"
OWNERS = [("user", "nateprich"), ("organization", "nateprich-projects")]
REPO = "nateprich-projects/command-center"

#: The single-in-motion lock. Codex acts as Nate through the gh CLI — no bot
#: identity, no originating-app marker — so no GitHub write can identify it and
#: assignment cannot serve as the lock. See LEARNINGS.md. The marker is instead a
#: Project field only the agent writes, through `funnel claim` / `funnel release`.
LOCK_FIELD = "In motion since"
LOCK_FIELD_ID = "PVTF_lAHOD7A-N84BihDgzhhZLyQ"
PROJECT_ID = "PVT_kwHOD7A-N84BihDg"

#: A claim older than this is stale and may be taken over. Longer than any
#: single ticket should honestly take.
LOCK_TTL = timedelta(hours=2)

#: How many tickets may be worked at once, across the whole funnel.
#:
#: This used to be implicit in the claim: `cmd_claim` refused whenever *any*
#: other ticket was claimed, so one mechanism did two jobs. Splitting them was
#: Nate's call on 2026-09-06, and the reason is that they are different kinds of
#: rule. **Two agents must never work one ticket** — that is correctness, and the
#: claim still enforces it. **How many tickets run at once** is policy, and `1`
#: was an arbitrary value hidden inside a correctness check.
#:
#: `plan.md`'s evidence for limiting work in flight — 15 concurrent projects, one
#: ever parked — is about *authorising* too much, and the gates already bound
#: that: nothing reaches `Building` without Nate passing a gate. This bounds
#: something else, parallelism inside work he already approved.
#:
#: **It should track review capacity, not coding capacity.** Codex can produce
#: PRs faster than Claude can review them, and review is the budget-starved
#: stage: on 2026-09-06 Codex built #19 and then re-worked it twice because
#: nothing could review it. Raising this above what review can clear buys a queue
#: of unmergeable PRs, not throughput. Two is a deliberate first step up from one,
#: not a measured number — raise it when review stops being the constraint.
#:
#: **Nate's condition for raising it, 2026-09-06:** *"As long as I'm personally
#: reviewing the funnel consistently and starting at the bottom consistently,
#: then the cap can go away or at least raise by a lot."* Worth keeping the two
#: queues apart when that is judged. His consistency bounds how much work reaches
#: `Building` at all. This bounds how fast approved work becomes **unreviewed
#: PRs**, which land on the Claude reviewer rather than on him — `plan.md` moved
#: his gate to the project level precisely so he would not see every PR. Clearing
#: the bottom reliably does not drain that queue.
WIP_LIMIT = 2

#: Funnel order. Index is the stage's depth; later means further along.
STAGES = ["Ideas", "Shaped", "Ready", "Building", "Done", "Parked"]

#: The ladder, best-first. Only finite classes may preempt in-flight work.
LADDER = ["Broken", "Maintenance", "Improve", "New", "Replace"]
PREEMPTING = {"Broken", "Maintenance"}

#: Which stages are waiting on a human, and the question each one asks.
GATES = {
    "Shaped": "Is the plan good?",
    "Ready": "Start now?",
    "Building": "Accept it?",  # only once every child has closed
}

#: Bottom-up: clear the decision closest to shipping first. Parking counts as
#: clearing, which is what stops a stalled item permanently plugging the queue.
DECISION_ORDER = ["Building", "Ready", "Shaped"]

MAINTENANCE_WINDOW = timedelta(days=30)

#: Regression issues opened by `funnel reject`. The issues themselves are the
#: counter — GitHub is the state, so there is nothing else to keep in step.
REGRESSION_PREFIX = "Regression from PR #"

#: Reasons are durable parking artifacts. The sibling brief command reads this
#: fixed marker back from issue comments, so it is a shared contract.
PARK_COMMENT_PREFIX = "**Parked:** "

#: Three rejected merges in a week means the auto-merge bar has failed. That is
#: not "there are bugs" — it is a different and more serious fact, and the
#: response is to stop auto-merging and fix the review prompt.
REJECTED_MERGE_ALARM = 3
REJECTED_MERGE_WINDOW = timedelta(days=7)


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclass
class Item:
    """One issue in the funnel, with its Project fields resolved."""

    repo: str
    number: int
    title: str
    url: str
    state: str  # OPEN | CLOSED
    state_reason: Optional[str] = None  # COMPLETED | NOT_PLANNED | REOPENED
    status: Optional[str] = None
    klass: Optional[str] = None
    status_since: Optional[datetime] = None
    labels: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    in_motion_since: Optional[datetime] = None
    item_id: Optional[str] = None  # the ProjectV2Item, needed to write the lock
    parent: Optional[str] = None  # "owner/repo#123"
    children_total: int = 0
    children_done: int = 0
    closed_at: Optional[datetime] = None

    @property
    def ref(self) -> str:
        return "{}#{}".format(self.repo, self.number)

    @property
    def is_blocked(self) -> bool:
        return "blocked" in self.labels

    @property
    def children_all_closed(self) -> bool:
        return self.children_total > 0 and self.children_done == self.children_total

    def waited(self, now: datetime) -> Optional[timedelta]:
        """How long this item has sat at its current gate."""
        if self.status_since is None:
            return None
        return now - self.status_since


# --------------------------------------------------------------------------
# Ordering. Pure functions over Items — no network, no clock beyond `now`.
# These are the part most likely to be subtly wrong, so they are what the
# fixture tests exercise.
# --------------------------------------------------------------------------


def stage_index(status: Optional[str]) -> int:
    return STAGES.index(status) if status in STAGES else -1


def ladder_index(klass: Optional[str]) -> int:
    """Rank on the ladder. An unset Class sorts last and never preempts.

    An unset Class must never behave like Broken; a forgotten field must not
    acquire preemption rights.
    """
    return LADDER.index(klass) if klass in LADDER else len(LADDER)


def effective_class(item: Item, by_ref: Dict[str, Item]) -> Optional[str]:
    """A ticket inherits its parent's Class — the ladder ranks projects, not
    individual tickets. Sub-issues join the parent's Project automatically with
    their fields blank, so this is the normal case, not an edge case."""
    parent = by_ref.get(item.parent or "")
    return (parent.klass if parent else None) or item.klass


def needs_class(item: Item) -> bool:
    """Anything not in Ideas must carry a Class. Unset is invalid.

    Tickets are exempt because they inherit. A parentless item is a project,
    and a project with no Status at all is exactly the forgotten-field case
    this is meant to catch — so a missing Status does not excuse a missing
    Class.
    """
    if item.state == "CLOSED" or item.parent:
        return False
    if item.status in ("Ideas", "Done", "Parked"):
        return False
    return item.klass not in LADDER


def gate_question(item: Item) -> Optional[str]:
    """The decision this item is waiting on, or None if it waits on no one."""
    if item.state != "OPEN":
        return None
    if item.is_blocked:
        return "Unblock or park?"
    if item.status == "Building":
        # Building waits on Nate only once every child has closed.
        return GATES["Building"] if item.children_all_closed else None
    if item.status == "Ready" and not item.children_total:
        # Nate writing Ready *is* his answer to "is the plan good?". Until the
        # plan has been broken into sub-issues the system owes the work, so this
        # waits on the funnel, not on him. Asking "start now?" about something
        # with nothing to start would be asking him to approve an empty box.
        return None
    return GATES.get(item.status or "")


def awaiting_decision(items: Iterable[Item]) -> List[Item]:
    """Nate's queue: everything waiting on him, bottom-up, oldest first.

    Bottom-up because the longest-stalled, furthest-along item is the most
    likely park candidate, and surfacing it first is what makes this ordering
    do disposal work rather than merely sequencing.
    """

    def key(item: Item):
        status = item.status or ""
        try:
            depth = DECISION_ORDER.index(status)
        except ValueError:
            depth = len(DECISION_ORDER)
        # Blocked items sort with their stage but ahead of it within the stage.
        since = item.status_since or datetime.max.replace(tzinfo=timezone.utc)
        return (depth, not item.is_blocked, since, item.repo, item.number)

    return sorted((i for i in items if gate_question(i)), key=key)


def ideas(items: Iterable[Item]) -> List[Item]:
    """Captured ideas, those flagged worth thinking through first.

    `brief` deliberately excludes Ideas from its counts — it is unbounded and
    guilt-free, and counting it turns it into pressure. But grilling is what
    feeds everything downstream, so there has to be *some* way to ask what is
    waiting to be shaped. This is it, and it is asked for rather than pushed.
    """
    return sorted(
        (i for i in items if i.state == "OPEN" and i.status == "Ideas"),
        key=lambda i: (
            "needs-shaping" not in i.labels,   # flagged ones first
            i.status_since or datetime.max.replace(tzinfo=timezone.utc),
            i.repo,
            i.number,
        ),
    )


def awaiting_breakdown(items: Iterable[Item]) -> List[Item]:
    """Approved plans with no tickets yet — Claude owes these a breakdown.

    Deliberately *after* reviewing in the Claude routine's order. Bottom-up says
    clear the lowest-funnel work first, and a review is Building-stage while a
    breakdown is Shaped-to-Ready; reviewing also finishes work where a breakdown
    creates it. Starvation is not a risk because PRs awaiting review are a finite
    class, bounded by what Codex can produce under the lock and the budget — and
    only finite classes may preempt.
    """
    return sorted(
        (
            i
            for i in items
            if i.state == "OPEN"
            and i.status == "Ready"
            and not i.children_total
            and not i.is_blocked
        ),
        key=lambda i: (i.status_since or datetime.max.replace(tzinfo=timezone.utc),
                       i.repo, i.number),
    )


#: Tiers a ticket can require. `escalated` means the cheap default engineer must
#: not take it — not that it is urgent. Deliberately not model ids: the ranking
#: engine must not know which vendor is cheap this month, and a routine declares
#: its own tier in its prompt.
TIERS = ("standard", "escalated")

#: A ticket declares its risk in its body, written by Claude at breakdown when
#: the plan is in front of it. `funnel.py` reads it; the engineer never decides.
#:
#:     Risk: standard
#:     Risk: escalated — concurrency, destructive
RISK_LINE = re.compile(r"^\s*Risk:\s*(standard|escalated)\b(.*)$",
                       re.IGNORECASE | re.MULTILINE)

#: A safety net for tickets written before markers existed, or by someone who
#: forgot. **Deliberately narrow.** This repository is *about* locks, gates and
#: destructive operations — `park` closes issues on purpose — so a broad keyword
#: list would escalate every ticket and the cheap default would never run. These
#: match phrases that are hard to write by accident.
ESCALATION_PATTERNS = {
    "credentials": r"\b(api[- ]key|access token|client secret|credential store|"
                   r"password|private key)\b",
    "authorisation": r"\b(authoris\w+|authoriz\w+|permission model|access control|"
                     r"oauth|scope grant)\b",
    "data-migration": r"\b(data migration|schema migration|backfill|"
                      r"irreversible migration)\b",
    "destructive": r"\b(force[- ]push|hard delete|permanently delete|"
                   r"drop the (table|branch)|rewrite history)\b",
    "concurrency": r"\b(race condition|deadlock|thread[- ]safe|mutex|"
                   r"atomic (write|commit))\b",
}


def escalation_reasons(title: str, body: str,
                       failed_before: bool = False) -> List[str]:
    """Why the cheap default engineer must not take this ticket.

    An explicit `Risk:` marker wins outright, in both directions — a ticket that
    says `Risk: standard` is standard even if its prose mentions a race
    condition, because the person who wrote the plan knew what it meant and a
    regex does not.
    """
    text = "{}\n{}".format(title or "", body or "")
    marker = RISK_LINE.search(text)
    if marker:
        if marker.group(1).lower() == "standard":
            return ["prior attempt failed"] if failed_before else []
        stated = marker.group(2).strip(" —-:").strip()
        reasons = ["declared: " + stated] if stated else ["declared"]
        return reasons + (["prior attempt failed"] if failed_before else [])

    found = [name for name, pattern in sorted(ESCALATION_PATTERNS.items())
             if re.search(pattern, text, re.IGNORECASE)]
    if failed_before:
        found.append("prior attempt failed")
    return found


def required_tier(title: str, body: str, failed_before: bool = False) -> str:
    return "escalated" if escalation_reasons(title, body, failed_before) else "standard"


def startable(items: Sequence[Item],
              awaiting_review: Optional[Set[str]] = None) -> List[Item]:
    """Tickets Codex may pick up, best-first.

    A ticket is an open issue with no children of its own, whose parent has
    passed the Ready gate. Tickets inherit their parent's Class — the ladder
    ranks projects, not individual tickets.

    `awaiting_review` holds refs whose work is already done and sitting in an
    open PR. A ticket's issue stays open until review merges it, so without this
    Codex re-picks finished work every run: on 2026-09-06 it built #19 at 08:00
    and then spent the 09:00 and 10:00 runs re-verifying the same branch, because
    Claude's routine was over pace and could not review it. Passing it in rather
    than querying here keeps this function pure and testable from fixtures.
    """
    awaiting_review = awaiting_review or frozenset()
    by_ref = {i.ref: i for i in items}

    def eligible(item: Item) -> bool:
        if item.state != "OPEN" or item.is_blocked or item.children_total:
            return False
        if item.ref in awaiting_review:
            return False
        parent = by_ref.get(item.parent or "")
        if parent is None:
            # A parentless item is a project, never a ticket — that is the whole
            # basis of the model. One with no children is awaiting its breakdown,
            # not waiting to be worked. Treating it as both is what made an issue
            # appear in two queues at once.
            return False
        # Only `Building`. `Ready` means "broken into issues" and is still
        # waiting on Nate's "start now?" — treating it as startable lets Codex
        # begin work he never authorised, jumping a gate. He answers that gate
        # by moving the parent to `Building`.
        return parent.status == "Building" and not parent.is_blocked

    def in_flight(item: Item) -> bool:
        """Once a project is Building, its remaining tickets finish first.

        Passing a gate is a commitment; nothing may silently un-commit it.
        """
        parent = by_ref.get(item.parent or "")
        return (parent.status if parent else item.status) == "Building"

    def key(item: Item):
        since = item.status_since or datetime.max.replace(tzinfo=timezone.utc)
        return (
            not in_flight(item),
            ladder_index(effective_class(item, by_ref)),
            since,
            item.repo,
            item.number,
        )

    return sorted((i for i in items if eligible(i)), key=key)


def in_motion(items: Iterable[Item], now: datetime) -> List[Item]:
    """Every ticket currently claimed, oldest claim first.

    A claim is written at run start, which is when it must exist — a PR or a
    branch appears too late to cover the window in which a run most often dies.
    """
    return sorted(
        (
            i
            for i in items
            if i.state == "OPEN"
            and i.in_motion_since is not None
            and now - i.in_motion_since < LOCK_TTL
        ),
        key=lambda i: i.in_motion_since or now,
    )


def lock_holder(items: Iterable[Item], now: datetime) -> Optional[Item]:
    """The oldest live claim, or None. Retained for callers that want one item."""
    held = in_motion(items, now)
    return held[0] if held else None


def at_capacity(items: Iterable[Item], now: datetime) -> bool:
    return len(in_motion(items, now)) >= WIP_LIMIT


def stale_locks(items: Iterable[Item], now: datetime) -> List[Item]:
    """Claims past the TTL. The next run takes these over; each takeover is a
    line in the brief, because three in a week means runs are dying."""
    return sorted(
        (
            i
            for i in items
            if i.state == "OPEN"
            and i.in_motion_since is not None
            and now - i.in_motion_since >= LOCK_TTL
        ),
        key=lambda i: i.in_motion_since or now,
    )


#: A review verdict lives in a PR comment behind this marker, as JSON. GitHub is
#: the state, so it goes where the PR is rather than into a file only one machine
#: can read.
REVIEW_MARKER = "<!-- command-center-review -->"

#: A reviewer that writes prose and then acts leaves nothing a later step can
#: check. That is how a merge became something a model simply decided to do, and
#: how a PR with requested changes ended up owned by nobody (#39).
VERDICTS = ("approved", "rejected")
CI_STATES = ("green", "red", "unknown")


def parse_verdict(body: str) -> Optional[Dict]:
    """The verdict carried by one comment, or None if it is not one."""
    if REVIEW_MARKER not in body:
        return None
    _, _, rest = body.partition(REVIEW_MARKER)
    start, end = rest.find("{"), rest.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        found = json.loads(rest[start:end + 1])
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def latest_verdict(repo: str, pr) -> Optional[Dict]:
    """The newest verdict on a PR.

    Newest wins: a re-review after a fix is a fresh read against the plan, and an
    older verdict must never authorise a diff it did not see.
    """
    rows = (_gh_json("gh", "pr", "view", str(pr), "--repo", repo,
                     "--json", "comments") or {}).get("comments", [])
    for row in reversed(rows):
        found = parse_verdict(row.get("body") or "")
        if found:
            return found
    return None


def awaiting_review(items: Sequence[Item]) -> Set[str]:
    """Tickets whose work is already in an open PR, waiting to be reviewed.

    Found by the `ticket/<number>` branch name the routine guarantees, which is
    the same handle `_ticket_pr` uses. One `gh pr list` per member repo, and only
    for repos that actually have candidate tickets.
    """
    repos = {i.repo for i in items}
    blocked: Set[str] = set()
    for repo in sorted(repos):
        rows = _gh_json("gh", "pr", "list", "--repo", repo, "--state", "open",
                        "--json", "headRefName,number", "--limit", "100") or []
        for row in rows:
            head = row.get("headRefName") or ""
            if not head.startswith("ticket/"):
                continue
            # A PR whose review asked for changes is *not* blocked: its ticket
            # goes back to the engineer to fix. Without this a rejected PR has no
            # owner — the reviewer will not revisit it and the engineer is never
            # offered it — so it waits for Nate. That is #39.
            verdict = latest_verdict(repo, row.get("number"))
            if verdict and verdict.get("verdict") == "rejected":
                continue
            blocked.add("{}#{}".format(repo, head.split("/", 1)[1]))
    return blocked


def next_ticket(items: Sequence[Item], now: datetime,
                blocked: Optional[Set[str]] = None) -> Optional[Item]:
    """The single ticket Codex should work, or None.

    Returns None when the funnel is at its work-in-progress limit. A Broken
    ticket may start anyway; that is the one sanctioned preemption.
    """
    queue = startable(items, awaiting_review=blocked)
    if not queue:
        return None

    claimed = {i.ref for i in in_motion(items, now)}
    free = [i for i in queue if i.ref not in claimed]
    if not free:
        return None
    if not at_capacity(items, now):
        return free[0]

    # At capacity. Only a Broken ticket may exceed it, and only when nothing
    # already in motion is Broken — preemption is for getting a fix moving, not
    # for stacking fixes on top of each other.
    by_ref = {i.ref: i for i in items}
    running = in_motion(items, now)
    if any(effective_class(i, by_ref) == "Broken" for i in running):
        return None
    for candidate in free:
        if effective_class(candidate, by_ref) == "Broken":
            return candidate
    return None


def rejected_merges(items: Iterable[Item], now: datetime) -> Dict[str, object]:
    """Merges Nate checked and found broken.

    The information carried here is not "there is a bug" — it is "the
    auto-merge bar failed", which is the feedback loop on letting Claude merge
    unattended. Without it, a failed bar is noticed only by someone remembering
    it happened before.
    """
    cutoff = now - REJECTED_MERGE_WINDOW
    recent = [
        i
        for i in items
        if i.title.startswith(REGRESSION_PREFIX)
        and i.status_since
        and i.status_since >= cutoff
    ]
    return {
        "window_days": REJECTED_MERGE_WINDOW.days,
        "count": len(recent),
        "refs": [i.ref for i in recent],
        "stop_auto_merging": len(recent) >= REJECTED_MERGE_ALARM,
    }


def unattended_merges(now: datetime) -> List[Dict[str, object]]:
    """Merges Claude made without Nate, read from its own heartbeat records.

    plan.md makes these a condition of unattended merging being allowed at all:
    they must appear in the brief as a record.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat

        rows = heartbeat.read("claude")
    except Exception:
        return []

    cutoff = (now - MAINTENANCE_WINDOW).timestamp()
    return [
        {
            "pr": row.get("merged"),
            "at": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
            "note": row.get("note"),
        }
        for row in rows
        if row.get("merged") and (row.get("ts") or 0) >= cutoff
    ]


def maintenance_load(items: Iterable[Item], now: datetime) -> Dict[str, object]:
    """The portfolio signal: is maintenance crowding out new work?

    If maintenance load blocks new work, that is not a tuning problem — it is
    the signal to reassess how many plates are spinning.
    """
    cutoff = now - MAINTENANCE_WINDOW
    recent = [
        i
        for i in items
        if i.closed_at
        and i.closed_at >= cutoff
        and i.state_reason != "NOT_PLANNED"
    ]
    upkeep = [i for i in recent if i.klass in PREEMPTING]

    started_new = [
        i.status_since
        for i in items
        if i.klass == "New" and i.status_since and i.status in ("Building", "Done")
    ]
    days_since_new = (
        (now - max(started_new)).days if started_new else None
    )

    return {
        "window_days": MAINTENANCE_WINDOW.days,
        "closed_in_window": len(recent),
        "upkeep_share": round(len(upkeep) / len(recent), 3) if recent else None,
        "days_since_anything_new_started": days_since_new,
    }


# --------------------------------------------------------------------------
# GitHub. One query root: the Project. See LEARNINGS.md for why an issue
# cannot be asked which Projects it belongs to.
# --------------------------------------------------------------------------

REPO_QUERY = """
query($cursor: String) {
  OWNER_KIND(login: "OWNER_LOGIN") {
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        isArchived
        repositoryTopics(first: 25) { nodes { topic { name } } }
      }
    }
  }
}
"""

ITEM_QUERY = """
query($login: String!, $number: Int!, $cursor: String) {
  user(login: $login) {
    projectV2(number: $number) {
      items(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          lock: fieldValueByName(name: "In motion since") {
            ... on ProjectV2ItemFieldTextValue { text }
          }
          status: fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          class: fieldValueByName(name: "Class") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            ... on Issue {
              number title url state stateReason closedAt
              repository { nameWithOwner }
              labels(first: 25) { nodes { name } }
              assignees(first: 10) { nodes { login } }
              parent { number repository { nameWithOwner } }
              subIssuesSummary { total completed }
              timelineItems(last: 60, itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]) {
                nodes {
                  ... on ProjectV2ItemStatusChangedEvent {
                    createdAt status project { number }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    pass


def gh_graphql(query: str, **variables) -> dict:
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        cmd += [flag, "{}={}".format(key, value)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip() or "gh exited {}".format(proc.returncode))
    payload = json.loads(proc.stdout)
    if payload.get("errors"):
        raise GitHubError(json.dumps(payload["errors"]))
    return payload["data"]


def parse_time(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp.

    GitHub's own fields are always well formed, but the lock field is written by
    an agent and could be edited by hand in the Project UI. An unparseable claim
    returns None, which reads as "not locked" — the safe direction, because the
    alternative is a garbled value wedging the queue until someone notices.
    """
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def member_repos() -> List[str]:
    """Repos that opted in by carrying the topic. Never an allowlist."""
    members: List[str] = []
    for kind, login in OWNERS:
        query = REPO_QUERY.replace("OWNER_KIND", kind).replace("OWNER_LOGIN", login)
        cursor = None
        while True:
            data = gh_graphql(query, **({"cursor": cursor} if cursor else {}))
            repos = data[kind]["repositories"]
            for node in repos["nodes"]:
                if node["isArchived"]:
                    continue
                topics = [t["topic"]["name"] for t in node["repositoryTopics"]["nodes"]]
                if TOPIC in topics:
                    members.append(node["nameWithOwner"])
            if not repos["pageInfo"]["hasNextPage"]:
                break
            cursor = repos["pageInfo"]["endCursor"]
    return sorted(members)


def _from_node(node: dict) -> Optional[Item]:
    content = node.get("content") or {}
    if not content.get("number"):
        return None  # a draft issue, or a pull request

    status = (node.get("status") or {}).get("name")
    parent = content.get("parent")
    summary = content.get("subIssuesSummary") or {}

    item = Item(
        repo=content["repository"]["nameWithOwner"],
        number=content["number"],
        title=content["title"],
        url=content["url"],
        state=content["state"],
        state_reason=content.get("stateReason"),
        status=status,
        klass=(node.get("class") or {}).get("name"),
        labels=[n["name"] for n in content["labels"]["nodes"]],
        assignees=[n["login"] for n in content["assignees"]["nodes"]],
        parent=(
            "{}#{}".format(parent["repository"]["nameWithOwner"], parent["number"])
            if parent
            else None
        ),
        children_total=summary.get("total") or 0,
        children_done=summary.get("completed") or 0,
        closed_at=parse_time(content.get("closedAt")),
        item_id=node.get("id"),
        in_motion_since=parse_time((node.get("lock") or {}).get("text")),
    )

    # Time at the current gate: the last status change into the status the item
    # actually holds, in *this* project. Events arrive oldest-first, and an
    # issue may sit in several projects — filtering on the project is what stops
    # time-at-gate being silently wrong.
    for event in content["timelineItems"]["nodes"]:
        if not event or (event.get("project") or {}).get("number") != PROJECT_NUMBER:
            continue
        if event.get("status") == status:
            item.status_since = parse_time(event.get("createdAt"))
    return item


def load_items() -> List[Item]:
    members = set(member_repos())
    items: List[Item] = []
    cursor = None
    while True:
        variables = {"login": PROJECT_OWNER, "number": PROJECT_NUMBER}
        if cursor:
            variables["cursor"] = cursor
        project = gh_graphql(ITEM_QUERY, **variables)["user"]["projectV2"]
        if project is None:
            raise GitHubError(
                "Project {}/{} not found or not visible".format(
                    PROJECT_OWNER, PROJECT_NUMBER
                )
            )
        page = project["items"]
        for node in page["nodes"]:
            item = _from_node(node)
            # Membership is the topic. An item whose repo has not opted in is
            # outside the funnel even though it sits in the Project.
            if item and item.repo in members:
                items.append(item)
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return items


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def humanise(delta: Optional[timedelta]) -> str:
    if delta is None:
        return "unknown"
    days = delta.days
    if days >= 1:
        return "{} day{}".format(days, "" if days == 1 else "s")
    hours = delta.seconds // 3600
    if hours >= 1:
        return "{} hour{}".format(hours, "" if hours == 1 else "s")
    return "under an hour"


def launch_command(item: Item) -> str:
    return 'claude "Work {} — {}"'.format(item.url, item.title)


def item_json(item: Item, now: datetime, by_ref: Optional[Dict[str, Item]] = None) -> dict:
    by_ref = by_ref if by_ref is not None else {}
    return {
        "ref": item.ref,
        "repo": item.repo,
        "title": item.title,
        "url": item.url,
        "status": item.status,
        "class": effective_class(item, by_ref),
        "waiting_on": gate_question(item),
        "waited": humanise(item.waited(now)),
        "waited_days": item.waited(now).days if item.waited(now) else None,
        "blocked": item.is_blocked,
        "launch": launch_command(item),
    }


def cmd_queue(items: List[Item], now: datetime) -> int:
    """Everything, ordered — both queues, each under its own heading.

    They are genuinely different orderings over different subsets, so a single
    merged list would have to pick one and misrepresent the other.
    """
    decisions = awaiting_decision(items)
    tickets = startable(items)

    print("Waiting on Nate ({}), bottom-up:".format(len(decisions)))
    if not decisions:
        print("  nothing")
    for item in decisions:
        print(
            "  {:<10} {:<34} {:<18} {}".format(
                item.status or "-",
                item.ref,
                humanise(item.waited(now)),
                gate_question(item),
            )
        )

    print("\nStartable by Codex ({}), ladder order:".format(len(tickets)))
    if not tickets:
        print("  nothing")
    by_ref = {i.ref: i for i in items}
    for item in tickets:
        klass = effective_class(item, by_ref)
        shown = (klass or "no class") + ("" if item.klass else " (inherited)" if klass else "")
        print("  {:<24} {:<34} {}".format(shown, item.ref, item.title))

    pending = awaiting_breakdown(items)
    if pending:
        print("\nApproved, awaiting breakdown into tickets ({}):".format(len(pending)))
        for item in pending:
            print("  {:<34} {:<18} {}".format(
                item.ref, humanise(item.waited(now)), item.title))

    missing = [i for i in items if needs_class(i)]
    if missing:
        print("\nInvalid — not in Ideas and carrying no Class ({}):".format(len(missing)))
        for item in missing:
            print("  {:<34} {}".format(item.ref, item.url))
    return 0


def cmd_next(items: List[Item], now: datetime, tier: Optional[str] = None) -> int:
    blocked = awaiting_review(items)
    ticket = next_ticket(items, now, blocked=blocked)

    # A caller declaring a tier gets only work of that tier — in both directions.
    #
    # `standard` walks past escalated tickets rather than refusing outright, or
    # the queue would stall behind one risky ticket until the other schedule came
    # round. `escalated` walks past *ordinary* ones, which is the same rule
    # applied the other way: the expensive engine is reserved for work that needs
    # it, and idling costs nothing because the cheap continuous schedule is
    # already working the ordinary queue, including overnight.
    reasons: List[str] = []
    if ticket is not None and tier:
        for candidate in startable(items, awaiting_review=blocked):
            found = escalation_reasons(
                candidate.title, _ticket_body(candidate.repo, candidate.number))
            wanted = bool(found) if tier == "escalated" else not found
            if wanted:
                ticket, reasons = candidate, found
                break
        else:
            print("nothing — no {} work waiting".format(tier), file=sys.stderr)
            return 1

    if ticket is None:
        holder = lock_holder(items, now)
        if holder is not None:
            print(
                "nothing — lock held by {} (claimed {} ago)".format(
                    holder.ref, humanise(now - holder.in_motion_since)
                ),
                file=sys.stderr,
            )
        return 1
    print(json.dumps(item_json(ticket, now, {i.ref: i for i in items}), indent=2))
    return 0


def cmd_brief(items: List[Item], now: datetime) -> int:
    decisions = awaiting_decision(items)
    by_ref = {i.ref: i for i in items}
    counts = {}
    for stage in STAGES:
        if stage == "Ideas":
            continue  # Ideas is unbounded and guilt-free; counting it is pressure
        counts[stage] = sum(
            1 for i in items if i.status == stage and i.state == "OPEN"
        )

    running = in_motion(items, now)
    brief = {
        "generated_at": now.isoformat(),
        "total_needing_nate": len(decisions),
        "counts_by_gate": counts,
        "items": [item_json(i, now, by_ref) for i in decisions],
        "needs_class": [item_json(i, now, by_ref) for i in items if needs_class(i)],
        "awaiting_breakdown": [
            item_json(i, now, by_ref) for i in awaiting_breakdown(items)
        ],
        "in_motion": [i.ref for i in running],
        "wip_limit": WIP_LIMIT,
        "stale_locks_taken_over": [i.ref for i in stale_locks(items, now)],
        "maintenance_load": maintenance_load(items, now),
        "unattended_merges": unattended_merges(now),
        "rejected_merges": rejected_merges(items, now),
    }
    print(json.dumps(brief, indent=2))
    return 0


SET_LOCK = """
mutation($project: ID!, $item: ID!, $field: ID!, $value: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field, value: {text: $value}
  }) { projectV2Item { id } }
}
"""


def write_lock(item: Item, value: str) -> None:
    if not item.item_id:
        raise GitHubError("{} is not in the Project; cannot claim it".format(item.ref))
    gh_graphql(
        SET_LOCK,
        project=PROJECT_ID,
        item=item.item_id,
        field=LOCK_FIELD_ID,
        value=value,
    )


def find(items: Sequence[Item], ref: str) -> Item:
    for i in items:
        if i.ref == ref or str(i.number) == ref or i.url == ref:
            return i
    raise GitHubError("no funnel item matches {}".format(ref))


def cmd_claim(items: List[Item], now: datetime, ref: str) -> int:
    """Claim a ticket, or refuse. Two separate refusals, deliberately.

    **Someone else already has this ticket** is correctness: two agents working
    one ticket produce two branches, two PRs, and a `prior_run.py` that cannot
    say what was intended.

    **The funnel is at its limit** is policy, and is the `WIP_LIMIT` above.
    Until 2026-09-06 these were one check, which is why raising the cap was
    impossible without also dropping the guard.

    Refusing is the normal outcome and is not an error worth shouting about; the
    caller distinguishes by exit code.
    """
    target = find(items, ref)
    running = in_motion(items, now)

    taken = next((i for i in running if i.ref == target.ref), None)
    if taken is not None and taken.in_motion_since != target.in_motion_since:
        print("refused — {} is already claimed".format(target.ref), file=sys.stderr)
        return 1

    if taken is None and len(running) >= WIP_LIMIT:
        print(
            "refused — {} tickets already in motion, limit is {} ({})".format(
                len(running), WIP_LIMIT,
                ", ".join(i.ref for i in running)),
            file=sys.stderr,
        )
        return 1

    stale = [i for i in stale_locks(items, now) if i.ref != target.ref]
    for item in stale:
        # Self-correcting, no human in the loop. Every takeover is a line in the
        # brief: one is noise, three in a week means runs are dying.
        write_lock(item, "")
        print("took over stale claim on {}".format(item.ref), file=sys.stderr)

    write_lock(target, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(target.url)
    return 0


SET_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}
"""

CLASS_FIELD_ID = "PVTSSF_lAHOD7A-N84BihDgzhhY15k"
STATUS_FIELD_ID = "PVTSSF_lAHOD7A-N84BihDgzhhY1tc"


def _option_id(field_id: str, name: str) -> str:
    data = gh_graphql(
        '{node(id:"%s"){... on ProjectV2SingleSelectField{options{id name}}}}'
        % field_id
    )
    for option in data["node"]["options"]:
        if option["name"] == name:
            return option["id"]
    raise GitHubError("no option {} on that field".format(name))


def cmd_park(items: List[Item], now: datetime, ref: str, reason: str) -> int:
    """Park a project with its durable reason attached to the issue."""
    item = find(items, ref)
    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))

    # Done and Parked must remain distinguishable. Set the Project status first,
    # then close with NOT_PLANNED, then leave the reason where it can be read
    # without opening the Project.
    gh_graphql(
        SET_FIELD,
        project=PROJECT_ID,
        item=item.item_id,
        field=STATUS_FIELD_ID,
        option=_option_id(STATUS_FIELD_ID, "Parked"),
    )

    close = subprocess.run(
        ["gh", "issue", "close", str(item.number), "--repo", item.repo,
         "--reason", "not planned"],
        capture_output=True, text=True,
    )
    if close.returncode != 0:
        raise GitHubError(close.stderr.strip())

    comment = subprocess.run(
        ["gh", "issue", "comment", str(item.number), "--repo", item.repo,
         "--body", PARK_COMMENT_PREFIX + reason],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(comment.stderr.strip())

    print("{} → Parked\n{}{}".format(item.ref, PARK_COMMENT_PREFIX, reason))
    return 0


def cmd_reject(items: List[Item], now: datetime, pr: str, note: Optional[str]) -> int:
    """Record that a merged PR turned out to be broken.

    This is not "file a bug". The information is that **the auto-merge bar
    failed**, which is a different and more serious fact, and it is the only
    feedback loop on letting Claude merge unattended. One action does all four
    steps, rather than leaving them to be remembered.

    This is one of two commands that write `Status` by code rather than by Nate;
    it remains the single case that also writes `Class`.
    """
    number = pr.rstrip("/").split("/")[-1].lstrip("#")
    repo = REPO

    def run(*args: str) -> str:
        out = subprocess.run(list(args), capture_output=True, text=True)
        if out.returncode != 0:
            raise GitHubError(out.stderr.strip())
        return out.stdout.strip()

    data = json.loads(run(
        "gh", "pr", "view", number, "--repo", repo,
        "--json", "title,url,headRefName,merged",
    ))
    if not data.get("merged"):
        raise GitHubError(
            "PR #{} is not merged — a rejected merge is one that landed".format(number)
        )

    branch = data.get("headRefName") or ""
    ticket_no = branch.split("/")[-1] if branch.startswith("ticket/") else None
    ticket = next((i for i in items if str(i.number) == ticket_no), None) if ticket_no else None

    # 1. Reopen the ticket, so the work is visibly unfinished again.
    if ticket:
        run("gh", "issue", "reopen", str(ticket.number), "--repo", repo)
        print("reopened {}".format(ticket.ref))

    # 2. File the regression against the merged PR. These issues *are* the
    #    counter — GitHub is the state, so there is nothing else to keep in step.
    body = "\n".join(filter(None, [
        "A merge that landed without Nate turned out to be broken.",
        "",
        "- Merged PR: {}".format(data.get("url")),
        "- Ticket: {}".format("#" + ticket_no if ticket_no else "unknown"),
        "",
        "**What this means:** not that there is a bug, but that the auto-merge bar",
        "failed. Three of these in a week and auto-merging stops until the review",
        "prompt in `routines/claude-review.md` is fixed.",
        "",
        "What is broken: {}".format(note) if note else None,
    ]))
    url = run(
        "gh", "issue", "create", "--repo", repo,
        "--title", "{}{}: {}".format(REGRESSION_PREFIX, number, data.get("title", "")),
        "--body", body,
    ).splitlines()[-1]
    print("filed {}".format(url))

    # 3. Return the parent to Building and mark it Broken. Broken is finite, so
    #    it may preempt — which is the point: a broken merge jumps the queue.
    parent = next(
        (i for i in items if ticket and i.ref == ticket.parent), None
    )
    if parent and parent.item_id:
        gh_graphql(SET_FIELD, project=PROJECT_ID, item=parent.item_id,
                   field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, "Building"))
        gh_graphql(SET_FIELD, project=PROJECT_ID, item=parent.item_id,
                   field=CLASS_FIELD_ID, option=_option_id(CLASS_FIELD_ID, "Broken"))
        print("{} -> Building / Broken".format(parent.ref))
    elif ticket:
        print("note: {} has no parent in the Project; set its Class by hand"
              .format(ticket.ref), file=sys.stderr)

    # 4. Say the count out loud. A counter nobody sees is not a counter.
    count = rejected_merges(items, now)["count"] + 1
    print("\nrejected merges in the last {} days: {}".format(
        REJECTED_MERGE_WINDOW.days, count))
    if count >= REJECTED_MERGE_ALARM:
        print("STOP AUTO-MERGING. Fix routines/claude-review.md before the next run.")
    return 0


def cmd_ideas(items: List[Item], now: datetime) -> int:
    rows = ideas(items)
    flagged = [i for i in rows if "needs-shaping" in i.labels]
    print("Ideas ({} total, {} flagged as worth shaping):".format(len(rows), len(flagged)))
    if not rows:
        print("  nothing captured")
    for item in rows:
        print("  {:<3} {:<34} {:<14} {}".format(
            "*" if "needs-shaping" in item.labels else " ",
            item.ref, humanise(item.waited(now)), item.title))
    if rows:
        print("\n  * = labelled needs-shaping. Grilling is interactive and is the"
              "\n      throttle on everything downstream — one at a time.")
    return 0


def cmd_capture(items: List[Item], now: datetime, title: str, note: Optional[str],
                repo: str, shaping: bool) -> int:
    """Capture an idea. Unbounded and guilt-free, by design."""
    body = note or "Captured from chat. Not yet thought through."
    args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    if shaping:
        args += ["--label", "needs-shaping"]
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    url = out.stdout.strip().splitlines()[-1]

    add = subprocess.run(
        ["gh", "project", "item-add", str(PROJECT_NUMBER), "--owner", PROJECT_OWNER,
         "--url", url, "--format", "json"],
        capture_output=True, text=True,
    )
    if add.returncode == 0:
        item_id = json.loads(add.stdout)["id"]
        gh_graphql(SET_FIELD, project=PROJECT_ID, item=item_id,
                   field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, "Ideas"))
        print("{}  → Ideas{}".format(url, " (needs-shaping)" if shaping else ""))
    else:
        print("{}\nnote: created, but not added to the Project".format(url),
              file=sys.stderr)
    return 0


def cmd_shaped(items: List[Item], now: datetime, ref: str, plan_file: str) -> int:
    """Record that an idea has been grilled and a plan now exists.

    Writes the plan into the issue body — `plan.md` puts it there through Ideas
    and Shaped, and it only becomes a repo's own `plan.md` at the Ready gate —
    then moves the item to `Shaped`, which is what asks Nate the next gate: is
    the plan good?
    """
    item = find(items, ref)
    try:
        plan = pathlib.Path(plan_file).read_text()
    except OSError as exc:
        raise GitHubError("cannot read {}: {}".format(plan_file, exc))
    if not plan.strip():
        raise GitHubError("the plan is empty; nothing to record")

    out = subprocess.run(
        ["gh", "issue", "edit", str(item.number), "--repo", item.repo,
         "--body-file", plan_file],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())

    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))
    gh_graphql(SET_FIELD, project=PROJECT_ID, item=item.item_id,
               field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, "Shaped"))
    subprocess.run(["gh", "issue", "edit", str(item.number), "--repo", item.repo,
                    "--remove-label", "needs-shaping"], capture_output=True)
    print("{} → Shaped\n{}".format(item.ref, item.url))
    print("\nIt now waits on you: is the plan good? Answer by moving it to Ready.")
    return 0


#: The gates, and the command that answers each. **These are Nate's answers to
#: his own questions — no agent may run them.** An agent writing `Ready` starts
#: work he never authorised; one writing `Done` accepts its own output. Both are
#: gate-jumping, which is the failure the whole funnel is arranged to prevent.
ANSWERS = {
    "approve": ("Shaped", "Ready", "the plan is good"),
    "start": ("Ready", "Building", "start now"),
    "accept": ("Building", "Done", "shipped and accepted"),
}


SUB_ISSUES = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 50) {
        nodes { number title state repository { nameWithOwner } }
      }
    }
  }
}
"""


def _gh_json(*args: str):
    out = subprocess.run(list(args), capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _ticket_body(repo: str, number: int) -> str:
    """One issue body, fetched only for a candidate about to be handed out."""
    row = _gh_json("gh", "issue", "view", str(number), "--repo", repo,
                   "--json", "body") or {}
    return row.get("body") or ""


def _ticket_pr(repo: str, number: int) -> Optional[Dict]:
    """The PR for a ticket, found by the branch name the routine guarantees."""
    rows = _gh_json("gh", "pr", "list", "--repo", repo, "--state", "all",
                    "--head", "ticket/{}".format(number), "--json",
                    "number,state,url,mergedAt,reviews") or []
    return rows[0] if rows else None


def review_queue(items: Sequence[Item], tier: Optional[str] = None) -> List[Dict]:
    """Open ticket PRs that need a review, best-first.

    A PR needs review when no verdict covers its **current head**. That covers
    three cases with one rule: never reviewed, reviewed and then pushed to, and
    reviewed-and-rejected then fixed. The last is what hands a rejected PR back
    to a reviewer once the engineer has acted on it.

    `tier` filters by the *ticket's* risk, not the PR's size. A reviewer is
    matched to the work the same way an engine is: the expensive judgement is
    spent where the ticket says the stakes are, and nowhere else.
    """
    found: List[Dict] = []
    for repo in sorted({i.repo for i in items}):
        rows = _gh_json("gh", "pr", "list", "--repo", repo, "--state", "open",
                        "--json", "number,headRefName,headRefOid",
                        "--limit", "100") or []
        for row in rows:
            head = row.get("headRefName") or ""
            if not head.startswith("ticket/"):
                continue
            ref = "{}#{}".format(repo, head.split("/", 1)[1])
            ticket = next((i for i in items if i.ref == ref), None)
            if ticket is None:
                continue
            verdict = latest_verdict(repo, row.get("number"))
            if verdict and verdict.get("head_sha") == row.get("headRefOid"):
                continue  # this exact diff has already been judged
            needed = required_tier(
                ticket.title, _ticket_body(repo, ticket.number))
            if tier and needed != tier:
                continue
            found.append({"pr": row.get("number"), "repo": repo, "ref": ref,
                          "tier": needed, "url": ticket.url,
                          "title": ticket.title})
    return found


def cmd_next_review(items: List[Item], tier: Optional[str]) -> int:
    """The single PR this reviewer should read, or nothing."""
    queue = review_queue(items, tier)
    if not queue:
        print("nothing — no {}review waiting".format(
            (tier + " ") if tier else ""), file=sys.stderr)
        return 1
    print(json.dumps(queue[0], indent=2))
    return 0


def cmd_review(repo: str, pr: int, verdict: str, ci: str,
               blocking: List[str], note: Optional[str]) -> int:
    """Record a structured review verdict on a PR.

    The reviewer's judgement is the part only a model can do. Writing it as
    prose and then acting on it is what leaves nothing checkable afterwards, so
    the verdict is written here, in one shape, stamped with the commit it
    actually reviewed.
    """
    head = (_gh_json("gh", "pr", "view", str(pr), "--repo", repo,
                     "--json", "headRefOid,state") or {})
    if head.get("state") != "OPEN":
        raise GitHubError("PR #{} is {}, not open".format(pr, head.get("state")))
    sha = head.get("headRefOid")
    if not sha:
        raise GitHubError("could not read the head commit of PR #{}".format(pr))

    body = {
        "verdict": verdict,
        "ci": ci,
        "head_sha": sha,
        "blocking": blocking,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }
    if note:
        body["note"] = note

    comment = "{}\n\n**Review: {}** (CI {})\n\n```json\n{}\n```".format(
        REVIEW_MARKER, verdict, ci, json.dumps(body, indent=2, sort_keys=True))
    if blocking:
        comment += "\n\nBlocking:\n" + "\n".join("- " + b for b in blocking)

    out = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", comment],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    print("recorded {} on PR #{} against {}".format(verdict, pr, sha[:12]))
    return 0


def merge_blockers(repo: str, pr: int, items: List[Item],
                   now: datetime) -> List[str]:
    """Every reason this PR may not be merged. Empty means it may.

    Deliberately a list rather than a bool: a gate that says only "no" makes the
    caller guess, and the reviewer needs to know which condition to fix.
    """
    why: List[str] = []
    data = _gh_json("gh", "pr", "view", str(pr), "--repo", repo, "--json",
                    "state,headRefName,headRefOid,mergeable,statusCheckRollup") or {}
    if not data:
        return ["PR #{} could not be read".format(pr)]

    if data.get("state") != "OPEN":
        why.append("PR is {}, not open".format(data.get("state")))

    branch = data.get("headRefName") or ""
    if not branch.startswith("ticket/"):
        why.append("branch {!r} is not a ticket/<n> branch".format(branch))
    else:
        ref = "{}#{}".format(repo, branch.split("/", 1)[1])
        ticket = next((i for i in items if i.ref == ref), None)
        if ticket is None:
            why.append("no ticket {} in the funnel".format(ref))
        else:
            parent = next((i for i in items if i.ref == ticket.parent), None)
            if parent is None or parent.status != "Building":
                why.append("{}'s project is not Building".format(ref))

    checks = data.get("statusCheckRollup") or []
    failed = [c.get("name") or c.get("context") for c in checks
              if (c.get("conclusion") or c.get("state")) not in
              ("SUCCESS", "NEUTRAL", "SKIPPED", None)]
    if failed:
        why.append("CI not green: " + ", ".join(str(f) for f in failed))
    elif not checks:
        why.append("no CI checks reported — refusing to merge unverified work")

    verdict = latest_verdict(repo, pr)
    if verdict is None:
        why.append("no review verdict recorded")
    else:
        if verdict.get("verdict") != "approved":
            why.append("latest review says {!r}".format(verdict.get("verdict")))
        # The decisive check. Without it, a push after approval merges on the
        # strength of a review that never saw it.
        if verdict.get("head_sha") != data.get("headRefOid"):
            why.append(
                "the approved commit {} is not the head {} — new commits since "
                "the review".format(
                    str(verdict.get("head_sha"))[:12],
                    str(data.get("headRefOid"))[:12]))
    return why


def cmd_merge(items: List[Item], now: datetime, repo: str, pr: int,
              confirmed: bool) -> int:
    """Merge a PR, but only when every condition holds.

    The model decides *approval*; this decides *merge*. A model adds value
    judging whether a diff matches the plan. It adds none by being the component
    that types `gh pr merge`, and being that component is what makes an
    unattended merge impossible to audit — which is why v0 is still unaccepted.
    """
    why = merge_blockers(repo, pr, items, now)
    if why:
        print("refusing to merge PR #{}:".format(pr), file=sys.stderr)
        for reason in why:
            print("  - " + reason, file=sys.stderr)
        return 1

    if not confirmed:
        print("PR #{} passes every merge condition.".format(pr))
        print("Nothing was changed. Re-run with --yes to merge.")
        return 0

    out = subprocess.run(
        ["gh", "pr", "merge", str(pr), "--repo", repo, "--squash",
         "--delete-branch"], capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    print("merged PR #{}".format(pr))
    return 0


def cmd_show(items: List[Item], now: datetime, ref: str) -> int:
    """Everything needed to answer this item's gate.

    A gate answered without context is not a decision, it is a coin toss. What
    matters differs by gate: the plan at Shaped, the tickets at Ready, and at
    Building what actually shipped and what the reviewer said about it.
    """
    item = find(items, ref)
    print("{}  {}".format(item.ref, item.title))
    print("{}  |  Class {}  |  {} at this gate".format(
        item.status or "no status", item.klass or "unset",
        humanise(item.waited(now))))
    print(item.url)
    print("=" * 72)
    print("GATE: {}".format(gate_question(item) or "not waiting on you"))
    print("")

    owner, name = item.repo.split("/")

    if item.status == "Shaped":
        detail = _gh_json("gh", "issue", "view", str(item.number), "--repo",
                          item.repo, "--json", "body") or {}
        body = (detail.get("body") or "").strip()
        print("--- the plan ---")
        print(body if len(body) < 4000 else body[:4000] + " ...[truncated]")
        print("")

    if item.children_total:
        data = gh_graphql(SUB_ISSUES, owner=owner, name=name, number=item.number)
        issue = (data.get("repository") or {}).get("issue") or {}
        children = (issue.get("subIssues") or {}).get("nodes") or []
        print("--- tickets ({}/{} closed) ---".format(
            item.children_done, item.children_total))
        for child in children:
            mark = "x" if child["state"] == "CLOSED" else " "
            print("  [{}] #{} {}".format(mark, child["number"], child["title"]))
            pr = _ticket_pr(child["repository"]["nameWithOwner"], child["number"])
            if pr:
                print("        PR #{} {}{}".format(
                    pr["number"], pr["state"].lower(),
                    " (merged)" if pr.get("mergedAt") else ""))
                for review in (pr.get("reviews") or [])[-2:]:
                    text = " ".join((review.get("body") or "").split())
                    if text:
                        print("        review: {}".format(text[:200]))
            elif child["state"] == "CLOSED":
                print("        closed with no ticket/* PR -- check why")
        print("")

    comments = (_gh_json("gh", "issue", "view", str(item.number), "--repo",
                         item.repo, "--json", "comments") or {}).get("comments", [])
    if comments:
        print("--- comments ({}) ---".format(len(comments)))
        for c in comments[-4:]:
            text = " ".join((c.get("body") or "").split())
            print("  {}: {}".format(
                (c.get("author") or {}).get("login", "?"), text[:200]))
        print("")

    if item.status == "Building":
        merged = [m for m in unattended_merges(now) if m.get("pr")]
        print("--- merged without you, last 30 days: {} ---".format(len(merged)))
        for m in merged[-5:]:
            print("  PR #{} at {}".format(m["pr"], m["at"][:16]))
        rejected = rejected_merges(items, now)
        print("rejected merges in the last {} days: {}{}".format(
            rejected["window_days"], rejected["count"],
            "   ** STOP AUTO-MERGING **" if rejected["stop_auto_merging"] else ""))
        print("")

    verb = {frm: v for v, (frm, _, _) in ANSWERS.items()}.get(item.status or "")
    if verb:
        print("Answer it:  python3 funnel.py {} {} --yes".format(verb, item.number))
    return 0


def cmd_answer(items: List[Item], now: datetime, verb: str, ref: str,
               confirmed: bool) -> int:
    """Answer a gate: move an item to the next stage.

    The brief shows what is waiting and asks the question; without this, the
    only way to answer was to open the Project and change a dropdown. Friction
    here lands on the one resource `plan.md` calls the bottleneck.

    **Dry run unless `--yes` is passed.** These commands answer Nate's gates, and
    the comment above `ANSWERS` saying no agent may run them proved insufficient
    on the day it was written: an agent ran `accept` as a guard test and moved v0
    to Done. A note in the source is not a control. Defaulting to a dry run means
    the reflexive way to try one of these is also the harmless way.
    """
    expected, nxt, meaning = ANSWERS[verb]
    item = find(items, ref)

    if item.status != expected:
        raise GitHubError(
            "{} is at {}, not {} — `{}` answers the {} gate".format(
                item.ref, item.status or "no status", expected, verb, expected)
        )
    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))

    if verb == "accept" and not item.children_all_closed:
        raise GitHubError(
            "{} still has open tickets ({}/{} closed). Accepting a project whose "
            "work is unfinished is how a thing gets called shipped while a third "
            "of it is missing.".format(
                item.ref, item.children_done, item.children_total)
        )

    if not confirmed:
        print("would move {} from {} to {} ({})".format(
            item.ref, expected, nxt, meaning))
        if verb == "accept":
            print("and close it as completed")
        print("\nNothing was changed. Re-run with --yes to answer the gate.")
        return 1

    gh_graphql(SET_FIELD, project=PROJECT_ID, item=item.item_id,
               field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, nxt))

    if verb == "accept":
        # Done and Parked must stay distinguishable: `completed` here,
        # `not planned` for a park. That ratio is the only way to tell whether
        # the gates are set right.
        subprocess.run(
            ["gh", "issue", "close", str(item.number), "--repo", item.repo,
             "--reason", "completed"], capture_output=True)

    print("{} → {}  ({})".format(item.ref, nxt, meaning))
    if verb == "approve":
        print("The next Claude run will break it into tickets.")
    elif verb == "start":
        print("Codex can now pick up its tickets.")
    return 0


def cmd_release(items: List[Item], now: datetime, ref: str) -> int:
    write_lock(find(items, ref), "")
    return 0


def _parking_reason(value: str) -> str:
    """Reject blank reasons during argument parsing, before GitHub is read."""
    reason = value.strip()
    if not reason:
        raise argparse.ArgumentTypeError("a non-empty parking reason is required")
    return reason


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("queue", help="everything, ordered")
    nxt = sub.add_parser(
        "next", help="the single next ticket Codex should work, or nothing")
    nxt.add_argument(
        "--tier", choices=TIERS, default=None,
        help="what this engine is allowed to work. `standard` skips tickets "
             "needing the escalated engine; `escalated` may take anything. "
             "Declared by the routine, never by the model.")
    sub.add_parser("brief", help="JSON for the /funnel skill and the morning brief")
    sub.add_parser("ideas", help="captured ideas, flagged ones first")
    show = sub.add_parser("show", help="everything needed to answer an item's gate")
    show.add_argument("ref", help="issue number, owner/repo#number, or URL")
    for verb, (frm, to, meaning) in ANSWERS.items():
        answer = sub.add_parser(
            verb, help="Nate's answer at the {} gate: {} ({} → {})".format(
                frm, meaning, frm, to))
        answer.add_argument("ref", help="issue number, owner/repo#number, or URL")
        answer.add_argument(
            "--yes", action="store_true", dest="confirmed",
            help="actually do it; without this the command is a dry run",
        )
    capture = sub.add_parser("capture", help="capture an idea into the funnel")
    capture.add_argument("title")
    capture.add_argument("--note", default=None, help="anything worth keeping now")
    capture.add_argument("--repo", default=REPO)
    capture.add_argument("--needs-shaping", action="store_true", dest="shaping",
                         help="flag it as worth thinking through")
    shaped = sub.add_parser("shaped", help="record a grilled plan and move to Shaped")
    shaped.add_argument("ref", help="issue number, owner/repo#number, or URL")
    shaped.add_argument("--plan", required=True, help="file holding the plan")
    claim = sub.add_parser("claim", help="take the single-in-motion lock on a ticket")
    claim.add_argument("ref", help="issue number, owner/repo#number, or URL")
    release = sub.add_parser("release", help="give up the lock on a ticket")
    release.add_argument("ref", help="issue number, owner/repo#number, or URL")
    park = sub.add_parser("park", help="stop a project and record why")
    park.add_argument("ref", help="issue number, owner/repo#number, or URL")
    park.add_argument(
        "--reason", required=True, type=_parking_reason,
        help="why this project is being stopped (required)",
    )
    reject = sub.add_parser(
        "reject", help="a merged PR turned out to be broken: undo and record it")
    reject.add_argument("pr", help="PR number or URL")

    review = sub.add_parser(
        "review", help="record a structured review verdict on a PR")
    review.add_argument("pr", type=int)
    review.add_argument("--repo", default=REPO)
    review.add_argument("--verdict", required=True, choices=VERDICTS)
    review.add_argument("--ci", required=True, choices=CI_STATES)
    review.add_argument("--blocking", action="append", default=[],
                        help="one blocking finding; repeat for more")
    review.add_argument("--note", default=None)

    merge = sub.add_parser(
        "merge", help="merge a PR if every condition holds — dry run without --yes")
    merge.add_argument("pr", type=int)
    merge.add_argument("--repo", default=REPO)
    merge.add_argument("--yes", action="store_true", dest="confirmed")

    nxr = sub.add_parser(
        "next-review", help="the single PR this reviewer should read, or nothing")
    nxr.add_argument("--tier", choices=TIERS, default=None,
                     help="review only work of this risk tier. Declared by the "
                          "routine, the same way an engine declares its own.")
    reject.add_argument("--note", default=None, help="what is broken")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    try:
        items = load_items()
    except GitHubError as exc:
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "claim":
            return cmd_claim(items, now, args.ref)
        if args.command == "release":
            return cmd_release(items, now, args.ref)
        if args.command == "park":
            return cmd_park(items, now, args.ref, args.reason)
        if args.command == "reject":
            return cmd_reject(items, now, args.pr, args.note)
        if args.command in ANSWERS:
            return cmd_answer(items, now, args.command, args.ref, args.confirmed)
        if args.command == "show":
            return cmd_show(items, now, args.ref)
        if args.command == "ideas":
            return cmd_ideas(items, now)
        if args.command == "capture":
            return cmd_capture(items, now, args.title, args.note, args.repo,
                               args.shaping)
        if args.command == "shaped":
            return cmd_shaped(items, now, args.ref, args.plan)
        if args.command == "next-review":
            return cmd_next_review(items, args.tier)
        if args.command == "review":
            return cmd_review(args.repo, args.pr, args.verdict, args.ci,
                              args.blocking, args.note)
        if args.command == "merge":
            return cmd_merge(items, now, args.repo, args.pr, args.confirmed)
        if args.command == "next":
            return cmd_next(items, now, tier=getattr(args, "tier", None))
        return {"queue": cmd_queue, "brief": cmd_brief}[args.command](
            items, now
        )
    except GitHubError as exc:
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
