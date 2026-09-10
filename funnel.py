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
import contextlib
import contextvars
from collections import namedtuple
import glob
import hashlib
import hmac
import io
import json
import os
import pathlib
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Set, Tuple)

from agent_health import assess as assess_agent_health

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
    filename = "codex-work.md" if agent == "codex" else agent + ".md"
    return CHECKOUT_ROOT / "routines" / filename

# One small, shared shape for every doctor check. Later doctor tickets add
# checks to the fixed list without changing the report contract.
Check = namedtuple("Check", "name ok found fix")

# GitHub's default issue labels are deliberately not part of the funnel's
# vocabulary.  Keep the names here rather than treating every label other than
# `blocked` and `needs-shaping` as a problem: Dependabot and a repository's own
# labels are outside this onboarding check.
STOCK_GITHUB_LABELS = frozenset({
    "bug",
    "documentation",
    "duplicate",
    "enhancement",
    "good first issue",
    "help wanted",
    "invalid",
    "question",
    "wontfix",
})


@dataclass(frozen=True)
class MemberRepoReadiness:
    """The checkable onboarding facts for one topic-bearing repository.

    ``topic`` is retained in the record even though ``member_repos()`` filters
    on it.  That makes the blocking predicate explicit for the queue ticket
    that consumes this result, while keeping a non-member out of the doctor
    report entirely.
    """

    repo: str
    topic: bool
    ci_workflow: bool
    stock_labels: Tuple[str, ...]
    dependabot: bool

    @property
    def blocking_reasons(self) -> Tuple[str, ...]:
        """Return only requirements whose absence prevents a merge."""
        reasons: List[str] = []
        if not self.topic:
            reasons.append("missing command-center topic")
        if not self.ci_workflow:
            reasons.append("no CI workflow")
        return tuple(reasons)

# `funnel doctor` only needs to know which ticket branches have merged. Keep
# the scan result separate from the pure contradiction detector, and carry its
# bounded-scan warning along with the refs that were found.
MergedPRFacts = namedtuple("MergedPRFacts", "ticket_refs truncated")

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

#: A run that has not pushed its deterministic ticket branch within this
#: window has left no durable work to protect. Branch absence is established
#: from one bounded repository scan, never from a per-ticket lookup.
CLAIM_BRANCH_GRACE = timedelta(minutes=30)

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
#: **What this does not fix, and what to watch.** Parallelism still multiplies
#: whatever the queue hands out, but the old risks are resolved: #129 and #161,
#: together with #175 and #177, shipped on 2026-09-08. The live risk from the
#: #178 investigation is stale Codex prompts, not a stale queue: all five Codex
#: automations have drifted from `routines/codex-work.md`; four escalated-lane
#: copies predate #268's decline handling, and the standard lane calls the
#: nonexistent `funnel next --not` flag. At four concurrent sessions that is
#: four runs executing an unparseable command instead of one. #178 is the fix,
#: not a lower number. Whether `WIP_LIMIT = 4` is still right is open and
#: unanswered; that is Nate's decision, not this comment's.
WIP_LIMIT = 4

#: Funnel order. Index is the stage's depth; later means further along.
STAGES = ["Ideas", "Shaped", "Ready", "Building", "Done", "Parked"]

#: The ladder, best-first. Only finite classes may preempt in-flight work.
LADDER = ["Investigate", "Broken", "Maintenance", "Improve", "New", "Replace"]

#: The finite classes. `plan.md`: "Broken and Maintenance preempt in-flight
#: work — and this is only safe because both are finite. The governing rule:
#: only classes that are finite may preempt." `startable()` ranks these ahead
#: of in-flight work of the unbounded classes (#435). The WIP-cap preemption in
#: `claim_ticket()` stays Broken-only: Maintenance may preempt ranking, not the
#: cap (`test_maintenance_does_not_preempt_the_limit`).
PREEMPTING_CLASSES = frozenset({"Broken", "Maintenance"})
PREEMPTING = {"Broken", "Maintenance"}

#: Existing-work classes and finite investigations may take the unattended
#: shaping path. Origin remains an independent condition: class describes the
#: work, not who raised it.
SELF_APPROVABLE_CLASSES = frozenset(
    {"Investigate", "Broken", "Maintenance", "Improve"}
)

#: Which stages can wait on a human, and the question each one asks.
GATES = {
    "Shaped": "Is the plan good?",
    "Building": "Accept it?",  # only once every child has closed
}

#: Bottom-up: clear the decision closest to shipping first. Parking counts as
#: clearing, which is what stops a stalled item permanently plugging the queue.
DECISION_ORDER = ["Building", "Ready", "Shaped"]

MAINTENANCE_WINDOW = timedelta(days=30)

# Only these harnesses expose the complete pair of input-token counts used by
# the re-send metric. Claude is unscheduled and Muse is unmetered, so their
# absence from the brief is intentional rather than missing data.
METERED_AGENTS = ("codex", "zcode")

# Sections that depend on the bounded PR/branch snapshot collected by the
# command-line entry point. A failed shared read must not let them infer a
# clean result from an incomplete fact set.
BRIEF_PR_FACT_SECTIONS = (
    "stranded", "in_motion", "stale_locks_taken_over",
)

# A doctor run asks for one row beyond the bound so it can distinguish a full
# result from a truncated one without an unbounded history scan. The bound is
# per member repository; a hand merge older than the newest 100 PRs is outside
# this diagnostic's deliberately finite window.
MERGED_PR_SCAN_LIMIT = 100

#: GraphQL reserve floors, expressed in **loads remaining** rather than raw
#: points so they stay correct as the per-load cost changes — #272 is about to
#: change it by roughly a factor of six.
#:
#: Two tiers, on Nate's call 2026-09-08: **reviewers keep going.** New work
#: consumes budget and adds in-flight PRs; review drains the lane and is cheap.
#: One floor for everything risks a stall where approved PRs sit unmerged until
#: the hour resets, and the undrained backlog makes the next hour worse than the
#: one that triggered it — the same instinct as `plan.md`'s "once a project is
#: Building, its remaining tickets finish first".
#:
#: Accepted knowingly: reviewers can still drive the budget to actual zero, so
#: the last review runs of a bad hour may crash rather than decline cleanly.
#: Two tiers make that rarer; they do not abolish it. Do not "fix" it by
#: collapsing the tiers.
ENGINEERING_RESERVE_LOADS = 20
REVIEW_RESERVE_LOADS = 5

#: Regression issues opened by `funnel reject`. The issues themselves are the
#: counter — GitHub is the state, so there is nothing else to keep in step.
REGRESSION_PREFIX = "Regression from PR #"

#: Reasons are durable parking artifacts. The sibling brief command reads this
#: fixed marker back from issue comments, so it is a shared contract.
PARK_COMMENT_PREFIX = "**Parked:** "

#: A project the funnel closes after its last upkeep ticket lands carries this
#: fixed prefix. The brief uses it to distinguish funnel-closed work from a
#: project Nate accepted at the gate; the JSON block after it carries the
#: ticket names and drift list for that later reader.
CLOSED_ITSELF_PREFIX = "**Closed itself:** "

#: The shaping path records its own Ready transition because the Project event
#: cannot say whether Nate or an agent wrote the field. The brief uses this
#: stable line only after finding a recent Shaped-to-Ready candidate.
SELF_APPROVED_PREFIX = "Self-approved: "
SELF_APPROVED_LINE = re.compile(
    r"^\s*(?:\*\*)?Self-approved:(?:\*\*)?\s*"
    r"(?P<basis>\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

#: A blocked comment names an optional, knowable condition after this marker.
#: The parser below owns the rest of the fixed header shape.
BLOCK_COMMENT_PREFIX = "**Blocked"
BLOCK_COMMENT_RE = re.compile(
    r"\A" + re.escape(BLOCK_COMMENT_PREFIX)
    + r"(?: on (?P<references>#[0-9]+(?: and #[0-9]+)*))?:\*\*"
)

#: A breakdown can leave a project waiting on Nate's answer. The header is
#: deliberately strict and anchored just like the ordinary block header so a
#: quoted or embedded sentence cannot become a gate question by accident.
NEEDS_DECISION_PREFIX = "**Needs a decision:**"
NEEDS_DECISION_RE = re.compile(
    r"\A" + re.escape(NEEDS_DECISION_PREFIX)
    + r"[ \t]+(?P<question>.+)", flags=re.DOTALL
)

#: A satisfied block is recorded before its label is removed. The structured
#: payload makes a partial failure idempotent: the next run can retry the label
#: write without posting a second provenance comment for the same block.
SATISFIED_BLOCK_PREFIX = "**Satisfied block:** "

#: Existing comments have no machine-readable provenance. Until the provenance
#: marker lands, show must fail closed rather than treat the GitHub account as
#: authorship.
UNATTRIBUTED = "UNATTRIBUTED"

#: Issue comments carry the voice that GitHub's author field cannot establish.
#: Keep this in the same HTML-comment-plus-JSON shape as REVIEW_MARKER below.
PROVENANCE_MARKER = "<!-- command-center-provenance -->"
PROVENANCE_VOICES = ("nate-direct", "nate-relayed", "agent")

#: Captured ideas carry their origin separately from the voice that wrote the
#: issue body. A Nate-raised idea is relayed by the capturing agent; an
#: agent-raised idea was observed by it. Keep these two values aligned with
#: the provenance convention, but do not overload the body-author marker.
ORIGIN_MARKER = "<!-- command-center-origin -->"
ORIGIN_VOICES = ("nate-relayed", "agent")

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

#: Keep funnel-closed work visible across several unattended brief runs. A
#: brief is hourly, so a one-run window would make the record disappear before
#: Nate could reasonably see it.
CLOSED_ITSELF_WINDOW = timedelta(days=7)

#: Keep mechanical block clears visible across several unattended brief runs,
#: for the same reason funnel-closed projects remain visible for a week.
CLEARED_BLOCK_WINDOW = timedelta(days=7)

# A brief has to answer before the 30-second session reply timeout leaves the
# caller unable to tell whether the session is alive. Keep a little room for
# JSON encoding and the session wrapper's bookkeeping; the section allocations
# below are deliberately explicit so the slowest reads stay visible and
# reviewable instead of turning into one arbitrary global timeout.
BRIEF_TOTAL_BUDGET_SECONDS = 29.0
BRIEF_SECTION_BUDGETS = {
    "ticket_pr_facts": 8.0,
    "items": 0.25,
    "counts_by_gate": 0.25,
    "in_motion": 0.25,
    "parked": 2.0,
    "closed_itself": 2.0,
    "cleared_blocks": 2.0,
    "blocked": 0.25,
    "prose_dependencies": 0.25,
    "suspected_human_steps": 0.25,
    "human_steps": 0.25,
    "closed_with_access_vocabulary": 0.25,
    "unclassed_captures": 0.25,
    "needs_class": 0.25,
    "awaiting_breakdown": 0.25,
    "stranded": 0.25,
    "stale_locks_taken_over": 0.25,
    "maintenance_load": 0.25,
    "resend_ratio": 3.0,
    "unattended_merges": 3.0,
    "unattended_approvals": 2.0,
    "agent_health": 1.0,
    "working_tree_touched": 1.0,
    "rejected_merges": 0.25,
}

# This is the brief's current gate-feeding section. Keep the set explicit so
# adding another value consumed by a merge routine cannot accidentally make a
# partial brief look safe.
BRIEF_GATE_SECTIONS = frozenset({"rejected_merges"})

#: Drift is reported, never used as a gate. Keep these names short and stable:
#: callers put them verbatim into comments and the brief.
DRIFT_PLAN_EDIT = "plan edited after Ready"
DRIFT_REJECTED_REVIEW = "review verdict rejected"
DRIFT_REGRESSION = "merge later rejected"
DRIFT_LATE_TICKET = "ticket added after Building"
DRIFT_SIGNAL_NAMES = (
    DRIFT_PLAN_EDIT,
    DRIFT_REJECTED_REVIEW,
    DRIFT_REGRESSION,
    DRIFT_LATE_TICKET,
)


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
    pinned: bool = False
    status_since: Optional[datetime] = None
    # ProjectV2 status history retained from the load query. The brief uses it
    # to find likely unattended shaping transitions before reading comments.
    status_events: List[Dict[str, object]] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    block_references: List[str] = field(default_factory=list)
    block_reason: Optional[str] = None
    needs_decision: Optional[str] = None
    unparseable_block_comments: List[str] = field(default_factory=list)
    block_comments_error: Optional[str] = None
    satisfied_block_record: Optional[Dict[str, object]] = None
    open_blockers: List[str] = field(default_factory=list)
    dead_blockers: List[str] = field(default_factory=list)
    assignees: List[str] = field(default_factory=list)
    in_motion_since: Optional[datetime] = None
    item_id: Optional[str] = None  # the ProjectV2Item, needed to write the lock
    parent: Optional[str] = None  # "owner/repo#123"
    children_total: int = 0
    children_done: int = 0
    # Derived at load time from child ticket bodies. This is deliberately not
    # a second GitHub record: the Human step marker remains the only source of
    # truth, including after its ticket closes.
    carried_human_step: bool = False
    first_child_created_at: Optional[datetime] = None
    last_child_closed_at: Optional[datetime] = None
    blocked_since: Optional[datetime] = None
    blocked_cleared_at: Optional[datetime] = None
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


@dataclass(frozen=True)
class DriftFacts:
    """History fetched for one project before applying the drift rules.

    The fields deliberately contain observations rather than pre-computed
    booleans. ``drift_since_approval`` stays a pure decision function, and a
    caller can fetch the same GitHub facts once for several consumers.
    """

    ready_at: Optional[datetime] = None
    plan_edit_times: Tuple[datetime, ...] = ()
    review_verdicts: Tuple[Dict[str, object], ...] = ()
    regression_pr_numbers: Tuple[int, ...] = ()
    building_at: Optional[datetime] = None
    ticket_created_at: Tuple[datetime, ...] = ()


def _after(value: object, boundary: Optional[datetime]) -> bool:
    """Whether a timestamp is strictly after a known approval boundary."""
    return isinstance(value, datetime) and boundary is not None and value > boundary


def drift_since_approval(
    item: Item, facts: Optional[DriftFacts] = None
) -> List[str]:
    """Return the stable drift signals that fired for ``item``.

    ``facts`` is optional for consumers such as ``funnel answer`` and
    ``funnel merge``; omitting it fetches the histories from GitHub. Tests and
    callers that already have those histories should pass ``DriftFacts`` and
    get a pure function over the supplied observations.

    Missing approval timestamps do not count as drift. Guessing from the
    current status would make a later state look like an earlier event and
    would turn an incomplete history read into a false report.
    """
    facts = facts if facts is not None else fetch_drift_facts(item)
    ready_at = facts.ready_at
    building_at = facts.building_at

    if ready_at is None and item.status == "Ready":
        ready_at = item.status_since
    if building_at is None and item.status == "Building":
        building_at = item.status_since

    signals: List[str] = []
    if any(_after(at, ready_at) for at in facts.plan_edit_times):
        signals.append(DRIFT_PLAN_EDIT)
    if any(
        isinstance(verdict, dict) and verdict.get("verdict") == "rejected"
        for verdict in facts.review_verdicts
    ):
        signals.append(DRIFT_REJECTED_REVIEW)
    if facts.regression_pr_numbers:
        signals.append(DRIFT_REGRESSION)
    if any(_after(at, building_at) for at in facts.ticket_created_at):
        signals.append(DRIFT_LATE_TICKET)
    return signals


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
        if item.parent is None and item.needs_decision:
            return "Answer the breakdown's question?"
        return "Unblock?" if item.parent else "Unblock or park?"
    if item.status == "Building":
        # New work and replacements always stop for acceptance. Upkeep closes
        # itself once #55 lands, except where Nate performed part of the work:
        # a project that ever carried a human-step ticket must still reach him.
        if not item.children_all_closed:
            return None
        # Keep this tied to the same existing-work class set used by the
        # unattended shaping rule. An unset or unknown Class fails closed into
        # the accept queue; it must never inherit the permissive path.
        if item.klass in SELF_APPROVABLE_CLASSES and not item.carried_human_step:
            return None
        return GATES["Building"]
    if item.status == "Shaped":
        # A plan with no Needs section has not earned an all-clear. The shared
        # parser fails closed so a missing section still reaches Nate rather
        # than silently bypassing the plan gate.
        return GATES["Shaped"] if plan_needs_nate(item.body or "") else None
    return None


def question_since(item: Item) -> Optional[datetime]:
    """When the item's current question became live.

    ``gate_question`` is the authority for which question is live. A Shaped
    project asks "Is the plan good?" only when its plan carries an open
    question. A Building project starts asking "Accept it?" when its last
    sub-issue closes. A blocked item starts asking its unblock question when
    the ``blocked`` label is applied. The status timestamp remains the
    fallback for older or partial fixture data without the event details.
    """
    question = gate_question(item)
    if item.is_blocked and question is not None:
        return item.blocked_since or item.status_since
    if question == GATES["Building"]:
        return item.last_child_closed_at or item.status_since
    return item.status_since


def awaiting_decision(items: Iterable[Item]) -> List[Item]:
    """Nate's queue: everything waiting on him, bottom-up, pinned then oldest.

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
            ladder_index(effective_class(item, by_ref)),
            0 if item.pinned else 1,
            since,
            item.repo,
            item.number,
        )

    return sorted((i for i in rows if gate_question(i)), key=key)


def parking_candidates(items: Iterable[Item]) -> List[Item]:
    """Open projects that have not reached ``Building``, oldest first.

    Parking is a project decision, not a way to close an individual ticket.
    The candidate age is the time at the item's current pre-Building stage;
    this keeps the prompt aligned with the funnel's oldest-at-gate ordering.
    """
    rows = list(items)
    pre_building = set(STAGES[:STAGES.index("Building")])

    def key(item: Item):
        since = question_since(item)
        return (
            since is None,
            since or datetime.max.replace(tzinfo=timezone.utc),
            item.repo,
            item.number,
        )

    return sorted(
        (
            item for item in rows
            if item.state == "OPEN"
            and item.parent is None
            and item.status in pre_building
        ),
        key=key,
    )


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

#: A ticket declares a step that is not workable in every agent environment in
#: its body, written at breakdown. An unmarked ticket is workable by any agent;
#: this reason is the middle outcome, workable only where Claude Code's local
#: environment is present. It is deliberately an allowlist: a lack of access
#: to the Claude Code environment is not the same as an engineer finding work
#: difficult.
MACHINE_LOCAL_REASON = "a Claude Code environment"
MACHINE_LOCAL_REASONS = (MACHINE_LOCAL_REASON,)

#: These reasons still mean that no agent can perform the step. Keep them
#: separate from MACHINE_LOCAL_REASONS so the next capability-aware consumer
#: can distinguish Claude-Code-only work from work Nate must perform.
HUMAN_STEP_PREFIX = "Human step: "
HUMAN_STEP_REASONS = (
    "an app UI with no API",
    "entering a credential",
    "an account or billing setting",
    "physical access to a machine",
)
HUMAN_STEP_MARKER_REASONS = MACHINE_LOCAL_REASONS + HUMAN_STEP_REASONS
HUMAN_STEP_LINE = re.compile(
    r"^\s*" + re.escape(HUMAN_STEP_PREFIX)
    + r"(?P<reason>"
    + "|".join(re.escape(reason) for reason in HUMAN_STEP_MARKER_REASONS)
    + r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_human_step(body: str) -> Optional[str]:
    """Return an allowlisted capability reason from a ticket body.

    Like ``RISK_LINE``, the marker must begin a body line. Matching only the
    stated access reasons keeps a ticket from becoming restricted merely
    because an agent found it difficult. ``None`` means any agent may work the
    ticket; ``MACHINE_LOCAL_REASON`` means only Claude Code may work it; and a
    reason in ``HUMAN_STEP_REASONS`` means no agent may work it.
    """
    if not isinstance(body, str):
        return None
    match = HUMAN_STEP_LINE.search(body)
    return match.group("reason") if match else None


def matching_human_step_reason(text: object) -> Optional[str]:
    """Return an allowlisted human-step reason found in block-comment text.

    A malformed block header may put the reason on the same line as the
    header, so ``parse_human_step`` cannot read it directly. This narrower
    scanner is used only on a parsed block reason or on the first line already
    recorded for a malformed block comment. It returns the canonical spelling
    from ``HUMAN_STEP_REASONS`` rather than trusting the comment's casing.
    """
    if not isinstance(text, str):
        return None
    for reason in HUMAN_STEP_REASONS:
        pattern = r"(?<!\w){}(?!\w)".format(re.escape(reason))
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    return None


def mark_projects_that_carried_human_steps(items: Sequence[Item]) -> None:
    """Derive project acceptance history from child ticket markers.

    Closed child tickets remain in the Project item feed, so deriving this
    after all pages load preserves "ever carried" without persisting a second
    field that could drift from the marker.
    """
    parent_refs = {
        item.parent
        for item in items
        if item.parent is not None
        and parse_human_step(item.body or "") is not None
    }
    for item in items:
        item.carried_human_step = item.ref in parent_refs


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

NEEDS_NATE_SIGNAL_REASONS = {
    "policy authority": (
        "cites plan.md or AGENTS.md on a gate, membership, or who may write"
    ),
    "unattended authority": "changes what an agent may do unattended",
    "gate authority": "changes a gate's question, answer, or owner",
    "field authority": "changes who may set a field that other rules act on",
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
NEEDS_NATE_CATEGORIES = (
    "Exposure",
    "Gates",
    "Scope and priority",
    "Preference",
)
NEEDS_NATE_CLEAR_ANSWERS = {
    "nothing",
    "nothing outstanding",
    "none",
}
NEEDS_NATE_CLAUSE_END = re.compile(r"[.;\u2013\u2014]| - ")


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


def _needs_nate_category_line(line: str) -> Optional[Tuple[str, str]]:
    """Return a category and answer from one supported Needs line."""
    text = re.sub(r"^(?:[-+*]|\d+[.)])\s+", "", line.strip())
    for category in NEEDS_NATE_CATEGORIES:
        escaped = re.escape(category)
        patterns = (
            rf"\*\*{escaped}\s*[:.]\s*\*\*\s*(?P<answer>.*)",
            rf"\*\*{escaped}\*\*\s*[:.]\s*(?P<answer>.*)",
            rf"{escaped}\s*[:.]\s*(?P<answer>.*)",
        )
        for pattern in patterns:
            match = re.fullmatch(pattern, text, re.IGNORECASE)
            if match:
                return category, match.group("answer").strip()
    return None


def _needs_nate_answer_is_clear(answer: str) -> bool:
    """Whether an answer's first clause is an explicit all-clear token."""
    clause = NEEDS_NATE_CLAUSE_END.split(answer, maxsplit=1)[0].strip().lower()
    return clause in NEEDS_NATE_CLEAR_ANSWERS


def _needs_nate_section_reason(section: str) -> Optional[str]:
    """Return a hold reason, or ``None`` when one Needs section is clear."""
    if section.strip().lower() in EMPTY_NEEDS_NATE:
        return None

    # A wrapped elaboration continues the category line above it: Muse writes
    # Markdown at eighty columns, and #514's clear section read as an open
    # question because its indented second lines counted as extra lines
    # (#524). A blank line, a new list item, or an unindented line still ends
    # the answer and is judged on its own.
    lines: List[str] = []
    for line in section.splitlines():
        if not line.strip():
            continue
        continues = (
            line[:1].isspace()
            and not re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line)
            and bool(lines)
        )
        if continues:
            lines[-1] = lines[-1].rstrip() + " " + line.strip()
        else:
            lines.append(line)
    found = set()
    for line in lines:
        parsed = _needs_nate_category_line(line)
        if parsed is None:
            return "plan has an open question"
        category, answer = parsed
        if category in found or not _needs_nate_answer_is_clear(answer):
            return "open question under {}".format(category)
        found.add(category)

    for category in NEEDS_NATE_CATEGORIES:
        if category not in found:
            return "open question under {}".format(category)
    return None


def shaped_plan_status(plan_body: str) -> Tuple[str, str]:
    """Return the status and reason earned by a newly recorded plan.

    The all-clear is deliberately narrow: a recognised Needs section must be
    present, either explicitly empty or made up of the four category lines with
    an explicit all-clear as each answer's first clause. Authority-shaped
    prose is reported separately by ``needs_nate_signals`` and does not change
    this section result. Everything else stays at Shaped with a reason the
    caller can print.
    """
    sections = _needs_nate_sections(plan_body)
    if not sections:
        return "Shaped", "plan has no ## Needs you section"
    for section in sections:
        reason = _needs_nate_section_reason(section)
        if reason:
            return "Shaped", reason
    return "Ready", "plan declares nothing open"


def plan_needs_nate(plan_body: str) -> bool:
    """Whether a plan's Needs Nate/Needs you section asks for Nate.

    A missing section is not an all-clear: the section itself is the plan's
    explicit evidence that Nate's questions were considered. Only a section
    containing exactly ``Nothing`` (with optional punctuation and whitespace)
    is empty. Multiple recognised sections fail closed if any one contains
    content. Authority-shaped prose is an advisory signal for the unattended
    approval record, not an open question for this parser.
    """
    return shaped_plan_status(plan_body)[0] == "Shaped"


def plan_is_escalated(plan_body: str) -> List[str]:
    """Return escalation reasons found across the whole plan body.

    Plans have no separate ticket title, so the plan is passed as the body to
    the shared escalation machinery. The empty-list result is the all-clear
    used by the self-approval condition.
    """
    return escalation_reasons("", plan_body)


# Plans already cite implementation details in ordinary Markdown. Keep this
# detector deliberately mechanical: it reports shared tokens for a reader to
# judge, rather than trying to decide whether two plans really collide.
PLAN_FUNCTION_RE = re.compile(
    r"(?<![\w.])(?P<name>(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*)"
    r"\s*\([^()\n]*\)"
)
PLAN_PATH_RE = re.compile(
    r"(?<![\w/.-])(?P<path>"
    r"(?:[A-Za-z0-9_.~-]+/)*[A-Za-z0-9_.~-]+\."
    r"(?:cpp|tsx|jsx|yaml|json|html|toml|xml|css|sql|ini|txt|js|md|py|sh|go|cc|ts|yml|h|c)"
    r"(?![A-Za-z0-9])"
    r")(?:\:\d+(?:-\d+)?)?(?:#L\d+(?:-L\d+)?)?"
)
PLAN_ISSUE_RE = re.compile(
    r"(?<![\w./-])(?P<ref>"
    r"(?:(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+)?#\d+\b)"
)


def _plan_overlap_signals(plan_body: object) -> Tuple[Set[str], Set[str], Set[str]]:
    """Extract the three checkable overlap signals from one plan body."""
    if not isinstance(plan_body, str):
        return set(), set(), set()

    functions = {
        match.group("name")
        for match in PLAN_FUNCTION_RE.finditer(plan_body)
    }
    paths = set()
    for match in PLAN_PATH_RE.finditer(plan_body):
        path = match.group("path")
        if path.startswith("./"):
            path = path[2:]
        paths.add(path)
    issues = {
        match.group("ref")
        for match in PLAN_ISSUE_RE.finditer(plan_body)
    }
    return functions, paths, issues


def plan_overlap_candidates(
    plan_ref: str,
    plan_body: str,
    other_plans: Iterable[Tuple[str, str]],
) -> List[str]:
    """Return advisory lines for mechanical overlap with other plans.

    ``other_plans`` supplies ``(ref, body)`` pairs for the open plans the
    caller wants to compare. Shared function calls, file paths and issue
    references are intentionally the whole signal: a human decides whether a
    candidate is a real collision. The output is deterministic so the shaping
    surface can print it without adding state or doing its own ranking.
    """
    current = _plan_overlap_signals(plan_body)
    candidates: List[str] = []
    seen: Set[str] = set()
    pairs = sorted(other_plans, key=lambda pair: pair[0])

    for other_ref, other_body in pairs:
        if other_ref == plan_ref:
            continue
        other = _plan_overlap_signals(other_body)

        for function in sorted(current[0] & other[0]):
            line = "{} and {} both name `{}()`".format(
                plan_ref, other_ref, function
            )
            if line not in seen:
                candidates.append(line)
                seen.add(line)
        for path in sorted(current[1] & other[1]):
            line = "{} and {} both touch `{}`".format(
                plan_ref, other_ref, path
            )
            if line not in seen:
                candidates.append(line)
                seen.add(line)
        for issue in sorted(current[2] & other[2]):
            line = "{} and {} both reference {}".format(
                plan_ref, other_ref, issue
            )
            if line not in seen:
                candidates.append(line)
                seen.add(line)

    return candidates


SHAPING_PLAN_STATUSES = frozenset(("Shaped", "Ready", "Building"))


def shaping_plan_overlap_candidates(
    items: Iterable[Item], item: Item, plan_body: str
) -> List[str]:
    """Find advisory overlaps with the other open project plans in flight.

    Status belongs to parent Project items, so child tickets are not plans even
    if a fixture or a future API response gives one a status. Closed projects
    are not in flight and must not keep influencing a newly shaped plan.
    """
    other_plans = (
        (other.ref, other.body or "")
        for other in items
        if other.ref != item.ref
        and other.parent is None
        and other.state == "OPEN"
        and other.status in SHAPING_PLAN_STATUSES
    )
    return plan_overlap_candidates(item.ref, plan_body, other_plans)


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
    """Return who should shape an item, or None for an invalid override.

    Capture uses the provenance voice vocabulary: ``agent`` means observed by
    an agent, while either Nate voice means he raised it. An authorised origin
    override is already reduced by its parser to ``nate`` or ``agents`` and
    supersedes that default. Missing or malformed origin resolves to Nate: the
    existing backlog predates the marker, and the safe direction is to keep it
    out of unattended shaping unless an authorised override says otherwise.
    """
    if override_target is not None:
        return override_target if override_target in ("nate", "agents") else None
    if origin_voice == "agent":
        return "agents"
    if origin_voice in ("nate-direct", "nate-relayed"):
        return "nate"
    return "nate"


def self_approval_eligible(klass: Optional[str], origin_voice: Optional[str],
                           override_target: Optional[str], *,
                           needs_nate: bool, escalated: bool) -> bool:
    """Whether all conditions permit one unattended shaping transition.

    #77 supplies the plan booleans and #80 owns the transition. Keeping class,
    origin, the Needs-section result, and escalation in this one predicate
    prevents origin from becoming a second gate that can drift from the
    existing self-approval rule. Authority signals remain advisory record
    data and are intentionally not a predicate term.
    """
    return (
        klass in SELF_APPROVABLE_CLASSES
        and effective_shape_owner(origin_voice, override_target) == "agents"
        and not needs_nate
        and not escalated
    )


def required_tier(title: str, body: str, failed_before: bool = False) -> str:
    return "escalated" if escalation_reasons(title, body, failed_before) else "standard"


def _startable_without_repo_readiness(
    item: Item,
    by_ref: Dict[str, Item],
    awaiting_review: Set[str],
    agent: str = "codex",
) -> bool:
    """Apply the queue exclusions that do not require a repository read."""
    capability_reason = parse_human_step(item.body or "")
    machine_local = (
        capability_reason is not None
        and capability_reason.casefold() == MACHINE_LOCAL_REASON.casefold()
    )
    if (
        item.state != "OPEN"
        or item.is_blocked
        or item.open_blockers
        or item.children_total
        # A machine-local marker is the middle capability outcome: Claude
        # Code may work it, while every other requester must leave it in
        # the queue. All other parsed markers remain human steps and are
        # excluded from every agent, as #141 established.
        or (capability_reason is not None
            and (not machine_local or agent != "claude"))
    ):
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
    # `Ready` or `Building`. `plan.md`: "Codex draws tickets from any
    # `Ready` or `Building` parent, so it stalls only if every parent lacks
    # tickets." `Building` is not a precondition for work but the record that
    # work began — `cmd_claim` writes it on the first claim.
    return parent.status in ("Ready", "Building") and not parent.is_blocked


def _repo_blocking_reasons(
    item: Item,
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]],
) -> Tuple[str, ...]:
    """Return blocking onboarding reasons for one loaded ticket.

    ``None`` means the caller did not request the optional repository snapshot;
    this keeps the ordering function usable with its existing fixture-only
    callers. Once a snapshot is supplied, a missing repo entry fails closed so
    a partial read cannot quietly offer work from an unchecked repository.
    """
    if repo_readiness is None:
        return ()
    readiness = repo_readiness.get(item.repo)
    if readiness is None:
        return ("repository readiness unavailable",)
    return readiness.blocking_reasons


def readiness_blockers(
    items: Sequence[Item],
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
    awaiting_review: Optional[Set[str]] = None,
    agent: str = "codex",
) -> List[Dict[str, object]]:
    """Return otherwise-eligible tickets withheld by repo readiness.

    The result is deliberately separate from ``startable()``'s list: callers
    such as ``begin`` need to say why a queue is empty without changing the
    shared ordering or turning diagnostics into another queue.
    """
    if repo_readiness is None:
        return []
    awaiting_review = awaiting_review or frozenset()
    rows = list(items)
    by_ref = {i.ref: i for i in rows}
    found: List[Dict[str, object]] = []
    for item in rows:
        if not _startable_without_repo_readiness(
            item, by_ref, awaiting_review, agent
        ):
            continue
        reasons = _repo_blocking_reasons(item, repo_readiness)
        if not reasons:
            continue
        found.append({
            "ref": item.ref,
            "repo": item.repo,
            "reasons": list(reasons),
        })
    return sorted(found, key=lambda row: (str(row["repo"]), str(row["ref"])))


def _readiness_blocker_summary(blockers: Sequence[Dict[str, object]]) -> str:
    """Render the stable queue-empty explanation for repository blockers."""
    details = []
    for blocker in blockers:
        reasons = ", ".join(str(reason) for reason in blocker["reasons"])
        details.append("{}: {}".format(blocker["ref"], reasons))
    return "tickets withheld by repository readiness — {}".format(
        "; ".join(details)
    )


def startable(
    items: Sequence[Item],
    awaiting_review: Optional[Set[str]] = None,
    agent: str = "codex",
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
) -> List[Item]:
    """Tickets the requesting agent may pick up, best-first.

    A ticket is an open issue with no children of its own, whose parent has
    passed the Ready gate. Tickets inherit their parent's Class — the ladder
    ranks projects, not individual tickets.

    `awaiting_review` holds refs whose work is already done and sitting in an
    open PR. A ticket's issue stays open until review merges it, so without this
    Codex re-picks finished work every run: on 2026-09-06 it built #19 at 08:00
    and then spent the 09:00 and 10:00 runs re-verifying the same branch, because
    Claude's routine was over pace and could not review it. Passing it in rather
    than querying here keeps this function pure and testable from fixtures.

    ``repo_readiness`` is an optional, caller-supplied snapshot from the member
    repository checks. Its blocking requirements are applied here; advisory
    facts remain available to ``doctor`` but never affect queue membership.
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
    # Membership, not a rank threshold: a class added above Broken in LADDER
    # (#130's Investigate) must not acquire preemption rights by position.
    # plan.md grants them only to the finite classes named in
    # PREEMPTING_CLASSES; a ticket that blocks one preempts with it.
    preempting = {
        ref: any(
            effective_class(by_ref[related], by_ref) in PREEMPTING_CLASSES
            for related in {ref} | descendants[ref]
        )
        for ref in by_ref
    }

    def eligible(item: Item) -> bool:
        if not _startable_without_repo_readiness(
            item, by_ref, awaiting_review, agent
        ):
            return False
        return not _repo_blocking_reasons(item, repo_readiness)

    def in_flight(item: Item) -> bool:
        """Once a project is Building, its remaining tickets finish first.

        Passing a gate is a commitment; nothing may silently un-commit it.
        """
        parent = by_ref.get(item.parent or "")
        return (parent.status if parent else item.status) == "Building"

    def key(item: Item):
        since = question_since(item) or datetime.max.replace(tzinfo=timezone.utc)
        return (
            # Finite classes preempt in-flight work of unbounded ones — the half
            # of plan.md's rule this key never implemented until #435. Measured
            # 2026-09-09: six Broken projects at Ready sat behind ten in-flight
            # Improve tickets all afternoon. Read through `effective_rank` so a
            # ticket that blocks a Broken one preempts with it.
            0 if preempting[item.ref] else 1,
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


def _ticket_branch_exists(
    item: Item,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]],
) -> Optional[bool]:
    """Return remote branch presence, or ``None`` when it was not established."""
    if pr_facts is None or item.ref not in pr_facts:
        return None
    fact = pr_facts[item.ref]
    if fact is None:
        return False
    value = fact.get("branch_exists")
    return value if isinstance(value, bool) else None


def in_motion(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Item]:
    """Every ticket currently claimed, oldest claim first.

    The claim covers the opening before a branch exists. After the grace
    period, only a confirmed remote branch keeps it live until the hard TTL.
    """
    rows = list(items)
    stale = {item.ref for item in stale_locks(rows, now, pr_facts=pr_facts)}
    return sorted(
        (
            i
            for i in rows
            if i.state == "OPEN"
            and i.in_motion_since is not None
            and i.ref not in stale
        ),
        key=lambda i: i.in_motion_since or now,
    )


def lock_holder(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> Optional[Item]:
    """The oldest live claim, or None. Retained for callers that want one item."""
    held = in_motion(items, now, pr_facts=pr_facts)
    return held[0] if held else None


def at_capacity(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> bool:
    return len(in_motion(items, now, pr_facts=pr_facts)) >= WIP_LIMIT


def stale_locks(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Item]:
    """Claims past the TTL or branchless after the opening grace period.

    Branch absence has to be explicit. Missing or truncated facts preserve the
    claim, so a run that pushed anything can never lose its lock because a scan
    could not see far enough.
    """
    return sorted(
        (
            i
            for i in items
            if i.state == "OPEN"
            and i.in_motion_since is not None
            and (
                now - i.in_motion_since >= LOCK_TTL
                or (
                    now - i.in_motion_since >= CLAIM_BRANCH_GRACE
                    and _ticket_branch_exists(i, pr_facts) is False
                )
            )
        ),
        key=lambda i: i.in_motion_since or now,
    )


#: A review verdict lives in a PR comment behind this marker, as JSON. GitHub is
#: the state, so it goes where the PR is rather than into a file only one machine
#: can read.
REVIEW_MARKER = "<!-- command-center-review -->"

#: The merge gate is software, not one of the model providers. Its verdicts
#: still use the agent voice because they are neither Nate's words nor his
#: relayed decision, but the agent field must say which component authored it.
MERGE_GATE_AGENT = "gate"

#: Shared wording for the blocker and for recognising the one refusal that
#: writes a gate-authored rejection. Keeping the suffix separate lets the
#: visible reason retain the branch name without making cmd_merge guess at it.
CONFLICTING_BRANCH_SUFFIX = (
    " is conflicting with the base — an engineer rebase is required"
)

#: A gate-authored verdict records only what the gate mechanically established.
#: It did not inspect the diff and must not imply that it did.
UNMERGEABLE_REJECTION_BLOCKING = "branch could not merge at this head"

#: A reviewer that writes prose and then acts leaves nothing a later step can
#: check. That is how a merge became something a model simply decided to do, and
#: how a PR with requested changes ended up owned by nobody (#39).
VERDICTS = ("approved", "rejected")
CI_STATES = ("green", "red", "unknown")


def _marked_json_blocks(body: str, marker: str) -> List[Tuple[Dict, str]]:
    """Return parseable JSON blocks owned by ``marker``, newest first.

    A comment may carry both a review verdict and provenance. Parsing from one
    marker to the last closing brace would join those two objects and make the
    review gate silently lose a valid verdict. Each marker occurrence therefore
    owns only the first fenced JSON block (or an immediately following bare
    object for compatibility with early markers) before another Command Center
    marker. A quoted marker can precede the real block, so occurrences are tried
    from last to first and malformed occurrences are skipped.
    """
    if not isinstance(body, str):
        return []

    marker_positions = [
        match.start() for match in re.finditer(re.escape(marker), body)
    ]
    blocks = []
    for marker_at in reversed(marker_positions):
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
                continue
            if isinstance(found, dict):
                end = marker_at + len(marker) + fenced.end()
                blocks.append((found, body[marker_at:end]))
            continue

        leading = len(rest) - len(rest.lstrip())
        raw = rest.lstrip()
        if not raw.startswith("{"):
            continue
        try:
            found, end = json.JSONDecoder().raw_decode(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(found, dict):
            block_end = marker_at + len(marker) + leading + end
            blocks.append((found, body[marker_at:block_end]))
    return blocks


def _marked_json(body: str, marker: str) -> Optional[Dict]:
    """Read the newest parseable JSON block owned by ``marker``."""
    blocks = _marked_json_blocks(body, marker)
    return blocks[0][0] if blocks else None


def _marked_json_block(body: str, marker: str) -> Optional[str]:
    """Return the newest parseable marker block, preserving its original text."""
    blocks = _marked_json_blocks(body, marker)
    return blocks[0][1] if blocks else None


def parse_self_approval(body: str) -> Optional[str]:
    """Return the stated basis from a self-approval marker line."""
    if not isinstance(body, str):
        return None
    match = SELF_APPROVED_LINE.search(body)
    if match is None:
        return None
    basis = match.group("basis").strip()
    return basis or None


def parse_verdict(body: str) -> Optional[Dict]:
    """The verdict carried by one comment, or None if it is not one."""
    return _marked_json(body, REVIEW_MARKER)


def parse_provenance(body: str) -> Optional[Dict]:
    """The provenance fields carried by one comment, or None if malformed."""
    found = _marked_json(body, PROVENANCE_MARKER)
    if found is None or found.get("voice") not in PROVENANCE_VOICES:
        return None
    return found


def parse_origin(body: str) -> Optional[Dict]:
    """The explicit capture origin, or None when it is absent or malformed."""
    found = _marked_json(body, ORIGIN_MARKER)
    if found is None or found.get("voice") not in ORIGIN_VOICES:
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


def parse_needs_decision_comment(bodies: Iterable[str]) -> Optional[str]:
    """Return the newest parseable breakdown question, if one exists."""
    for body in reversed(list(bodies)):
        if not isinstance(body, str):
            continue
        match = NEEDS_DECISION_RE.match(body)
        if not match:
            continue
        question = _visible_comment(match.group("question")).strip()
        if question:
            return question
    return None


def parse_satisfied_block_comment(body: str) -> Optional[Dict[str, object]]:
    """Return a valid agent-authored satisfied-block record, or ``None``.

    Both markers are load-bearing. The first carries the mechanical facts;
    the ordinary provenance marker says an agent, rather than Nate, recorded
    the unattended action.
    """
    found = _marked_json(body, SATISFIED_BLOCK_PREFIX)
    provenance = parse_provenance(body)
    if found is None or provenance is None or provenance.get("voice") != "agent":
        return None
    conditions = found.get("conditions")
    if (
        not isinstance(conditions, list)
        or not conditions
        or not all(isinstance(value, str) and value for value in conditions)
        or parse_time(found.get("found_closed_at")) is None
    ):
        return None
    return found


def unparseable_block_comment_lines(bodies: Iterable[str]) -> List[str]:
    """Return first lines that look like block comments but fail the parser."""
    findings: List[str] = []
    for body in bodies:
        if not isinstance(body, str):
            continue
        if not body.lstrip().startswith(BLOCK_COMMENT_PREFIX):
            continue
        if BLOCK_COMMENT_RE.match(body):
            continue
        lines = body.splitlines()
        findings.append(lines[0] if lines else body)
    return findings


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


def self_approval_comment(basis: str, at: Optional[datetime] = None,
                          run: Optional[str] = None,
                          agent: Optional[str] = None) -> str:
    """Build the durable marker for an unattended shaping decision."""
    if not isinstance(basis, str) or not basis.strip():
        raise ValueError("self-approval basis must not be empty")
    return append_provenance(
        SELF_APPROVED_PREFIX + basis.strip(), "agent",
        at=at, run=run, agent=agent,
    )


def origin_block(voice: str, at: Optional[datetime] = None,
                 run: Optional[str] = None,
                 agent: Optional[str] = None) -> str:
    """Build the explicit origin block attached to a captured idea."""
    if voice not in ORIGIN_VOICES:
        raise ValueError("unknown capture origin {!r}".format(voice))
    run, agent = _heartbeat_context(run, agent)
    fields = {
        "agent": agent,
        "at": (at or datetime.now(timezone.utc)).isoformat(),
        "run": run,
        "voice": voice,
    }
    return "{}\n\n```json\n{}\n```".format(
        ORIGIN_MARKER, json.dumps(fields, indent=2, sort_keys=True)
    )


def append_origin(body: str, voice: str, at: Optional[datetime] = None,
                  run: Optional[str] = None,
                  agent: Optional[str] = None) -> str:
    """Append an explicit capture origin without changing the supplied body."""
    return "{}\n\n{}".format(
        body, origin_block(voice, at=at, run=run, agent=agent)
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


#: A run that finished its ticket by comments -- an investigation or proposal
#: with no code change, so no `ticket/*` branch and no PR possible -- says so
#: with this prefix in its finish note. The queue withholds the bound ticket
#: until Nate closes it or a later run finishes it another way (#498).
COMMENTS_DELIVERABLE_PREFIX = "finished by comments:"


def finished_by_comments(items: Sequence[Item]) -> Set[str]:
    """Open tickets whose latest run finished them by comments, waiting on Nate.

    Read from the heartbeat records the way ``awaiting_review`` reads PRs: the
    run's ``bind`` record names the ticket, and its finish carries the
    ``skipped-human-step`` outcome with ``COMMENTS_DELIVERABLE_PREFIX`` in the
    note. The latest finish per ticket decides, so a later run that finishes
    the ticket another way returns it to the queue, and a closed ticket is
    never withheld. Observed 2026-09-09: eleven consecutive runs re-claimed
    #277 and re-verified the same nine comments in 85 minutes, because nothing
    recorded that the deliverable had already been delivered.
    """
    open_refs = {i.ref for i in items if i.state == "OPEN" and i.parent}
    if not open_refs:
        return set()
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat
    except Exception:
        return set()
    latest: Dict[str, Tuple[float, bool]] = {}
    for agent in sorted(heartbeat.PROVIDERS):
        if agent in heartbeat.RETIRED_AGENTS:
            continue
        try:
            records = heartbeat.read(agent)
        except Exception:
            continue
        bound = heartbeat.bindings(records)
        for row in records:
            if row.get("phase") != "finish" or not row.get("run"):
                continue
            binding = bound.get(row["run"])
            if not binding or binding.get("do") != "ticket":
                continue
            ref = str(binding.get("work"))
            if ref not in open_refs:
                continue
            ts = float(row.get("ts") or 0)
            note = str(row.get("note") or "").lower()
            marker = (row.get("outcome") == "skipped-human-step"
                      and COMMENTS_DELIVERABLE_PREFIX in note)
            if ref not in latest or ts >= latest[ref][0]:
                latest[ref] = (ts, marker)
    return {ref for ref, (_, marker) in latest.items() if marker}


def verdict_covers_head(verdict: Optional[Dict], head_oid: Optional[str]) -> bool:
    """Whether a verdict judged exactly the commit that is the branch head now."""
    return bool(verdict) and bool(head_oid) and verdict.get("head_sha") == head_oid


def rejected_at_current_head(verdict: Optional[Dict], head_oid: Optional[str]) -> bool:
    """A rejection hands the ticket back only while the head it judged is still the head.

    Both selectors read this one predicate (#487). A rejected verdict on the
    current head means the engineer owes a fix, so the ticket is engineering
    work. The moment a new head is pushed the rejection no longer covers the
    diff, and the ticket is awaiting review -- offered by `next-review` once
    and by `begin` not at all. Reading the verdict alone, as `awaiting_review`
    did until 2026-09-10, handed the fixed ticket to both at the same time.
    """
    return verdict_covers_head(verdict, head_oid) and verdict.get("verdict") == "rejected"


def approved_conflicting_current_head(
    pr: Optional[Dict[str, object]],
) -> bool:
    """Whether an open PR is an approved head that now cannot merge.

    ``UNKNOWN`` is deliberately not enough: GitHub has not established that
    state as a conflict, so handing it back would reintroduce the unconditional
    open-PR re-offer that this predicate is meant to avoid.
    """
    if not isinstance(pr, dict):
        return False
    if str(pr.get("state") or "").upper() != "OPEN":
        return False
    if str(pr.get("mergeable") or "").upper() != "CONFLICTING":
        return False
    verdict = pr.get("verdict")
    return (
        isinstance(verdict, dict)
        and verdict.get("verdict") == "approved"
        and verdict_covers_head(verdict, pr.get("headRefOid"))
    )


def approved_conflicting_refs(
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]],
) -> Set[str]:
    """Return refs whose approved current-head PR is definitively conflicting."""
    if pr_facts is None:
        return set()
    return {
        ref for ref, pr in pr_facts.items()
        if approved_conflicting_current_head(pr)
    }


def awaiting_review(items: Sequence[Item]) -> Set[str]:
    """Tickets whose work is already in an open PR, waiting to be reviewed.

    Found by the `ticket/<number>` branch name the routine guarantees, which is
    the same handle `ticket_pr_index` uses. One `gh pr list` per member repo, and
    for repos that actually have candidate tickets.
    """
    repos = {i.repo for i in items}
    blocked: Set[str] = set()
    for repo in sorted(repos):
        rows = _gh_json("gh", "pr", "list", "--repo", repo, "--state", "open",
                        "--json", "headRefName,headRefOid,number",
                        "--limit", "100") or []
        for row in rows:
            head = row.get("headRefName") or ""
            if not head.startswith("ticket/"):
                continue
            # A PR whose review asked for changes is *not* blocked: its ticket
            # goes back to the engineer to fix. Without this a rejected PR has no
            # owner — the reviewer will not revisit it and the engineer is never
            # offered it — so it waits for Nate. That is #39. The hand-back
            # lasts only while the rejected head is still the head: once the
            # engineer pushes, the ticket is review work again (#487).
            verdict = latest_verdict(repo, row.get("number"))
            if rejected_at_current_head(verdict, row.get("headRefOid")):
                continue
            blocked.add("{}#{}".format(repo, head.split("/", 1)[1]))
    return blocked


def next_ticket(items: Sequence[Item], now: datetime,
                blocked: Optional[Set[str]] = None,
                excluded: Optional[Set[str]] = None,
                agent: str = "codex",
                repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
                pr_facts: Optional[
                    Dict[str, Optional[Dict[str, object]]]
                ] = None,
                ) -> Optional[Item]:
    """The single ticket the requesting agent should work, or None.

    Returns None when the funnel is at its work-in-progress limit. A Broken
    ticket may start anyway; that is the one sanctioned preemption. `excluded`
    is a per-call filter for a caller that has already declined a candidate;
    it never changes the queue or persists any state.
    """
    excluded = excluded or frozenset()
    queue = [
        item for item in startable(
            items,
            awaiting_review=blocked,
            agent=agent,
            repo_readiness=repo_readiness,
        )
        if item.ref not in excluded
    ]
    if not queue:
        return None

    claimed = {i.ref for i in in_motion(items, now, pr_facts=pr_facts)}
    free = [i for i in queue if i.ref not in claimed]
    if not free:
        return None
    if not at_capacity(items, now, pr_facts=pr_facts):
        return free[0]

    # At capacity. Only a Broken ticket may exceed it, and only when nothing
    # already in motion is Broken — preemption is for getting a fix moving, not
    # for stacking fixes on top of each other.
    by_ref = {i.ref: i for i in items}
    running = in_motion(items, now, pr_facts=pr_facts)
    if any(effective_class(i, by_ref) == "Broken" for i in running):
        return None
    for candidate in free:
        if effective_class(candidate, by_ref) == "Broken":
            return candidate
    return None


def next_ticket_for_tier(items: Sequence[Item], now: datetime,
                         tier: Optional[str] = None,
                         blocked: Optional[Set[str]] = None,
                         excluded: Optional[Set[str]] = None,
                         agent: str = "codex",
                         repo_readiness: Optional[
                             Mapping[str, MemberRepoReadiness]
                         ] = None,
                         pr_facts: Optional[
                             Dict[str, Optional[Dict[str, object]]]
                         ] = None) -> Optional[Item]:
    """Return the first shared-order ticket belonging to ``tier``.

    Tier filtering has to happen by asking ``next_ticket`` repeatedly rather
    than by filtering its input first. The latter would hide other in-motion
    tickets from the WIP cap and could let a standard caller exceed it while
    walking past escalated work. Reusing ``next_ticket`` also keeps Broken
    preemption and all other ordering rules in one place.
    """
    excluded = set(excluded or ())
    while True:
        ticket = next_ticket(
            items, now, blocked=blocked, excluded=excluded,
            agent=agent,
            repo_readiness=repo_readiness,
            pr_facts=pr_facts,
        )
        if ticket is None or tier is None:
            return ticket

        reasons = escalation_reasons(
            ticket.title, _ticket_body(ticket.repo, ticket.number)
        )
        wanted = bool(reasons) if tier == "escalated" else not reasons
        if wanted:
            return ticket
        excluded.add(ticket.ref)


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

    HEAD changes are one checkout transition even when several runs were open
    across it. Changes where only the dirty count moved stay one row per run:
    the heartbeat has no identity for a dirty-only event to group on.
    """
    found: List[Dict[str, object]] = []
    transitions: Dict[Tuple[object, object], Dict[str, object]] = {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat

        for agent in sorted(heartbeat.PROVIDERS):
            rows = _brief_heartbeat_rows(agent)
            starts = {r.get("run"): r for r in rows if r.get("phase") == "start"}
            for row in rows:
                if row.get("phase") != "finish":
                    continue
                began = starts.get(row.get("run"))
                before = (began or {}).get("repo")
                after = row.get("repo")
                if not before or not after or before == after:
                    continue
                timestamp = row.get("ts") or 0
                if timestamp < (now - MAINTENANCE_WINDOW).timestamp():
                    continue
                at = datetime.fromtimestamp(
                    timestamp, timezone.utc
                ).isoformat()
                before_head = before.get("head") if isinstance(before, dict) else None
                after_head = after.get("head") if isinstance(after, dict) else None
                if (before_head is not None and after_head is not None
                        and before_head != after_head):
                    key = (before_head, after_head)
                    transition = transitions.get(key)
                    if transition is None:
                        transition = {
                            "at": at,
                            "before": before,
                            "after": after,
                            "observers": [],
                            "_at_ts": timestamp,
                            "_observers": [],
                        }
                        transitions[key] = transition
                    if timestamp < transition["_at_ts"]:
                        transition.update(
                            at=at, before=before, after=after, _at_ts=timestamp
                        )
                    transition["_observers"].append((
                        timestamp,
                        {"agent": agent, "run": row.get("run")},
                    ))
                    continue
                found.append({
                    "agent": agent,
                    "run": row.get("run"),
                    "at": at,
                    "before": before,
                    "after": after,
                })
    except Exception:
        return []

    for transition in transitions.values():
        observers = sorted(
            transition.pop("_observers"),
            key=lambda entry: (entry[0], entry[1]["agent"],
                               str(entry[1]["run"])),
        )
        transition["observers"] = [entry[1] for entry in observers]
        transition.pop("_at_ts")
    found.extend(transitions.values())
    return sorted(found, key=lambda r: str(r["at"]))


def unattended_merges(now: datetime) -> List[Dict[str, object]]:
    """Merges a reviewer made without Nate, read from every live agent's heartbeat.

    plan.md makes these a condition of unattended merging being allowed at all:
    they must appear in the brief as a record. Until 2026-09-10 this read only
    the retired Claude routine's spool, so every Muse merge since the review
    handover was missing from the brief (#489). The reader set is the live
    provider set -- ``heartbeat.PROVIDERS`` minus ``heartbeat.RETIRED_AGENTS`` --
    the same pattern ``agent_health`` and ``working_tree_touched`` use, so the
    next rotation cannot reintroduce the blind spot. Each record carries the
    ``agent`` that merged, oldest first.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat
    except Exception:
        return []

    cutoff = (now - MAINTENANCE_WINDOW).timestamp()
    found: List[Dict[str, object]] = []
    for agent in sorted(heartbeat.PROVIDERS):
        if agent in heartbeat.RETIRED_AGENTS:
            # A stopped schedule must not read as activity (#431).
            continue
        try:
            rows = _brief_heartbeat_rows(agent)
        except Exception:
            continue
        for row in rows:
            if not row.get("merged") or (row.get("ts") or 0) < cutoff:
                continue
            found.append({
                "pr": row.get("merged"),
                "at": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
                "note": row.get("note"),
                "agent": agent,
            })
    found.sort(key=lambda record: (record["at"], record["pr"] or 0))
    return found


def _self_approval_transition_times(item: Item, now: datetime) -> List[datetime]:
    """Find recent Ready transitions that could have been self-approval."""
    cutoff = now - MAINTENANCE_WINDOW
    found: Set[datetime] = set()
    for event in item.status_events:
        if not isinstance(event, dict) or event.get("status") != "Ready":
            continue
        previous = event.get("previous_status")
        if previous is None:
            previous = event.get("previousStatus")
        # `cmd_shaped` writes Ideas -> Ready on a clear first pass, while a
        # plan held at Shaped produces Shaped -> Ready when it is later
        # approved by the path. Both need the marker check below; Nate's
        # explicit approval has no Self-approved line and is omitted.
        if previous not in ("Ideas", "Shaped"):
            continue
        at = event.get("at")
        if not isinstance(at, datetime):
            created = event.get("created_at")
            if created is None:
                created = event.get("createdAt")
            at = parse_time(created)
        if at is None or at < cutoff:
            continue
        found.add(at)
    return sorted(found)


def _self_approval_markers(item: Item) -> List[Dict[str, object]]:
    """Read marker comments for one already-identified candidate item."""
    comments = _issue_comments(item)

    found: List[Dict[str, object]] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        basis = parse_self_approval(comment.get("body") or "")
        if basis is None:
            continue
        found.append({
            "basis": basis,
            "at": parse_time(comment.get("createdAt")),
        })
    return found


def unattended_approvals(
    items: Iterable[Item], now: datetime
) -> List[Dict[str, object]]:
    """Recent agent Ready transitions carrying their own approval marker.

    Status history is already part of the Project load. Only items with a
    recent Ideas/Shaped-to-Ready candidate pay for an issue-comment lookup,
    and a record is emitted only when the marker is present. That makes a
    Nate-authored ``approve`` transition invisible here without reading every
    issue's comments.
    """
    found: List[Dict[str, object]] = []
    for item in items:
        transition_times = _self_approval_transition_times(item, now)
        if not transition_times:
            continue
        markers = _self_approval_markers(item)
        if not markers:
            continue

        used: Set[int] = set()
        for at in transition_times:
            matched = None
            for index, marker in enumerate(markers):
                if index in used:
                    continue
                marker_at = marker.get("at")
                if isinstance(marker_at, datetime) and marker_at < at:
                    continue
                matched = (index, marker)
                break
            if matched is None:
                continue
            index, marker = matched
            used.add(index)
            found.append({
                "ref": item.ref,
                "title": item.title,
                "url": item.url,
                "at": at.isoformat(),
                "basis": marker["basis"],
            })

    return sorted(found, key=lambda row: (row["at"], row["ref"]), reverse=True)


def agent_health(now: datetime) -> List[Dict[str, str]]:
    """Raised watchdog conditions, derived from the same heartbeat rows."""
    try:
        import heartbeat

        providers = sorted(heartbeat.PROVIDERS)
    except Exception:
        return []

    found: List[Dict[str, str]] = []
    retired = getattr(heartbeat, "RETIRED_AGENTS", frozenset())
    for agent in providers:
        if agent in retired:
            continue  # a stopped schedule is not a dying one (#431)
        try:
            conditions = assess_agent_health(
                agent, _brief_heartbeat_rows(agent), now.timestamp()
            )
        except Exception:
            # The brief is a diagnostic surface. An unreachable heartbeat must
            # not hide the rest of the funnel or turn rendering into a failure.
            continue
        found.extend(
            {"agent": agent, "condition": condition}
            for condition in conditions
        )
    return found


def recent_resend_ratio(now: datetime) -> Dict[str, Optional[float]]:
    """Return recent weighted input re-send ratios for metered agents.

    Heartbeat finish records carry total and fresh input counts when the
    harness exposes both. Sum those counts across the existing diagnostic
    window before dividing so a short run cannot outweigh a long one merely
    because its individual ratio is larger. Malformed or partial telemetry is
    ignored; a diagnostic must not invent a figure from an incomplete record.
    """
    try:
        import heartbeat
    except Exception:
        return {}

    cutoff = (now - MAINTENANCE_WINDOW).timestamp()
    ratios: Dict[str, Optional[float]] = {}
    for agent in METERED_AGENTS:
        try:
            rows = _brief_heartbeat_rows(agent)
        except Exception:
            continue

        total = 0
        fresh = 0
        found = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("phase") != "finish":
                continue
            timestamp = row.get("ts")
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
            ):
                continue
            if timestamp < cutoff:
                continue
            usage = row.get("input_usage")
            if not isinstance(usage, dict):
                continue
            row_total = usage.get("total_input_tokens")
            row_fresh = usage.get("fresh_input_tokens")
            if (
                isinstance(row_total, bool)
                or not isinstance(row_total, int)
                or row_total < 0
                or isinstance(row_fresh, bool)
                or not isinstance(row_fresh, int)
                or row_fresh < 0
                or row_fresh > row_total
            ):
                continue
            total += row_total
            fresh += row_fresh
            found = True

        if found:
            ratios[agent] = total / fresh if fresh else None
    return ratios


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
CHECKOUT_STALENESS_FIX = "git -C ~/.claude/command-center pull --ff-only"
STATUSLINE_COMMAND = "~/.claude/statusline.sh"

# `usage.py:36` reads this cache, while `statusline.sh:24` writes it. Keep the
# path here rather than importing usage.py: doctor has to diagnose a broken
# install when that module is one of the links that is missing.
USAGE_CACHE = pathlib.Path.home() / ".claude" / "command-center-usage.json"

# `usage.py:174` uses fifteen minutes to decide whether one gate reading is
# usable. Doctor reports the source the gate would choose, so it uses the same
# freshness boundary before trying the transcript estimate.
USAGE_CACHE_BROKEN_AFTER = timedelta(minutes=15)
USAGE_CACHE_FIX = "run a Claude Code session so a transcript exists"

# Keep the fallback's discovery local for the same reason the cache path lives
# here: doctor must still be able to diagnose an install when usage.py is one of
# the things that is missing. These mirror usage.py's estimate inputs without
# making the doctor depend on that module.
CLAUDE_TRANSCRIPTS = str(
    pathlib.Path.home() / ".claude" / "projects" / "*" / "*.jsonl"
)
CLAUDE_ESTIMATE_FIVE_HOUR = timedelta(hours=5)
CLAUDE_ESTIMATE_RESET_WEEKDAY = 5  # usage.py: Saturday, local time
CLAUDE_ESTIMATE_RESET_HOUR = 12
CLAUDE_ESTIMATE_MODEL = "opus"

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
  rateLimit { cost remaining resetAt }
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


def _symlink_fix(link: pathlib.Path, expected: pathlib.Path) -> str:
    """Suggest only a repair that ``scripts/install.sh`` can perform."""
    try:
        expected_exists = expected.exists()
    except OSError:
        expected_exists = False
    if not expected_exists:
        return "restore {}, then run {}".format(expected, INSTALL_FIX)

    try:
        is_real_path = link.exists() and not link.is_symlink()
    except OSError:
        is_real_path = False
    if is_real_path:
        return "move {} aside, then run {}".format(link, INSTALL_FIX)
    return INSTALL_FIX


def _legacy_invocation_count(checkout_root: pathlib.Path,
                             legacy_funnel_path: str) -> int:
    """Count commands that execute the old, human-owned funnel checkout."""
    invocation = "python3 " + legacy_funnel_path
    count = 0
    for routine in sorted((checkout_root / "routines").glob("*.md")):
        # The routine files retain a prohibition saying never to touch this
        # checkout even after #205 removes the commands. Count invocations,
        # not bare path mentions, so that prose does not defeat retirement.
        count += routine.read_text(encoding="utf-8").count(invocation)
    return count


def _git_ahead_behind(root: pathlib.Path) -> Tuple[int, int]:
    """Return commits ahead/behind ``origin/main`` without changing the tree."""
    proc = _run_bounded_subprocess(
        ["git", "-C", str(root), "rev-list", "--left-right", "--count",
         "HEAD...origin/main"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise OSError(detail or "git exited {}".format(proc.returncode))
    fields = proc.stdout.split()
    if len(fields) != 2:
        raise OSError("git returned an invalid ahead/behind count")
    try:
        return int(fields[0]), int(fields[1])
    except ValueError as exc:
        raise OSError("git returned an invalid ahead/behind count") from exc


def check_repository_drift(
    checkout_root: Optional[os.PathLike] = None,
) -> Check:
    """Report local repository state that can make the checkout misleading.

    This is deliberately diagnostic and read-only. A dirty tree, a branch
    other than ``main`` (including detached HEAD), or commits that are not on
    ``origin/main`` all make it harder to tell which committed code a local
    command is actually exercising.
    """
    root = _path(checkout_root, CHECKOUT_ROOT)
    try:
        git_env = os.environ.copy()
        # `git status` may refresh the index unless optional locks are
        # disabled. Doctor must not write even that incidental repository
        # metadata while it is inspecting the checkout.
        git_env["GIT_OPTIONAL_LOCKS"] = "0"
        status = _run_bounded_subprocess(
            ["git", "-C", str(root), "status", "--porcelain=v1",
             "--untracked-files=all"],
            capture_output=True,
            text=True,
            env=git_env,
        )
        if status.returncode != 0:
            detail = (status.stderr or status.stdout or "").strip()
            raise OSError(detail or "git status exited {}".format(
                status.returncode))
        paths = [
            line[3:] if len(line) >= 3 else line
            for line in status.stdout.splitlines()
            if line.strip()
        ]

        branch = _run_bounded_subprocess(
            ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short",
             "HEAD"],
            capture_output=True,
            text=True,
        )
        if branch.returncode == 0:
            current_branch = branch.stdout.strip()
            if not current_branch:
                raise OSError("git returned an empty branch name")
        elif branch.returncode == 1:
            current_branch = None
        else:
            detail = (branch.stderr or branch.stdout or "").strip()
            raise OSError(detail or "git symbolic-ref exited {}".format(
                branch.returncode))

        ahead, _ = _git_ahead_behind(root)
    except OSError as exc:
        return Check(
            "repository drift", False,
            "could not inspect {} ({})".format(
                root, str(exc) or "unknown error"),
            "",
        )

    findings: List[str] = []
    if paths:
        findings.append("uncommitted changes: {}".format(", ".join(paths)))
    if current_branch is None:
        findings.append("detached HEAD (not main)")
    elif current_branch != "main":
        findings.append(
            "current branch is {}, not main".format(current_branch)
        )
    if ahead:
        findings.append(
            "{} commit(s) on the current branch are not present on "
            "origin/main".format(ahead)
        )

    return Check("repository drift", not findings, "\n".join(findings), "")


def check_checkout_staleness(
    claude_dir: Optional[os.PathLike] = None,
    checkout_root: Optional[os.PathLike] = None,
) -> Check:
    """Check Nate's checkout only while routines still execute it.

    The check is intentionally self-retiring. Once the routines point at the
    maintained run clone, this checkout may remain stale or dirty without
    affecting an unattended run, so the doctor says nothing about it.
    """
    claude = _path(claude_dir, CLAUDE_DIR)
    source = _path(checkout_root, CHECKOUT_ROOT)
    canonical = claude / "command-center"
    legacy_funnel_path = str(canonical / "funnel.py")

    try:
        invocations = _legacy_invocation_count(source, legacy_funnel_path)
    except OSError as exc:
        return Check(
            "checkout staleness", False,
            "could not inspect routines for {} ({})".format(
                legacy_funnel_path, str(exc) or "unknown error"),
            "restore the checkout and rerun funnel doctor",
        )

    if not invocations:
        return Check("checkout staleness", True, "", "")

    try:
        ahead, behind = _git_ahead_behind(canonical)
    except OSError as exc:
        return Check(
            "checkout staleness", False,
            "{} routine invocation(s) still use {}; could not compare {} "
            "with origin/main ({})".format(
                invocations, legacy_funnel_path, canonical,
                str(exc) or "unknown error"),
            "restore the checkout and rerun funnel doctor",
        )

    if behind:
        detail = "{} is {} commit(s) behind origin/main".format(
            canonical, behind)
        if ahead:
            detail += " and {} commit(s) ahead".format(ahead)
        return Check(
            "checkout staleness", False,
            "{} while {} routine invocation(s) still use {}".format(
                detail, invocations, legacy_funnel_path),
            CHECKOUT_STALENESS_FIX,
        )

    if ahead:
        found = "{} is {} commit(s) ahead of origin/main".format(
            canonical, ahead)
    else:
        found = "{} matches origin/main".format(canonical)
    return Check(
        "checkout staleness", True,
        "{}; {} routine invocation(s) still use {}".format(
            found, invocations, legacy_funnel_path),
        "",
    )


def _expected_links(claude_dir: pathlib.Path,
                    checkout_root: pathlib.Path) -> List[tuple]:
    """Return links into the checkout that scripts/install.sh creates."""
    links = [
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
    fixes: List[str] = []
    links = _expected_links(claude, root)

    for link, expected in links:
        try:
            exists = os.path.lexists(link)
        except OSError:
            exists = False
        shown = str(link)
        if not exists:
            errors.append("{} is missing".format(shown))
            if not fixes:
                fixes.append(_symlink_fix(link, expected))
            continue
        if not link.is_symlink():
            errors.append("{} is not a symlink".format(shown))
            if not fixes:
                fixes.append(_symlink_fix(link, expected))
            continue

        try:
            resolved = link.resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append("{} does not resolve".format(shown))
            if not fixes:
                fixes.append(_symlink_fix(link, expected))
            continue

        if not _inside(resolved, root_resolved):
            errors.append("{} points outside the checkout ({})".format(
                shown, resolved))
            if not fixes:
                fixes.append(_symlink_fix(link, expected))
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
            if not fixes:
                fixes.append(_symlink_fix(link, expected))

    if errors:
        return Check("install symlinks", False, "; ".join(errors),
                     "; ".join(fixes) or INSTALL_FIX)
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


def _claude_estimate_reset(now: float) -> float:
    """Return the local-time weekly reset used by Claude's estimate."""
    here = datetime.fromtimestamp(now)
    candidate = here.replace(
        hour=CLAUDE_ESTIMATE_RESET_HOUR, minute=0, second=0, microsecond=0
    ) - timedelta(
        days=(here.weekday() - CLAUDE_ESTIMATE_RESET_WEEKDAY) % 7
    )
    if candidate > here:
        candidate -= timedelta(days=7)
    return candidate.timestamp()


def _latest_claude_estimate(transcript_glob: os.PathLike,
                            now: float) -> Optional[Tuple[pathlib.Path, float]]:
    """Find the newest transcript record the Claude estimate can read.

    This is deliberately the small availability slice of
    ``usage.read_claude_local``. It does not calculate a percentage; doctor only
    needs to know whether that fallback source exists and how old its newest
    usable record is.
    """
    cutoff = min(
        _claude_estimate_reset(now),
        now - CLAUDE_ESTIMATE_FIVE_HOUR.total_seconds(),
    )
    newest: Optional[Tuple[float, pathlib.Path]] = None

    for name in glob.glob(os.path.expanduser(str(transcript_glob))):
        path = pathlib.Path(name)
        try:
            with path.open(errors="replace") as stream:
                for line in stream:
                    if '"usage"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    model = str(message.get("model") or "").lower()
                    if CLAUDE_ESTIMATE_MODEL not in model:
                        continue
                    usage = message.get("usage")
                    tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
                    if not isinstance(tokens, (int, float)) or isinstance(tokens, bool) or tokens <= 0:
                        continue
                    stamp = _timestamp(record.get("timestamp"))
                    if stamp is None or stamp < cutoff:
                        continue
                    if newest is None or stamp > newest[0]:
                        newest = (stamp, path)
        except OSError:
            continue

    if newest is None:
        return None
    return newest[1], now - newest[0]


def check_usage_cache(cache_path: Optional[os.PathLike] = None,
                      now: Optional[float] = None) -> Check:
    """Report the source the Claude usage gate would use.

    A fresh statusline cache is preferred. When it is absent or too old, the
    same recent transcript records used by ``usage.read_claude_local`` are the
    fallback. This intentionally does not import usage.py: doctor must diagnose
    a broken install even when that module is one of the missing links.
    """
    cache = _path(cache_path, USAGE_CACHE)
    now = time.time() if now is None else now

    def fallback(cache_finding: str) -> Check:
        estimate = _latest_claude_estimate(CLAUDE_TRANSCRIPTS, now)
        if estimate is not None:
            transcript, age = estimate
            return Check(
                "usage cache", True,
                "transcript estimate {} is readable ({})".format(
                    transcript, _age_text(age)),
                "",
            )
        return Check(
            "usage cache", False,
            "{}; transcript estimate is unavailable".format(cache_finding),
            USAGE_CACHE_FIX,
        )

    try:
        exists = cache.exists()
    except OSError:
        exists = False
    if not exists:
        return fallback(
            "{} is missing (age unavailable)".format(cache)
        )

    file_age = _mtime_age(cache, now)
    try:
        with cache.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return fallback(
            "{} is missing (age unavailable)".format(cache)
        )
    except (OSError, UnicodeError) as exc:
        return fallback(
            "{} is present but cannot be read ({}; {})".format(
                cache, exc, _age_text(file_age))
        )
    except ValueError:
        return fallback(
            "{} is present but unparseable ({})".format(
                cache, _age_text(file_age))
        )

    if not isinstance(data, dict):
        return fallback(
            "{} is present but unparseable (top-level JSON is not an object; {})".format(
                cache, _age_text(file_age))
        )

    captured_at = _timestamp(data.get("captured_at"))
    if captured_at is None:
        return fallback(
            "{} is present but unparseable (captured_at is missing or invalid; {})".format(
                cache, _age_text(file_age))
        )

    age = now - captured_at
    if age < -60:
        return fallback(
            "{} is present but its timestamp is from the future ({})".format(
                cache, _age_text(age))
        )
    if age > USAGE_CACHE_BROKEN_AFTER.total_seconds():
        return fallback(
            "{} is present but stale ({}; freshness threshold is 15 minutes)".format(
                cache, _age_text(age))
        )
    return Check(
        "usage cache", True,
        "statusline cache {} is present and fresh ({})".format(
            cache, _age_text(age)),
        "",
    )


def gh_auth_status() -> dict:
    """Read the active GitHub account without ever requesting its token."""
    proc = _run_gh(
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
        proc = _run_gh(
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
    """List spool files without importing heartbeat.py."""
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


def _spool_pending_records(files: Iterable[pathlib.Path]) -> List[object]:
    """Read non-empty spool lines without importing heartbeat.py.

    A drained spool file remains on disk as a zero-byte file. Count records
    from its non-empty lines instead of treating the file itself as pending.
    Keep the raw decoded values so a malformed line is still visible as a
    pending record, while a well-formed heartbeat record can supply its own
    timestamp for the age diagnostic.
    """
    records: List[object] = []
    for path in files:
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        records.append(None)
        except (OSError, UnicodeError) as exc:
            raise OSError("{}: {}".format(path, exc)) from exc
    return records


def check_heartbeat(spool_dir: Optional[os.PathLike] = None,
                    now: Optional[float] = None) -> Check:
    """Check the remote heartbeat branch and any undrained local spool files."""
    spool = _path(spool_dir, HEARTBEAT_SPOOL)
    now = time.time() if now is None else now
    findings: List[str] = []
    branch_ok: Optional[bool]
    files: List[pathlib.Path] = []
    pending_records: List[object] = []

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
        findings.append("local heartbeat spool {} cannot be read ({})".format(
            spool, exc))
    else:
        if not files:
            findings.append("local heartbeat spool {} is empty".format(spool))
        else:
            try:
                pending_records = _spool_pending_records(files)
            except OSError as exc:
                findings.append("local heartbeat spool {} cannot be read ({})".format(
                    spool, exc))
            else:
                if not pending_records:
                    findings.append(
                        "local heartbeat spool {} has {} file(s), all drained "
                        "(no pending records)".format(spool, len(files))
                    )
                else:
                    timestamps = [
                        _timestamp(record.get("ts"))
                        for record in pending_records
                        if isinstance(record, dict)
                    ]
                    valid_timestamps = [
                        timestamp for timestamp in timestamps
                        if timestamp is not None
                    ]
                    oldest = min(valid_timestamps) if valid_timestamps else None
                    age = _age_text(None if oldest is None else now - oldest)
                    findings.append(
                        "local heartbeat spool {} has {} pending record(s) "
                        "across {} file(s); oldest is {} old".format(
                            spool,
                            len(pending_records),
                            len(files),
                            age[4:] if age.startswith("age ") else age,
                        )
                    )

    broken = (
        branch_ok is not True
        or bool(pending_records)
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


def _workflow_rows(payload: object) -> List[dict]:
    """Normalise the REST workflow response used by the repo doctor check."""
    if isinstance(payload, dict):
        payload = payload.get("workflows")
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _ci_workflow_present(payload: object) -> bool:
    """Whether the response contains a workflow stored in `.github/workflows`."""
    for row in _workflow_rows(payload):
        path = row.get("path")
        # `actions/workflows` normally supplies `path`.  Accept a pathless
        # fixture row as a workflow too, while excluding GitHub's synthetic
        # Dependabot workflow, whose path is `dynamic/dependabot/...`.
        if path is None or str(path).startswith(".github/workflows/"):
            return True
    return False


def _repo_label_names(payload: object) -> Tuple[str, ...]:
    """Return label names from a repository-label REST response."""
    if not isinstance(payload, list):
        raise GitHubError("repository labels response was not a list")
    names = {
        str(row["name"])
        for row in payload
        if isinstance(row, dict) and row.get("name") is not None
    }
    return tuple(sorted(names, key=str.casefold))


def _stock_label_names(labels: Iterable[str]) -> Tuple[str, ...]:
    """Return remaining default GitHub labels, preserving displayed spelling."""
    return tuple(sorted(
        (label for label in labels
         if label.casefold() in STOCK_GITHUB_LABELS),
        key=str.casefold,
    ))


def _dependabot_configured(repo: str) -> bool:
    """Whether either supported Dependabot config filename exists."""
    for filename in ("dependabot.yml", "dependabot.yaml"):
        payload = _gh_json(
            "gh", "api", "repos/{}/contents/.github/{}".format(repo, filename)
        )
        if payload is not None:
            return True
    return False


def member_repo_readiness(repo: str) -> MemberRepoReadiness:
    """Read the checkable onboarding facts for one topic-bearing repository."""
    workflows = _gh_json(
        "gh", "api", "repos/{}/actions/workflows".format(repo)
    )
    labels = _gh_json(
        "gh", "api", "repos/{}/labels?per_page=100".format(repo)
    )
    return MemberRepoReadiness(
        repo=repo,
        # `member_repos()` is the membership query and only returns repos with
        # this topic.  Keep the fact explicit for the later queue predicate.
        topic=True,
        ci_workflow=_ci_workflow_present(workflows),
        stock_labels=_stock_label_names(_repo_label_names(labels)),
        dependabot=_dependabot_configured(repo),
    )


def _member_repo_found(readiness: MemberRepoReadiness) -> str:
    """Render every onboarding fact, including advisory findings."""
    ci = (
        "CI workflow present"
        if readiness.ci_workflow
        else "CI workflow missing (blocking)"
    )
    topic = (
        "command-center topic applied"
        if readiness.topic
        else "command-center topic missing (blocking)"
    )
    labels = (
        "stock GitHub labels removed"
        if not readiness.stock_labels
        else "stock GitHub labels remain: {} (advisory)".format(
            ", ".join(readiness.stock_labels)
        )
    )
    dependabot = (
        "Dependabot configured"
        if readiness.dependabot
        else "Dependabot not configured (advisory)"
    )
    return "; ".join((ci, topic, labels, dependabot))


def check_member_repo(repo: str) -> Check:
    """Build one doctor check for a topic-bearing member repository."""
    try:
        readiness = member_repo_readiness(repo)
    except Exception as exc:
        return Check(
            "member repo {}".format(repo), False,
            "{}: readiness query failed: {}".format(
                repo, str(exc) or "unknown error"
            ),
            "restore GitHub access, then rerun funnel doctor",
        )

    blocking = readiness.blocking_reasons
    fix = (
        "add a CI workflow before starting work in {}".format(repo)
        if blocking
        and not readiness.ci_workflow
        else ""
    )
    return Check(
        "member repo {}".format(repo),
        not blocking,
        _member_repo_found(readiness),
        fix,
    )


def check_member_repos(repos: Optional[Iterable[str]] = None) -> List[Check]:
    """Return one readiness check for every topic-bearing member repository."""
    try:
        names = list(member_repos() if repos is None else repos)
    except Exception as exc:
        return [Check(
            "member repos", False,
            "GitHub member-repo search failed: {}".format(
                str(exc) or "unknown error"
            ),
            "restore GitHub access, then rerun funnel doctor",
        )]

    return [
        check_member_repo(repo)
        for repo in sorted(set(names))
    ]


def repo_readiness_for_items(
    items: Iterable[Item],
) -> Dict[str, MemberRepoReadiness]:
    """Read onboarding facts once for each repository in the loaded funnel."""
    return {
        repo: member_repo_readiness(repo)
        for repo in sorted({item.repo for item in items})
    }


def _status_for_consistency(item: Item) -> str:
    """Name an unset Project Status without inventing a value for it."""
    return item.status or "unset"


def item_consistency_findings(
    items: Iterable[Item],
    merged_pr_facts: Optional[MergedPRFacts] = None,
) -> List[str]:
    """Return one read-only finding for each item with contradictory facts.

    Issue state and Project Status are separate GitHub facts, as is the
    descriptive ``needs-shaping`` label. The funnel reports disagreements but
    never chooses which side to rewrite. Multiple disagreements on one item
    stay on one line so the output remains one line per item. Merged PR facts
    are passed in by the caller so this remains pure over fixture Items.
    """
    merged_ticket_refs = (
        merged_pr_facts.ticket_refs if merged_pr_facts else frozenset()
    )
    findings: List[str] = []
    for item in items:
        status = _status_for_consistency(item)
        reasons: List[str] = []

        if (
            item.state == "CLOSED"
            and not item.parent
            and item.status not in ("Done", "Parked")
        ):
            reasons.append("state is CLOSED but Status is {}".format(status))
        if item.state == "OPEN" and item.status == "Done":
            reasons.append("state is OPEN but Status is Done")
        if "needs-shaping" in item.labels and item.status != "Ideas":
            reasons.append(
                "label needs-shaping is present but Status is {}".format(status)
            )
        if (
            item.state == "OPEN"
            and not item.parent
            and item.status != "Building"
            and item.children_all_closed
        ):
            reasons.append(
                "project has all children closed but Status is {}; resolve the "
                "Status before accepting it".format(status)
            )
        if (
            item.state == "OPEN"
            and item.parent
            and item.ref in merged_ticket_refs
        ):
            finding = "open ticket has a merged PR"
            if merged_pr_facts and merged_pr_facts.truncated:
                finding += (
                    " (merged PR scan truncated after newest {} entries)"
                    .format(MERGED_PR_SCAN_LIMIT)
                )
            reasons.append(finding)

        if reasons:
            findings.append("{}: {}".format(item.ref, "; ".join(reasons)))
    return findings


def check_item_consistency(
    items: Iterable[Item],
    merged_pr_facts: Optional[MergedPRFacts] = None,
) -> Check:
    """Build the doctor check for contradictions already present in ``items``."""
    findings = item_consistency_findings(items, merged_pr_facts=merged_pr_facts)
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


def unparseable_block_comment_findings(items: Iterable[Item]) -> List[str]:
    """Return one finding for each malformed block comment on loaded items."""
    return [
        "{}: {}".format(item.ref, first_line)
        for item in items
        for first_line in item.unparseable_block_comments
    ]


def check_block_comments(items: Iterable[Item]) -> Check:
    """Build the read-only block-comment syntax check for ``funnel doctor``."""
    rows = list(items)
    findings = unparseable_block_comment_findings(rows)
    findings.extend(
        "{}: {}".format(item.ref, item.block_comments_error)
        for item in rows
        if item.block_comments_error
    )
    return Check("block comments", not findings, "\n".join(findings), "")


def suspected_human_step_findings(items: Iterable[Item]) -> List[str]:
    """Return blocked tickets whose human requirement is not machine-readable."""
    return [
        "{}: suspected human step ({})".format(
            item.ref, suspected_human_step_reason(item)
        )
        for item in suspected_human_step_items(items)
    ]


def check_suspected_human_steps(items: Iterable[Item]) -> Check:
    """Build the read-only suspected-human-step doctor check."""
    findings = suspected_human_step_findings(items)
    return Check(
        "suspected human steps", not findings, "\n".join(findings), ""
    )


def check_block_conditions(items: Iterable[Item]) -> Check:
    """Report blocked items whose conditions are satisfied or unresolvable.

    The loaded Project rows contain both the parsed block comments and the
    native dependency facts. Keep this check pure so ``funnel doctor`` does not
    pay for a second request per blocked ticket, and keep still-waiting items
    visible without making an ordinary open dependency a doctor failure.
    """
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    findings: List[str] = []
    broken = False

    candidates = sorted(
        (
            item for item in rows
            if item.state == "OPEN"
            and (
                item.is_blocked
                or item.block_reason is not None
                or item.block_references
                or item.open_blockers
                or item.dead_blockers
            )
        ),
        key=lambda item: (item.repo, item.number),
    )

    for item in candidates:
        satisfied = satisfied_block_refs(item, by_ref)
        if satisfied:
            findings.append(
                "{}: satisfied block conditions: {}".format(
                    item.ref, ", ".join(satisfied)
                )
            )
            broken = True
            continue

        dead = _dead_dependency_refs(item, by_ref)
        if dead:
            findings.append(
                "{}: unresolvable block conditions: {}".format(
                    item.ref, ", ".join(dead)
                )
            )
            broken = True
            continue

        if item.block_reason is None:
            detail = "block comment is not parseable"
        elif not item.block_references:
            detail = "no machine-readable conditions"
        else:
            resolved = [
                _dependency_ref(item, value) or str(value).strip()
                for value in item.block_references
            ]
            detail = "on {}".format(", ".join(resolved))
        findings.append("{}: still-waiting ({})".format(item.ref, detail))

    return Check("block conditions", not broken, "\n".join(findings), "")


def doctor_checks(claude_dir: Optional[os.PathLike] = None,
                  checkout_root: Optional[os.PathLike] = None,
                  usage_cache: Optional[os.PathLike] = None,
                  heartbeat_spool: Optional[os.PathLike] = None,
                  items: Optional[Iterable[Item]] = None,
                  merged_pr_facts: Optional[MergedPRFacts] = None) -> List[Check]:
    """Run every fixed check, even when an earlier one is broken.

    ``items`` is supplied by ``cmd_doctor`` after the normal Project load. It
    keeps the consistency check pure and lets fixture callers avoid GitHub.
    """
    checks = [
        check_symlinks(claude_dir=claude_dir, checkout_root=checkout_root),
        check_checkout_staleness(
            claude_dir=claude_dir, checkout_root=checkout_root),
        check_repository_drift(checkout_root=checkout_root),
        check_settings(claude_dir=claude_dir),
        check_auth_scope(),
        check_project_fields(),
        check_topic(),
        *check_member_repos(),
        check_usage_cache(cache_path=usage_cache),
        check_heartbeat(spool_dir=heartbeat_spool),
    ]
    if items is not None:
        items = list(items)
        checks.append(check_item_consistency(
            items, merged_pr_facts=merged_pr_facts
        ))
        checks.append(check_class_assignments(items))
        checks.append(check_block_comments(items))
        checks.append(check_block_conditions(items))
        checks.append(check_prose_dependencies(items))
        checks.append(check_suspected_human_steps(items))
    return checks


def check_api_usage(command: str = "funnel doctor") -> Check:
    """Report this command's calls and measured GraphQL headroom.

    A `gh` CLI response does not carry the GraphQL point cost, so keep those
    invocations visible as a separate count. The remaining figure is only the
    latest value returned in a GraphQL `rateLimit` block; REST's
    `/rate_limit` endpoint is a different counter and is deliberately never
    consulted here.
    """
    usage = api_usage()
    if not usage["calls"]:
        return Check("API usage", True, "", "")

    remaining = usage["remaining"]
    found = (
        "{} made {} API call(s) ({} GraphQL, {} gh CLI); cost {} GraphQL "
        "point(s); {} remaining".format(
            command,
            usage["calls"],
            usage["graphql_calls"],
            usage["cli_calls"],
            usage["cost"],
            "unknown" if remaining is None else remaining,
        )
    )
    if usage["reset_at"]:
        found += ", resets {}".format(usage["reset_at"])

    if remaining is None:
        return Check(
            "API usage", False, found,
            "restore GraphQL rate-limit data, then rerun funnel doctor",
        )
    return Check("API usage", True, found, "")


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
        try:
            merged_facts = merged_pr_facts(items)
        except GitHubError as exc:
            # Keep the local and Project checks useful, but fail closed on the
            # new remote fact rather than silently claiming consistency when
            # the bounded merged-PR scan could not run.
            checks = doctor_checks(items=items)
            consistency = next(
                (index for index, check in enumerate(checks)
                 if check.name == "item consistency"),
                None,
            )
            failure = "merged PR scan failed: {}".format(
                str(exc) or "unknown error"
            )
            if consistency is None:
                checks.append(Check(
                    "item consistency", False, failure,
                    "restore GitHub access, then rerun funnel doctor",
                ))
            else:
                check = checks[consistency]
                found = "\n".join(filter(None, [check.found, failure]))
                checks[consistency] = Check(
                    check.name, False, found,
                    "restore GitHub access, then rerun funnel doctor",
                )
        else:
            checks = doctor_checks(
                items=items, merged_pr_facts=merged_facts
            )
    checks.append(check_api_usage())
    render_checks(checks)
    return 0 if all(check.ok for check in checks) else 1


# --------------------------------------------------------------------------
# GitHub. One query root: the Project. See LEARNINGS.md for why an issue
# cannot be asked which Projects it belongs to.
# --------------------------------------------------------------------------

REPO_QUERY = """
query($cursor: String) {
  rateLimit { cost remaining resetAt }
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
  rateLimit { cost remaining resetAt }
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
          pinned: fieldValueByName(name: "Pinned") {
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
              timelineItems(last: 60, itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT, LABELED_EVENT, UNLABELED_EVENT]) {
                nodes {
                  __typename
                  ... on ProjectV2ItemStatusChangedEvent {
                    createdAt previousStatus status project { number }
                  }
                  ... on LabeledEvent {
                    createdAt label { name }
                  }
                  ... on UnlabeledEvent {
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


#: Per-process GraphQL spend, accumulated from the responses themselves.
#:
#: Not state of record and it never outlives the process, so `GitHub is the
#: state` is untouched. It exists because consumption was invisible until it
#: hit zero: on 2026-09-08 `funnel.py` stopped entirely — `brief`, `queue`,
#: `next`, every gate command — for the better part of an hour, several times
#: in one day, with no earlier symptom to read (#273).
_GRAPHQL_SPEND: Dict[str, object] = {
    "calls": 0, "cost": 0, "remaining": None, "reset_at": None,
}

#: Number of GraphQL responses whose own rate-limit block exposed a usable
#: integer `cost`.  A zero cost is a real measurement; a missing block is not.
#: Keep this separate from `_GRAPHQL_SPEND` so the existing doctor/report
#: contract remains unchanged while a heartbeat finish can fail closed on an
#: unreadable point total.
_GRAPHQL_COST_READS = 0

#: Every attempted `gh` call in the current funnel command, split at the
#: boundary where the response can expose a cost.  GraphQL gives us a true
#: per-query point cost; the `gh` CLI does not expose one, so its calls remain
#: visible as a separate count rather than being assigned a guessed cost.
_API_USAGE: Dict[str, int] = {"graphql_calls": 0, "cli_calls": 0}

#: The parsed command's optional provenance values, used only by the process
#: exit hook below.  Commands without `--run` still resolve an unambiguous open
#: heartbeat start through `_heartbeat_context`.
_ACTIVE_HEARTBEAT_RUN: Optional[str] = None
_ACTIVE_HEARTBEAT_AGENT: Optional[str] = None

#: Once one response says the GraphQL route is exhausted, every later
#: GraphQL-backed call in this process is refused without being attempted.
#: Measured 2026-09-09: after the hourly budget hit zero, a single funnel
#: command went on issuing a `gh` call per ticket or PR it examined, each one
#: failing the same way, and the schedules re-fired into it every five minutes
#: (#429). A refused call is counted here and named in the error; the run
#: stops and the next scheduled run picks the work up after the reset.
_ROUTE_EXHAUSTED: Dict[str, Optional[Dict[str, object]]] = {"graphql": None}

#: What GitHub says when the route has nothing left. Matched case-insensitively
#: against `gh`'s stderr; a transient failure never matches.
EXHAUSTED_SIGNALS = (
    "api rate limit already exceeded",
    "api rate limit exceeded",
    "rate limit exceeded",
)


def route_exhausted() -> Optional[Dict[str, object]]:
    """The exhaustion record for this process, or None while the route is live."""
    return _ROUTE_EXHAUSTED["graphql"]


def reset_route_state() -> None:
    """Forget an exhaustion seen earlier; tests and fresh processes only."""
    _ROUTE_EXHAUSTED["graphql"] = None


def _uses_graphql(command: Sequence[str]) -> bool:
    """Whether a `gh` invocation spends the GraphQL budget.

    `gh api <path>` is REST and has its own counter; everything else the
    funnel runs -- `gh api graphql`, `gh pr`, `gh issue`, `gh project` -- is
    GraphQL-backed and shares the one exhausted route.
    """
    command = list(command)
    if command[:2] == ["gh", "api"]:
        return command[:3] == ["gh", "api", "graphql"]
    return True


def _mark_exhausted(message: str, reset_at: Optional[str] = None) -> None:
    if _ROUTE_EXHAUSTED["graphql"] is not None:
        return
    _ROUTE_EXHAUSTED["graphql"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "reset_at": reset_at or _GRAPHQL_SPEND.get("reset_at"),
        "message": (message or "").strip()[:200],
    }


def _is_exhausted_signal(stderr: object) -> bool:
    text = (stderr or "") if isinstance(stderr, str) else ""
    lowered = text.lower()
    return any(signal in lowered for signal in EXHAUSTED_SIGNALS)


def _refuse_if_exhausted(command: Sequence[str]) -> None:
    state = _ROUTE_EXHAUSTED["graphql"]
    if state is None or not _uses_graphql(command):
        return
    _API_USAGE["refused_exhausted"] = int(_API_USAGE.get("refused_exhausted", 0)) + 1
    reset = state.get("reset_at")
    raise GitHubError(
        "GitHub GraphQL budget exhausted earlier in this run{}; not attempted: {}. "
        "Stopping rather than retrying into it -- the next scheduled run picks "
        "this up after the reset.".format(
            " (resets {})".format(reset) if reset else "",
            " ".join(str(part) for part in list(command)[:6]),
        )
    )



def graphql_spend() -> Dict[str, object]:
    """What this process has spent on GraphQL so far, read from responses."""
    return dict(_GRAPHQL_SPEND)


def reset_api_usage() -> None:
    """Start a fresh per-command API measurement."""
    global _GRAPHQL_COST_READS
    _API_USAGE.update({"graphql_calls": 0, "cli_calls": 0, "refused_exhausted": 0})
    _GRAPHQL_SPEND.update(
        {"calls": 0, "cost": 0, "remaining": None, "reset_at": None}
    )
    _GRAPHQL_COST_READS = 0


def api_usage() -> Dict[str, object]:
    """Return attempted calls plus measured GraphQL cost and headroom."""
    graphql_calls = int(_API_USAGE["graphql_calls"])
    cli_calls = int(_API_USAGE["cli_calls"])
    spend = graphql_spend()
    return {
        "calls": graphql_calls + cli_calls,
        "graphql_calls": graphql_calls,
        "cli_calls": cli_calls,
        "refused_exhausted": int(_API_USAGE.get("refused_exhausted", 0)),
        "cost": spend["cost"],
        "remaining": spend["remaining"],
        "reset_at": spend["reset_at"],
    }


def api_cost() -> Dict[str, Optional[int]]:
    """Return the two per-command measurements used by heartbeat records.

    GraphQL points are known only when every attempted GraphQL response exposed
    an integer `rateLimit.cost`; a missing block is reported as ``None`` rather
    than the misleading zero held by the doctor counter.  The total `gh_calls`
    value is the same call count used by the API-scaling guard: every actual
    invocation through `_run_gh`, whether GraphQL or another `gh` command.
    A command with no calls has no measurement at all and returns two nulls.
    """
    usage = api_usage()
    calls = int(usage["calls"])
    if not calls:
        return {"graphql_points": None, "gh_calls": None}

    graphql_calls = int(usage["graphql_calls"])
    if not graphql_calls or _GRAPHQL_COST_READS == graphql_calls:
        points = int(usage["cost"])
    else:
        points = None
    return {"graphql_points": points, "gh_calls": calls}


def _run_gh(args: Sequence[str], **kwargs):
    """Run `gh` and count the attempted call in the right bucket.

    Refuses without running when this process has already seen the GraphQL
    route exhausted and the call would spend it (#429); learns the exhaustion
    from `gh`'s own stderr when the output is captured.
    """
    command = list(args)
    _refuse_if_exhausted(command)
    if command[:3] == ["gh", "api", "graphql"]:
        _API_USAGE["graphql_calls"] += 1
    else:
        _API_USAGE["cli_calls"] += 1
    brief_timeout = _brief_timeout_remaining()
    if brief_timeout is not None:
        existing_timeout = kwargs.get("timeout")
        if existing_timeout is None:
            kwargs["timeout"] = brief_timeout
        else:
            kwargs["timeout"] = min(float(existing_timeout), brief_timeout)
    try:
        proc = _run_bounded_subprocess(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        state = _BRIEF_SECTION_STATE.get()
        if state:
            raise BriefSectionTimeout(
                state[0], "GitHub read timed out"
            ) from exc
        raise
    if (getattr(proc, "returncode", 0) != 0 and _uses_graphql(command)
            and _is_exhausted_signal(getattr(proc, "stderr", None))):
        _mark_exhausted(proc.stderr)
    return proc


def _record_rate_limit(block: object) -> None:
    """Accumulate one response's reported cost.

    **Summed from each response's own ``cost``, never inferred by differencing
    ``remaining`` between calls.** The token is shared — a measurement on
    2026-09-08 found 1,826 points spent by something other than the measuring
    session — so ``remaining`` moves under the funnel's feet from Nate's own
    `gh` use and every other agent on it. A delta would attribute their spend
    to the funnel.
    """
    global _GRAPHQL_COST_READS
    _GRAPHQL_SPEND["calls"] = int(_GRAPHQL_SPEND["calls"]) + 1
    if not isinstance(block, dict):
        return
    cost = block.get("cost")
    if isinstance(cost, int) and not isinstance(cost, bool):
        _GRAPHQL_COST_READS += 1
        _GRAPHQL_SPEND["cost"] = int(_GRAPHQL_SPEND["cost"]) + cost
    remaining = block.get("remaining")
    if isinstance(remaining, int) and not isinstance(remaining, bool):
        _GRAPHQL_SPEND["remaining"] = remaining
    if isinstance(block.get("resetAt"), str):
        _GRAPHQL_SPEND["reset_at"] = block["resetAt"]


def gh_graphql(query: str, **variables) -> dict:
    """Run a GraphQL query and preserve its top-level ``rateLimit`` field.

    Read queries include ``rateLimit`` beside their existing root fields, so
    callers can inspect the cost without changing the shape they already
    index into. Mutations deliberately do not request it: GitHub exposes the
    field on the query root, not the mutation root.

    Each response's rate-limit block is also accumulated into
    ``graphql_spend()`` so a run can report what it spent.
    """
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        cmd += [flag, "{}={}".format(key, value)]
    proc = _run_gh(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitHubError(proc.stderr.strip() or "gh exited {}".format(proc.returncode))
    payload = json.loads(proc.stdout)
    if payload.get("errors"):
        raise GitHubError(json.dumps(payload["errors"]))
    data = payload["data"]
    if isinstance(data, dict):
        block = data.get("rateLimit")
        _record_rate_limit(block)
        if isinstance(block, dict) and block.get("remaining") == 0:
            _mark_exhausted("rateLimit.remaining is 0", block.get("resetAt"))
    return data


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
        pinned=(node.get("pinned") or {}).get("name") == "Pinned",
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
    timeline_nodes = ((content.get("timelineItems") or {}).get("nodes") or [])
    for event in timeline_nodes:
        if not event:
            continue
        label = (event.get("label") or {}).get("name")
        if label == "blocked" and event.get("__typename") == "LabeledEvent":
            item.blocked_since = parse_time(event.get("createdAt"))
            continue
        if label == "blocked" and event.get("__typename") == "UnlabeledEvent":
            item.blocked_cleared_at = parse_time(event.get("createdAt"))
            continue
        if (event.get("project") or {}).get("number") != PROJECT_NUMBER:
            continue
        if event.get("__typename") == "ProjectV2ItemStatusChangedEvent":
            at = parse_time(event.get("createdAt"))
            if at is not None:
                item.status_events.append({
                    "previous_status": event.get("previousStatus"),
                    "status": event.get("status"),
                    "at": at,
                })
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
    mark_projects_that_carried_human_steps(items)
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


def print_parking_prompt(items: Iterable[Item], now: datetime) -> None:
    """Print copy-ready parking commands for the oldest unstarted projects."""
    candidates = parking_candidates(items)
    if not candidates:
        return

    print("\nLongest-waiting unstarted projects — consider parking any that no longer earn their place:")
    for item in candidates:
        print("  {} — {} ({})".format(
            item.ref, item.title, humanise(item.waited(now))))
        print('    funnel park {} --reason "<why>"'.format(item.ref))


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
    rendered = {
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
    if item.pinned:
        rendered["pinned"] = True
    if item.needs_decision is not None:
        rendered["needs_decision"] = item.needs_decision
    return rendered


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
    comments = _issue_comments(item)
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


def closed_itself_items(items: Iterable[Item], now: datetime) -> List[Item]:
    """Closed projects recent enough to carry a funnel-close record."""
    cutoff = now - CLOSED_ITSELF_WINDOW
    return sorted(
        (
            item for item in items
            if item.parent is None
            and item.state == "CLOSED"
            and item.status == "Done"
            and item.closed_at is not None
            and item.closed_at >= cutoff
        ),
        key=lambda item: (
            -item.closed_at.timestamp(), item.repo, item.number
        ),
    )


def _closed_itself_item_json(item: Item) -> Optional[Dict[str, object]]:
    """Render one funnel-close marker, or omit an ordinary accepted close."""
    comments = _issue_comments(item)
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        payload = _marked_json(
            comment.get("body") or "", CLOSED_ITSELF_PREFIX
        )
        if payload is None:
            continue
        drift = payload.get("drift", [])
        if not isinstance(drift, list):
            drift = []
        drift = [value for value in drift if isinstance(value, str)]
        return {
            "ref": item.ref,
            "title": item.title,
            "url": item.url,
            "closed_at": item.closed_at.isoformat() if item.closed_at else None,
            "drift": drift,
        }
    return None


def closed_itself_json(
    items: Iterable[Item], now: datetime
) -> List[Dict[str, object]]:
    """The brief's recent funnel-close records, newest first."""
    rows = []
    for item in closed_itself_items(items, now):
        row = _closed_itself_item_json(item)
        if row is not None:
            rows.append(row)
    return rows


def cleared_block_items(items: Iterable[Item], now: datetime) -> List[Item]:
    """Recently unblocked issues that may carry a mechanical-clear record."""
    cutoff = now - CLEARED_BLOCK_WINDOW
    return sorted(
        (
            item for item in items
            if not item.is_blocked
            and item.blocked_cleared_at is not None
            and item.blocked_cleared_at >= cutoff
        ),
        key=lambda item: (
            -item.blocked_cleared_at.timestamp(), item.repo, item.number
        ),
    )


def _cleared_block_item_json(item: Item) -> Optional[Dict[str, object]]:
    """Render the newest valid satisfied-block record on one unblocked item."""
    comments = _issue_comments(item)
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        record = parse_satisfied_block_comment(comment.get("body") or "")
        if record is None:
            continue
        return {
            "ref": item.ref,
            "title": item.title,
            "url": item.url,
            "conditions": record["conditions"],
            "cleared_at": (
                item.blocked_cleared_at.isoformat()
                if item.blocked_cleared_at else None
            ),
        }
    return None


def cleared_blocks_json(
    items: Iterable[Item], now: datetime
) -> List[Dict[str, object]]:
    """The brief's recent provenance-backed mechanical block clears."""
    rows = []
    for item in cleared_block_items(items, now):
        row = _cleared_block_item_json(item)
        if row is not None:
            rows.append(row)
    return rows


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
    rendered = {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "reason": item.block_reason,
        "conditions": item.block_references,
        "blocked_at": item.status_since.isoformat() if item.status_since else None,
    }
    if item.needs_decision is not None:
        rendered["needs_decision"] = item.needs_decision
    return rendered


def blocked_json(items: Iterable[Item]) -> List[Dict[str, object]]:
    """The brief's blocked section, reusing one load-time comment fetch."""
    return [_blocked_item_json(item) for item in blocked_items(items)]


PROSE_DEPENDENCY_RE = re.compile(
    r"\b(?:depends\s+on|blocked\s+on)\s+"
    r"(?P<references>#[0-9]+(?:\s*(?:,|and)\s*#[0-9]+)*"
    r"|(?P<unnumbered>(?![^.!?]*#[0-9]+)[^.!?]+))"
    r"|\bafter\s+(?P<after>#[0-9]+)\s+lands\b"
    r"|\b(?:until|requires)\s+(?P<single>#[0-9]+)\b",
    re.IGNORECASE,
)

# A Nate-origin idea may move through the shaping command without an assigned
# Class, but only when the plan leaves him a visible proposal to answer. This
# is a presence check, not a parser for a value: no agent infers or writes a
# Class from plan prose on Nate's behalf.
PROPOSED_CLASS_RE = re.compile(
    r"^[ \t]*Proposed[ \t]+class:[ \t]*\S.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _prose_dependency_sentences(body: str) -> Iterable[Tuple[str, List[str]]]:
    """Yield recognised dependency sentences and their named issue numbers."""
    if not isinstance(body, str):
        return

    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip()
            if not sentence:
                continue
            match = PROSE_DEPENDENCY_RE.search(sentence)
            if match is None:
                continue
            references = (
                match.group("references")
                or match.group("after")
                or match.group("single")
            )
            yield sentence, re.findall(r"#[0-9]+", references or "")


def prose_dependencies(items: Iterable[Item]) -> List[Dict[str, object]]:
    """Report open prose dependencies that have no matching native edge.

    This is deliberately pure over the Project items already loaded by the
    funnel. A named issue is reportable only when it is present and open in
    that set; resolving an absent issue would require a new API call and would
    turn a diagnostic into a second dependency source.
    """
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    found: List[Dict[str, object]] = []

    for item in rows:
        if item.state != "OPEN" or item.parent is None:
            continue

        native = {
            _dependency_ref(item, value) or str(value).strip()
            for value in item.open_blockers
        }
        for sentence, numbers in _prose_dependency_sentences(item.body or ""):
            if not numbers:
                # An unnumbered dependency cannot be matched to one edge. It
                # is reportable only while the ticket has no native dependency
                # at all; the repair run reads the sentence before choosing
                # which blocker to write.
                if native or item.dead_blockers:
                    continue
                found.append({
                    "ref": item.ref,
                    "names": [],
                    "sentence": sentence,
                })
                continue
            names: List[str] = []
            for number in numbers:
                ref = item.repo + number
                blocker = by_ref.get(ref)
                if (
                    blocker is None
                    or blocker.state != "OPEN"
                    or ref in native
                    or ref in names
                ):
                    continue
                names.append(ref)
            if names:
                found.append({
                    "ref": item.ref,
                    "names": names,
                    "sentence": sentence,
                })

    return sorted(found, key=lambda row: row["ref"])


def prose_dependency_findings(items: Iterable[Item]) -> List[str]:
    """Render the shared prose-dependency rows for ``funnel doctor``."""
    findings = []
    for row in prose_dependencies(items):
        names = row["names"]
        if names:
            detail = "missing native blocked_by edge for {}: {}".format(
                ", ".join(names), row["sentence"]
            )
        else:
            detail = "unnumbered dependency: {}".format(row["sentence"])
        findings.append("{}: {}".format(row["ref"], detail))
    return findings


def check_prose_dependencies(items: Iterable[Item]) -> Check:
    """Build the report-only prose-dependency doctor check."""
    findings = prose_dependency_findings(items)
    return Check("prose dependencies", not findings, "\n".join(findings), "")


def has_proposed_class(plan: str) -> bool:
    """Whether a plan gives Nate a non-empty ``Proposed class:`` line."""
    return isinstance(plan, str) and PROPOSED_CLASS_RE.search(plan) is not None


def capture_origin(item: Item) -> str:
    """Return the recorded capture origin, or ``unknown`` when absent."""
    origin = parse_origin(item.body or "")
    if origin is None:
        return "unknown"
    return origin["voice"]


def unclassed_capture_items(items: Iterable[Item]) -> List[Item]:
    """Open Ideas whose Project Class is missing or invalid.

    Ideas stay outside the decision counts. This diagnostic only makes the
    forgotten assignment visible, preserving the funnel's unbounded Ideas
    stage and leaving the origin-specific repair to the right actor.
    """
    return [item for item in ideas(items) if item.klass not in LADDER]


def _unclassed_capture_item_json(item: Item) -> Dict[str, object]:
    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "origin": capture_origin(item),
    }


def unclassed_captures_json(items: Iterable[Item]) -> List[Dict[str, object]]:
    """Render the diagnostic list without adding a human decision."""
    return [
        _unclassed_capture_item_json(item)
        for item in unclassed_capture_items(items)
    ]


def suspected_human_step_reason(item: Item) -> Optional[str]:
    """Return a human-step reason hidden inside an unreadable block.

    This is deliberately narrower than ``human_step_items``. It only reports
    open child issues that are already blocked and whose block has no
    machine-readable references. A named block stays an ordinary machine
    block, even when its prose happens to mention a human-step reason.
    """
    if (
        item.state != "OPEN"
        or item.parent is None
        or not item.is_blocked
        or item.block_references
    ):
        return None

    reason = matching_human_step_reason(item.block_reason)
    if reason is not None:
        return reason

    # ``_load_block_comment`` keeps the first line of malformed block comments
    # so the existing doctor check can report it without another fetch. Use the
    # newest recorded line first, and fail closed when it contains no exact
    # allowlisted reason.
    for first_line in reversed(item.unparseable_block_comments):
        reason = matching_human_step_reason(first_line)
        if reason is not None:
            return reason
    return None


def suspected_human_step_items(items: Iterable[Item]) -> List[Item]:
    """Return blocked child issues that may hide an unrecognised human step."""
    return sorted(
        (
            item for item in items
            if suspected_human_step_reason(item) is not None
        ),
        key=lambda item: (item.repo, item.number),
    )


def _suspected_human_step_item_json(item: Item) -> Dict[str, object]:
    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "reason": suspected_human_step_reason(item),
    }


def suspected_human_step_json(
    items: Iterable[Item],
) -> List[Dict[str, object]]:
    """Render suspected human steps without changing any GitHub state."""
    return [
        _suspected_human_step_item_json(item)
        for item in suspected_human_step_items(items)
    ]


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


def satisfied_block_refs(
    item: Item, by_ref: Dict[str, Item]
) -> Optional[List[str]]:
    """Return the parsed block conditions that are all satisfied.

    A missing parsed comment, an empty reference list, an unresolvable
    reference, a missing blocker, an open blocker, or a blocker that is
    explicitly unable to close all fail closed with ``None``. The checks are
    deliberately separate so a caller can report which part of the
    four-part satisfaction test failed without treating an empty list as
    vacuously satisfied.

    This mirrors ``_dead_dependency_refs`` over the already-loaded native and
    comment dependency facts. It never fetches a blocker: a reference must be
    present in ``by_ref`` before it can satisfy a block.
    """
    # ``block_reason`` is populated only when ``parse_block_comment`` found a
    # matching header. An empty reason is still a parsed comment; ``None`` is
    # the unparsed state and must not be treated as satisfied.
    if item.block_reason is None:
        return None

    values = list(item.block_references)
    if not values:
        return None

    resolved: List[str] = []
    for value in values:
        ref = _dependency_ref(item, value)
        if ref is None:
            return None
        resolved.append(ref)

    native_open = {
        _dependency_ref(item, value) or str(value).strip()
        for value in item.open_blockers
    }
    native_dead = set(getattr(item, "dead_blockers", []))
    satisfied: Set[str] = set()
    for ref in resolved:
        blocker = by_ref.get(ref)
        if (
            blocker is None
            or blocker.state != "CLOSED"
            or ref in native_open
            or ref in native_dead
            or _never_closing(blocker)
        ):
            return None
        satisfied.add(ref)

    return sorted(satisfied)


def satisfied_block_comment(
    conditions: Sequence[str], now: datetime,
    run: Optional[str] = None, agent: Optional[str] = None,
) -> str:
    """Build the durable, provenance-marked record for one mechanical clear."""
    refs = sorted(set(conditions))
    found_closed_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "conditions": refs,
        "found_closed_at": found_closed_at,
    }
    body = (
        SATISFIED_BLOCK_PREFIX
        + "all machine-readable conditions were found closed: {}.\n\n"
          "Found closed at `{}`.\n\n```json\n{}\n```".format(
              ", ".join(refs), found_closed_at,
              json.dumps(payload, indent=2, sort_keys=True),
          )
    )
    return append_provenance(
        body, "agent", at=now, run=run, agent=agent
    )


def _record_covers_current_block(item: Item, conditions: Sequence[str]) -> bool:
    """Whether the loaded record already covers this exact blocking episode."""
    record = item.satisfied_block_record
    if not isinstance(record, dict):
        return False
    if sorted(record.get("conditions") or []) != sorted(set(conditions)):
        return False
    recorded_at = parse_time(record.get("found_closed_at"))
    return recorded_at is not None and (
        item.blocked_since is None or recorded_at >= item.blocked_since
    )


def clear_satisfied_blocks(
    items: Sequence[Item], now: datetime,
    run: Optional[str] = None, agent: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Clear every fully parsed satisfied block and return what moved.

    The record is posted first. If removing the label then fails, a later run
    sees that record on the still-blocked issue and retries only the label
    write. A partial network failure therefore cannot erase the reason for an
    unattended clear or manufacture duplicate comments.
    """
    by_ref = {item.ref: item for item in items}
    cleared: List[Dict[str, object]] = []
    candidates = sorted(
        (
            item for item in items
            if item.state == "OPEN" and item.is_blocked
        ),
        key=lambda item: (item.repo, item.number),
    )
    for item in candidates:
        conditions = satisfied_block_refs(item, by_ref)
        if not conditions:
            continue

        if not _record_covers_current_block(item, conditions):
            comment = _run_gh(
                [
                    "gh", "issue", "comment", str(item.number),
                    "--repo", item.repo,
                    "--body", satisfied_block_comment(
                        conditions, now, run=run, agent=agent
                    ),
                ],
                capture_output=True, text=True,
            )
            if comment.returncode != 0:
                raise GitHubError(
                    "could not record satisfied block on {}: {}".format(
                        item.ref, comment.stderr.strip()
                    )
                )

        edit = _run_gh(
            [
                "gh", "issue", "edit", str(item.number),
                "--repo", item.repo, "--remove-label", "blocked",
            ],
            capture_output=True, text=True,
        )
        if edit.returncode != 0:
            raise GitHubError(
                "recorded satisfied block on {}, but could not remove its "
                "blocked label: {}".format(item.ref, edit.stderr.strip())
            )

        item.labels = [label for label in item.labels if label != "blocked"]
        item.blocked_cleared_at = now
        cleared.append({
            "ref": item.ref,
            "conditions": conditions,
            "cleared_at": now.isoformat(),
        })
    return cleared


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


def _block_cycle_reasons(
    items: Sequence[Item], by_ref: Dict[str, Item]
) -> Dict[str, List[str]]:
    """Return one diagnostic reason for each detected open block cycle.

    Edges point from a waiting item to its blocker. Native dependency facts and
    parsed comment references are deliberately combined here: either source
    can be stale or incomplete on its own, while a cycle may cross both. Only
    open items in the loaded Project can participate in a stranded cycle;
    missing or closed references are ignored just as they are by the other
    derived dependency checks.

    A cycle is searched from its lowest-numbered member, and candidate edges
    back to an earlier member are skipped. That makes the displayed path
    canonical and prevents the same loop from being reported once per member.
    The walk is iterative so malformed dependency data cannot exhaust Python's
    call stack while producing a diagnostic.
    """
    open_refs = {
        item.ref for item in items if item.state == "OPEN"
    }
    graph: Dict[str, Set[str]] = {ref: set() for ref in open_refs}
    for item in items:
        if item.ref not in open_refs:
            continue
        for value in list(item.open_blockers) + list(item.block_references):
            ref = _dependency_ref(item, value)
            if ref in open_refs:
                graph[item.ref].add(ref)

    def order(ref: str) -> Tuple[int, str]:
        item = by_ref[ref]
        return item.number, item.repo

    def display(refs: Sequence[str]) -> str:
        cycle_items = [by_ref[ref] for ref in refs]
        same_repo = len({item.repo for item in cycle_items}) == 1
        labels = (
            ["#{}".format(item.number) for item in cycle_items]
            if same_repo
            else list(refs)
        )
        return "block cycle: " + " → ".join(labels)

    reasons: Dict[str, List[str]] = {}
    for start in sorted(graph, key=order):
        pending = [(start, [start])]
        cycle = None
        while pending:
            current, path = pending.pop()
            for target in sorted(graph[current], key=order, reverse=True):
                if target == start:
                    cycle = path + [start]
                    break
                if target in path or order(target) < order(start):
                    continue
                pending.append((target, path + [target]))
            if cycle is not None:
                break
        if cycle is not None:
            reasons.setdefault(start, []).append(display(cycle))
    return reasons


def stranded_items(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Render items for which no current agent or gate can make progress.

    This is deliberately a diagnostic, not a queue. The first release only
    uses facts the funnel already knows how to read: an approved current-head
    verdict on a conflicting PR, a stale claim with no PR, a childless
    ``Building`` project, a self-approvable ``Building`` project whose upkeep
    children all closed but the project itself did not, a native or named
    dependency closed as ``not_planned``, plus cycles formed by native or
    parsed block edges, and two PR-side strands when PR facts were requested:
    an open PR on a closed ticket or an open PR whose project's Status is not
    ``Building``. Missing CI history is intentionally absent; no fetched fact
    distinguishes that from a PR whose first check is still pending.

    ``pr_facts`` is optional so the function remains fixture-pure. ``None``
    means the caller has not requested PR lookups and therefore treats a stale
    claim as having no PR; a supplied mapping distinguishes a known no-PR
    result from a fact that was not fetched.
    """
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    stale = {
        item.ref for item in stale_locks(rows, now, pr_facts=pr_facts)
    }
    cycle_reasons = _block_cycle_reasons(rows, by_ref)
    found: List[Dict[str, object]] = []

    for item in rows:
        reasons: List[str] = []
        pr_known = pr_facts is None or item.ref in pr_facts
        pr = None if pr_facts is None else pr_facts.get(item.ref)

        if (
            isinstance(pr, dict)
            and str(pr.get("state") or "").upper() == "OPEN"
            and item.parent is not None
        ):
            if item.state == "CLOSED":
                reasons.append("open PR on closed ticket")
            elif item.state == "OPEN":
                parent = by_ref.get(item.parent)
                if parent is not None and parent.status != "Building":
                    status = parent.status or "unset"
                    reasons.append(
                        "open PR on open ticket whose project Status is {}; "
                        "merge gate will refuse it".format(status)
                    )

        if item.state != "OPEN":
            if reasons:
                found.append({
                    "ref": item.ref,
                    "title": item.title,
                    "url": item.url,
                    "reason": "; ".join(reasons),
                })
            continue

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

        if (
            item.parent is None
            and item.status == "Building"
            and item.klass in SELF_APPROVABLE_CLASSES
            and item.children_all_closed
            and not item.carried_human_step
        ):
            reasons.append("finished upkeep project not closed")

        dead = _dead_dependency_refs(item, by_ref)
        if dead:
            reasons.append(
                "blocked on blocker that will never close: {}".format(
                    ", ".join(dead))
            )

        reasons.extend(cycle_reasons.get(item.ref, []))

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


def _print_queue_section(items: Sequence[Item], render) -> None:
    """Print a queue section, grouping by repo only when it spans repos."""
    if not items:
        print("  nothing")
        return

    by_repo: Dict[str, List[Item]] = {}
    for item in items:
        by_repo.setdefault(item.repo, []).append(item)

    if len(by_repo) == 1:
        for item in items:
            print(render(item, "  "))
        return

    for repo, repo_items in by_repo.items():
        print("  {}:".format(repo))
        for item in repo_items:
            print(render(item, "    "))


def cmd_queue(
    items: List[Item],
    now: datetime,
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
) -> int:
    """Everything, ordered — both queues, each under its own heading.

    They are genuinely different orderings over different subsets, so a single
    merged list would have to pick one and misrepresent the other.
    """
    decisions = awaiting_decision(items)
    tickets = startable(items, repo_readiness=repo_readiness)

    print("Waiting on Nate ({}), bottom-up:".format(len(decisions)))
    by_ref = {i.ref: i for i in items}
    _print_queue_section(
        decisions,
        lambda item, prefix: "{}{:<10} {:<24} {:<34} {:<18} {}".format(
            prefix,
            item.status or "-",
            class_display(item, by_ref),
            item.ref,
            humanise(item.waited(now)),
            gate_question(item),
        ),
    )

    print("\nStartable by Codex ({}), ladder order:".format(len(tickets)))
    _print_queue_section(
        tickets,
        lambda item, prefix: "{}{:<24} {:<34} {}".format(
            prefix,
            class_display(item, by_ref),
            item.ref,
            item.title,
        ),
    )

    withheld = readiness_blockers(items, repo_readiness=repo_readiness)
    if withheld:
        print("\nWithheld by repository readiness ({}):".format(len(withheld)))
        for blocker in withheld:
            print("  {:<34} {}".format(
                blocker["ref"], ", ".join(blocker["reasons"])))

    pending = awaiting_breakdown(items)
    if pending:
        print("\nApproved, awaiting breakdown into tickets ({}):".format(len(pending)))
        _print_queue_section(
            pending,
            lambda item, prefix: "{}{:<24} {:<34} {:<18} {}".format(
                prefix,
                class_display(item, by_ref),
                item.ref,
                humanise(item.waited(now)),
                item.title,
            ),
        )

    missing = [i for i in items if needs_class(i)]
    if missing:
        print("\nInvalid — not in Ideas and carrying no Class ({}):".format(len(missing)))
        for item in missing:
            print("  {:<34} {}".format(item.ref, item.url))
    return 0


def _brief_error(exc: BaseException) -> str:
    """Return a short, useful diagnostic for one unreadable brief section."""
    return str(exc).strip() or exc.__class__.__name__


def _brief_read(
    section: str,
    reader: Callable[[], object],
    missing: List[Dict[str, str]],
    default: object = None,
) -> object:
    """Read one brief section without hiding an unreadable response.

    A caller may pre-record a section when a shared read failed before the
    renderer was entered. That keeps dependent sections from guessing from a
    partial fact set. Successful sections retain their existing JSON shape;
    failed ones become ``null`` and are named in the top-level ``missing``
    list.
    """
    if any(entry.get("section") == section for entry in missing):
        return default
    try:
        return reader()
    except BriefSectionTimeout:
        # The timing wrapper owns the budget diagnostic and must see this
        # exception rather than turning it into an ordinary missing read.
        raise
    except Exception as exc:
        missing.append({"section": section, "error": _brief_error(exc)})
        return default


def cmd_next(
    items: List[Item],
    now: datetime,
    tier: Optional[str] = None,
    excluded: Optional[Set[str]] = None,
    agent: str = "codex",
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> int:
    # Accept bare numbers as well as refs (#436). The routine passes whatever
    # the model copied from the ticket JSON, and a bare number matched nothing
    # in `next_ticket`'s ref comparison — so the run released its claim and
    # was handed the same ticket straight back.
    resolved = set()
    for ref in excluded or ():
        try:
            resolved.add(find(items, ref).ref)
        except (GitHubError, SystemExit, ValueError):
            resolved.add(ref)
    excluded = frozenset(resolved)
    # A ref reaches `--not` only from the run that was offered it, and `next`
    # never offers a claimed ticket to a second run — so the caller holds the
    # claim it is declining. Release it here rather than trusting the routine
    # to: on 2026-09-09 five declined claims were left behind, filled the WIP
    # cap within an hour, and stalled every engineer run behind them (#394).
    for ref in sorted(excluded):
        try:
            declined = find(items, ref)
        except (GitHubError, SystemExit, ValueError):
            continue
        if declined.in_motion_since:
            write_lock(declined, "")
            object.__setattr__(declined, "in_motion_since", None)
            print("released {} (declined)".format(declined.ref), file=sys.stderr)
    blocked = awaiting_review(items)
    # An approved current head that GitHub now reports as conflicting is no
    # longer review work: the reviewer already judged it, and the engineer must
    # rebase it. Every other open PR remains withheld, including UNKNOWN and
    # approvals for an older head.
    blocked.difference_update(approved_conflicting_refs(pr_facts))
    blocked.update(finished_by_comments(items))
    ticket = next_ticket_for_tier(
        items, now, tier=tier, blocked=blocked, excluded=excluded,
        agent=agent,
        repo_readiness=repo_readiness,
        pr_facts=pr_facts,
    )

    if ticket is None:
        holder = lock_holder(items, now, pr_facts=pr_facts)
        withheld = readiness_blockers(
            items, repo_readiness=repo_readiness, awaiting_review=blocked,
            agent=agent,
        )
        if holder is not None:
            print(
                "nothing — lock held by {} (claimed {} ago)".format(
                    holder.ref, humanise(now - holder.in_motion_since)
                ),
                file=sys.stderr,
            )
        elif withheld:
            print("nothing — {}".format(
                _readiness_blocker_summary(withheld)
            ), file=sys.stderr)
        elif tier:
            print("nothing — no {} work waiting".format(tier), file=sys.stderr)
        return 1
    print(json.dumps(item_json(ticket, now, {i.ref: i for i in items}), indent=2))
    return 0


_BRIEF_UNAVAILABLE = object()
_ACTIVE_BRIEF_CACHE: contextvars.ContextVar = contextvars.ContextVar(
    "active_brief_cache", default=None
)
_BRIEF_SECTION_STATE: contextvars.ContextVar = contextvars.ContextVar(
    "brief_section_state", default=None
)


class BriefSectionTimeout(RuntimeError):
    """A bounded read stopped because its brief section budget expired."""

    def __init__(self, section: str, reason: str):
        self.section = section
        self.reason = reason
        super().__init__(reason)


class BriefGateTimeout(RuntimeError):
    """A gate-feeding brief section cannot safely return a partial brief."""

    def __init__(self, section: str, elapsed: float, budget: float,
                 reason: str):
        self.section = section
        self.elapsed = elapsed
        self.budget = budget
        self.reason = reason
        super().__init__(
            "section {!r} exceeded its {:.3f}s budget after {:.3f}s: {}"
            .format(section, budget, elapsed, reason)
        )


def _brief_timeout_remaining() -> Optional[float]:
    """Return the active section's remaining subprocess budget, if any."""
    state = _BRIEF_SECTION_STATE.get()
    if not state:
        return None
    remaining = float(state[1]) - time.monotonic()
    return max(0.001, remaining)


class BriefCache:
    """Lazy, per-run cache for auxiliary brief reads.

    The cache is owned by ``FunnelSession`` and is never written to disk or
    shared across runs. That gives repeated commands in one Muse run the same
    cheap-read behaviour as the session's Project item load without turning
    diagnostic data into a second source of truth.
    """

    def __init__(self):
        self._pr_facts = _BRIEF_UNAVAILABLE
        self._heartbeat_rows: Dict[str, List[Dict]] = {}

    def clear(self) -> None:
        """Forget auxiliary reads after a command may have mutated GitHub."""
        self._pr_facts = _BRIEF_UNAVAILABLE
        self._heartbeat_rows.clear()

    def get_pr_facts(self, items: Sequence[Item]):
        if self._pr_facts is _BRIEF_UNAVAILABLE:
            self._pr_facts = ticket_pr_facts(items)
        return self._pr_facts

    def heartbeat_rows(self, agent: str) -> List[Dict]:
        if agent in self._heartbeat_rows:
            return self._heartbeat_rows[agent]

        import heartbeat

        timeout = _brief_timeout_remaining()
        try:
            if timeout is None:
                rows = heartbeat.read(agent)
            else:
                try:
                    rows = heartbeat.read(agent, timeout=timeout)
                except TypeError as exc:
                    # Keep fixture-era one-argument test doubles compatible
                    # while the real heartbeat reader uses the bound.
                    if "timeout" not in str(exc):
                        raise
                    rows = heartbeat.read(agent)
        except subprocess.TimeoutExpired as exc:
            state = _BRIEF_SECTION_STATE.get()
            section = state[0] if state else "unknown"
            raise BriefSectionTimeout(
                section, "heartbeat read timed out"
            ) from exc

        self._heartbeat_rows[agent] = rows
        return rows


def _brief_heartbeat_rows(agent: str) -> List[Dict]:
    """Read one heartbeat spool, reusing the active brief cache when present."""
    cache = _ACTIVE_BRIEF_CACHE.get()
    if cache is not None:
        return cache.heartbeat_rows(agent)
    import heartbeat
    return heartbeat.read(agent)


def _brief_degraded_record(section: str, elapsed: float, budget: float,
                           reason: str) -> Dict[str, object]:
    return {
        "section": section,
        "elapsed_seconds": round(max(0.0, elapsed), 6),
        "budget_seconds": budget,
        "reason": reason,
    }


def _brief_timed(
    section: str,
    reader: Callable[[], object],
    timings: Dict[str, float],
    degraded: Optional[List[Dict[str, object]]] = None,
    *,
    deadline: Optional[float] = None,
    budget: Optional[float] = None,
    gate: Optional[bool] = None,
) -> object:
    """Run one brief section, enforce its budget, and record its timing."""
    degraded = degraded if degraded is not None else []
    budget = float(
        BRIEF_SECTION_BUDGETS.get(section, 1.0)
        if budget is None else budget
    )
    gate = section in BRIEF_GATE_SECTIONS if gate is None else gate
    started = time.perf_counter()
    allowed = max(0.0, budget)
    if deadline is not None:
        allowed = min(allowed, max(0.0, deadline - started))
    limit = started + allowed

    def stop(reason: str, elapsed: float) -> object:
        timings[section] = round(max(0.0, elapsed), 6)
        if gate:
            raise BriefGateTimeout(section, elapsed, budget, reason)
        degraded.append(_brief_degraded_record(
            section, elapsed, budget, reason
        ))
        return _BRIEF_UNAVAILABLE

    if started >= limit:
        return stop("brief budget exhausted before the section started", 0.0)

    token = _BRIEF_SECTION_STATE.set(
        (section, time.monotonic() + allowed, budget)
    )
    try:
        try:
            value = reader()
        except BriefSectionTimeout as exc:
            elapsed = time.perf_counter() - started
            return stop(str(exc) or "section read timed out", elapsed)
    finally:
        _BRIEF_SECTION_STATE.reset(token)

    elapsed = time.perf_counter() - started
    timings[section] = round(max(0.0, elapsed), 6)
    if elapsed > budget or (deadline is not None and started + elapsed > deadline):
        reason = "section exceeded its time budget"
        if deadline is not None and started + elapsed > deadline:
            reason = "brief transport budget was exceeded"
        if gate:
            raise BriefGateTimeout(section, elapsed, budget, reason)
        degraded.append(_brief_degraded_record(
            section, elapsed, budget, reason
        ))
    return value


def cmd_brief(
    items: List[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
    missing: Optional[List[Dict[str, str]]] = None,
    timings: Optional[Dict[str, float]] = None,
    degraded: Optional[List[Dict[str, object]]] = None,
    deadline: Optional[float] = None,
    brief_cache: Optional[BriefCache] = None,
) -> int:
    missing = list(missing or [])
    timings = dict(timings or {})
    degraded = list(degraded or [])
    if deadline is None:
        deadline = time.perf_counter() + BRIEF_TOTAL_BUDGET_SECONDS
    cache = brief_cache or _ACTIVE_BRIEF_CACHE.get() or BriefCache()
    cache_token = _ACTIVE_BRIEF_CACHE.set(cache)

    def section(name: str, reader: Callable[[], object], default):
        value = _brief_timed(
            name,
            lambda: _brief_read(name, reader, missing),
            timings,
            degraded,
            deadline=deadline,
        )
        return default if value is _BRIEF_UNAVAILABLE else value

    def decision_payload():
        decisions = awaiting_decision(items)
        by_ref = {i.ref: i for i in items}
        return decisions, by_ref, [
            item_json(i, now, by_ref) for i in decisions
        ]

    try:
        decision_result = section("items", decision_payload, None)
        if decision_result is None:
            decisions = []
            by_ref = {i.ref: i for i in items}
            decision_rows = []
        else:
            decisions, by_ref, decision_rows = decision_result

        counts = section(
            "counts_by_gate",
            lambda: {
                stage: sum(
                    1 for i in items if i.status == stage and i.state == "OPEN"
                )
                for stage in STAGES
                if stage != "Ideas"
            },
            {},
        )
        running = section(
            "in_motion",
            lambda: in_motion(items, now, pr_facts=pr_facts),
            [],
        )
        parked = section("parked", lambda: parked_json(items), [])
        closed_itself = section(
            "closed_itself", lambda: closed_itself_json(items, now), []
        )
        cleared_blocks = section(
            "cleared_blocks", lambda: cleared_blocks_json(items, now), []
        )
        blocked = section("blocked", lambda: blocked_json(items), [])
        prose = section(
            "prose_dependencies", lambda: prose_dependencies(items), []
        )
        suspected = section(
            "suspected_human_steps",
            lambda: suspected_human_step_json(items),
            [],
        )
        human = section("human_steps", lambda: human_step_json(items), [])
        closed_access = section(
            "closed_with_access_vocabulary",
            lambda: closed_with_access_vocabulary_json(items),
            [],
        )
        unclassed = section(
            "unclassed_captures",
            lambda: unclassed_captures_json(items),
            [],
        )
        needs = section(
            "needs_class",
            lambda: [item_json(i, now, by_ref) for i in items if needs_class(i)],
            [],
        )
        breakdown = section(
            "awaiting_breakdown",
            lambda: [
                item_json(i, now, by_ref) for i in awaiting_breakdown(items)
            ],
            [],
        )
        stranded = section(
            "stranded",
            lambda: stranded_json(items, now, pr_facts=pr_facts),
            [],
        )
        stale = section(
            "stale_locks_taken_over",
            lambda: stale_locks(items, now, pr_facts=pr_facts),
            [],
        )
        maintenance = section(
            "maintenance_load", lambda: maintenance_load(items, now), {}
        )
        resend = section("resend_ratio", lambda: recent_resend_ratio(now), {})
        merges = section(
            "unattended_merges", lambda: unattended_merges(now), []
        )
        approvals = section(
            "unattended_approvals",
            lambda: unattended_approvals(items, now),
            [],
        )
        health = section("agent_health", lambda: agent_health(now), [])
        touched = section(
            "working_tree_touched", lambda: working_tree_touched(now), []
        )
        rejected = section(
            "rejected_merges", lambda: rejected_merges(items, now), {}
        )

        blocked_comment_errors = [
            "{}: {}".format(item.ref, item.block_comments_error)
            for item in items
            if item.block_comments_error
        ]
        if blocked_comment_errors and not any(
            entry.get("section") == "blocked" for entry in missing
        ):
            missing.append({
                "section": "blocked",
                "error": "; ".join(blocked_comment_errors),
            })

        brief = {
            "generated_at": now.isoformat(),
            "total_needing_nate": len(decisions),
            "counts_by_gate": counts,
            "items": decision_rows,
            "parked": parked,
            "closed_itself": closed_itself,
            "cleared_blocks": cleared_blocks,
            "blocked": blocked,
            "prose_dependencies": prose,
            "suspected_human_steps": suspected,
            "human_steps": human,
            "closed_with_access_vocabulary": closed_access,
            "unclassed_captures": unclassed,
            "needs_class": needs,
            "awaiting_breakdown": breakdown,
            "stranded": stranded,
            "in_motion": [i.ref for i in running] if running is not None else None,
            "wip_limit": WIP_LIMIT,
            "stale_locks_taken_over": [
                i.ref for i in stale
            ] if stale is not None else None,
            "maintenance_load": maintenance,
            "resend_ratio": resend,
            "unattended_merges": merges,
            "unattended_approvals": approvals,
            "agent_health": health,
            "working_tree_touched": touched,
            "rejected_merges": rejected,
            "degraded": degraded,
            "timings": timings,
            "missing": missing,
        }
        print(json.dumps(brief, indent=2))
        return 0
    except BriefGateTimeout as exc:
        print(
            "funnel: brief failed closed: {} (no partial JSON emitted)"
            .format(exc),
            file=sys.stderr,
        )
        return 2
    finally:
        _ACTIVE_BRIEF_CACHE.reset(cache_token)


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
    """Resolve an exact ref or URL before considering a bare issue number."""
    for i in items:
        if i.ref == ref or i.url == ref:
            return i

    matches = [i for i in items if str(i.number) == ref]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GitHubError(
            "ambiguous funnel item ref {} matches {}".format(
                ref, ", ".join(i.ref for i in matches)
            )
        )
    raise GitHubError("no funnel item matches {}".format(ref))


def claim_ticket(
    items: List[Item],
    now: datetime,
    target: Item,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> Optional[str]:
    """Write a ticket claim, or return the reason it must be refused."""
    running = in_motion(items, now, pr_facts=pr_facts)

    taken = next((i for i in running if i.ref == target.ref), None)
    if taken is not None and taken.in_motion_since != target.in_motion_since:
        return "refused — {} is already claimed".format(target.ref)

    if taken is None and len(running) >= WIP_LIMIT:
        by_ref = {i.ref: i for i in items}
        preempts = (
            effective_class(target, by_ref) == "Broken"
            and not any(
                effective_class(item, by_ref) == "Broken" for item in running
            )
        )
        if not preempts:
            return "refused — {} tickets already in motion, limit is {} ({})".format(
                len(running), WIP_LIMIT,
                ", ".join(i.ref for i in running))

    stale = [
        i for i in stale_locks(items, now, pr_facts=pr_facts)
        if i.ref != target.ref
    ]
    for item in stale:
        # Self-correcting, no human in the loop. Every takeover is a line in the
        # brief: one is noise, three in a week means runs are dying.
        write_lock(item, "")
        print("took over stale claim on {}".format(item.ref), file=sys.stderr)

    write_lock(target, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    # `Ready -> Building` is written on the first claim, here in the shared
    # implementation so `funnel begin` (#349) and `funnel claim` promote alike.
    _begin_parent(items, target)
    return None


def cmd_claim(
    items: List[Item],
    now: datetime,
    ref: str,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> int:
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
    refusal = claim_ticket(items, now, target, pr_facts=pr_facts)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1
    print(target.url)
    return 0


def _begin_parent(items: Sequence[Item], ticket: Item) -> None:
    """Move a claimed ticket's project to `Building`, once.

    `plan.md`: "`Ready → Building` is now written by Codex when it claims the
    first ticket. Each write is still explicit and attached to an event." This
    is that write, and it is the only one — `#287` deleted the `start` gate
    deliberately, because it asked Nate to re-decide a priority `funnel.py`
    already computes. The stage is the record that work began, not a permission
    to begin it.

    Only from `Ready`, so a later claim on the same project does not rewrite a
    stage that is already correct, and so nothing can drag a project backwards
    out of `Done` or `Parked`.

    Deliberately not fatal. The claim is the correctness-bearing write and it
    has already succeeded; a project left at `Ready` is visibly wrong and the
    next claim fixes it, whereas an exception here would lose a lock that is
    already held.
    """
    parent = next((i for i in items if i.ref == ticket.parent), None)
    if parent is None or parent.status != "Ready" or not parent.item_id:
        return
    try:
        gh_graphql(SET_FIELD, project=PROJECT_ID, item=parent.item_id,
                   field=STATUS_FIELD_ID,
                   option=_option_id(STATUS_FIELD_ID, "Building"))
        print("{} -> Building".format(parent.ref), file=sys.stderr)
    except GitHubError as exc:
        print("note: claimed {} but could not move {} to Building: {}".format(
            ticket.ref, parent.ref, exc), file=sys.stderr)


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
PINNED_FIELD_ID = "PVTSSF_lAHOD7A-N84BihDgzhhygHE"
PINNED_OPTION = "Pinned"

CLEAR_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field
  }) { projectV2Item { id } }
}
"""


def _option_id(field_id: str, name: str) -> str:
    data = gh_graphql(
        '{ rateLimit { cost remaining resetAt } '
        'node(id:"%s"){... on ProjectV2SingleSelectField{options{id name}}}}'
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

    close = _run_gh(
        ["gh", "issue", "close", str(item.number), "--repo", item.repo,
         "--reason", "not planned"],
        capture_output=True, text=True,
    )
    if close.returncode != 0:
        raise GitHubError(close.stderr.strip())

    comment = _run_gh(
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


def _pinnable_item(items: Sequence[Item], ref: str) -> Item:
    """Return a Project item suitable for a pin mutation."""
    item = find(items, ref)
    if item.parent:
        raise GitHubError(
            "{} is a ticket; only projects can be pinned".format(item.ref)
        )
    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))
    return item


def cmd_pin(items: List[Item], now: datetime, ref: str, confirmed: bool,
            run: Optional[str] = None, agent: Optional[str] = None) -> int:
    """Pin a Project item, recording Nate's explicit decision."""
    return _set_pinned(items, now, ref, True, confirmed, run=run, agent=agent)


def cmd_unpin(items: List[Item], now: datetime, ref: str, confirmed: bool,
              run: Optional[str] = None, agent: Optional[str] = None) -> int:
    """Clear a Project item's pin, recording Nate's explicit decision."""
    return _set_pinned(items, now, ref, False, confirmed, run=run, agent=agent)


def _set_pinned(items: List[Item], now: datetime, ref: str, pinned: bool,
                confirmed: bool, run: Optional[str] = None,
                agent: Optional[str] = None) -> int:
    """Write or preview one Project-level pin decision."""
    item = _pinnable_item(items, ref)
    verb = "pin" if pinned else "unpin"
    state = "Pinned" if pinned else "Unpinned"

    if not confirmed:
        print("would {} {} ({})".format(verb, item.ref, item.title))
        print("\nNothing was changed. Re-run with --yes to {} it.".format(verb))
        return 1

    if pinned:
        gh_graphql(
            SET_FIELD,
            project=PROJECT_ID,
            item=item.item_id,
            field=PINNED_FIELD_ID,
            option=_option_id(PINNED_FIELD_ID, PINNED_OPTION),
        )
    else:
        gh_graphql(
            CLEAR_FIELD,
            project=PROJECT_ID,
            item=item.item_id,
            field=PINNED_FIELD_ID,
        )

    comment = _run_gh(
        ["gh", "issue", "comment", str(item.number), "--repo", item.repo,
         "--body", append_provenance(
             "**{}:** Nate decided to {} this project.".format(state, verb),
             "nate-relayed", at=now, run=run, agent=agent)],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(comment.stderr.strip())

    print("{} → {}".format(item.ref, state))
    return 0


def cmd_comment(items: List[Item], now: datetime, ref: str, body: str,
                voice: str, run: Optional[str] = None,
                agent: Optional[str] = None,
                apply_blocked: bool = False) -> int:
    """Post an issue comment with an explicit, stamped voice."""
    if not body.strip():
        raise GitHubError("a non-empty comment body is required")
    item = find(items, ref)
    comment = _run_gh(
        ["gh", "issue", "comment", str(item.number), "--repo", item.repo,
         "--body", append_provenance(body, voice, at=now, run=run, agent=agent)],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(comment.stderr.strip())
    if apply_blocked:
        edit = _run_gh(
            [
                "gh", "issue", "edit", str(item.number), "--repo", item.repo,
                "--add-label", "blocked",
            ],
            capture_output=True, text=True,
        )
        if edit.returncode != 0:
            raise GitHubError(
                "recorded needs-decision comment on {}, but could not add its "
                "blocked label: {}".format(item.ref, edit.stderr.strip())
            )
        if not item.is_blocked:
            item.labels.append("blocked")
    print("recorded {} comment on {}".format(voice, item.ref))
    return 0


def cmd_reject(items: List[Item], now: datetime, pr: str, note: Optional[str]) -> int:
    """Record that a merged PR turned out to be broken.

    This is not "file a bug". The information is that **the auto-merge bar
    failed**, which is a different and more serious fact, and it is the only
    feedback loop on letting Claude merge unattended. One action does all four
    steps, rather than leaving them to be remembered.

    This is one of three commands that write `Status` by code rather than by Nate;
    it remains the single case that also writes `Class`.
    """
    number = pr.rstrip("/").split("/")[-1].lstrip("#")
    repo = REPO

    def run(*args: str) -> str:
        out = _run_gh(args, capture_output=True, text=True)
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
                agent: Optional[str] = None,
                origin: Optional[str] = None,
                klass: Optional[str] = None) -> int:
    """Capture an idea. Unbounded and guilt-free, by design."""
    if origin not in ORIGIN_VOICES:
        raise GitHubError(
            "capture requires an explicit --origin (nate-relayed or agent)"
        )
    if origin == "agent" and klass is None:
        raise GitHubError("capture requires --class when --origin agent")
    if klass is not None and klass not in LADDER:
        raise GitHubError(
            "unknown capture class {!r}; choose one of {}".format(
                klass, ", ".join(LADDER)
            )
        )
    repo = resolve_repo(repo)
    body = append_provenance(
        note or "Captured from chat. Not yet thought through.", "agent",
        at=now, run=run, agent=agent,
    )
    body = append_origin(body, origin, at=now, run=run, agent=agent)
    args = [
        "gh", "issue", "create", "--repo", repo, "--title", title,
        "--body", body, "--label", "needs-shaping",
    ]
    out = _run_gh(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    url = out.stdout.strip().splitlines()[-1]

    add = _run_gh(
        ["gh", "project", "item-add", str(PROJECT_NUMBER), "--owner", PROJECT_OWNER,
         "--url", url, "--format", "json"],
        capture_output=True, text=True,
    )
    if add.returncode == 0:
        item_id = json.loads(add.stdout)["id"]
        gh_graphql(SET_FIELD, project=PROJECT_ID, item=item_id,
                   field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, "Ideas"))
        if klass is not None:
            gh_graphql(
                SET_FIELD,
                project=PROJECT_ID,
                item=item_id,
                field=CLASS_FIELD_ID,
                option=_option_id(CLASS_FIELD_ID, klass),
            )
        print("{}  → Ideas (needs-shaping) in {}".format(url, repo))
    else:
        print("{} in {}\nnote: created, but not added to the Project".format(
                  url, repo),
              file=sys.stderr)
    return 0


def cmd_shaped(items: List[Item], now: datetime, ref: str, plan_file: str,
               run: Optional[str] = None, agent: Optional[str] = None,
               klass: Optional[str] = None) -> int:
    """Record that an idea has been grilled and a plan now exists.

    Writes the plan into the issue body — `plan.md` puts it there through Ideas
    and Shaped, and it only becomes a repo's own `plan.md` at the Ready gate —
    then moves the item to `Ready` only when the unattended self-approval
    predicate accepts the effective Class, origin, Needs section, and risk.
    Otherwise it stays at `Shaped`, which asks Nate the next gate: is the plan
    good?
    """
    item = find(items, ref)
    if klass is not None and klass not in LADDER:
        raise GitHubError(
            "unknown shaping class {!r}; choose one of {}".format(
                klass, ", ".join(LADDER)
            )
        )

    original_body = item.body or ""
    origin = parse_origin(original_body)
    origin_voice = origin["voice"] if origin is not None else None
    captured_origin = (
        _marked_json_block(original_body, ORIGIN_MARKER)
        if origin is not None else None
    )
    class_missing = item.klass not in LADDER
    if class_missing and origin_voice == "agent" and klass is None:
        raise GitHubError(
            "agent-origin idea has no Class; pass --class with one of {}".format(
                ", ".join(LADDER)
            )
        )

    try:
        if plan_file == "-":
            # Muse runs with --disable-write and cannot write a plan file; a
            # pipe writes nothing to disk (#366).
            plan = sys.stdin.read()
        else:
            plan = pathlib.Path(plan_file).read_text()
    except OSError as exc:
        raise GitHubError("cannot read {}: {}".format(plan_file, exc))
    if not plan.strip():
        raise GitHubError("the plan is empty; nothing to record")
    if class_missing and origin_voice != "agent" and not has_proposed_class(plan):
        raise GitHubError(
            "unclassed idea requires a non-empty Proposed class: line"
        )
    authority_signals = needs_nate_signals(plan)
    body = append_provenance(plan, "agent", at=now, run=run, agent=agent)
    if captured_origin is not None:
        body = "{}\n\n{}".format(body, captured_origin)
    overlaps = shaping_plan_overlap_candidates(items, item, plan)

    out = _run_gh(
        ["gh", "issue", "edit", str(item.number), "--repo", item.repo,
         "--body", body],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())

    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))
    plan_status, plan_reason = shaped_plan_status(plan)
    by_ref = {candidate.ref: candidate for candidate in items}
    effective_klass = effective_class(item, by_ref)
    if class_missing and origin_voice == "agent" and klass is not None:
        effective_klass = klass
    override = parse_origin_override(item.body or "")
    override_target = override["target"] if override is not None else None
    escalation_reasons = plan_is_escalated(plan)
    needs_nate = plan_status != "Ready"
    eligible = self_approval_eligible(
        effective_klass,
        origin_voice,
        override_target,
        needs_nate=needs_nate,
        escalated=bool(escalation_reasons),
    )
    status = "Ready" if eligible else "Shaped"
    failed_conditions = []
    if effective_klass not in SELF_APPROVABLE_CLASSES:
        class_name = effective_klass or "unset"
        failed_conditions.append(
            "class {} is not self-approvable".format(class_name)
        )
    if effective_shape_owner(origin_voice, override_target) != "agents":
        failed_conditions.append("origin is Nate's")
    if needs_nate:
        failed_conditions.append(plan_reason)
    if escalation_reasons:
        failed_conditions.append(
            "escalated risk ({})".format(", ".join(escalation_reasons))
        )
    reason = plan_reason if status == "Ready" else "; ".join(failed_conditions)
    if class_missing and origin_voice == "agent":
        # This recovery write is the only place shaping may assign a Class.
        # Keep it immediately before the Status mutation so the latter never
        # makes an unclassed idea look like it advanced cleanly.
        gh_graphql(SET_FIELD, project=PROJECT_ID, item=item.item_id,
                   field=CLASS_FIELD_ID, option=_option_id(CLASS_FIELD_ID, klass))
    gh_graphql(SET_FIELD, project=PROJECT_ID, item=item.item_id,
               field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, status))
    _run_gh(["gh", "issue", "edit", str(item.number), "--repo", item.repo,
                    "--remove-label", "needs-shaping"], capture_output=True)
    if status == "Ready":
        basis = "{}; no escalated risk".format(reason)
        if authority_signals:
            basis += "; authority signals: {}".format(
                ", ".join(authority_signals)
            )
        comment = _run_gh(
            ["gh", "issue", "comment", str(item.number), "--repo", item.repo,
             "--body", self_approval_comment(
                 basis, at=now, run=run, agent=agent
             )],
            capture_output=True, text=True,
        )
        if comment.returncode != 0:
            raise GitHubError(comment.stderr.strip())
    print("{} → {}\n{}".format(item.ref, status, item.url))
    if status == "Ready":
        print("advanced to Ready: {}".format(reason))
    else:
        print("held at Shaped: {}".format(reason))
    print("\n--- plan overlap candidates (advisory) ---")
    if overlaps:
        print("Read each candidate and record the conclusion in the plan:")
        for overlap in overlaps:
            print("  {}".format(overlap))
    else:
        print("  none found")
    if authority_signals:
        print("\n--- self-approval advisory ---")
        print("Authority signals are recorded in the Self-approved basis:")
        for signal in authority_signals:
            print("  {}: {}".format(
                signal, NEEDS_NATE_SIGNAL_REASONS[signal]
            ))
    if status != "Ready":
        print("\nIt now waits on you: is the plan good? Answer by moving it to Ready.")
    return 0


#: The gates, and the command that answers each. **These are Nate's answers to
#: his own questions — no agent may decide them.** An agent writing `Done`
#: accepts its own output. That is gate-jumping, which the funnel is arranged
#: to prevent. The funnel may write `Ready` only on its explicit fail-closed
#: plan condition; an agent may never bypass an open question.
ANSWERS = {
    "approve": ("Shaped", "Ready", "the plan is good"),
    "accept": ("Building", "Done", "shipped and accepted"),
}


SUB_ISSUES = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 50) {
        nodes { number title state createdAt repository { nameWithOwner } }
      }
    }
  }
}
"""


DRIFT_STATUS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      timelineItems(last: 100, itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT]) {
        nodes {
          __typename
          ... on ProjectV2ItemStatusChangedEvent {
            createdAt
            previousStatus
            status
            project { number }
          }
        }
      }
    }
  }
}
"""


DRIFT_EDIT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      userContentEdits(first: 100) {
        nodes { editedAt }
      }
    }
  }
}
"""


def _gh_json(*args: str):
    out = _run_gh(args, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def _issue_comments(item: Item) -> List[dict]:
    """Read one issue's comments, keeping a failed response distinguishable."""
    payload = _gh_json(
        "gh", "issue", "view", str(item.number), "--repo", item.repo,
        "--json", "comments",
    )
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if not isinstance(comments, list):
        raise GitHubError("could not read comments for {}".format(item.ref))
    return comments


def _project_status_times(item: Item) -> Dict[str, Optional[datetime]]:
    """Return the latest Ready and Building transitions for one Project item."""
    try:
        owner, name = item.repo.split("/", 1)
    except ValueError:
        raise GitHubError("invalid repository ref {}".format(item.repo))
    data = gh_graphql(
        DRIFT_STATUS_QUERY, owner=owner, name=name, number=item.number
    )
    issue = (data.get("repository") or {}).get("issue") if isinstance(data, dict) else None
    if issue is None:
        raise GitHubError("could not read Project history for {}".format(item.ref))
    nodes = ((issue.get("timelineItems") or {}).get("nodes") or [])
    found: Dict[str, List[datetime]] = {"Ready": [], "Building": []}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        project = node.get("project") or {}
        if project.get("number") != PROJECT_NUMBER:
            continue
        status = node.get("status")
        at = parse_time(node.get("createdAt"))
        if status in found and at is not None:
            found[status].append(at)
    return {
        status: max(times) if times else None
        for status, times in found.items()
    }


def _plan_edit_times(item: Item) -> Tuple[datetime, ...]:
    """Read issue body edits; title-only edits are not plan drift."""
    try:
        owner, name = item.repo.split("/", 1)
    except ValueError:
        raise GitHubError("invalid repository ref {}".format(item.repo))
    data = gh_graphql(
        DRIFT_EDIT_QUERY, owner=owner, name=name, number=item.number
    )
    issue = (data.get("repository") or {}).get("issue") if isinstance(data, dict) else None
    if issue is None:
        raise GitHubError("could not read issue edit history for {}".format(item.ref))
    edits: List[datetime] = []
    for edit in ((issue.get("userContentEdits") or {}).get("nodes") or []):
        if not isinstance(edit, dict):
            continue
        at = parse_time(edit.get("editedAt"))
        if at is not None:
            edits.append(at)
    return tuple(edits)


def _subissue_rows(item: Item) -> List[dict]:
    """Fetch the child issues, including creation times, for one project."""
    try:
        owner, name = item.repo.split("/", 1)
    except ValueError:
        raise GitHubError("invalid repository ref {}".format(item.repo))
    data = gh_graphql(SUB_ISSUES, owner=owner, name=name, number=item.number)
    issue = (data.get("repository") or {}).get("issue") if isinstance(data, dict) else None
    if issue is None:
        raise GitHubError("could not read tickets for {}".format(item.ref))
    nodes = (issue.get("subIssues") or {}).get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def _ticket_prs(repo: str, number: int) -> List[Tuple[str, int]]:
    """Return every PR ever made from a ticket's convention-named branch."""
    rows = _gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "all",
        "--head", "ticket/{}".format(number), "--json", "number",
    )
    if rows is None or not isinstance(rows, list):
        raise GitHubError("could not read PRs for {}#{}".format(repo, number))
    found: List[Tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pr_number = row.get("number")
        if isinstance(pr_number, int) and not isinstance(pr_number, bool):
            found.append((repo, pr_number))
    return found


def _review_verdicts(prs: Iterable[Tuple[str, int]]) -> Tuple[Dict[str, object], ...]:
    """Read every structured verdict comment on every ticket PR."""
    verdicts: List[Dict[str, object]] = []
    for repo, number in prs:
        payload = _gh_json(
            "gh", "pr", "view", str(number), "--repo", repo,
            "--json", "comments",
        )
        if payload is None or not isinstance(payload, dict):
            raise GitHubError("could not read review history for {} PR #{}".format(
                repo, number))
        for comment in payload.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            verdict = parse_verdict(comment.get("body") or "")
            if verdict is not None:
                verdicts.append(verdict)
    return tuple(verdicts)


def _regression_pr_numbers(prs: Iterable[Tuple[str, int]]) -> Tuple[int, ...]:
    """Match funnel-created regression records to the project's PRs."""
    project_prs = set(prs)
    if not project_prs:
        return ()
    rows = _gh_json(
        "gh", "issue", "list", "--repo", REPO, "--state", "all",
        "--search", "{} in:title".format(REGRESSION_PREFIX),
        "--limit", "100", "--json", "title,body",
    )
    if rows is None or not isinstance(rows, list):
        raise GitHubError("could not read regression records")

    found: List[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = row.get("title") or ""
        if not title.startswith(REGRESSION_PREFIX):
            continue
        match = re.match(
            r"{}(?P<number>[0-9]+)".format(re.escape(REGRESSION_PREFIX)),
            title,
        )
        if not match:
            continue
        pr_number = int(match.group("number"))
        body = row.get("body") or ""
        exact_url = any(
            "https://github.com/{}/pull/{}".format(repo, number) in body
            for repo, number in project_prs
        )
        central_pr = (REPO, pr_number) in project_prs
        if central_pr or exact_url:
            found.append(pr_number)
    return tuple(found)


def fetch_drift_facts(item: Item) -> DriftFacts:
    """Fetch the GitHub histories used by ``drift_since_approval``."""
    status_times = _project_status_times(item)
    child_rows = _subissue_rows(item)
    child_created: List[datetime] = []
    prs: List[Tuple[str, int]] = []
    for child in child_rows:
        created_at = parse_time(child.get("createdAt"))
        if created_at is not None:
            child_created.append(created_at)
        child_repo = (child.get("repository") or {}).get("nameWithOwner") or item.repo
        child_number = child.get("number")
        if isinstance(child_number, int) and not isinstance(child_number, bool):
            prs.extend(_ticket_prs(child_repo, child_number))

    return DriftFacts(
        ready_at=status_times["Ready"],
        plan_edit_times=_plan_edit_times(item),
        review_verdicts=_review_verdicts(prs),
        regression_pr_numbers=_regression_pr_numbers(prs),
        building_at=status_times["Building"],
        ticket_created_at=tuple(child_created),
    )


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


def _load_block_comment(item: Item) -> None:
    """Populate one open blocked item's parsed comment state.

    Block comments are intentionally loaded outside the Project query. The
    normal load pays for this only for open blocked items, and the parsed state
    stays on ``Item`` for pure queue functions and the brief to reuse.
    """
    payload = _gh_json(
        "gh", "issue", "view", str(item.number), "--repo", item.repo,
        "--json", "comments",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
        item.block_comments_error = "could not read comments"
        return
    bodies = [
        comment.get("body") or ""
        for comment in payload["comments"]
        if isinstance(comment, dict)
    ]
    item.unparseable_block_comments = unparseable_block_comment_lines(bodies)
    parsed = parse_block_comment(bodies)
    if parsed is not None:
        item.block_references, item.block_reason = parsed
    item.needs_decision = parse_needs_decision_comment(bodies)
    for body in reversed(bodies):
        record = parse_satisfied_block_comment(body)
        if record is not None:
            item.satisfied_block_record = record
            break


def _ticket_body(repo: str, number: int) -> str:
    """One issue body, fetched only for a candidate about to be handed out."""
    row = _gh_json("gh", "issue", "view", str(number), "--repo", repo,
                   "--json", "body") or {}
    return row.get("body") or ""


def ticket_pr_index(repo: str) -> Tuple[Dict[str, Dict], bool]:
    """Every `ticket/<n>` PR in one repo, indexed by ticket ref.

    One bounded ``gh pr list`` for the whole repository, so a caller pays once
    however many tickets it is about to ask about. Returns the index and whether
    the scan was truncated, because a truncated scan cannot tell "no PR" from
    "PR older than the window" and only the caller knows which answer is safe.
    """
    rows = _gh_json(
        "gh", "pr", "list", "--repo", repo, "--state", "all",
        "--json",
        "number,state,url,headRefName,headRefOid,mergeable,mergedAt,reviews",
        "--limit", str(MERGED_PR_SCAN_LIMIT + 1),
    )
    if rows is None:
        raise GitHubError("could not read PRs for {}".format(repo))
    if not isinstance(rows, list):
        raise GitHubError("invalid PR response for {}".format(repo))

    truncated = len(rows) > MERGED_PR_SCAN_LIMIT
    index: Dict[str, Dict] = {}
    for row in rows[:MERGED_PR_SCAN_LIMIT]:
        if not isinstance(row, dict):
            continue
        ref = ticket_ref_from_branch(repo, row.get("headRefName") or "")
        # `gh pr list` returns newest first, so the first row for a branch is
        # the one the old per-ticket lookup's `rows[0]` used to return.
        if ref and ref not in index:
            index[ref] = row
    return index, truncated


def ticket_branch_index(repo: str) -> Tuple[Set[str], bool]:
    """Remote ``ticket/<n>`` branches from one bounded repository scan."""
    rows = _gh_json(
        "gh", "api",
        "repos/{}/git/matching-refs/heads/ticket?per_page={}".format(
            repo, MERGED_PR_SCAN_LIMIT
        ),
    )
    if rows is None:
        raise GitHubError("could not read ticket branches for {}".format(repo))
    if not isinstance(rows, list):
        raise GitHubError("invalid ticket branch response for {}".format(repo))

    # The endpoint caps a page at 100. At the cap, absent refs are unknown and
    # must preserve their claims; positively returned refs remain safe to use.
    truncated = len(rows) >= MERGED_PR_SCAN_LIMIT
    refs: Set[str] = set()
    for row in rows[:MERGED_PR_SCAN_LIMIT]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("ref") or "")
        if not name.startswith("refs/heads/"):
            continue
        ref = ticket_ref_from_branch(repo, name[len("refs/heads/"):])
        if ref:
            refs.add(ref)
    return refs, truncated


def ticket_pr_facts(
    items: Sequence[Item],
) -> Dict[str, Optional[Dict[str, object]]]:
    """Read PR facts needed by the brief's stranded-work diagnostics.

    Two bounded reads per member repository — one PR list and one remote ticket
    branch list — never one lookup per ticket. The per-ticket PR form was the
    single largest GraphQL consumer in the system: 68 requests on the board of
    2026-09-08, 93 of a full brief's 110 points, and it grew with the board
    (#272).

    An explicit ``None`` means both scans established no PR and no branch. A
    dict carries PR data when present plus ``branch_exists``; a branch without
    a PR is a minimal dict. A missing PR in a truncated PR scan is represented
    only by its branch fact, while an absent key means branch presence itself
    was not established. That distinction keeps a scan blind spot from becoming
    either a false stranded diagnostic or an unsafe takeover.
    """
    wanted = {
        item.ref: item for item in items
        if item.parent or (item.state == "OPEN" and item.in_motion_since is not None)
    }
    facts: Dict[str, Optional[Dict[str, object]]] = {}

    for repo in sorted({item.repo for item in wanted.values()}):
        index, truncated = ticket_pr_index(repo)
        branch_refs, branches_truncated = ticket_branch_index(repo)
        for ref, item in wanted.items():
            if item.repo != repo:
                continue
            fact: Optional[Dict[str, object]]
            if ref in index:
                fact = dict(index[ref])
                if (
                    item.state == "OPEN"
                    and str(fact.get("state") or "").upper() == "OPEN"
                ):
                    if str(fact.get("mergeable") or "").upper() == "CONFLICTING":
                        # Kept conditional: a verdict lookup per ticket would
                        # undo the saving this scan exists for.
                        fact["verdict"] = latest_verdict(
                            item.repo, fact.get("number")
                        )
            elif not truncated:
                fact = None
            else:
                # PR absence is unknown beyond the bounded history, but a
                # complete branch scan can still establish branch absence.
                fact = {}

            if ref in branch_refs:
                if fact is None:
                    fact = {"headRefName": "ticket/{}".format(item.number)}
                fact["branch_exists"] = True
                facts[ref] = fact
            elif not branches_truncated:
                if fact is not None:
                    fact["branch_exists"] = False
                facts[ref] = fact
            elif fact:
                # Preserve useful PR diagnostics while explicitly withholding
                # the branch-absence conclusion from stale-lock detection.
                fact["branch_exists"] = None
                facts[ref] = fact

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
                        "--json", "number,headRefName,headRefOid,createdAt",
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
            if verdict_covers_head(verdict, row.get("headRefOid")):
                continue  # this exact diff has already been judged
            needed = required_tier(
                ticket.title, _ticket_body(repo, ticket.number))
            if tier and needed != tier:
                continue
            found.append({"pr": row.get("number"), "repo": repo, "ref": ref,
                          "tier": needed, "url": ticket.url,
                          "title": ticket.title,
                          "opened": row.get("createdAt") or ""})
    # Oldest first. `gh pr list` returns newest first, and handing a reviewer
    # `queue[0]` from that order starved the oldest PR indefinitely: on
    # 2026-09-09 four PRs opened before 11:00 were still unreviewed at 15:17
    # while every newer one merged, and the prerequisite one of them held fed
    # the decline-and-leak loop in #394. Oldest-at-gate is the tiebreak in
    # every other queue here (plan.md); a review is a gate too (#320).
    found.sort(key=lambda entry: (entry.get("opened") or "", entry.get("pr") or 0))
    return found


def shapeable_idea(items: Sequence[Item], tier: Optional[str],
                   reading: Dict) -> Optional[Item]:
    """Return the first idea this run may shape, or ``None``.

    Shaping starts new work, so it is the last optional job after review and
    breakdown. The ordering itself stays in ``ideas()``; this function only
    filters that shared order through the existing tier and headroom rules.
    """
    import usage

    if not usage.shaping_allowed(reading):
        return None

    for item in ideas(items):
        body = getattr(item, "body", None)
        if body is None:
            body = _ticket_body(item.repo, item.number)
        if tier is not None and required_tier(item.title, body) != tier:
            continue
        return item
    return None


def approved_merge_candidates(items: Sequence[Item]) -> List[Dict[str, object]]:
    """Find open ticket PRs whose latest verdict approves their current head."""
    tickets = {
        item.ref: item
        for item in items
        if (getattr(item, "state", "OPEN") or "OPEN").upper() == "OPEN"
    }
    if not tickets:
        return []

    candidates: List[Dict[str, object]] = []
    for repo in sorted({item.repo for item in tickets.values()}):
        rows = _gh_json(
            "gh", "pr", "list", "--repo", repo, "--state", "open",
            "--json", "number,headRefName,headRefOid", "--limit", "100",
        ) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            branch = row.get("headRefName") or ""
            ref = ticket_ref_from_branch(repo, branch)
            if ref is None or ref not in tickets or row.get("number") is None:
                continue
            verdict = latest_verdict(repo, row.get("number"))
            # The current-head proof is deliberately strict here. The stranded
            # diagnostic accepts fixture rows without SHAs, but reconciliation
            # must never turn missing evidence into an unattended merge.
            if not row.get("headRefOid") or not isinstance(verdict, dict):
                continue
            if not verdict.get("head_sha"):
                continue
            probe = dict(row)
            probe["verdict"] = verdict
            if not _approved_current_head(probe):
                continue
            candidates.append({"repo": repo, "pr": row["number"], "ref": ref})

    candidates.sort(key=lambda candidate: (
        candidate["repo"], str(candidate["pr"])
    ))
    return candidates


def reconcile_orphaned_starts(
    items: Sequence[Item], now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Close a work-issuing start whose ticket PR merged under another run (#497).

    The run that opened a PR sometimes never records its finish -- a session
    that lost its id, a rate-limit death after the push -- while a reviewer
    run merges the PR and records `merged` on its own finish. The start then
    stands open until the watchdog calls it dead, for work that demonstrably
    shipped. This writes the missing finish, naming the run that merged and
    carrying no `merged` field of its own, so the brief's unattended-merge
    record stays exactly once. A start whose PR has not merged, or whose merge
    no finish record claims, is left alone: the dead-run signal catches
    rate-limit deaths and must stay loud. Idempotent, because a closed start
    is no longer open.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat
    except Exception:
        return []
    by_ref = {item.ref: item for item in items}
    spools: Dict[str, List[Dict]] = {}
    for agent in sorted(heartbeat.PROVIDERS):
        if agent in heartbeat.RETIRED_AGENTS:
            continue
        try:
            spools[agent] = heartbeat.read(agent)
        except Exception:
            continue

    # Which run recorded each merged PR, across every live agent.
    merged_by: Dict[int, Tuple[str, str]] = {}
    for agent, records in spools.items():
        for row in records:
            if row.get("phase") == "finish" and row.get("merged") and row.get("run"):
                try:
                    merged_by[int(row["merged"])] = (str(row["run"]), agent)
                except (TypeError, ValueError):
                    continue

    candidates = []
    for agent, records in spools.items():
        bound = heartbeat.bindings(records)
        for start in heartbeat.open_starts(records):
            binding = bound.get(start.get("run"))
            if not binding or binding.get("do") != "ticket":
                continue
            ref = str(binding.get("work"))
            if ref in by_ref:
                candidates.append((agent, start, ref))
    if not candidates:
        return []
    if pr_facts is None:
        try:
            pr_facts = ticket_pr_facts([by_ref[ref] for _, _, ref in candidates])
        except GitHubError:
            return []

    closed: List[Dict[str, object]] = []
    for agent, start, ref in candidates:
        fact = pr_facts.get(ref) or {}
        merged = bool(fact.get("mergedAt")) or fact.get("state") == "MERGED"
        number = fact.get("number")
        if not merged or number is None:
            continue
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        under = merged_by.get(number)
        if under is None:
            continue
        other_run, other_agent = under
        if other_run == start.get("run"):
            continue
        record = {
            "run": start.get("run"),
            "agent": agent,
            "phase": "finish",
            "ts": int(now.timestamp()),
            "outcome": "done",
            "note": "reconciled: PR #{} for {} merged under run {} ({}); "
                    "this start never recorded its finish".format(
                        number, ref, other_run, other_agent),
            "reconciled_from": other_run,
        }
        try:
            kept = heartbeat.append(agent, record)
        except Exception:
            continue
        closed.append({
            "run": start.get("run"), "agent": agent, "ref": ref,
            "pr": number, "merged_under": other_run, "kept": kept,
        })
    return closed


def reconcile_approved_merges(
    items: List[Item], now: datetime
) -> List[Dict[str, object]]:
    """Retry the merge gate for every approved current-head ticket PR.

    ``cmd_merge`` owns all merge conditions and the actual writes. This wrapper
    only finds the half-applied approval sequence, keeps its human-readable
    output on stderr so ``begin`` remains JSON, and records the result for the
    caller. A refusal is a normal reconciliation result, not a queue failure.
    """
    results: List[Dict[str, object]] = []
    by_ref = {item.ref: item for item in items}
    for candidate in approved_merge_candidates(items):
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cmd_merge(
                    items,
                    now,
                    candidate["repo"],
                    candidate["pr"],
                    True,
                )
        except GitHubError as exc:
            code = None
            error = str(exc)
        else:
            error = None

        if stdout.getvalue():
            print(stdout.getvalue(), end="", file=sys.stderr)
        if stderr.getvalue():
            print(stderr.getvalue(), end="", file=sys.stderr)

        result: Dict[str, object] = dict(candidate)
        if code == 0:
            result["result"] = "merged"
            ticket = by_ref.get(candidate["ref"])
            if ticket is not None:
                # ``load_items`` ran before the merge. Keep this process's
                # queue consistent with the GitHub close that just happened.
                ticket.state = "CLOSED"
                ticket.state_reason = "COMPLETED"
                ticket.in_motion_since = None
        elif code == 1:
            result["result"] = "refused"
            reasons = [
                line[4:]
                for line in stderr.getvalue().splitlines()
                if line.startswith("  - ")
            ]
            if reasons:
                result["reasons"] = reasons
        else:
            result["result"] = "error"
            result["error"] = error or "merge gate failed without a result"
        results.append(result)
    return results


def cmd_begin(items: List[Item], now: datetime, agent: str, tier: Optional[str],
              idle: bool, breakdown: bool = False,
              routine_sha_literal: Optional[str] = None,
              repo_readiness: Optional[
                  Mapping[str, MemberRepoReadiness]
              ] = None) -> int:
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
    run = _run_bounded_subprocess(
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

    reconciled = reconcile_approved_merges(items, now)
    if reconciled:
        out["reconciled"] = reconciled

    auto_closed = reconcile_auto_closeable_projects(items)
    if auto_closed:
        out["auto_closed"] = auto_closed

    reconciled = reconcile_closed_items(items)
    if reconciled:
        out["reconciled"] = reconciled
    orphaned = reconcile_orphaned_starts(items, now)
    if orphaned:
        out["reconciled_starts"] = orphaned

    if agent == "codex":
        cleared = clear_satisfied_blocks(
            items, now, run=out.get("run"), agent=agent
        )
        if cleared:
            out["cleared_blocks"] = cleared
        try:
            pr_facts = ticket_pr_facts(items)
        except GitHubError as exc:
            out.update(
                do="stop",
                why="could not establish ticket branch facts: {}".format(exc),
            )
            print(json.dumps(out, indent=2))
            return 0
        blocked = awaiting_review(items)
        # Keep the normal open-PR exclusion as the default. Only the
        # machine-readable approved-plus-conflicting state hands ownership back
        # to the engineer; the supplied PR snapshot is also the one used by the
        # claim/WIP checks below.
        blocked.difference_update(approved_conflicting_refs(pr_facts))
        blocked.update(finished_by_comments(items))
        ticket = next_ticket_for_tier(
            items, now, tier=tier, blocked=blocked,
            agent=agent,
            repo_readiness=repo_readiness,
            pr_facts=pr_facts,
        )
        if ticket is None:
            holder = lock_holder(items, now, pr_facts=pr_facts)
            withheld = readiness_blockers(
                items, repo_readiness=repo_readiness, awaiting_review=blocked
            )
            if withheld:
                out["withheld"] = withheld
            if holder is not None:
                why = "nothing — lock held by {} (claimed {} ago)".format(
                    holder.ref, humanise(now - holder.in_motion_since)
                )
            elif withheld:
                why = "nothing — {}".format(
                    _readiness_blocker_summary(withheld)
                )
            elif tier:
                why = "nothing — no {} work waiting".format(tier)
            else:
                why = "nothing to do"
            out.update(do="stop", why=why)
        else:
            refusal = claim_ticket(items, now, ticket, pr_facts=pr_facts)
            if refusal is not None:
                out.update(do="stop", why=refusal)
            else:
                out.update(
                    do="ticket",
                    work=item_json(ticket, now, {i.ref: i for i in items}),
                )
        _bind_run(agent, out)
        print(json.dumps(out, indent=2))
        return 0

    queue = review_queue(items, tier)
    review = queue[0] if queue else None

    # The fixed job order remains the tiebreak within a class group, but a
    # finite preempting class can cross stages. Build only the head of each
    # queue: the next run gets the next item if this run preempts it.
    by_ref = {item.ref: item for item in items}
    pending = awaiting_breakdown(items) if breakdown else []
    breakdown_item = pending[0] if pending else None
    shape_item = shapeable_idea(items, tier, reading)
    candidates: List[Tuple[int, int, str, object]] = []

    if review is not None:
        review_item = by_ref.get(review.get("ref"))
        review_class = (
            effective_class(review_item, by_ref)
            if review_item is not None else None
        )
        candidates.append((
            0 if review_class in PREEMPTING_CLASSES else 1,
            0,
            "review",
            review,
        ))
    if breakdown_item is not None:
        breakdown_class = getattr(breakdown_item, "klass", None)
        candidates.append((
            0 if breakdown_class in PREEMPTING_CLASSES else 1,
            1,
            "breakdown",
            breakdown_item,
        ))
    if shape_item is not None:
        shape_class = getattr(shape_item, "klass", None)
        candidates.append((
            0 if shape_class in PREEMPTING_CLASSES else 1,
            2,
            "shape",
            shape_item,
        ))

    if candidates:
        _, _, job, payload = min(candidates, key=lambda candidate: candidate[:2])
        if job == "review":
            out.update(do="review", work=payload)
        elif job == "breakdown":
            item = payload
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
            item = payload
            out.update(
                do="shape",
                work={"ref": item.ref, "url": item.url,
                      "title": item.title},
            )
    else:
        out.update(
            do="stop",
            why=("nothing to review and nothing to break down"
                 if breakdown else "nothing to review"),
        )

    reserve = _reserve_verdict(out.get("do"))
    if reserve is not None:
        out.update(reserve)
        heartbeat.record_event(agent, out["run"], "skipped-api-reserve",
                               note=out["why"])
    _bind_run(agent, out)
    print(json.dumps(out, indent=2))
    return 0


def _bind_run(agent: str, out: Dict[str, object]) -> None:
    """Bind the work this run was issued to its start record (#497).

    A finish that names other work is then refused by the heartbeat rather
    than filed under a run that never issued it. The binding is also printed
    under ``bound`` so the routine can hand it back on ``finish --work``.
    Bookkeeping never stops the run: a binding that cannot be written is
    reported by the heartbeat and the run proceeds with the work.
    """
    do = out.get("do")
    work = out.get("work")
    run = out.get("run")
    if do not in ("ticket", "review", "breakdown", "shape") or not run:
        return
    if not isinstance(work, dict):
        return
    subject = work.get("pr") if do == "review" else work.get("ref")
    if subject is None:
        return
    out["bound"] = {"do": do, "work": str(subject)}
    try:
        import heartbeat

        heartbeat.record_binding(agent, str(run), str(do), str(subject))
    except Exception:
        # Instrumentation must not gate the thing it instruments.
        pass


def _reserve_verdict(do: object) -> Optional[Dict[str, object]]:
    """Decline this run if the GraphQL budget is below its tier's floor.

    Returns ``None`` to proceed. A run that was already stopping is left alone:
    relabelling "nothing to review" as a budget decline would report a refusal
    the system never had to make.

    ``review`` takes the lower floor; ``breakdown`` and ``shape`` take the
    engineering one. The split is consumers versus drainers — both of the
    latter open new work, while review drains the lane and is cheap. Keyed on
    what the run would do rather than on which agent asked, so there is no list
    of agent names to keep in step.

    Coast to a stop rather than seize. Retry was rejected as the mechanism —
    waiting out a reset that can be fifty minutes away either sleeps through the
    run's own schedule or fails anyway, and spends the run's credits sitting
    still. Declining cleanly *is* the backoff; the schedule supplies it.
    """
    if do not in ("review", "breakdown", "shape"):
        return None

    spend = graphql_spend()
    remaining = spend.get("remaining")
    load_cost = spend.get("cost") or 0

    if not spend.get("calls"):
        # No GraphQL call was made, so there is no budget question to answer —
        # distinct from a call whose rate-limit block was unreadable. In a real
        # run this cannot happen: `load_items` queries before `begin` is
        # dispatched, and an unreachable GitHub raises there first. Failing
        # closed here would refuse on the absence of a question rather than on
        # the absence of an answer.
        return None
    if remaining is None:
        # A call was made and its block could not be read. Fail closed,
        # matching the unreadable-usage branch above.
        return {"gate": "reserve", "do": "stop",
                "why": "GraphQL budget could not be read; a run that cannot "
                       "read its budget does not work"}

    loads = (REVIEW_RESERVE_LOADS if do == "review"
             else ENGINEERING_RESERVE_LOADS)
    floor = loads * int(load_cost)
    if int(remaining) < floor:
        return {"gate": "reserve", "do": "stop",
                "why": "GraphQL budget {} is below the {} floor of {} "
                       "({} loads at {} points)".format(
                           remaining, "review" if do == "review"
                           else "engineering", floor, loads, load_cost)}
    return None


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

    return _write_verdict(
        repo, pr, sha, verdict, ci, blocking, note, run=run, agent=agent
    )


def _write_verdict(repo: str, pr: int, sha: str, verdict: str, ci: str,
                   blocking: List[str], note: Optional[str],
                   run: Optional[str] = None,
                   agent: Optional[str] = None) -> int:
    """Write the one structured verdict artifact shared by models and gates."""

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

    out = _run_gh(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", comment],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    print("recorded {} on PR #{} against {} in {}".format(
        verdict, pr, sha[:12], repo))
    return 0


def _conflicting_branch_blocker(data: Dict) -> Optional[str]:
    """The mechanical conflict reason, or None for every other branch state."""
    if str(data.get("mergeable") or "").upper() != "CONFLICTING":
        return None
    return "branch {!r}{}".format(
        data.get("headRefName") or "", CONFLICTING_BRANCH_SUFFIX
    )


def _is_conflicting_branch_blocker(reason: str) -> bool:
    return reason.startswith("branch ") and reason.endswith(
        CONFLICTING_BRANCH_SUFFIX
    )


def _record_unmergeable_rejection(repo: str, pr: int) -> None:
    """Reject an approved current head that the live merge gate finds conflicting.

    Re-reading the head and mergeability keeps the verdict tied to the fact the
    gate actually observed. Requiring an approval at that exact SHA excludes the
    two self-resolving refusal shapes: no verdict and a verdict for an older head.
    It also makes retries idempotent, because the newest verdict is then already
    the gate-authored rejection rather than an approval.
    """
    data = _gh_json(
        "gh", "pr", "view", str(pr), "--repo", repo, "--json",
        "state,headRefName,headRefOid,mergeable",
    ) or {}
    if data.get("state") != "OPEN":
        return
    sha = data.get("headRefOid")
    reason = _conflicting_branch_blocker(data)
    if not sha or reason is None:
        return

    verdict = latest_verdict(repo, pr)
    if (
        verdict is None
        or verdict.get("verdict") != "approved"
        or verdict.get("head_sha") != sha
    ):
        return

    _write_verdict(
        repo, pr, sha, "rejected", "unknown",
        [UNMERGEABLE_REJECTION_BLOCKING], None,
        agent=MERGE_GATE_AGENT,
    )


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


def closed_itself_comment(tickets: Sequence[Item], drift: Sequence[str]) -> str:
    """Render the durable marker comment for a funnel-closed project."""
    payload = {
        "drift": list(drift),
        "tickets": [
            {"ref": ticket.ref, "title": ticket.title}
            for ticket in tickets
        ],
    }
    return CLOSED_ITSELF_PREFIX + "\n```json\n{}\n```".format(
        json.dumps(payload, indent=2, sort_keys=True)
    )


def _auto_closeable_project(item: Item, *, children_done: Optional[int] = None
                            ) -> bool:
    """Whether a project has earned the funnel's unattended close.

    ``funnel merge`` sees the Project summary before GitHub closes the ticket,
    so it supplies the post-merge child count. Every other caller uses the
    count already loaded on the project.
    """
    completed = item.children_done if children_done is None else children_done
    return (
        item.parent is None
        and item.state == "OPEN"
        and item.status == "Building"
        and item.klass in SELF_APPROVABLE_CLASSES
        and item.children_total > 0
        and completed == item.children_total
        and not item.carried_human_step
    )


def _close_auto_closeable_project(items: Sequence[Item], project: Item,
                                  *, children_done: Optional[int] = None
                                  ) -> bool:
    """Move one eligible project to Done, close it, and record its marker."""
    if not _auto_closeable_project(project, children_done=children_done):
        return False
    if not project.item_id:
        raise GitHubError(
            "{} is not in the Project; cannot auto-close it".format(project.ref)
        )

    drift = drift_since_approval(project)
    tickets = [item for item in items if item.parent == project.ref]
    tickets.sort(key=lambda item: (item.repo, item.number))

    gh_graphql(
        SET_FIELD,
        project=PROJECT_ID,
        item=project.item_id,
        field=STATUS_FIELD_ID,
        option=_option_id(STATUS_FIELD_ID, "Done"),
    )

    close = _run_gh(
        ["gh", "issue", "close", str(project.number), "--repo", project.repo,
         "--reason", "completed"],
        capture_output=True, text=True,
    )
    if close.returncode != 0:
        raise GitHubError(
            "could not auto-close {}: {}".format(
                project.ref, close.stderr.strip()
            )
        )

    comment = _run_gh(
        ["gh", "issue", "comment", str(project.number), "--repo", project.repo,
         "--body", closed_itself_comment(tickets, drift)],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(
            "{} was moved to Done and closed, but its closing comment could not "
            "be recorded: {}".format(project.ref, comment.stderr.strip())
        )

    # Keep fixture and same-process callers in sync with the writes. A fresh
    # `begin` reloads these facts from GitHub, where the closed state is the
    # idempotence guard.
    project.status = "Done"
    project.state = "CLOSED"
    project.state_reason = "COMPLETED"
    print("auto-closed {}".format(project.ref), file=sys.stderr)
    return True


def reconcile_auto_closeable_projects(items: Sequence[Item]) -> List[str]:
    """Close every already-finished upkeep project before queue selection."""
    closed: List[str] = []
    projects = sorted(
        (item for item in items if _auto_closeable_project(item)),
        key=lambda item: (item.repo, item.number),
    )
    for project in projects:
        if _close_auto_closeable_project(items, project):
            closed.append(project.ref)
    return closed


def reconcile_closed_items(items: Sequence[Item]) -> List[str]:
    """Repair terminal Project state and stale shaping labels on closed items.

    Closing an issue outside the funnel does not update its Project item. The
    issue's state reason is the authoritative terminal choice, and a closed
    item cannot still be waiting in Ideas. Only closed items participate so an
    open issue is never changed by this unattended repair.
    """
    repaired: List[str] = []
    terminal_statuses = {
        "COMPLETED": "Done",
        "NOT_PLANNED": "Parked",
    }
    candidates = sorted(
        (item for item in items if item.state == "CLOSED"),
        key=lambda item: (item.repo, item.number),
    )
    for item in candidates:
        reason = str(item.state_reason or "").upper().replace("-", "_")
        target = terminal_statuses.get(reason)
        changed = False

        if target is not None and not item.parent and item.status != target:
            if not item.item_id:
                raise GitHubError(
                    "{} is not in the Project; cannot set Status to {}".format(
                        item.ref, target
                    )
                )
            gh_graphql(
                SET_FIELD,
                project=PROJECT_ID,
                item=item.item_id,
                field=STATUS_FIELD_ID,
                option=_option_id(STATUS_FIELD_ID, target),
            )
            item.status = target
            changed = True

        if "needs-shaping" in item.labels and item.status != "Ideas":
            edit = _run_gh(
                [
                    "gh", "issue", "edit", str(item.number), "--repo", item.repo,
                    "--remove-label", "needs-shaping",
                ],
                capture_output=True, text=True,
            )
            if edit.returncode != 0:
                raise GitHubError(
                    "reconciled {} but could not remove its needs-shaping "
                    "label: {}".format(item.ref, edit.stderr.strip())
                )
            item.labels = [
                label for label in item.labels if label != "needs-shaping"
            ]
            changed = True

        if changed:
            repaired.append(item.ref)
    return repaired


def _auto_close_parent(items: Sequence[Item], ticket: Item) -> bool:
    """Close a finished upkeep project after its last ticket merge.

    The Project summary is read before the merge, so exactly one open child is
    the evidence that this merge was the last open ticket. The project-level
    eligibility decision is shared with the begin reconcile.
    """
    parent = next((item for item in items if item.ref == ticket.parent), None)
    if parent is None:
        return False
    if (
        ticket.state != "OPEN"
        or parent.state != "OPEN"
        or parent.status != "Building"
        or parent.children_total <= 0
        or parent.children_done != parent.children_total - 1
    ):
        return False
    merge_items = list(items)
    if not any(item.ref == ticket.ref for item in merge_items):
        merge_items.append(ticket)
    return _close_auto_closeable_project(
        merge_items, parent, children_done=parent.children_total
    )


def merged_pr_facts(items: Sequence[Item]) -> MergedPRFacts:
    """Find open tickets whose convention-named PR has already merged.

    The scan is one `gh pr list` request per member repository, independent of
    the number of tickets. It asks for one row beyond
    ``MERGED_PR_SCAN_LIMIT`` so a bounded result can report that it was
    truncated. Only the branch name is needed; the pure detector receives the
    intersected ticket refs rather than querying GitHub itself.
    """
    open_ticket_refs = {
        item.ref for item in items
        if item.state == "OPEN" and item.parent
    }
    repos = sorted({item.repo for item in items if item.ref in open_ticket_refs})
    merged_ticket_refs: Set[str] = set()
    truncated = False

    for repo in repos:
        rows = _gh_json(
            "gh", "pr", "list", "--repo", repo, "--state", "merged",
            "--json", "headRefName", "--limit", str(MERGED_PR_SCAN_LIMIT + 1),
        )
        if rows is None:
            raise GitHubError("could not read merged PRs for {}".format(repo))
        if not isinstance(rows, list):
            raise GitHubError("invalid merged PR response for {}".format(repo))

        if len(rows) > MERGED_PR_SCAN_LIMIT:
            truncated = True
        for row in rows[:MERGED_PR_SCAN_LIMIT]:
            if not isinstance(row, dict):
                continue
            ref = ticket_ref_from_branch(repo, row.get("headRefName") or "")
            if ref in open_ticket_refs:
                merged_ticket_refs.add(ref)

    return MergedPRFacts(frozenset(merged_ticket_refs), truncated)


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
    conflict = _conflicting_branch_blocker(data)
    if conflict is not None:
        why.append(conflict)
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
        if any(_is_conflicting_branch_blocker(reason) for reason in why):
            _record_unmergeable_rejection(repo, pr)
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

    out = _run_gh(
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
    if str(state).upper() != "CLOSED":
        closed = _run_gh(
            ["gh", "issue", "close", number, "--repo", repo,
             "--reason", "completed"],
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

    ticket = next((item for item in items if item.ref == ref), None)
    if ticket is not None:
        _auto_close_parent(items, ticket)
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
    question = gate_question(item)
    print("GATE: {}".format(question or "not waiting on you"))
    if item.needs_decision is not None:
        print("NEEDS DECISION: {}".format(item.needs_decision))
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
        # One scan per repo rather than one lookup per child: a project with
        # twenty tickets cost twenty requests here.
        indexes: Dict[str, Dict[str, Dict]] = {}
        for child in children:
            mark = "x" if child["state"] == "CLOSED" else " "
            print("  [{}] #{} {}".format(mark, child["number"], child["title"]))
            child_repo = child["repository"]["nameWithOwner"]
            if child_repo not in indexes:
                indexes[child_repo] = ticket_pr_index(child_repo)[0]
            pr = indexes[child_repo].get(
                "{}#{}".format(child_repo, child["number"]))
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
    if verb and question:
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

    drift = []
    if verb == "accept" and gate_question(item) == GATES["Building"]:
        drift = drift_since_approval(item)

    if drift:
        print("drift since approval:")
        for signal in drift:
            print("  {}".format(signal))

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
        _run_gh(
            ["gh", "issue", "close", str(item.number), "--repo", item.repo,
             "--reason", "completed"], capture_output=True)

    print("{} → {}  ({})".format(item.ref, nxt, meaning))
    if verb == "approve":
        print("The next Claude run will break it into tickets.")
    elif verb == "accept":
        print_parking_prompt(items, now)
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


def _comment_reason(value: str) -> str:
    """Reject blank reasons for canonical blocked comments during parsing."""
    reason = value.strip()
    if not reason:
        raise argparse.ArgumentTypeError("a non-empty block reason is required")
    return reason


def _decision_question(value: str) -> str:
    """Reject blank questions before loading GitHub."""
    question = value.strip()
    if not question:
        raise argparse.ArgumentTypeError(
            "a non-empty decision question is required"
        )
    return question


def _blocked_reference(value: str) -> str:
    """Normalise one issue number for the strict block-comment header."""
    reference = value.strip()
    if reference.startswith("#"):
        reference = reference[1:]
    if not reference.isdigit() or int(reference) < 1:
        raise argparse.ArgumentTypeError(
            "a positive issue number is required for --blocked-on"
        )
    return reference


def _blocked_comment_body(blocked_on: Sequence[str], because: str) -> str:
    """Render the block-comment header owned by ``BLOCK_COMMENT_RE``."""
    references = " and ".join("#{}".format(number) for number in blocked_on)
    return "**Blocked on {}:** {}".format(references, because)


def _needs_decision_comment_body(question: str) -> str:
    """Render the breakdown-question header owned by its parser."""
    return "{} {}".format(NEEDS_DECISION_PREFIX, question)


def main(argv: Optional[Sequence[str]] = None, *,
         _items: Optional[List[Item]] = None,
         _items_loader: Optional[Callable[[], List[Item]]] = None,
         _reset_api_usage: bool = True) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("queue", help="everything, ordered")
    nxt = sub.add_parser(
        "next", help="the single next ticket this agent should work, or nothing")
    nxt.add_argument(
        "--tier", choices=TIERS, default=None,
        help="what this engine is allowed to work. `standard` skips tickets "
             "needing the escalated engine; `escalated` may take anything. "
             "Declared by the routine, never by the model.")
    nxt.add_argument(
        "--agent", default="codex",
        help="agent whose capabilities filter the queue (default: codex)",
    )
    nxt.add_argument(
        "--not", dest="excluded", action="append", default=[], metavar="<ref>",
        help="exclude a candidate for this call; repeat for multiple refs",
    )
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
    capture.add_argument(
        "--origin", required=True, choices=ORIGIN_VOICES,
        help="idea origin: nate-relayed if Nate raised it, agent if observed",
    )
    capture.add_argument(
        "--class", dest="klass", choices=LADDER, default=None,
        help="ladder class; required when --origin agent",
    )
    shaped = sub.add_parser("shaped", help="record a grilled plan and move to Shaped")
    shaped.add_argument("ref", help="issue number, owner/repo#number, or URL")
    shaped.add_argument("--plan", required=True,
                        help="file holding the plan, or - to read it from stdin")
    shaped.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    shaped.add_argument(
        "--agent", default=None,
        help="agent that wrote the body; otherwise read the heartbeat spool",
    )
    shaped.add_argument(
        "--class", dest="klass", choices=LADDER, default=None,
        help="fill an unset Class on an agent-origin idea",
    )
    claim = sub.add_parser("claim", help="take the single-in-motion lock on a ticket")
    claim.add_argument("ref", help="issue number, owner/repo#number, or URL")
    release = sub.add_parser("release", help="give up the lock on a ticket")
    release.add_argument("ref", help="issue number, owner/repo#number, or URL")
    for verb, help_text in (
        ("pin", "pin a project within its current gate"),
        ("unpin", "clear a project's pin"),
    ):
        pin = sub.add_parser(verb, help=help_text)
        pin.add_argument("ref", help="issue number, owner/repo#number, or URL")
        pin.add_argument(
            "--yes", action="store_true", dest="confirmed",
            help="actually do it; without this the command is a dry run",
        )
        pin.add_argument(
            "--run", default=None,
            help="heartbeat run id; otherwise infer a unique open local start",
        )
        pin.add_argument(
            "--agent", default=None,
            help="agent that wrote the comment; otherwise read the heartbeat spool",
        )
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
    comment_body = comment.add_mutually_exclusive_group(required=True)
    comment_body.add_argument("--body", help="comment text")
    comment_body.add_argument("--body-file", help="file containing the comment text")
    comment_body.add_argument(
        "--blocked-on", action="append", type=_blocked_reference, metavar="N",
        help="post a canonical block header; repeat for multiple issue numbers",
    )
    comment_body.add_argument(
        "--needs-decision", type=_decision_question, metavar="QUESTION",
        help="post a breakdown question and apply the blocked label",
    )
    comment.add_argument(
        "--because", type=_comment_reason,
        help="reason appended to a canonical block header (requires --blocked-on)",
    )
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

    session_server = sub.add_parser(
        "session-server",
        help="serve one in-memory Project view for a single Muse run",
    )
    session_server.add_argument(
        "--parent-pid", type=int, default=None,
        help="stop when the owning runner process exits",
    )

    nxr = sub.add_parser(
        "next-review", help="the single PR this reviewer should read, or nothing")
    nxr.add_argument("--tier", choices=TIERS, default=None,
                     help="review only work of this risk tier. Declared by the "
                          "routine, the same way an engine declares its own.")
    reject.add_argument("--note", default=None, help="what is broken")
    args = parser.parse_args(argv)

    if args.command == "comment":
        if args.blocked_on and args.because is None:
            parser.error("--because is required with --blocked-on")
        if args.because is not None and not args.blocked_on:
            parser.error("--because requires --blocked-on")
    if (args.command == "capture" and args.origin == "agent"
            and args.klass is None):
        parser.error("--class is required with --origin agent")

    global _ACTIVE_HEARTBEAT_RUN, _ACTIVE_HEARTBEAT_AGENT
    _ACTIVE_HEARTBEAT_RUN = getattr(args, "run", None)
    _ACTIVE_HEARTBEAT_AGENT = getattr(args, "agent", None)

    # A process normally serves one CLI command. Resetting here also keeps
    # repeated `main()` calls in tests from blending two commands' readings.
    if _reset_api_usage:
        reset_api_usage()
    now = datetime.now(timezone.utc)
    if args.command == "session-server":
        return serve_session(args.parent_pid)
    # Doctor keeps its fixed checks runnable when the Project cannot be loaded;
    # the data-dependent consistency check is added when that read succeeds.
    if args.command == "doctor":
        return cmd_doctor()

    try:
        if _items is not None:
            items = _items
        elif _items_loader is not None:
            items = _items_loader()
        else:
            items = load_items()
    except GitHubError as exc:
        if args.command == "brief":
            print(json.dumps({
                "generated_at": now.isoformat(),
                "missing": [{
                    "section": "items",
                    "error": _brief_error(exc),
                }],
            }, indent=2))
            return 0
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2

    try:
        repo_readiness = None
        if (
            args.command in ("next", "queue")
            or (args.command == "begin" and args.agent == "codex")
        ):
            repo_readiness = repo_readiness_for_items(items)
        if args.command == "claim":
            return cmd_claim(
                items, now, args.ref, pr_facts=ticket_pr_facts(items)
            )
        if args.command == "release":
            return cmd_release(items, now, args.ref)
        if args.command == "pin":
            return cmd_pin(items, now, args.ref, args.confirmed,
                           args.run, args.agent)
        if args.command == "unpin":
            return cmd_unpin(items, now, args.ref, args.confirmed,
                             args.run, args.agent)
        if args.command == "park":
            return cmd_park(items, now, args.ref, args.reason,
                            args.run, args.agent)
        if args.command == "comment":
            if args.needs_decision is not None:
                body = _needs_decision_comment_body(args.needs_decision)
            elif args.blocked_on:
                body = _blocked_comment_body(args.blocked_on, args.because)
            else:
                body = args.body
            if args.body_file:
                try:
                    body = pathlib.Path(args.body_file).read_text()
                except OSError as exc:
                    raise GitHubError(
                        "cannot read {}: {}".format(args.body_file, exc)
                    )
            return cmd_comment(
                items, now, args.ref, body or "", args.voice,
                args.run, args.agent, apply_blocked=args.needs_decision is not None,
            )
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
                               args.run, args.agent, args.origin, args.klass)
        if args.command == "shaped":
            return cmd_shaped(items, now, args.ref, args.plan,
                              args.run, args.agent, args.klass)
        if args.command == "begin":
            return cmd_begin(items, now, args.agent, args.tier, args.idle,
                             args.breakdown, args.routine_sha,
                             repo_readiness=repo_readiness)
        if args.command == "next-review":
            return cmd_next_review(items, args.tier)
        if args.command == "review":
            return cmd_review(args.repo, args.pr, args.verdict, args.ci,
                              args.blocking, args.note, args.run, args.agent)
        if args.command == "merge":
            return cmd_merge(items, now, args.repo, args.pr, args.confirmed)
        if args.command == "next":
            return cmd_next(
                items,
                now,
                tier=getattr(args, "tier", None),
                excluded=set(getattr(args, "excluded", [])),
                agent=getattr(args, "agent", "codex"),
                repo_readiness=repo_readiness,
                pr_facts=ticket_pr_facts(items),
            )
        if args.command == "brief":
            missing = []
            timings: Dict[str, float] = {}
            degraded: List[Dict[str, object]] = []
            deadline = time.perf_counter() + BRIEF_TOTAL_BUDGET_SECONDS
            cache = _ACTIVE_BRIEF_CACHE.get() or BriefCache()

            pr_facts_error: List[str] = []

            def read_pr_facts():
                try:
                    return cache.get_pr_facts(items)
                except GitHubError as exc:
                    pr_facts_error.append(
                        "could not read ticket branch facts: {}".format(exc)
                    )
                    for section in BRIEF_PR_FACT_SECTIONS:
                        missing.append({
                            "section": section,
                            "error": pr_facts_error[-1],
                        })
                    return {}

            pr_facts = _brief_timed(
                "ticket_pr_facts",
                read_pr_facts,
                timings,
                degraded,
                deadline=deadline,
            )
            if pr_facts is _BRIEF_UNAVAILABLE:
                # A missing PR/branch scan must not turn a stale claim into a
                # false diagnostic. An empty mapping says those facts are
                # unavailable, so the pure consumers preserve the safe side.
                pr_facts = {}
                if not pr_facts_error:
                    error = "could not read ticket branch facts: brief section read timed out"
                    for section in BRIEF_PR_FACT_SECTIONS:
                        missing.append({"section": section, "error": error})
            return cmd_brief(
                items,
                now,
                pr_facts=pr_facts,
                missing=missing,
                timings=timings,
                degraded=degraded,
                deadline=deadline,
                brief_cache=cache,
            )
        if args.command == "queue":
            return cmd_queue(items, now, repo_readiness=repo_readiness)
        return cmd_brief(items, now, pr_facts=ticket_pr_facts(items))
    except GitHubError as exc:
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2


SESSION_ENV = "FUNNEL_SESSION"
SESSION_SERVER_ENV = "FUNNEL_SESSION_SERVER"
SESSION_TIMEOUT_SECONDS = 30
# Leave one second for the session handler to format and send its response
# before the client's transport budget expires. The child process doing the
# external work receives this deadline through `_run_bounded_subprocess`, so a
# timed-out command releases the serialized session lock for the next command.
SESSION_COMMAND_BUDGET_SECONDS = SESSION_TIMEOUT_SECONDS - 1.0
SESSION_HEALTH_TIMEOUT_SECONDS = 1
SESSION_STDIN_LIMIT = 1_000_000
# The stdin limit is a content limit. JSON framing and escaped characters add
# overhead before the request reaches the server, so the request reader needs
# a larger bound than the existing one-megabyte reply reader.
SESSION_REQUEST_LIMIT = SESSION_STDIN_LIMIT * 6 + 4_096
SESSION_RESPONSE_LIMIT = 1_000_000
_SESSION_STDIN_UNSET = object()
_SESSION_COMMAND_STATE: contextvars.ContextVar = contextvars.ContextVar(
    "session_command_state", default=None
)


class SessionCommandTimeout(RuntimeError):
    """A session command ran out of server-side time before it answered."""

    def __init__(self, command: str):
        self.command = command
        self.budget = SESSION_COMMAND_BUDGET_SECONDS
        super().__init__(
            "{} exceeded the {:.1f}s server-side budget".format(
                command, self.budget
            )
        )


def _session_command_remaining() -> Optional[float]:
    """Return the active command's remaining budget, or ``None`` outside it."""
    state = _SESSION_COMMAND_STATE.get()
    if state is None:
        return None
    command, deadline = state
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise SessionCommandTimeout(command)
    return remaining


def _run_bounded_subprocess(command: Sequence[str], **kwargs):
    """Run a child process with the current session command's deadline."""
    remaining = _session_command_remaining()
    if remaining is not None:
        existing_timeout = kwargs.get("timeout")
        if existing_timeout is None:
            kwargs["timeout"] = remaining
        else:
            kwargs["timeout"] = min(float(existing_timeout), remaining)
    try:
        return subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        state = _SESSION_COMMAND_STATE.get()
        if state is not None:
            raise SessionCommandTimeout(state[0]) from exc
        raise


class FunnelSession:
    """Serve one run's commands from one in-memory Project read.

    This is deliberately a per-run session, not a cache or a daemon. The
    Project is loaded lazily on the first command so a ``begin`` claim still
    reads GitHub at claim time. Later commands in the same Muse run reuse the
    already-loaded objects and their locally updated mutation state.
    """

    def __init__(self, loader=None):
        self._loader = load_items if loader is None else loader
        self.items: Optional[List[Item]] = None
        self._brief_cache = BriefCache()

    def _load_items(self) -> List[Item]:
        if self.items is None:
            self.items = self._loader()
        return self.items

    def dispatch(self, argv: Sequence[str], stdin=None):
        """Run one normal funnel command and return ``(code, stdout, stderr)``.

        ``stdin`` is optional because an interactive client must not have its
        terminal consumed by the session shim. When present, it is visible as
        ``sys.stdin`` only while this command's ``main()`` runs.
        """
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = 2
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if not argv or argv[0] != "brief":
                self._brief_cache.clear()
            cache_token = _ACTIVE_BRIEF_CACHE.set(self._brief_cache)
            try:
                # Reset before the lazy load so its GraphQL work is measured as
                # part of the first command rather than erased by ``main``.
                reset_api_usage()
                if stdin is None:
                    code = main(list(argv), _items=self.items,
                                _items_loader=self._load_items,
                                _reset_api_usage=False)
                else:
                    if isinstance(stdin, bytes):
                        stdin = stdin.decode("utf-8")
                    if not isinstance(stdin, str):
                        raise ValueError("invalid session stdin payload")
                    previous_stdin = sys.stdin
                    sys.stdin = io.StringIO(stdin)
                    try:
                        code = main(list(argv), _items=self.items,
                                    _items_loader=self._load_items,
                                    _reset_api_usage=False)
                    finally:
                        sys.stdin = previous_stdin
                # A command that only did Python work still has to answer
                # inside the same server-side envelope as its GitHub calls.
                _session_command_remaining()
            except SessionCommandTimeout as exc:
                # A timed-out GitHub mutation may have reached the server
                # before its response was lost. Throw away the local view so
                # the next command reloads GitHub state rather than acting on
                # a stale Project snapshot. Never retry the command here.
                self.items = None
                self._brief_cache.clear()
                stdout.seek(0)
                stdout.truncate(0)
                stderr.seek(0)
                stderr.truncate(0)
                print(
                    "funnel: command-timeout: {}; session remains usable; "
                    "the next command will reload Project state".format(exc),
                    file=sys.stderr,
                )
                code = 2
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
            except Exception as exc:
                print("funnel: {}".format(exc), file=sys.stderr)
            finally:
                # A session has one process but several commands. Keep the
                # existing per-command heartbeat measurements, while the
                # in-memory Project read is shared across them.
                report_api_cost()
                report_graphql_spend()
                _ACTIVE_BRIEF_CACHE.reset(cache_token)
        return int(code), stdout.getvalue(), stderr.getvalue()


class _SessionServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """A per-run server with a lock-free health request.

    Normal commands remain serialised because ``FunnelSession`` owns mutable
    in-memory Project and stdin state.  The transport is threaded so a health
    request can report the command holding that lock while the command itself
    is still reading GitHub. Each command also carries a deadline; bounded
    child processes fail loud before the transport timeout so the lock is
    released for the next request.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._active_command: Optional[str] = None
        self._active_started: Optional[float] = None
        self._last_timing: Optional[Dict[str, object]] = None

    @staticmethod
    def _command_name(argv: Sequence[str]) -> str:
        return argv[0] if argv else "<empty>"

    def dispatch(self, argv: Sequence[str], stdin=_SESSION_STDIN_UNSET):
        """Run one command and return it with its server-side timing."""
        command = self._command_name(argv)
        with self.command_lock:
            command_token = _SESSION_COMMAND_STATE.set((
                command,
                time.monotonic() + SESSION_COMMAND_BUDGET_SECONDS,
            ))
            started = time.perf_counter()
            with self._activity_lock:
                self._active_command = command
                self._active_started = started
            try:
                if stdin is _SESSION_STDIN_UNSET:
                    code, stdout, stderr = self.session.dispatch(argv)
                else:
                    code, stdout, stderr = self.session.dispatch(
                        argv, stdin=stdin
                    )
            finally:
                timing = {
                    "command": command,
                    "elapsed_seconds": round(
                        max(0.0, time.perf_counter() - started), 6
                    ),
                }
                with self._activity_lock:
                    self._active_command = None
                    self._active_started = None
                    self._last_timing = timing
                _SESSION_COMMAND_STATE.reset(command_token)
        return code, stdout, stderr, timing

    def health_payload(self) -> Dict[str, object]:
        """Return cheap liveness and current-command instrumentation."""
        with self._activity_lock:
            command = self._active_command
            started = self._active_started
            last_timing = self._last_timing
            elapsed = (
                round(max(0.0, time.perf_counter() - started), 6)
                if started is not None else 0.0
            )

        status = "busy" if command is not None else "idle"
        health: Dict[str, object] = {
            "ok": True,
            "status": status,
            "busy": status == "busy",
            "command": command,
            "elapsed_seconds": elapsed,
        }
        if last_timing is not None:
            health["last_timing"] = dict(last_timing)
        return {
            "code": 0,
            "stdout": "",
            "stderr": "",
            "health": health,
        }


class _SessionHandler(socketserver.StreamRequestHandler):
    """One newline-delimited request from a funnel client shim."""

    def _reply(self, payload: Dict[str, Any]) -> None:
        wire = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        self.wfile.write(wire)
        self.wfile.flush()

    def handle(self) -> None:
        line = self.rfile.readline(SESSION_REQUEST_LIMIT)
        try:
            request = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._reply({
                "code": 2,
                "stdout": "",
                "stderr": "funnel: invalid session request\n",
            })
            return

        token = request.get("token") if isinstance(request, dict) else None
        if not isinstance(token, str) or not hmac.compare_digest(
                token, self.server.token):
            self._reply({
                "code": 2,
                "stdout": "",
                "stderr": "funnel: invalid session token\n",
            })
            return

        if request.get("shutdown") is True:
            self.server.stop_requested = True
            self._reply({"code": 0, "stdout": "", "stderr": ""})
            return

        if request.get("health") is True:
            self._reply(self.server.health_payload())
            return

        argv = request.get("argv")
        if (not isinstance(argv, list)
                or not all(isinstance(part, str) for part in argv)):
            self._reply({
                "code": 2,
                "stdout": "",
                "stderr": "funnel: invalid session arguments\n",
            })
            return

        stdin = request.get("stdin")
        if "stdin" in request and not isinstance(stdin, str):
            self._reply({
                "code": 2,
                "stdout": "",
                "stderr": "funnel: invalid session stdin\n",
            })
            return

        if "stdin" in request:
            code, stdout, stderr, timing = self.server.dispatch(
                argv, stdin=stdin
            )
        else:
            code, stdout, stderr, timing = self.server.dispatch(argv)
        self._reply({
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "timing": timing,
        })


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def serve_session(parent_pid: Optional[int] = None) -> int:
    """Serve a disposable funnel session on loopback until it is stopped."""
    server = _SessionServer(("127.0.0.1", 0), _SessionHandler)
    server.token = secrets.token_hex(24)
    server.session = FunnelSession()
    server.stop_requested = False
    server.timeout = 1
    host, port = server.server_address
    print("{}:{}:{}".format(host, port, server.token), flush=True)

    try:
        while not server.stop_requested:
            if parent_pid is not None and not _process_is_alive(parent_pid):
                break
            server.handle_request()
    finally:
        server.server_close()
    return 0


def _read_session_stdin() -> Optional[str]:
    """Read piped stdin for a session request, or return ``None`` for a TTY."""
    try:
        if sys.stdin.isatty():
            return None
    except (AttributeError, OSError):
        # A real CLI stdin always has `isatty`; if a test double or unusual
        # wrapper cannot answer, do not risk blocking it as though it were a
        # pipe.
        return None

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    data = stream.read(SESSION_STDIN_LIMIT + 1)
    if isinstance(data, str):
        encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
        data = data.encode(encoding)
    elif not isinstance(data, bytes):
        data = bytes(data)

    if len(data) > SESSION_STDIN_LIMIT:
        raise ValueError(
            "stdin exceeds the {}-byte session limit".format(SESSION_STDIN_LIMIT)
        )

    encoding = getattr(sys.stdin, "encoding", None) or "utf-8"
    try:
        return data.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError(
            "stdin is not decodable as {}: {}".format(encoding, exc)
        ) from exc


def _session_command_uses_stdin(argv: Sequence[str]) -> bool:
    """Return whether this command's arguments request a piped plan."""
    parts = list(argv)
    if not parts or parts[0] != "shaped":
        return False
    for index, part in enumerate(parts):
        if part == "--plan" and index + 1 < len(parts):
            return parts[index + 1] == "-"
        if part == "--plan=-":
            return True
    return False


def _session_health(host: str, port: int, token: str) -> Optional[Dict[str, object]]:
    """Ask the live server for status without waiting on its command lock."""
    request = {"token": token, "health": True}
    try:
        with socket.create_connection(
            (host, port), timeout=SESSION_HEALTH_TIMEOUT_SECONDS
        ) as connection:
            stream = connection.makefile("rwb")
            with stream:
                stream.write(
                    (json.dumps(request, separators=(",", ":")) + "\n")
                    .encode("utf-8")
                )
                stream.flush()
                line = stream.readline(SESSION_RESPONSE_LIMIT)
    except (OSError, TypeError, ValueError):
        return None

    try:
        response = json.loads(line.decode("utf-8"))
        health = response["health"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None
    return health if isinstance(health, dict) else None


def _session_client(argv: Sequence[str]) -> int:
    """Forward one CLI invocation to the current Muse run's session."""
    endpoint = os.environ.get(SESSION_ENV, "")
    try:
        host, port_text, token = endpoint.split(":", 2)
        port = int(port_text)
        if not host or not token or not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        print("funnel: invalid {} endpoint".format(SESSION_ENV), file=sys.stderr)
        return 2

    request = {
        "token": token,
        "argv": list(argv),
    }
    if list(argv) == ["session-stop"]:
        request["shutdown"] = True
    elif _session_command_uses_stdin(argv):
        try:
            stdin = _read_session_stdin()
        except (OSError, TypeError, ValueError) as exc:
            print("funnel: {}".format(exc), file=sys.stderr)
            return 2
        if stdin is not None:
            request["stdin"] = stdin

    try:
        connection = socket.create_connection(
            (host, port), timeout=SESSION_TIMEOUT_SECONDS
        )
    except socket.timeout as exc:
        print(
            "funnel: connect-timeout: {} session unreachable within {}s: {}"
            .format(SESSION_ENV, SESSION_TIMEOUT_SECONDS, exc),
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print("funnel: could not reach {}: {}".format(SESSION_ENV, exc),
              file=sys.stderr)
        return 2

    try:
        with connection:
            stream = connection.makefile("rwb")
            with stream:
                stream.write(
                    (json.dumps(request, separators=(",", ":")) + "\n")
                    .encode("utf-8")
                )
                stream.flush()
                line = stream.readline(SESSION_RESPONSE_LIMIT)
    except socket.timeout as exc:
        health = _session_health(host, port, token)
        slow_command = None
        if health is not None and health.get("status") == "busy":
            candidate = health.get("command")
            if isinstance(candidate, str) and candidate:
                slow_command = candidate
        detail = (
            "slow command: {}".format(slow_command)
            if slow_command is not None else "slow command unknown"
        )
        print(
            "funnel: reply-timeout: {} session busy past the {}s reply "
            "budget ({}): {}".format(
                SESSION_ENV, SESSION_TIMEOUT_SECONDS, detail, exc
            ),
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print("funnel: could not reach {}: {}".format(SESSION_ENV, exc),
              file=sys.stderr)
        return 2

    try:
        response = json.loads(line.decode("utf-8"))
        code = int(response["code"])
        stdout = response.get("stdout", "")
        stderr = response.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise ValueError
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        print("funnel: invalid session response", file=sys.stderr)
        return 2

    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


def report_api_cost(run: Optional[str] = None,
                    agent: Optional[str] = None) -> None:
    """Append this command's API measurements to its open heartbeat run.

    This is best-effort telemetry. The command must still return its real
    result when the heartbeat spool or GitHub is unavailable, so every failure
    here is deliberately swallowed; `heartbeat finish` will then report nulls
    for the missing measurement.
    """
    if not api_usage()["calls"]:
        return
    try:
        run, agent = _heartbeat_context(
            run if run is not None else _ACTIVE_HEARTBEAT_RUN,
            agent if agent is not None else _ACTIVE_HEARTBEAT_AGENT,
        )
        if not run or not agent:
            return
        import heartbeat

        heartbeat.record_api_cost(agent, run, api_cost())
    except Exception:
        # Instrumentation must not gate the command it instruments.  The
        # absence of this event is represented by nulls at heartbeat finish.
        return


def report_graphql_spend(stream=None) -> None:
    """Print what this process spent on GraphQL, if it spent anything.

    To **stderr**, never stdout: `begin`, `brief` and `next` emit JSON that
    routines parse, and a spend line on stdout would corrupt it. A run that
    made no GraphQL call prints nothing rather than a row of zeros.
    """
    spend = graphql_spend()
    if not spend["calls"]:
        return
    print(
        "graphql: {} call(s), {} point(s) spent, {} remaining{}".format(
            spend["calls"], spend["cost"],
            "unknown" if spend["remaining"] is None else spend["remaining"],
            "" if not spend["reset_at"] else ", resets {}".format(spend["reset_at"]),
        ),
        file=stream if stream is not None else sys.stderr,
    )


if __name__ == "__main__":
    if os.environ.get(SESSION_ENV) and not os.environ.get(SESSION_SERVER_ENV):
        code = _session_client(sys.argv[1:])
    elif os.environ.get(SESSION_SERVER_ENV):
        # The session server reports each forwarded command itself. Do not let
        # the normal process-exit hook record the last command a second time.
        code = main()
    else:
        try:
            code = main()
        finally:
            report_api_cost()
            # In a `finally` so a run that dies on an exhausted budget still says
            # what it spent — that run is exactly the one whose numbers matter.
            report_graphql_spend()
    sys.exit(code)
