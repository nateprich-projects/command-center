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
from collections import namedtuple
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# --------------------------------------------------------------------------
# Configuration. These are the only knobs; everything else is derived.
# --------------------------------------------------------------------------

PROJECT_OWNER = "nateprich"
PROJECT_NUMBER = 2
TOPIC = "command-center"
OWNERS = [("user", "nateprich"), ("organization", "nateprich-projects")]
REPO = "nateprich-projects/command-center"

# The local checks deliberately keep their paths as module-level values. Tests
# can point them at a temporary checkout and home directory without ever
# reading the real ~/.claude.
CHECKOUT_ROOT = pathlib.Path(__file__).resolve().parent
CLAUDE_DIR = pathlib.Path.home() / ".claude"

# The routine declares its own identity in the opening command. The argument
# is deliberately removed rather than replaced with a placeholder: the file
# with no literal and the same file after the literal is pasted must hash to
# the same bytes. Keeping this normalisation here gives the paste helper and
# `begin` one implementation to share.
ROUTINE_SHA_ARGUMENT = re.compile(rb"(?:[ \t]+)?--routine-sha[ \t]+\S+")


def normalized_routine(path: os.PathLike) -> bytes:
    """Read a routine with its self-referential sha argument removed."""
    return ROUTINE_SHA_ARGUMENT.sub(b"", pathlib.Path(path).read_bytes())


def routine_sha(path: os.PathLike) -> str:
    """Return the stable sha256 of a routine, ignoring its own literal."""
    return hashlib.sha256(normalized_routine(path)).hexdigest()


def routine_path(agent: str) -> pathlib.Path:
    """The checked-in routine whose pasted copy identifies itself."""
    return CHECKOUT_ROOT / "routines" / (agent + ".md")

# One small, shared shape for every doctor check. Later doctor tickets add
# checks to the fixed list without changing the report contract.
Check = namedtuple("Check", "name ok found fix")

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
#:
#: **Raised 2 -> 4 on 2026-09-08, on the condition this comment already set.**
#: "Raise it when review stops being the constraint" — review has stopped being
#: the constraint, and it is measured rather than assumed: Muse reviews on a
#: five-minute standard schedule plus an hourly escalated one, is unmetered, and
#: finished 48 runs `nothing-to-do` in the six hours to 08:24 for want of a PR to
#: look at. The premise that made two right — Codex outrunning a metered Claude
#: reviewer — no longer holds.
#:
#: **What this does not fix, and what to watch.** Parallelism multiplies whatever
#: the queue hands out. Six of the top twelve startable tickets carry a prose
#: dependency `startable()` cannot read, so extra sessions can be handed chained
#: work, decline it, and record `errored` (#175) only to be handed it again
#: (#177) — on 2026-09-08 three runs opened within one minute and all three took
#: the same unworkable ticket. More branches against a faster-moving `main` also
#: means more conflicts, and `merge_blockers` still does not read the `mergeable`
#: it fetches (#161), so the gate passes a conflicting branch and fails at
#: `gh pr merge`. If this raise goes badly, those are the two reasons; #129's
#: #153/#154 and #161 are the fixes, not a lower number.
WIP_LIMIT = 4

#: Funnel order. Index is the stage's depth; later means further along.
STAGES = ["Ideas", "Shaped", "Ready", "Building", "Done", "Parked"]

#: The ladder, best-first. Only finite classes may preempt in-flight work.
LADDER = ["Broken", "Maintenance", "Improve", "New", "Replace"]
PREEMPTING = {"Broken", "Maintenance"}

#: Existing-work classes may take the unattended shaping path. Origin remains
#: an independent condition: class describes the work, not who raised it.
SELF_APPROVABLE_CLASSES = frozenset({"Broken", "Maintenance", "Improve"})

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

#: A blocked comment names an optional, knowable condition after this marker.
#: The parser below owns the rest of the fixed header shape.
BLOCK_COMMENT_PREFIX = "**Blocked"
BLOCK_COMMENT_RE = re.compile(
    r"\A" + re.escape(BLOCK_COMMENT_PREFIX)
    + r"(?: on (?P<references>#[0-9]+(?: and #[0-9]+)*))?:\*\*"
)

#: Existing comments have no machine-readable provenance. Until the provenance
#: marker lands, show must fail closed rather than treat the GitHub account as
#: authorship.
UNATTRIBUTED = "UNATTRIBUTED"

#: Issue comments carry the voice that GitHub's author field cannot establish.
#: Keep this in the same HTML-comment-plus-JSON shape as REVIEW_MARKER below.
PROVENANCE_MARKER = "<!-- command-center-provenance -->"
PROVENANCE_VOICES = ("nate-direct", "nate-relayed", "agent")

#: An origin is only a default for who shapes an idea. This marker records the
#: explicit exception without rewriting that historical fact. Moving work back
#: toward Nate is always safe; moving it toward agents requires Nate's voice.
ORIGIN_OVERRIDE_MARKER = "<!-- command-center-origin-override -->"
ORIGIN_OVERRIDE_TARGETS = ("nate", "agents")

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
    body: Optional[str] = None
    state_reason: Optional[str] = None  # COMPLETED | NOT_PLANNED | REOPENED
    status: Optional[str] = None
    klass: Optional[str] = None
    status_since: Optional[datetime] = None
    labels: List[str] = field(default_factory=list)
    block_references: List[str] = field(default_factory=list)
    block_reason: Optional[str] = None
    open_blockers: List[str] = field(default_factory=list)
    dead_blockers: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    in_motion_since: Optional[datetime] = None
    item_id: Optional[str] = None  # the ProjectV2Item, needed to write the lock
    parent: Optional[str] = None  # "owner/repo#123"
    children_total: int = 0
    children_done: int = 0
    first_child_created_at: Optional[datetime] = None
    last_child_closed_at: Optional[datetime] = None
    blocked_since: Optional[datetime] = None
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
        """How long this item has waited on its current gate or work queue."""
        since = question_since(self)
        if since is None:
            return None
        return now - since


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


def dependency_descendants(items: Sequence[Item]) -> Dict[str, Set[str]]:
    """Return the tickets each item transitively blocks.

    ``open_blockers`` points from the waiting ticket to its prerequisite. The
    rank needs the reverse direction, so this builds that graph from the
    native dependency facts already loaded on each item. The walk is
    iterative on purpose: a malformed or manually-created cycle must not make
    queue calculation recurse forever. References outside the loaded Project
    are ignored because there is no item here that can be sorted.
    """
    by_ref = {item.ref: item for item in items}
    downstream = {ref: set() for ref in by_ref}
    for dependent in items:
        for blocker_ref in dependent.open_blockers:
            if blocker_ref in by_ref:
                downstream[blocker_ref].add(dependent.ref)

    descendants: Dict[str, Set[str]] = {}
    for ref in by_ref:
        found: Set[str] = set()
        pending = list(downstream[ref])
        while pending:
            child = pending.pop()
            if child == ref or child in found:
                continue
            found.add(child)
            pending.extend(downstream.get(child, ()))
        descendants[ref] = found
    return descendants


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
        # A named condition is knowable work for the system, not a question for
        # Nate. A silent block still needs his attention, but only a project
        # can be parked; a ticket can only be unblocked.
        if item.block_references:
            return None
        return "Unblock?" if item.parent else "Unblock or park?"
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


def question_since(item: Item) -> Optional[datetime]:
    """When the item's current question became live.

    ``gate_question`` is the authority for which question is live. A Ready
    project starts asking "Start now?" when its first sub-issue exists, not
    when the project entered Ready. A Building project starts asking "Accept
    it?" when its last sub-issue closes. A blocked item starts asking its
    unblock question when the ``blocked`` label is applied. The status
    timestamp remains the fallback for older or partial fixture data without
    the event details.
    """
    question = gate_question(item)
    if item.is_blocked and question is not None:
        return item.blocked_since or item.status_since
    if question == GATES["Building"]:
        return item.last_child_closed_at or item.status_since
    if question == GATES["Ready"]:
        return item.first_child_created_at or item.status_since
    return item.status_since


def awaiting_decision(items: Iterable[Item]) -> List[Item]:
    """Nate's queue: everything waiting on him, bottom-up, oldest first.

    Bottom-up because the longest-stalled, furthest-along item is the most
    likely park candidate, and surfacing it first is what makes this ordering
    do disposal work rather than merely sequencing.
    """
    rows = list(items)
    by_ref = {i.ref: i for i in rows}

    def key(item: Item):
        status = item.status or ""
        try:
            depth = DECISION_ORDER.index(status)
        except ValueError:
            depth = len(DECISION_ORDER)
        # Blocked items sort with their stage but ahead of it within the stage.
        since = question_since(item) or datetime.max.replace(tzinfo=timezone.utc)
        return (
            depth,
            not item.is_blocked,
            0 if effective_class(item, by_ref) == "Broken" else 1,
            since,
            item.repo,
            item.number,
        )

    return sorted((i for i in rows if gate_question(i)), key=key)


def ideas(items: Iterable[Item]) -> List[Item]:
    """Captured ideas, with Broken items first and oldest items next.

    `brief` deliberately excludes Ideas from its counts — it is unbounded and
    guilt-free, and counting it turns it into pressure. But grilling is what
    feeds everything downstream, so there has to be *some* way to ask what is
    waiting to be shaped. This is it, and it is asked for rather than pushed.
    """
    rows = list(items)
    by_ref = {i.ref: i for i in rows}
    return sorted(
        (i for i in rows if i.state == "OPEN" and i.status == "Ideas"),
        key=lambda i: (
            0 if effective_class(i, by_ref) == "Broken" else 1,
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

#: A ticket declares a step that only Nate can perform in its body, written at
#: breakdown. The reason is deliberately an allowlist: inability to figure out
#: engineering work is not a reason to route that work to him.
HUMAN_STEP_PREFIX = "Human step: "
HUMAN_STEP_REASONS = (
    "an app UI with no API",
    "entering a credential",
    "an account or billing setting",
    "physical access to a machine",
)
HUMAN_STEP_LINE = re.compile(
    r"^\s*" + re.escape(HUMAN_STEP_PREFIX)
    + r"(?P<reason>"
    + "|".join(re.escape(reason) for reason in HUMAN_STEP_REASONS)
    + r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_human_step(body: str) -> Optional[str]:
    """Return an allowlisted human-step reason from a ticket body.

    Like ``RISK_LINE``, the marker must begin a body line. Matching only the
    stated access reasons keeps a ticket from becoming Nate's work merely
    because an agent found it difficult.
    """
    if not isinstance(body, str):
        return None
    match = HUMAN_STEP_LINE.search(body)
    return match.group("reason") if match else None

#: A safety boundary for tickets written before markers existed, or by someone
#: who forgot. False positives cost one escalated review; false negatives can
#: authorise risky work unattended, so these deliberately match the vocabulary
#: plans use when describing risky actions — including rejected alternatives.
#: Category names and `lock`, `park`, `close` or `delete` alone stay ordinary
#: subject matter: this repository discusses them even when no risky action is
#: proposed, and matching them would keep the standard engine from ever running.
ESCALATION_PATTERNS = {
    "credentials": r"(?<!no )\b(api[- ]key|access token|client secret|"
                   r"credential store|password|private key)\b|"
                   r"(?<!not )(?<!never )\b(?:access|chang|creat|enter|expos|"
                   r"handl|load|read|replac|revok|rotat|stor|suppl|touch|"
                   r"use|uses|used|using|writ)\w*"
                   r"(?:\s+(?!(?:no|not|nothing)\b)[\w'’-]+){0,4}"
                   r"\s+credentials?\b",
    "authorisation": r"(?<!no )(?<!not )(?<!never )\b("
                     r"authoris(?:e|es|ed|ing)|authoriz(?:e|es|ed|ing)|"
                     r"permission model|access control|oauth|scope grant)\b|"
                     r"(?<!not )(?<!never )\b(?:broaden|chang|elevat|expand|"
                     r"grant|reduc|revok|tighten)\w*"
                     r"(?:\s+(?!(?:no|not|nothing)\b)[\w'’-]+){0,4}"
                     r"\s+permissions?\b",
    "data-migration": r"(?<!no )\b(data migration|schema migration|backfill|"
                      r"irreversible migration|migrat(?:e|es|ed|ing))\b",
    "destructive": r"(?<!no )\b(force[- ]push|hard delete|permanently delete|"
                   r"drop the (table|branch)|rewrite history|"
                   r"(?:allow|enable|perform|permit|run)\w*"
                   r"(?:\s+[\w'’-]+){0,4}\s+(?:destructive|irreversible)"
                   r"\s+(?:actions?|changes?|commands?|operations?))\b",
    "concurrency": r"(?<!no )\b(race condition|deadlock|thread[- ]safe|mutex|"
                   r"atomic (write|commit)|"
                   r"(?:concurrent|overlapping)\s+(?:mutations?|processes|runs?|"
                   r"writers?|writes?))\b",
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


NEEDS_NATE_PATTERNS = {
    # A citation is only a signal when it is being used to ground authority,
    # not when a plan happens to mention either instruction file.
    "policy authority": (
        r"\b(?:plan\.md|agents\.md)\b[^.!?]{0,160}"
        r"\b(?:gate|membership|who\s+may\s+(?:write|set|change|edit|update)|"
        r"what\s+(?:an\s+)?agent\s+may\s+(?:do|write|set|approve)|"
        r"agents?\s+(?:may|can|must|may\s+not))\b|"
        r"\b(?:gate|membership|who\s+may\s+(?:write|set|change|edit|update))\b"
        r"[^.!?]{0,160}\b(?:plan\.md|agents\.md)\b"
    ),
    "unattended authority": (
        r"\bself[- ]approv\w*\b|"
        r"\b(?:unattended)\b[^.!?]{0,160}"
        r"\b(?:approv\w*|merge\w*|agent|routine|automation|"
        r"write\w*|set\w*|create\w*|act\w*)\b|"
        r"\b(?:agent|agents|routine|automation)\b[^.!?]{0,160}"
        r"\b(?:without\s+(?:Nate|him|human|a\s+human|review|approval)|"
        r"on\s+its\s+own|by\s+itself|skip(?:s|ping)?\s+(?:Nate|him)|"
        r"unattended)\b"
    ),
    "gate authority": (
        r"\b(?:gate|gates)(?:'s)?[^.!?]{0,120}"
        r"\b(?:question|answer|answered|who\s+answers|owner|owned|"
        r"belongs|claim|decision)\b|"
        r"\b(?:question|answer|answered|who\s+answers|owner|owned|belongs)\b"
        r"[^.!?]{0,120}\b(?:gate|gates)\b|"
        r"\b(?:add|adds|remove|removes|change|changes|replace|replaces|"
        r"redefine\w*|move\w*|shift\w*|become\w*|turn\w*)\b"
        r"[^.!?]{0,80}\b(?:a\s+)?(?:gate|gates)\b"
    ),
    "field authority": (
        r"\b(?:who|which|only\s+Nate|Nate|agent|agents|routine|"
        r"automation|human)\b[^.!?]{0,100}"
        r"\b(?:may|can|must|is\s+(?:allowed|authori[sz]ed)\s+to|will)?\s*"
        r"(?:set|write|change|edit|update|assign)\w*\b[^.!?]{0,120}"
        r"\b(?:field|fields|status|class|label|labels|marker|markers|option|"
        r"options)\b|"
        r"\b(?:field|fields|status|class|label|labels|marker|markers|option|"
        r"options)\b[^.!?]{0,120}"
        r"\b(?:set|written|changed|edited|updated|assigned)\b[^.!?]{0,100}"
        r"\b(?:by|who|agent|agents|Nate)\b"
    ),
}


def needs_nate_signals(plan_body: str) -> List[str]:
    """Return authority signals that contradict an all-clear Needs section.

    The section parser owns whether a plan explicitly asks Nate a question.
    This scan is the independent check against the plan's own prose: it reports
    authority-shaped claims without judging how risky the plan is. Matching is
    deliberately sentence-local so ordinary discussion of gates and fields
    does not become an authority claim merely because the vocabulary appears.
    """
    text = re.sub(r"\s+", " ", plan_body or "")
    return [name for name, pattern in sorted(NEEDS_NATE_PATTERNS.items())
            if re.search(pattern, text, re.IGNORECASE)]


PLAN_HEADING = re.compile(r"^\s{0,3}(?P<marks>#{1,6})\s+(?P<title>.*?)\s*#*\s*$")
NEEDS_NATE_HEADINGS = {"needs nate", "needs you"}
EMPTY_NEEDS_NATE = {"", "nothing", "nothing."}


def _needs_nate_sections(plan_body: str) -> List[str]:
    """Return the contents of every recognised Needs section in a plan."""
    if not isinstance(plan_body, str):
        return []

    sections: List[str] = []
    lines = plan_body.splitlines()
    index = 0
    while index < len(lines):
        match = PLAN_HEADING.match(lines[index])
        if not match:
            index += 1
            continue

        title = match.group("title").strip().lower().rstrip(":").strip()
        if title not in NEEDS_NATE_HEADINGS:
            index += 1
            continue

        level = len(match.group("marks"))
        body: List[str] = []
        index += 1
        while index < len(lines):
            next_heading = PLAN_HEADING.match(lines[index])
            if next_heading and len(next_heading.group("marks")) <= level:
                break
            body.append(lines[index])
            index += 1
        sections.append("\n".join(body))

    return sections


def plan_needs_nate(plan_body: str) -> bool:
    """Whether a plan's Needs Nate/Needs you section asks for Nate.

    A missing section is not an all-clear: the section itself is the plan's
    explicit evidence that Nate's questions were considered. Only a section
    containing exactly ``Nothing`` (with optional punctuation and whitespace)
    is empty. Multiple recognised sections fail closed if any one contains
    content. An otherwise empty section also fails closed when the plan body
    contains an authority signal that contradicts the section's claim.
    """
    sections = _needs_nate_sections(plan_body)
    if not sections:
        return True
    if any(section.strip().lower() not in EMPTY_NEEDS_NATE
           for section in sections):
        return True
    return bool(needs_nate_signals(plan_body))


def plan_is_escalated(plan_body: str) -> List[str]:
    """Return escalation reasons found across the whole plan body.

    Plans have no separate ticket title, so the plan is passed as the body to
    the shared escalation machinery. The empty-list result is the all-clear
    used by the self-approval condition.
    """
    return escalation_reasons("", plan_body)


# These are evidence words, not a closed list of human-step categories. A plan
# can use one while describing a rejected alternative or an already-automated
# action, so the scan is a prompt to inspect the checklist rather than proof
# that a human ticket is required. Keep the vocabulary in one place so real
# misses can extend it without a second copy drifting in the breakdown skill.
ACCESS_PATTERNS = {
    "account": r"\baccounts?\b",
    "api key": r"\bapi[\s-]+keys?\b",
    "billing": r"\bbilling\b",
    "dns": r"\bdns\b",
    "enable": r"\benabl\w*\b",
    "oauth": r"\boauth\b",
    "register": r"\b(?:register|registrat)\w*\b",
    "settings": r"\bsettings?\b",
    "sign in": r"\bsign(?:ed)?[\s-]+in\b",
    "token": r"\btokens?\b",
    "tunnel": r"\btunnels?\b",
    "verify": r"\bverif\w*\b",
}


def access_signals(plan_body: str) -> List[str]:
    """Return access-shaped vocabulary found in a plan body.

    This is the independent check used beside the breakdown boundary
    checklist. It deliberately reports vocabulary only: matching a word does
    not prove that the plan needs Nate, and no match does not prove that it
    does not.
    """
    if not isinstance(plan_body, str):
        return []
    text = re.sub(r"\s+", " ", plan_body)
    return [name for name, pattern in sorted(ACCESS_PATTERNS.items())
            if re.search(pattern, text, re.IGNORECASE)]


def effective_shape_owner(origin_voice: Optional[str],
                          override_target: Optional[str] = None) -> Optional[str]:
    """Return who should shape an item, or None when origin is untrusted.

    Capture uses the provenance voice vocabulary: ``agent`` means observed by
    an agent, while either Nate voice means he raised it. An authorised origin
    override is already reduced by its parser to ``nate`` or ``agents`` and
    supersedes that default. Missing or malformed origin fails closed here; the
    backlog-wide default remains #151's concern.
    """
    if override_target is not None:
        return override_target if override_target in ("nate", "agents") else None
    if origin_voice == "agent":
        return "agents"
    if origin_voice in ("nate-direct", "nate-relayed"):
        return "nate"
    return None


def self_approval_eligible(klass: Optional[str], origin_voice: Optional[str],
                           override_target: Optional[str], *,
                           needs_nate: bool, escalated: bool) -> bool:
    """Whether all conditions permit one unattended shaping transition.

    #77 supplies the plan booleans and #80 owns the transition. Keeping class,
    origin, the Needs-Nate result (including #84's authority verifier), and
    escalation in this one predicate prevents origin from becoming a second
    gate that can drift from the existing self-approval rule.
    """
    return (
        klass in SELF_APPROVABLE_CLASSES
        and effective_shape_owner(origin_voice, override_target) == "agents"
        and not needs_nate
        and not escalated
    )


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
    descendants = dependency_descendants(items)
    effective_rank = {
        ref: min(
            ladder_index(effective_class(by_ref[related], by_ref))
            for related in {ref} | descendants[ref]
        )
        for ref in by_ref
    }

    def eligible(item: Item) -> bool:
        if item.state != "OPEN" or item.is_blocked or item.open_blockers or item.children_total:
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
        since = question_since(item) or datetime.max.replace(tzinfo=timezone.utc)
        return (
            not in_flight(item),
            effective_rank[item.ref],
            # A blocker with the same effective rank as its dependent still
            # has to go first. An ancestor reaches every descendant, so this
            # count is strictly larger for an acyclic dependency edge while
            # remaining deterministic and finite for cycles.
            -len(descendants[item.ref]),
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


def _marked_json(body: str, marker: str) -> Optional[Dict]:
    """Read the first JSON block owned by ``marker``.

    A comment may carry both a review verdict and provenance. Parsing from one
    marker to the last closing brace would join those two objects and make the
    review gate silently lose a valid verdict. The first fenced JSON block (or
    an immediately following bare object for compatibility with early markers)
    belongs to the requested marker; another Command Center marker never does.
    """
    marker_at = body.find(marker)
    if marker_at < 0:
        return None

    rest = body[marker_at + len(marker):]
    next_marker = rest.find("<!-- command-center-")
    if next_marker >= 0:
        rest = rest[:next_marker]

    fenced = re.search(
        r"```json[ \t]*\r?\n(.*?)\r?\n```", rest, flags=re.DOTALL
    )
    if fenced:
        raw = fenced.group(1)
        try:
            found = json.loads(raw)
        except (TypeError, ValueError):
            return None
    else:
        raw = rest.lstrip()
        if not raw.startswith("{"):
            return None
        try:
            found, _ = json.JSONDecoder().raw_decode(raw)
        except (TypeError, ValueError):
            return None

    return found if isinstance(found, dict) else None


def parse_verdict(body: str) -> Optional[Dict]:
    """The verdict carried by one comment, or None if it is not one."""
    return _marked_json(body, REVIEW_MARKER)


def parse_provenance(body: str) -> Optional[Dict]:
    """The provenance fields carried by one comment, or None if malformed."""
    found = _marked_json(body, PROVENANCE_MARKER)
    if found is None or found.get("voice") not in PROVENANCE_VOICES:
        return None
    return found


def parse_origin_override(body: str) -> Optional[Dict]:
    """Return an authorised origin override, or None when it fails closed.

    Anyone may ask that an item be shaped with Nate. Only a marker carrying a
    Nate provenance voice may hand an item to agents for unattended shaping.
    """
    found = _marked_json(body, ORIGIN_OVERRIDE_MARKER)
    if found is None or found.get("target") not in ORIGIN_OVERRIDE_TARGETS:
        return None
    if found["target"] == "agents":
        provenance = parse_provenance(body)
        if provenance is None or provenance["voice"] == "agent":
            return None
    return found


def parse_block_comment(bodies: Iterable[str]) -> Optional[Tuple[List[str], str]]:
    """Return the newest parseable block comment's references and reason.

    The header is deliberately strict and anchored at the start of the body so
    an old or embedded mention cannot accidentally become a condition.
    """
    for body in reversed(list(bodies)):
        if not isinstance(body, str):
            continue
        match = BLOCK_COMMENT_RE.match(body)
        if not match:
            continue
        references = match.group("references")
        return (
            references.split(" and ") if references else [],
            body[match.end():].strip(),
        )
    return None


def _heartbeat_context(run: Optional[str], agent: Optional[str]):
    """Fill missing provenance metadata from the local heartbeat spool.

    An explicit value wins. If no run is supplied, infer it only when the
    spool has exactly one unfinished start; guessing among overlapping runs
    would recreate the attribution bug the marker is meant to fix.
    """
    run = run or os.environ.get("COMMAND_CENTER_RUN")
    agent = agent or os.environ.get("COMMAND_CENTER_AGENT")
    if run and agent:
        return run, agent

    spool = pathlib.Path.home() / ".claude" / "command-center-heartbeat"
    records = []
    try:
        for path in sorted(spool.glob("*.jsonl")):
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except (OSError, UnicodeError):
        return run, agent

    starts = {
        row.get("run"): row
        for row in records
        if row.get("phase") == "start" and row.get("run")
    }
    finished = {
        row.get("run")
        for row in records
        if row.get("phase") == "finish" and row.get("run")
    }
    if run and run in starts:
        agent = agent or starts[run].get("agent")
    elif not run:
        open_starts = [
            row for key, row in starts.items()
            if key not in finished and (not agent or row.get("agent") == agent)
        ]
        if len(open_starts) == 1:
            run = open_starts[0].get("run")
            agent = agent or open_starts[0].get("agent")

    if run and agent:
        return run, agent

    # A healthy heartbeat is normally drained from the local spool to GitHub
    # immediately. Read that durable copy as a fallback; failures here must
    # never prevent the comment itself from being posted.
    try:
        import heartbeat

        providers = sorted(heartbeat.PROVIDERS)
        if agent and agent in heartbeat.PROVIDERS:
            providers = [agent]
        candidates = []
        for provider in providers:
            for row in heartbeat.open_starts(heartbeat.read(provider)):
                if run and row.get("run") != run:
                    continue
                candidates.append(row)
        if run and len(candidates) == 1:
            agent = agent or candidates[0].get("agent")
        elif not run and len(candidates) == 1:
            run = candidates[0].get("run")
            agent = agent or candidates[0].get("agent")
    except Exception:
        # Provenance is additive instrumentation. A broken or unreachable
        # heartbeat must degrade to an unattributed marker, not lose the
        # underlying issue comment.
        pass
    return run, agent


def provenance_block(voice: str, at: Optional[datetime] = None,
                     run: Optional[str] = None,
                     agent: Optional[str] = None) -> str:
    """Build the invisible, machine-readable provenance block."""
    if voice not in PROVENANCE_VOICES:
        raise ValueError("unknown provenance voice {!r}".format(voice))
    run, agent = _heartbeat_context(run, agent)
    fields = {
        "agent": agent,
        "at": (at or datetime.now(timezone.utc)).isoformat(),
        "run": run,
        "voice": voice,
    }
    return "{}\n\n```json\n{}\n```".format(
        PROVENANCE_MARKER, json.dumps(fields, indent=2, sort_keys=True)
    )


def append_provenance(body: str, voice: str, at: Optional[datetime] = None,
                      run: Optional[str] = None,
                      agent: Optional[str] = None) -> str:
    """Append one provenance block without changing the supplied body."""
    return "{}\n\n{}".format(
        body, provenance_block(voice, at=at, run=run, agent=agent)
    )


def render_voice(body: str) -> str:
    """Render the four-value voice contract, failing closed when needed."""
    found = parse_provenance(body)
    if found is None:
        return UNATTRIBUTED
    voice = found["voice"]
    if voice == "nate-direct":
        return "Nate (direct)"
    agent = found.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return UNATTRIBUTED
    if voice == "nate-relayed":
        return "Nate (relayed by {})".format(agent)
    return agent


def _visible_comment(body: str) -> str:
    """Remove the invisible provenance trailer from a CLI preview."""
    marker_at = body.find(PROVENANCE_MARKER)
    if marker_at < 0:
        return body
    return body[:marker_at].rstrip()


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


def working_tree_touched(now: datetime) -> List[Dict[str, object]]:
    """Runs during which Nate's own checkout changed underneath him.

    No routine has business writing it — engineers work in their own clones and
    reviewers are read-only — but only Codex is actually prevented, by its
    sandbox. zcode has none, so this is detection where prevention is not
    available.

    It reports a *change*, not a crime: Nate committing while a run is in flight
    looks identical from here. The point is that a routine moving his checkout
    stops being invisible, which on 2026-09-06 it was — a run added worktrees and
    ran `git pull` in it, and nothing recorded that but the transcript.
    """
    found: List[Dict[str, object]] = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat

        for agent in sorted(heartbeat.PROVIDERS):
            rows = heartbeat.read(agent)
            starts = {r.get("run"): r for r in rows if r.get("phase") == "start"}
            for row in rows:
                if row.get("phase") != "finish":
                    continue
                began = starts.get(row.get("run"))
                before = (began or {}).get("repo")
                after = row.get("repo")
                if not before or not after or before == after:
                    continue
                if (row.get("ts") or 0) < (now - MAINTENANCE_WINDOW).timestamp():
                    continue
                found.append({
                    "agent": agent,
                    "run": row.get("run"),
                    "at": datetime.fromtimestamp(
                        row["ts"], timezone.utc).isoformat(),
                    "before": before,
                    "after": after,
                })
    except Exception:
        return []
    return sorted(found, key=lambda r: str(r["at"]))


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
# Local doctor checks. These deliberately do not import usage.py or
# heartbeat.py: those modules are among the things a broken install can make
# unreachable, and the doctor must still be able to report that failure.
# --------------------------------------------------------------------------


INSTALL_FIX = "bash scripts/install.sh"
STATUSLINE_COMMAND = "~/.claude/statusline.sh"

# `usage.py:36` reads this cache, while `statusline.sh:24` writes it. Keep the
# path here rather than importing usage.py: doctor has to diagnose a broken
# install when that module is one of the links that is missing.
USAGE_CACHE = pathlib.Path.home() / ".claude" / "command-center-usage.json"

# `usage.py:174` uses fifteen minutes to decide whether one gate reading is
# usable. Doctor answers a different question: has the status line stopped
# writing altogether? Claude Code refreshes this file repeatedly while open, so
# one hour (four missed gate intervals) distinguishes a stopped status line from
# a single stale reading without calling a 20-minute gap a broken install.
USAGE_CACHE_BROKEN_AFTER = timedelta(hours=1)
USAGE_CACHE_FIX = "open Claude Code on the Mac mini"

# These mirror `heartbeat.py:38` and `heartbeat.py:52`. They are intentionally
# local constants so doctor still works when heartbeat.py itself is unavailable.
HEARTBEAT_BRANCH = "heartbeat"
HEARTBEAT_SPOOL = pathlib.Path.home() / ".claude" / "command-center-heartbeat"
HEARTBEAT_FIX = "restore GitHub access so the heartbeat branch and local spool can drain"
AUTH_LOGIN_FIX = "gh auth login"
AUTH_SCOPE_FIX = "gh auth refresh -s project"
TOPIC_FIX = "add the `command-center` topic to at least one repository"


PROJECT_FIELDS_QUERY = """
query($login: String!, $number: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      fields(first: 100) {
        nodes {
          ... on ProjectV2Field { name }
          ... on ProjectV2IterationField { name }
          ... on ProjectV2SingleSelectField {
            name
            options { name }
          }
        }
      }
    }
  }
}
"""


def _path(value: Optional[os.PathLike], default: pathlib.Path) -> pathlib.Path:
    """Resolve an injectable path while keeping the production default local."""
    return pathlib.Path(default if value is None else value)


def _volume_for(path: pathlib.Path) -> Optional[str]:
    """Return the mounted-volume path for a checkout under /Volumes, if any."""
    try:
        relative = path.absolute().relative_to(pathlib.Path("/Volumes"))
    except ValueError:
        return None
    if not relative.parts:
        return "/Volumes"
    return "/Volumes/{}".format(relative.parts[0])


def _checkout_readable(root: pathlib.Path) -> bool:
    """Check the root itself, not just a child that may happen to exist."""
    try:
        if not root.is_dir():
            return False
        # Opening the directory catches an unmounted or otherwise inaccessible
        # volume where a metadata-only exists() check can be misleading.
        os.listdir(root)
    except OSError:
        return False
    return True


def _inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _expected_links(claude_dir: pathlib.Path,
                    checkout_root: pathlib.Path) -> List[tuple]:
    """Return every link created by scripts/install.sh, discovering skills."""
    links = [
        (claude_dir / "command-center", checkout_root),
        (claude_dir / "statusline.sh", checkout_root / "statusline.sh"),
    ]

    skills = checkout_root / "skills"
    try:
        skill_dirs = sorted(
            (entry for entry in skills.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError:
        skill_dirs = []
    links.extend(
        (claude_dir / "skills" / skill.name, skill)
        for skill in skill_dirs
    )
    return links


def check_symlinks(claude_dir: Optional[os.PathLike] = None,
                   checkout_root: Optional[os.PathLike] = None) -> Check:
    """Check the install links and that each resolves into this checkout."""
    claude = _path(claude_dir, CLAUDE_DIR)
    root = _path(checkout_root, CHECKOUT_ROOT)

    if not _checkout_readable(root):
        volume = _volume_for(root)
        if volume:
            found = (
                "checkout volume {} is unmounted; {} is not readable"
                .format(volume, root)
            )
            fix = "mount {}, then run {}".format(volume, INSTALL_FIX)
        else:
            found = "checkout root {} is not readable".format(root)
            fix = "restore access to the checkout, then run {}".format(INSTALL_FIX)
        return Check("install symlinks", False, found, fix)

    root_resolved = root.resolve(strict=False)
    errors: List[str] = []
    links = _expected_links(claude, root)

    for link, expected in links:
        try:
            exists = os.path.lexists(link)
        except OSError:
            exists = False
        shown = str(link)
        if not exists:
            errors.append("{} is missing".format(shown))
            continue
        if not link.is_symlink():
            errors.append("{} is not a symlink".format(shown))
            continue

        try:
            resolved = link.resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append("{} does not resolve".format(shown))
            continue

        if not _inside(resolved, root_resolved):
            errors.append("{} points outside the checkout ({})".format(
                shown, resolved))
            continue

        # The install script has one intended target for each link. Checking
        # it catches a link that points at a different, but still internal,
        # file and would otherwise look healthy.
        try:
            expected_resolved = expected.resolve(strict=True)
        except (OSError, RuntimeError):
            expected_resolved = None
        if expected_resolved is None or resolved != expected_resolved:
            wanted = expected_resolved or expected
            errors.append("{} points to {} (expected {})".format(
                shown, resolved, wanted))

    if errors:
        return Check("install symlinks", False, "; ".join(errors), INSTALL_FIX)
    return Check(
        "install symlinks", True,
        "{} links resolve inside {}".format(len(links), root_resolved),
        "",
    )


def check_settings(claude_dir: Optional[os.PathLike] = None) -> Check:
    """Check the JSON settings entry used to activate the status line."""
    claude = _path(claude_dir, CLAUDE_DIR)
    settings = claude / "settings.json"

    if not settings.exists():
        return Check(
            "settings.json", False, "{} is missing".format(settings), INSTALL_FIX)

    try:
        with settings.open() as stream:
            data = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        return Check(
            "settings.json", False,
            "{} is not valid JSON ({})".format(settings, detail),
            "repair the JSON syntax in {}".format(settings),
        )
    except OSError as exc:
        return Check(
            "settings.json", False,
            "{} cannot be read ({})".format(settings, exc),
            "restore read access to {}".format(settings),
        )

    status_line = data.get("statusLine") if isinstance(data, dict) else None
    command = status_line.get("command") if isinstance(status_line, dict) else None
    expected = {STATUSLINE_COMMAND, str(claude / "statusline.sh")}
    if command not in expected:
        if not isinstance(data, dict) or "statusLine" not in data:
            found = "statusLine key is missing from {}".format(settings)
        else:
            found = "statusLine points to {!r}, not {}".format(
                command, STATUSLINE_COMMAND)
        return Check("settings.json", False, found, INSTALL_FIX)

    if status_line.get("type") != "command":
        return Check(
            "settings.json", False,
            "statusLine has type {!r}, not command".format(status_line.get("type")),
            INSTALL_FIX,
        )

    return Check(
        "settings.json", True,
        "valid JSON with statusLine pointing to {}".format(STATUSLINE_COMMAND),
        "",
    )


def _timestamp(value: object) -> Optional[float]:
    """Parse the cache timestamp without consulting usage.py."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        return stamp if stamp == stamp and abs(stamp) != float("inf") else None
    if not isinstance(value, str):
        return None

    try:
        return float(value)
    except ValueError:
        pass
    try:
        iso = value.strip()
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        parsed = datetime.fromisoformat(iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _age_text(seconds: Optional[float]) -> str:
    """Make an age readable without hiding a future or unavailable timestamp."""
    if seconds is None:
        return "age unavailable"
    if seconds < 0:
        return "age is in the future"
    if seconds < 60:
        return "age less than 1 minute"
    if seconds < 3600:
        return "age {} minutes".format(int(seconds // 60))
    if seconds < 86400:
        return "age {} hours".format(int(seconds // 3600))
    return "age {} days".format(int(seconds // 86400))


def _mtime_age(path: pathlib.Path, now: float) -> Optional[float]:
    try:
        return now - path.stat().st_mtime
    except OSError:
        return None


def check_usage_cache(cache_path: Optional[os.PathLike] = None,
                      now: Optional[float] = None) -> Check:
    """Check the Claude usage cache directly, including its writing age.

    This intentionally does not import usage.py. Its tolerant readers are right
    for a budget gate but would erase the distinction between a missing file,
    malformed JSON, and a cache that simply stopped being refreshed.
    """
    cache = _path(cache_path, USAGE_CACHE)
    now = time.time() if now is None else now

    try:
        exists = cache.exists()
    except OSError:
        exists = False
    if not exists:
        return Check(
            "usage cache", False,
            "{} is missing (age unavailable)".format(cache),
            USAGE_CACHE_FIX,
        )

    file_age = _mtime_age(cache, now)
    try:
        with cache.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return Check(
            "usage cache", False,
            "{} is missing (age unavailable)".format(cache),
            USAGE_CACHE_FIX,
        )
    except (OSError, UnicodeError) as exc:
        return Check(
            "usage cache", False,
            "{} is present but cannot be read ({}; {})".format(
                cache, exc, _age_text(file_age)),
            USAGE_CACHE_FIX,
        )
    except ValueError:
        return Check(
            "usage cache", False,
            "{} is present but unparseable ({})".format(
                cache, _age_text(file_age)),
            USAGE_CACHE_FIX,
        )

    if not isinstance(data, dict):
        return Check(
            "usage cache", False,
            "{} is present but unparseable (top-level JSON is not an object; {})".format(
                cache, _age_text(file_age)),
            USAGE_CACHE_FIX,
        )

    captured_at = _timestamp(data.get("captured_at"))
    if captured_at is None:
        return Check(
            "usage cache", False,
            "{} is present but unparseable (captured_at is missing or invalid; {})".format(
                cache, _age_text(file_age)),
            USAGE_CACHE_FIX,
        )

    age = now - captured_at
    if age < 0:
        return Check(
            "usage cache", False,
            "{} is present but its timestamp is from the future ({})".format(
                cache, _age_text(age)),
            USAGE_CACHE_FIX,
        )
    if age >= USAGE_CACHE_BROKEN_AFTER.total_seconds():
        return Check(
            "usage cache", False,
            "{} is present but stale ({}; health threshold is 1 hour)".format(
                cache, _age_text(age)),
            USAGE_CACHE_FIX,
        )
    return Check(
        "usage cache", True,
        "{} is present and fresh ({})".format(cache, _age_text(age)),
        "",
    )


def gh_auth_status() -> dict:
    """Read the active GitHub account without ever requesting its token."""
    proc = subprocess.run(
        ["gh", "auth", "status", "--active", "--hostname", "github.com",
         "--json", "hosts"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitHubError(detail or "gh auth status exited {}".format(proc.returncode))
    try:
        return json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise GitHubError("gh auth status returned invalid JSON: {}".format(exc))


def _auth_scopes(entry: dict) -> List[str]:
    """Normalise gh's current comma-separated output and test fixtures' lists."""
    raw = entry.get("scopes") or []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(scope).strip() for scope in raw if str(scope).strip()]


def _auth_entry(payload: object) -> Optional[dict]:
    """Select the active github.com account from `gh auth status --json hosts`."""
    if not isinstance(payload, dict):
        return None
    hosts = payload.get("hosts")
    if not isinstance(hosts, dict):
        return None
    entries = hosts.get("github.com")
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        return None
    accounts = [entry for entry in entries if isinstance(entry, dict)]
    if not accounts:
        return None
    return next((entry for entry in accounts if entry.get("active")), accounts[0])


def _auth_failure_is_missing_login(error: BaseException) -> bool:
    text = str(error).lower()
    return any(
        phrase in text
        for phrase in ("not logged in", "not authenticated", "no accounts",
                       "authentication required", "login required")
    )


def check_auth_scope() -> Check:
    """Check that the active gh token is authenticated and can write Projects."""
    try:
        entry = _auth_entry(gh_auth_status())
    except Exception as exc:
        if _auth_failure_is_missing_login(exc):
            return Check(
                "gh auth", False, "not authenticated to github.com", AUTH_LOGIN_FIX)
        return Check(
            "gh auth", False,
            "gh auth status failed: {}".format(str(exc) or "unknown error"),
            "restore gh access, then run {}".format(AUTH_LOGIN_FIX),
        )

    if entry is None or entry.get("state") not in (None, "success"):
        return Check(
            "gh auth", False, "not authenticated to github.com", AUTH_LOGIN_FIX)

    scopes = _auth_scopes(entry)
    shown_scopes = ", ".join(scopes) if scopes else "none"
    account = entry.get("login") or "the active account"
    found = "authenticated as {}; token scopes: {}".format(account, shown_scopes)
    if "project" not in {scope.strip("'\"`").lower() for scope in scopes}:
        return Check(
            "gh auth", False, found + " (missing project scope)", AUTH_SCOPE_FIX)
    return Check("gh auth", True, found, "")


def _field_options(field: dict) -> Set[str]:
    raw = field.get("options") or []
    if isinstance(raw, dict):
        raw = raw.get("nodes") or []
    if not isinstance(raw, list):
        return set()
    return {
        str(option.get("name"))
        for option in raw
        if isinstance(option, dict) and option.get("name") is not None
    }


def _project_fields(data: object) -> Optional[List[dict]]:
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    if not isinstance(user, dict):
        return None
    project = user.get("projectV2")
    if not isinstance(project, dict):
        return None
    fields = project.get("fields")
    if not isinstance(fields, dict):
        return None
    nodes = fields.get("nodes")
    if not isinstance(nodes, list):
        return None
    return [node for node in nodes if isinstance(node, dict)]


def check_project_fields() -> Check:
    """Check only the Project fields and options funnel.py writes by name."""
    try:
        data = gh_graphql(
            PROJECT_FIELDS_QUERY, login=PROJECT_OWNER, number=PROJECT_NUMBER)
        fields = _project_fields(data)
        if fields is None:
            return Check(
                "Project fields", False,
                "Project {}/{} is missing or not visible".format(
                    PROJECT_OWNER, PROJECT_NUMBER),
                "create or restore Project {}/{} with its required fields".format(
                    PROJECT_OWNER, PROJECT_NUMBER),
            )
    except Exception as exc:
        return Check(
            "Project fields", False,
            "GitHub Project query failed: {}".format(str(exc) or "unknown error"),
            "restore GitHub access, then check Project {}/{}".format(
                PROJECT_OWNER, PROJECT_NUMBER),
        )

    by_name: Dict[str, List[dict]] = {}
    for field in fields:
        name = field.get("name")
        if name:
            by_name.setdefault(str(name), []).append(field)

    problems: List[str] = []
    for field_name, expected_options in (("Status", STAGES), ("Class", LADDER)):
        matching = by_name.get(field_name)
        if not matching:
            problems.append("missing field {}".format(field_name))
            continue
        actual_options = set().union(*(_field_options(field) for field in matching))
        missing = [option for option in expected_options if option not in actual_options]
        if missing:
            problems.append(
                "{} is missing option{} {}".format(
                    field_name,
                    "s" if len(missing) != 1 else "",
                    ", ".join(missing),
                )
            )

    if LOCK_FIELD not in by_name:
        problems.append("missing field {}".format(LOCK_FIELD))

    if problems:
        return Check(
            "Project fields", False,
            "Project {}/{}: {}".format(
                PROJECT_OWNER, PROJECT_NUMBER, "; ".join(problems)),
            "restore the missing Project field or option",
        )

    return Check(
        "Project fields", True,
        "Project {}/{} has Status, Class and {} with the required options".format(
            PROJECT_OWNER, PROJECT_NUMBER, LOCK_FIELD),
        "",
    )


def gh_branch_exists() -> bool:
    """Return whether the heartbeat branch exists, or raise on other failures."""
    try:
        proc = subprocess.run(
            ["gh", "api", "repos/{}/git/ref/heads/{}".format(
                REPO, HEARTBEAT_BRANCH)],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise GitHubError("gh could not query the heartbeat branch: {}".format(exc))

    if proc.returncode == 0:
        return True
    detail = (proc.stderr or proc.stdout or "").strip()
    # A missing ref is a healthy answer to the query, not a failed query. gh's
    # API output has used both `404` and `not found` wording across versions.
    lowered = detail.lower()
    if "404" in lowered or "not found" in lowered:
        return False
    raise GitHubError(
        "gh heartbeat branch query failed: {}".format(
            detail or "gh exited {}".format(proc.returncode))
    )


def _spool_files(spool_dir: pathlib.Path) -> List[pathlib.Path]:
    """List pending spool files without importing heartbeat.py."""
    try:
        if not spool_dir.exists():
            return []
        if not spool_dir.is_dir():
            raise OSError("{} is not a directory".format(spool_dir))
        return sorted(
            (entry for entry in spool_dir.iterdir() if entry.is_file()),
            key=lambda entry: entry.name,
        )
    except FileNotFoundError:
        return []


def check_heartbeat(spool_dir: Optional[os.PathLike] = None,
                    now: Optional[float] = None) -> Check:
    """Check the remote heartbeat branch and any undrained local spool files."""
    spool = _path(spool_dir, HEARTBEAT_SPOOL)
    now = time.time() if now is None else now
    findings: List[str] = []
    branch_ok: Optional[bool]

    try:
        branch_ok = gh_branch_exists()
    except Exception as exc:
        branch_ok = None
        findings.append(
            "could not check heartbeat branch `{}` ({})".format(
                HEARTBEAT_BRANCH, str(exc) or "unknown error")
        )
    else:
        if branch_ok:
            findings.append("heartbeat branch `{}` exists".format(HEARTBEAT_BRANCH))
        else:
            findings.append("heartbeat branch `{}` is absent".format(HEARTBEAT_BRANCH))

    try:
        files = _spool_files(spool)
    except OSError as exc:
        files = []
        findings.append("local heartbeat spool {} cannot be read ({})".format(
            spool, exc))
    else:
        if not files:
            findings.append("local heartbeat spool {} is empty".format(spool))
        else:
            try:
                oldest = min(path.stat().st_mtime for path in files)
            except OSError as exc:
                findings.append(
                    "local heartbeat spool {} has {} file(s), but its age cannot "
                    "be read ({})".format(spool, len(files), exc)
                )
            else:
                age = _age_text(now - oldest)
                findings.append(
                    "local heartbeat spool {} has {} file(s); oldest is {} old".format(
                        spool, len(files), age[4:] if age.startswith("age ") else age)
                )

    broken = (
        branch_ok is not True
        or bool(files)
        or any("cannot be read" in finding for finding in findings)
    )
    if broken:
        return Check("heartbeat branch", False, "; ".join(findings), HEARTBEAT_FIX)
    return Check("heartbeat branch", True, "; ".join(findings), "")


def check_topic() -> Check:
    """Check the same opt-in topic search that supplies load_items()."""
    try:
        count = len(member_repos())
    except Exception as exc:
        return Check(
            "command-center topic", False,
            "GitHub topic search failed: {}".format(str(exc) or "unknown error"),
            "restore GitHub access, then check the command-center topic",
        )

    found = "{} repos carry the `{}` topic".format(count, TOPIC)
    if count == 0:
        return Check("command-center topic", False, found, TOPIC_FIX)
    return Check("command-center topic", True, found, "")


def _status_for_consistency(item: Item) -> str:
    """Name an unset Project Status without inventing a value for it."""
    return item.status or "unset"


def item_consistency_findings(items: Iterable[Item]) -> List[str]:
    """Return one read-only finding for each item with contradictory facts.

    Issue state and Project Status are separate GitHub facts, as is the
    descriptive ``needs-shaping`` label. The funnel reports disagreements but
    never chooses which side to rewrite. Multiple disagreements on one item
    stay on one line so the output remains one line per item.
    """
    findings: List[str] = []
    for item in items:
        status = _status_for_consistency(item)
        reasons: List[str] = []

        if item.state == "CLOSED" and item.status not in ("Done", "Parked"):
            reasons.append("state is CLOSED but Status is {}".format(status))
        if item.state == "OPEN" and item.status == "Done":
            reasons.append("state is OPEN but Status is Done")
        if "needs-shaping" in item.labels and item.status != "Ideas":
            reasons.append(
                "label needs-shaping is present but Status is {}".format(status)
            )

        if reasons:
            findings.append("{}: {}".format(item.ref, "; ".join(reasons)))
    return findings


def check_item_consistency(items: Iterable[Item]) -> Check:
    """Build the doctor check for contradictions already present in ``items``."""
    findings = item_consistency_findings(items)
    return Check("item consistency", not findings, "\n".join(findings), "")


def class_assignment_lines(items: Iterable[Item]) -> List[str]:
    """Return direct Class assignments in a stable, hand-retypable format."""
    assigned = sorted(
        (item for item in items if item.klass),
        key=lambda item: (item.repo, item.number),
    )
    return [
        "{} | issue number {} | Class {}".format(
            item.ref, item.number, item.klass
        )
        for item in assigned
    ]


def check_class_assignments(items: Iterable[Item]) -> Check:
    """Build the read-only Class-assignment dump for ``funnel doctor``."""
    return Check("Class assignments", True, "\n".join(
        class_assignment_lines(items)
    ), "")


def doctor_checks(claude_dir: Optional[os.PathLike] = None,
                  checkout_root: Optional[os.PathLike] = None,
                  usage_cache: Optional[os.PathLike] = None,
                  heartbeat_spool: Optional[os.PathLike] = None,
                  items: Optional[Iterable[Item]] = None) -> List[Check]:
    """Run every fixed check, even when an earlier one is broken.

    ``items`` is supplied by ``cmd_doctor`` after the normal Project load. It
    keeps the consistency check pure and lets fixture callers avoid GitHub.
    """
    checks = [
        check_symlinks(claude_dir=claude_dir, checkout_root=checkout_root),
        check_settings(claude_dir=claude_dir),
        check_auth_scope(),
        check_project_fields(),
        check_topic(),
        check_usage_cache(cache_path=usage_cache),
        check_heartbeat(spool_dir=heartbeat_spool),
    ]
    if items is not None:
        items = list(items)
        checks.append(check_item_consistency(items))
        checks.append(check_class_assignments(items))
    return checks


def render_checks(checks: Iterable[Check]) -> None:
    """Render one stable, actionable line for each doctor check."""
    for check in checks:
        if not check.found:
            continue
        for finding in str(check.found).splitlines():
            line = "{}: {}".format(check.name, finding)
            if not check.ok and check.fix:
                line += " — fix: {}".format(check.fix)
            print(line)


def cmd_doctor() -> int:
    try:
        items = load_items()
    except Exception as exc:
        checks = doctor_checks()
        checks.append(Check(
            "item consistency", False,
            "Project items could not be loaded: {}".format(
                str(exc) or "unknown error"),
            "restore GitHub access, then rerun funnel doctor",
        ))
    else:
        checks = doctor_checks(items=items)
    render_checks(checks)
    return 0 if all(check.ok for check in checks) else 1


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
              number title url body state stateReason closedAt
              repository { nameWithOwner }
              labels(first: 25) { nodes { name } }
              assignees(first: 10) { nodes { login } }
              parent { number repository { nameWithOwner } }
              subIssuesSummary { total completed }
              subIssues(first: 50) {
                nodes { createdAt closedAt }
              }
              blockedBy(first: 50) {
                nodes { number state stateReason repository { nameWithOwner } }
              }
              timelineItems(last: 60, itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT, LABELED_EVENT]) {
                nodes {
                  __typename
                  ... on ProjectV2ItemStatusChangedEvent {
                    createdAt status project { number }
                  }
                  ... on LabeledEvent {
                    createdAt label { name }
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


def resolve_repo(repo: Optional[str]) -> str:
    """Choose a member repo only when that choice is unambiguous."""
    if repo:
        return repo

    members = sorted(set(member_repos()))
    if len(members) == 1:
        return members[0]
    if not members:
        raise GitHubError("no member repos found; --repo is required")
    raise GitHubError(
        "multiple member repos: {}; --repo is required".format(
            ", ".join(members)
        )
    )


def _from_node(node: dict) -> Optional[Item]:
    content = node.get("content") or {}
    if not content.get("number"):
        return None  # a draft issue, or a pull request

    status = (node.get("status") or {}).get("name")
    parent = content.get("parent")
    summary = content.get("subIssuesSummary") or {}
    child_times = []
    child_close_times = []
    for child in ((content.get("subIssues") or {}).get("nodes") or []):
        if not isinstance(child, dict):
            continue
        created_at = parse_time(child.get("createdAt"))
        if created_at is not None:
            child_times.append(created_at)
        closed_at = parse_time(child.get("closedAt"))
        if closed_at is not None:
            child_close_times.append(closed_at)

    item = Item(
        repo=content["repository"]["nameWithOwner"],
        number=content["number"],
        title=content["title"],
        url=content["url"],
        state=content["state"],
        body=content.get("body"),
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
        first_child_created_at=min(child_times) if child_times else None,
        last_child_closed_at=max(child_close_times) if child_close_times else None,
        closed_at=parse_time(content.get("closedAt")),
        item_id=node.get("id"),
        in_motion_since=parse_time((node.get("lock") or {}).get("text")),
    )

    # Native dependencies apply to tickets, not the parent project, and the
    # same open/dead split the REST helper produces. This comes out of the
    # Project query that already ran, so it costs no extra request.
    if item.state == "OPEN" and item.parent:
        blockers = (content.get("blockedBy") or {}).get("nodes") or []
        dependencies = classify_blockers(blockers, item.repo)
        item.open_blockers = dependencies["open"]
        item.dead_blockers = dependencies["dead"]

    # Time at the current gate: the last status change into the status the item
    # actually holds, in *this* project. Events arrive oldest-first, and an
    # issue may sit in several projects — filtering on the project is what stops
    # time-at-gate being silently wrong.
    for event in content["timelineItems"]["nodes"]:
        if not event:
            continue
        label = (event.get("label") or {}).get("name")
        if label == "blocked":
            item.blocked_since = parse_time(event.get("createdAt"))
            continue
        if (event.get("project") or {}).get("number") != PROJECT_NUMBER:
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
                # Dependencies are already on the item: `_from_node` reads them
                # from `blockedBy` in the Project query. They used to be fetched
                # here instead, one REST call per open ticket per command — ~78
                # calls a run, ~1,800 an hour across the scheduled agents, which
                # exhausted the API budget on 2026-09-08 and took `brief` down
                # entirely. Do not restore a per-item read in this loop.
                if item.state == "OPEN" and item.is_blocked:
                    _load_block_comment(item)
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


def breakdown_latency(item: Item) -> Optional[timedelta]:
    """How long a Ready project waited for its first ticket.

    This is a useful funnel-side measurement, but it is not time Nate spent
    answering the current gate. Keep it separate from ``Item.waited`` and only
    report a positive gap while the item's current status is still Ready; the
    current Project timeline does not retain the earlier Ready timestamp after
    a project advances.
    """
    if item.status != "Ready":
        return None
    if item.status_since is None or item.first_child_created_at is None:
        return None
    gap = item.first_child_created_at - item.status_since
    return gap if gap > timedelta(0) else None


def launch_command(item: Item) -> str:
    return 'claude "Work {} — {}"'.format(item.url, item.title)


def class_display(item: Item, by_ref: Dict[str, Item]) -> str:
    """Show the effective Class without assigning one to an unclassed item."""
    klass = effective_class(item, by_ref)
    if not klass:
        return "no class"
    return klass if item.klass else "{} (inherited)".format(klass)


def item_json(item: Item, now: datetime, by_ref: Optional[Dict[str, Item]] = None) -> dict:
    by_ref = by_ref if by_ref is not None else {}
    breakdown = breakdown_latency(item)
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
        "breakdown_latency": humanise(breakdown) if breakdown else None,
        "breakdown_latency_days": breakdown.days if breakdown else None,
        "blocked": item.is_blocked,
        "launch": launch_command(item),
    }


def parked_items(items: Iterable[Item]) -> List[Item]:
    """Parked projects, newest first.

    A parked item is closed and therefore absent from the gate queue, but its
    reason is still useful context in the brief. ``status_since`` is the time
    the Project item entered Parked; items with no matching timeline event are
    retained and sorted last rather than disappearing.
    """
    def key(item: Item):
        return (
            item.status_since is None,
            -(item.status_since.timestamp() if item.status_since else 0),
            item.repo,
            item.number,
        )

    return sorted((i for i in items if i.status == "Parked"), key=key)


def _parked_item_json(item: Item) -> Dict[str, object]:
    """Render one parked item and read its durable reason comment.

    Comments are deliberately fetched here, rather than in ``ITEM_QUERY``:
    parked items are uncommon and the normal Project load must not pay for a
    comment request for every issue.
    """
    comments = (_gh_json(
        "gh", "issue", "view", str(item.number), "--repo", item.repo,
        "--json", "comments",
    ) or {}).get("comments", [])
    reason = None
    for comment in reversed(comments):
        body = comment.get("body") or ""
        if body.startswith(PARK_COMMENT_PREFIX):
            reason = body[len(PARK_COMMENT_PREFIX):].strip()
            break

    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "parked_at": item.status_since.isoformat() if item.status_since else None,
        "reason": reason,
    }


def parked_json(items: Iterable[Item]) -> List[Dict[str, object]]:
    """The brief's parked section, with one comment lookup per parked item."""
    return [_parked_item_json(item) for item in parked_items(items)]


def blocked_items(items: Iterable[Item]) -> List[Item]:
    """Open, label-blocked items, oldest first.

    Block comments are loaded once while the Project items are read. This
    renderer deliberately reuses the parsed state on ``Item`` so the brief
    does not fetch each blocked issue a second time.
    """
    return sorted(
        (
            item for item in items
            if item.state == "OPEN" and item.is_blocked
        ),
        key=lambda item: (
            item.status_since is None,
            item.status_since or datetime.max.replace(tzinfo=timezone.utc),
            item.repo,
            item.number,
        ),
    )


def _blocked_item_json(item: Item) -> Dict[str, object]:
    """Render one blocked item from the parsed block-comment state."""
    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "reason": item.block_reason,
        "conditions": item.block_references,
        "blocked_at": item.status_since.isoformat() if item.status_since else None,
    }


def blocked_json(items: Iterable[Item]) -> List[Dict[str, object]]:
    """The brief's blocked section, reusing one load-time comment fetch."""
    return [_blocked_item_json(item) for item in blocked_items(items)]


def _item_human_step_reason(item: Item) -> Optional[str]:
    """Return the parsed marker, tolerating fixture Items without a body."""
    return parse_human_step(item.body or "")


def human_step_items(items: Iterable[Item]) -> List[Item]:
    """Open child issues that Nate must complete himself.

    Human-step tickets are work, not decisions. They are therefore rendered in
    their own brief section instead of being added to the decision queue.
    Closed tickets remain in ``items`` so the completed-project backstop can
    tell a project that carried a human step from one that never had one.
    """
    return sorted(
        (
            item for item in items
            if item.state == "OPEN"
            and item.parent is not None
            and _item_human_step_reason(item) is not None
        ),
        key=lambda item: (item.repo, item.number),
    )


def _human_step_item_json(item: Item) -> Dict[str, object]:
    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "reason": _item_human_step_reason(item),
    }


def human_step_json(items: Iterable[Item]) -> List[Dict[str, object]]:
    """Render the open human-step work owed by Nate."""
    return [_human_step_item_json(item) for item in human_step_items(items)]


def completed_projects_missing_human_steps(items: Iterable[Item]) -> List[Item]:
    """Completed projects whose plans mention access but have no marker.

    This is a detective signal, not proof that a human step was required. A
    parked project is deliberately excluded: parking is not a claim that its
    plan shipped. Any human-step ticket, including one already closed, clears
    the flag because the backstop asks whether the project ever carried one.
    """
    rows = list(items)
    human_step_parents = {
        item.parent
        for item in rows
        if item.parent is not None and _item_human_step_reason(item) is not None
    }
    return sorted(
        (
            item for item in rows
            if item.parent is None
            and item.state == "CLOSED"
            and item.state_reason != "NOT_PLANNED"
            and item.status != "Parked"
            and item.ref not in human_step_parents
            and access_signals(item.body or "")
        ),
        key=lambda item: (
            (item.closed_at or item.status_since) is None,
            -((item.closed_at or item.status_since).timestamp()
              if item.closed_at or item.status_since else 0),
            item.repo,
            item.number,
        ),
    )


def _completed_project_missing_human_steps_json(item: Item) -> Dict[str, object]:
    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "access_signals": access_signals(item.body or ""),
    }


def closed_with_access_vocabulary_json(
    items: Iterable[Item],
) -> List[Dict[str, object]]:
    """Render the completed-project detective flags for the brief."""
    return [
        _completed_project_missing_human_steps_json(item)
        for item in completed_projects_missing_human_steps(items)
    ]


def _never_closing(item: Item) -> bool:
    """Whether an issue cannot resolve the prerequisite it represents."""
    reason = str(item.state_reason or "").lower().replace("-", "_").replace(" ", "_")
    return item.status == "Parked" or reason == "not_planned"


def _dependency_ref(item: Item, value: str) -> Optional[str]:
    """Expand the block-comment shorthand without guessing cross-repo refs."""
    value = str(value).strip()
    if value.startswith("#"):
        return item.repo + value
    return value if "/" in value and "#" in value else None


def _dead_dependency_refs(item: Item, by_ref: Dict[str, Item]) -> List[str]:
    """Return blockers that are explicitly unable to close.

    ``dead_blockers`` comes from the native dependency response and covers
    blockers outside the Project too. The other two sources let fixture and
    already-loaded Project data prove the same condition for a local blocker.
    """
    refs = set(getattr(item, "dead_blockers", []))
    for value in list(item.open_blockers) + list(item.block_references):
        ref = _dependency_ref(item, value)
        blocker = by_ref.get(ref or "")
        if blocker is not None and _never_closing(blocker):
            refs.add(ref)
    return sorted(refs)


def _approved_current_head(pr: Optional[Dict[str, object]]) -> bool:
    """Whether a PR carries approval for the head currently being inspected."""
    if not isinstance(pr, dict):
        return False
    verdict = pr.get("verdict")
    if isinstance(verdict, dict):
        approved = verdict.get("verdict") == "approved"
        reviewed_head = verdict.get("head_sha")
    else:
        approved = verdict == "approved"
        reviewed_head = None
    if not approved:
        return False
    head = pr.get("headRefOid")
    # A fixture may omit the SHA when it is only testing the state pair. Live
    # facts include both values, and a moved head must not be called stranded:
    # it is waiting for a fresh review instead.
    return not head or not reviewed_head or head == reviewed_head


def stranded_items(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Render open items for which no current agent or gate can make progress.

    This is deliberately a diagnostic, not a queue. The first release only
    uses facts the funnel already knows how to read: an approved current-head
    verdict on a conflicting PR, a stale claim with no PR, a childless
    ``Building`` project, and a native or named dependency closed as
    ``not_planned``. Missing CI history is intentionally absent; no fetched
    fact distinguishes that from a PR whose first check is still pending.

    ``pr_facts`` is optional so the function remains fixture-pure. ``None``
    means the caller has not requested PR lookups and therefore treats a stale
    claim as having no PR; a supplied mapping distinguishes a known no-PR
    result from a fact that was not fetched.
    """
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    stale = {item.ref for item in stale_locks(rows, now)}
    found: List[Dict[str, object]] = []

    for item in rows:
        if item.state != "OPEN":
            continue

        reasons: List[str] = []
        pr_known = pr_facts is None or item.ref in pr_facts
        pr = None if pr_facts is None else pr_facts.get(item.ref)
        if (
            pr
            and str(pr.get("state") or "").upper() == "OPEN"
            and str(pr.get("mergeable") or "").upper() == "CONFLICTING"
            and _approved_current_head(pr)
        ):
            reasons.append("approved verdict against an unmergeable branch")

        if item.ref in stale and pr_known and pr is None:
            reasons.append("claim past its TTL with no PR")

        if item.parent is None and item.status == "Building" and not item.children_total:
            reasons.append("Building project has no tickets")

        dead = _dead_dependency_refs(item, by_ref)
        if dead:
            reasons.append(
                "blocked on blocker that will never close: {}".format(
                    ", ".join(dead))
            )

        if reasons:
            found.append({
                "ref": item.ref,
                "title": item.title,
                "url": item.url,
                "reason": "; ".join(reasons),
            })
    return found


def stranded_json(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """The brief's stranded-work diagnostic array."""
    return stranded_items(items, now, pr_facts=pr_facts)


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
    by_ref = {i.ref: i for i in items}
    for item in decisions:
        print(
            "  {:<10} {:<24} {:<34} {:<18} {}".format(
                item.status or "-",
                class_display(item, by_ref),
                item.ref,
                humanise(item.waited(now)),
                gate_question(item),
            )
        )

    print("\nStartable by Codex ({}), ladder order:".format(len(tickets)))
    if not tickets:
        print("  nothing")
    for item in tickets:
        print("  {:<24} {:<34} {}".format(
            class_display(item, by_ref), item.ref, item.title))

    pending = awaiting_breakdown(items)
    if pending:
        print("\nApproved, awaiting breakdown into tickets ({}):".format(len(pending)))
        for item in pending:
            print("  {:<24} {:<34} {:<18} {}".format(
                class_display(item, by_ref), item.ref,
                humanise(item.waited(now)), item.title))

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


def cmd_brief(
    items: List[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> int:
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
        "parked": parked_json(items),
        "blocked": blocked_json(items),
        "human_steps": human_step_json(items),
        "closed_with_access_vocabulary": closed_with_access_vocabulary_json(
            items
        ),
        "needs_class": [item_json(i, now, by_ref) for i in items if needs_class(i)],
        "awaiting_breakdown": [
            item_json(i, now, by_ref) for i in awaiting_breakdown(items)
        ],
        "stranded": stranded_json(items, now, pr_facts=pr_facts),
        "in_motion": [i.ref for i in running],
        "wip_limit": WIP_LIMIT,
        "stale_locks_taken_over": [i.ref for i in stale_locks(items, now)],
        "maintenance_load": maintenance_load(items, now),
        "unattended_merges": unattended_merges(now),
        "working_tree_touched": working_tree_touched(now),
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


def cmd_park(items: List[Item], now: datetime, ref: str, reason: str,
             run: Optional[str] = None, agent: Optional[str] = None) -> int:
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
         "--body", append_provenance(
             PARK_COMMENT_PREFIX + reason, "nate-relayed", at=now,
             run=run, agent=agent)],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(comment.stderr.strip())

    print("{} → Parked\n{}{}".format(item.ref, PARK_COMMENT_PREFIX, reason))
    return 0


def cmd_comment(items: List[Item], now: datetime, ref: str, body: str,
                voice: str, run: Optional[str] = None,
                agent: Optional[str] = None) -> int:
    """Post an issue comment with an explicit, stamped voice."""
    if not body.strip():
        raise GitHubError("a non-empty comment body is required")
    item = find(items, ref)
    comment = subprocess.run(
        ["gh", "issue", "comment", str(item.number), "--repo", item.repo,
         "--body", append_provenance(body, voice, at=now, run=run, agent=agent)],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(comment.stderr.strip())
    print("recorded {} comment on {}".format(voice, item.ref))
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
    ref = ticket_ref_from_branch(repo, branch)
    ticket = next((i for i in items if i.ref == ref), None) if ref else None

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
    by_ref = {i.ref: i for i in items}
    for item in rows:
        print("  {:<3} {:<24} {:<34} {:<14} {}".format(
            "*" if "needs-shaping" in item.labels else " ",
            class_display(item, by_ref), item.ref,
            humanise(item.waited(now)), item.title))
    if rows:
        print("\n  * = labelled needs-shaping. Grilling is interactive and is the"
              "\n      throttle on everything downstream — one at a time.")
    return 0


def cmd_capture(items: List[Item], now: datetime, title: str, note: Optional[str],
                repo: Optional[str], run: Optional[str] = None,
                agent: Optional[str] = None) -> int:
    """Capture an idea. Unbounded and guilt-free, by design."""
    repo = resolve_repo(repo)
    body = append_provenance(
        note or "Captured from chat. Not yet thought through.", "agent",
        at=now, run=run, agent=agent,
    )
    args = [
        "gh", "issue", "create", "--repo", repo, "--title", title,
        "--body", body, "--label", "needs-shaping",
    ]
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
        print("{}  → Ideas (needs-shaping) in {}".format(url, repo))
    else:
        print("{} in {}\nnote: created, but not added to the Project".format(
                  url, repo),
              file=sys.stderr)
    return 0


def cmd_shaped(items: List[Item], now: datetime, ref: str, plan_file: str,
               run: Optional[str] = None, agent: Optional[str] = None) -> int:
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
    body = append_provenance(plan, "agent", at=now, run=run, agent=agent)

    out = subprocess.run(
        ["gh", "issue", "edit", str(item.number), "--repo", item.repo,
         "--body", body],
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


def dependency_facts(repo: str, number: int) -> Dict[str, List[str]]:
    """Return the dependency states that affect whether a ticket can finish.

    GitHub's endpoint returns full issue objects for both open and closed
    blockers. Open blockers still belong in the queue's exclusion set. A
    blocker closed as ``not_planned`` is different: it will never close as the
    prerequisite the ticket names, so retain that ref for the stranded-work
    diagnostic without treating it as an ordinary open blocker.
    """
    endpoint = "repos/{}/issues/{}/dependencies/blocked_by".format(repo, number)
    blockers = _gh_json("gh", "api", endpoint)
    if blockers is None:
        raise GitHubError("could not read blockers for {}#{}".format(repo, number))
    if not isinstance(blockers, list):
        raise GitHubError("invalid blockers response for {}#{}".format(repo, number))
    return classify_blockers(blockers, repo)


def classify_blockers(blockers: Iterable[dict], repo: str) -> Dict[str, List[str]]:
    """Split blocker objects into the two states the funnel acts on.

    Deliberately tolerant of both wire shapes. REST spells the fields
    ``state_reason`` / ``full_name`` and lowercases the state; GraphQL spells
    them ``stateReason`` / ``nameWithOwner`` and uppercases it. One classifier
    for both is what keeps the batched Project read and the single-ticket REST
    helper from drifting into two different definitions of "blocked".
    """
    refs: Dict[str, List[str]] = {"open": [], "dead": []}
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        blocker_number = blocker.get("number")
        if not isinstance(blocker_number, int) or isinstance(blocker_number, bool):
            continue
        blocker_repo = (blocker.get("repository") or {}).get("full_name") \
            or (blocker.get("repository") or {}).get("nameWithOwner") or repo
        ref = "{}#{}".format(blocker_repo, blocker_number)
        state = str(blocker.get("state") or "").lower()
        state_reason = str(
            blocker.get("state_reason") or blocker.get("stateReason") or ""
        ).lower().replace("-", "_").replace(" ", "_")
        if state == "open":
            refs["open"].append(ref)
        elif state_reason == "not_planned":
            refs["dead"].append(ref)
    return refs


def open_blockers(repo: str, number: int) -> List[str]:
    """Return open native dependency blockers for one ticket.

    The public helper keeps its original narrow contract. ``load_items`` uses
    ``dependency_facts`` directly so the same endpoint read can also preserve
    blockers that were explicitly parked or marked not planned.
    """
    return dependency_facts(repo, number)["open"]


def _load_block_comment(item: Item) -> None:
    """Populate one open blocked item's parsed comment state.

    Block comments are intentionally loaded outside the Project query. The
    normal load pays for this only for open blocked items, and the parsed state
    stays on ``Item`` for pure queue functions and the brief to reuse.
    """
    comments = (_gh_json(
        "gh", "issue", "view", str(item.number), "--repo", item.repo,
        "--json", "comments",
    ) or {}).get("comments", [])
    parsed = parse_block_comment([
        comment.get("body") or ""
        for comment in comments
        if isinstance(comment, dict)
    ])
    if parsed is not None:
        item.block_references, item.block_reason = parsed


def _ticket_body(repo: str, number: int) -> str:
    """One issue body, fetched only for a candidate about to be handed out."""
    row = _gh_json("gh", "issue", "view", str(number), "--repo", repo,
                   "--json", "body") or {}
    return row.get("body") or ""


def _ticket_pr(repo: str, number: int) -> Optional[Dict]:
    """The PR for a ticket, found by the branch name the routine guarantees."""
    rows = _gh_json("gh", "pr", "list", "--repo", repo, "--state", "all",
                    "--head", "ticket/{}".format(number), "--json",
                    "number,state,url,headRefOid,mergeable,mergedAt,reviews") or []
    return rows[0] if rows else None


def ticket_pr_facts(
    items: Sequence[Item],
) -> Dict[str, Optional[Dict[str, object]]]:
    """Read PR facts needed by the brief's stranded-work diagnostics.

    Only open child tickets and claimed items need a PR lookup. The result is a
    map with an explicit ``None`` for a known missing PR, so the pure detector
    can distinguish that from a ticket omitted by a caller that did not fetch
    PR facts at all.
    """
    facts: Dict[str, Optional[Dict[str, object]]] = {}
    for item in items:
        if item.state != "OPEN" or not (item.parent or item.in_motion_since is not None):
            continue
        pr = _ticket_pr(item.repo, item.number)
        if pr and str(pr.get("state") or "").upper() == "OPEN":
            pr = dict(pr)
            if str(pr.get("mergeable") or "").upper() == "CONFLICTING":
                pr["verdict"] = latest_verdict(item.repo, pr.get("number"))
        facts[item.ref] = pr
    return facts


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


def cmd_begin(items: List[Item], now: datetime, agent: str, tier: Optional[str],
              idle: bool, breakdown: bool = False,
              routine_sha_literal: Optional[str] = None) -> int:
    """Start a run and say what — if anything — there is to do. One call.

    A polling routine spends most of its runs discovering there is nothing to
    do, and on a credit-metered pool that discovery is not free: every separate
    tool call is another model turn carrying the whole context. Collapsing the
    opening — heartbeat, budget gate, work lookup — into one command makes an
    empty poll about as cheap as it can be, which is what lets the schedule run
    often without the polling itself becoming the cost.

    Always prints JSON, always starts the heartbeat first. A run that cannot be
    recorded is one the watchdog reads as never having happened, so the record
    comes before the decision, not after it.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import heartbeat
    import usage

    out: Dict[str, object] = {"agent": agent}
    run = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "heartbeat.py"), "start", "--agent", agent],
        capture_output=True, text=True)
    out["run"] = (run.stdout or "").strip().splitlines()[-1] if run.stdout else None

    if routine_sha_literal is not None:
        path = routine_path(agent)
        try:
            actual = routine_sha(path)
            status = "ok" if actual == routine_sha_literal else "drift"
            check = {
                "expected": routine_sha_literal,
                "actual": actual,
                "status": status,
            }
        except (OSError, UnicodeError) as exc:
            check = {
                "expected": routine_sha_literal,
                "actual": None,
                "status": "unreadable",
                "error": str(exc),
            }
        out["routine_sha"] = check
        if check["status"] != "ok":
            heartbeat.record_event(
                agent,
                out["run"],
                "prompt-drift",
                note="{} routine {} does not match the pasted literal".format(
                    agent, check["status"]),
                routine_sha=check,
            )

    reading = usage.read_agent(agent, now.timestamp())
    if reading is None:
        out.update(gate="unknown", do="stop",
                   why="usage could not be read; a run that cannot read its "
                       "budget does not work")
        print(json.dumps(out, indent=2))
        return 0

    verdict = usage.pace(reading, now.timestamp(),
                         provider=usage.provider_of(agent))
    idle_verdict = usage.idle_verdict(agent, reading) if idle else None
    if verdict.get("over_pace") or (idle_verdict or {}).get("over"):
        out.update(gate="over", do="stop",
                   why=(idle_verdict or {}).get("why") or "over pace")
        print(json.dumps(out, indent=2))
        return 0

    out["gate"] = "ok"
    if reading.get("unmetered"):
        # Say so rather than letting `gate: ok` imply a budget was checked. An
        # unmetered provider is a standing exception recorded in AGENTS.md, and
        # a run that never had a budget to check should not read like one that
        # passed a check.
        out["unmetered"] = True
    queue = review_queue(items, tier)
    if queue:
        out.update(do="review", work=queue[0])
    else:
        # Breakdown is opt-in per routine. Claude reviews only — its breakdown
        # job moved to the cheaper pool — so offering it one would send the
        # scarce reviewer off to do mechanical decomposition.
        if breakdown:
            pending = awaiting_breakdown(items)
            if pending:
                item = pending[0]
                work = {
                    "ref": item.ref,
                    "url": item.url,
                    "title": item.title,
                    "access_signals": access_signals(
                        _ticket_body(item.repo, item.number)
                    ),
                }
                out.update(do="breakdown", work=work)
            else:
                out.update(do="stop",
                           why="nothing to review and nothing to break down")
        else:
            out.update(do="stop", why="nothing to review")
    print(json.dumps(out, indent=2))
    return 0


def cmd_next_review(items: List[Item], tier: Optional[str]) -> int:
    """The single PR this reviewer should read, or nothing."""
    queue = review_queue(items, tier)
    if not queue:
        print("nothing — no {}review waiting".format(
            (tier + " ") if tier else ""), file=sys.stderr)
        return 1
    print(json.dumps(queue[0], indent=2))
    return 0


def cmd_review(repo: Optional[str], pr: int, verdict: str, ci: str,
               blocking: List[str], note: Optional[str],
               run: Optional[str] = None, agent: Optional[str] = None) -> int:
    """Record a structured review verdict on a PR.

    The reviewer's judgement is the part only a model can do. Writing it as
    prose and then acting on it is what leaves nothing checkable afterwards, so
    the verdict is written here, in one shape, stamped with the commit it
    actually reviewed.
    """
    repo = resolve_repo(repo)
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
    comment = append_provenance(comment, "agent", run=run, agent=agent)

    out = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", comment],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    print("recorded {} on PR #{} against {} in {}".format(
        verdict, pr, sha[:12], repo))
    return 0


def ticket_ref_from_branch(repo: str, branch: str) -> Optional[str]:
    """The ticket a `ticket/<n>` branch belongs to, or None.

    Pure, and shared, because "which issue does this PR finish" was being
    derived independently in the merge gate, in `cmd_reject`, and — once the
    post-merge close was added — in `cmd_merge`. Three parsers are three
    definitions, and the one that disagrees stops a ticket from closing.
    """
    if not branch.startswith("ticket/"):
        return None
    tail = branch.split("/", 1)[1]
    return "{}#{}".format(repo, tail) if tail.isdigit() else None


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

    mergeable = str(data.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        why.append(
            "branch {!r} is conflicting with the base — an engineer rebase is required"
            .format(data.get("headRefName") or "")
        )
    elif mergeable != "MERGEABLE":
        why.append(
            "mergeability for branch {!r} has not been computed yet — retry on the next run"
            .format(data.get("headRefName") or "")
        )

    branch = data.get("headRefName") or ""
    ref = ticket_ref_from_branch(repo, branch)
    if ref is None:
        why.append("branch {!r} is not a ticket/<n> branch".format(branch))
    else:
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


def cmd_merge(items: List[Item], now: datetime, repo: Optional[str], pr: int,
              confirmed: bool) -> int:
    """Merge a PR, but only when every condition holds.

    The model decides *approval*; this decides *merge*. A model adds value
    judging whether a diff matches the plan. It adds none by being the component
    that types `gh pr merge`, and being that component is what makes an
    unattended merge impossible to audit — which is why v0 is still unaccepted.
    """
    repo = resolve_repo(repo)
    why = merge_blockers(repo, pr, items, now)
    if why:
        print("refusing to merge PR #{}:".format(pr), file=sys.stderr)
        for reason in why:
            print("  - " + reason, file=sys.stderr)
        return 1

    if not confirmed:
        print("PR #{} in {} passes every merge condition.".format(pr, repo))
        print("Nothing was changed. Re-run with --yes to merge.")
        return 0

    # Read the branch before merging: `--delete-branch` removes it, and the
    # branch is how the ticket is identified.
    branch = (_gh_json("gh", "pr", "view", str(pr), "--repo", repo,
                       "--json", "headRefName") or {}).get("headRefName") or ""
    ref = ticket_ref_from_branch(repo, branch)

    out = subprocess.run(
        ["gh", "pr", "merge", str(pr), "--repo", repo, "--squash",
         "--delete-branch"], capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    print("merged PR #{} in {}".format(pr, repo))

    # Close the ticket this PR finished. GitHub closes it only when the PR body
    # carries a `Closes #N` link, which is written by hand and was missing on 13
    # of the merged ticket PRs by 2026-09-08. A ticket left open stays startable
    # and is re-served to the engineer every run: 38 of 176 Codex runs that day —
    # 22% — did nothing but re-verify a PR that had already merged. Closing here
    # makes the link a convenience rather than the mechanism.
    if ref is None:
        return 0
    number = ref.split("#", 1)[1]
    state = (_gh_json("gh", "issue", "view", number, "--repo", repo,
                      "--json", "state") or {}).get("state")
    if str(state).upper() == "CLOSED":
        return 0

    closed = subprocess.run(
        ["gh", "issue", "close", number, "--repo", repo, "--reason", "completed"],
        capture_output=True, text=True)
    if closed.returncode != 0:
        # Say plainly that the merge succeeded, then exit non-zero: a ticket
        # left open is the exact failure this close exists to prevent, and a
        # silent 0 hides it. Re-running is harmless — the gate refuses a PR
        # that is no longer open — so the risk is only a visible retry, which
        # is cheaper than an invisible stranded ticket. (#236's plan.)
        print("MERGED, but {} could not be closed: {}".format(
            ref, closed.stderr.strip()), file=sys.stderr)
        print("the merge succeeded; close {} by hand — it stays startable "
              "until you do.".format(ref), file=sys.stderr)
        return 1
    print("closed {}".format(ref))
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
            body = c.get("body") or ""
            text = " ".join(_visible_comment(body).split())
            print("  {}: {}".format(
                render_voice(body), text[:200]))
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
               confirmed: bool, no_tickets: bool = False) -> int:
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
        if item.children_total == 0 and no_tickets:
            # Deliberate escape hatch, and narrow. A project with *open* tickets
            # is never acceptable; a project with *no* tickets is a different
            # thing, and the guard cannot tell "not broken down yet" from "the
            # work shipped another way". #26 was the second: Nate had it built
            # outside the pipeline, so it sat at Ready invisible to his queue,
            # permanently in awaiting_breakdown burning a routine run every time,
            # and refused by this very check. Asking him to say so explicitly
            # keeps the guard honest for the normal case.
            pass
        elif item.children_total == 0:
            raise GitHubError(
                "{} has no tickets. If its work shipped outside the pipeline, say "
                "so with --no-tickets; otherwise it is waiting to be broken down, "
                "not waiting to be accepted.".format(item.ref)
            )
        else:
            raise GitHubError(
                "{} still has open tickets ({}/{} closed). Accepting a project "
                "whose work is unfinished is how a thing gets called shipped "
                "while a third of it is missing.".format(
                    item.ref, item.children_done, item.children_total)
            )

    if not confirmed:
        print("would move {} from {} to {} ({})".format(
            item.ref, expected, nxt, meaning))
        if no_tickets:
            print("accepting a project with no tickets — its work shipped "
                  "outside the pipeline")
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
    sub.add_parser(
        "doctor", help="check the local install and report actionable failures")
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
        if verb == "accept":
            answer.add_argument(
                "--no-tickets", action="store_true",
                help="accept a project that has no tickets, because its work "
                     "shipped outside the pipeline. Refused without this: a "
                     "project with no tickets is normally waiting to be broken "
                     "down, not waiting to be accepted.",
            )
    capture = sub.add_parser("capture", help="capture an idea into the funnel")
    capture.add_argument("title")
    capture.add_argument("--note", default=None, help="anything worth keeping now")
    capture.add_argument("--repo", default=None)
    capture.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    capture.add_argument(
        "--agent", default=None,
        help="agent that wrote the body; otherwise read the heartbeat spool",
    )
    shaped = sub.add_parser("shaped", help="record a grilled plan and move to Shaped")
    shaped.add_argument("ref", help="issue number, owner/repo#number, or URL")
    shaped.add_argument("--plan", required=True, help="file holding the plan")
    shaped.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    shaped.add_argument(
        "--agent", default=None,
        help="agent that wrote the body; otherwise read the heartbeat spool",
    )
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
    park.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    park.add_argument(
        "--agent", default=None,
        help="agent that wrote the comment; otherwise read the heartbeat spool",
    )
    comment = sub.add_parser(
        "comment", help="post an issue comment with an explicit voice"
    )
    comment.add_argument("ref", help="issue number, owner/repo#number, or URL")
    comment.add_argument(
        "--voice", required=True, choices=PROVENANCE_VOICES,
        help="who the comment speaks for",
    )
    body = comment.add_mutually_exclusive_group(required=True)
    body.add_argument("--body", help="comment text")
    body.add_argument("--body-file", help="file containing the comment text")
    comment.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    comment.add_argument(
        "--agent", default=None,
        help="agent that wrote the comment; otherwise read the heartbeat spool",
    )
    reject = sub.add_parser(
        "reject", help="a merged PR turned out to be broken: undo and record it")
    reject.add_argument("pr", help="PR number or URL")

    review = sub.add_parser(
        "review", help="record a structured review verdict on a PR")
    review.add_argument("pr", type=int)
    review.add_argument("--repo", default=None)
    review.add_argument("--verdict", required=True, choices=VERDICTS)
    review.add_argument("--ci", required=True, choices=CI_STATES)
    review.add_argument("--blocking", action="append", default=[],
                        help="one blocking finding; repeat for more")
    review.add_argument("--note", default=None)
    review.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    review.add_argument(
        "--agent", default=None,
        help="agent that wrote the comment; otherwise read the heartbeat spool",
    )

    merge = sub.add_parser(
        "merge", help="merge a PR if every condition holds — dry run without --yes")
    merge.add_argument("pr", type=int)
    merge.add_argument("--repo", default=None)
    merge.add_argument("--yes", action="store_true", dest="confirmed")

    begin = sub.add_parser(
        "begin", help="start a run and say what there is to do, in one call")
    begin.add_argument("--agent", required=True)
    begin.add_argument("--tier", choices=TIERS, default=None)
    begin.add_argument("--idle", action="store_true")
    begin.add_argument("--breakdown", action="store_true",
                       help="also offer an approved plan to break down when "
                            "there is nothing to review")
    begin.add_argument(
        "--routine-sha", dest="routine_sha", default=None,
        help="compare the checked-in routine's normalized sha256 to this literal",
    )

    nxr = sub.add_parser(
        "next-review", help="the single PR this reviewer should read, or nothing")
    nxr.add_argument("--tier", choices=TIERS, default=None,
                     help="review only work of this risk tier. Declared by the "
                          "routine, the same way an engine declares its own.")
    reject.add_argument("--note", default=None, help="what is broken")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    # Doctor keeps its fixed checks runnable when the Project cannot be loaded;
    # the data-dependent consistency check is added when that read succeeds.
    if args.command == "doctor":
        return cmd_doctor()

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
            return cmd_park(items, now, args.ref, args.reason,
                            args.run, args.agent)
        if args.command == "comment":
            body = args.body
            if args.body_file:
                try:
                    body = pathlib.Path(args.body_file).read_text()
                except OSError as exc:
                    raise GitHubError(
                        "cannot read {}: {}".format(args.body_file, exc)
                    )
            return cmd_comment(items, now, args.ref, body or "", args.voice,
                               args.run, args.agent)
        if args.command == "reject":
            return cmd_reject(items, now, args.pr, args.note)
        if args.command in ANSWERS:
            return cmd_answer(items, now, args.command, args.ref, args.confirmed,
                              getattr(args, "no_tickets", False))
        if args.command == "show":
            return cmd_show(items, now, args.ref)
        if args.command == "ideas":
            return cmd_ideas(items, now)
        if args.command == "capture":
            return cmd_capture(items, now, args.title, args.note, args.repo,
                               args.run, args.agent)
        if args.command == "shaped":
            return cmd_shaped(items, now, args.ref, args.plan,
                              args.run, args.agent)
        if args.command == "begin":
            return cmd_begin(items, now, args.agent, args.tier, args.idle,
                             args.breakdown, args.routine_sha)
        if args.command == "next-review":
            return cmd_next_review(items, args.tier)
        if args.command == "review":
            return cmd_review(args.repo, args.pr, args.verdict, args.ci,
                              args.blocking, args.note, args.run, args.agent)
        if args.command == "merge":
            return cmd_merge(items, now, args.repo, args.pr, args.confirmed)
        if args.command == "next":
            return cmd_next(items, now, tier=getattr(args, "tier", None))
        if args.command == "brief":
            return cmd_brief(items, now, pr_facts=ticket_pr_facts(items))
        return {"queue": cmd_queue, "brief": cmd_brief}[args.command](
            items, now
        )
    except GitHubError as exc:
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
