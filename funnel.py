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
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence

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


def startable(items: Sequence[Item]) -> List[Item]:
    """Tickets Codex may pick up, best-first.

    A ticket is an open issue with no children of its own, whose parent has
    passed the Ready gate. Tickets inherit their parent's Class — the ladder
    ranks projects, not individual tickets.
    """
    by_ref = {i.ref: i for i in items}

    def eligible(item: Item) -> bool:
        if item.state != "OPEN" or item.is_blocked or item.children_total:
            return False
        parent = by_ref.get(item.parent or "")
        if parent is None:
            # A standalone issue is its own project; it must itself be startable.
            return item.status in ("Ready", "Building") and not item.is_blocked
        return parent.status in ("Ready", "Building") and not parent.is_blocked

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


def lock_holder(items: Iterable[Item], now: datetime) -> Optional[Item]:
    """The item currently holding the single-in-motion lock, if any.

    The lock is an open ticket carrying a claim younger than the TTL. The claim
    is written at run start, which is when the lock must exist — a PR or a
    branch appears too late to cover the window in which a run most often dies.
    """
    held = [
        i
        for i in items
        if i.state == "OPEN"
        and i.in_motion_since is not None
        and now - i.in_motion_since < LOCK_TTL
    ]
    return sorted(held, key=lambda i: i.in_motion_since or now)[0] if held else None


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


def next_ticket(items: Sequence[Item], now: datetime) -> Optional[Item]:
    """The single ticket Codex should work, or None.

    Returns None when the lock is genuinely held. A Broken ticket may take the
    lock before the TTL expires; that is the one sanctioned preemption.
    """
    queue = startable(items)
    if not queue:
        return None

    holder = lock_holder(items, now)
    if holder is None:
        return queue[0]

    by_ref = {i.ref: i for i in items}

    if effective_class(holder, by_ref) != "Broken":
        for candidate in queue:
            if effective_class(candidate, by_ref) == "Broken" and candidate.ref != holder.ref:
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

    missing = [i for i in items if needs_class(i)]
    if missing:
        print("\nInvalid — not in Ideas and carrying no Class ({}):".format(len(missing)))
        for item in missing:
            print("  {:<34} {}".format(item.ref, item.url))
    return 0


def cmd_next(items: List[Item], now: datetime) -> int:
    ticket = next_ticket(items, now)
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

    holder = lock_holder(items, now)
    brief = {
        "generated_at": now.isoformat(),
        "total_needing_nate": len(decisions),
        "counts_by_gate": counts,
        "items": [item_json(i, now, by_ref) for i in decisions],
        "needs_class": [item_json(i, now, by_ref) for i in items if needs_class(i)],
        "in_motion": holder.ref if holder else None,
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
    """Take the single-in-motion lock, or refuse.

    Refusing is the normal outcome and is not an error worth shouting about;
    the caller distinguishes by exit code.
    """
    target = find(items, ref)
    holder = lock_holder(items, now)
    if holder is not None and holder.ref != target.ref:
        print(
            "refused — lock held by {} (claimed {} ago)".format(
                holder.ref, humanise(now - holder.in_motion_since)
            ),
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


def cmd_reject(items: List[Item], now: datetime, pr: str, note: Optional[str]) -> int:
    """Record that a merged PR turned out to be broken.

    This is not "file a bug". The information is that **the auto-merge bar
    failed**, which is a different and more serious fact, and it is the only
    feedback loop on letting Claude merge unattended. One action does all four
    steps, rather than leaving them to be remembered.

    This is also the single case in the design where `Class` is written by code
    rather than by Nate.
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


def cmd_release(items: List[Item], now: datetime, ref: str) -> int:
    write_lock(find(items, ref), "")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("queue", help="everything, ordered")
    sub.add_parser("next", help="the single next ticket Codex should work, or nothing")
    sub.add_parser("brief", help="JSON for the /funnel skill and the morning brief")
    claim = sub.add_parser("claim", help="take the single-in-motion lock on a ticket")
    claim.add_argument("ref", help="issue number, owner/repo#number, or URL")
    release = sub.add_parser("release", help="give up the lock on a ticket")
    release.add_argument("ref", help="issue number, owner/repo#number, or URL")
    reject = sub.add_parser(
        "reject", help="a merged PR turned out to be broken: undo and record it")
    reject.add_argument("pr", help="PR number or URL")
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
        if args.command == "reject":
            return cmd_reject(items, now, args.pr, args.note)
        return {"queue": cmd_queue, "next": cmd_next, "brief": cmd_brief}[args.command](
            items, now
        )
    except GitHubError as exc:
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
