#!/usr/bin/env python3
"""funnel.py — the shared ranking program.

Both Claude and Codex call this; neither ranks anything itself. See AGENTS.md.

GitHub is the state. There is no funnel-owned cache, state file or lock file here.
The GitHub CLI's response cache is used only for explicitly non-gating REST reads;
it never becomes a source of funnel state.

Authentication is delegated entirely to the `gh` CLI, so no token is ever read,
stored or passed by this program.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import contextvars
import copy
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
import errno
import glob
import hmac
import inspect
import io
import json
import math
import os
import pathlib
import random
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import (IO, Any, Callable, Collection, Dict, Iterable, Iterator,
                    List, Mapping, Optional, Sequence, Set, Tuple)

import agent_health as agent_health_module
from agent_health import assess as assess_agent_health
from decline_classifier import classify_decline_reason

# --------------------------------------------------------------------------
# Configuration. These are the only knobs; everything else is derived.
# --------------------------------------------------------------------------

PROJECT_OWNER = "nateprich"
PROJECT_NUMBER = 2
TOPIC = "command-center"
OWNERS = [("user", "nateprich"), ("organization", "nateprich-projects")]
REPO = "nateprich-projects/command-center"

# The begin path has several independent, read-only GitHub fetches after its
# batched Project load. Keep their concurrency bounded so a large portfolio
# cannot turn one opening command into an unbounded request burst.
BEGIN_FETCH_POOL_SIZE = 4

# The local checks deliberately keep their paths as module-level values. Tests
# can point them at a temporary checkout and home directory without ever
# reading the real ~/.claude.
CHECKOUT_ROOT = pathlib.Path(__file__).resolve().parent
CLAUDE_DIR = pathlib.Path.home() / ".claude"

#: The vendor-specific facts a Codex implementation run needs in addition to
#: the ticket packet. Keep these structured and code-owned: the shortened
#: routine tells Codex to follow ``begin`` rather than duplicating sandbox and
#: path rules in a pasted prompt that can drift.
CODEX_IMPLEMENT_VENDOR = {
    "sandbox": {
        "writable": [
            "the current Codex per-session working directory",
            "~/.claude/command-center-heartbeat",
        ],
        "read_execute_only": [
            "/Users/nateprich/.claude/command-center-run",
        ],
    },
    "path_spelling": (
        "Invoke every Command Center helper through exactly "
        "/Users/nateprich/.claude/command-center-run. Never rewrite that "
        "spelling to the symlink target; only Codex sandbox configuration may "
        "contain the resolved target."
    ),
    "answer_handoff": (
        "Write the one structured answer to a file in the Codex session "
        "directory, outside the ticket checkout, then invoke finish-ticket "
        "from the checkout with --answer-file PATH."
    ),
    "begin_wait": (
        "begin can take several minutes. If the exec tool yields a timeout or "
        "partial output while the process is still running, keep reading the "
        "same exec session until the process exits. Never treat that yield as "
        "a failure and never invoke begin again."
    ),
}

#: The same facts for Claude's Saturday implement lane (#1557). Claude runs as a
#: Desktop scheduled task on the Mac mini, not in a sandbox, so the only rules
#: are where helpers live and where the checkout and answer go.
CLAUDE_IMPLEMENT_VENDOR = {
    "path_spelling": (
        "Invoke every Command Center helper through exactly "
        "/Users/nateprich/.claude/command-center-run."
    ),
    "checkout": (
        "Clone packet.repo into a fresh directory under the session's scratch "
        "or temporary directory, never inside a Command Center checkout."
    ),
    "answer_handoff": (
        "Write the one structured answer to a file outside the ticket "
        "checkout, then invoke finish-ticket --agent claude from the checkout "
        "with --run RUN --answer-file PATH."
    ),
}

#: Claude implements only on Saturday mornings, spending what is left of the
#: Anthropic week before it resets at noon local (usage.WEEKLY_RESET_*). No new
#: work starts at or after 11:15, so a run, or an automatic resume after a
#: usage-limit pause, cannot begin a ticket that would run into next week's
#: allowance. The routine prompt stops in-flight work at 11:45. Nate, 2026-09-25
#: (#1557).
IMPLEMENT_VENDORS = {
    "codex": CODEX_IMPLEMENT_VENDOR,
    "claude": CLAUDE_IMPLEMENT_VENDOR,
}

CLAUDE_WINDOW_WEEKDAY = 5  # Monday is 0, so 5 is Saturday
CLAUDE_WINDOW_LAST_START = (11, 15)

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
    # ``None`` means the live Actions probe was not requested.  Keeping that
    # distinct from ``unknown`` lets the doctor retain its existing contract
    # while ``begin`` can add a fresh per-repository signal to queue reads.
    ci_state: Optional[str] = None
    ci_annotation: Optional[str] = None

    @property
    def blocking_reasons(self) -> Tuple[str, ...]:
        """Return only requirements whose absence prevents a merge."""
        reasons: List[str] = []
        if not self.topic:
            reasons.append("missing command-center topic")
        if not self.ci_workflow:
            reasons.append("no CI workflow")
        if self.ci_state == CI_COULD_NOT_RUN:
            reasons.append(
                "CI could not run: {}".format(
                    self.ci_annotation or "startup or account failure"
                )
            )
        return tuple(reasons)

# `funnel doctor` only needs to know which ticket branches have merged. Keep
# the scan result separate from the pure contradiction detector, and carry its
# bounded-scan warning along with the refs that were found.
MergedPRFacts = namedtuple("MergedPRFacts", "ticket_refs truncated")


class TicketPRIndex(dict):
    """A ticket-branch index plus the rows from the bounded repository scan.

    Most funnel callers need only the newest PR for each ticket branch. Outcome
    derivation also needs every PR on a branch so it can count attempts and
    turns without falling back to one lookup per ticket. Keeping the complete
    bounded scan as an attribute preserves the existing mapping contract while
    letting that consumer reuse the same repository-wide read.
    """

    def __init__(self, *args, all_rows: Iterable[Dict] = ()):
        super().__init__(*args)
        self.all_rows = tuple(all_rows)


@dataclass(frozen=True)
class BatchedPRRead:
    """Repository-wide PR observations returned by one GraphQL batch.

    The row lists stay grouped by repository so callers can preserve their
    existing bounded-scan contracts. Branch refs travel with the same query:
    the queue needs them for abandoned claims, while a second REST scan would
    put the fan-out back under a different name.
    """

    rows_by_repo: Mapping[str, Tuple[Dict[str, object], ...]]
    branch_refs_by_repo: Mapping[str, Set[str]]
    pr_truncated_by_repo: Mapping[str, bool]
    branches_truncated_by_repo: Mapping[str, bool]


class TicketPRFacts(dict):
    """Primary ticket facts plus every PR row on each ticket branch.

    Most queue and brief consumers need one newest row. Review and merge
    reconciliation need all open rows so an older PR on the same branch cannot
    hide a current candidate. Keeping the extra rows as an in-memory attribute
    preserves the established mapping returned to callers and keeps GitHub as
    the only source of state.
    """

    def __init__(
        self,
        *args,
        rows_by_ref: Optional[
            Mapping[str, Iterable[Mapping[str, object]]]
        ] = None,
    ):
        super().__init__(*args)
        self.rows_by_ref = {
            ref: tuple(dict(row) for row in rows if isinstance(row, Mapping))
            for ref, rows in (rows_by_ref or {}).items()
        }

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

#: A ``begin`` can spend long enough reading and reconciling the Project for a
#: second lane to select the same unclaimed ticket from its own earlier view.
#: Re-read the selected ticket immediately before its claim and pass over only
#: a claim written inside this short collision window. The one-minute bound is
#: deliberately narrower than the normal lock lifetime: it catches concurrent
#: lane starts without hiding a ticket that has become available again.
BEGIN_CLAIM_COLLISION_WINDOW = timedelta(seconds=60)

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
#: five-minute standard schedule plus an hourly escalated one, with cost metered
#: from its local attribution journal, and
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

#: Repo tiers for the engineers' queue (Nate, 2026-09-25): 1 is the tooling
#: that keeps everything else running, 2 has real-world impact, and every
#: other member repo is a hobby at 3. Ranked below finite work and pins and
#: above the Building commitment, so a hobby project already Building waits
#: while higher-tier work is startable (plan.md, "Codex's work runs the
#: ladder"). Keyed by repository name, without the owner.
REPO_TIERS = {
    "command-center": 1, "github-runners": 1, "workbench": 1,
    "career-toolset": 2, "jeffy-finance-agent": 2,
}
HOBBY_TIER = 3


def repo_tier(repo: str) -> int:
    """A repository's tier; any repo not named in ``REPO_TIERS`` is a hobby."""
    return REPO_TIERS.get(repo.rsplit("/", 1)[-1], HOBBY_TIER)
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

# The dashboard is a display-only consumer of a successful brief. Its spool is
# deliberately outside the repository and is a buffer, not another source of
# funnel state. Keep the board order separate from the funnel's decision order:
# the dashboard puts Parked before the recent Done column.
DASHBOARD_SPOOL_ENV = "COMMAND_CENTER_DASHBOARD_SPOOL"
#: Entries kept in the dashboard spool after each write. Readers only want the
#: newest, but each one parses every entry to find it, and a brief is about
#: 500 KB, so an unpruned spool grew by 72 MB a day (#979).
DASHBOARD_SPOOL_KEEP = 50
_DASHBOARD_SPOOL_ENTRY = re.compile(r"^brief-(\d+)-[0-9a-f]+\.json$")
DASHBOARD_BOARD_STAGES = ("Ideas", "Shaped", "Ready", "Building", "Parked", "Done")
DASHBOARD_DONE_WINDOW = timedelta(days=7)

# Only these harnesses expose the complete pair of input-token counts used by
# the re-send metric. Claude is unscheduled, and Muse cost attribution is read
# by the usage gate rather than from the heartbeat token binding, so their
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

# `gh api --cache` is safe only for reads whose staleness cannot change queue,
# claim, or gate decisions. Keep the duration in one place so every cached REST
# lookup has the same bounded freshness window and tests can pin the policy.
GH_API_CACHE_DURATION = "5m"

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

# The hourly GraphQL window is 5,000 points. The ticket leaves the exact cap
# unstated; 826 is the highest whole-point floor that clears the observed
# false stand-down at 826 points, while preserving the 42-point calculation
# before the cap is applied.
GRAPHQL_RESERVE_POINT_CEILING = 826


def _reserve_floor(loads: int, load_cost: object) -> int:
    """Return the proportional reserve, bounded by the hourly point ceiling."""
    return min(loads * int(load_cost), GRAPHQL_RESERVE_POINT_CEILING)


# #1047 measured the direct GraphQL cost of one disposable begin session's
# Project load at 42 points, including the member-repository and paged item
# reads.  The rate-limit-only pre-read below must reserve that known load before
# it happens; using the pre-read's own cost would reserve only one cheap query.
BEGIN_PROJECT_LOAD_COST = 42

#: Regression issues opened by `funnel reject`. The issues themselves are the
#: counter — GitHub is the state, so there is nothing else to keep in step.
REGRESSION_PREFIX = "Regression from PR #"

#: Reasons are durable parking artifacts. The sibling brief command reads this
#: fixed marker back from issue comments, so it is a shared contract.
PARK_COMMENT_PREFIX = "**Parked:** "

#: A dated park records the prior Project Status on its own fixed header line.
#: The brief and the future wake path share this parser contract.
PARK_WAKE_PREFIX = "**Parked wake:** "
PARK_WAKE_STATUSES = frozenset(status for status in STAGES if status != "Parked")
_PARK_WAKE_STATUS_PATTERN = "|".join(
    re.escape(status) for status in STAGES if status != "Parked"
)
PARK_WAKE_RE = re.compile(
    r"\A" + re.escape(PARK_WAKE_PREFIX)
    + r"date=(?P<wake_date>[0-9]{4}-[0-9]{2}-[0-9]{2}) "
    + r"status=(?P<prior_status>" + _PARK_WAKE_STATUS_PATTERN + r")\Z"
)

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
    + r"(?: until (?P<blocked_until>[0-9]{4}-[0-9]{2}-[0-9]{2}))?"
      r"(?: on (?P<references>#[0-9]+(?: and #[0-9]+)*))?:\*\*"
)

#: A blocked ticket may wait on the one event kind the queue understands.
#: The body after this header must be a strict fenced JSON spec.
BLOCK_EVENT_COMMENT_PREFIX = "**Blocked until event:**"
BLOCK_EVENT_COMMENT_RE = re.compile(
    r"\A" + re.escape(BLOCK_EVENT_COMMENT_PREFIX)
    + r"[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*```json[ \t]*\r?\n"
      r"(?P<event_spec>.*?)\r?\n[ \t]*```[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)
BLOCK_EVENT_KIND_HEADER_RE = re.compile(
    r"\A\*\*Blocked until (?P<kind>[^:\r\n]+):\*\*"
)
BLOCK_FENCED_PAYLOAD_RE = re.compile(
    r"\A[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*```[^\r\n]*\r?\n"
    r".*?\r?\n[ \t]*```[ \t]*(?:\r?\n|$)",
    re.DOTALL,
)

#: A breakdown can leave a project waiting on Nate's answer. The header is
#: deliberately strict and anchored just like the ordinary block header so a
#: quoted or embedded sentence cannot become a gate question by accident.
NEEDS_DECISION_PREFIX = "**Needs a decision:**"
NEEDS_DECISION_RE = re.compile(
    r"\A" + re.escape(NEEDS_DECISION_PREFIX)
    + r"[ \t]+(?P<question>.+)", flags=re.DOTALL
)

#: The implement runner's decline record (``finish_declined``). It names a
#: reason in prose, not a condition the funnel can clear, so readers show it
#: and ``stranded_items`` flags the block until a parseable one replaces it.
DECLINED_PREFIX = "**Declined:**"

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

# Analysis projects deliver findings for Nate to read and judge; unlike upkeep
# work, they must reach the human acceptance gate regardless of Class or origin.
ANALYSIS_MARKER = "<!-- command-center-analysis -->"

# A capture may carry the earlier PR or ticket that caused the idea. Keep this
# separate from capture origin: origin says who raised it, while this marker
# says what the work is a response to. The brief uses the marker as durable
# evidence instead of scanning human prose for a plausible-looking reference.
CAUSED_BY_MARKER = "<!-- command-center-caused-by -->"

#: An origin is only a default for who shapes an idea. This marker records the
#: explicit exception without rewriting that historical fact. Moving work back
#: toward Nate is always safe; moving it toward agents requires Nate's voice.
ORIGIN_OVERRIDE_MARKER = "<!-- command-center-origin-override -->"
ORIGIN_OVERRIDE_TARGETS = ("nate", "agents")

#: Nate answers a breakdown's open Gates question in session, and the answer
#: lands as a comment. Every lane reads the plan body, so a comment answer is
#: invisible: observed on #1167, where the question was answered at 21:53:02Z
#: and a breakdown lane posted the same question again four minutes later.
#: This marker is the body record of that answer — the verbatim quote, when it
#: was given, and who gave it — so the ask and the read share one predicate
#: instead of each lane scanning prose for an answer-shaped sentence.
GATES_ANSWER_MARKER = "<!-- command-center-gates-answer -->"

#: The human half of the same record. ``engine/shape.py`` renders each open
#: Needs category as ``- <Category>: <text>``, so the Gates line is
#: addressable on its own. The legacy heading remains readable during the
#: migration, although new plans use ``Needs Nate`` only.
GATES_LINE_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)-[ \t]+Gates:[ \t]*(?P<text>.*)$"
)

#: What an answered Gates line says. The verbatim answer is repeated here
#: rather than summarised, so the line a person reads and the marker a lane
#: reads cannot disagree about what was decided — the whole point of writing
#: both in one operation.
GATES_ANSWERED_LINE = "{indent}- Gates: answered {at} by {decider}. {answer}"

#: Three rejected merges in a week means the auto-merge bar has failed. That is
#: not "there are bugs" — it is a different and more serious fact, and the
#: response is to stop auto-merging and fix the review prompt.
REJECTED_MERGE_ALARM = 3
REJECTED_MERGE_WINDOW = timedelta(days=7)

#: Keep funnel-closed work visible across several unattended brief runs. A
#: brief is hourly, so a one-run window would make the record disappear before
#: Nate could reasonably see it.
CLOSED_ITSELF_WINDOW = timedelta(days=7)

# The approve gate may adopt exactly one explicit class proposal. Keep the
# human override visible in the durable record rather than treating the write
# as an inferred classification.
CLASS_ADOPTION_OVERRIDE_NOTE = "Nate may override it at any time."

# The auto-close writer appends its marker when it closes a project. Reading a
# bounded tail is enough to retain that write while preventing one unusually
# noisy issue from expanding a gate read without limit.
CLOSED_ITSELF_COMMENT_PAGE_SIZE = 20
CLOSED_ITSELF_COMMENT_BATCH_SIZE = 100

#: Keep mechanical block clears visible across several unattended brief runs,
#: for the same reason funnel-closed projects remain visible for a week.
CLEARED_BLOCK_WINDOW = timedelta(days=7)

# A brief has to answer well inside the session reply timeout (three minutes,
# see SESSION_TIMEOUT_SECONDS) so the caller can tell a slow brief from a dead
# session. It was 29 s while that timeout was 30 s, and a single Project load
# already took longer than that on 2026-09-10 (#595). The section allocations
# below are deliberately explicit so the slowest reads stay visible and
# reviewable instead of turning into one arbitrary global timeout.
#
# Measured 2026-09-11 on the 377-item Project board over three ordinary
# sequential passes: p90 was 44.438 s for closed_itself (101 candidates),
# 6.098 s for cleared_blocks (13 candidates), and 48.017 s for
# unattended_approvals (109 candidates). Round each up to a simple cap while
# leaving the existing fixed floors for the cheap sections unchanged; the
# 120 s total remains the transport envelope.
BRIEF_TOTAL_BUDGET_SECONDS = 120.0
BRIEF_SECTION_BUDGETS = {
    # Past the 22.9 s observed max over 738 refs plus headroom for GitHub
    # variance (#1168, #1210).
    "ticket_pr_facts": 35.0,
    "items": 0.25,
    "counts_by_gate": 0.25,
    "in_motion": 0.25,
    # 30 s, not 2 s. The ticket asked for 5 s, sized from a 2.0168 s read on a
    # 738-ref board. Re-measured 2026-09-21 on 1091 items, three consecutive
    # reads: 27.86 s, 19.17 s, 22.24 s. 5 s would have degraded every one of
    # them, so the cap is set from the measurement rather than from the
    # ticket's number, and 30 s clears the observed maximum (#1211).
    "parked": 30.0,
    "closed_itself": 45.0,
    # 30 s, not 7 s, and not the ticket's 12 s: re-measured on the same
    # 1091-item board at 20.93 s, 25.17 s, 22.94 s.
    "cleared_blocks": 30.0,
    "blocked": 0.25,
    "event_block_inconsistencies": 0.25,
    "human_steps": 0.25,
    "machine_local_steps": 0.25,
    "blocked_human_steps": 0.25,
    "blocked_machine_local_steps": 0.25,
    "closed_with_access_vocabulary": 0.25,
    "unclassed_captures": 0.25,
    "needs_class": 0.25,
    "awaiting_breakdown": 0.25,
    "stranded": 0.25,
    "stale_locks_taken_over": 0.25,
    "maintenance_load": 0.25,
    "disposal": 0.25,
    "resend_ratio": 3.0,
    "unattended_merges": 3.0,
    "unattended_approvals": 49.0,
    "run_summary": 1.0,
    "agent_health": 1.0,
    "working_tree_touched": 1.0,
    # Pure over the items already loaded: no read of its own to time out.
    "status_state_mismatches": 0.25,
    # One REST read per member repo for main's head, plus a bounded follow-up
    # only where that head's run failed. Six direct main_ci_json passes across
    # 2026-09-22/23: 5.92, 6.30, 7.11, 7.5276, 6.6979, 7.7154 s
    # (5.92–7.7154 s; 1.7954 s spread). Brief runs
    # at 2026-09-22 14:27Z and 2026-09-23 13:37Z timed out at 3.0177 and
    # 3.0112 s. 10 s leaves 2.2846 s (29.6%) above the measured direct tail;
    # the 2026-09-23 13:42Z brief completed in 6.9585 s and returned []. A
    # captured 2026-09-23 16:26Z brief stdout excerpt is in
    # evidence/main-ci-brief-2026-09-23.json: main_ci was [], missing was [],
    # and the only degraded section was closed_with_access_vocabulary.
    "main_ci": 10.0,
    # One `gh issue list` per member repo. Sized like the other live scans.
    "member_issues_without_project_items": 8.0,
    "outcome_signals": 3.0,
    # 10 s, not 3 s: the ticket-PR share pages every merged PR in the window
    # (#1286). Measured 2026-09-23 on 476 merges (5 pages): 2.27 s, 2.62 s,
    # 2.97 s, so 3 s degraded about one read in three, and the window grows.
    "portfolio_metrics": 10.0,
    # Reads the forward-only decline-routing signal from issue comments and
    # current GitHub state. The search is per member repo and paged.
    "decline_routing": 10.0,
    "rejected_merges": 0.25,
}

# No brief section feeds a gate any more: the merge gate reads the
# rejected-merge counter itself (#801), and every other consumer reads the
# published brief. A slow or unreadable section therefore degrades into an
# explicit `degraded` or `missing` entry; it never fails the whole brief.
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
    # Routing policy lives in Project fields. Origin is set on parent ideas;
    # Risk and Needs are set on every active row. Issue prose may explain a
    # value, but it is never a second machine-readable copy of one.
    origin: Optional[str] = None  # "agent" | "Nate"
    risk: Optional[str] = None  # "standard" | "escalated"
    pinned: bool = False
    # The row's Needs single-select. Tickets use it for work ownership;
    # projects use it for decision routing. ``agent`` owns blocked work;
    # ``external-event`` suppresses the unblock question only with a parsed
    # event condition.
    needs: Optional[str] = None
    status_since: Optional[datetime] = None
    # ProjectV2 status history retained from the load query. The brief uses it
    # to find likely unattended shaping transitions before reading comments.
    status_events: List[Dict[str, object]] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)
    block_references: List[str] = field(default_factory=list)
    block_reason: Optional[str] = None
    blocked_until: Optional[date] = None
    needs_decision: Optional[str] = None
    decline_reason: Optional[str] = None
    unparseable_block_comments: List[str] = field(default_factory=list)
    block_comments_error: Optional[str] = None
    satisfied_block_record: Optional[Dict[str, object]] = None
    open_blockers: List[str] = field(default_factory=list)
    # The shape packet can request the target idea's complete issue thread
    # in the same GraphQL operation that loads Project items. Other funnel
    # callers leave this unset and do not pay for comment reads.
    issue_comments: Optional[List[Dict[str, object]]] = None
    dead_blockers: List[str] = field(default_factory=list)
    # Complete native Issue.blockedBy refs from the Project item query.
    # None means that the connection was missing, malformed, or truncated.
    blocked_by_refs: Optional[List[str]] = None
    assignees: List[str] = field(default_factory=list)
    in_motion_since: Optional[datetime] = None
    item_id: Optional[str] = None  # the ProjectV2Item, needed to write the lock
    parent: Optional[str] = None  # "owner/repo#123"
    children_total: int = 0
    children_done: int = 0
    # Derived at load time from child ticket Needs fields. This is
    # deliberately not a second GitHub record: the Needs field remains the
    # only source of truth, including after its ticket closes.
    carried_human_step: bool = False
    first_child_created_at: Optional[datetime] = None
    last_child_closed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    blocked_since: Optional[datetime] = None
    blocked_cleared_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    block_event: Optional[Dict[str, str]] = None

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


def _item_blocked_until(item: Item) -> Optional[date]:
    """Return the trusted date condition stored on an Item, if any."""
    value = item.blocked_until
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _block_condition_date(now: Optional[datetime] = None) -> date:
    """Return the UTC calendar date used by date-conditioned blocks."""
    return (now or datetime.now(timezone.utc)).date()


def _block_date_condition(item: Item) -> Optional[str]:
    """Render a parsed date condition for reports and clear records."""
    blocked_until = _item_blocked_until(item)
    if blocked_until is None:
        return None
    return "until {}".format(blocked_until.isoformat())


def gate_question(item: Item) -> Optional[str]:
    """The decision this item is waiting on, or None if it waits on no one."""
    if item.state != "OPEN":
        return None
    if item.is_blocked:
        if item.needs == "agent":
            return None
        # A named condition is knowable work for the system, not a question for
        # Nate. A silent block still needs his attention, but only a project
        # can be parked; a ticket can only be unblocked.
        # A valid date condition is also machine-readable. Both future and
        # passed dates stay out of the question queue; the begin path clears a
        # passed condition before selecting work.
        # A well-formed event spec is also a named condition. Needs:
        # external-event routes the ticket, but cannot suppress the question
        # without that condition attached.
        if (
            item.block_event is not None
            or item.block_references
            or _item_blocked_until(item) is not None
        ):
            return None
        if item.parent is None and item.needs_decision:
            # An answered Gates question is settled, whatever the comment
            # thread still says. The marker is consulted before the question
            # is surfaced so the ask and the read share one predicate; an
            # absent or malformed marker asks exactly as before.
            if parse_gates_answer(item.body) is not None:
                return None
            return "Answer the breakdown's question?"
        return "Unblock?" if item.parent else "Unblock or park?"
    if item.status == "Building":
        # New work, replacements, and Nate-owned improvements stop for
        # acceptance. The same class/origin predicate drives the unattended
        # close path below, so the gate cannot drift from the writer.
        if not item.children_all_closed:
            return None
        if _can_close_itself(item):
            return None
        return GATES["Building"]
    if item.status == "Shaped":
        # The shape runner decides Ready versus Shaped at write time, so an
        # item at Shaped was held and waits on Nate. This reader must not
        # re-derive eligibility from plan prose: re-deriving could only hide
        # an item the writer held.
        waits_for_nate = (
            item.origin not in ORIGIN_OPTIONS
            or item.risk not in RISK_OPTIONS
            or item.needs not in NEEDS_OPTIONS
            or item.needs == "human"
            or item.risk == "escalated"
            or item.origin == "Nate"
        )
        return GATES["Shaped"] if waits_for_nate else None
    return None


def _acceptance_waiting_reason(
    item: Item, question: Optional[str]
) -> Optional[str]:
    """Explain why a completed Building project is still awaiting acceptance."""
    if question != GATES["Building"]:
        return None
    body = item.body if isinstance(item.body, str) else ""
    if ANALYSIS_MARKER in body:
        return "Analysis review"
    return "Ordinary accept"


def question_since(item: Item) -> Optional[datetime]:
    """When the item's current question became live.

    ``gate_question`` is the authority for which question is live. A Shaped
    project asks "Is the plan good?": the shape runner held it. A Building
    project starts asking "Accept it?" when its last
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

#: Capabilities belong to roles, not harness names. Keeping every implementer
#: in this one registry prevents a second inline literal from drifting.
#:
#: From 2026-09-18 to 2026-09-22 Muse implemented both tiers too, while
#: Codex's Plus week was nearly spent.
#: Who may implement at which tier. Codex implements both tiers from its
#: in-app automations; Muse judges and no longer implements (Nate,
#: 2026-09-22, #1315). `scripts/muse-implement` stays as the reversal path:
#: putting `muse` back here is the switch.
#: Claude implements on Saturday mornings only (#1557); `begin` enforces the
#: window. It takes the whole shared order, so ``None`` (untiered) is allowed.
AGENTS_BY_ROLE = {
    "implement": {
        "codex": frozenset(TIERS),
        "claude": frozenset(TIERS + (None,)),
    },
}


# A caller names its role so an agent that both reviews and implements (Muse
# did until #1322) is routed by what it asked for, not by its name. Keep the
# shorter role names canonical and accept the descriptive forms at the CLI
# boundary as well.
BEGIN_CALLER_ROLE_ALIASES = {
    "review": "review",
    "reviewer": "review",
    "implement": "implement",
    "implementer": "implement",
}
BEGIN_CALLER_ROLES = tuple(BEGIN_CALLER_ROLE_ALIASES)


def begin_uses_ticket_path(
    agent: str, tier: Optional[str], caller_role: Optional[str] = None
) -> bool:
    """Whether ``begin`` should open the implementation path.

    An omitted role retains the pre-existing agent capability lookup.  When a
    caller names its role, that declaration resolves the reviewer/implementer
    collision for agents such as Muse; the caller role is intentionally the
    source of routing in that case rather than the agent name.
    """
    if caller_role is None:
        # Muse's standard review schedule opens without a role in older
        # callers. Implementing at standard therefore needs the explicit
        # `--role implement`, so a reviewer that omits its role can never
        # become a writer (2026-09-18, when Muse gained the standard tier).
        if agent == "muse" and tier != "escalated":
            return False
        return agent_has_role(agent, "implement", tier)
    try:
        role = BEGIN_CALLER_ROLE_ALIASES[caller_role.casefold()]
    except (AttributeError, KeyError):
        raise ValueError(
            "unknown begin caller role {!r}; expected one of {}".format(
                caller_role, ", ".join(sorted(BEGIN_CALLER_ROLES))
            )
        )
    return role == "implement"


def agent_has_role(agent: str, role: str, tier: Optional[str]) -> bool:
    """Whether ``agent`` owns ``role`` in the requested execution tier."""
    tiers = AGENTS_BY_ROLE.get(role, {}).get(agent, frozenset())
    return tier in tiers


def _begin_role_refusal(agent: str, tier: Optional[str],
                        caller_role: Optional[str]) -> Optional[Dict[str, str]]:
    """Stop an implement caller whose agent does not implement at this tier.

    `begin_uses_ticket_path` routes a caller that declares `--role implement`
    by the declaration alone, so removing an agent from `AGENTS_BY_ROLE`
    would not stop its implement runner (#1322). This is the refusal that
    makes the roster the switch. An unknown role is left to the router,
    which rejects it.
    """
    if caller_role is None:
        return None
    role = BEGIN_CALLER_ROLE_ALIASES.get(str(caller_role).casefold())
    if role != "implement" or agent_has_role(agent, "implement", tier):
        return None
    return {
        "gate": "role",
        "do": "stop",
        "why": "{} does not implement {} work; AGENTS_BY_ROLE names who "
               "does (#1322)".format(agent, tier or "untiered"),
    }

#: Legacy risk lines remain import evidence for the canonical Risk field and
#: inputs to capture-time classification. Live routing reads the field only.
#:
#:     Risk: standard
#:     Risk: escalated — concurrency, destructive
RISK_LINE = re.compile(r"^\s*Risk:\s*(standard|escalated)\b(.*)$",
                       re.IGNORECASE | re.MULTILINE)

# A ticket's capability is its Needs Project field (#826): "none" means any
# agent may work it, "claude-code-environment" means only Claude Code may,
# and "human" means no agent may. The Human step body marker this replaced
# is deleted with its parser; readers check item.needs and nothing else.


def mark_projects_that_carried_human_steps(items: Sequence[Item]) -> None:
    """Derive project acceptance history from child ticket Needs fields.

    Closed child tickets remain in the Project item feed, so deriving this
    after all pages load preserves "ever carried" without persisting a second
    record that could drift from the field.
    """
    parent_refs = {
        item.parent
        for item in items
        if item.parent is not None
        and item.needs in ("human", "claude-code-environment")
    }
    for item in items:
        item.carried_human_step = item.ref in parent_refs


#: A safety boundary for capture and migration. False positives cost one
#: escalated review; false negatives can
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


#: A fenced block opens and closes with three or more backticks or tildes,
#: optionally followed by an info string on the opening fence.
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")

#: A block quote is a line whose first non-space character is ``>``.
_QUOTE_RE = re.compile(r"^\s*>")

#: A single-backtick code span must open and close on the same line. Runs of
#: multiple backticks and unmatched delimiters remain part of the scan.
_INLINE_CODE_RE = re.compile(r"(?<!`)`(?!`)[^`\n]*`(?!`)")



def asserted_text(text: str) -> str:
    """``text`` with quoted regions removed, keeping line structure.

    Fenced blocks and block quotes are things the item is *showing*: a pasted
    log line, a job name, an error string, an alternative a plan records that
    it will not take. Paired single-backtick spans on one line quote the same
    kind of evidence. They are not statements about what the work will do, and
    scanning them is how a report about a deadlock became a ticket with a
    concurrency risk.

    Lines are blanked rather than deleted, and inline spans are replaced with
    same-width spaces, so anchors and word boundaries keep their positions and
    a marker cannot be joined to the sentence above it. Unclosed or multiline
    backtick runs stay searchable because they do not prove a quoted span.

    Fences and block quotes are removed first; then only paired single-backtick
    spans contained on each remaining line are blanked. Everything else is
    scanned exactly as before.
    """
    if not text:
        return text or ""
    kept: List[str] = []
    fence: Optional[str] = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if fence is not None:
            # Inside a fence: the closing fence must use the same character.
            if match and match.group(1)[0] == fence[0]:
                fence = None
            kept.append("")
            continue
        if match:
            fence = match.group(1)
            kept.append("")
            continue
        kept.append("" if _QUOTE_RE.match(line) else line)
    return "\n".join(
        _INLINE_CODE_RE.sub(lambda span: " " * len(span.group(0)), line)
        for line in kept
    )


def escalation_matches(title: str, body: str,
                       failed_before: bool = False
                       ) -> List[Dict[str, Optional[str]]]:
    """Return escalation reasons with the line that supports each one.

    A legacy `Risk:` marker wins outright during classification — a ticket that
    says `Risk: standard` is standard even if its prose mentions a race
    condition, because the person who wrote the plan knew what it meant and a
    regex does not. Live queue and gate readers do not call this classifier;
    they read the canonical Project field.

    Only asserted prose is scanned. Quoted evidence — fenced blocks, block
    quotes, and paired single-backtick spans on one line — is excluded: words
    the item was reporting on rather than words describing its work must not
    change its tier. No pattern is removed, and the marker still outranks the
    regex in both directions.

    Each matched reason carries its first matching line, trimmed. The synthetic
    "prior attempt failed" reason has no matching line.
    """
    text = asserted_text("{}\n{}".format(title or "", body or ""))
    marker = RISK_LINE.search(text)
    if marker:
        if marker.group(1).lower() == "standard":
            return ([{"reason": "prior attempt failed", "line": None}]
                    if failed_before else [])
        stated = marker.group(2).strip(" —-:").strip()
        found = [{
            "reason": "declared: " + stated if stated else "declared",
            "line": marker.group(0).strip(),
        }]
        if failed_before:
            found.append({"reason": "prior attempt failed", "line": None})
        return found

    found: List[Dict[str, Optional[str]]] = []
    for name, pattern in sorted(ESCALATION_PATTERNS.items()):
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.start())
        if line_end < 0:
            line_end = len(text)
        found.append({
            "reason": name,
            "line": text[line_start:line_end].strip(),
        })
    if failed_before:
        found.append({"reason": "prior attempt failed", "line": None})
    return found


def escalation_reasons(title: str, body: str,
                       failed_before: bool = False) -> List[str]:
    """Return the stable list of escalation reason names."""
    return [entry["reason"] for entry in
            escalation_matches(title, body, failed_before)
            if isinstance(entry.get("reason"), str)]


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


def plan_is_escalated(plan_body: str) -> List[str]:
    """Return plan escalation reason names using the plan-only scan region."""
    return [entry["reason"] for entry in plan_escalation_matches(plan_body)
            if isinstance(entry.get("reason"), str)]


_PLAN_ATX_HEADING_RE = re.compile(
    r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<title>.*?)|[ \t]*)$"
)
_PLAN_MALFORMED_ATX_RE = re.compile(
    r"^ {0,3}(?:#{7,}|#{1,6}(?!#)\S).*$"
)
_PLAN_MALFORMED_REJECTED_RE = re.compile(
    r"^ {0,3}#{1,6}(?!#)[ \t]*Rejected\b", re.IGNORECASE
)
_PLAN_REJECTED_INLINE_RE = re.compile(
    r"[ \t]+\(rejected:[^\r\n]*\)[ \t]*$", re.IGNORECASE
)


def _plan_escalation_scan_text(plan_body: str) -> str:
    """Remove plan-only rejected prose before using the shared word matcher.

    A malformed Rejected heading or a malformed heading inside its section
    makes the section boundary ambiguous. In that case keep the original body
    intact so an uncertain parse cannot hide a scan hit.
    """
    body = plan_body or ""
    raw_lines = body.splitlines(keepends=True)
    visible_lines = asserted_text(body).splitlines()
    if len(visible_lines) > len(raw_lines):
        return body
    visible_lines.extend([""] * (len(raw_lines) - len(visible_lines)))

    headings = []
    malformed = []
    rejected_sections = []
    malformed_rejected = False
    for index, line in enumerate(visible_lines):
        match = _PLAN_ATX_HEADING_RE.match(line)
        if match:
            level = len(match.group("hashes"))
            title = (match.group("title") or "").strip()
            title = re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()
            headings.append((index, level, title))
            if re.match(r"(?i)^rejected\b", title):
                if level != 2 or title != "Rejected":
                    malformed_rejected = True
                else:
                    rejected_sections.append(index)
        elif _PLAN_MALFORMED_ATX_RE.match(line):
            malformed.append(index)
            if _PLAN_MALFORMED_REJECTED_RE.match(line):
                malformed_rejected = True

    if malformed_rejected or len(rejected_sections) > 1:
        return body

    start = rejected_sections[0] if rejected_sections else None
    end = len(raw_lines)
    if start is not None:
        end = next((index for index, level, _ in headings
                    if index > start and level <= 2), len(raw_lines))
        if any(start < index < end for index in malformed):
            return body

    agent_decision_lines = set()
    for section_start, level, title in headings:
        if level != 2 or title != "Decided by the agent":
            continue
        section_end = next((index for index, next_level, _ in headings
                            if index > section_start and next_level <= 2),
                           len(raw_lines))
        if any(section_start < index < section_end for index in malformed):
            continue
        agent_decision_lines.update(range(section_start + 1, section_end))

    for index, line in enumerate(raw_lines):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        if start is not None and start <= index < end:
            raw_lines[index] = ending
            continue
        if index in agent_decision_lines:
            content = _PLAN_REJECTED_INLINE_RE.sub(
                lambda match: " " * len(match.group(0)), content
            )
        raw_lines[index] = content + ending
    return "".join(raw_lines)


def plan_escalation_matches(plan_body: str
                            ) -> List[Dict[str, Optional[str]]]:
    """Return plan risks after excluding its recorded rejected alternatives.

    Plan-section knowledge stays here; ticket text still uses the canonical
    matcher unchanged. If a Rejected boundary cannot be parsed, the whole body
    is scanned as written.
    """
    return escalation_matches("", _plan_escalation_scan_text(plan_body))


def plan_needs_nate(plan_body: str) -> bool:
    """Whether rendered prose retains an unanswered Needs Nate question.

    Canonical routing reads the Project field. This reader is limited to the
    sanctioned Gates-answer edit, where it decides whether another visible
    question remains after replacing that one line. New plans omit null
    categories and the whole section when all are null. Legacy all-clear
    lines remain readable until migration prose has been trimmed.
    """
    text = plan_body or ""
    headings = list(re.finditer(
        r"(?im)^##[ \t]+Needs[ \t]+(?:Nate|you)[ \t]*$", text
    ))
    if not headings:
        return False
    if len(headings) != 1:
        return True

    section_start = headings[0].end()
    section_boundary = re.search(
        r"(?m)^#{1,6}[ \t]+|^[ \t]*<!-- command-center-[\w-]+ -->[ \t]*$",
        text[section_start:],
    )
    section_end = (
        section_start + section_boundary.start()
        if section_boundary else len(text)
    )
    section = text[section_start:section_end]
    category_line = re.compile(
        r"(?im)^[ \t]*(?:-[ \t]*)?"
        r"(?P<category>Exposure|Gates|Scope(?:[ \t]+and[ \t]+priority)?|"
        r"Preference)[ \t]*:[ \t]*(?P<answer>.*?)\s*$"
    )
    answers: Dict[str, str] = {}
    for line in section.splitlines():
        if not line.strip():
            continue
        match = category_line.fullmatch(line)
        if match is None:
            return True
        category = match.group("category").casefold()
        category = "scope" if category.startswith("scope") else category
        if category in answers:
            return True
        answers[category] = match.group("answer").strip()

    return any(
        not (
            re.match(r"(?i)^nothing outstanding\b", answer)
            or re.match(r"(?i)^answered\b", answer)
        )
        for answer in answers.values()
    )


SHAPING_PLAN_STATUSES = frozenset(("Shaped", "Ready", "Building"))


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
    if origin_voice in ("Nate", "nate-direct", "nate-relayed"):
        return "nate"
    return "nate"


def self_approval_eligible(klass: Optional[str], origin_voice: Optional[str],
                           override_target: Optional[str], *,
                           needs_nate: bool, escalated: bool,
                           state: Optional[str] = None) -> bool:
    """Whether all conditions permit one unattended shaping transition.

    #77 supplies the plan booleans and #80 owns the transition. Keeping class,
    origin, the Needs-section result, and escalation in this one predicate
    prevents origin from becoming a second gate that can drift from the
    existing self-approval rule. Authority signals remain advisory record
    data and are intentionally not a predicate term.

    ``state`` is the issue's GitHub state, and a closed issue is never
    self-approvable: advancing one to ``Ready`` puts it in the startable queue
    with nothing able to close it again (#1206). It is widened here rather
    than checked by a second predicate for the same reason the rest of the
    rule lives in one place — a parallel eligibility test is a gate that can
    drift. ``None`` means the caller has no state to offer and leaves the rule
    exactly as it was.
    """
    if state is not None and str(state).upper() != "OPEN":
        return False
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
    needs = item.needs
    machine_local = needs == "claude-code-environment"
    human = needs == "human"
    if (
        item.state != "OPEN"
        or item.is_blocked
        or item.open_blockers
        or item.children_total
        or needs not in NEEDS_OPTIONS
        # The Needs field is the only capability signal (#826). A
        # claude-code-environment ticket is the middle outcome: Claude Code
        # may work it, while every other requester must leave it in the
        # queue. A human ticket is excluded from every agent, as #141
        # established for the marker this field replaced.
        or (human or (machine_local and agent != "claude"))
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
    # work began — `cmd_claim` writes it on the first claim. A parent without a
    # valid Class is the plan.md-invalid, not-startable case; do not let its
    # residual ticket silently promote it to Building.
    return (
        parent.status in ("Ready", "Building")
        and parent.klass in LADDER
        and not parent.is_blocked
    )


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


#: The issue whose landing ends the frozen-ground queue rule. The freeze is
#: owned by #794 alone: #1044 measures the cutover but its closing changes
#: nothing here.
FREEZE_OWNER_REF = REPO + "#794"

#: Section headers a ticket body may carry (skills/breakdown). Only What and
#: Accept describe the work; the rest name context, not files to change.
_TICKET_SECTION_RE = re.compile(
    r"(?m)^\s*(Parent|Depends on|What|Accept|Risk|Sequencing|Human step)\s*:"
)

#: Canonical freeze lists live in ``engine/review.py``. This module reads
#: them at run time through a lazy import inside ``_canonical_freeze_lists``,
#: so no copy of the lists lives here. #794 closeout deletes this adapter
#: together with the review-side freeze row, so neither outlives the freeze.
def _canonical_freeze_lists(
) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[int, ...]]:
    """The canonical freeze lists, parsed out of the review engine source.

    The package dependency runs one way only — the review package reads
    this module, never the reverse — so this adapter parses the three
    assignments out of the ``review.py`` file as text instead of importing
    them. Returns (paths, parsers, exempt parents) as tuples, so the queue
    predicate can never drift from the freeze row it mirrors.
    """
    import ast
    source_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "engine", "review.py")
    with open(source_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    wanted = ("FROZEN_PATHS", "FROZEN_PARSERS", "FREEZE_PARENT_NUMBERS")
    values: Dict[str, object] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in wanted):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    return (tuple(values["FROZEN_PATHS"]),  # type: ignore[arg-type]
            tuple(values["FROZEN_PARSERS"]),  # type: ignore[arg-type]
            tuple(values["FREEZE_PARENT_NUMBERS"]))  # type: ignore[arg-type]


def _freeze_governing(
    by_ref: Mapping[str, Item]
) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[int, ...]]]:
    """The freeze lists while the freeze governs, else None.

    The predicate goes inert when the #794 issue itself has closed, so a
    landed freeze releases the queue even before its code is removed.
    Anything else — including #794 missing this read, as in fixture-only
    callers — reads as active, so a partial load can never quietly offer
    frozen work.
    """
    owner = by_ref.get(FREEZE_OWNER_REF)
    if owner is not None and owner.state != "OPEN":
        return None
    return _canonical_freeze_lists()


def freeze_active(by_ref: Mapping[str, Item]) -> bool:
    """Whether the #794 frozen-ground rule still withholds queue tickets."""
    return _freeze_governing(by_ref) is not None


def _ticket_work_text(body: Optional[str]) -> str:
    """The What and Accept sections of a ticket body.

    Anything else names context, not work: #1187's Sequencing paragraph
    mentions routines/muse.md as the rollback that stays, not as a file to
    change. A body with no recognisable sections is scanned whole, so a
    malformed body cannot smuggle frozen work past the matcher.
    """
    if not isinstance(body, str) or not body.strip():
        return ""
    matches = list(_TICKET_SECTION_RE.finditer(body))
    if not any(match.group(1) in ("What", "Accept") for match in matches):
        return body
    parts = []
    for index, match in enumerate(matches):
        if match.group(1) not in ("What", "Accept"):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        parts.append(body[match.end():end])
    return "\n".join(parts)


def freeze_markers(body: Optional[str]) -> List[str]:
    """Frozen-ground markers a ticket body names in What or Accept.

    Paths match the canonical frozen prefixes; parser names match the
    canonical parser tuple by substring, exactly as the review-side freeze
    row's diff scan does. The match is deliberately conservative: any hit
    withholds, because a false positive costs one reason line while a
    false negative costs a full run ending in a certain rejection.
    """
    text = _ticket_work_text(body)
    if not text:
        return []
    paths, parsers, _exempt = _canonical_freeze_lists()
    found: Set[str] = set()
    if paths:
        pattern = re.compile(
            "(?:{})[^\\s`'\"(),;:!?]*".format(
                "|".join(re.escape(prefix) for prefix in paths)))
        for match in pattern.finditer(text):
            found.add(match.group(0).rstrip("."))
    for name in parsers:
        if name and name in text:
            found.add(name)
    return sorted(found)


FREEZE_BLOCKER_URL = "https://github.com/{}/issues/794".format(REPO)
FREEZE_BLOCKER_NUMBER = FREEZE_BLOCKER_URL.rsplit("/", 1)[-1]
FREEZE_BLOCKER_SENTENCE = (
    "Blocked on #794: the frozen-ground freeze must end before this work starts."
)


def _gh_option_value(command: Sequence[str], option: str) -> Optional[str]:
    """Return one `gh` option value in either supported spelling."""
    for index, part in enumerate(command):
        if part == option:
            if index + 1 < len(command):
                return command[index + 1]
            return None
        if part.startswith(option + "="):
            return part[len(option) + 1:]
    return None


def _append_gh_csv_option(command: List[str], option: str, value: str,
                          repo: str) -> None:
    """Add a native issue-reference option without repeating an existing edge."""
    target = value
    same_repo_number = target.rsplit("/", 1)[-1]
    if repo == REPO:
        target = same_repo_number

    for index, part in enumerate(command):
        if part == option and index + 1 < len(command):
            values = [entry.strip() for entry in command[index + 1].split(",")
                      if entry.strip()]
            if not _gh_blocker_has_freeze_edge(values, repo):
                values.append(target)
            command[index + 1] = ",".join(values)
            return
        if part.startswith(option + "="):
            values = [entry.strip() for entry in
                      part[len(option) + 1:].split(",") if entry.strip()]
            if not _gh_blocker_has_freeze_edge(values, repo):
                values.append(target)
            command[index] = option + "=" + ",".join(values)
            return
    command.extend([option, target])


def _gh_blocker_has_freeze_edge(values: Sequence[str], repo: str) -> bool:
    """Whether a create command already names the #794 issue as a blocker."""
    for value in values:
        if value == FREEZE_BLOCKER_URL:
            return True
        if repo == REPO and value == FREEZE_BLOCKER_NUMBER:
            return True
        if re.search(r"/issues/{}/?\Z".format(FREEZE_BLOCKER_NUMBER), value):
            return True
    return False


def _body_with_freeze_blocker(body: str) -> str:
    """Keep a readable #794 dependency beside the native blocker edge."""
    if FREEZE_BLOCKER_SENTENCE in body:
        return body
    note = FREEZE_BLOCKER_SENTENCE
    risk = RISK_LINE.search(body)
    if risk:
        return (body[:risk.start()].rstrip() + "\n\n" + note + "\n\n"
                + body[risk.start():].lstrip())
    return body.rstrip() + "\n\n" + note


def _frozen_ticket_create_command(command: List[str]) -> List[str]:
    """Make a child issue blocked on #794 when its work names frozen ground.

    The breakdown engine creates sub-issues through ``_run_gh``. Keeping the
    check at that boundary lets the breakdown implementation reuse the exact
    matcher and source adapter used by ``startable()`` without a second list.
    """
    if command[:3] != ["gh", "issue", "create"]:
        return command
    raw_parent = _gh_option_value(command, "--parent")
    if raw_parent is None:
        return command
    try:
        parent_number = int(raw_parent)
    except (TypeError, ValueError):
        raise GitHubError(
            "cannot check the frozen-ground rule: invalid sub-issue parent {!r}"
            .format(raw_parent))
    _paths, _parsers, exempt = _canonical_freeze_lists()
    if parent_number in exempt:
        return command
    body = _gh_option_value(command, "--body")
    markers = freeze_markers(body)
    if not markers:
        return command

    owner = _gh_json(
        "gh", "issue", "view", "794", "--repo", REPO, "--json", "state")
    if not isinstance(owner, dict) or not isinstance(owner.get("state"), str):
        raise GitHubError(
            "could not verify whether the #794 frozen-ground freeze is active")
    state = owner["state"].upper()
    if state == "CLOSED":
        return command
    if state != "OPEN":
        raise GitHubError(
            "could not verify whether the #794 frozen-ground freeze is active")

    repo = _gh_option_value(command, "--repo") or REPO
    _append_gh_csv_option(command, "--blocked-by", FREEZE_BLOCKER_URL, repo)
    for index, part in enumerate(command):
        if part == "--body" and index + 1 < len(command):
            command[index + 1] = _body_with_freeze_blocker(command[index + 1])
            break
        if part.startswith("--body="):
            command[index] = "--body=" + _body_with_freeze_blocker(
                part[len("--body="):])
            break
    return command


def _parent_ticket_number(item: Item) -> Optional[int]:
    """The ticket's parent issue number, or None when it cannot be read."""
    parent = getattr(item, "parent", None)
    if not isinstance(parent, str) or "#" not in parent:
        return None
    try:
        return int(parent.rsplit("#", 1)[1])
    except ValueError:
        return None


#: Consecutive failed runs on one piece of work before it is withheld.
#: Measured 2026-09-21 over the heartbeat's 10,000-record window: 27 of 493
#: bound works recorded two or more errored finishes, and the worst were
#: FF-Weekly-Start-Sit#208 (11 errored in 23.1 hours, one success), The-League#186
#: (9 in 21.8 hours), the shape job on command-center#1178 (7 in 4.7 hours) and
#: The-League#225 (5 in 5.9 hours with no success at all). Three is the first
#: count that cuts those runs materially while leaving a transient — one bad
#: GitHub response, one rate limit — to resolve itself on the next pass.
BACKOFF_FAILURES = 3

#: How long a backed-off job waits before it is offered again. The observed
#: retry cadence is 40 minutes to 2 hours, so six hours turns the 11-attempt
#: day above into about five while never parking work overnight. A further
#: failure restarts it; any non-errored finish clears it outright.
#:
#: Both constants are scheduling arithmetic, and re-tuning them is the whole
#: rollback: nothing is stored, and the count is derived from the heartbeat
#: every run.
BACKOFF_COOLDOWN = timedelta(hours=6)


def consecutive_failures(
    rows: Sequence[Mapping[str, object]]
) -> Dict[str, Tuple[int, float]]:
    """Trailing failed runs per work, as ``{work: (count, last_failure)}``.

    Counted newest-first and stopped at the first finish that was not an
    error, so a job that failed four times and then succeeded reads as zero.
    A skip is not a failure and does not break the run either — a lane that
    stood down for the budget says nothing about whether the work is broken —
    so only ``errored`` counts and only ``done`` clears.

    Pure over heartbeat rows. The caller reads them; this decides nothing
    about GitHub and stores nothing.
    """
    import heartbeat

    bound = heartbeat.bindings(list(rows))
    finishes: Dict[str, List[Tuple[float, str]]] = {}
    for row in rows:
        if row.get("phase") != "finish":
            continue
        binding = bound.get(row.get("run"))
        work = binding.get("work") if binding else None
        if not work:
            continue
        stamp = row.get("ts")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        outcome = str(row.get("outcome") or "")
        finishes.setdefault(str(work), []).append((float(stamp), outcome))

    found: Dict[str, Tuple[int, float]] = {}
    for work, history in finishes.items():
        history.sort(key=lambda entry: entry[0], reverse=True)
        count = 0
        last = 0.0
        for stamp, outcome in history:
            if outcome == "errored":
                count += 1
                last = max(last, stamp)
                continue
            if outcome == "done":
                break
            # Anything else — a skip, a quota park — is neither a failure nor
            # a success, and the run of failures continues through it.
        if count:
            found[work] = (count, last)
    return found


def backoff_withheld(
    rows: Sequence[Mapping[str, object]], now: datetime
) -> Dict[str, Dict[str, object]]:
    """Work withheld by repeated failure, by ref, with the reason to say.

    A withheld job carries its count and the condition that releases it —
    never a silent hold, which is the state `plan.md` rejects. It is offered
    again the moment either condition is met: the cooldown passes, or a run
    finishes it successfully.
    """
    found: Dict[str, Dict[str, object]] = {}
    for work, (count, last) in consecutive_failures(rows).items():
        if count < BACKOFF_FAILURES:
            continue
        until = datetime.fromtimestamp(last, timezone.utc) + BACKOFF_COOLDOWN
        if until <= now:
            continue
        found[work] = {
            "ref": work,
            "failures": count,
            "until": until,
            "reason": (
                "backoff: {} consecutive failed runs, the last at {}; "
                "startable again after {} or as soon as a run finishes it"
                .format(count,
                        datetime.fromtimestamp(last, timezone.utc).isoformat(),
                        until.isoformat())
            ),
        }
    return found


def backoff_withheld_rows(
    items: Sequence[Item],
    rows: Sequence[Mapping[str, object]],
    now: datetime,
) -> List[Dict[str, object]]:
    """The diagnostic twin of the ``startable()`` exclusion, like freeze's.

    Only work still in the funnel is reported: a backed-off job whose ticket
    has since closed is history, not a withheld queue entry.
    """
    open_refs = {item.ref for item in items if item.state == "OPEN"}
    return sorted(
        (row for ref, row in backoff_withheld(rows, now).items()
         if ref in open_refs),
        key=lambda row: str(row["ref"]),
    )


def freeze_withhold_reason(
    item: Item, by_ref: Mapping[str, Item]
) -> Optional[str]:
    """Why the #794 frozen-ground rule withholds this ticket, or None.

    Mirrors ``engine/review.py`` precheck_freeze, applied to the ticket's
    What and Accept text instead of a diff: a frozen marker named under a
    non-exempt parent withholds while the freeze is active. The reason names
    the marker and the parent number, so the queue never withholds silently.
    """
    governing = _freeze_governing(by_ref)
    if governing is None:
        return None
    _paths, _parsers, exempt = governing
    parent_number = _parent_ticket_number(item)
    if parent_number in exempt:
        return None
    markers = freeze_markers(getattr(item, "body", None))
    if not markers:
        return None
    allowed = ", ".join("#{}".format(n) for n in exempt)
    if parent_number is None:
        where = "ticket {} has no parent number".format(item.ref)
    else:
        where = "ticket {} is under #{}".format(item.ref, parent_number)
    return ("freeze: {} named but {}; frozen while #794 lands "
            "(exempt parents: {})".format(", ".join(markers), where, allowed))


def freeze_withheld(
    items: Sequence[Item],
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
    awaiting_review: Optional[Set[str]] = None,
    agent: str = "codex",
) -> List[Dict[str, object]]:
    """Otherwise-eligible tickets the #794 frozen-ground rule withholds.

    The diagnostic twin of the ``startable()`` exclusion, shaped after
    ``readiness_blockers``: callers that find an empty queue can say why
    without changing the shared ordering. Each row names the frozen marker
    and the parent number, so a withheld ticket never goes stale invisibly.
    """
    awaiting_review = awaiting_review or frozenset()
    rows = list(items)
    by_ref = {i.ref: i for i in rows}
    if not freeze_active(by_ref):
        return []
    found: List[Dict[str, object]] = []
    for item in rows:
        if not _startable_without_repo_readiness(
            item, by_ref, awaiting_review, agent
        ):
            continue
        if _repo_blocking_reasons(item, repo_readiness):
            continue
        reason = freeze_withhold_reason(item, by_ref)
        if reason is None:
            continue
        found.append({
            "ref": item.ref,
            "repo": item.repo,
            "parent": _parent_ticket_number(item),
            "markers": freeze_markers(getattr(item, "body", None)),
            "reason": reason,
        })
    return sorted(found, key=lambda row: (str(row["repo"]), str(row["ref"])))


def _freeze_withheld_summary(withheld: Sequence[Dict[str, object]]) -> str:
    """Render the stable queue-empty explanation for frozen-ground rows."""
    return "tickets withheld by frozen ground — {}".format(
        "; ".join(str(row["reason"]) for row in withheld)
    )


def queue_classes(
    items: Sequence[Item],
    descendants: Optional[Mapping[str, Set[str]]] = None,
) -> Dict[str, Optional[str]]:
    """The class each item ranks as in ``startable()``.

    A ticket inherits its project's class, and a ticket that blocks work
    higher on the ladder, directly or down a chain, ranks with that work.
    ``None`` is an unset Class, which sorts last.
    """
    by_ref = {i.ref: i for i in items}
    if descendants is None:
        descendants = dependency_descendants(items)
    return {
        ref: min(
            (
                effective_class(by_ref[related], by_ref)
                for related in {ref} | set(descendants[ref])
            ),
            key=ladder_index,
        )
        for ref in by_ref
    }


def startable(
    items: Sequence[Item],
    awaiting_review: Optional[Set[str]] = None,
    agent: str = "codex",
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
    backed_off: Optional[Mapping[str, object]] = None,
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

    Tickets naming frozen ground in What or Accept are withheld while the
    #794 freeze governs (see ``freeze_withhold_reason``): offering them
    would only fail at review, on a diff that does not exist until a run
    has already built it.

    ``backed_off`` holds refs withheld by repeated failure (see
    ``backoff_withheld``). Offering a job that has failed three times in a row
    spends a whole run to fail a fourth: FF-Weekly-Start-Sit#208 was offered
    eleven times in 23 hours and errored on every one but the last.
    """
    awaiting_review = awaiting_review or frozenset()
    backed_off = backed_off or {}
    by_ref = {i.ref: i for i in items}
    descendants = dependency_descendants(items)
    effective_rank = {
        ref: ladder_index(klass)
        for ref, klass in queue_classes(items, descendants).items()
    }
    # A ticket that blocks higher-tier work takes that tier, as it takes the
    # class above: otherwise tier-1 work would wait on its own prerequisite.
    effective_tier = {
        ref: min(
            repo_tier(by_ref[related].repo)
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
        if _repo_blocking_reasons(item, repo_readiness):
            return False
        # Repeated failure is passed in rather than read here, like
        # `awaiting_review`: the count lives in the heartbeat and this
        # function stays pure over Items and testable from fixtures.
        if item.ref in backed_off:
            return False
        return freeze_withhold_reason(item, by_ref) is None

    def in_flight(item: Item) -> bool:
        """Once a project is Building, its remaining tickets finish first.

        Passing a gate is a commitment; nothing may silently un-commit it.
        """
        parent = by_ref.get(item.parent or "")
        return (parent.status if parent else item.status) == "Building"

    def pinned_ancestor(item: Item) -> bool:
        """Whether the project this ticket belongs to is pinned.

        A pin is Nate's explicit ordering call, and until 2026-09-12 it reached
        only his decision queue: a pinned project's ticket sat 18th behind older
        Broken work while he asked why (#673). It now outranks the ladder's
        default order. It orders and nothing more -- the WIP-cap preemption
        stays with the finite classes, and a pin never unblocks or unlocks.
        """
        seen: Set[str] = set()
        current: Optional[Item] = item
        while current is not None and current.ref not in seen:
            if current.pinned:
                return True
            seen.add(current.ref)
            current = by_ref.get(current.parent or "")
        return False

    def key(item: Item):
        since = question_since(item) or datetime.max.replace(tzinfo=timezone.utc)
        return (
            # Finite classes preempt in-flight work of unbounded ones — the half
            # of plan.md's rule this key never implemented until #435. Measured
            # 2026-09-09: six Broken projects at Ready sat behind ten in-flight
            # Improve tickets all afternoon. Read through `effective_rank` so a
            # ticket that blocks a Broken one preempts with it. Finite work
            # leads a pin (Nate, 2026-09-25).
            0 if preempting[item.ref] else 1,
            0 if pinned_ancestor(item) else 1,
            # Above the Building commitment: higher-tier work need not wait
            # for a lower tier's in-flight project (Nate, 2026-09-25).
            effective_tier[item.ref],
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


def projected_pull_order(
    items: Sequence[Item], now: Optional[datetime] = None,
    paused: Collection[str] = (),
) -> List[str]:
    """Every ticket's projected turn, found by running ``startable()`` forward.

    This is the dashboard's order: "an accurate representation of what comes
    next, not a string of contrived rules" (Nate, 2026-09-24). Each round
    takes the ticket ``startable()`` ranks first, counts it done, and lifts
    what that frees: a native edge on it, a labelled block whose every
    condition has now cleared, and a project ref once all of that project's
    tickets are done. A Ready project moves to Building on its first turn,
    as ``claim`` would move it.

    Work already under way -- a PR in review, a human or Claude Code step --
    queues with everything else under the same key, because it is being
    taken now. What never becomes startable (a future date, a reason with no
    reference, a blocker off the board, a missing Class) gets no turn and is
    absent from the list. The items are copied; nothing is written.

    ``paused`` names tickets held by ``backoff_withheld`` after repeated
    failed runs. The engineers will not take them before the hold lifts, so
    they wait until everything available now has had its turn (Nate,
    2026-09-25: a row must never claim a next step the engineers won't take).
    """
    sim = [copy.copy(item) for item in items]
    by_ref = {item.ref: item for item in sim}
    for item in sim:
        if item.needs in ("human", "claude-code-environment"):
            item.needs = "none"
    done: Set[str] = set()

    def lift_blocks() -> None:
        for item in sim:
            if item.state == "OPEN" and item.open_blockers:
                item.open_blockers = [
                    ref for ref in item.open_blockers if ref not in done
                ]
            if (
                item.state == "OPEN" and item.is_blocked
                and satisfied_block_refs(item, by_ref, now=now) is not None
            ):
                item.labels = [
                    label for label in item.labels if label != "blocked"
                ]

    def finish(item: Item) -> None:
        item.state = "CLOSED"
        done.add(item.ref)
        parent = by_ref.get(item.parent or "")
        if parent is None:
            return
        parent.children_done += 1
        if parent.status == "Ready":
            parent.status = "Building"
        if (
            parent.state == "OPEN"
            and parent.children_done >= parent.children_total
        ):
            parent.state = "CLOSED"
            done.add(parent.ref)

    order: List[str] = []
    held = {ref: {} for ref in paused}
    lift_blocks()
    for _ in range(len(sim) + 2):
        queue = startable(sim, backed_off=held)
        if not queue and held:
            held = {}
            continue
        if not queue:
            break
        order.append(queue[0].ref)
        finish(queue[0])
        lift_blocks()
    return order


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

#: A reviewer that writes prose and then acts leaves nothing a later step can
#: check. That is how a merge became something a model simply decided to do, and
#: how a PR with requested changes ended up owned by nobody (#39).
VERDICTS = ("approved", "rejected")
CI_COULD_NOT_RUN = "could-not-run"
CI_STATES = ("green", "red", "unknown", CI_COULD_NOT_RUN)

#: Conclusions GitHub reports for a check that passed or was excused.
CI_SUCCESS_CONCLUSIONS = ("SUCCESS", "NEUTRAL", "SKIPPED")

#: States GitHub reports for a check that has not reached a conclusion yet.
CI_PENDING_STATES = ("PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING",
                     "REQUESTED", "STALE")

# GitHub has used more than one spelling for the same Actions startup stop.
# Match the stable meaning, not one complete annotation sentence: the account
# notice has changed between "payments have failed" and "spending limit".
CI_STARTUP_MARKERS = (
    "startup failure",
    "startup failed",
)
CI_BILLING_MARKERS = (
    "billing",
    "payment failed",
    "payments failed",
    "spending limit",
    "spend limit",
)

#: A self-hosted runner that stops answering fails the job with an annotation
#: and no test output at all. Run 35546576274 on main at a3c97b14 died this way
#: after ten minutes; the same SHA went green on a manual rerun fifteen minutes
#: later, and main stayed red for 2h12m in between because nothing read the
#: annotation (#1178). These phrases are GitHub's own wording for the
#: condition, kept narrow on purpose: a real test failure must never match one.
CI_LOST_RUNNER_MARKERS = (
    "lost communication with the server",
    "lost contact with the server",
    "has not been able to communicate with the server",
    "the runner has received a shutdown signal",
)
# Deliberately absent: "the operation was canceled". A cancellation is
# ambiguous — a person pressing cancel looks exactly like a host dying — and
# the plan's rule is that anything ambiguous reads as a real failure.


def _ci_result(check: Mapping[str, object]) -> Optional[str]:
    """Return a rollup result in the case-insensitive wire vocabulary."""
    value = check.get("conclusion") or check.get("state")
    if value in (None, ""):
        return None
    return str(value).upper()


def _ci_annotation_texts(value: object) -> List[str]:
    """Extract human-readable annotation text from common GitHub shapes."""
    found: List[str] = []

    def add(text: object) -> None:
        if not isinstance(text, str):
            return
        text = text.strip()
        if text and text not in found:
            found.append(text)

    def visit(node: object) -> None:
        if isinstance(node, str):
            add(node)
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
            return
        if not isinstance(node, Mapping):
            return
        for key in (
            "message", "title", "raw_details", "rawDetails",
            "description", "text",
        ):
            if key in node:
                visit(node[key])
        for key in ("annotation", "annotations", "check_run", "checkRun"):
            if key in node:
                visit(node[key])
        if "nodes" in node:
            visit(node["nodes"])

    visit(value)
    return found


def _ci_annotation_is_startup_or_billing(text: str) -> bool:
    normalized = re.sub(r"[-_]+", " ", text.casefold())
    return (
        any(marker in normalized for marker in CI_STARTUP_MARKERS)
        or any(marker in normalized for marker in CI_BILLING_MARKERS)
        or ("payment" in normalized and "fail" in normalized)
    )


def _ci_annotation_is_lost_runner(text: str) -> bool:
    """Whether one annotation names a runner that stopped answering."""
    normalized = re.sub(r"[-_]+", " ", text.casefold())
    return any(marker in normalized for marker in CI_LOST_RUNNER_MARKERS)


def _ci_completed_steps(value: Mapping[str, object]) -> Optional[int]:
    """Read an explicit or nested completed-step count, if one is present."""
    for key in ("completed_steps", "completedSteps"):
        count = value.get(key)
        if isinstance(count, int) and not isinstance(count, bool):
            return count
        if isinstance(count, str) and count.strip().isdigit():
            return int(count.strip())

    def completed(step: object) -> bool:
        if not isinstance(step, Mapping):
            return False
        status = str(step.get("status") or "").upper()
        conclusion = step.get("conclusion")
        return (
            status == "COMPLETED"
            or conclusion not in (None, "")
            or step.get("completedAt") not in (None, "")
            or step.get("completed_at") not in (None, "")
        )

    if "steps" in value:
        steps = value.get("steps")
        if isinstance(steps, list):
            return sum(1 for step in steps if completed(step))
        return None

    if "jobs" in value:
        jobs = value.get("jobs")
        if not isinstance(jobs, list):
            return None
        if not jobs:
            return 0
        saw_steps = False
        count = 0
        for job in jobs:
            if not isinstance(job, Mapping) or "steps" not in job:
                continue
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            saw_steps = True
            count += sum(1 for step in steps if completed(step))
        return count if saw_steps else None
    return None


def ci_could_not_run_reasons(checks: Sequence[dict]) -> List[str]:
    """Return startup/account-stop evidence from a check or run rollup.

    A failure with an explicitly empty step set is the stable no-start shape.
    An annotation naming startup or billing is independently sufficient because
    GitHub has changed which fields it populates when Actions never starts.
    """
    reasons: List[str] = []
    for check in checks or []:
        if not isinstance(check, Mapping):
            continue
        annotations = [
            text for text in _ci_annotation_texts(check)
            if _ci_annotation_is_startup_or_billing(text)
        ]
        result = _ci_result(check)
        if annotations:
            candidates = annotations
        elif result == "FAILURE" and _ci_completed_steps(check) == 0:
            candidates = ["failure with zero completed steps"]
        else:
            candidates = []
        for reason in candidates:
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def ci_could_not_run_reason(checks: Sequence[dict]) -> Optional[str]:
    """The first startup/account explanation, or ``None``."""
    reasons = ci_could_not_run_reasons(checks)
    return reasons[0] if reasons else None


def ci_rollup_state(checks: Sequence[dict]) -> str:
    """Classify a rollup as green, red, unknown, or ``could-not-run``.

    The one place that reads a rollup, so the review packet, the merge gate and
    the review queue cannot drift apart about what CI said. Any reported
    conclusion outside the success set is ``red``. Anything unfinished, or no
    checks at all, is ``unknown`` rather than green — an absent signal must
    never read as a passing one. Tolerates both wire shapes: CheckRun
    (``conclusion``/``status``) and Status (``state``/``context``).
    """
    if not checks:
        return "unknown"
    pending = False
    could_not_run = False
    for check in checks:
        if not isinstance(check, Mapping):
            continue
        result = _ci_result(check)
        could_reason = ci_could_not_run_reason([check])
        if could_reason is not None:
            could_not_run = True
        # A pending state is read before the red rule, or it would fall into
        # it: "PENDING" is not in the success set either. The engine's reader
        # had this the other way round, so a Status-shaped rollup that had not
        # reported yet came back red while the CheckRun shape came back
        # unknown. Both now say unknown, which is what both docstrings claimed.
        if result is not None and result in CI_PENDING_STATES:
            pending = True
            continue
        if result in CI_SUCCESS_CONCLUSIONS or result is None:
            if result is None:
                status = str(check.get("status") or "").upper()
                if status and status != "COMPLETED":
                    pending = True
                elif not status:
                    # A bare entry with neither conclusion nor status carries
                    # no signal yet.
                    pending = True
            continue
        if could_reason is not None:
            # An explicit startup/account annotation is stronger than the
            # generic red conclusion.  A genuine failure without that evidence
            # still takes the ordinary red path below.
            continue
        if result not in CI_SUCCESS_CONCLUSIONS + (None, ""):
            return "red"
    if pending:
        return "unknown"
    if could_not_run:
        return CI_COULD_NOT_RUN
    return "green"


def checks_still_running(checks: Sequence[dict]) -> bool:
    """Whether a rollup holds a check that has not reported yet.

    Deliberately narrower than ``ci_rollup_state(...) == "unknown"``: an empty
    rollup is also unknown, and "no checks at all" is a different fact from
    "the checks are still running". The first must stay visible as a refusal
    with a reason; only the second is worth waiting for.
    """
    entries = [check for check in (checks or []) if isinstance(check, dict)]
    if not entries:
        return False
    return ci_rollup_state(entries) == "unknown"


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


def _verdict_from_comment(row: Mapping[str, object]) -> Optional[Dict]:
    """Read a verdict and retain the timestamp of its GitHub comment."""
    found = parse_verdict(str(row.get("body") or ""))
    if found is None:
        return None
    verdict = dict(found)
    created_at = row.get("createdAt") or row.get("created_at")
    if isinstance(created_at, str) and created_at.strip():
        verdict["comment_created_at"] = created_at
    return verdict


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


def parse_analysis_marker(body: str) -> Optional[bool]:
    """Return marker validity, or None when the analysis marker is absent.

    A valid marker carries ``{"analysis": true}``. Any marker occurrence that
    cannot be read in that shape is malformed and still opts into the safe
    outcome: wait for Nate rather than silently closing the project.
    """
    if not isinstance(body, str) or ANALYSIS_MARKER not in body:
        return None

    found = _marked_json(body, ANALYSIS_MARKER)
    if found is not None:
        return found.get("analysis") is True
    return False


def parse_caused_by(body: str) -> List[str]:
    """Return durable PR/ticket references recorded at capture.

    The first version of the marker stored one string; repeated
    ``--caused-by`` flags store a list. Reading both shapes keeps the body
    contract forward-compatible without treating a malformed value as a
    recorded cause.
    """
    found = _marked_json(body, CAUSED_BY_MARKER)
    if not isinstance(found, dict):
        return []
    raw = found.get("caused_by", found.get("refs"))
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        return []
    result: List[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return result


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


def parse_gates_answer(body: str) -> Optional[Dict]:
    """Return a valid answered-Gates record, or ``None`` when it fails closed.

    The record is complete or it is nothing: an answer quote, the time it was
    given, and the decider who gave it. A marker missing any of the three is a
    degraded read, and a degraded read waits toward Nate rather than reporting
    a gate as settled — the same direction ``parse_satisfied_block_comment``
    takes, and for the same reason. Guessing "answered" would silently drop a
    real question; guessing "open" costs one re-ask.
    """
    found = _marked_json(body, GATES_ANSWER_MARKER)
    if found is None:
        return None
    answer = found.get("answer")
    decider = found.get("decider")
    if not isinstance(answer, str) or not answer.strip():
        return None
    if not isinstance(decider, str) or not decider.strip():
        return None
    if parse_time(found.get("at")) is None:
        return None
    return found


class PlanWriteRefused(Exception):
    """A post-Ready plan-body write that would have changed something else.

    Raised rather than returned. The whole reason this write exists is that
    hand-editing a plan after Ready is the drift it replaces; a refusal that
    a caller could ignore by not reading a return value would reintroduce
    exactly that.
    """


def gates_answer_block(answer: str, decider: str,
                       at: Optional[datetime] = None,
                       run: Optional[str] = None,
                       agent: Optional[str] = None) -> str:
    """Build the marker block recording one answered Gates question.

    The payload carries the instruction verbatim rather than a paraphrase.
    A record saying only that something was answered is the state #1167 was
    already in — the block had cleared and nobody could see what it cleared
    on — so ``answer`` is the words that were said, and ``decider`` is who
    said them. ``run`` and ``agent`` name the session that heard it, which
    is instrumentation and never part of the validity test.
    """
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("a Gates answer must not be empty")
    if not isinstance(decider, str) or not decider.strip():
        raise ValueError("a Gates answer must name its decider")
    run, agent = _heartbeat_context(run, agent)
    # `parse_gates_answer` validates this through `parse_time`, which accepts
    # only `%Y-%m-%dT%H:%M:%SZ` — narrower than the `isoformat()` every other
    # marker in this file writes. A writer that followed the house style here
    # would emit a record its own reader fails closed on, and the symptom is
    # the gate silently staying open: the exact defect #1261 exists to end.
    # Measured 2026-09-22: `parse_time` returns None for both
    # `2026-09-22T17:59:09.465909+00:00` and `2026-09-22T17:59:09+00:00`.
    stamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    fields = {
        "agent": agent,
        "answer": answer.strip(),
        "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decider": decider.strip(),
        "run": run,
    }
    return "{}\n\n```json\n{}\n```".format(
        GATES_ANSWER_MARKER, json.dumps(fields, indent=2, sort_keys=True)
    )


def _without_gates_record(body: str) -> str:
    """``body`` with the Gates line and any answer marker removed.

    The comparison basis for the confinement check below. Both halves of a
    legitimate write disappear from it, so two bodies that differ only in
    those two places compare equal and anything else does not.
    """
    remainder = GATES_LINE_RE.sub("", body)
    while True:
        block = _marked_json_block(remainder, GATES_ANSWER_MARKER)
        if block is None:
            return remainder
        remainder = remainder.replace(block, "", 1)


def _refuse_unconfined_write(original: str, updated: str) -> None:
    """Raise unless the only changes are the marker and the Gates line.

    This is the loud refusal the plan asks for, and it is deliberately a
    check on the *result* rather than on the caller's intent. ``answered_gates_body``
    takes no prose and so cannot be asked to edit any, but that is an
    argument about today's code; a later edit that widened it would pass
    every test that only checked the arguments. Comparing what actually
    changed cannot be widened by accident.

    Whitespace is normalised before the comparison because removing a list
    item leaves the blank structure around it slightly different on each
    side, which is not a prose edit.
    """
    def basis(value: str) -> str:
        return re.sub(r"\s+", " ", _without_gates_record(value)).strip()

    if basis(original) != basis(updated):
        raise PlanWriteRefused(
            "refusing the write: it changes the plan outside the Gates line "
            "and the answer marker. Editing plan prose after Ready is the "
            "drift this write replaces; change the plan before Ready, or "
            "record the disagreement as a comment"
        )


def answered_gates_body(body: str, answer: str, decider: str,
                        at: Optional[datetime] = None,
                        run: Optional[str] = None,
                        agent: Optional[str] = None) -> str:
    """``body`` with the Gates question recorded as answered.

    Exactly two things change: the ``- Gates:`` line in the Needs section,
    and the answer marker — replaced in place when one is already there, so
    a second answer supersedes the first rather than stacking a block the
    newest-first reader would have to arbitrate.

    A body with no Gates line is refused. That is not pedantry about
    formatting: every plan this write is for was rendered by the shaper,
    which always emits the four category lines, so a body without one is
    not the plan this caller thinks it is holding.
    """
    if not isinstance(body, str) or not body.strip():
        raise PlanWriteRefused("refusing the write: the plan body is empty")
    matches = list(GATES_LINE_RE.finditer(body))
    if not matches:
        raise PlanWriteRefused(
            "refusing the write: this body has no `- Gates:` line, so it is "
            "not a shaped plan and there is no question here to answer"
        )
    if len(matches) > 1:
        # Refusing rather than picking one. A plan may quote the Needs form
        # while rejecting an alternative — `skills/shape` documents these
        # lines verbatim, so a quoted `- Gates: …` in a Rejected section is
        # an ordinary thing to write. Guessing first would rewrite the
        # quotation and leave the real question open, and the confinement
        # check could not tell: it strips every Gates line from both sides,
        # so the wrong one moving looks exactly like the right one moving.
        raise PlanWriteRefused(
            "refusing the write: this body has {} `- Gates:` lines and "
            "nothing here can tell which one is the question. Leave one, or "
            "record the answer as a comment".format(len(matches))
        )
    match = matches[0]
    block = gates_answer_block(
        answer, decider, at=at, run=run, agent=agent)
    record = json.loads(block.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    line = GATES_ANSWERED_LINE.format(
        indent=match.group("indent"),
        at=record["at"],
        decider=record["decider"],
        answer=record["answer"],
    )
    updated = body[:match.start()] + line + body[match.end():]

    existing = _marked_json_block(updated, GATES_ANSWER_MARKER)
    if existing is not None:
        updated = updated.replace(existing, block, 1)
    else:
        updated = "{}\n\n{}".format(updated.rstrip("\n"), block)

    _refuse_unconfined_write(body, updated)
    return updated


def _unique_json_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    """Reject ambiguous JSON objects instead of silently taking the last key."""
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_block_event_spec(raw: str) -> Optional[Dict[str, str]]:
    """Return the one supported event spec, or None for malformed input."""
    try:
        spec = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        return None
    if not isinstance(spec, dict) or set(spec) != {
        "agent", "job", "outcome", "after"
    }:
        return None
    if (
        not isinstance(spec["agent"], str)
        or not spec["agent"].strip()
        or not isinstance(spec["job"], str)
        or not spec["job"].strip()
        or spec["outcome"] != "errored"
        or not isinstance(spec["after"], str)
        or parse_time(spec["after"]) is None
    ):
        return None
    return spec


def _unconditioned_event_reason(body: str) -> Optional[str]:
    """Keep malformed or unknown event forms as unconditioned block reasons."""
    header = BLOCK_EVENT_KIND_HEADER_RE.match(body)
    if header is None:
        return None
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", header.group("kind")):
        return None
    suffix = body[header.end():]
    payload = BLOCK_FENCED_PAYLOAD_RE.match(suffix)
    if payload is not None:
        suffix = suffix[payload.end():]
    return suffix.strip()


def _parse_block_comment_header(
    body: str,
) -> Optional[Tuple[re.Match, Optional[date], Optional[Dict[str, str]]]]:
    """Return a block header and its validated date or event condition."""
    event_match = BLOCK_EVENT_COMMENT_RE.match(body)
    if event_match is not None:
        event = _parse_block_event_spec(event_match.group("event_spec"))
        if event is None:
            return None
        return event_match, None, event

    match = BLOCK_COMMENT_RE.match(body)
    if match is None:
        return None
    raw_date = match.group("blocked_until")
    if raw_date is None:
        return match, None, None
    try:
        blocked_until = date.fromisoformat(raw_date)
    except ValueError:
        # The regex establishes the shape; the date parser establishes that
        # the calendar date actually exists (for example, no February 30).
        return None
    return match, blocked_until, None


def _parse_block_comment_details(
    bodies: Iterable[str],
) -> Optional[Tuple[List[str], Optional[date], str, Optional[Dict[str, str]]]]:
    """Return the newest parseable block's refs, date, reason and event."""
    for body in reversed(list(bodies)):
        if not isinstance(body, str):
            continue
        parsed_header = _parse_block_comment_header(body)
        if parsed_header is None:
            reason = _unconditioned_event_reason(body)
            if reason is not None:
                return [], None, reason, None
            continue
        match, blocked_until, event = parsed_header
        references = match.groupdict().get("references")
        return (
            references.split(" and ") if references else [],
            blocked_until,
            body[match.end():].strip(),
            event,
        )
    return None


def parse_block_comment(
    bodies: Iterable[str],
) -> Optional[Tuple[List[str], Optional[date], str]]:
    """Return the newest parseable block comment's refs, date and reason.

    The header is deliberately strict and anchored at the start of the body so
    an old or embedded mention cannot accidentally become a condition.
    """
    parsed = _parse_block_comment_details(bodies)
    return parsed[:3] if parsed is not None else None


def parse_decline_comment(bodies: Iterable[str]) -> Optional[str]:
    """Return the newest decline record's first line of reason, if any."""
    for body in reversed(list(bodies)):
        if not isinstance(body, str) or not body.startswith(DECLINED_PREFIX):
            continue
        reason = body[len(DECLINED_PREFIX):].strip()
        return reason.splitlines()[0].strip() if reason else ""
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
        if _parse_block_comment_header(body) is not None:
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

    spool = pathlib.Path(
        os.environ.get("COMMAND_CENTER_HEARTBEAT_SPOOL")
        or pathlib.Path.home() / ".claude" / "command-center-heartbeat"
    )
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
        cache = (
            _ACTIVE_BRIEF_CACHE.get()
            if "_ACTIVE_BRIEF_CACHE" in globals() else None
        )
        candidates = []
        for provider in providers:
            rows = (
                cache.heartbeat_rows(provider)
                if cache is not None else heartbeat.read(provider)
            )
            for row in heartbeat.open_starts(rows):
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


def class_adoption_comment(
    klass: str, source_line: str, at: Optional[datetime] = None,
) -> str:
    """Build the durable record for a class adopted at Nate's approve gate."""
    if klass not in LADDER:
        raise ValueError("unknown adopted class {!r}".format(klass))
    if not isinstance(source_line, str) or not source_line.strip():
        raise ValueError("class adoption source line must not be empty")
    body = (
        "**Class adopted:** {}\n\n"
        "Source line: `{}`\n\n{}"
    ).format(klass, source_line.strip(), CLASS_ADOPTION_OVERRIDE_NOTE)
    return append_provenance(body, "nate-relayed", at=at)


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


def caused_by_block(refs: Iterable[str], at: Optional[datetime] = None) -> str:
    """Build the machine-readable capture-cause block."""
    values = []
    for ref in refs:
        if not isinstance(ref, str):
            continue
        ref = ref.strip()
        if ref and ref not in values:
            values.append(ref)
    if not values:
        raise ValueError("at least one cause reference is required")
    fields = {
        "at": (at or datetime.now(timezone.utc)).isoformat(),
        "caused_by": values[0] if len(values) == 1 else values,
    }
    return "{}\n\n```json\n{}\n```".format(
        CAUSED_BY_MARKER, json.dumps(fields, indent=2, sort_keys=True)
    )


def append_caused_by(body: str, refs: Iterable[str],
                     at: Optional[datetime] = None) -> str:
    """Append durable cause references to a captured issue body."""
    return "{}\n\n{}".format(body, caused_by_block(refs, at=at))


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
        found = _verdict_from_comment(row)
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


def verdict_covers_head(
    verdict: Optional[Dict],
    head_oid: Optional[str],
    pr_comments: Optional[Sequence[Mapping[str, object]]] = None,
) -> bool:
    """Whether a verdict still covers the branch's current head.

    A requirement-unsure rejection stops covering after a later PR comment:
    the new evidence needs a fresh judgement. All other verdicts keep their
    existing same-head coverage. Missing timestamps or comment reads do not
    establish that evidence arrived later, so they preserve current coverage.
    """
    if not verdict or not head_oid or verdict.get("head_sha") != head_oid:
        return False
    if verdict.get("verdict") != "rejected":
        return True
    blocking = verdict.get("blocking")
    if not isinstance(blocking, (list, tuple)) or not any(
        isinstance(reason, str)
        and reason.startswith("requirement unsure:")
        for reason in blocking
    ):
        return True

    verdict_at = parse_time(verdict.get("comment_created_at"))
    if verdict_at is None:
        return True
    for comment in pr_comments or ():
        if not isinstance(comment, Mapping):
            continue
        created_at = comment.get("createdAt") or comment.get("created_at")
        comment_at = parse_time(
            created_at if isinstance(created_at, str) else None
        )
        if comment_at is not None and comment_at > verdict_at:
            return False
    return True


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
    if _conflicting_branch_blocker(pr) is None:
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


def awaiting_review(
    items: Sequence[Item],
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]] = None,
) -> Set[str]:
    """Tickets whose work is already in an open PR, waiting to be reviewed.

    Found by the `ticket/<number>` branch name the routine guarantees, which is
    the same handle `ticket_pr_index` uses. The normal caller supplies the
    shared batch from ``ticket_pr_facts``; a direct caller creates that one
    repository-wide GraphQL read here rather than paying once per PR.
    """
    if pr_facts is None:
        pr_facts = ticket_pr_facts(items)
    blocked: Set[str] = set()
    for item in items:
        for row in _pr_rows_for_ref(pr_facts, item.ref):
            if str(row.get("state") or "OPEN").upper() != "OPEN":
                continue
            # A PR whose review asked for changes is *not* blocked: its ticket
            # goes back to the engineer to fix. Without this a rejected PR has no
            # owner — the reviewer will not revisit it and the engineer is never
            # offered it — so it waits for Nate. That is #39. The hand-back
            # lasts only while the rejected head is still the head: once the
            # engineer pushes, the ticket is review work again (#487).
            verdict = _row_verdict(row, item.repo)
            if rejected_at_current_head(verdict, row.get("headRefOid")):
                continue
            blocked.add(item.ref)
    return blocked


def next_ticket(items: Sequence[Item], now: datetime,
                blocked: Optional[Set[str]] = None,
                excluded: Optional[Set[str]] = None,
                agent: str = "codex",
                repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
                pr_facts: Optional[
                    Dict[str, Optional[Dict[str, object]]]
                ] = None,
                backed_off: Optional[Mapping[str, object]] = None,
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
            backed_off=backed_off,
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
                         ] = None,
                         backed_off: Optional[
                             Mapping[str, object]
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
            backed_off=backed_off,
        )
        if ticket is None or tier is None:
            return ticket

        reasons = escalation_reasons(ticket.title, _loaded_item_body(ticket))
        wanted = bool(reasons) if tier == "escalated" else not reasons
        if wanted:
            return ticket
        excluded.add(ticket.ref)


def held_claims_before(
    items: Sequence[Item],
    now: datetime,
    ticket: Optional[Item],
    *,
    blocked: Optional[Set[str]] = None,
    agent: str = "codex",
    repo_readiness: Optional[Mapping[str, MemberRepoReadiness]] = None,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[str]:
    """Claimed queue entries a selector passed before its chosen ticket.

    ``next_ticket`` already removes live claims before choosing, but ``begin``
    also needs to make that walk observable. The returned refs keep the same
    shared ordering as ``startable``; when no ticket can be selected, every
    otherwise-startable live claim is reported.
    """
    claimed = {item.ref for item in in_motion(items, now, pr_facts=pr_facts)}
    passed: List[str] = []
    for candidate in startable(
        items,
        awaiting_review=blocked,
        agent=agent,
        repo_readiness=repo_readiness,
    ):
        if ticket is not None and candidate.ref == ticket.ref:
            break
        if candidate.ref in claimed:
            passed.append(candidate.ref)
    return passed


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


AUTHORING_PR_RE = re.compile(r"^\s*PR\s+#(?P<pr>\d+)\b")


def _authoring_pr_agents(rows: Iterable[Dict[str, object]]) -> Dict[str, Set[str]]:
    """Return the agents whose completed ticket runs opened each PR.

    Implementing routines finish with ``PR #n`` while reviewing routines use
    the structured ``merged`` field (and may also mention a PR in prose).
    Restricting the note parser to completed, non-merge runs keeps a review
    record from masquerading as the authoring run. A bound review run is
    excluded as an additional guard for older notes that start with ``PR``.
    """
    bindings = {
        row.get("run"): row.get("do")
        for row in rows
        if isinstance(row, dict)
        and row.get("phase") == "bind"
        and row.get("run")
    }
    found: Dict[str, Set[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (
            row.get("phase") != "finish"
            or row.get("outcome") != "done"
            or row.get("merged") is not None
            or bindings.get(row.get("run")) == "review"
        ):
            continue
        note = row.get("note")
        if not isinstance(note, str):
            continue
        match = AUTHORING_PR_RE.match(note)
        agent = row.get("agent")
        if match is None or not isinstance(agent, str) or not agent:
            continue
        found.setdefault(match.group("pr"), set()).add(agent)
    return found


def _dashboard_authoring_pr_agents() -> Dict[str, Set[str]]:
    """Collect runner attribution for PRs shown on the dashboard.

    The dashboard already runs alongside the brief's heartbeat reads. Reuse
    those per-run reads so a standard PR opened by an interactive or
    funnel-watch Claude session can hand current-head rework back to Claude;
    the pure board builder still accepts an explicit mapping for fixtures and
    callers that already have the evidence.
    """
    try:
        import heartbeat
    except Exception:
        return {}

    authored_by: Dict[str, Set[str]] = {}
    for agent in sorted(heartbeat.PROVIDERS):
        try:
            rows = _brief_heartbeat_rows(agent)
        except Exception:
            continue
        for pr, agents in _authoring_pr_agents(rows).items():
            authored_by.setdefault(pr, set()).update(agents)
    return authored_by


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
    live_rows: List[Tuple[str, List[Dict[str, object]]]] = []
    for agent in sorted(heartbeat.PROVIDERS):
        if agent in heartbeat.RETIRED_AGENTS:
            # A stopped schedule must not read as activity (#431).
            continue
        try:
            rows = _brief_heartbeat_rows(agent)
        except Exception:
            continue
        live_rows.append((agent, rows))

    authored_by: Dict[str, Set[str]] = {}
    for _agent, rows in live_rows:
        for pr, agents in _authoring_pr_agents(rows).items():
            authored_by.setdefault(pr, set()).update(agents)

    for agent, rows in live_rows:
        for row in rows:
            if not row.get("merged") or (row.get("ts") or 0) < cutoff:
                continue
            record = {
                "pr": row.get("merged"),
                "at": datetime.fromtimestamp(row["ts"], timezone.utc).isoformat(),
                "note": row.get("note"),
                "agent": agent,
            }
            if authored_by.get(str(row.get("merged"))) == {agent}:
                record["self_reviewed"] = True
            found.append(record)
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
        # The shape path writes Ideas -> Ready on a clear first pass, while a
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


def _self_approval_markers(
    item: Item, comments: Optional[Sequence[dict]] = None
) -> List[Dict[str, object]]:
    """Read marker comments for one already-identified candidate item."""
    if comments is None:
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
    items: Iterable[Item], now: datetime, brief_cache=None
) -> List[Dict[str, object]]:
    """Recent agent Ready transitions carrying their own approval marker.

    Status history is already part of the Project load. Only items with a
    recent Ideas/Shaped-to-Ready candidate pay for a bounded, batched
    issue-comment lookup, and a record is emitted only when the marker is
    present. That makes a Nate-authored ``approve`` transition invisible here
    without reading every issue's comments.
    """
    candidates = []
    for item in items:
        transition_times = _self_approval_transition_times(item, now)
        if not transition_times:
            continue

        candidates.append((item, transition_times))

    if not candidates:
        return []

    cache = brief_cache or _ACTIVE_BRIEF_CACHE.get() or BriefCache()
    comments_by_ref = cache.comment_tails(
        [item for item, _transition_times in candidates]
    )

    found: List[Dict[str, object]] = []
    for item, transition_times in candidates:
        markers = _self_approval_markers(
            item, comments_by_ref.get(item.ref, [])
        )
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
    """Brief health conditions; errored-run alerts include regressions only."""
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
            # The provider hold is Muse's file and speaks only for Muse. A
            # parked lane is explained, not dead, and the watchdog reads the
            # same record the runners write so the two cannot disagree.
            hold_until = (
                agent_health_module.quota_hold_until()
                if agent == agent_health_module.QUOTA_HOLD_AGENT else None
            )
            conditions = assess_agent_health(
                agent, _brief_heartbeat_rows(agent), now.timestamp(),
                hold_until=hold_until,
                regressions_only=True,
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


def agent_run_summary(now: datetime) -> List[Dict[str, object]]:
    """Recent per-agent run accounting for the brief's health line."""
    try:
        import heartbeat

        providers = sorted(heartbeat.PROVIDERS)
    except Exception:
        return []

    found: List[Dict[str, object]] = []
    retired = getattr(heartbeat, "RETIRED_AGENTS", frozenset())
    for agent in providers:
        if agent in retired:
            continue  # a stopped schedule is not a dying one (#431)
        try:
            rows = _brief_heartbeat_rows(agent)
            summary = heartbeat.run_summary(
                rows, now=now.timestamp()
            )
        except Exception:
            # This is diagnostic input. A heartbeat read failure must not make
            # the brief fail or turn an unavailable count into zero.
            continue
        if any(summary.values()):
            row: Dict[str, object] = {"agent": agent, **summary}
            if agent == "muse":
                # Start/end usage snapshots carry Muse's standard-rate
                # dollars and reset stamp. Keep the calibration beside the
                # run counts so the brief reads one heartbeat history once.
                consumption = heartbeat.muse_window_consumption(rows)
                if consumption:
                    row["window_consumption"] = consumption
            found.append(row)
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
        if i.parent is None and i.klass is not None and i.closed_at
        and i.closed_at >= cutoff
        and i.state_reason != "NOT_PLANNED"
    ]
    # The Execution metrics plan defines upkeep as these three project
    # classes.  Keep this reporting definition separate from PREEMPTING,
    # which controls ticket ordering and intentionally has different scope.
    upkeep = [i for i in recent if i.klass in {"Broken", "Maintenance", "Investigate"}]

    started_new = []
    for item in items:
        if item.parent is not None or item.klass != "New":
            continue
        transitions = []
        for event in item.status_events:
            if not isinstance(event, Mapping) or event.get("status") != "Building":
                continue
            previous = event.get("previous_status") or event.get("previousStatus")
            if previous == "Building":
                continue
            at = event.get("at")
            if not isinstance(at, datetime):
                at = parse_time(event.get("created_at") or event.get("createdAt"))
            if at is not None:
                transitions.append(at)
        # A current Building item can lack a timeline event on its first
        # status assignment. A Done item's current timestamp is its exit from
        # Building, so it cannot stand in for when the project started.
        if not transitions and item.status == "Building" and item.status_since:
            transitions.append(item.status_since)
        started_new.extend(transitions)
    days_since_new = (
        (now - max(started_new)).days if started_new else None
    )

    return {
        "window_days": MAINTENANCE_WINDOW.days,
        "closed_in_window": len(recent),
        # Keep the exact numerator beside the displayed share.  A later
        # rollup cannot recover it from the rounded three-decimal value.
        "upkeep_projects": len(upkeep),
        "upkeep_share": round(len(upkeep) / len(recent), 3) if recent else None,
        "days_since_anything_new_started": days_since_new,
        # Preserve the timestamps behind the newest-work signal so an hourly
        # metrics row can count the events in its own hour without reversing
        # a rounded age or treating a missing history as zero.
        "new_started_at": sorted(
            (stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=timezone.utc))
            .astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            for stamp in started_new
        ),
    }


def disposal(items: Iterable[Item], now: datetime) -> Dict[str, object]:
    """Report recent project completion, parking, and open growth.

    A Project issue is the parentless unit. Child tickets are deliberately
    excluded from every count, including the created/closed context, because
    their lifecycle is implementation detail rather than portfolio disposal.
    ``Done`` divided by ``Parked`` is the finished-vs-abandoned ratio; when no
    project was parked there is no denominator, so the ratio is unknown rather
    than an invented zero or infinity.
    """
    cutoff = now - MAINTENANCE_WINDOW
    projects = [item for item in items if item.parent is None]

    def in_window(at: Optional[datetime]) -> bool:
        return at is not None and at >= cutoff

    done = [
        item for item in projects
        if item.state == "CLOSED"
        and item.status == "Done"
        and in_window(item.closed_at)
    ]
    parked = [
        item for item in projects
        if item.state == "CLOSED"
        and item.status == "Parked"
        and in_window(item.closed_at)
    ]
    created = [item for item in projects if in_window(item.created_at)]
    closed = [item for item in projects if in_window(item.closed_at)]

    return {
        "window_days": MAINTENANCE_WINDOW.days,
        "done": len(done),
        "parked": len(parked),
        "finished_vs_abandoned": (
            round(len(done) / len(parked), 3) if parked else None
        ),
        "net_open_growth": len(created) - len(closed),
    }


def _metric_time(value: object) -> Optional[datetime]:
    """Parse a GitHub timestamp for a portfolio metric.

    The Project loader uses GitHub's second-precision ``Z`` form, while the
    CLI's PR JSON may also contain fractional seconds or an explicit offset.
    Metrics should accept both without making a malformed timestamp look old.
    """
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


def _metric_recent(item: Item, cutoff: datetime) -> bool:
    """Whether a Project item has a known lifecycle event in the window."""
    return any(
        at is not None and at >= cutoff
        for at in (item.created_at, item.status_since, item.closed_at)
    )


_CAUSE_REFERENCE_RE = re.compile(
    r"^(?:"
    r"#(?P<bare_number>[0-9]+)"
    r"|(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<repo_number>[0-9]+)"
    r"|(?:PR|pull(?:[ -]+request)?|ticket)\s*#?(?P<label_number>[0-9]+)"
    r")$",
    flags=re.IGNORECASE,
)
_CAUSE_URL_RE = re.compile(
    r"^https?://github\.com/"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"(?:issues|pull)/(?P<number>[0-9]+)(?:[/?#].*)?$",
    flags=re.IGNORECASE,
)


def _normalise_cause_reference(value: object) -> Optional[str]:
    """Return a canonical issue ref for a PR/ticket-shaped cause value."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    url = _CAUSE_URL_RE.fullmatch(text)
    if url:
        return "{}/{}#{}".format(
            url.group("owner"), url.group("repo"), url.group("number")
        )
    match = _CAUSE_REFERENCE_RE.fullmatch(text)
    if not match:
        return None
    number = (
        match.group("bare_number")
        or match.group("repo_number")
        or match.group("label_number")
    )
    if match.group("repo"):
        return "{}#{}".format(match.group("repo"), number)
    return "{}#{}".format(REPO, number)


def _regression_ticket_ref(body: Optional[str]) -> Optional[str]:
    """Read the machine-readable ticket line from a funnel regression issue."""
    if not isinstance(body, str):
        return None
    for line in body.splitlines():
        if not re.match(r"^\s*-\s*Ticket:\s*", line, flags=re.IGNORECASE):
            continue
        value = re.sub(
            r"^\s*-\s*Ticket:\s*", "", line, flags=re.IGNORECASE
        ).strip()
        return _normalise_cause_reference(value)
    return None


def recorded_cause_regressions(
    items: Iterable[Item], now: datetime
) -> Dict[str, object]:
    """Count recent Broken projects with an explicit cause record.

    A capture marker is durable evidence on the project itself. A rejection
    marker is the regression issue created by ``funnel reject``; its Ticket
    line is joined to the loaded child ticket and then to that ticket's
    parent. Human prose elsewhere in an issue is deliberately ignored.
    """
    rows = list(items)
    cutoff = now - MAINTENANCE_WINDOW
    projects = [
        item for item in rows
        if item.parent is None and item.klass == "Broken"
    ]
    recent_projects = [item for item in projects if _metric_recent(item, cutoff)]
    project_refs = {item.ref for item in recent_projects}
    recorded = {
        item.ref
        for item in recent_projects
        if any(
            _normalise_cause_reference(value) is not None
            for value in parse_caused_by(item.body or "")
        )
    }

    by_ref = {item.ref: item for item in rows}
    for regression in rows:
        if not regression.title.startswith(REGRESSION_PREFIX):
            continue
        if not _metric_recent(regression, cutoff):
            continue
        ticket_ref = _regression_ticket_ref(regression.body)
        ticket = by_ref.get(ticket_ref or "")
        if ticket is not None and ticket.parent in project_refs:
            recorded.add(ticket.parent)

    total = len(recent_projects)
    count = len(recorded)
    return {
        "window_days": MAINTENANCE_WINDOW.days,
        "definition": (
            "parent projects classified Broken with a created, status, or "
            "closed event in the last 30 days and a capture caused_by marker "
            "or a recent funnel reject regression record"
        ),
        "status": "available",
        "available": True,
        "broken_projects": total,
        "with_recorded_cause": count,
        "without_recorded_cause": total - count,
        "count": count,
        "share": round(count / total, 3) if total else None,
        "value": count,
    }


def _recent_merged_pr_rows(
    repo: str, cutoff: datetime
) -> List[Dict[str, object]]:
    """Read merged PRs updated within the window using a light paged query.

    The portfolio metric only needs the branch and merge timestamps. Reusing
    ``ticket_pr_index`` would also request CI rollups and scan every PR state;
    the brief's 100-row bound can also hide valid merges. Merged PRs sort by
    ``updatedAt``, so the first row older than the cutoff proves that later
    rows cannot have merged in the window.
    """
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        raise GitHubError("invalid repository ref {}".format(repo))

    query = """query($cursor: String) {{
  rateLimit {{ cost remaining resetAt }}
  repo0: repository(owner: {owner}, name: {name}) {{
    pullRequests(
      first: {page_size}, after: $cursor, states: [MERGED],
      orderBy: {{field: UPDATED_AT, direction: DESC}}
    ) {{
      nodes {{ state headRefName mergedAt updatedAt }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}""".format(
        owner=json.dumps(owner),
        name=json.dumps(name),
        page_size=PR_GRAPHQL_PAGE_SIZE,
    )

    rows: List[Dict[str, object]] = []
    cursor: Optional[str] = None
    while True:
        variables = {"cursor": cursor} if cursor is not None else {}
        data = gh_graphql(query, **variables)
        if not isinstance(data, dict):
            raise GitHubError("merged PR response was not an object")
        repository = data.get("repo0")
        if not isinstance(repository, dict):
            raise GitHubError(
                "could not read repository {} in merged PR response".format(
                    repo
                )
            )
        pull_requests = repository.get("pullRequests")
        if not isinstance(pull_requests, dict):
            raise GitHubError(
                "invalid merged pull-request response for {}".format(repo)
            )
        nodes = pull_requests.get("nodes")
        page_info = pull_requests.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubError(
                "invalid merged pull-request page for {}".format(repo)
            )

        for node in nodes:
            if not isinstance(node, dict):
                raise GitHubError("invalid merged pull-request row")
            updated_at = _metric_time(node.get("updatedAt"))
            if updated_at is None:
                raise GitHubError(
                    "merged pull request has no parseable updatedAt"
                )
            if updated_at < cutoff:
                return rows
            rows.append({
                "state": node.get("state"),
                "headRefName": node.get("headRefName"),
                "mergedAt": node.get("mergedAt"),
            })

        if not page_info.get("hasNextPage"):
            return rows
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise GitHubError(
                "merged pull-request page for {} has no next cursor".format(
                    repo
                )
            )
        if next_cursor == cursor:
            raise GitHubError(
                "merged pull-request page for {} repeated its cursor".format(
                    repo
                )
            )
        cursor = next_cursor


def command_center_ticket_pr_share(
    items: Iterable[Item], now: datetime
) -> Dict[str, object]:
    """Report the fraction of recent command-center merges on ticket branches."""
    del items  # The command-center repository is the source of PR truth.
    cutoff = now - MAINTENANCE_WINDOW
    try:
        rows = _recent_merged_pr_rows(REPO, cutoff)
    except Exception as exc:
        return {
            "window_days": MAINTENANCE_WINDOW.days,
            "definition": (
                "merged command-center PRs in the last 30 days whose branch "
                "is ticket/<number>"
            ),
            "status": "unavailable",
            "available": False,
            "value": None,
            "share": None,
            "reason": "could not read command-center PRs: {}".format(
                _brief_error(exc)
            ),
        }
    merged = 0
    ticket_merged = 0
    unknown_timestamps = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        merged_at_raw = row.get("mergedAt") or row.get("merged_at")
        state = str(row.get("state") or "").upper()
        if not merged_at_raw and state != "MERGED":
            continue
        merged_at = _metric_time(merged_at_raw)
        if merged_at is None:
            unknown_timestamps += 1
            continue
        if merged_at < cutoff:
            continue
        merged += 1
        if ticket_ref_from_branch(REPO, row.get("headRefName") or ""):
            ticket_merged += 1

    definition = (
        "merged command-center PRs in the last 30 days whose branch is "
        "ticket/<number>"
    )
    if unknown_timestamps:
        return {
            "window_days": MAINTENANCE_WINDOW.days,
            "definition": definition,
            "status": "partial",
            "available": False,
            "merged_prs": None,
            "ticket_merged_prs": None,
            "value": None,
            "share": None,
            "unknown_merged_prs": unknown_timestamps,
            "reason": "one or more merged PRs had no parseable mergedAt",
        }

    share = ticket_merged / merged if merged else None
    return {
        "window_days": MAINTENANCE_WINDOW.days,
        "definition": definition,
        "status": "available" if merged else "insufficient_data",
        "available": True,
        "merged_prs": merged,
        "ticket_merged_prs": ticket_merged,
        "value": round(share, 3) if share is not None else None,
        "share": round(share, 3) if share is not None else None,
        "reason": None if merged else "no merged PRs in the window",
    }


DECLINE_ROUTING_CUTOFF_PR = 1447
DECLINE_ROUTING_REVIEW_MARKER = "<!-- command-center-review-routing -->"
DECLINE_ROUTING_SEARCH = """
query($search: String!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  search(type: ISSUE, query: $search, first: 100, after: $cursor) {
    issueCount
    nodes {
      __typename
      ... on Issue {
        number
        repository { nameWithOwner }
        body
        state
        stateReason
        closedAt
        labels(first: 100) { totalCount nodes { name } }
        blockedBy(first: 100) {
          totalCount
          nodes { number repository { nameWithOwner } }
        }
        comments(last: 100) {
          pageInfo { hasPreviousPage }
          nodes { body createdAt }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _decline_routing_cutoff() -> datetime:
    """Read the first classifier ticket's merge time from GitHub."""
    payload = _gh_json(
        "gh", "pr", "view", str(DECLINE_ROUTING_CUTOFF_PR),
        "--repo", REPO, "--json", "mergedAt",
    )
    if not isinstance(payload, dict):
        raise GitHubError(
            "could not read classifier PR #{}".format(
                DECLINE_ROUTING_CUTOFF_PR
            )
        )
    cutoff = _metric_time(payload.get("mergedAt"))
    if cutoff is None:
        raise GitHubError(
            "classifier PR #{} has no parseable merge time".format(
                DECLINE_ROUTING_CUTOFF_PR
            )
        )
    return cutoff


def _decline_routing_search_query(repo: str, start: datetime) -> str:
    """Find issue candidates whose comments may contain an in-window decline."""
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
        raise GitHubError("invalid repository ref {}".format(repo))
    return (
        'repo:{} is:issue in:comments "Declined:" updated:>={}'
    ).format(repo, start.date().isoformat())


def _decline_routing_issue_pages(
    repo: str, start: datetime
) -> Iterable[Dict[str, object]]:
    """Yield bounded issue-search pages with the state needed to classify them."""
    search_text = _decline_routing_search_query(repo, start)
    cursor: Optional[str] = None
    seen_cursors: Set[str] = set()
    while True:
        variables: Dict[str, object] = {"search": search_text}
        if cursor is not None:
            variables["cursor"] = cursor
        response = gh_graphql(DECLINE_ROUTING_SEARCH, **variables)
        connection = response.get("search") if isinstance(response, dict) else None
        if not isinstance(connection, dict):
            raise GitHubError(
                "could not read decline-search results for {}".format(repo)
            )
        issue_count = connection.get("issueCount")
        if (not isinstance(issue_count, int) or isinstance(issue_count, bool)
                or issue_count < 0):
            raise GitHubError("invalid decline-search count for {}".format(repo))
        if issue_count > 1000:
            raise GitHubError(
                "decline search for {} exceeded GitHub's 1,000-issue limit"
                .format(repo)
            )
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise GitHubError("invalid decline-search page for {}".format(repo))
        for node in nodes:
            if not isinstance(node, dict) or node.get("__typename") != "Issue":
                raise GitHubError(
                    "decline search returned an invalid issue for {}".format(repo)
                )
            repository = node.get("repository")
            if not isinstance(repository, dict):
                raise GitHubError("decline-search issue has no repository")
            if repository.get("nameWithOwner") != repo:
                raise GitHubError(
                    "decline search returned an issue outside {}".format(repo)
                )
            yield node
        if not page_info.get("hasNextPage"):
            return
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise GitHubError(
                "decline-search page for {} has no next cursor".format(repo)
            )
        if next_cursor == cursor or next_cursor in seen_cursors:
            raise GitHubError(
                "decline-search page for {} repeated its cursor".format(repo)
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _decline_routing_comment_rows(
    issue: Mapping, start: datetime
) -> List[Dict[str, object]]:
    """Validate the comment tail and return its rows without reading old bodies."""
    comments = issue.get("comments")
    if not isinstance(comments, dict):
        raise GitHubError("decline-search issue has no comments connection")
    nodes = comments.get("nodes")
    page_info = comments.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise GitHubError("invalid decline comments connection")
    rows: List[Dict[str, object]] = []
    times: List[datetime] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise GitHubError("invalid issue-comment row in decline search")
        created_at = _metric_time(node.get("createdAt"))
        body = node.get("body")
        if created_at is None or not isinstance(body, str):
            raise GitHubError("decline-search issue has an unreadable comment")
        times.append(created_at)
        rows.append({"body": body, "created_at": created_at})

    # The last 100 comments are enough unless more comments on this issue
    # also fall inside the reporting window. In that case fail closed instead
    # of silently dropping older declines or routing comments.
    if page_info.get("hasPreviousPage"):
        if not times:
            raise GitHubError("decline comments page is unexpectedly empty")
        if min(times) > start:
            raise GitHubError(
                "decline comments for {} exceed the bounded comment page".format(
                    (issue.get("repository") or {}).get("nameWithOwner")
                )
            )
    return rows


def _codex_decline_events(
    comments: Sequence[Mapping], start: datetime, now: datetime
) -> List[Tuple[datetime, str, str]]:
    """Read in-window Codex declines as (time, run, reason)."""
    found: List[Tuple[datetime, str, str]] = []
    for comment in comments:
        body = comment.get("body")
        created_at = comment.get("created_at")
        if not isinstance(body, str) or not body.lstrip().startswith(DECLINED_PREFIX):
            continue
        if not isinstance(created_at, datetime):
            raise GitHubError("decline comment has no parseable creation time")
        # Keep the historical hand-fixed cases outside the parser path.
        if created_at < start or created_at > now:
            continue
        provenance = parse_provenance(body)
        if (
            provenance is None
            or provenance.get("voice") != "agent"
            or provenance.get("agent") != "codex"
        ):
            continue
        run = provenance.get("run")
        if isinstance(run, str) and run:
            reason = body.lstrip()[len(DECLINED_PREFIX):].split(
                PROVENANCE_MARKER, 1
            )[0].strip()
            found.append((created_at, run, reason))
    return found


def _decline_routing_outcome(
    issue: Mapping, comments: Sequence[Mapping],
    decline_at: datetime, run: str, decline_reason: str,
    start: datetime, now: datetime,
) -> Optional[str]:
    """Classify one ticket's latest in-window Codex decline from GitHub facts."""
    repository = issue.get("repository")
    repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
    if not isinstance(repo, str):
        raise GitHubError("decline-search issue has no repository")
    decline_class, decline_target = classify_decline_reason(
        decline_reason, repo, issue.get("body") or ""
    )

    routing_comment = False
    for comment in comments:
        body = comment.get("body")
        created_at = comment.get("created_at")
        if (
            not isinstance(body, str)
            or DECLINE_ROUTING_REVIEW_MARKER not in body
            or not isinstance(created_at, datetime)
            or created_at < max(start, decline_at)
            or created_at > now
        ):
            continue
        record = _marked_json(body, DECLINE_ROUTING_REVIEW_MARKER)
        provenance = parse_provenance(body)
        routing_comment = bool(
            isinstance(record, dict)
            and record.get("type") == "accept-body-conflict"
            and isinstance(record.get("decline_excerpt"), str)
            and isinstance(record.get("conflict_pointer"), str)
            and record.get("conflict_pointer")
            and provenance is not None
            and provenance.get("voice") == "agent"
            and provenance.get("agent") == "codex"
            and provenance.get("run") == run
        )
        if routing_comment:
            break

    labels = issue.get("labels")
    label_nodes = labels.get("nodes") if isinstance(labels, dict) else None
    label_count = labels.get("totalCount") if isinstance(labels, dict) else None
    if not isinstance(label_nodes, list) or label_count != len(label_nodes):
        raise GitHubError("decline-search issue has incomplete labels")
    blocked_label = any(
        isinstance(label, dict) and label.get("name") == "blocked"
        for label in label_nodes
    )

    blocked_by = issue.get("blockedBy")
    blocker_nodes = blocked_by.get("nodes") if isinstance(blocked_by, dict) else None
    edge_count = blocked_by.get("totalCount") if isinstance(blocked_by, dict) else None
    if (
        not isinstance(blocker_nodes, list)
        or not isinstance(edge_count, int)
        or isinstance(edge_count, bool)
        or edge_count < len(blocker_nodes)
        or edge_count < 0
    ):
        raise GitHubError("decline-search issue has an unreadable blocked-by count")
    blocker_refs = set()
    for blocker in blocker_nodes:
        if not isinstance(blocker, dict):
            raise GitHubError("decline-search issue has an invalid blocked-by edge")
        number = blocker.get("number")
        repository = blocker.get("repository")
        owner_repo = (
            repository.get("nameWithOwner")
            if isinstance(repository, dict) else None
        )
        if (
            not isinstance(number, int) or isinstance(number, bool)
            or not isinstance(owner_repo, str)
        ):
            raise GitHubError("decline-search issue has an invalid blocked-by edge")
        blocker_refs.add("{}#{}".format(owner_repo, number))
    if (
        decline_class == "prerequisite-ticket"
        and isinstance(decline_target, str)
        and decline_target not in blocker_refs
        and edge_count > len(blocker_nodes)
    ):
        raise GitHubError(
            "blocked-by edges for {} exceed the bounded response".format(repo)
        )

    state = str(issue.get("state") or "").upper()
    reason = str(issue.get("stateReason") or "").upper()
    closed_at = _metric_time(issue.get("closedAt"))
    completed_close = (
        state == "CLOSED" and reason == "COMPLETED"
        and closed_at is not None and closed_at >= decline_at
    )

    if decline_class == "defer-note-proof" and completed_close:
        return "closed_as_proven_defer"
    if decline_class == "accept-body-conflict" and routing_comment:
        return "routed_to_review"
    if (
        decline_class == "prerequisite-ticket"
        and isinstance(decline_target, str)
        and decline_target in blocker_refs
        and not blocked_label
    ):
        return "became_edge"
    if blocked_label:
        return "stayed_blocked"
    return None


def decline_routing_metric(
    items: Iterable[Item], now: datetime
) -> Dict[str, object]:
    """Count forward-only Codex decline outcomes from current GitHub state.

    The latest Codex decline per issue is classified in a rolling 30-day
    window. The window is clipped to #1419's merge, so the four hand-fixed
    2026-09-23 declines never enter the parser or the counts.
    """
    normalized_now = _metric_time(now)
    if normalized_now is None:
        raise GitHubError("decline-routing metric needs a parseable current time")
    merged_at = _decline_routing_cutoff()
    start = max(normalized_now - MAINTENANCE_WINDOW, merged_at)
    repos = {REPO}
    for item in items:
        repo = getattr(item, "repo", None)
        if not isinstance(repo, str):
            raise GitHubError("decline-routing metric found an invalid item repo")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) is None:
            raise GitHubError("invalid repository ref {}".format(repo))
        repos.add(repo)

    counts = {
        "became_edge": 0,
        "closed_as_proven_defer": 0,
        "routed_to_review": 0,
        "stayed_blocked": 0,
    }
    declines = 0
    unclassified = 0
    seen_issues: Set[str] = set()
    for repo in sorted(repos):
        for issue in _decline_routing_issue_pages(repo, start):
            number = issue.get("number")
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                raise GitHubError("decline-search issue has an invalid number")
            ref = "{}#{}".format(repo, number)
            if ref in seen_issues:
                raise GitHubError("decline search repeated {}".format(ref))
            seen_issues.add(ref)
            comments = _decline_routing_comment_rows(issue, start)
            events = _codex_decline_events(comments, start, normalized_now)
            if not events:
                continue
            decline_at, run, decline_reason = max(
                events, key=lambda event: event[0]
            )
            declines += 1
            outcome = _decline_routing_outcome(
                issue, comments, decline_at, run, decline_reason,
                start, normalized_now,
            )
            if outcome is None:
                unclassified += 1
            else:
                counts[outcome] += 1

    return {
        "window_days": MAINTENANCE_WINDOW.days,
        "cutoff_pr": DECLINE_ROUTING_CUTOFF_PR,
        "cutoff_at": merged_at.isoformat().replace("+00:00", "Z"),
        "from": start.isoformat().replace("+00:00", "Z"),
        "through": normalized_now.isoformat().replace("+00:00", "Z"),
        "definition": (
            "latest Codex-provenanced **Declined:** comment per issue in the "
            "rolling 30-day window, clipped to the merge of classifier PR "
            "#{}; current GitHub state supplies the outcome"
        ).format(DECLINE_ROUTING_CUTOFF_PR),
        "status": "available" if not unclassified else "partial",
        "available": not unclassified,
        "declines": declines,
        **counts,
        "unclassified": unclassified,
        "reason": (
            "{} decline ticket(s) did not have exactly one routing outcome"
            .format(unclassified) if unclassified else None
        ),
    }


def _read_portfolio_metrics(
    items: Iterable[Item], now: datetime
) -> Dict[str, object]:
    """Read the two parent-plan portfolio signals for the published brief."""
    return {
        "recorded_cause_regressions": recorded_cause_regressions(items, now),
        "command_center_ticket_pr_share": command_center_ticket_pr_share(
            items, now
        ),
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
HEARTBEAT_SPOOL = pathlib.Path(
    os.environ.get("COMMAND_CENTER_HEARTBEAT_SPOOL")
    or pathlib.Path.home() / ".claude" / "command-center-heartbeat"
)
HEARTBEAT_FIX = "restore GitHub access so the heartbeat branch and local spool can drain"
AUTH_LOGIN_FIX = "gh auth login"
AUTH_SCOPE_FIX = "gh auth refresh -s project"
TOPIC_FIX = "add the `command-center` topic to at least one repository"

# Keep this operational alarm separate from the Project Class ladder. It is a
# local capacity signal, not a queue-ranking rule.
PROCESS_TABLE_ALARM_THRESHOLD = 0.60
PROCESS_TABLE_FIX = "close idle sessions or processes, then rerun funnel doctor"


PROJECT_FIELDS_QUERY = """
query($login: String!, $number: Int!) {
  rateLimit { cost remaining resetAt }
  user(login: $login) {
    projectV2(number: $number) {
      fields(first: 100) {
        nodes {
          ... on ProjectV2Field { id name }
          ... on ProjectV2IterationField { id name }
          ... on ProjectV2SingleSelectField {
            id name
            options { id name color description }
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


def _process_table_limit(proc: object) -> Optional[int]:
    """Parse ``sysctl`` output in either quiet or labelled form."""
    output = getattr(proc, "stdout", "") or ""
    value = str(output).strip()
    if ":" in value:
        value = value.rsplit(":", 1)[1].strip()
    if not value.isdigit():
        return None
    limit = int(value)
    return limit if limit > 0 else None


def check_process_table() -> Check:
    """Report the current user's process count against macOS's per-user cap."""
    try:
        limit_proc = _run_bounded_subprocess(
            ["sysctl", "-n", "kern.maxprocperuid"],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            "process table", False,
            "could not read kern.maxprocperuid ({})".format(
                str(exc) or "unknown error"
            ),
            PROCESS_TABLE_FIX,
        )

    if limit_proc.returncode != 0:
        detail = (getattr(limit_proc, "stderr", "")
                  or getattr(limit_proc, "stdout", "")
                  or "sysctl exited {}".format(limit_proc.returncode)).strip()
        return Check(
            "process table", False,
            "could not read kern.maxprocperuid ({})".format(detail),
            PROCESS_TABLE_FIX,
        )

    limit = _process_table_limit(limit_proc)
    if limit is None:
        return Check(
            "process table", False,
            "could not parse kern.maxprocperuid output",
            PROCESS_TABLE_FIX,
        )

    try:
        process_proc = _run_bounded_subprocess(
            ["ps", "-u", str(os.getuid()), "-o", "pid="],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            "process table", False,
            "could not count current-user processes ({})".format(
                str(exc) or "unknown error"
            ),
            PROCESS_TABLE_FIX,
        )

    if process_proc.returncode != 0:
        detail = (getattr(process_proc, "stderr", "")
                  or getattr(process_proc, "stdout", "")
                  or "ps exited {}".format(process_proc.returncode)).strip()
        return Check(
            "process table", False,
            "could not count current-user processes ({})".format(detail),
            PROCESS_TABLE_FIX,
        )

    count = sum(
        1 for line in (getattr(process_proc, "stdout", "") or "").splitlines()
        if line.strip()
    )
    ratio = count / limit
    percent = ratio * 100
    alarm = ratio > PROCESS_TABLE_ALARM_THRESHOLD
    found = "{} / {} processes ({:.1f}%; alarm above {:.0f}%)".format(
        count, limit, percent, PROCESS_TABLE_ALARM_THRESHOLD * 100
    )
    if alarm:
        found += "; alert"
    return Check(
        "process table", not alarm, found,
        PROCESS_TABLE_FIX if alarm else "",
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
    for field_name, expected_options in (
        ("Status", STAGES),
        ("Class", LADDER),
        ("Origin", ORIGIN_OPTIONS),
        ("Risk", RISK_OPTIONS),
        ("Needs", NEEDS_OPTIONS),
    ):
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
        "Project {}/{} has Status, Class, Origin, Risk, Needs and {} with "
        "the required options".format(PROJECT_OWNER, PROJECT_NUMBER, LOCK_FIELD),
        "",
    )


def gh_branch_exists() -> bool:
    """Return whether the heartbeat branch exists, or raise on other failures."""
    try:
        proc = _run_gh(
            _gh_api_command(
                "repos/{}/git/ref/heads/{}".format(REPO, HEARTBEAT_BRANCH),
                cache=True,
            ),
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


def _actions_runs(payload: object) -> List[dict]:
    """Normalise the REST newest-run response."""
    if isinstance(payload, dict):
        payload = payload.get("workflow_runs")
    if not isinstance(payload, list):
        return []
    return [row for row in payload if isinstance(row, dict)]


def _actions_jobs(payload: object) -> Optional[List[dict]]:
    """Normalise an Actions jobs response while preserving unreadable state."""
    if isinstance(payload, dict):
        payload = payload.get("jobs")
    if not isinstance(payload, list):
        return None
    return [row for row in payload if isinstance(row, dict)]


def _actions_annotations(payload: object) -> Optional[List[object]]:
    """Normalise check-run annotations while preserving an absent response."""
    if isinstance(payload, dict):
        payload = payload.get("annotations", payload.get("nodes"))
    if not isinstance(payload, list):
        return None
    return list(payload)


def enrich_actions_run(repo: str, source: Mapping[str, object]) -> Dict[str, object]:
    """Attach bounded jobs/annotation evidence to one Actions run row."""
    run = dict(source)
    result = _ci_result(run)

    # Some fixtures and API adapters already include this evidence. Avoid
    # paying any follow-up read when the newest-run payload is complete.
    jobs_known = "jobs" in run
    annotations_known = "annotations" in run
    jobs = _actions_jobs(run.get("jobs")) if jobs_known else None
    if result == "FAILURE" and not jobs_known:
        jobs = _actions_jobs(_gh_api_json(
            "repos/{}/actions/runs/{}/jobs?per_page=100".format(
                repo, run.get("id") or run.get("databaseId")
            ),
            cache=False,
        )) if run.get("id") or run.get("databaseId") else None
        if jobs is not None:
            run["jobs"] = jobs
            jobs_known = True

    annotations: Optional[List[object]] = None
    if annotations_known:
        annotations = _actions_annotations(run.get("annotations"))
    elif isinstance(run.get("check_runs"), list):
        annotations = []
        for check_run in run["check_runs"]:
            if isinstance(check_run, Mapping):
                annotations.extend(
                    _actions_annotations(check_run.get("annotations"))
                    or []
                )
        run["annotations"] = annotations
        annotations_known = True
    elif result == "FAILURE" and jobs:
        # REST exposes annotations on check runs (the same records that the
        # Actions jobs endpoint returns IDs for), not on the workflow run
        # itself. Query only failed/unknown jobs in this run.
        annotations = []
        for job in jobs:
            if not isinstance(job, Mapping):
                continue
            job_result = _ci_result(job)
            if job_result in CI_SUCCESS_CONCLUSIONS:
                continue
            check_run_id = job.get("id") or job.get("databaseId")
            if check_run_id is None:
                continue
            found = _actions_annotations(_gh_api_json(
                "repos/{}/check-runs/{}/annotations?per_page=100".format(
                    repo, check_run_id
                ),
                cache=False,
            ))
            if found:
                annotations.extend(found)
        if annotations:
            run["annotations"] = annotations
    return run


#: What the main-CI check says about one red main. Two values only: an
#: infrastructure stop that a rerun would clear, and a real failure that needs
#: a person or a fix. Anything the read cannot settle is ``real`` — a wrong
#: ``infra`` invites a pointless rerun and hides a genuine break, while a wrong
#: ``real`` only costs a look (#1178).
MAIN_CI_INFRA = "infra"
MAIN_CI_REAL = "real"


def _main_ci_failed_job(
    run: Mapping[str, object]
) -> Tuple[Optional[str], Optional[object]]:
    """The first job in a run that did not succeed, as ``(name, id)``."""
    jobs = run.get("jobs")
    if not isinstance(jobs, list):
        return None, None
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        if _ci_result(job) in CI_SUCCESS_CONCLUSIONS:
            continue
        name = job.get("name")
        identifier = job.get("id") or job.get("databaseId")
        if isinstance(name, str) and name.strip():
            return name.strip(), identifier
        if identifier is not None:
            return None, identifier
    return None, None


def _main_ci_attempt(run: Mapping[str, object]) -> Optional[int]:
    """How many times this run has been attempted, if GitHub says."""
    for key in ("run_attempt", "runAttempt", "attempt"):
        value = run.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def main_ci_infra_reason(run: Mapping[str, object]) -> Optional[str]:
    """Why this failed run never really ran, or ``None`` if it did.

    Two signatures, both from #1178: a failure with zero completed steps, and a
    failure annotation naming a runner that stopped answering. The first is
    already the shared no-start shape (`ci_could_not_run_reasons`); the second
    is this check's addition, because a lost self-hosted runner reports a plain
    job failure with no test output rather than a startup failure.
    """
    for text in _ci_annotation_texts(run):
        if _ci_annotation_is_lost_runner(text):
            return text
    return ci_could_not_run_reason([run])


def main_ci_row(repo: str) -> Optional[Dict[str, object]]:
    """One member repo's main-branch CI verdict, or ``None`` when it is green.

    The canonical reader: every lane that wants to know whether a red main is
    worth a rerun asks this, so no two of them can disagree about what the
    annotations said.

    A read this cannot complete is reported as a ``real`` failure carrying the
    reason, never as silence and never as green. That is the same fail-closed
    direction the merge gate takes, for the same reason: an absent signal that
    reads as good news is how main stayed red for 2h12m with nobody told.
    """
    try:
        head = _gh_api_json(
            "repos/{}/commits/main".format(repo), cache=False
        )
        sha = head.get("sha") if isinstance(head, Mapping) else None
        if not isinstance(sha, str) or not sha:
            return {
                "repo": repo, "sha": None, "job": None,
                "verdict": MAIN_CI_REAL,
                "reason": "could not read the main head for {}".format(repo),
            }
        runs = _actions_runs(_gh_api_json(
            "repos/{}/actions/runs?branch=main&head_sha={}&per_page=1".format(
                repo, sha
            ),
            cache=False,
        ))
        if not runs:
            # No workflow run for this head is not a failure: CI may not have
            # started yet, and the merge gate already refuses on an absent
            # check. Stay quiet rather than inventing a red main.
            return None
        run = enrich_actions_run(repo, runs[0])
        result = _ci_result(run)
        if result in CI_SUCCESS_CONCLUSIONS:
            return None
        if result is None or result != "FAILURE":
            # Still running, or a conclusion this does not recognise. Neither
            # is a red main to act on.
            return None
        reason = main_ci_infra_reason(run)
        job_name, job_id = _main_ci_failed_job(run)
        return {
            "repo": repo,
            "sha": sha,
            "job": job_name,
            "job_id": job_id,
            "run_id": run.get("id") or run.get("databaseId"),
            # A rerun raises the attempt count, so GitHub itself records
            # whether this SHA has already had its one retry (#1220). No
            # state file: the fact is already in the run.
            "attempt": _main_ci_attempt(run),
            "verdict": MAIN_CI_INFRA if reason else MAIN_CI_REAL,
            "reason": reason or "no infrastructure signature in this failure",
        }
    except (GitHubError, OSError, subprocess.SubprocessError,
            TypeError, ValueError) as exc:
        return {
            "repo": repo, "sha": None, "job": None,
            "verdict": MAIN_CI_REAL,
            "reason": "could not read main CI for {}: {}".format(repo, exc),
        }


#: What a retry attempt did, for the record it leaves and the row it returns.
MAIN_CI_RETRIED = "retried"
MAIN_CI_NOT_RETRIED = "not-retried"


def main_ci_retry(
    repo: str,
    row: Optional[Mapping[str, object]] = None,
    rerun: Optional[Callable[[Sequence[str]], object]] = None,
) -> Optional[Dict[str, object]]:
    """Rerun one main job that never really ran — once per SHA, never twice.

    The narrow half of #1178: main stayed red for 2h12m on a run that died
    when a self-hosted runner stopped answering, and the same SHA went green
    on a manual rerun fifteen minutes later. Every merge waited in between,
    because the merge gate reads main.

    Four conditions, all of them refusals rather than retries:

    - **`real` never retries.** Rerunning a genuine break burns runner time
      and hides the signal, which is why the plan rejects retry-until-green.
    - **One attempt only.** A rerun raises GitHub's own ``run_attempt``, so
      the second red on a SHA is visible as attempt 2 and stays red for the
      watch. That is the whole once-per-SHA mechanism: no state file, no
      journal, no label — GitHub is the state, and it already records this.
    - **An unreadable attempt count does not retry.** Without it there is no
      way to tell a first failure from a second, and retrying blindly is how
      a loop starts.
    - **A green, running or absent main has nothing to retry.**

    Returns the decision either way, so a caller can say what happened and
    why. ``None`` only when there is no red main at all.
    """
    row = row if row is not None else main_ci_row(repo)
    if row is None:
        return None
    decision = dict(row)
    verdict = row.get("verdict")
    attempt = row.get("attempt")
    run_id = row.get("run_id")
    job_id = row.get("job_id")

    if verdict != MAIN_CI_INFRA:
        decision.update(
            retry=MAIN_CI_NOT_RETRIED,
            retry_reason="a real failure is not retried; it needs a person",
        )
        return decision
    if not isinstance(attempt, int) or attempt < 1:
        decision.update(
            retry=MAIN_CI_NOT_RETRIED,
            retry_reason="could not read how many times this run was "
                         "attempted, so a first failure and a second are "
                         "indistinguishable",
        )
        return decision
    if attempt > 1:
        decision.update(
            retry=MAIN_CI_NOT_RETRIED,
            retry_reason="already retried once on this SHA (attempt {}); "
                         "a second red stays red".format(attempt),
        )
        return decision
    if run_id is None:
        decision.update(
            retry=MAIN_CI_NOT_RETRIED,
            retry_reason="no run id to rerun",
        )
        return decision

    command = ["gh", "run", "rerun", str(run_id), "--repo", str(repo)]
    if job_id is not None:
        command += ["--job", str(job_id)]
    runner = rerun if rerun is not None else _run_gh
    try:
        result = runner(command, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError, GitHubError) as exc:
        decision.update(
            retry=MAIN_CI_NOT_RETRIED,
            retry_reason="rerun failed: {}".format(exc),
        )
        return decision
    if getattr(result, "returncode", 0) != 0:
        decision.update(
            retry=MAIN_CI_NOT_RETRIED,
            retry_reason="rerun refused: {}".format(
                (getattr(result, "stderr", "") or "").strip()
            ),
        )
        return decision
    decision.update(
        retry=MAIN_CI_RETRIED,
        retry_reason="infrastructure stop on attempt 1; reran {}".format(
            row.get("job") or "the failed job"
        ),
    )
    return decision


def main_ci_retries(
    repos: Optional[Sequence[str]] = None,
    rerun: Optional[Callable[[Sequence[str]], object]] = None,
) -> List[Dict[str, object]]:
    """One retry decision per member repo whose main is red."""
    names = list(repos) if repos is not None else member_repos()
    rows = [main_ci_retry(repo, rerun=rerun) for repo in names]
    return [row for row in rows if row is not None]


def main_ci_json(repos: Optional[Sequence[str]] = None) -> List[Dict[str, object]]:
    """The brief's thin reader over `main_ci_row`, one row per red main.

    Derived every run from the run, job, step and annotation facts. No label,
    no Project field, nothing stored: a verdict written down is a verdict that
    can go stale, and this one changes the moment somebody reruns the job.
    """
    names = list(repos) if repos is not None else member_repos()
    rows = [main_ci_row(repo) for repo in names]
    return [row for row in rows if row is not None]


def latest_actions_run_probe(repo: str) -> Tuple[Optional[str], Optional[str]]:
    """Read the newest Actions run once and classify a startup/account stop.

    The queue only needs a per-repository hold signal, not a history of runs.
    The newest run endpoint is therefore the probe boundary.  When a failed
    run does not carry its jobs or annotations inline, the bounded follow-up
    reads only fill the evidence needed to distinguish "never started" from a
    real test failure; they are never used as a second queue or cache.
    """
    try:
        payload = _gh_api_json(
            "repos/{}/actions/runs?per_page=1".format(repo), cache=False
        )
        runs = _actions_runs(payload)
        if not runs:
            return None, None
        run = enrich_actions_run(repo, runs[0])
        reason = ci_could_not_run_reason([run])
        if reason is None:
            return ci_rollup_state([run]), None
        return CI_COULD_NOT_RUN, reason
    except (GitHubError, OSError, subprocess.SubprocessError, TypeError, ValueError):
        # This is a diagnostic hold, not a replacement for the merge gate. If
        # the optional probe cannot be read, leave queue readiness unchanged;
        # the PR gate still fails closed on its own live CI read.
        return None, None


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
        payload = _gh_api_json(
            "repos/{}/contents/.github/{}".format(repo, filename),
            cache=True,
        )
        if payload is not None:
            return True
    return False


def member_repo_readiness(repo: str) -> MemberRepoReadiness:
    """Read the checkable onboarding facts for one topic-bearing repository."""
    # CI workflow presence is a blocking queue-readiness gate. It must stay
    # live so a newly removed workflow cannot be hidden by a stale response.
    workflows = _gh_api_json(
        "repos/{}/actions/workflows".format(repo), cache=False
    )
    # Labels and Dependabot are advisory onboarding context, never a reason to
    # hand out or withhold a ticket, so their REST responses may be cached.
    labels = _gh_api_json(
        "repos/{}/labels?per_page=100".format(repo), cache=True
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
    if readiness.ci_state == CI_COULD_NOT_RUN:
        ci = "CI could not run (blocking): {}".format(
            readiness.ci_annotation or "startup or account failure"
        )
    else:
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


#: Where a `muse exec` invocation can live on this machine. The explicit
#: entries are the checkouts that do not follow the installed-agent layout;
#: the glob catches the ones that do, so an agent installed next month is
#: scanned without this list being edited. A path that is not present is
#: skipped: not every machine holds every checkout.
MUSE_SCAN_ROOTS = (
    CHECKOUT_ROOT,
    CLAUDE_DIR / "command-center-run",
    pathlib.Path.home() / "workbench",
    pathlib.Path.home() / "private" / "career-toolset",
    pathlib.Path.home() / ".local" / "bin",
)

#: Globbed roots, relative to home. The installed-agent layout, plus the
#: loose checkouts under ``~/code``.
MUSE_SCAN_GLOBS = (".local/share/*/checkout", "code/*")

#: Only these are read. A `muse exec` lives in a script, and walking every
#: file in every checkout would read data, caches and vendored trees.
#:
#: The empty string is the extensionless executable, which is how the
#: runners here and `ff-operate` are written. **Not covered, and say so
#: rather than let a reader assume otherwise:** `.bash`, `.zsh`, `.rb`,
#: `.pl`, `.js`, `.ts`, workflow YAML, and plists — a plist could not
#: match in any case, since `<string>muse</string><string>exec</string>`
#: has no adjacency the patterns recognise.
MUSE_SCAN_SUFFIXES = (".sh", ".py", "")

#: Directories never descended into. ``tests`` is here for a different
#: reason than the rest: a test that asserts on an invocation contains the
#: text of one without ever running it, and this repo's own fixtures for
#: this very check would otherwise be reported. A test is not a call site.
#:
#: **The cost, stated as what the code does rather than what was
#: intended:** this skips *any path component* named ``tests``, so a
#: production script that happens to live under one is invisible to the
#: check — not merely a test helper that shells out to Muse. Nothing on
#: this machine is in that position today.
MUSE_SCAN_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "site-packages", ".tox", "dist", "build", "tests",
})

#: File names skipped for the same reason, where the tests do not live in
#: a directory of their own.
MUSE_SCAN_SKIP_FILE_RE = re.compile(r"\A(?:test_.*|.*_test)\.py\Z")

#: A file larger than this is not a script.
MUSE_SCAN_MAX_BYTES = 512 * 1024

#: A `muse exec` invocation written as a shell command: a bare `muse`, or
#: a variable holding the binary path, followed by the `exec` subcommand.
#:
#: The trailing lookahead is what separates an invocation from prose. Both
#: runners log `"muse exec failed (exit $status)"`; without it this check
#: reported fourteen call sites that do not exist and buried the one that
#: does. A real invocation is followed by a flag, a quoted argument, a
#: variable, a continuation, a redirect, a pipe, a separator, or the end of
#: the line — never by a bare English word.
#:
#: **The costs of that rule, stated plainly rather than implied away.**
#: `muse exec prompt.txt`, a positional prompt with no flags, is
#: indistinguishable from prose by this test and is not reported;
#: admitting a bare word after `exec` re-admits every "muse exec failed"
#: sentence in the tree, which is the noise that hides real findings.
#: And in the other direction, a shell line that *quotes* a whole flagged
#: command — `grep -c "muse exec --json" "$log"` — is reported. An earlier
#: draft rejected any match inside a string to suppress that, which made
#: `bash -lc "muse exec"`, `ssh host "muse exec"` and any line with a
#: stray apostrophe invisible instead. A spurious line is cheap; a missed
#: call is the whole failure. This applies to shell only: Python is
#: parsed, so it has neither problem.
#:
#: The gap is real and narrow. This is not "catches anything added next".
MUSE_EXEC_RE = re.compile(
    r"""(?x)
    (?:
        (?: \bmuse \s+ exec \b )
      | (?: (?:"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?)
            \s+ exec \b )
    )
    (?= \s* (?: $ | \d+ [<>] | [-\\"\',$(\[|&;<>] ) )
    """
)

#: How far a single invocation may run. A bash continuation or a Python
#: argv list is a handful of lines; this only stops a malformed file from
#: swallowing the rest of itself.
MUSE_STATEMENT_MAX_LINES = 40

MUSE_MODEL_PIN_FIX = (
    "pass --model explicitly at each call site; "
    "muse-spark-1.3-contributor is the catalog default"
)
MUSE_MODEL_CATALOG_FIX = (
    "replace stale or invalid --model values with a visible model from the "
    "provider catalog, then rerun funnel doctor"
)
# Muse writes its provider-owned catalog here under the current user's home.
# Keep only the relative suffix in Git; the home directory is machine-specific.
MUSE_MODEL_CATALOG_RELATIVE = pathlib.Path(".local/share/muse/model-catalog")
MuseModelInvocation = namedtuple(
    "MuseModelInvocation", "path line model")


def _muse_shell_model_value(statement: str, start: int = 0) -> Optional[str]:
    """The literal or dynamic value passed after ``--model`` in a shell call.

    The doctor does not execute a runner to resolve shell variables. Those are
    still reported as dynamic values so the catalog check is explicit about
    what it could not verify.
    """
    tail = statement[start:].replace("\\\n", " ")
    control = re.search(r"(?:&&|\|\||[;|])", tail)
    if control is not None:
        tail = tail[:control.start()]
    match = re.search(
        r"(?<![\w-])--model(?:\s*=\s*|\s+)"
        r'(?:"([^"\n]*)"|\'([^\'\n]*)\'|([^\s\\;|&<>]+))',
        tail,
    )
    if match is None:
        return None
    return next(value for value in match.groups() if value is not None)


def _muse_shell_invocations(text: str) -> List[Tuple[int, Optional[str]]]:
    """Return shell Muse call line numbers and their ``--model`` values."""
    found: List[Tuple[int, Optional[str]]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        code = _muse_shell_code(line)
        matches = list(MUSE_EXEC_RE.finditer(code))
        if not matches:
            continue
        statement = _muse_shell_statement(lines, index)
        for match in matches:
            found.append((index + 1, _muse_shell_model_value(
                statement, match.start())))
    return found


def _muse_shell_code(line: str) -> str:
    """``line`` with any trailing shell comment removed.

    A `#` counts only where a word begins — start of line or after
    whitespace — so `$#`, a fragment identifier in a URL and a `#` inside
    a word all survive. No quote tracking: pairing apostrophes is how a
    naive scanner turns `echo don't  # it's fine` into code, and how it
    swallows a real call between two unrelated quotes.
    """
    match = re.search(r"(?:^|\s)#", line)
    return line if match is None else line[:match.start()]


def _muse_shell_statement(lines: Sequence[str], start: int) -> str:
    """The shell statement beginning on line ``start``.

    Backslash continuations only. An earlier draft also followed trailing
    commas, to reach the end of a Python argv list; that rule let a
    sibling entry which pinned absolve one which did not, so a real
    finding was reported clean depending on its position in a dict.
    Python is parsed rather than scanned now, and this follows the one
    continuation shell actually has.
    """
    collected = []
    for offset in range(min(MUSE_STATEMENT_MAX_LINES, len(lines) - start)):
        code = _muse_shell_code(lines[start + offset])
        collected.append(code)
        if not code.rstrip().endswith("\\"):
            break
    return "\n".join(collected)


def _muse_shell_findings(text: str) -> List[int]:
    """Line numbers of shell `muse exec` invocations with no ``--model``."""
    return [line for line, model in _muse_shell_invocations(text)
            if model is None]


def _muse_string_is_a_command(value: object) -> bool:
    """Whether a Python string literal is a `muse exec` command line."""
    return isinstance(value, str) and bool(MUSE_EXEC_RE.search(value))


def _muse_argv_literals(node: "ast.AST") -> Optional[List[str]]:
    """The string elements of ``node`` if it is a Muse argv sequence.

    A list or tuple naming the `exec` subcommand, where something in it
    refers to Muse — a literal path, or a constant like ``MUSE_COMMAND``.
    Returns ``None`` when it is some other sequence.
    """
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    literals = [element.value for element in node.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)]
    if "exec" not in literals:
        return None
    names = [element.id for element in node.elts
             if isinstance(element, ast.Name)]
    names += [element.attr for element in node.elts
              if isinstance(element, ast.Attribute)]
    mentions_muse = (any("muse" in text.lower() for text in literals)
                     or any("muse" in name.lower() for name in names))
    return literals if mentions_muse else None


def _muse_python_model_value(node: "ast.AST") -> Optional[str]:
    """The value following ``--model`` in a parsed Muse argv sequence."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    for index, element in enumerate(node.elts):
        if not isinstance(element, ast.Constant) \
                or not isinstance(element.value, str):
            continue
        argument = element.value
        if argument.startswith("--model="):
            return argument[len("--model="):]
        if argument != "--model":
            continue
        if index + 1 >= len(node.elts):
            return None
        value = node.elts[index + 1]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        try:
            expression = ast.unparse(value)
        except (AttributeError, TypeError, ValueError):
            expression = type(value).__name__
        return "<dynamic {}>".format(expression)
    return None


def _muse_python_invocations(
    text: str,
) -> Optional[List[Tuple[int, Optional[str]]]]:
    """Line numbers and model values for Python `muse exec` invocations.

    Parsed, not scanned. Every false positive and every laundered finding
    the line-based scanner had in Python came from guessing at structure:
    a docstring that reads like a command, a sibling list entry that
    pinned, flags appended after the call, `shell=True` with the command
    in a string. The tree answers all of them exactly.

    Returns ``None`` when the source will not parse, so the caller can
    fall back rather than silently reporting a file clean.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    found: Set[Tuple[int, Optional[str]]] = set()
    for node in ast.walk(tree):
        literals = _muse_argv_literals(node)
        if literals is not None:
            found.add((node.lineno, _muse_python_model_value(node)))
            continue
        if not isinstance(node, ast.Call):
            continue
        # `subprocess.run("muse exec ...", shell=True)`, `os.system(...)`:
        # the command is a string, so the argv rule above never sees it.
        for argument in node.args:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Constant) \
                        and _muse_string_is_a_command(inner.value):
                    for line, model in _muse_shell_invocations(inner.value):
                        found.add((inner.lineno + line - 1, model))
    return sorted(found, key=lambda item: (item[0], item[1] or ""))


def _muse_python_findings(text: str) -> Optional[List[int]]:
    """Line numbers of Python `muse exec` calls with no ``--model``."""
    invocations = _muse_python_invocations(text)
    if invocations is None:
        return None
    return [line for line, model in invocations if model is None]


def _muse_invocations(
    path: pathlib.Path, text: str,
) -> List[Tuple[int, Optional[str]]]:
    """Parse all Muse invocations from one script-shaped file."""
    if "exec" not in text:
        return []
    if path.suffix == ".py":
        parsed = _muse_python_invocations(text)
        if parsed is not None:
            return parsed
    return _muse_shell_invocations(text)


def _muse_findings(path: pathlib.Path, text: str) -> List[int]:
    """Line numbers of unpinned invocations in one file.

    Python is parsed; shell is scanned. A `.py` that will not parse —
    Python 2, a template, a fragment — falls back to the scanner rather
    than being passed over, because a file this cannot read is exactly
    where an unpinned call would sit unnoticed.
    """
    return [line for line, model in _muse_invocations(path, text)
            if model is None]


def muse_model_invocations(
    roots: Optional[Iterable[os.PathLike]] = None,
) -> List[MuseModelInvocation]:
    """Every Muse call site and its optional model value in scan roots.

    One walk supplies both the existing missing-pin check and catalog
    validation, so the two checks cannot silently disagree about which
    invocation is being inspected.
    """
    findings: Set[MuseModelInvocation] = set()
    for root in _muse_scan_roots(roots):
        for path in _muse_scan_files(root):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for number, model in _muse_invocations(path, text):
                findings.add(MuseModelInvocation(str(path), number, model))
    return sorted(findings, key=lambda item: (
        item.path, item.line, item.model or ""))


def muse_unpinned_invocations(
    roots: Optional[Iterable[os.PathLike]] = None,
) -> List[str]:
    """Every `muse exec` call site that does not name its model.

    This keeps the #1303 omission check available to callers while sharing the
    same parsed call sites that the doctor uses for catalog validation.
    """
    return ["{}:{}".format(invocation.path, invocation.line)
            for invocation in muse_model_invocations(roots)
            if invocation.model is None]


def _muse_scan_roots(
    roots: Optional[Iterable[os.PathLike]] = None,
) -> List[pathlib.Path]:
    """The scan roots that exist, explicit ones plus the globbed layout."""
    if roots is not None:
        candidates = [pathlib.Path(root) for root in roots]
    else:
        candidates = list(MUSE_SCAN_ROOTS)
        home = pathlib.Path.home()
        for pattern in MUSE_SCAN_GLOBS:
            candidates.extend(sorted(home.glob(pattern)))
    seen: List[pathlib.Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.append(resolved)
    return seen


def _muse_scan_files(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Script-shaped files under ``root``, skipping vendored trees."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in sorted(dirnames)
                       if name not in MUSE_SCAN_SKIP_DIRS]
        for filename in sorted(filenames):
            path = pathlib.Path(dirpath) / filename
            if path.suffix not in MUSE_SCAN_SUFFIXES:
                continue
            if MUSE_SCAN_SKIP_FILE_RE.match(filename):
                continue
            try:
                if path.is_symlink() or path.stat().st_size > \
                        MUSE_SCAN_MAX_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _muse_read_model_catalog(
    catalog_dir: Optional[os.PathLike] = None,
) -> Tuple[Optional[Dict[str, bool]], Optional[str]]:
    """Read provider catalog rows from Muse's local cache.

    ``None`` models means the cache could not be checked. The doctor reports
    that as unknown rather than treating every pin as stale.
    """
    directory = (pathlib.Path(catalog_dir) if catalog_dir is not None else
                 pathlib.Path.home() / MUSE_MODEL_CATALOG_RELATIVE)
    try:
        files = sorted(path for path in directory.iterdir()
                       if path.is_file() and path.suffix == ".json")
    except OSError:
        return None, "model-catalog directory is missing or unreadable"
    if not files:
        return None, "no model-catalog/*.json files were found"

    visibility: Dict[str, List[bool]] = {}
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None, "model-catalog file {} is unreadable".format(
                path.name)
        rows = data.get("rows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return None, "model-catalog file {} has no rows list".format(
                path.name)
        for row in rows:
            if not isinstance(row, dict) \
                    or not isinstance(row.get("model_id"), str) \
                    or not row["model_id"].strip() \
                    or not isinstance(row.get("visibility"), str):
                return None, "model-catalog file {} has an invalid row".format(
                    path.name)
            model_id = row["model_id"].strip()
            visibility.setdefault(model_id, []).append(
                row["visibility"] == "visible")
    return {model_id: any(states)
            for model_id, states in visibility.items()}, None


def _muse_dynamic_model(value: str) -> bool:
    """Whether a parsed model expression needs shell/runtime evaluation."""
    return value.startswith("<dynamic ") or "$" in value or "`" in value


def check_muse_model_pins(
    roots: Optional[Iterable[os.PathLike]] = None,
    catalog_dir: Optional[os.PathLike] = None,
) -> Check:
    """Check every Muse model pin against the provider's local catalog."""
    try:
        invocations = muse_model_invocations(roots)
    except OSError as exc:
        return Check(
            "muse model pins", False,
            "could not scan for muse exec invocations ({})".format(
                str(exc) or "unknown error"),
            MUSE_MODEL_PIN_FIX,
        )

    unpinned = [invocation for invocation in invocations
                if invocation.model is None]
    pins = [invocation for invocation in invocations
            if invocation.model is not None]
    if not pins:
        if not unpinned:
            return Check("muse model pins", True, "", "")
        return Check(
            "muse model pins", False,
            "{} muse exec invocation(s) do not pass --model, so they run on "
            "the contributor model by default:\n{}".format(
                len(unpinned), "\n".join(
                    "  {}:{}".format(item.path, item.line)
                    for item in unpinned)),
            MUSE_MODEL_PIN_FIX,
        )

    catalog, catalog_error = _muse_read_model_catalog(catalog_dir)
    details: List[str] = []
    failed = 0
    unknown = 0
    verified = 0
    if catalog_error is not None:
        unknown = len(pins)
        details.append(
            "model catalog unknown ({}): 0 verified, 0 failed, {} unknown"
            .format(catalog_error, unknown))
        for invocation in pins:
            details.append(
                "  UNKNOWN {} — {}:{} (muse exec --model {}; catalog "
                "unavailable)".format(
                    repr(invocation.model), invocation.path, invocation.line,
                    invocation.model))
    else:
        for invocation in pins:
            model = invocation.model
            location = "{}:{}".format(invocation.path, invocation.line)
            if _muse_dynamic_model(model):
                unknown += 1
                details.append(
                    "  UNKNOWN dynamic model value {} — {}:{} (muse exec "
                    "--model {})".format(
                        repr(model), invocation.path, invocation.line, model))
            elif model not in catalog:
                failed += 1
                details.append(
                    "  UNKNOWN/INVALID model id {} — {} (muse exec "
                    "--model {})".format(repr(model), location, model))
            elif not catalog[model]:
                failed += 1
                details.append(
                    "  STALE model id {} is not visible — {} (muse exec "
                    "--model {})".format(repr(model), location, model))
            else:
                verified += 1
                details.append(
                    "  VERIFIED model id {} — {} (muse exec --model {})"
                    .format(repr(model), location, model))
        details.insert(0, "model catalog: {} verified, {} failed, {} unknown"
                       .format(verified, failed, unknown))

    if unpinned:
        details.append(
            "{} muse exec invocation(s) do not pass --model, so they run on "
            "the contributor model by default:".format(len(unpinned)))
        details.extend("  {}:{}".format(item.path, item.line)
                       for item in unpinned)

    fixes = []
    if unpinned:
        fixes.append(MUSE_MODEL_PIN_FIX)
    if failed:
        fixes.append(MUSE_MODEL_CATALOG_FIX)
    return Check(
        "muse model pins", not (unpinned or failed), "\n".join(details),
        "; ".join(fixes),
    )


#: Nate keeps the Codex standard automation at every ten minutes until the
#: backlog starts clearing (2026-09-22, #1315). The share of its runs that
#: find the queue empty is that signal, read directly.
CODEX_EMPTY_SHARE_LIMIT = 0.5
CODEX_EMPTY_MIN_RUNS = 12
CODEX_EMPTY_WINDOW_SECONDS = 24 * 60 * 60
CODEX_EMPTY_READ_TIMEOUT = 30
CODEX_EMPTY_RUNS_FIX = (
    "move command-center-tickets-hourly to every 20 minutes (#1315)")

#: The slower cadence the fix names, and the spacing between standard-lane
#: starts that counts as already there. Once the lane runs at twenty minutes
#: the share can stay high with nothing left to change, so the row stops
#: asking. The cadence is read from the most recent gaps only, so the row
#: notices a change within hours rather than a day.
CODEX_SLOW_CADENCE_SECONDS = 20 * 60
CODEX_SLOWED_GAP_SECONDS = 15 * 60
CODEX_CADENCE_GAPS = 12


def codex_empty_run_share(records: Iterable[object],
                          now: float) -> Dict[str, object]:
    """How many of the standard lane's recent runs found the queue empty.

    Counted in runs, not rows (``heartbeat.one_record_per_run`` is the
    canonical rule; a run can carry several records). A run is standard
    when its start record says so; starts written before #1320 carry no
    tier and are left out rather than guessed at.

    Neither side reads the finish, which the routine can file as
    ``nothing-to-do`` for a GitHub failure or a held lock. A run *worked*
    when ``begin`` bound a ticket to it; a run *found nothing* when
    ``begin`` recorded ``queue: empty``. Everything else, budget gates,
    errors, locks and withheld work, is neither and is left out.

    The cadence is the median of the most recent spacings between
    standard-lane starts, so the row can tell whether its own fix has been
    applied.
    """
    rows = [row for row in records if isinstance(row, dict)]

    def recent(row):
        stamp = row.get("ts")
        return (not isinstance(stamp, bool)
                and isinstance(stamp, (int, float))
                and now - stamp <= CODEX_EMPTY_WINDOW_SECONDS)

    standard = {row.get("run") for row in rows
                if row.get("phase") == "start" and row.get("run")
                and row.get("tier") == "standard"}
    worked = {row.get("run") for row in rows
              if row.get("phase") == "bind" and row.get("do") == "ticket"
              and row.get("run") in standard and recent(row)}
    empty = {row.get("run") for row in rows
             if row.get("phase") == "event"
             and row.get("outcome") == "nothing-to-do"
             and row.get("queue") == "empty"
             and row.get("run") in standard and recent(row)} - worked
    counted = len(worked | empty)

    starts = sorted({float(row["ts"]) for row in rows
                     if row.get("phase") == "start"
                     and row.get("run") in standard and recent(row)})
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    gaps = gaps[-CODEX_CADENCE_GAPS:]
    cadence = None
    if gaps:
        ordered = sorted(gaps)
        middle = len(ordered) // 2
        cadence = (ordered[middle] if len(ordered) % 2
                   else (ordered[middle - 1] + ordered[middle]) / 2)
    return {"runs": counted, "empty": len(empty),
            "share": (len(empty) / counted) if counted else None,
            "cadence_seconds": cadence}


def check_codex_empty_runs(records: Optional[Iterable[object]] = None,
                           now: Optional[float] = None) -> Check:
    """Report when the Codex standard lane mostly finds its queue empty.

    A signal for a cadence decision, not a health check: an unreadable
    heartbeat, odd records or a thin window pass with a note, because the
    heartbeat's own row reports whether it can be read. It asks for the
    slower cadence only while the lane is still faster than that.
    """
    name = "codex empty runs"
    now = time.time() if now is None else now
    try:
        if records is None:
            import heartbeat

            records = _call_with_optional_keyword(
                heartbeat.read, "timeout", CODEX_EMPTY_READ_TIMEOUT, "codex")
        stats = codex_empty_run_share(list(records or []), now)
    except Exception as exc:
        return Check(name, True,
                     "codex heartbeat could not be read ({}); no cadence "
                     "signal this run".format(str(exc) or type(exc).__name__),
                     "")
    if stats["runs"] < CODEX_EMPTY_MIN_RUNS:
        return Check(name, True,
                     "{} standard-lane run(s) in 24 hours reached the queue, "
                     "too few to judge the cadence".format(stats["runs"]), "")
    found = ("{} of {} standard-lane runs in 24 hours found the queue empty "
             "({:.0%})".format(stats["empty"], stats["runs"], stats["share"]))
    cadence = stats["cadence_seconds"]
    if cadence is not None:
        found += "; starts every {:.0f} min".format(cadence / 60)
    if stats["share"] < CODEX_EMPTY_SHARE_LIMIT:
        return Check(name, True, found, "")
    if cadence is not None and cadence >= CODEX_SLOWED_GAP_SECONDS:
        return Check(name, True,
                     found + "; already at the slower cadence", "")
    return Check(name, False,
                 found + "; the backlog has cleared enough to slow the lane",
                 CODEX_EMPTY_RUNS_FIX)


CODEX_AUTOMATIONS_FIX = (
    "quit the ChatGPT app, bring each automation.toml to codex_run.py's "
    "manifest, relaunch, and rerun funnel doctor (#1321)")


def check_codex_automations(root: Optional[str] = None) -> Check:
    """Report Codex automations whose settings differ from the manifest.

    The app holds each automation's model, effort, schedule and status
    outside the repository and has written stale copies back over edits
    (LEARNINGS.md, 2026-09-06), so drift in a paused automation must show
    before anyone turns it on. Prompts are the run-keeper's to install and
    report; this row reads everything else. Each automation's status and
    memory size are reported either way (#1318).
    """
    name = "codex automations"
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import codex_run

        findings = codex_run.automation_findings(root)
    except Exception as exc:
        return Check(name, False,
                     "could not read the Codex automations ({})".format(
                         str(exc) or type(exc).__name__),
                     CODEX_AUTOMATIONS_FIX)
    if findings["drift"]:
        # Drift lines only: the doctor appends the fix to every line of a
        # failing row, and a status note is not something to fix.
        return Check(name, False, "\n".join(
            "  " + line for line in findings["drift"]), CODEX_AUTOMATIONS_FIX)
    return Check(name, True, "\n".join(
        "  " + note for note in findings["notes"]), "")


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
    *,
    _executor=None,
) -> Dict[str, MemberRepoReadiness]:
    """Read onboarding and newest-run facts once per loaded repository."""
    repos = sorted({item.repo for item in items})
    if _executor is None:
        return {repo: _begin_repo_readiness(repo) for repo in repos}

    # Submit independent repository reads to the caller's bounded begin pool.
    # Collect in key order, not completion order, so both the mapping and the
    # first reported failure are stable across runs.
    futures = {
        repo: _submit_begin_read(_executor, _begin_repo_readiness, repo)
        for repo in repos
    }
    return {repo: futures[repo].result() for repo in repos}


def _begin_repo_readiness(repo: str) -> MemberRepoReadiness:
    """Read one repository's onboarding and newest-run facts."""
    base = member_repo_readiness(repo)
    state, annotation = latest_actions_run_probe(repo)
    if state is not None:
        base = replace(
            base,
            ci_state=state,
            ci_annotation=annotation,
        )
    return base


def _submit_begin_read(executor, function, *args):
    """Submit a read while preserving the command's context variables."""
    context = contextvars.copy_context()
    return executor.submit(context.run, function, *args)


def _timed_begin_pr_facts(items):
    """Return a branch snapshot with the duration measured in its worker."""
    started = time.perf_counter()
    try:
        return ticket_pr_facts(items), None, max(
            0.0, time.perf_counter() - started
        )
    except GitHubError as exc:
        return None, exc, max(0.0, time.perf_counter() - started)


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


def check_block_conditions(
    items: Iterable[Item], now: Optional[datetime] = None
) -> Check:
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
                or item.blocked_until is not None
                or item.open_blockers
                or item.dead_blockers
            )
        ),
        key=lambda item: (item.repo, item.number),
    )

    for item in candidates:
        satisfied = satisfied_block_refs(item, by_ref, now=now)
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

        if unclearable_block(item):
            findings.append("{}: stranded: {}".format(
                item.ref, _unclearable_block_reason(item)))
            broken = True
            continue

        if item.block_reason is None and item.open_blockers:
            detail = "on {} (native edge)".format(", ".join(
                _dependency_ref(item, value) or str(value).strip()
                for value in item.open_blockers
            ))
        elif item.block_reason is None:
            detail = "block comment is not parseable"
        else:
            details = []
            if item.block_references:
                resolved = [
                    _dependency_ref(item, value) or str(value).strip()
                    for value in item.block_references
                ]
                details.append("on {}".format(", ".join(resolved)))
            blocked_until = _item_blocked_until(item)
            if blocked_until is not None and (
                blocked_until > _block_condition_date(now)
            ):
                details.append("until {}".format(blocked_until.isoformat()))
            detail = ", ".join(details) or "no machine-readable conditions"
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
        check_process_table(),
        check_auth_scope(),
        check_project_fields(),
        check_topic(),
        *check_member_repos(),
        check_muse_model_pins(),
        check_usage_cache(cache_path=usage_cache),
        check_heartbeat(spool_dir=heartbeat_spool),
        check_codex_empty_runs(),
        check_codex_automations(),
    ]
    if items is not None:
        items = list(items)
        checks.append(check_item_consistency(
            items, merged_pr_facts=merged_pr_facts
        ))
        checks.append(check_class_assignments(items))
        checks.append(check_block_comments(items))
        checks.append(check_block_conditions(items))
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


def check_project_pagination(
    project_item_count: Optional[int], project_item_pages: Optional[int],
) -> Check:
    """Report the observed Project page count against the old page size.

    The comparison is derived from the rows and requests in this doctor run,
    so it describes the saving for the same board rather than comparing two
    different snapshots. The historical #655 API number remains a fixed
    baseline from the parent plan and is quoted for continuity.
    """
    if (
        project_item_count is None
        or project_item_pages is None
        or project_item_pages == 0
    ):
        return Check("Project pagination", True, "", "")
    if (
        isinstance(project_item_count, bool)
        or not isinstance(project_item_count, int)
        or isinstance(project_item_pages, bool)
        or not isinstance(project_item_pages, int)
        or project_item_count < 0
        or project_item_pages < 0
    ):
        return Check(
            "Project pagination", False,
            "Project page measurement is malformed", "rerun funnel doctor",
        )

    previous_pages = max(
        1,
        (project_item_count + PROJECT_ITEM_PREVIOUS_PAGE_SIZE - 1)
        // PROJECT_ITEM_PREVIOUS_PAGE_SIZE,
    )
    difference = previous_pages - project_item_pages
    if difference > 0:
        saving = "{} fewer, {:.1f}% fewer".format(
            difference, difference * 100.0 / previous_pages
        )
    elif difference < 0:
        saving = "{} more, {:.1f}% more".format(
            -difference, -difference * 100.0 / previous_pages
        )
    else:
        saving = "no reduction, 0.0% fewer"
    found = (
        "{} Project row(s) used {} page request(s) at first:{}; first:{} "
        "would require {} page request(s) for the same count ({}); "
        "#655 before: 42 API calls and 47 GraphQL points"
    ).format(
        project_item_count,
        project_item_pages,
        PROJECT_ITEM_PAGE_SIZE,
        PROJECT_ITEM_PREVIOUS_PAGE_SIZE,
        previous_pages,
        saving,
    )
    return Check("Project pagination", True, found, "")


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
    project_item_pages = None
    project_item_count = None
    try:
        items = load_items()
        project_item_pages, project_item_count = project_item_load_measurement()
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
    checks.append(check_project_pagination(
        project_item_count, project_item_pages
    ))
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

# GitHub permits up to 100 Project items per connection page. The brief reads
# the same Project view on every run, so use the largest bounded page to avoid
# paying the per-request latency for multiple 50-item pages. The doctor reports
# the same-item-count comparison for #660, and the cursor still makes this safe
# for a board larger than one page.
PROJECT_ITEM_PAGE_SIZE = 100
PROJECT_ITEM_PREVIOUS_PAGE_SIZE = 50

ITEM_QUERY = """
query($login: String!, $number: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  user(login: $login) {
    projectV2(number: $number) {
      items(first: %d, after: $cursor) {
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
          origin: fieldValueByName(name: "Origin") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          risk: fieldValueByName(name: "Risk") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          pinned: fieldValueByName(name: "Pinned") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          needs: fieldValueByName(name: "Needs") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            ... on Issue {
              number title url body state stateReason createdAt closedAt
              repository { nameWithOwner }
              labels(first: 25) { nodes { name } }
              assignees(first: 10) { nodes { login } }
              parent { number repository { nameWithOwner } }
              subIssuesSummary { total completed }
              blockedBy(first: 50) {
                totalCount
                nodes { number state stateReason repository { nameWithOwner } }
              }
            }
          }
        }
      }
    }
  }
}
""" % PROJECT_ITEM_PAGE_SIZE


SHAPE_ISSUE_COMMENTS_PAGE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 100, after: $cursor) {
        nodes { author { login } body createdAt }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def _item_query_with_shape_comments(repo: str, number: int) -> str:
    """Add the target idea's first comment page to the Project item query."""
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise GitHubError("invalid repository ref {}".format(repo)) from exc
    if (not owner or not name or not isinstance(number, int)
            or isinstance(number, bool) or number < 1):
        raise GitHubError("invalid shape issue ref {}#{}".format(repo, number))
    closing = ITEM_QUERY.rfind("\n}")
    if closing < 0:
        raise GitHubError("could not extend the Project item query")
    issue_field = (
        "\n  shapeIssue: repository(owner: {}, name: {}) {{\n"
        "    issue(number: {}) {{\n"
        "      comments(first: 100) {{\n"
        "        nodes {{ author {{ login }} body createdAt }}\n"
        "        pageInfo {{ hasNextPage endCursor }}\n"
        "      }}\n"
        "    }}\n"
        "  }}\n"
    ).format(json.dumps(owner), json.dumps(name), number)
    return ITEM_QUERY[:closing] + issue_field + ITEM_QUERY[closing:]


def _shape_issue_comments_from_response(
    data: object, repo: str, number: int,
) -> List[Dict[str, object]]:
    """Read every issue comment, failing closed on an incomplete connection."""
    repository = data.get("shapeIssue") if isinstance(data, dict) else None
    issue = repository.get("issue") if isinstance(repository, dict) else None
    connection = issue.get("comments") if isinstance(issue, dict) else None
    comments: List[Dict[str, object]] = []
    cursors = set()

    def add_page(page: object) -> Tuple[bool, Optional[str]]:
        if not isinstance(page, dict):
            raise GitHubError(
                "could not read comments for {}#{}".format(repo, number)
            )
        nodes = page.get("nodes")
        page_info = page.get("pageInfo")
        if (not isinstance(nodes, list) or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)):
            raise GitHubError(
                "could not read a complete comments connection for {}#{}".
                format(repo, number)
            )
        for node in nodes:
            if not isinstance(node, dict):
                raise GitHubError(
                    "could not read a complete comments connection for {}#{}".
                    format(repo, number)
                )
            comments.append(node)
        has_next = page_info["hasNextPage"]
        cursor = page_info.get("endCursor")
        if has_next and (not isinstance(cursor, str) or not cursor):
            raise GitHubError(
                "comments pagination is incomplete for {}#{}".
                format(repo, number)
            )
        return has_next, cursor if isinstance(cursor, str) else None

    if not isinstance(connection, dict):
        raise GitHubError("could not read comments for {}#{}".format(
            repo, number
        ))
    has_next, cursor = add_page(connection)
    owner, name = repo.split("/", 1)
    while has_next:
        if cursor in cursors:
            raise GitHubError(
                "comments pagination did not advance for {}#{}".
                format(repo, number)
            )
        cursors.add(cursor)
        page_data = gh_graphql(
            SHAPE_ISSUE_COMMENTS_PAGE_QUERY,
            owner=owner, name=name, number=number, cursor=cursor,
        )
        page_repository = (
            page_data.get("repository")
            if isinstance(page_data, dict) else None
        )
        page_issue = (
            page_repository.get("issue")
            if isinstance(page_repository, dict) else None
        )
        page_connection = (
            page_issue.get("comments") if isinstance(page_issue, dict) else None
        )
        has_next, cursor = add_page(page_connection)
    return comments

# The paged list is the cheap gate input. History and child timestamps are
# fetched below only for the candidate items a caller has kept after its cheap
# checks; they do not belong on every Project row.
PROJECT_ITEM_DETAIL_BATCH_SIZE = 100

ITEM_DETAILS_QUERY = """
query($ids: [ID!]!, $childIds: [ID!]!) {
  rateLimit { cost remaining resetAt }
  history: nodes(ids: $ids) {
    ... on ProjectV2Item {
      id
      content {
        ... on Issue {
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
  children: nodes(ids: $childIds) {
    ... on ProjectV2Item {
      id
      content {
        ... on Issue {
          subIssues(first: 50) {
            nodes { createdAt closedAt }
          }
        }
      }
    }
  }
}
"""

# GitHub's CLI does not pass an empty list variable, so a detail batch with no
# child-bearing rows uses this timeline-only form instead of sending an invalid
# required ``childIds`` variable. It preserves the full timeline query above
# without asking for child timestamp connections that cannot contribute.
ITEM_TIMELINE_DETAILS_QUERY = """
query($ids: [ID!]!) {
  rateLimit { cost remaining resetAt }
  nodes(ids: $ids) {
    ... on ProjectV2Item {
      id
      content {
        ... on Issue {
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
"""


def _item_detail_request(
    ids: Sequence[str], child_ids: Sequence[str],
) -> Tuple[str, Dict[str, List[str]], str, Optional[str]]:
    """Assemble one batched document for history and optional child nodes.

    History is fetched for every selected Project item in one ``nodes`` list.
    Child timestamps share that GraphQL document when any selected item has
    children; otherwise use the smaller history-only document.
    """
    variables = {"ids": list(ids)}
    if child_ids:
        variables["childIds"] = list(child_ids)
        return ITEM_DETAILS_QUERY, variables, "history", "children"
    return ITEM_TIMELINE_DETAILS_QUERY, variables, "nodes", None


ITEM_LOCK_QUERY = """
query($item: ID!) {
  rateLimit { cost remaining resetAt }
  node(id: $item) {
    ... on ProjectV2Item {
      lock: fieldValueByName(name: "In motion since") {
        ... on ProjectV2ItemFieldTextValue { text }
      }
    }
  }
}
"""


class GitHubError(RuntimeError):
    def __init__(self, message: object, *, transient: bool = False,
                 request_id: Optional[str] = None):
        super().__init__(message)
        self.transient = transient
        self.request_id = request_id


class BeginReserveStop(RuntimeError):
    """Stop before the Project read when the first query shows low headroom."""

    def __init__(self, result: Dict[str, object]):
        self.result = result
        super().__init__(str(result.get("why") or "begin reserve gate"))


# `gh api graphql` occasionally returns a partial JSON document. The CLI
# reports the same condition as `unexpected end of JSON input` when it cannot
# decode the response itself. Keep this retry deliberately narrow: a real
# GraphQL error, an exhausted route, or an ordinary CLI failure must still
# stop on its first attempt.
GRAPHQL_MAX_ATTEMPTS = 3
GRAPHQL_RETRY_DELAY_SECONDS = 0.5
GRAPHQL_TRUNCATED_RESPONSE_SIGNALS = (
    "unexpected end of json input",
    "unexpected end of input",
    "unexpected eof",
)
# A gateway error is GitHub's server timing out, not an answer. The batched PR
# scan runs close to that ~10 s limit, so one 502 or 504 used to fail a whole
# begin closed: three of four Codex ticks on 2026-09-17 (#1030). It gets the
# same bounded retry, after a longer pause than a truncated response.
GRAPHQL_GATEWAY_RETRY_DELAY_SECONDS = 2.0
GRAPHQL_GATEWAY_ERROR_RE = re.compile(r"\bHTTP 50[234]\b")
GRAPHQL_REQUEST_ID_RE = re.compile(
    r"(?:graphql\s+request\s+id|x-github-request-id|request[-\s]+id)"
    r"\s*:?\s*([A-Za-z0-9][A-Za-z0-9:._-]*)",
    re.IGNORECASE,
)


#: Per-process GraphQL spend, with one call counted per attempted response and
#: points accumulated only from the responses themselves.
#:
#: Not state of record and it never outlives the process, so `GitHub is the
#: state` is untouched. It exists because consumption was invisible until it
#: hit zero: on 2026-09-08 `funnel.py` stopped entirely — `brief`, `queue`,
#: `next`, every gate command — for the better part of an hour, several times
#: in one day, with no earlier symptom to read (#273).
_GRAPHQL_SPEND: Dict[str, object] = {
    "calls": 0, "cost": 0, "remaining": None, "reset_at": None,
}

# Caller names are deliberately code-owned so a typo cannot silently create a
# new accounting lane. Calls without a known caller or a readable cost go in
# the explicit unattributed bucket.
GRAPHQL_CALLERS = (
    "standard", "escalated", "publisher", "watch", "begin", "breakdown",
)
GRAPHQL_UNATTRIBUTED = "unattributed"
_GRAPHQL_CALLER_SPEND: Dict[str, Dict[str, object]] = {}
_ACTIVE_GRAPHQL_CALLER: contextvars.ContextVar = contextvars.ContextVar(
    "active_graphql_caller", default=GRAPHQL_UNATTRIBUTED
)

#: Number of Project item list requests and rows in this command. These are
#: per-process measurements for doctor output, not funnel state.
_PROJECT_ITEM_PAGE_COUNT = 0
_PROJECT_ITEM_ROW_COUNT = 0

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

# A brief's existing ``timings`` field is also the safe place for temporary
# diagnosis of its expensive inputs.  The profile is active only while a
# brief command loads and renders; all writes are best effort so timing can
# never make the command it observes fail.
_ACTIVE_BRIEF_TIMINGS: contextvars.ContextVar = contextvars.ContextVar(
    "active_brief_timings", default=None
)


def _graphql_operation(query: str) -> str:
    """Name the read shape represented by one GraphQL query."""
    compact = " ".join(query.split())
    if "repositories(first:" in compact:
        return "member_repos"
    if "pullRequests(first:" in compact:
        return "pull_requests"
    if "nodes(ids:" in compact:
        return "project_item_details"
    if "projectV2(number:" in compact and "items(first:" in compact:
        return "project_items"
    if "comments(last:" in compact:
        return "brief_comments"
    if "fields(first:" in compact:
        return "project_fields"
    if "userContentEdits" in compact:
        return "issue_edits"
    if "timelineItems" in compact:
        return "issue_timeline"
    if "subIssues(first:" in compact:
        return "sub_issues"
    if compact.startswith("mutation"):
        return "mutation"
    return "other"


def _record_brief_graphql_timing(query: str, elapsed: float) -> None:
    """Aggregate one GraphQL operation into the active brief profile."""
    timings = _ACTIVE_BRIEF_TIMINGS.get()
    if timings is None:
        return
    try:
        key = "graphql.{}".format(_graphql_operation(query))
        timings[key] = round(
            float(timings.get(key, 0.0)) + max(0.0, elapsed), 6
        )
        calls_key = key + ".calls"
        timings[calls_key] = int(timings.get(calls_key, 0)) + 1
    except Exception:
        # Instrumentation must not gate or otherwise alter the brief.
        return

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

# This is deliberately outside the ``skipped-*`` family.  A begin that could
# not start because the shared pool was empty is visible health data, not a
# normal budget decline (#932).
BEGIN_BUDGET_EXHAUSTED_OUTCOME = "budget-exhausted"


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


def _graphql_text(value: object) -> str:
    """Return subprocess text in a form safe for matching and diagnostics."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _graphql_request_id(text: object) -> Optional[str]:
    """Extract GitHub's request identifier from a CLI diagnostic, if present."""
    match = GRAPHQL_REQUEST_ID_RE.search(_graphql_text(text))
    return match.group(1) if match else None


def _is_truncated_graphql_text(text: object) -> bool:
    """Whether CLI text identifies the known partial-response failure."""
    lowered = _graphql_text(text).lower()
    return any(signal in lowered for signal in GRAPHQL_TRUNCATED_RESPONSE_SIGNALS)


def _is_gateway_error_text(text: object) -> bool:
    """Whether CLI text reports a GitHub 502, 503 or 504."""
    return bool(GRAPHQL_GATEWAY_ERROR_RE.search(_graphql_text(text)))


def _record_graphql_attempt() -> None:
    """Count one GraphQL subprocess attempt, even without a usable response."""
    _GRAPHQL_SPEND["calls"] = int(_GRAPHQL_SPEND["calls"]) + 1


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


def graphql_caller_spend() -> Dict[str, Dict[str, object]]:
    """Snapshot this process's GraphQL readings and totals by caller."""
    result = {}
    for caller, values in _GRAPHQL_CALLER_SPEND.items():
        readings = values.get("readings")
        result[caller] = {
            **values,
            "readings": (
                [dict(reading) for reading in readings
                 if isinstance(reading, dict)]
                if isinstance(readings, list) else []
            ),
        }
    return result


@contextlib.contextmanager
def graphql_caller(caller: Optional[str]) -> Iterator[None]:
    """Attribute GraphQL responses inside this scope to one known caller."""
    selected = caller if caller in GRAPHQL_CALLERS else GRAPHQL_UNATTRIBUTED
    token = _ACTIVE_GRAPHQL_CALLER.set(selected)
    try:
        yield
    finally:
        _ACTIVE_GRAPHQL_CALLER.reset(token)


def _graphql_tier_caller(tier: Optional[str]) -> str:
    return tier if tier in ("standard", "escalated") else GRAPHQL_UNATTRIBUTED


def _argv_value(argv: Sequence[str], name: str) -> Optional[str]:
    try:
        index = list(argv).index(name)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def graphql_caller_for_run(run: Optional[str],
                           agent: Optional[str] = None) -> str:
    """Read a run's lane from its local heartbeat start, without GitHub reads."""
    run = run or os.environ.get("COMMAND_CENTER_RUN")
    agent = agent or os.environ.get("COMMAND_CENTER_AGENT")
    spool = pathlib.Path(
        os.environ.get("COMMAND_CENTER_HEARTBEAT_SPOOL")
        or pathlib.Path.home() / ".claude" / "command-center-heartbeat"
    )
    starts = {}
    finishes = set()
    try:
        for path in sorted(spool.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("phase") == "start" and row.get("run"):
                    starts[row["run"]] = row
                elif row.get("phase") == "finish" and row.get("run"):
                    finishes.add(row["run"])
    except (OSError, UnicodeError):
        return GRAPHQL_UNATTRIBUTED
    if run is None:
        open_starts = [
            key for key, row in starts.items()
            if key not in finishes and (not agent or row.get("agent") == agent)
        ]
        if len(open_starts) != 1:
            return GRAPHQL_UNATTRIBUTED
        run = open_starts[0]
    start = starts.get(run)
    if start is None or (agent and start.get("agent") != agent):
        return GRAPHQL_UNATTRIBUTED
    return _graphql_tier_caller(start.get("tier"))


def graphql_caller_for_command(argv: Sequence[str], *,
                               tier: Optional[str] = None) -> str:
    """Classify a funnel command using its explicit job or heartbeat lane."""
    if not argv:
        return GRAPHQL_UNATTRIBUTED
    command = argv[0]
    if command == "begin":
        return "begin"
    if command == "brief":
        return "publisher"
    if command in ("main-ci", "watch"):
        return "watch"
    if command in ("breakdown-packet", "breakdown-apply"):
        return "breakdown"
    if command in ("next", "next-review"):
        command_tier = _argv_value(argv, "--tier") or tier
        return _graphql_tier_caller(command_tier)
    command_tier = tier or _argv_value(argv, "--tier")
    if command_tier is None:
        command_tier = graphql_caller_for_run(
            _argv_value(argv, "--run"), _argv_value(argv, "--agent")
        )
    return _graphql_tier_caller(command_tier)


def _graphql_response_timestamp() -> Optional[float]:
    """Read a best-effort receipt time without gating a GraphQL response."""
    try:
        received_at = float(time.time())
    except Exception:
        return None
    return received_at if math.isfinite(received_at) and received_at >= 0 else None


def _record_graphql_caller_response(
        block: object, received_at: Optional[float]) -> None:
    """Record one response, including its window metadata and receipt time."""
    values = block if isinstance(block, dict) else {}
    cost = values.get("cost")
    cost_readable = (
        isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0
    )
    caller = _ACTIVE_GRAPHQL_CALLER.get()
    if caller not in GRAPHQL_CALLERS or not cost_readable:
        caller = GRAPHQL_UNATTRIBUTED

    bucket = _GRAPHQL_CALLER_SPEND.get(caller)
    if bucket is None:
        bucket = {
            "calls": 0, "points": 0, "remaining": None, "readings": [],
        }
        _GRAPHQL_CALLER_SPEND[caller] = bucket
    bucket["calls"] = int(bucket.get("calls") or 0) + 1
    if cost_readable:
        if bucket.get("points") is not None:
            bucket["points"] = int(bucket["points"] or 0) + cost
    else:
        bucket["points"] = None

    remaining = values.get("remaining")
    if (isinstance(remaining, int) and not isinstance(remaining, bool)
            and remaining >= 0):
        bucket["remaining"] = remaining

    readings = bucket["readings"]
    reset_at = values.get("resetAt")
    reading = {
        "cost": cost if cost_readable else None,
        "remaining": (
            remaining
            if isinstance(remaining, int) and not isinstance(remaining, bool)
            and remaining >= 0 else None
        ),
        "reset_at": (
            reset_at.strip()
            if isinstance(reset_at, str) and reset_at.strip() else None
        ),
        "received_at": received_at,
    }
    if isinstance(readings, list):
        readings.append(reading)


def _budget_exhaustion_signal() -> Optional[Tuple[int, str]]:
    """Return the structured zero-budget signal, if this run observed it.

    The rate-limit error text is not a sufficient discriminator: it is a
    diagnostic string and can be emitted for several transport shapes.  A
    named lost tick requires both values GitHub reports in ``rateLimit``.
    """
    spend = graphql_spend()
    remaining = spend.get("remaining")
    reset_at = spend.get("reset_at")
    if (
        isinstance(remaining, int)
        and not isinstance(remaining, bool)
        and remaining == 0
        and isinstance(reset_at, str)
        and reset_at.strip()
    ):
        return remaining, reset_at.strip()
    return None


def reset_api_usage() -> None:
    """Start a fresh per-command API measurement."""
    global _GRAPHQL_COST_READS, _PROJECT_ITEM_PAGE_COUNT
    global _PROJECT_ITEM_ROW_COUNT
    _API_USAGE.update({"graphql_calls": 0, "cli_calls": 0, "refused_exhausted": 0})
    _GRAPHQL_SPEND.update(
        {"calls": 0, "cost": 0, "remaining": None, "reset_at": None}
    )
    _GRAPHQL_COST_READS = 0
    _GRAPHQL_CALLER_SPEND.clear()
    _PROJECT_ITEM_PAGE_COUNT = 0
    _PROJECT_ITEM_ROW_COUNT = 0


def project_item_load_measurement() -> Tuple[int, int]:
    """Return this process's Project-page and raw-row counts."""
    return _PROJECT_ITEM_PAGE_COUNT, _PROJECT_ITEM_ROW_COUNT


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
    command = _frozen_ticket_create_command(list(args))
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
    started = time.perf_counter()
    try:
        cmd = ["gh", "api", "graphql", "-f", "query=" + query]
        for key, value in variables.items():
            if isinstance(value, (list, tuple)):
                # `gh api` builds GraphQL list variables from repeated `key[]`
                # fields. Passing Python's repr as one raw field produces an
                # invalid GraphQL variable (and silently turns a batch into a
                # failed per-item fallback), so keep list encoding here with
                # the scalar path in one place.
                for entry in value:
                    cmd += ["-F", "{}[]={}".format(key, entry)]
                continue
            flag = "-F" if isinstance(value, (int, bool)) else "-f"
            cmd += [flag, "{}={}".format(key, value)]
        last_request_id = None

        for attempt in range(GRAPHQL_MAX_ATTEMPTS):
            try:
                proc = _run_gh(cmd, capture_output=True, text=True)
            except GitHubError:
                # The exhausted-route guard raises before a subprocess is run,
                # so it is not an attempt and must not be counted a second time.
                raise
            except Exception:
                # `_run_gh` has already counted this launch in `_API_USAGE`.
                # Keep the GraphQL spend count aligned even when no child
                # response exists to carry a rate-limit block.
                _record_graphql_attempt()
                _record_graphql_caller_response(None, None)
                raise

            received_at = _graphql_response_timestamp()
            _record_graphql_attempt()
            stderr = _graphql_text(getattr(proc, "stderr", ""))
            stdout = getattr(proc, "stdout", "")
            request_id = _graphql_request_id(stderr)
            if request_id is None:
                request_id = _graphql_request_id(stdout)
            if request_id is not None:
                last_request_id = request_id

            if proc.returncode != 0:
                # A failed CLI command can still carry a partial GraphQL
                # response. Read its rateLimit block when available; otherwise
                # keep the attempted cost visible under `unattributed`.
                try:
                    error_payload = json.loads(stdout)
                except (TypeError, ValueError):
                    error_payload = None
                error_data = (
                    error_payload.get("data")
                    if isinstance(error_payload, dict) else None
                )
                error_block = (
                    error_data.get("rateLimit")
                    if isinstance(error_data, dict) else None
                )
                _record_graphql_caller_response(error_block, received_at)
                if isinstance(error_data, dict):
                    _record_rate_limit(error_block)
                    if (isinstance(error_block, dict)
                            and error_block.get("remaining") == 0):
                        _mark_exhausted(
                            "rateLimit.remaining is 0",
                            error_block.get("resetAt"),
                        )
                detail = stderr.strip() or _graphql_text(stdout).strip()
                detail = detail or "gh exited {}".format(proc.returncode)
                # Rate-limit exhaustion is a deliberate fail-fast path, even
                # if a CLI diagnostic happens to include another transport phrase.
                if _is_exhausted_signal(detail):
                    raise GitHubError(detail, request_id=request_id)
                gateway = _is_gateway_error_text(detail)
                error = GitHubError(
                    detail,
                    transient=gateway or _is_truncated_graphql_text(stderr),
                    request_id=request_id or last_request_id,
                )
                if not error.transient or attempt + 1 >= GRAPHQL_MAX_ATTEMPTS:
                    raise error
                time.sleep(
                    GRAPHQL_GATEWAY_RETRY_DELAY_SECONDS if gateway
                    else GRAPHQL_RETRY_DELAY_SECONDS
                )
                continue

            try:
                payload = json.loads(stdout)
            except (TypeError, ValueError) as exc:
                _record_graphql_caller_response(None, received_at)
                error = GitHubError(
                    "malformed GraphQL response: {}".format(exc),
                    transient=True,
                    request_id=request_id or last_request_id,
                )
                if attempt + 1 >= GRAPHQL_MAX_ATTEMPTS:
                    raise error from exc
                time.sleep(GRAPHQL_RETRY_DELAY_SECONDS)
                continue

            if not isinstance(payload, dict):
                _record_graphql_caller_response(None, received_at)
                error = GitHubError(
                    "malformed GraphQL response: top-level JSON is not an object",
                    transient=True,
                    request_id=request_id or last_request_id,
                )
                if attempt + 1 >= GRAPHQL_MAX_ATTEMPTS:
                    raise error
                time.sleep(GRAPHQL_RETRY_DELAY_SECONDS)
                continue

            data = payload.get("data")
            # GitHub can return a partial GraphQL answer: the error tells us
            # the query failed while the data-side rateLimit block still
            # carries the authoritative remaining/resetAt pair. Record it
            # before handling errors so begin can classify that failure
            # without matching prose or an exit code.
            block = data.get("rateLimit") if isinstance(data, dict) else None
            _record_graphql_caller_response(block, received_at)
            if isinstance(data, dict):
                _record_rate_limit(block)
                if isinstance(block, dict) and block.get("remaining") == 0:
                    _mark_exhausted(
                        "rateLimit.remaining is 0", block.get("resetAt")
                    )

            if payload.get("errors"):
                raise GitHubError(
                    json.dumps(payload["errors"]), request_id=request_id
                )

            if not isinstance(data, dict):
                error = GitHubError(
                    "malformed GraphQL response: data is not an object",
                    transient=True,
                    request_id=request_id or last_request_id,
                )
                if attempt + 1 >= GRAPHQL_MAX_ATTEMPTS:
                    raise error
                time.sleep(GRAPHQL_RETRY_DELAY_SECONDS)
                continue

            return data

        # The loop always returns or raises. Keep a defensive error for static
        # analyzers and for a future change to the attempt constants.
        raise GitHubError(
            "GraphQL request failed after {} attempts".format(GRAPHQL_MAX_ATTEMPTS),
            transient=True,
            request_id=last_request_id,
        )
    finally:
        _record_brief_graphql_timing(
            query, time.perf_counter() - started
        )


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


def _record_begin_load_phase(
    timings: Optional[Dict[str, object]], phase: str, elapsed: float,
) -> None:
    """Add one non-gating begin-load duration to the existing timings map."""
    if timings is None:
        return
    try:
        timings["begin_load." + phase] = round(max(0.0, elapsed), 6)
    except Exception:
        # Instrumentation must not gate the command it instruments.
        return


def _begin_load_timed(
    timings: Optional[Dict[str, object]],
    phase: str,
    reader: Callable[[], Any],
) -> Any:
    """Run one begin-load phase and record its duration when requested."""
    if timings is None:
        return reader()
    started = time.perf_counter()
    try:
        return reader()
    finally:
        _record_begin_load_phase(
            timings, phase, time.perf_counter() - started
        )


def _report_begin_phase_boundary(phase: str, started: float) -> None:
    """Write a non-gating elapsed marker before a post-load begin phase.

    Inside a session server, ``dispatch`` captures ``sys.stderr`` and returns
    it only with the reply, so a begin that overruns the client's reply
    budget would lose every marker it wrote. The markers exist for exactly
    that fire (#1519), so there they go to the server's own stderr, which is
    the runner's error log.
    """
    try:
        elapsed = max(0.0, time.perf_counter() - started)
        stream = sys.stderr
        if os.environ.get(SESSION_SERVER_ENV) and sys.__stderr__ is not None:
            stream = sys.__stderr__
        print(
            "begin_review_phase phase={} elapsed_seconds={:.6f}".format(
                phase, elapsed,
            ),
            file=stream,
            flush=True,
        )
    except Exception:
        # Instrumentation must not gate the command it instruments.
        return


def member_repos(
    after_first_response: Optional[Callable[[Mapping[str, object]], None]] = None,
) -> List[str]:
    """Repos that opted in by carrying the topic. Never an allowlist."""
    members: List[str] = []
    first_response = after_first_response
    for kind, login in OWNERS:
        query = REPO_QUERY.replace("OWNER_KIND", kind).replace("OWNER_LOGIN", login)
        cursor = None
        while True:
            data = gh_graphql(query, **({"cursor": cursor} if cursor else {}))
            if first_response is not None:
                callback = first_response
                first_response = None
                callback(data)
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


def capture_repo(repo: Optional[str], run: Optional[str] = None,
                 agent: Optional[str] = None) -> str:
    """Where a capture goes: the flag, else the run's own work, else the sole member.

    With one member repo the answer was never in doubt. When a second repo
    joined on 2026-09-12, ``resolve_repo`` refused every routine capture,
    because no routine passes ``--repo`` -- the agents' only channel for
    reporting defects closed (#668). A routine captures what it saw while
    working its ticket or reviewing its PR, so the run's ``bind`` record is
    the right default; the explicit flag still wins, and two members with no
    binding still refuse rather than guess.
    """
    if repo:
        return repo
    run, agent = _heartbeat_context(run, agent)
    if run and agent:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import heartbeat

            bound = heartbeat.bindings(heartbeat.read(agent)).get(run)
        except Exception:
            bound = None
        if bound:
            if bound.get("repo"):
                return str(bound["repo"])
            work = str(bound.get("work") or "")
            if "#" in work and "/" in work.split("#", 1)[0]:
                return work.split("#", 1)[0]
    return resolve_repo(None)


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


def _apply_item_timeline_fields(item: Item, content: dict) -> None:
    """Apply status and blocking history from a targeted Project read."""
    item.status_events = []
    item.status_since = None
    item.blocked_since = None
    item.blocked_cleared_at = None
    matching_status_times = []

    timeline_nodes = ((content.get("timelineItems") or {}).get("nodes") or [])
    for event in timeline_nodes:
        if not isinstance(event, dict):
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
                if item.status is not None and event.get("status") == item.status:
                    matching_status_times.append(at)

    # Gate age uses the newest transition into the current status in this
    # Project. Select by timestamp so correctness does not depend on connection
    # ordering, and exclude matching status events from other Projects above.
    item.status_since = (
        max(matching_status_times) if matching_status_times else None
    )


def _apply_item_detail_fields(
    item: Item,
    content: dict,
    *,
    include_children: bool = True,
    include_timeline: bool = True,
) -> None:
    """Apply child timestamps and timeline history from targeted Project reads."""
    if include_children:
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

        item.first_child_created_at = min(child_times) if child_times else None
        item.last_child_closed_at = max(child_close_times) if child_close_times else None
    if include_timeline:
        _apply_item_timeline_fields(item, content)


def _blocked_by_refs_from_connection(connection: object) -> Optional[List[str]]:
    """Return complete native blocker refs, or None when unreadable.

    A partial connection cannot prove an edge is absent, so callers that use
    this list to avoid a duplicate write must fail closed on None.
    """
    if not isinstance(connection, dict):
        return None
    nodes = connection.get("nodes")
    total = connection.get("totalCount")
    if (
        not isinstance(nodes, list)
        or not isinstance(total, int)
        or isinstance(total, bool)
        or total < 0
        or total != len(nodes)
    ):
        return None
    refs = []
    for blocker in nodes:
        if not isinstance(blocker, dict):
            return None
        number = blocker.get("number")
        repository = blocker.get("repository")
        repo = (
            repository.get("nameWithOwner")
            if isinstance(repository, dict) else None
        )
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
            or not isinstance(repo, str)
            or not repo.strip()
        ):
            return None
        refs.append("{}#{}".format(repo, number))
    return refs


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
        body=content.get("body"),
        state_reason=content.get("stateReason"),
        created_at=parse_time(content.get("createdAt")),
        status=status,
        klass=(node.get("class") or {}).get("name"),
        origin=(node.get("origin") or {}).get("name"),
        risk=(node.get("risk") or {}).get("name"),
        pinned=(node.get("pinned") or {}).get("name") == "Pinned",
        needs=(node.get("needs") or {}).get("name"),
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
        blocked_by_refs=_blocked_by_refs_from_connection(
            content.get("blockedBy")
        ),
    )
    # Keep fixture and caller-supplied full nodes compatible while the live
    # paged query stays compact. A targeted read can apply these fields again.
    _apply_item_detail_fields(item, content)

    # Native dependencies apply to tickets, not the parent project, and the
    # same open/dead split the REST helper produces. This comes out of the
    # Project query that already ran, so it costs no extra request.
    if item.state == "OPEN" and item.parent:
        blockers = (content.get("blockedBy") or {}).get("nodes") or []
        dependencies = classify_blockers(blockers, item.repo)
        item.open_blockers = dependencies["open"]
        item.dead_blockers = dependencies["dead"]

    return item


def hydrate_item_details(
    items: Sequence[Item], candidates: Optional[Iterable[Item]] = None,
) -> None:
    """Read child timestamps and timeline history for selected Project items.

    The paged Project list is deliberately the cheap candidate scan. Callers
    that need a full board view may omit ``candidates``; queue paths pass only
    the items that survived their cheap gates. A missing detail response is a
    load failure, never permission to guess at gate age.
    """
    by_id = {
        item.item_id: item
        for item in items
        if isinstance(item.item_id, str) and item.item_id
    }
    selected = list(items if candidates is None else candidates)
    ids = []
    child_ids = []
    seen: Set[str] = set()
    for item in selected:
        item_id = item.item_id
        if (
            isinstance(item_id, str)
            and item_id in by_id
            and item_id not in seen
        ):
            ids.append(item_id)
            seen.add(item_id)
            if item.children_total > 0:
                child_ids.append(item_id)
    if not ids:
        return

    child_id_set = set(child_ids)
    for start in range(0, len(ids), PROJECT_ITEM_DETAIL_BATCH_SIZE):
        batch = ids[start:start + PROJECT_ITEM_DETAIL_BATCH_SIZE]
        child_batch = [item_id for item_id in batch if item_id in child_id_set]
        query, variables, history_field, child_field = _item_detail_request(
            batch, child_batch
        )
        data = gh_graphql(query, **variables)
        if not isinstance(data, dict):
            timeline_nodes = None
            child_nodes = None if child_field is not None else []
        else:
            timeline_nodes = data.get(history_field)
            child_nodes = (
                data.get(child_field) if child_field is not None else []
            )
        if (
            not isinstance(timeline_nodes, list)
            or not isinstance(child_nodes, list)
        ):
            raise GitHubError("Project item detail response was malformed")
        for node in timeline_nodes:
            if not isinstance(node, dict):
                continue
            item = by_id.get(node.get("id"))
            content = node.get("content")
            if item is not None and isinstance(content, dict):
                _apply_item_detail_fields(
                    item, content, include_children=False
                )
        for node in child_nodes:
            if not isinstance(node, dict):
                continue
            item = by_id.get(node.get("id"))
            content = node.get("content")
            if item is not None and isinstance(content, dict):
                _apply_item_detail_fields(
                    item, content, include_timeline=False
                )


def load_items(
    include_details: bool = True,
    member_repo_names: Optional[Sequence[str]] = None,
    timings: Optional[Dict[str, object]] = None,
    shape_issue: Optional[Tuple[str, int]] = None,
) -> List[Item]:
    global _PROJECT_ITEM_PAGE_COUNT, _PROJECT_ITEM_ROW_COUNT
    members = set(
        member_repo_names
        if member_repo_names is not None
        else _begin_load_timed(timings, "member_repos", member_repos)
    )
    items: List[Item] = []
    cursor = None
    shape_comments: Optional[List[Dict[str, object]]] = None
    project_started = time.perf_counter() if timings is not None else None
    block_comment_seconds = 0.0
    try:
        while True:
            variables = {"login": PROJECT_OWNER, "number": PROJECT_NUMBER}
            if cursor:
                variables["cursor"] = cursor
            _PROJECT_ITEM_PAGE_COUNT += 1
            query = ITEM_QUERY
            if shape_issue is not None and cursor is None:
                query = _item_query_with_shape_comments(*shape_issue)
            response = gh_graphql(query, **variables)
            if shape_issue is not None and cursor is None:
                shape_comments = _shape_issue_comments_from_response(
                    response, *shape_issue
                )
            project = response["user"]["projectV2"]
            if project is None:
                raise GitHubError(
                    "Project {}/{} not found or not visible".format(
                        PROJECT_OWNER, PROJECT_NUMBER
                    )
                )
            page = project["items"]
            nodes = page["nodes"]
            _PROJECT_ITEM_ROW_COUNT += len(nodes)
            for node in nodes:
                item = _from_node(node)
                # Membership is the topic. An item whose repo has not opted in
                # is outside the funnel even though it sits in the Project.
                if item and item.repo in members:
                    # Dependencies are already on the item: `_from_node` reads
                    # them from `blockedBy` in the Project query. They used to
                    # be fetched here instead, one REST call per open ticket —
                    # do not restore a per-item dependency read in this loop.
                    if item.state == "OPEN" and item.is_blocked:
                        comment_started = time.perf_counter()
                        try:
                            _load_block_comment(item)
                        finally:
                            block_comment_seconds += (
                                time.perf_counter() - comment_started
                            )
                    items.append(item)
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    finally:
        if project_started is not None:
            project_elapsed = max(
                0.0,
                time.perf_counter() - project_started - block_comment_seconds,
            )
            _record_begin_load_phase(
                timings, "project_items", project_elapsed,
            )
            _record_begin_load_phase(
                timings, "block_comments", block_comment_seconds
            )
    if shape_issue is not None:
        if shape_comments is None:
            raise GitHubError(
                "could not read comments for {}#{}".format(*shape_issue)
            )
        target_ref = "{}#{}".format(*shape_issue)
        for item in items:
            if item.ref == target_ref:
                item.issue_comments = shape_comments
                break
    if include_details:
        _begin_load_timed(
            timings, "item_details", lambda: hydrate_item_details(items)
        )
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
    question = gate_question(item)
    acceptance_reason = _acceptance_waiting_reason(item, question)
    rendered = {
        "ref": item.ref,
        "repo": item.repo,
        "title": item.title,
        "url": item.url,
        "status": item.status,
        "class": effective_class(item, by_ref),
        "waiting_on": question,
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
    if acceptance_reason is not None:
        rendered["waiting_reason"] = acceptance_reason
    return rendered


def _dashboard_spool_dir() -> pathlib.Path:
    """Return the display snapshot spool, allowing tests and installs to override it."""
    configured = os.environ.get(DASHBOARD_SPOOL_ENV)
    if configured:
        return pathlib.Path(configured).expanduser()
    return pathlib.Path.home() / ".claude" / "command-center-dashboard-spool"


def _dashboard_stage_since(item: Item) -> Optional[datetime]:
    """Return the best-known time the Project item entered its board stage."""
    if item.status_since is not None:
        return item.status_since
    # A closed Done item can lack the Project timeline event in an old or
    # partial fixture. Its issue close is still a useful lower-fidelity age;
    # never use a gate timestamp here because the board means time at stage.
    if item.status == "Done":
        return item.closed_at
    return None


def _dashboard_stage_age(item: Item, now: datetime) -> str:
    since = _dashboard_stage_since(item)
    if since is None:
        return "unknown"
    return humanise(max(timedelta(0), now - since))


def _dashboard_stage_wait_seconds(item: Item, now: datetime) -> Optional[float]:
    since = _dashboard_stage_since(item)
    if since is None:
        return None
    return round(max(0.0, (now - since).total_seconds()), 3)


#: Who owes the next move on a ticket, for the dashboard's owner flag. These
#: are display names for the reader, derived from facts the funnel already
#: holds: the Needs field, the PR and its verdict, and the risk tier.
OWNER_NATE = "Nate"
OWNER_CLAUDE = "Claude"
OWNER_MUSE = "Muse"
OWNER_CODEX = "Codex"

# Claude's interactive and funnel-watch PRs carry this standard footer. The
# runner-created PRs are attributed from heartbeat finish records instead; a
# missing or ambiguous attribution deliberately falls back to Codex below.
CLAUDE_CODE_PR_RE = re.compile(
    r"Generated with\s+\[Claude Code\]", re.IGNORECASE
)


def _dashboard_block_reason(item: Item) -> Optional[str]:
    """One short phrase saying why a ticket is blocked, or None.

    A block whose comment the funnel could not parse still has its first line,
    which is better than a bare "blocked" chip: #711 carried a date hold whose
    header was missing its colon, so nothing downstream could say why.
    """
    if item.block_reason:
        return item.block_reason.strip().splitlines()[0][:120]
    if item.decline_reason:
        # A decline records why in prose the parser does not read. Showing it
        # beats "no reason recorded", which is what FF#289 and
        # career-toolset#210/#211 looked like on 2026-09-24 (#1432).
        return "declined: " + item.decline_reason[:110]
    for comment in item.unparseable_block_comments or ():
        text = str(comment).strip()
        if text:
            return text.splitlines()[0][:120] + " (unparsed)"
    return None


def _dashboard_implementer(tier: str) -> str:
    """The implementer the roster names for ``tier``: Codex while Codex is on
    it (#1322), Muse only if the roster names Muse alone."""
    if agent_has_role("codex", "implement", tier):
        return OWNER_CODEX
    if agent_has_role("muse", "implement", tier):
        return OWNER_MUSE
    return OWNER_CODEX


def _dashboard_rework_owner(
    tier: str,
    pr_fact: Optional[Mapping[str, object]],
    authoring_agents: Iterable[str] = (),
) -> str:
    """Return the implementer who owns a rejected current PR head.

    Rework follows the same implementation registry used by ``begin``:
    Codex implements both tiers since #1322, so Muse-authored rework goes to
    Codex too, unless Muse is put back on the roster.
    Work that was authored by Claude stays with Claude, at either tier, whether its
    provenance came from a heartbeat-bound funnel run or the Claude Code PR
    footer used by interactive and funnel-watch sessions. Everything else is
    Codex-owned, including unreadable or conflicting attribution.
    """
    agents = {
        str(agent).strip().casefold()
        for agent in authoring_agents
        if str(agent).strip()
    }
    if "claude" in agents:
        return OWNER_CLAUDE
    if agents == {"muse"} and agent_has_role("muse", "implement", tier):
        return OWNER_MUSE

    body = (pr_fact or {}).get("body")
    if isinstance(body, str) and CLAUDE_CODE_PR_RE.search(body):
        return OWNER_CLAUDE

    # The roster's implementer, Codex since #1322. Explicit, so a missing
    # heartbeat or PR description cannot hand work to a reviewer by accident.
    return _dashboard_implementer(tier)


def _dashboard_ticket(
    item: Item,
    pr_fact: Optional[Mapping[str, object]],
    verdict: Optional[Mapping[str, object]],
    queue_rank: Optional[int] = None,
    blockers: Sequence[str] = (),
    pr_known: bool = True,
    parent_block: Optional[str] = None,
    authoring_agents: Iterable[str] = (),
    siblings: Collection[str] = (),
    projected_turn: Optional[int] = None,
    queue_class: Optional[str] = None,
    unblocks: Sequence[str] = (),
    unblocks_later: Sequence[str] = (),
    paused: Optional[Mapping[str, object]] = None,
    finished: bool = False,
) -> Dict[str, object]:
    """One ticket row for the dashboard, with its PR, tier and owner flags.

    ``pr_fact`` is the row this ticket's PR already produced for the brief's
    shared scan, so this adds no per-ticket read. ``verdict`` normally comes
    from the same batch; the dashboard retains a legacy fallback for fixture
    callers that provide a row without the batch's comment tail.

    A ticket reads as blocked for any block the funnel honours, not only its
    own label: an open native blocker, or a blocked parent (``parent_block``
    is that parent's reason, "" when it has none). Showing only the label left
    #807 and #702 looking like work an engineer could take (#966).

    ``siblings`` are the refs of the other tickets under the same parent. A
    block made only of those is the plan's own sequencing, not a stall, and
    the page draws it apart from a block from outside (Nate, 2026-09-24).
    """
    blocked = bool(
        item.is_blocked or item.open_blockers or parent_block is not None
    )
    blocked_by_siblings = bool(
        blocked and parent_block is None
        and _blocked_only_by(item, siblings)
    )
    sibling_blockers = [
        ref for ref in _block_refs(item) if ref in siblings
    ] if blocked else []
    block_reason = _dashboard_block_reason(item)
    if block_reason is None and parent_block is not None:
        if not parent_block:
            block_reason = "project blocked"
        elif parent_block.startswith("by "):
            block_reason = "project blocked " + parent_block
        else:
            block_reason = "project blocked: " + parent_block
    needs = item.needs
    tier = item.risk
    if tier not in RISK_OPTIONS:
        tier = "escalated"
    matches: List[Dict[str, Optional[str]]] = []

    pr_state = str((pr_fact or {}).get("state") or "").upper()
    pr_number = (pr_fact or {}).get("number")
    head = (pr_fact or {}).get("headRefOid")
    pr: Optional[str] = None
    if not pr_known and item.state == "OPEN":
        # The PR scan failed or timed out. "No PR" would be a guess, and the
        # column cannot tell the two apart (#934).
        pr = "unknown"
    elif pr_state == "MERGED":
        pr = "merged"
    elif pr_state == "OPEN":
        approved = (
            isinstance(verdict, dict)
            and verdict.get("verdict") == "approved"
            and verdict_covers_head(verdict, head)
        )
        rejected = rejected_at_current_head(
            verdict if isinstance(verdict, dict) else None, head
        )
        if rejected:
            pr = "changes requested"
        else:
            pr = "approved" if approved else "submitted"

    if item.state != "OPEN":
        owner: Optional[str] = None
    elif blocked:
        owner = None
    elif finished:
        # Its comments record it done: closing it is Nate's move.
        owner = OWNER_NATE
    elif paused:
        # Held after repeated failed runs; nobody takes it before the hold
        # lifts, and the page says until when.
        owner = None
    elif needs not in NEEDS_OPTIONS:
        owner = None
    elif needs == "claude-code-environment":
        owner = OWNER_CLAUDE
    elif needs == "human":
        owner = OWNER_NATE
    elif pr == "changes requested":
        owner = _dashboard_rework_owner(
            tier, pr_fact, authoring_agents=authoring_agents
        )
    elif pr == "submitted" or pr == "approved":
        # An open PR is the reviewer's move, whichever engine wrote it.
        owner = OWNER_MUSE
    else:
        owner = _dashboard_implementer(tier)

    return {
        "ref": item.ref,
        "number": item.number,
        "title": item.title,
        "url": item.url,
        "state": item.state,
        # Where this ticket sits in the engineers' own queue: 0 is the ticket
        # the next run takes. None means it is not startable — closed, blocked,
        # or already sitting in review. The order is `startable()`'s, never a
        # second opinion computed here.
        "queue_rank": queue_rank,
        "blockers": list(blockers),
        "blocked_until": (
            item.blocked_until.isoformat() if item.blocked_until else None
        ),
        "block_reason": block_reason,
        "pr": pr,
        "pr_number": pr_number if isinstance(pr_number, int) else None,
        "tier": tier,
        "escalation_matches": matches,
        "owner": owner,
        "blocked": blocked,
        "blocked_by_siblings": blocked_by_siblings,
        # The tickets under the same parent this one waits on, which is what
        # lines the bar's blocked segments up behind their blockers.
        "sibling_blockers": sibling_blockers,
        # This ticket's place in ``projected_pull_order``, or None when the
        # projection never reaches it.
        "projected_turn": projected_turn,
        # The class it ranks as (``queue_classes``), and the open tickets
        # waiting on it: why a ticket can rank above its project's class
        # (Nate, 2026-09-24). Open tickets only.
        "class": queue_class if item.state == "OPEN" else None,
        "unblocks": list(unblocks) if item.state == "OPEN" else [],
        # Open tickets further down the chain, freed once those above are.
        "unblocks_later": (
            list(unblocks_later) if item.state == "OPEN" else []
        ),
        "human_step": needs if needs in ("human", "claude-code-environment") else None,
        # Engine holds the Project fields do not show (Nate, 2026-09-25).
        "paused_until": _dashboard_paused_until(paused),
        "paused_failures": (
            paused.get("failures") if isinstance(paused, Mapping) else None
        ),
        "finished_by_comments": bool(finished),
    }


def _dashboard_paused_until(
    paused: Optional[Mapping[str, object]]
) -> Optional[str]:
    """When a backoff hold lifts, as ISO text, or None when not paused."""
    if not isinstance(paused, Mapping):
        return None
    until = paused.get("until")
    if isinstance(until, datetime):
        return until.isoformat()
    return str(until) if until else None


def _block_refs(item: Item) -> List[Optional[str]]:
    """Every ticket ``item``'s block names: native edges, and the block
    comment's refs when it carries the label. ``None`` is an unresolvable
    ref; duplicates are dropped, first mention kept."""
    values = list(item.open_blockers)
    if item.is_blocked:
        values += list(item.block_references)
    refs: List[Optional[str]] = []
    for value in values:
        ref = _dependency_ref(item, value)
        if ref not in refs:
            refs.append(ref)
    return refs


def _blocked_only_by(item: Item, refs: Collection[str]) -> bool:
    """True when every condition on ``item``'s block names one of ``refs``.

    A date hold, or a label with a reason and no references, is a condition
    outside the set, so either one answers False.
    """
    if item.blocked_until is not None:
        return False
    if item.is_blocked and not item.block_references:
        return False
    resolved = _block_refs(item)
    return bool(resolved) and all(ref in refs for ref in resolved)


def _dashboard_item(
    item: Item,
    now: datetime,
    by_ref: Dict[str, Item],
    tickets: Optional[List[Dict[str, object]]] = None,
    next_step_blocked: bool = False,
) -> Dict[str, object]:
    """Render the small parent-project row consumed by the dashboard page."""
    return {
        "repo": item.repo.rsplit("/", 1)[-1],
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "class": effective_class(item, by_ref),
        "pinned": bool(item.pinned),
        "waited": _dashboard_stage_age(item, now),
        "waited_seconds": _dashboard_stage_wait_seconds(item, now),
        "tickets_closed": item.children_done,
        "tickets_total": item.children_total,
        # The owner of the next step in the chain, not every owner on the
        # project: Nate, 2026-09-15, "only the assignment for the next step".
        "next_owner": _dashboard_next_owner(tickets or ()),
        # Nothing on the project can move until a block lifts, whether the
        # block is on the project or on every open ticket. The page reads
        # "Blocked" where the owner would be (Nate, 2026-09-24).
        "next_step_blocked": bool(next_step_blocked),
        # Nothing else on the project can move while a ticket waits out a
        # backoff hold: the page reads "Paused" and says until when.
        "next_step_paused_until": _dashboard_next_paused(
            tickets or (), next_step_blocked
        ),
        "blocked": bool(item.is_blocked),
        "blockers": list(item.block_references) if item.is_blocked else [],
        "block_reason": (
            _dashboard_block_reason(item) if item.is_blocked else None
        ),
        "pips": _dashboard_pips(tickets or ()),
        "tickets": _dashboard_row_order(tickets or ()),
    }


#: Progress order for the sub-issue bar: finished work fills from the left,
#: the way a progress bar reads, whatever order the tickets are queued in.
#: Open work sits left of blocked work (Nate, 2026-09-24). A ticket waiting
#: only on its siblings is "queued"; it and "blocked" share one run, ordered
#: by ``_dashboard_pip_order``.
PIP_PROGRESS_ORDER = (
    "closed", "approved", "changes-requested", "submitted", "unknown",
    "open", "queued", "blocked",
)
_PIP_BLOCKED_STATES = ("queued", "blocked")


def _dashboard_pip_state(ticket: Mapping[str, object]) -> str:
    """The furthest state one ticket has reached, for its bar segment."""
    if ticket.get("state") != "OPEN":
        return "closed"
    pr = ticket.get("pr")
    if pr == "approved":
        return "approved"
    if pr == "changes requested":
        return "changes-requested"
    if pr in ("submitted", "merged"):
        return "submitted"
    if pr == "unknown":
        return "unknown"
    if ticket.get("blocked"):
        if ticket.get("blocked_by_siblings"):
            return "queued"
        return "blocked"
    return "open"


#: The bar is this many segments whatever the ticket count. Forty-four
#: segments in a 130px column render as one solid block (Nate, 2026-09-16),
#: so past this the bar becomes proportional rather than one-per-ticket.
PIP_SEGMENTS = 12


def _dashboard_work_order(
    tickets: Sequence[Mapping[str, object]]
) -> List[Mapping[str, object]]:
    """The tickets in bar order: finished work, then the order the rest
    will be worked.

    Blocked tickets line up behind whatever blocks them, so the bar shows
    who is blocking whom (Nate, 2026-09-24). A blocked ticket's depth is 0
    unless a sibling it waits on is itself blocked, then one more than the
    deepest such sibling. The run sorts by depth; at equal depth a ticket
    waiting on a sibling leads one blocked from outside, so it sits nearer
    the open work that frees it. Otherwise the tickets keep queue order.
    """
    states = [_dashboard_pip_state(ticket) for ticket in tickets]
    state_of = {
        ticket.get("ref"): state for ticket, state in zip(tickets, states)
    }
    waits_on = {
        ticket.get("ref"): [
            ref for ref in (ticket.get("sibling_blockers") or ())
            if state_of.get(ref) in _PIP_BLOCKED_STATES
        ]
        for ticket in tickets
    }
    depths: Dict[object, int] = {}

    def depth(ref: object, seen: Tuple[object, ...] = ()) -> int:
        if ref in depths:
            return depths[ref]
        found = 0
        for blocker in waits_on.get(ref, ()):
            if blocker in seen or blocker == ref:
                continue  # a cycle adds no depth
            found = max(found, 1 + depth(blocker, seen + (ref,)))
        depths[ref] = found
        return found

    def key(index: int):
        state = states[index]
        if state not in _PIP_BLOCKED_STATES:
            return (PIP_PROGRESS_ORDER.index(state), 0, 0, index)
        return (
            PIP_PROGRESS_ORDER.index(_PIP_BLOCKED_STATES[0]),
            depth(tickets[index].get("ref")),
            _PIP_BLOCKED_STATES.index(state),
            index,
        )

    return [tickets[i] for i in sorted(range(len(states)), key=key)]


def _dashboard_row_order(
    tickets: Sequence[Mapping[str, object]]
) -> List[Mapping[str, object]]:
    """The sub-issue rows: open tickets in their projected turn, then open
    tickets the projection never reaches (each blocked one behind its
    blocker), then closed tickets where they already were. The bar follows
    the same order (Nate, 2026-09-24: "I only need the unfinished rows to
    agree with the pips")."""
    open_rows = [t for t in tickets if t.get("state") == "OPEN"]
    turned = sorted(
        (t for t in open_rows if t.get("projected_turn") is not None),
        key=lambda t: t["projected_turn"],
    )
    unreached = [
        t for t in _dashboard_work_order(open_rows)
        if t.get("projected_turn") is None
    ]
    return turned + unreached + [
        t for t in tickets if t.get("state") != "OPEN"
    ]


def _dashboard_pip_order(
    tickets: Sequence[Mapping[str, object]]
) -> List[str]:
    """One state per ticket, in bar order: closed work first, then the open
    rows in row order."""
    rows = _dashboard_row_order(tickets)
    closed = [t for t in rows if t.get("state") != "OPEN"]
    rest = [t for t in rows if t.get("state") == "OPEN"]
    return [_dashboard_pip_state(t) for t in closed + rest]


def _dashboard_pips(
    tickets: Sequence[Mapping[str, object]]
) -> List[str]:
    """Bar segments in progress order, proportional once the count is large.

    Up to ``PIP_SEGMENTS`` tickets get one segment each. Beyond that the
    states are scaled to that many segments by largest remainder, and any
    state with at least one ticket keeps at least one segment: three submitted
    PRs among forty-four tickets must still be visible, and they sit at the
    end of the coloured run where the work actually is. A scaled bar cannot
    keep each blocked ticket behind its blocker, so its two blocked states
    appear in the order they first do in the full bar.
    """
    states = _dashboard_pip_order(tickets)
    if len(states) <= PIP_SEGMENTS:
        return states

    counts = {state: states.count(state) for state in PIP_PROGRESS_ORDER}
    present = list(dict.fromkeys(states))
    total = len(states)
    exact = {state: counts[state] * PIP_SEGMENTS / total for state in present}
    share = {state: max(1, int(exact[state])) for state in present}

    # Largest remainder, then trim from the largest share, so the segments
    # always sum to PIP_SEGMENTS without dropping a state to zero.
    while sum(share.values()) < PIP_SEGMENTS:
        state = max(present, key=lambda s: (exact[s] - share[s], counts[s]))
        share[state] += 1
    while sum(share.values()) > PIP_SEGMENTS:
        state = max(present, key=lambda s: (share[s] - exact[s], share[s]))
        if share[state] <= 1:
            break
        share[state] -= 1

    bar: List[str] = []
    for state in present:
        bar.extend([state] * share.get(state, 0))
    return bar


def _dashboard_next_paused(
    tickets: Sequence[Mapping[str, object]], next_step_blocked: bool,
) -> Optional[str]:
    """The earliest hold's end when a backoff hold is all that stops the
    project's next step, else None."""
    if next_step_blocked or _dashboard_next_owner(tickets):
        return None
    holds = sorted(
        str(ticket["paused_until"]) for ticket in tickets
        if ticket.get("state") == "OPEN" and ticket.get("paused_until")
    )
    return holds[0] if holds else None


def _dashboard_next_owner(
    tickets: Sequence[Mapping[str, object]]
) -> Optional[str]:
    """Owner of the first open ticket in queue order, or None."""
    for ticket in tickets:
        if ticket.get("state") == "OPEN" and ticket.get("owner"):
            return str(ticket["owner"])
    return None


def dashboard_board(
    items: Iterable[Item],
    now: datetime,
    pr_facts: Optional[Mapping[str, Optional[Mapping[str, object]]]] = None,
    pr_facts_known: Optional[bool] = None,
    authoring_pr_agents: Optional[Mapping[str, Iterable[str]]] = None,
    backed_off: Optional[Mapping[str, Mapping[str, object]]] = None,
) -> Dict[str, List[Dict[str, object]]]:
    """Build the ordered parent-project board for one already-loaded brief.

    The board is what comes next (Nate, 2026-09-24): ``projected_pull_order``
    runs ``startable()`` forward, and projects and their open tickets follow
    their projected turns. Closed tickets sink to the bottom of their
    project. Work the projection never reaches follows in gate order, with
    anything that cannot move below what can. `Done` is newest-first. The
    order is `startable()`'s throughout — this function never invents a rank.

    ``backed_off`` is ``backoff_withheld``'s mapping, read from the local
    heartbeat by the caller. With the tickets finished by comments, these are
    the holds the engineers honour that the Project fields do not show, so
    the rows name them rather than a next step nobody will take.
    """
    rows = list(items)
    paused_rows = dict(backed_off or {})
    finished: Set[str] = set()
    by_ref = {item.ref: item for item in rows}
    done_cutoff = now - DASHBOARD_DONE_WINDOW
    max_time = datetime.max.replace(tzinfo=timezone.utc)

    facts = dict(pr_facts or {})
    authoring = authoring_pr_agents or {}
    # The brief returns an empty mapping both when nothing has a PR and when
    # the scan failed, so it says which through ``pr_facts_known``. A caller
    # that says nothing is not claiming a failure: fixture-pure callers pass no
    # facts at all and expect the old "no PR recorded" reading.
    known = True if pr_facts_known is None else bool(pr_facts_known)
    in_review: Set[str] = set()
    for ref, fact in facts.items():
        if (
            not isinstance(fact, Mapping)
            or str(fact.get("state") or "").upper() != "OPEN"
        ):
            continue
        verdict = fact.get("verdict")
        if rejected_at_current_head(
            verdict if isinstance(verdict, dict) else None,
            fact.get("headRefOid"),
        ):
            # This is implementation work again, matching ``begin``'s
            # rejected-current-head predicate; a pushed head falls through
            # to the reviewer-owned in-review set.
            continue
        in_review.add(ref)
    try:
        # Match what `cmd_next` withholds, so the rank shown is the rank the
        # engineers actually use: work already in review, and work finished by
        # comments and waiting on Nate to close.
        finished = set(finished_by_comments(rows))
        withheld = set(in_review) | finished
        queue = startable(rows, awaiting_review=withheld)
    except Exception:
        # The board is instrumentation: an ordering failure must not cost the
        # snapshot. Fall back to no ranks rather than no board.
        queue = []
    queue_rank = {item.ref: index for index, item in enumerate(queue)}
    try:
        downstream = dependency_descendants(rows)
        ranked_as = queue_classes(rows, downstream)
    except Exception:
        downstream, ranked_as = {}, {}
    waiting_on: Dict[str, List[str]] = {}
    for row in rows:
        if row.state == "OPEN":
            for blocker in row.open_blockers:
                waiting_on.setdefault(blocker, []).append(row.ref)

    def by_number(refs: Iterable[str]) -> List[str]:
        return sorted(refs, key=lambda ref: (
            ref.rsplit("#", 1)[0], int(ref.rsplit("#", 1)[1]),
        ))
    try:
        # The board's order for work in motion: each ticket's projected turn.
        turn = {
            ref: index
            for index, ref in enumerate(
                projected_pull_order(rows, now, paused=paused_rows)
            )
        }
    except Exception:
        turn = {}

    children: Dict[str, List[Item]] = {}
    for row in rows:
        if row.parent:
            children.setdefault(row.parent, []).append(row)

    verdicts: Dict[str, Optional[Dict]] = {}

    def verdict_for(ticket: Item) -> Optional[Dict]:
        """Return the batch verdict, with a fixture-only legacy fallback."""
        fact = facts.get(ticket.ref) or {}
        if str(fact.get("state") or "").upper() != "OPEN":
            return None
        if "verdict" in fact:
            found = fact.get("verdict")
            return found if isinstance(found, dict) else None
        if ticket.ref in verdicts:
            return verdicts[ticket.ref]
        number = fact.get("number")
        found = latest_verdict(ticket.repo, number) if number else None
        verdicts[ticket.ref] = found
        return found

    def authoring_for(fact: Optional[Mapping[str, object]]) -> Iterable[str]:
        """Return durable authoring agents for one PR number, if known."""
        number = (fact or {}).get("number")
        if number is None:
            return ()
        found = authoring.get(str(number))
        return found if found is not None else ()

    def ticket_blocked(child: Item) -> bool:
        """The block ``_dashboard_ticket`` honours, read from the Item."""
        parent = by_ref.get(child.parent or "")
        return bool(
            child.is_blocked or child.open_blockers
            or (parent is not None and parent.is_blocked)
        )

    def ticket_key(child: Item):
        """Open tickets in their projected turn, then open tickets the
        projection never reaches, then closed. ``next_owner`` reads this
        order; ``_dashboard_row_order`` lays out the unreached ones."""
        if child.state != "OPEN":
            return (2, 0, child.number)
        if child.ref in turn:
            return (0, turn[child.ref], child.number)
        return (1, 0, child.number)

    def ticket_rows(parent: Item) -> List[Dict[str, object]]:
        # `startable()` withholds every ticket of a blocked parent, so the
        # rows say so too, with the parent's reason.
        parent_block: Optional[str] = None
        if parent.is_blocked:
            refs = ", ".join(
                "#" + str(ref).rsplit("#", 1)[-1]
                for ref in parent.block_references
            )
            parent_block = (
                "by " + refs if refs
                else (_dashboard_block_reason(parent) or "")
            )
        siblings_of = list(children.get(parent.ref, ()))
        siblings = {child.ref for child in siblings_of}
        return [
            _dashboard_ticket(
                child,
                facts.get(child.ref),
                verdict_for(child),
                queue_rank.get(child.ref),
                # Native edges when there are any, else the refs parsed from
                # the block comment, so "blocked" always says by what.
                list(child.open_blockers or child.block_references),
                known,
                parent_block if child.state == "OPEN" else None,
                authoring_for(facts.get(child.ref)),
                siblings,
                turn.get(child.ref) if child.state == "OPEN" else None,
                ranked_as.get(
                    child.ref, effective_class(child, by_ref)
                ),
                by_number(waiting_on.get(child.ref, ())),
                by_number(
                    ref for ref in downstream.get(child.ref, ())
                    if ref not in waiting_on.get(child.ref, ())
                    and by_ref.get(ref) is not None
                    and by_ref[ref].state == "OPEN"
                ),
                paused_rows.get(child.ref) if child.state == "OPEN" else None,
                child.ref in finished and child.state == "OPEN",
            )
            for child in sorted(siblings_of, key=ticket_key)
        ]

    def best_rank(item: Item) -> Optional[int]:
        ranks = [
            queue_rank[child.ref]
            for child in children.get(item.ref, ())
            if child.ref in queue_rank
        ]
        return min(ranks) if ranks else None

    def stage_since(item: Item) -> Optional[datetime]:
        return _dashboard_stage_since(item)

    def board_key(item: Item):
        """Pinned first, then the ladder, then queue order, then gate age.

        A pin is Nate's explicit ordering call and leads whatever else is
        true (#902). Then the ladder, whoever owns the next step: a `Broken`
        project Muse is reviewing reads above `Improve` work Codex can start,
        because reviewing and implementing run side by side and the class is
        the priority (Nate, 2026-09-24, choosing this over the engineers'
        queue leading the board). Within a class, projects with startable
        tickets follow the engineers' own queue order, and the rest follow
        by time at gate. This is the gate order; ``column_order`` puts
        projected turns ahead of it.
        """
        rank = best_rank(item)
        since = stage_since(item)
        return (
            0 if item.pinned else 1,
            ladder_index(effective_class(item, by_ref)),
            rank is None,
            rank if rank is not None else 0,
            since is None,
            since or max_time,
            item.repo,
            item.number,
        )

    def done_key(item: Item):
        """Done reads newest-first: the last thing finished is the useful one."""
        closed = item.closed_at or stage_since(item)
        return (closed is None, -(closed.timestamp() if closed else 0),
                item.repo, item.number)

    def next_step_blocked(
        item: Item, tickets: Sequence[Mapping[str, object]]
    ) -> bool:
        """True when nothing on an open project can move until a block
        lifts. Finished work is never "blocked", whatever label it kept."""
        if item.state != "OPEN":
            return False
        if item.is_blocked:
            return True
        open_rows = [t for t in tickets if t.get("state") == "OPEN"]
        return bool(open_rows) and all(t.get("blocked") for t in open_rows)

    def column_order(
        stage_items: List[Item],
        tickets_by_ref: Mapping[str, Sequence[Mapping[str, object]]],
    ) -> List[Item]:
        """What comes next, top to bottom (Nate, 2026-09-24: "an accurate
        representation of what comes next, not a string of contrived
        rules").

        A pin leads whatever else is true (#902). A project with a projected
        turn sits at its first one, so a project freed by other work lands
        wherever the engineers' queue would take it. Everything the
        projection never reaches follows in gate order (``board_key``), with
        a project that cannot move until a block lifts below those that can.
        """
        def key(item: Item):
            turns = [
                turn[child.ref] for child in children.get(item.ref, ())
                if child.ref in turn
            ]
            return (
                not item.pinned,
                not turns,
                min(turns) if turns else 0,
                next_step_blocked(item, tickets_by_ref.get(item.ref, ())),
                board_key(item),
            )
        return sorted(stage_items, key=key)

    def include(item: Item, stage: str) -> bool:
        if item.parent is not None or item.status != stage:
            return False
        if stage != "Done":
            return True
        return (
            item.state == "CLOSED"
            and item.closed_at is not None
            and item.closed_at >= done_cutoff
        )

    columns: List[Dict[str, object]] = []
    for stage in DASHBOARD_BOARD_STAGES:
        stage_items = [item for item in rows if include(item, stage)]
        tickets_by_ref = {item.ref: ticket_rows(item) for item in stage_items}
        if stage == "Done":
            ordered = sorted(stage_items, key=done_key)
        else:
            try:
                ordered = column_order(stage_items, tickets_by_ref)
            except Exception:
                # Instrumentation again: a placement failure falls back to
                # the plain order rather than costing the snapshot.
                ordered = sorted(stage_items, key=board_key)
        columns.append({
            "stage": stage,
            "items": [
                _dashboard_item(
                    item, now, by_ref, tickets_by_ref[item.ref],
                    next_step_blocked(item, tickets_by_ref[item.ref]),
                )
                for item in ordered
            ],
        })
    return {"columns": columns}


def _dashboard_muse_usage(now_epoch: float) -> Optional[Dict[str, object]]:
    """Return the compact Muse spend row for the dashboard snapshot.

    Rolling seven-day dollars against the cap only; the 2026-09-18 decision
    declined a 24-hour companion line. Best effort like the rest of the
    snapshot: an unreadable reader yields None and the page hides the row,
    never a failed brief.
    """
    try:
        import usage
    except Exception:
        return None
    try:
        reading = usage.read_muse(now_epoch)
    except Exception:
        return None
    if not isinstance(reading, dict):
        return None
    windows = reading.get("windows")
    window = windows.get("seven_day") if isinstance(windows, dict) else None
    if not isinstance(window, dict):
        return None
    spent = reading.get("spent_dollars")
    cap = reading.get("cap_dollars")
    percent = window.get("used_percent")
    calls = window.get("calls")
    for value in (spent, cap, percent):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or value != value
            or value in (float("inf"), float("-inf"))
        ):
            return None
    if cap <= 0 or spent < 0:
        return None
    if (
        not isinstance(calls, int)
        or isinstance(calls, bool)
        or calls < 0
    ):
        return None
    row: Dict[str, object] = {
        "spent_dollars": float(spent),
        "cap_dollars": float(cap),
        "used_percent": float(percent),
        "calls": calls,
    }
    # #1199: the pace signal, when the reader carries one, so the run-out time
    # is on the page days ahead instead of discovered at the wall. Best effort
    # like the rest of the row: a missing or odd value is simply left out.
    try:
        verdict = usage.pace(
            reading, now_epoch, provider=usage.provider_of("muse"))
    except Exception:
        verdict = {}
    for weekly in (verdict.get("windows") or ()):
        if not isinstance(weekly, dict) or not weekly.get("band"):
            continue
        row["band"] = weekly["band"]
        for key in ("projected_percent", "daily_rate_dollars", "runs_out_at"):
            value = weekly.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                row[key] = float(value)
        break
    return row


def write_dashboard_snapshot(
    brief: Mapping[str, object],
    board: Mapping[str, object],
    generated_at: str,
    usage: Optional[Mapping[str, object]] = None,
) -> pathlib.Path:
    """Atomically append one display snapshot to the local dashboard spool."""
    spool_dir = _dashboard_spool_dir()
    payload = {
        "brief": brief,
        "board": board,
        "generated_at": generated_at,
        "usage": dict(usage) if isinstance(usage, Mapping) else None,
    }
    text = json.dumps(payload, indent=2) + "\n"
    name = "brief-{}-{}.json".format(
        time.time_ns(), secrets.token_hex(8)
    )
    target = spool_dir / name
    temporary = spool_dir / ("." + name + ".tmp")

    spool_dir.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, target)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    _prune_dashboard_spool(spool_dir, keep=target.name)
    return target


def _prune_dashboard_spool(spool_dir: pathlib.Path, keep: str) -> None:
    """Delete all but the newest ``DASHBOARD_SPOOL_KEEP`` spool entries.

    Best effort: the snapshot is already written, so a failure here is
    reported and never raised. Only ``brief-<time_ns>-<hex>.json`` names are
    touched, ordered by their write time, and ``keep`` always survives.
    """
    try:
        entries = []
        for name in os.listdir(spool_dir):
            match = _DASHBOARD_SPOOL_ENTRY.match(name)
            if match:
                entries.append((int(match.group(1)), name))
        entries.sort(reverse=True)
        for _, name in entries[DASHBOARD_SPOOL_KEEP:]:
            if name == keep:
                continue
            try:
                (spool_dir / name).unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        print("funnel: could not prune dashboard spool: {}".format(exc),
              file=sys.stderr)


def _snapshot_generated_at(value: object) -> Optional[float]:
    """Parse a spool ``generated_at`` to epoch seconds, or None.

    Mirrors the publisher's ordering so routines, /funnel, and the
    dashboard agree on which snapshot is newest. Naive timestamps read
    as UTC; anything else means the entry cannot be ordered, so the
    caller skips it instead of guessing.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _newest_snapshot_entry(
        spool_dir: pathlib.Path) -> Tuple[Optional[bytes], List[str]]:
    """Return the newest parseable spool entry's bytes, plus skip warnings.

    A missing spool directory means no brief has been published yet, which
    is normal on a fresh install: it reads as an empty spool, not an error.
    """
    try:
        names = sorted(os.listdir(spool_dir))
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        raise OSError(
            "cannot read snapshot spool {}: {}".format(spool_dir, exc))
    warnings: List[str] = []
    best: Optional[Tuple[float, str, bytes]] = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = spool_dir / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            warnings.append("skipping {}: cannot read it ({})".format(
                name, exc))
            continue
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            warnings.append(
                "skipping {}: not parseable JSON".format(name))
            continue
        epoch = _snapshot_generated_at(
            payload.get("generated_at") if isinstance(payload, dict)
            else None)
        if epoch is None:
            warnings.append(
                "skipping {}: no parseable generated_at".format(name))
            continue
        if best is None or (epoch, name) > (best[0], best[1]):
            best = (epoch, name, data)
    if best is None:
        return None, warnings
    return best[2], warnings


def cmd_snapshot() -> int:
    """Print the newest published snapshot without running a live brief.

    Runners and routines read this artifact; only the publisher runs
    ``brief``. The bytes are exactly what ``brief`` spooled, so the
    top-level ``generated_at`` is the snapshot's age.
    """
    spool_dir = _dashboard_spool_dir()
    try:
        data, warnings = _newest_snapshot_entry(spool_dir)
    except OSError as exc:
        print("funnel: {}".format(exc), file=sys.stderr)
        return 1
    for warning in warnings:
        print("funnel: snapshot warning: {}".format(warning),
              file=sys.stderr)
    if data is None:
        print(
            "funnel: no published snapshot yet in {} "
            "(no brief has been spooled)".format(spool_dir),
            file=sys.stderr,
        )
        return 1
    text = data.decode("utf-8")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


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


def parse_park_comment(body: str) -> Optional[Dict[str, object]]:
    """Read a durable park reason and its optional wake record.

    A dated park starts with one strict, machine-readable header line, followed
    by the same reason line used by an ordinary park. The provenance trailer is
    invisible to this contract and is removed before parsing.
    """
    if not isinstance(body, str):
        return None
    visible = _visible_comment(body)
    wake_date = None
    prior_status = None

    if visible.startswith(PARK_WAKE_PREFIX):
        header, separator, reason_line = visible.partition("\n")
        match = PARK_WAKE_RE.fullmatch(header)
        if match is not None:
            try:
                parsed_date = date.fromisoformat(match.group("wake_date"))
            except ValueError:
                parsed_date = None
            if parsed_date is not None:
                wake_date = parsed_date
                prior_status = match.group("prior_status")
        if not separator:
            return None
    else:
        reason_line = visible

    if not reason_line.startswith(PARK_COMMENT_PREFIX):
        return None
    return {
        "reason": reason_line[len(PARK_COMMENT_PREFIX):].strip(),
        "wake_date": wake_date,
        "prior_status": prior_status,
    }


def _parked_item_json(item: Item) -> Dict[str, object]:
    """Render one parked item and read its durable reason comment.

    Comments are deliberately fetched here, rather than in ``ITEM_QUERY``:
    parked items are uncommon and the normal Project load must not pay for a
    comment request for every issue.
    """
    comments = _issue_comments(item)
    parsed = None
    for comment in reversed(comments):
        body = comment.get("body") or ""
        parsed = parse_park_comment(body)
        if parsed is not None:
            break

    rendered = {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "parked_at": item.status_since.isoformat() if item.status_since else None,
        "reason": parsed["reason"] if parsed is not None else None,
    }
    if parsed is not None and parsed["wake_date"] is not None:
        rendered["wake_date"] = parsed["wake_date"].isoformat()
        rendered["wake_status"] = parsed["prior_status"]
    return rendered


def parked_json(items: Iterable[Item]) -> List[Dict[str, object]]:
    """The brief's parked section, with one comment lookup per parked item."""
    return [_parked_item_json(item) for item in parked_items(items)]


def pending_wakes_json(
        parked: Optional[Sequence[Dict[str, object]]]
) -> Optional[List[Dict[str, object]]]:
    """Derive the wake list from the parked rows already read for the brief.

    The wake date and destination Status come from the durable comment header
    parsed by ``_parked_item_json``. Reusing those rows avoids a second comment
    read, and an item disappears as soon as its Project Status leaves Parked.
    """
    if parked is None:
        return None

    wakes = []
    for item in parked:
        wake_date = item.get("wake_date")
        wake_status = item.get("wake_status")
        if not isinstance(wake_date, str) or not isinstance(wake_status, str):
            continue
        wakes.append({
            "ref": item["ref"],
            "title": item["title"],
            "url": item["url"],
            "wake_date": wake_date,
            "wake_status": wake_status,
        })
    return wakes


def closed_itself_items(items: Iterable[Item], now: datetime) -> List[Item]:
    """Closed projects recent enough to plausibly carry a funnel-close record."""
    cutoff = now - CLOSED_ITSELF_WINDOW
    return sorted(
        (
            item for item in items
            if item.parent is None
            and item.state == "CLOSED"
            and item.status == "Done"
            and item.closed_at is not None
            and item.closed_at >= cutoff
            and _could_carry_closed_itself_marker(item)
        ),
        key=lambda item: (
            -item.closed_at.timestamp(), item.repo, item.number
        ),
    )


def _can_close_itself(item: Item) -> bool:
    """Whether a finished project may close without Nate's acceptance.

    An analysis marker always keeps the human acceptance gate, including when
    the marker is malformed. Otherwise the existing class/origin rules apply.

    The upkeep classes are safe to close regardless of who raised them. An
    ``Improve`` project is safe only when its effective shape owner is the
    agents, using the same origin and authorised override reading as the
    unattended shaping predicate. Missing or malformed origin therefore
    resolves to Nate and fails closed.
    """
    body = item.body if isinstance(item.body, str) else ""
    if parse_analysis_marker(body) is not None:
        return False

    if item.klass in {"Investigate", "Broken", "Maintenance"}:
        return True
    if item.klass != "Improve":
        return False

    override = parse_origin_override(body)
    return effective_shape_owner(
        item.origin,
        override.get("target") if override is not None else None,
    ) == "agents"


def _could_carry_closed_itself_marker(
    item: Item, *, children_done: Optional[int] = None
) -> bool:
    """Whether completed children may carry the funnel-close marker."""
    completed = item.children_done if children_done is None else children_done
    return (
        _can_close_itself(item)
        and item.children_total > 0
        and completed == item.children_total
    )


def _closed_itself_item_json(
    item: Item, comments: Sequence[dict]
) -> Optional[Dict[str, object]]:
    """Render one funnel-close marker, or omit an ordinary accepted close."""
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
    items: Iterable[Item], now: datetime, brief_cache=None
) -> List[Dict[str, object]]:
    """The brief's recent funnel-close records, newest first."""
    candidates = closed_itself_items(items, now)
    if not candidates:
        return []
    cache = brief_cache or _ACTIVE_BRIEF_CACHE.get() or BriefCache()
    comments_by_ref = cache.closed_itself_comments(candidates)
    rows = []
    for item in candidates:
        row = _closed_itself_item_json(
            item, comments_by_ref.get(item.ref, [])
        )
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


def _blocked_item_json(item: Item, now: datetime) -> Dict[str, object]:
    """Render one blocked item from the parsed block-comment state."""
    rendered = {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "reason": item.block_reason,
        "conditions": item.block_references,
        "blocked_at": item.status_since.isoformat() if item.status_since else None,
    }
    blocked_until = _item_blocked_until(item)
    if blocked_until is not None:
        rendered["blocked_until"] = blocked_until.isoformat()
    if item.block_event is not None:
        rendered["event_condition"] = dict(item.block_event)
        after = parse_time(item.block_event.get("after"))
        if after is not None:
            elapsed = max(timedelta(0), now - after)
            rendered["event_wait"] = humanise(elapsed)
            rendered["event_wait_seconds"] = round(
                elapsed.total_seconds(), 3
            )
    if item.needs_decision is not None:
        rendered["needs_decision"] = item.needs_decision
    return rendered


def blocked_json(
    items: Iterable[Item], now: datetime,
) -> List[Dict[str, object]]:
    """The brief's blocked section, reusing one load-time comment fetch."""
    return [_blocked_item_json(item, now) for item in blocked_items(items)]


def _event_block_mismatch(item: Item) -> Optional[str]:
    """How a blocked ticket's event spec and Needs routing disagree, if at all.

    Only tickets are checked, and only when their block comments were read:
    an unread comment cannot show whether a spec is there.
    """
    if item.parent is None or item.block_comments_error:
        return None
    if item.block_event is not None:
        if item.needs == "external-event":
            return None
        return "well-formed event spec without Needs: external-event"
    if item.needs == "external-event":
        return "Needs: external-event without a well-formed event spec"
    return None


def event_block_inconsistencies_json(
    items: Iterable[Item],
) -> List[Dict[str, object]]:
    """List blocked tickets whose event spec and Needs routing disagree."""
    found: List[Dict[str, object]] = []
    for item in blocked_items(items):
        mismatch = _event_block_mismatch(item)
        if mismatch is None:
            continue
        row = {
            "ref": item.ref,
            "title": item.title,
            "url": item.url,
            "needs": item.needs,
            "mismatch": mismatch,
        }
        if item.block_event is not None:
            row["event_condition"] = dict(item.block_event)
        found.append(row)
    return found


# Approval may adopt an unset Class only from an explicit, whole-line
# proposal: no agent infers or writes a Class from plan prose on Nate's
# behalf. The value match below is exact, so a fuzzy line stays with him.
PROPOSED_CLASS_LINE_RE = re.compile(
    r"^[ \t]*Proposed[ \t]+class:[ \t]*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)


def proposed_class_for_approval(
    plan: object,
) -> Optional[Tuple[str, str]]:
    """Return one exact class proposal and its source line, or ``None``.

    Approval may fill an unset Project Class only from an explicit, whole-line
    proposal. A malformed proposal, a fuzzy value, or more than one proposal
    is ambiguous and therefore stays with Nate instead of becoming an
    inference from plan prose.
    """
    if not isinstance(plan, str):
        return None

    matches: List[Tuple[str, str]] = []
    for raw_line in plan.splitlines():
        match = PROPOSED_CLASS_LINE_RE.fullmatch(raw_line)
        if match is None:
            continue
        value = match.group("value").strip()
        if value not in LADDER:
            return None
        matches.append((value, raw_line.strip()))

    return matches[0] if len(matches) == 1 else None


def capture_origin(item: Item) -> str:
    """Return the recorded capture origin, or ``unknown`` when absent."""
    return item.origin or "unknown"


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


def blocked_step_reason(
    item: Item, by_ref: Mapping[str, Item]
) -> Optional[str]:
    """Return why a Needs child ticket is not currently actionable.

    This is deliberately a smaller readiness check than ``startable``. A
    Needs step is withheld from its work-owner section when it carries the
    ``blocked`` label, has an open native dependency, or belongs to a blocked
    parent. The parent lookup uses the Project rows already loaded for the
    brief; it never fetches issue state of its own.
    """
    reasons = []
    if item.is_blocked:
        reasons.append("ticket carries blocked marker")
    if item.open_blockers:
        reasons.append("open native blockers")
    parent = by_ref.get(item.parent or "")
    if parent is not None and parent.is_blocked:
        reasons.append("parent carries blocked marker")
    return "; ".join(reasons) if reasons else None


def _blocked_step_refs(item: Item, by_ref: Mapping[str, Item]) -> List[str]:
    """Return stable blocker references for one withheld Needs step."""
    refs: List[str] = []
    if item.is_blocked:
        refs.extend(item.block_references)
    refs.extend(item.open_blockers)
    parent = by_ref.get(item.parent or "")
    if parent is not None and parent.is_blocked:
        refs.append(parent.ref)

    # Keep the source order while removing duplicate facts from overlapping
    # native/comment observations.
    unique: List[str] = []
    for ref in refs:
        if ref not in unique:
            unique.append(ref)
    return unique


def _blocked_step_item_json(
    item: Item, by_ref: Mapping[str, Item]
) -> Dict[str, object]:
    """Render one withheld human or machine-local step for the brief."""
    rendered = _human_step_item_json(item)
    rendered["blocked_reason"] = blocked_step_reason(item, by_ref)
    rendered["blockers"] = _blocked_step_refs(item, by_ref)
    return rendered


def human_step_items(items: Iterable[Item]) -> List[Item]:
    """Open child issues that Nate must complete himself.

    Human-step tickets are work, not decisions. They are therefore rendered in
    their own brief section instead of being added to the decision queue.
    The Needs field is the only signal (#826); a ticket reads here exactly
    when its field is "human".
    """
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    return sorted(
        (
            item for item in rows
            if item.state == "OPEN"
            and item.parent is not None
            and item.needs == "human"
            and blocked_step_reason(item, by_ref) is None
        ),
        key=lambda item: (item.repo, item.number),
    )


def _human_step_item_json(
    item: Item, now: Optional[datetime] = None
) -> Dict[str, object]:
    row = {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "reason": item.needs,
    }
    # How long this action has been waiting on him, phrased exactly as the
    # decision rows are (Nate, 2026-09-16). A ticket with no creation time
    # recorded says so rather than guessing.
    since = item.created_at
    if now is not None:
        row["waited"] = (
            humanise(max(timedelta(0), now - since))
            if since is not None else "unknown"
        )
    return row


def human_step_json(
    items: Iterable[Item], now: Optional[datetime] = None
) -> List[Dict[str, object]]:
    """Render the open human-step work owed by Nate."""
    return [_human_step_item_json(item, now) for item in human_step_items(items)]


def blocked_human_step_items(items: Iterable[Item]) -> List[Item]:
    """Open human-step tickets withheld by a native or label block."""
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    return sorted(
        (
            item for item in rows
            if item.state == "OPEN"
            and item.parent is not None
            and item.needs == "human"
            and blocked_step_reason(item, by_ref) is not None
        ),
        key=lambda item: (item.repo, item.number),
    )


def blocked_human_step_json(
    items: Iterable[Item],
) -> List[Dict[str, object]]:
    """Render human-step work that remains visible but cannot start."""
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    return [
        _blocked_step_item_json(item, by_ref)
        for item in blocked_human_step_items(rows)
    ]


def machine_local_step_items(items: Iterable[Item]) -> List[Item]:
    """Open child issues whose work needs Claude Code's local environment."""
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    return sorted(
        (
            item for item in rows
            if item.state == "OPEN"
            and item.parent is not None
            and item.needs == "claude-code-environment"
            and blocked_step_reason(item, by_ref) is None
        ),
        key=lambda item: (item.repo, item.number),
    )


def machine_local_step_json(
    items: Iterable[Item],
) -> List[Dict[str, object]]:
    """Render open work waiting on a Claude Code session."""
    return [
        _human_step_item_json(item)
        for item in machine_local_step_items(items)
    ]


def blocked_machine_local_step_items(items: Iterable[Item]) -> List[Item]:
    """Open Claude-local tickets withheld by a native or label block."""
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    return sorted(
        (
            item for item in rows
            if item.state == "OPEN"
            and item.parent is not None
            and item.needs == "claude-code-environment"
            and blocked_step_reason(item, by_ref) is not None
        ),
        key=lambda item: (item.repo, item.number),
    )


def blocked_machine_local_step_json(
    items: Iterable[Item],
) -> List[Dict[str, object]]:
    """Render Claude-local work that remains visible but cannot start."""
    rows = list(items)
    by_ref = {item.ref: item for item in rows}
    return [
        _blocked_step_item_json(item, by_ref)
        for item in blocked_machine_local_step_items(rows)
    ]


def completed_projects_missing_human_steps(items: Iterable[Item]) -> List[Item]:
    """Completed projects whose plans mention access but have no Needs ticket.

    This is a detective signal, not proof that a human step was required. A
    parked project is deliberately excluded: parking is not a claim that its
    plan shipped. Any Needs ticket, including one already closed, clears
    the flag because the backstop asks whether the project ever carried one.
    """
    rows = list(items)
    human_step_parents = {
        item.parent
        for item in rows
        if item.parent is not None
        and item.needs in ("human", "claude-code-environment")
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
        if blocker is not None and (
            _never_closing(blocker)
            or _abandoned_ticket(blocker, item, by_ref)
        ):
            refs.add(ref)
    return sorted(refs)


def _abandoned_ticket(blocker: Item, dependent: Item,
                      by_ref: Dict[str, Item]) -> bool:
    """Whether ``blocker`` is an open ticket whose project will not finish it.

    A ticket under a parked or closed project is never offered to a lane, so
    nothing that waits on it can move: #227 and #229 waited on #165, a ticket
    of parked #15 (#1432). A sibling under the same parked project is not
    stranded by it: parking is a decision, and its tickets are meant to sit.
    """
    if blocker.state != "OPEN" or not blocker.parent:
        return False
    project = by_ref.get(blocker.parent)
    if project is None:
        return False
    if project.state == "OPEN" and project.status != "Parked":
        return False
    return dependent.parent != blocker.parent


def unclearable_block(item: Item) -> bool:
    """Whether a blocked item has no condition that can lift it and no asker.

    ``clear_satisfied_blocks`` lifts a parsed reference, date, or matching
    event record, and a native edge lifts itself. ``gate_question`` stays
    silent for Needs ``agent`` and a well-formed event spec. Needs
    ``external-event`` alone asks the existing unblock question. The funnel
    watch supports ``claude-code-environment``. A block outside all of those
    waits forever and is seen by no one: a Codex decline (Needs ``agent``, a
    ``**Declined:**`` comment) lands here (#1432).
    """
    if item.state != "OPEN" or not item.is_blocked:
        return False
    if item.block_references or item.open_blockers:
        return False
    if _item_blocked_until(item) is not None:
        return False
    if item.needs == "claude-code-environment":
        return False
    return gate_question(item) is None


def _unclearable_block_reason(item: Item) -> str:
    return (
        "blocked with no condition that can clear it, and no one is asked "
        "(Needs: {})".format(item.needs or "unset")
    )


def _satisfied_block_event(
    event: Mapping[str, str], heartbeat_records: Sequence[Dict[str, object]],
) -> Optional[str]:
    """Name the earliest matching GitHub finish after the threshold."""
    after = parse_time(event.get("after"))
    if after is None:
        return None

    matches: List[Tuple[float, str, str]] = []
    for record in heartbeat_records:
        if not isinstance(record, dict) or (
            record.get("phase") != "finish"
            or record.get("agent") != event.get("agent")
            or record.get("job") != event.get("job")
            or record.get("outcome") != event.get("outcome")
        ):
            continue
        stamp = record.get("ts")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        try:
            occurred = datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        if occurred <= after:
            continue
        run = record.get("run")
        if not isinstance(run, str) or not run.strip():
            continue
        at = occurred.strftime("%Y-%m-%dT%H:%M:%SZ")
        condition = (
            "heartbeat finish agent={} job={} outcome={} run={} at={}"
            .format(event["agent"], event["job"], event["outcome"],
                    run.strip(), at)
        )
        matches.append((occurred.timestamp(), run.strip(), condition))
    return min(matches)[2] if matches else None


def satisfied_block_refs(
    item: Item, by_ref: Dict[str, Item], now: Optional[datetime] = None,
    heartbeat_records: Sequence[Dict[str, object]] = (),
) -> Optional[List[str]]:
    """Return all parsed block conditions that are satisfied.

    A missing parsed comment, an empty reference list, an unresolvable
    reference, a missing blocker, an open blocker, a future date, or a blocker
    that is explicitly unable to close all fail closed with ``None``. The
    checks are deliberately separate so a caller can report which part of the
    conjunction failed without treating an empty list as vacuously satisfied.
    Event conditions use only the GitHub heartbeat finishes supplied by the
    caller; an absent or non-matching record fails closed.

    This mirrors ``_dead_dependency_refs`` over the already-loaded native and
    comment dependency facts. It never fetches a blocker: a reference must be
    present in ``by_ref`` before it can satisfy a block. A date condition is
    represented as ``until YYYY-MM-DD`` in the returned condition list.
    """
    # ``block_reason`` is populated only when ``parse_block_comment`` found a
    # matching header. An empty reason is still a parsed comment; ``None`` is
    # the unparsed state and must not be treated as satisfied.
    if item.block_reason is None:
        return None

    blocked_until = _item_blocked_until(item)
    if item.blocked_until is not None and blocked_until is None:
        return None

    conditions: List[str] = []
    if item.block_event is not None:
        event_condition = _satisfied_block_event(
            item.block_event, heartbeat_records
        )
        if event_condition is None:
            return None
        conditions.append(event_condition)

    if blocked_until is not None:
        if blocked_until > _block_condition_date(now):
            return None
        date_condition = _block_date_condition(item)
        if date_condition is not None:
            conditions.append(date_condition)

    values = list(item.block_references)
    if not values:
        return conditions or None

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

    conditions.extend(sorted(satisfied))
    return conditions or None


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
        + "all machine-readable conditions were satisfied: {}.\n\n"
          "Found satisfied at `{}`.\n\n```json\n{}\n```".format(
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

    # Event satisfaction is derived from the durable heartbeat branch on this
    # queue read. A local write-ahead spool is not enough to clear a remote
    # block.
    event_agents = sorted({
        item.block_event["agent"]
        for item in candidates
        if isinstance(item.block_event, dict)
        and isinstance(item.block_event.get("agent"), str)
    })
    heartbeat_records: Dict[str, List[Dict[str, object]]] = {}
    if event_agents:
        import heartbeat

        for event_agent in event_agents:
            if event_agent not in heartbeat.PROVIDERS:
                heartbeat_records[event_agent] = []
                continue
            try:
                heartbeat_records[event_agent] = heartbeat.read_github(
                    event_agent
                )
            except heartbeat.HeartbeatError as exc:
                raise GitHubError(
                    "could not read GitHub heartbeat records for {}: {}".format(
                        event_agent, exc
                    )
                )

    for item in candidates:
        event_records = (
            heartbeat_records.get(item.block_event.get("agent"), [])
            if isinstance(item.block_event, dict) else []
        )
        conditions = satisfied_block_refs(
            item, by_ref, now=now, heartbeat_records=event_records
        )
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

        if item.needs == "external-event":
            if not item.item_id:
                raise GitHubError("{} is not in the Project".format(item.ref))
            write_project_select(item.item_id, "Needs", "none", item.ref)
            item.needs = "none"

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
    ``Building`` project, an auto-closeable ``Building`` project whose upkeep
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

        # An open ticket under a closed project has nowhere to go: no gate is
        # watching it, the ladder ranks it through a parent that is finished,
        # and nothing will close it. Parked parents are excluded on purpose —
        # parking is a decision, and its tickets are meant to sit. Detection
        # only; nothing here closes anything.
        parent = by_ref.get(item.parent or "")
        if (
            parent is not None
            and parent.state != "OPEN"
            and parent.status != "Parked"
        ):
            reasons.append(
                "parent {} is closed with Status {}".format(
                    parent.ref, parent.status or "unset"
                )
            )

        if _auto_closeable_project(item):
            reasons.append("finished upkeep project not closed")

        dead = _dead_dependency_refs(item, by_ref)
        if dead:
            reasons.append(
                "blocked on blocker that will never close: {}".format(
                    ", ".join(dead))
            )

        if unclearable_block(item):
            reasons.append(_unclearable_block_reason(item))

        reasons.extend(cycle_reasons.get(item.ref, []))

        if reasons:
            found.append({
                "ref": item.ref,
                "title": item.title,
                "url": item.url,
                "reason": "; ".join(reasons),
            })
    return found


#: The two long-running watch logs. They live in the repo as issues so the
#: check-ins have somewhere to write, and they are deliberately not funnel
#: work: adding them to the Project would put a running commentary in the
#: queue. Excluded by number because that is what they are — two specific
#: issues, not a category.
WATCH_LOG_ISSUES = {
    "nateprich-projects/command-center": (579, 684),
}

#: #794's sub-issues are tracked through their parent rather than as Project
#: items of their own. Excluded by parent, so the exclusion follows the
#: breakdown rather than needing a list of numbers kept in step.
ORPHAN_SCAN_EXEMPT_PARENTS = (794,)


def _orphan_scan_excluded(repo: str, issue: Mapping[str, object]) -> bool:
    """Whether one open member issue is a known non-Project issue."""
    number = issue.get("number")
    if number in WATCH_LOG_ISSUES.get(repo, ()):
        return True
    parent = issue.get("parent")
    parent_number = (
        parent.get("number") if isinstance(parent, Mapping) else None
    )
    return parent_number in ORPHAN_SCAN_EXEMPT_PARENTS


def member_issues_without_project_items(
    items: Sequence[Item], repos: Optional[Sequence[str]] = None
) -> Dict[str, object]:
    """Open member-repo issues that are in no Project item, or why not read.

    Membership comes from the `command-center` topic, never a hardcoded list,
    so a repo that joins the funnel is scanned the run after it opts in.

    Detection only. Nothing is added at `Ideas`: an issue outside the Project
    may be deliberate, and the two watch logs are exactly that. Known
    non-Project issues are excluded — the watch logs by number, #794's
    sub-issues by parent — and everything else is listed for a person to
    judge. `nateprich-projects/jeffy-finance-agent#53` is expected to appear
    and is an honest exception rather than a defect; it is not excluded in
    code, because an exclusion is a claim that something can never be wrong.

    A scan that fails says so. `status` is `read` or `degraded`, never an
    empty list standing in for an unread one: zero orphans and an unread scan
    are the same shape and opposite news.
    """
    known = {item.ref for item in items}
    names = list(repos) if repos is not None else None
    if names is None:
        try:
            names = member_repos()
        except (GitHubError, OSError, subprocess.SubprocessError) as exc:
            return {
                "status": "degraded",
                "reason": "could not read member repositories: {}".format(exc),
                "issues": [],
            }

    found: List[Dict[str, object]] = []
    unread: List[str] = []
    for repo in names:
        payload = _gh_json(
            "gh", "issue", "list", "--repo", repo, "--state", "open",
            "--limit", "200", "--json", "number,title,url,parent",
        )
        if not isinstance(payload, list):
            unread.append(repo)
            continue
        for issue in payload:
            if not isinstance(issue, Mapping):
                continue
            ref = "{}#{}".format(repo, issue.get("number"))
            if ref in known or _orphan_scan_excluded(repo, issue):
                continue
            found.append({
                "ref": ref,
                "repo": repo,
                "title": issue.get("title"),
                "url": issue.get("url"),
            })

    found.sort(key=lambda row: str(row["ref"]))
    if unread:
        return {
            "status": "degraded",
            "reason": "could not list open issues for {}".format(
                ", ".join(sorted(unread))
            ),
            "issues": found,
        }
    return {"status": "read", "issues": found}
def status_state_mismatches(items: Iterable[Item]) -> List[Dict[str, object]]:
    """Items whose Project Status and GitHub state contradict each other.

    Two directions, both derived from the one ``load_items()`` pass with no
    extra read, no cache and nothing stored:

    - **closed but not finished** — a closed issue at any Status other than
      ``Done`` or ``Parked``. #1206 was one of these: a CLOSED project written
      to ``Ready``, which put it in the startable queue and the self-approval
      path at once with nothing able to close it again.
    - **open but Done** — an issue recorded as finished that is still open.
      The lane filters all read OPEN, so it keeps being treated as live work
      while every count says it is finished.

    Deliberately its own section rather than a widening of the lane filters:
    those stay on OPEN so a closed-at-Ready item appears here and nowhere
    else, instead of turning up as breakdown work (#1209).
    """
    found: List[Dict[str, object]] = []
    for item in items:
        if item.status is None:
            continue
        if item.state != "OPEN" and item.status not in TERMINAL_STATUSES:
            mismatch = "closed at Status {}, which is not {}".format(
                item.status, " or ".join(TERMINAL_STATUSES)
            )
        elif item.state == "OPEN" and item.status == "Done":
            mismatch = "open at Status Done"
        else:
            continue
        found.append({
            "ref": item.ref,
            "title": item.title,
            "url": item.url,
            "state": item.state,
            "status": item.status,
            "mismatch": mismatch,
        })
    return sorted(found, key=lambda row: str(row["ref"]))


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
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]] = None,
) -> int:
    """Everything, ordered — both queues, each under its own heading.

    They are genuinely different orderings over different subsets, so a single
    merged list would have to pick one and misrepresent the other.

    Work already sitting in an open PR is not startable, and this listing used
    to show it anyway: ``startable`` takes the exclusion as an argument, and
    the queue was the one caller that never passed it. The read follows
    ``cmd_next_review``'s pattern — supplied by the caller, else fetched once
    and retained in an active brief cache.

    A PR read that fails says so. An unfiltered list is the wrong fallback
    here: it is indistinguishable from a correct one, and the whole defect is
    a ticket that looks startable and is not.
    """
    pr_facts_unavailable: Optional[str] = None
    if pr_facts is None:
        try:
            pr_facts = ticket_pr_facts(items)
        except (GitHubError, BriefSectionTimeout, OSError,
                subprocess.SubprocessError) as exc:
            pr_facts_unavailable = str(exc)
            pr_facts = None
        else:
            cache = _ACTIVE_BRIEF_CACHE.get()
            if cache is not None:
                cache._pr_facts = pr_facts

    in_review: Set[str] = set()
    if pr_facts_unavailable is None:
        try:
            in_review = _call_with_optional_keyword(
                awaiting_review, "pr_facts", pr_facts, items
            )
        except (GitHubError, BriefSectionTimeout, OSError,
                subprocess.SubprocessError) as exc:
            pr_facts_unavailable = str(exc)

    decisions = awaiting_decision(items)
    tickets = startable(
        items, awaiting_review=in_review, repo_readiness=repo_readiness
    )

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

    if pr_facts_unavailable is not None:
        # The brief's degraded convention, in the queue's voice: name the
        # section, say it could not be read, and say what that costs.
        print("\ndegraded — open-PR facts: {}. Tickets already in an open PR "
              "cannot be excluded, so the list below may name work that is "
              "already done.".format(pr_facts_unavailable))

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

    frozen = freeze_withheld(items, repo_readiness=repo_readiness)
    if frozen:
        print("\nWithheld by frozen ground ({}):".format(len(frozen)))
        for row in frozen:
            print("  {:<34} {}".format(row["ref"], row["reason"]))

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


def _backoff_rows() -> List[Dict[str, object]]:
    """Heartbeat records for the backoff read, or none if they cannot be had.

    A selector must not stop because its scheduling history is unreadable:
    with no rows, ``consecutive_failures`` finds nothing and every ticket is
    offered exactly as it was before this existed.
    """
    rows: List[Dict[str, object]] = []
    try:
        import heartbeat

        for agent in sorted(heartbeat.PROVIDERS):
            try:
                rows.extend(_brief_heartbeat_rows(agent))
            except Exception:
                continue
    except Exception:
        return []
    return rows


def _backed_off_work(
    items: Sequence[Item], now: datetime
) -> Dict[str, Dict[str, object]]:
    """Refs withheld by repeated failure, read from the heartbeat."""
    return backoff_withheld(_backoff_rows(), now)


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
    shared_review_read = _accepts_keyword(awaiting_review, "pr_facts")
    if pr_facts is None and shared_review_read:
        pr_facts = ticket_pr_facts(items)
    blocked = (
        _call_with_optional_keyword(
            awaiting_review, "pr_facts", pr_facts, items
        )
        if shared_review_read
        else awaiting_review(items)
    )
    # An approved current head that GitHub now reports as conflicting is no
    # longer review work: the reviewer already judged it, and the engineer must
    # rebase it. Every other open PR remains withheld, including UNKNOWN and
    # approvals for an older head.
    blocked.difference_update(approved_conflicting_refs(pr_facts))
    blocked.update(finished_by_comments(items))
    backoff_rows = _backoff_rows()
    backed_off = backoff_withheld(backoff_rows, now)
    ticket = next_ticket_for_tier(
        items, now, tier=tier, blocked=blocked, excluded=excluded,
        agent=agent,
        repo_readiness=repo_readiness,
        pr_facts=pr_facts,
        backed_off=backed_off,
    )

    if ticket is None:
        holder = lock_holder(items, now, pr_facts=pr_facts)
        withheld = readiness_blockers(
            items, repo_readiness=repo_readiness, awaiting_review=blocked,
            agent=agent,
        )
        frozen = freeze_withheld(
            items, repo_readiness=repo_readiness, awaiting_review=blocked,
            agent=agent,
        )
        for row in backoff_withheld_rows(items, backoff_rows, now):
            print("withheld — {}: {}".format(row["ref"], row["reason"]),
                  file=sys.stderr)
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
        elif frozen:
            print("nothing — {}".format(
                _freeze_withheld_summary(frozen)
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
        self._comment_tails: Dict[str, List[Dict]] = {}

    def clear(self) -> None:
        """Forget auxiliary reads after a command may have mutated GitHub."""
        self._pr_facts = _BRIEF_UNAVAILABLE
        self._heartbeat_rows.clear()
        self._comment_tails.clear()

    def get_pr_facts(self, items: Sequence[Item]):
        if self._pr_facts is _BRIEF_UNAVAILABLE:
            self._pr_facts = ticket_pr_facts(items)
        return self._pr_facts

    def comment_tails(
        self, items: Sequence[Item]
    ) -> Dict[str, List[Dict]]:
        """Read and retain bounded marker tails for one run only."""
        missing = [
            item for item in items
            if item.ref not in self._comment_tails
        ]
        if missing:
            self._comment_tails.update(
                _batched_issue_comments(missing)
            )
        return {
            item.ref: self._comment_tails[item.ref]
            for item in items
            if item.ref in self._comment_tails
        }

    def closed_itself_comments(
        self, items: Sequence[Item]
    ) -> Dict[str, List[Dict]]:
        """Read the bounded comment tails used by the closed-itself section."""
        return self.comment_tails(items)

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


def _read_outcome_signals(now: datetime) -> Dict[str, object]:
    """Read named outcome signals without turning an unavailable read into zeros."""
    try:
        import outcomes

        return outcomes.signal_summary(outcomes.read_records(), now=now)
    except Exception as exc:
        # Outcome history is diagnostic input. Keep the brief usable when its
        # separate heartbeat-branch read is unavailable, while making the
        # uncertainty explicit instead of presenting an empty result as truth.
        return {
            "schema_version": 1,
            "source": "outcomes",
            "derived_at": now.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "status": "unavailable",
            "reason": "could not read durable outcome records: {}".format(
                _brief_error(exc)
            ),
            "signals": {},
        }


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
    timings: Dict[str, object],
    degraded: Optional[List[Dict[str, object]]] = None,
    *,
    deadline: Optional[float] = None,
    budget: Optional[float] = None,
) -> object:
    """Run one brief section, enforce its budget, and record its timing.

    An over-budget section degrades into an explicit record and yields
    ``_BRIEF_UNAVAILABLE``; no section fails the whole brief.
    """
    degraded = degraded if degraded is not None else []
    budget = float(
        BRIEF_SECTION_BUDGETS.get(section, 1.0)
        if budget is None else budget
    )
    started = time.perf_counter()
    allowed = max(0.0, budget)
    if deadline is not None:
        allowed = min(allowed, max(0.0, deadline - started))
    limit = started + allowed

    def stop(reason: str, elapsed: float) -> object:
        timings[section] = round(max(0.0, elapsed), 6)
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
        degraded.append(_brief_degraded_record(
            section, elapsed, budget, reason
        ))
    return value


def cmd_brief(
    items: List[Item],
    now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
    missing: Optional[List[Dict[str, str]]] = None,
    timings: Optional[Dict[str, object]] = None,
    degraded: Optional[List[Dict[str, object]]] = None,
    deadline: Optional[float] = None,
    brief_cache: Optional[BriefCache] = None,
    outcome_signals: Optional[Dict[str, object]] = None,
    portfolio_metrics: Optional[Dict[str, object]] = None,
    decline_routing: Optional[Dict[str, object]] = None,
    main_ci: Optional[List[Dict[str, object]]] = None,
    orphan_issues: Optional[Dict[str, object]] = None,
) -> int:
    missing = list(missing or [])
    timings = {} if timings is None else timings
    degraded = list(degraded or [])
    if deadline is None:
        deadline = time.perf_counter() + BRIEF_TOTAL_BUDGET_SECONDS
    cache = brief_cache or _ACTIVE_BRIEF_CACHE.get() or BriefCache()
    cache_token = _ACTIVE_BRIEF_CACHE.set(cache)

    def section(
        name: str,
        reader: Callable[[], object],
        default,
    ):
        value = _brief_timed(
            name,
            lambda: _brief_read(name, reader, missing),
            timings,
            degraded,
            deadline=deadline,
        )
        return default if value is _BRIEF_UNAVAILABLE else value

    def named_section(name: str, reader: Callable[[], object]):
        """A section whose unread state must not look like an empty result.

        `parked` and `cleared_blocks` both read as *news* when empty — nothing
        is parked, nothing was unblocked — so degrading them to `[]` reports
        the opposite of what happened. These return null and name themselves
        in `missing`, the same shape the shared PR-facts read uses.
        """
        value = _brief_timed(
            name,
            lambda: _brief_read(name, reader, missing),
            timings,
            degraded,
            deadline=deadline,
        )
        if value is _BRIEF_UNAVAILABLE:
            if not any(entry.get("section") == name for entry in missing):
                missing.append({
                    "section": name,
                    "error": "could not read {} within its budget; this is "
                             "an unread section, not an empty one".format(name),
                })
            return None
        return value

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
        parked = named_section("parked", lambda: parked_json(items))
        pending_wakes = pending_wakes_json(parked)
        closed_itself = section(
            "closed_itself",
            lambda: closed_itself_json(items, now, brief_cache=cache),
            [],
        )
        cleared_blocks = named_section(
            "cleared_blocks", lambda: cleared_blocks_json(items, now)
        )
        blocked = section("blocked", lambda: blocked_json(items, now), [])
        event_block_inconsistencies = section(
            "event_block_inconsistencies",
            lambda: event_block_inconsistencies_json(items),
            [],
        )
        human = section("human_steps", lambda: human_step_json(items, now), [])
        machine_local = section(
            "machine_local_steps",
            lambda: machine_local_step_json(items),
            [],
        )
        blocked_human = section(
            "blocked_human_steps",
            lambda: blocked_human_step_json(items),
            [],
        )
        blocked_machine_local = section(
            "blocked_machine_local_steps",
            lambda: blocked_machine_local_step_json(items),
            [],
        )
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
        disposal_report = section(
            "disposal", lambda: disposal(items, now), {}
        )
        resend = section("resend_ratio", lambda: recent_resend_ratio(now), {})
        merges = section(
            "unattended_merges", lambda: unattended_merges(now), []
        )
        approvals = section(
            "unattended_approvals",
            lambda: unattended_approvals(items, now, brief_cache=cache),
            [],
        )
        run_summary = section(
            "run_summary", lambda: agent_run_summary(now), []
        )
        health = section("agent_health", lambda: agent_health(now), [])
        touched = section(
            "working_tree_touched", lambda: working_tree_touched(now), []
        )
        rejected = section(
            "rejected_merges", lambda: rejected_merges(items, now), {}
        )
        status_mismatches = section(
            "status_state_mismatches",
            lambda: status_state_mismatches(items),
            [],
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

        api = api_cost()
        # The rendered brief already exposes the documented timings map. Keep
        # API counters there so the hourly metrics can read them without
        # adding an undocumented top-level brief field.
        for name in ("graphql_points", "gh_calls"):
            value = api.get(name)
            if isinstance(value, int) and not isinstance(value, bool):
                timings["api_cost." + name] = value
        for caller, values in graphql_caller_spend().items():
            for name in ("calls", "points", "remaining"):
                timings[
                    "api_cost.graphql_by_caller.{}.{}".format(caller, name)
                ] = values.get(name)

        assembly_started = time.perf_counter()
        brief = {
            "generated_at": now.isoformat(),
            "total_needing_nate": len(decisions),
            "counts_by_gate": counts,
            "items": decision_rows,
            "parked": parked,
            "pending_wakes": pending_wakes,
            "closed_itself": closed_itself,
            "cleared_blocks": cleared_blocks,
            "blocked": blocked,
            "event_block_inconsistencies": event_block_inconsistencies,
            "human_steps": human,
            "machine_local_steps": machine_local,
            "blocked_human_steps": blocked_human,
            "blocked_machine_local_steps": blocked_machine_local,
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
            "disposal": disposal_report,
            "recorded_cause_regressions": (
                portfolio_metrics.get("recorded_cause_regressions")
                if isinstance(portfolio_metrics, dict) else None
            ),
            "command_center_ticket_pr_share": (
                portfolio_metrics.get("command_center_ticket_pr_share")
                if isinstance(portfolio_metrics, dict) else None
            ),
            "decline_routing": decline_routing,
            "resend_ratio": resend,
            "unattended_merges": merges,
            "unattended_approvals": approvals,
            "run_summary": run_summary,
            "agent_health": health,
            "working_tree_touched": touched,
            "status_state_mismatches": status_mismatches,
            "main_ci": main_ci,
            "member_issues_without_project_items": orphan_issues,
            "outcome_signals": outcome_signals,
            "rejected_merges": rejected,
            "degraded": degraded,
            "timings": timings,
            "missing": missing,
        }
        timings["brief_assembly"] = round(
            max(0.0, time.perf_counter() - assembly_started), 6
        )
        print(json.dumps(brief, indent=2))
        return 0
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


def read_lock(item: Item) -> Optional[datetime]:
    """Read one ticket's current Project lock immediately before claiming it."""
    if not item.item_id:
        raise GitHubError(
            "{} is not in the Project; cannot read its claim".format(item.ref)
        )
    node = gh_graphql(ITEM_LOCK_QUERY, item=item.item_id).get("node")
    if node is None:
        raise GitHubError(
            "{} left the Project before its claim could be read".format(item.ref)
        )
    return parse_time((node.get("lock") or {}).get("text"))


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

    if target.ref in {i.ref for i in running}:
        return "refused — {} is already claimed".format(target.ref)

    if len(running) >= WIP_LIMIT:
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
    refusal = status_write_refusal(parent, "Building")
    if refusal is not None:
        _post_status_refusal(parent, refusal)
        print("note: claimed {} but {}".format(ticket.ref, refusal),
              file=sys.stderr)
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
ORIGIN_OPTIONS = ("agent", "Nate")
RISK_OPTIONS = ("standard", "escalated")
# Third Project single-select, decided by Nate 2026-09-13 (#794), created #808.
NEEDS_FIELD_ID = "PVTSSF_lAHOD7A-N84BihDgzhiOk9Q"
NEEDS_OPTION_NONE = "259da669"
NEEDS_OPTION_HUMAN = "cda6f372"
NEEDS_OPTION_CLAUDE_CODE_ENVIRONMENT = "cffda418"
NEEDS_OPTIONS = (
    "none", "agent", "human", "claude-code-environment", "external-event",
)

_PROJECT_SELECT_CACHE: Optional[Dict[str, List[dict]]] = None


def clear_project_field_cache() -> None:
    """Discard the process-local view after a schema mutation."""
    global _PROJECT_SELECT_CACHE
    _PROJECT_SELECT_CACHE = None


def project_single_select(field_name: str) -> dict:
    """Read one uniquely named Project single-select and all option IDs.

    Field configuration is GitHub state. Writers resolve it at the point of
    use rather than duplicating newly created field and option IDs in code.
    Duplicate names and incomplete options fail closed.
    """
    global _PROJECT_SELECT_CACHE
    if _PROJECT_SELECT_CACHE is None:
        data = gh_graphql(
            PROJECT_FIELDS_QUERY, login=PROJECT_OWNER, number=PROJECT_NUMBER)
        fields = _project_fields(data)
        if fields is None:
            raise GitHubError(
                "Project {}/{} is missing or not visible".format(
                    PROJECT_OWNER, PROJECT_NUMBER))
        grouped: Dict[str, List[dict]] = {}
        for field in fields:
            if isinstance(field.get("options"), list):
                grouped.setdefault(str(field.get("name")), []).append(field)
        _PROJECT_SELECT_CACHE = grouped
    matching = _PROJECT_SELECT_CACHE.get(field_name, [])
    if len(matching) != 1:
        raise GitHubError(
            "Project field {} resolved to {} fields".format(
                field_name, len(matching)))
    field = matching[0]
    options = field.get("options")
    if not isinstance(field.get("id"), str) or not isinstance(options, list):
        raise GitHubError("Project field {} is not a single-select".format(
            field_name))
    by_name = {
        option.get("name"): option.get("id")
        for option in options if isinstance(option, dict)
    }
    if len(by_name) != len(options) or any(
            not isinstance(value, str) or not value for value in by_name.values()):
        raise GitHubError(
            "Project field {} has ambiguous or incomplete options".format(
                field_name))
    return {"id": field["id"], "options": by_name}


def write_project_select(item_id: str, field_name: str, value: str,
                         ref: str) -> None:
    """Write and confirm one canonical Project single-select value."""
    field = project_single_select(field_name)
    option = field["options"].get(value)
    if option is None:
        raise GitHubError(
            "Project field {} has no option {}".format(field_name, value))
    response = gh_graphql(
        SET_FIELD, project=PROJECT_ID, item=item_id,
        field=field["id"], option=option)
    if not _status_write_confirmed(response, item_id):
        raise GitHubError(
            "GitHub did not confirm the {} update for {} to {}".format(
                field_name, ref, value))

CLEAR_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field
  }) { projectV2Item { id } }
}
"""


def _status_write_confirmed(response: object, item_id: str) -> bool:
    """Return whether GitHub acknowledged the Project item status write."""
    if not isinstance(response, dict):
        return False
    mutation = response.get("updateProjectV2ItemFieldValue")
    if not isinstance(mutation, dict):
        return False
    project_item = mutation.get("projectV2Item")
    return (
        isinstance(project_item, dict)
        and project_item.get("id") == item_id
    )


#: The two stages a closed issue may hold. Everything else describes work in
#: progress, and a closed issue has none: #1206 moved a CLOSED project to
#: ``Ready``, which put it in the startable queue and the self-approval path
#: at once, with nothing able to close it again.
TERMINAL_STATUSES = ("Done", "Parked")


def live_issue_state(item: Item) -> Optional[str]:
    """Read one issue's state from GitHub now, or ``None`` if it cannot.

    Deliberately a fresh read rather than ``item.state``. A FunnelSession
    reuses its Project objects across commands, so the loaded state can be
    minutes old and a close that happened in between is exactly the case this
    guards. No cache, no journal, no state file: GitHub is the state.
    """
    try:
        payload = _gh_json(
            "gh", "issue", "view", str(item.number), "--repo", item.repo,
            "--json", "state",
        )
    except (GitHubError, OSError, subprocess.SubprocessError, ValueError):
        return None
    state = payload.get("state") if isinstance(payload, dict) else None
    return str(state).upper() if isinstance(state, str) and state else None


def status_write_refusal(item: Item, status: str) -> Optional[str]:
    """Why this Status write must not happen, or ``None`` to allow it.

    A closed issue may be recorded as ``Done`` or ``Parked`` — those are what
    being closed means — and as nothing else.

    When the live read cannot be made, the loaded state decides. That is a
    weaker check and deliberately not a refusal: the loaded value is usually
    seconds old and was CLOSED in the case this guards (#1206), while refusing
    on an unreadable read would let one GitHub hiccup block every approve and
    every claim. The live read is the improvement; it is not a new dependency
    the whole funnel stops for.
    """
    if status in TERMINAL_STATUSES:
        return None
    state = live_issue_state(item) or (
        str(item.state).upper() if item.state else None
    )
    if state is None or state == "OPEN":
        return None
    return (
        "refusing to write Status {} on {}: the issue is {} on GitHub, and a "
        "closed issue may only be {}".format(
            status, item.ref, state, " or ".join(TERMINAL_STATUSES)
        )
    )


def _post_status_refusal(item: Item, refusal: str) -> None:
    """Leave a refused Status write on the issue it was refused for.

    Best effort on purpose. The refusal has already done its job by not
    writing; failing to record it must not turn a safe refusal into an error.
    """
    body = (
        "**Status write refused.** {}\n\nA closed issue may be recorded as "
        "{} and as nothing else — every other stage describes work in "
        "progress, and a closed issue has none. Reopen it if the work is "
        "live, or leave the stage alone.".format(
            refusal, " or ".join(TERMINAL_STATUSES)
        )
    )
    try:
        _run_gh(
            ["gh", "issue", "comment", str(item.number), "--repo", item.repo,
             "--body", append_provenance(
                 body, "agent", at=datetime.now(timezone.utc)
             )],
            capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError, GitHubError):
        pass


def _write_status(item: Item, status: str, now: datetime) -> Optional[str]:
    """Write and locally record a Project status, or return a failure reason.

    A FunnelSession deliberately reuses its Project objects across commands.
    Updating GitHub without updating this object makes a same-session ``show``
    report the stage that was true before the mutation. Treat the mutation
    payload as the confirmation boundary: only the expected Project item
    response permits the local state and its gate timestamp to advance.

    The closed-issue guard lives here because this is the one helper every
    Status write goes through. A refusal is both returned to the caller and
    left on the issue, so the record of what was refused survives the session
    that refused it.
    """
    if not item.item_id:
        return "{} is not in the Project".format(item.ref)

    refusal = status_write_refusal(item, status)
    if refusal is not None:
        _post_status_refusal(item, refusal)
        return refusal

    try:
        response = gh_graphql(
            SET_FIELD,
            project=PROJECT_ID,
            item=item.item_id,
            field=STATUS_FIELD_ID,
            option=_option_id(STATUS_FIELD_ID, status),
        )
    except GitHubError as exc:
        return "{}".format(exc)

    if not _status_write_confirmed(response, item.item_id):
        return (
            "GitHub did not confirm the Status update for {} to {}"
            .format(item.ref, status)
        )

    previous = item.status
    item.status = status
    item.status_since = now
    if previous != status:
        item.status_events.append({
            "previous_status": previous,
            "status": status,
            "at": now,
        })
    return None


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


def reconcile_parked_wakes(
    items: Sequence[Item], now: datetime
) -> List[str]:
    """Resume dated parks whose UTC wake date has arrived.

    The latest parseable park comment is the source of both the date and the
    prior Status. A missing or malformed header is deliberately inert: the
    funnel never guesses where to put an item. Reopen first so the shared
    guarded Status writer can safely restore an active stage; if that write
    fails, the still-Parked item is eligible for an idempotent retry next run.

    Every candidate's comments are read before any write (#1592): one batched
    GraphQL pass for the bounded tails, then a complete read only for an issue
    whose full tail holds no park header. A failed or partial read raises, so
    `begin` records the reason and this fire makes no wake writes at all.
    """
    candidates = sorted(
        (
            item for item in items
            if not item.parent
            and item.status == "Parked"
            and item.state in ("CLOSED", "OPEN")
        ),
        key=lambda item: (item.repo, item.number),
    )
    if not candidates:
        return []
    woke: List[str] = []
    today = _block_condition_date(now)

    tails = _batched_issue_comments(candidates)
    parks: Dict[str, Optional[Dict[str, object]]] = {}
    for item in candidates:
        tail = tails[item.ref]
        parsed = _latest_park_comment(tail)
        if parsed is None and len(tail) >= CLOSED_ITSELF_COMMENT_PAGE_SIZE:
            # A full tail may have cut the park comment off. Reading that as
            # "no wake date" would strand the item silently, so page the whole
            # thread for this one issue instead.
            parsed = _latest_park_comment(_issue_comments(item))
        parks[item.ref] = parsed

    for item in candidates:
        parsed = parks[item.ref]
        if parsed is None:
            continue

        wake_date = parsed.get("wake_date")
        prior_status = parsed.get("prior_status")
        if (
            not isinstance(wake_date, date)
            or isinstance(wake_date, datetime)
            or wake_date > today
            or not isinstance(prior_status, str)
            or prior_status not in PARK_WAKE_STATUSES
        ):
            continue
        if not item.item_id:
            raise GitHubError(
                "{} is not in the Project; cannot restore its wake Status"
                .format(item.ref)
            )

        if item.state == "CLOSED":
            reopened = _run_gh(
                [
                    "gh", "issue", "reopen", str(item.number),
                    "--repo", item.repo,
                ],
                capture_output=True, text=True,
            )
            if reopened.returncode != 0:
                raise GitHubError(
                    "could not reopen {} for its wake date: {}".format(
                        item.ref, reopened.stderr.strip()
                    )
                )
            item.state = "OPEN"
            item.state_reason = "REOPENED"

        refusal = _write_status(item, prior_status, now)
        if refusal is not None:
            raise GitHubError(
                "could not restore {} to Status {} after its wake date: {}"
                .format(item.ref, prior_status, refusal)
            )
        woke.append(item.ref)

    return woke


def _latest_park_comment(
    comments: Sequence[object],
) -> Optional[Dict[str, object]]:
    """Parse the newest park header in oldest-first comments, if any."""
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        parsed = parse_park_comment(comment.get("body") or "")
        if parsed is not None:
            return parsed
    return None


def cmd_park(items: List[Item], now: datetime, ref: str, reason: str,
             run: Optional[str] = None, agent: Optional[str] = None,
             wake_date: Optional[date] = None) -> int:
    """Park a project with its durable reason attached to the issue."""
    item = find(items, ref)
    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))
    if wake_date is not None:
        if wake_date <= _block_condition_date(now):
            raise GitHubError("wake date must be after today's UTC date")
        if item.status not in PARK_WAKE_STATUSES:
            raise GitHubError(
                "a wake date requires a recorded Project Status before parking"
            )

    park_comment = PARK_COMMENT_PREFIX + reason
    if wake_date is not None:
        park_comment = "{}date={} status={}\n{}".format(
            PARK_WAKE_PREFIX, wake_date.isoformat(), item.status, park_comment
        )

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
             park_comment, "nate-relayed", at=now,
             run=run, agent=agent)],
        capture_output=True, text=True,
    )
    if comment.returncode != 0:
        raise GitHubError(comment.stderr.strip())

    print("{} → Parked\n{}".format(item.ref, park_comment))
    return 0


def cmd_answer_gates(items: List[Item], now: datetime, ref: str,
                     answer: str, decider: str,
                     run: Optional[str] = None,
                     agent: Optional[str] = None) -> int:
    """Record one answered Gates question in the plan body.

    The sanctioned post-Ready write. It is one ``gh issue edit``: the marker
    and the Gates line move together or neither does, so no reader can catch
    the body in a state where the two disagree.

    Deliberately not gated on Status. The question this answers is asked from
    a block, and a blocked project can be sitting at Shaped, Ready or Building
    depending on when the lane reached it; refusing on a stage would make the
    write unusable exactly where #1167 happened. What is enforced is the
    shape of the change, which is the thing that can corrupt a plan.
    """
    item = find(items, ref)
    if item.state != "OPEN":
        raise GitHubError(
            "{} is {}; a closed project takes no answer".format(
                item.ref, item.state)
        )
    if item.parent is not None:
        raise GitHubError(
            "{} is a ticket; the Gates question belongs to its project "
            "{}".format(item.ref, item.parent)
        )
    if not item.item_id:
        raise GitHubError("{} is not in the Project".format(item.ref))

    body = answered_gates_body(
        item.body or "", answer, decider, at=now, run=run, agent=agent)
    out = _run_gh(
        ["gh", "issue", "edit", str(item.number), "--repo", item.repo,
         "--body", body],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    # The caller may evaluate this item again in the same session; keep it
    # aligned with what GitHub now holds rather than with what it held.
    item.body = body
    remaining_needs = "human" if plan_needs_nate(body) else "none"
    write_project_select(item.item_id, "Needs", remaining_needs, item.ref)
    item.needs = remaining_needs

    recorded = parse_gates_answer(body)
    if recorded is None:
        # Unreachable through this path, and checked anyway: the reader is
        # the whole point of the write, and a record it rejects is a gate
        # that stays open while the body claims otherwise.
        raise GitHubError(
            "wrote {} but the answered-Gates reader rejects the "
            "record".format(item.ref)
        )
    print("{} Gates answered by {}\n{}\n{}".format(
        item.ref, recorded["decider"], recorded["answer"], item.url))
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
        if not item.item_id:
            raise GitHubError("{} is not in the Project".format(item.ref))
        write_project_select(item.item_id, "Needs", "human", item.ref)
        item.needs = "human"
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
        "- Ticket: {}".format(ticket.ref if ticket else (ref or "unknown")),
        "",
        "**What this means:** not that there is a bug, but that the auto-merge bar",
        "failed. Three of these in a week and auto-merging stops until the review",
        "prompt in `routines/muse-review.md` is fixed.",
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
        print("STOP AUTO-MERGING. Fix routines/muse-review.md before the next run.")
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


# `gh project item-add` can fail transiently after the issue has already been
# created. Keep this retry local to capture so a retry never creates a second
# issue or repeats the later field writes.
CAPTURE_ITEM_ADD_MAX_ATTEMPTS = 3
CAPTURE_ITEM_ADD_RETRY_DELAY_SECONDS = 0.5
CAPTURE_ITEM_ADD_TRANSIENT_SIGNALS = (
    "something went wrong while executing your query",
    "internal server error",
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "connection reset by peer",
    "connection refused",
    "connection timed out",
    "context deadline exceeded",
    "i/o timeout",
    "tls handshake timeout",
    "temporary failure in name resolution",
    "network is unreachable",
    "no such host",
    "unexpected eof",
)


def _capture_item_add_error(add) -> str:
    """Return the most useful error text from a failed Project add."""
    for stream in ("stderr", "stdout"):
        detail = str(getattr(add, stream, "") or "").strip()
        if detail:
            return detail
    return "gh project item-add exited with status {}".format(
        getattr(add, "returncode", "unknown")
    )


def _capture_item_add_is_transient(error: str) -> bool:
    """Return whether an item-add error is an explicitly known transient."""
    lowered = error.lower()
    if any(signal in lowered for signal in CAPTURE_ITEM_ADD_TRANSIENT_SIGNALS):
        return True
    return bool(re.search(r"\b(?:500|502|503|504)\b", lowered))


def cmd_capture(items: List[Item], now: datetime, title: str, note: Optional[str],
                repo: Optional[str], run: Optional[str] = None,
                agent: Optional[str] = None,
                origin: Optional[str] = None,
                klass: Optional[str] = None,
                caused_by: Optional[Sequence[str]] = None) -> int:
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
    caused_by_refs = []
    if caused_by is not None:
        caused_by_refs = [
            value.strip() for value in caused_by
            if isinstance(value, str) and value.strip()
        ]
        if not caused_by_refs:
            raise GitHubError("--caused-by requires a non-empty PR or ticket reference")
    repo = capture_repo(repo, run, agent)
    body = append_provenance(
        note or "Captured from chat. Not yet thought through.", "agent",
        at=now, run=run, agent=agent,
    )
    if caused_by_refs:
        body = append_caused_by(body, caused_by_refs, at=now)
    args = [
        "gh", "issue", "create", "--repo", repo, "--title", title,
        "--body", body, "--label", "needs-shaping",
    ]
    out = _run_gh(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise GitHubError(out.stderr.strip())
    url = out.stdout.strip().splitlines()[-1]

    add_args = [
        "gh", "project", "item-add", str(PROJECT_NUMBER), "--owner", PROJECT_OWNER,
        "--url", url, "--format", "json",
    ]
    add = None
    for attempt in range(CAPTURE_ITEM_ADD_MAX_ATTEMPTS):
        add = _run_gh(add_args, capture_output=True, text=True)
        if add.returncode == 0:
            break
        error = _capture_item_add_error(add)
        if (attempt + 1 >= CAPTURE_ITEM_ADD_MAX_ATTEMPTS
                or not _capture_item_add_is_transient(error)):
            break
        time.sleep(CAPTURE_ITEM_ADD_RETRY_DELAY_SECONDS)

    if add is not None and add.returncode == 0:
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
        write_project_select(
            item_id, "Origin", "agent" if origin == "agent" else "Nate", url)
        write_project_select(
            item_id, "Risk", required_tier(title, note or ""), url)
        write_project_select(item_id, "Needs", "none", url)
        print("{}  → Ideas (needs-shaping) in {}".format(url, repo))
    else:
        raise GitHubError(_capture_item_add_error(add))
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


def _gh_api_command(endpoint: str, *, cache: bool = False) -> List[str]:
    """Build one read-only REST command with an explicit cache policy.

    The endpoint-only form is a GET. `cache=True` is reserved for data that is
    advisory or diagnostic; callers that influence queue, claim, or gate
    decisions must leave it false. Put the flags after the endpoint so the
    command remains easy for fixture doubles to inspect and for `gh` to parse.
    """
    command = ["gh", "api", endpoint]
    if cache:
        command.extend(["--cache", GH_API_CACHE_DURATION])
    return command


def _gh_api_json(endpoint: str, *, cache: bool = False):
    """Read one REST GET, optionally through `gh`'s bounded response cache."""
    return _gh_json(*_gh_api_command(endpoint, cache=cache))


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


def _brief_comment_query(
    items: Sequence[Item],
) -> Tuple[str, Dict[str, Tuple[str, str]]]:
    """Build one bounded GraphQL read for a batch of candidate issues."""
    grouped: Dict[str, List[Item]] = {}
    for item in items:
        grouped.setdefault(item.repo, []).append(item)

    lines = [
        "query {",
        "  rateLimit { cost remaining resetAt }",
    ]
    aliases: Dict[str, Tuple[str, str]] = {}
    for repo_index, (repo, repo_items) in enumerate(grouped.items()):
        try:
            owner, name = repo.split("/", 1)
        except ValueError:
            raise GitHubError("invalid repository ref {}".format(repo))
        repo_alias = "repo{}".format(repo_index)
        lines.append(
            "  {}: repository(owner: {}, name: {}) {{".format(
                repo_alias, json.dumps(owner), json.dumps(name)
            )
        )
        for issue_index, item in enumerate(repo_items):
            issue_alias = "issue{}".format(issue_index)
            lines.append(
                "    {}: issue(number: {}) {{ comments(last: {}) {{ "
                "nodes {{ body createdAt }} }} }}".format(
                    issue_alias, item.number, CLOSED_ITSELF_COMMENT_PAGE_SIZE
                )
            )
            aliases[item.ref] = (repo_alias, issue_alias)
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines), aliases


def _closed_itself_comment_query(
    items: Sequence[Item],
) -> Tuple[str, Dict[str, Tuple[str, str]]]:
    """Compatibility wrapper for the brief's shared comment query builder."""
    return _brief_comment_query(items)


def _batched_issue_comments(
    items: Sequence[Item],
) -> Dict[str, List[Dict]]:
    """Read bounded comment tails in GraphQL batches, with measured cost."""
    found: Dict[str, List[Dict]] = {}
    for start in range(0, len(items), CLOSED_ITSELF_COMMENT_BATCH_SIZE):
        batch = items[start:start + CLOSED_ITSELF_COMMENT_BATCH_SIZE]
        query, aliases = _brief_comment_query(batch)
        data = gh_graphql(query)
        for item in batch:
            repo_alias, issue_alias = aliases[item.ref]
            repository = data.get(repo_alias) if isinstance(data, dict) else None
            issue = (
                repository.get(issue_alias)
                if isinstance(repository, dict)
                else None
            )
            comments = issue.get("comments") if isinstance(issue, dict) else None
            nodes = comments.get("nodes") if isinstance(comments, dict) else None
            if not isinstance(nodes, list):
                raise GitHubError(
                    "could not read comments for {}".format(item.ref)
                )
            found[item.ref] = [
                comment for comment in nodes if isinstance(comment, dict)
            ]
    return found


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
    item.block_event = None
    parsed = _parse_block_comment_details(bodies)
    if parsed is not None:
        (
            item.block_references,
            item.blocked_until,
            item.block_reason,
            item.block_event,
        ) = parsed
    item.needs_decision = parse_needs_decision_comment(bodies)
    item.decline_reason = parse_decline_comment(bodies)
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


def _loaded_item_body(item: Item) -> str:
    """Use the Project-loaded body without opening a candidate issue view."""
    body = getattr(item, "body", None)
    if isinstance(body, str):
        return body
    # A few library callers pass a lightweight candidate object rather than an
    # Item. Keep that compatibility path explicit; real Project Items always
    # carry ``body`` from the single Project load above.
    if not hasattr(item, "body"):
        repo = getattr(item, "repo", None)
        number = getattr(item, "number", None)
        if isinstance(repo, str) and number is not None:
            return _ticket_body(repo, number)
    return ""


def _accepts_keyword(func: Callable, name: str) -> bool:
    """Whether a callable can receive a keyword added by a newer seam."""
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == name
        or parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _call_with_optional_keyword(
    func: Callable, keyword: str, value: object, *args
):
    """Call a seam with its new shared snapshot when the seam supports it."""
    if _accepts_keyword(func, keyword):
        return func(*args, **{keyword: value})
    return func(*args)


def _call_with_optional_keywords(func: Callable, *args, **kwargs):
    """Pass only supported optional keywords to a compatibility seam."""
    accepted = {
        name: value for name, value in kwargs.items()
        if _accepts_keyword(func, name)
    }
    return func(*args, **accepted)


PR_GRAPHQL_PAGE_SIZE = 100
PR_GRAPHQL_COMMENT_PAGE_SIZE = 100
PR_GRAPHQL_REF_PAGE_SIZE = 100

#: Pull requests asked for per repository per page of the batched PR read.
#: Deliberately below the API maximum the sub-selections above use. One
#: document asking 100 PRs per member repository, each with its comment tail,
#: body, refs and check rollup, stopped being served as the board grew past a
#: thousand items: HTTP 502/504 at about 37 seconds, six attempts out of six
#: (#1217, measured 2026-09-21). At 50 the same call returns in 13.2s with
#: identical coverage, because MERGED_PR_SCAN_LIMIT still decides how many rows
#: arrive — this decides only how many are asked for at once. The ceiling moves
#: with the board, so this is a measured number: if the read starts timing out
#: again, measure and halve it rather than raising it back.
PR_GRAPHQL_PR_PAGE_SIZE = 50


def _batched_pr_query(
    repos: Sequence[str],
    first: Mapping[str, int],
    states: Sequence[str],
    *,
    include_comments: bool,
    include_reviews: bool,
    include_closing_refs: bool,
    include_refs: bool,
    include_body: bool = False,
) -> Tuple[str, Dict[str, str]]:
    """Build one GraphQL document for the active repository PR pages.

    Repository names are known GitHub refs, but JSON quoting still matters:
    they become GraphQL string literals rather than variables because a single
    document needs a separate repository root for each member repo. Cursors
    remain variables so the same batch can page without constructing an
    unbounded number of documents.
    """
    if not repos:
        raise ValueError("at least one repository is required")
    if not states:
        raise ValueError("at least one pull-request state is required")

    aliases: Dict[str, str] = {}
    lines = ["query({}) {{".format(
        ", ".join("${}: String".format("cursor{}".format(index))
                  for index in range(len(repos)))
    )]
    lines.append("  rateLimit { cost remaining resetAt }")
    state_literal = "[{}]".format(", ".join(states))

    for index, repo in enumerate(repos):
        try:
            owner, name = repo.split("/", 1)
        except ValueError:
            raise GitHubError("invalid repository ref {}".format(repo))
        alias = "repo{}".format(index)
        cursor_name = "cursor{}".format(index)
        aliases[repo] = alias
        lines.append(
            "  {}: repository(owner: {}, name: {}) {{".format(
                alias, json.dumps(owner), json.dumps(name)
            )
        )
        lines.append(
            "    pullRequests(first: {}, after: ${}, states: {}, "
            "orderBy: {{field: CREATED_AT, direction: DESC}}) {{".format(
                first[repo], cursor_name, state_literal
            )
        )
        lines.append("      pageInfo { hasNextPage endCursor }")
        lines.append("      nodes {")
        lines.append(
            "        number title state url headRefName headRefOid "
            "mergeable mergeStateStatus mergedAt createdAt closedAt"
        )
        if include_body:
            lines.append("        body")
        lines.append("        author { login }")
        lines.append("        mergedBy { login }")
        if include_reviews:
            lines.append(
                "        reviews(first: {}) {{ nodes {{ body state submittedAt "
                "author {{ login }} }} }}".format(PR_GRAPHQL_PAGE_SIZE)
            )
        if include_comments:
            lines.append(
                "        comments(last: {}) {{ nodes {{ body createdAt "
                "author {{ login }} }} }}".format(
                    PR_GRAPHQL_COMMENT_PAGE_SIZE
                )
            )
        if include_closing_refs:
            lines.append(
                "        closingIssuesReferences(first: {}) {{ nodes {{ number "
                "repository {{ nameWithOwner }} }} }}".format(
                    PR_GRAPHQL_PAGE_SIZE
                )
            )
        lines.append("        commits(last: 1) {")
        lines.append("          nodes {")
        lines.append("            commit {")
        lines.append("              statusCheckRollup {")
        lines.append("                contexts(first: {}) {{".format(
            PR_GRAPHQL_PAGE_SIZE
        ))
        lines.append("                  nodes {")
        lines.append("                    __typename")
        lines.append(
            "                    ... on CheckRun { name conclusion status }"
        )
        lines.append(
            "                    ... on StatusContext { context state }"
        )
        lines.append("                  }")
        lines.append("                }")
        lines.append("              }")
        lines.append("            }")
        lines.append("          }")
        lines.append("        }")
        lines.append("      }")
        lines.append("    }")
        if include_refs:
            lines.append(
                "    refs(refPrefix: \"refs/heads/\", first: {}) "
                "{{ pageInfo {{ hasNextPage }} "
                "nodes {{ name }} }}".format(PR_GRAPHQL_REF_PAGE_SIZE)
            )
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines), aliases


def _normalise_pr_node(node: object) -> Optional[Dict[str, object]]:
    """Convert one GraphQL PullRequest node to the established row shape."""
    if not isinstance(node, dict) or node.get("number") is None:
        return None

    row: Dict[str, object] = {
        name: node.get(name)
        for name in (
            "number", "title", "state", "url", "headRefName",
            "headRefOid", "mergeable", "mergeStateStatus", "mergedAt",
            "createdAt", "closedAt",
        )
    }
    if "body" in node:
        row["body"] = node.get("body")
    for name in ("author", "mergedBy"):
        value = node.get(name)
        row[name] = value if isinstance(value, dict) else None

    for name in ("reviews", "comments", "closingIssuesReferences"):
        connection = node.get(name)
        if isinstance(connection, dict) and isinstance(connection.get("nodes"), list):
            row[name] = [
                value for value in connection["nodes"] if isinstance(value, dict)
            ]

    checks: List[Dict[str, object]] = []
    commits = node.get("commits")
    commit_nodes = commits.get("nodes") if isinstance(commits, dict) else None
    if isinstance(commit_nodes, list):
        for commit_node in commit_nodes:
            if not isinstance(commit_node, dict):
                continue
            commit = commit_node.get("commit")
            rollup = (
                commit.get("statusCheckRollup")
                if isinstance(commit, dict) else None
            )
            contexts = (
                rollup.get("contexts")
                if isinstance(rollup, dict) else None
            )
            context_nodes = (
                contexts.get("nodes") if isinstance(contexts, dict) else None
            )
            if isinstance(context_nodes, list):
                checks.extend(
                    value for value in context_nodes if isinstance(value, dict)
                )
    row["statusCheckRollup"] = checks
    return row


def _read_batched_pr_snapshots(
    repos: Sequence[str],
    *,
    states: Sequence[str] = ("OPEN", "CLOSED", "MERGED"),
    limit: int = MERGED_PR_SCAN_LIMIT,
    include_comments: bool = False,
    include_reviews: bool = False,
    include_closing_refs: bool = False,
    include_refs: bool = False,
    include_body: bool = False,
) -> BatchedPRRead:
    """Read bounded PR and ticket-branch facts in repository-wide batches.

    A normal funnel read fits in one GraphQL request across all member repos.
    Larger historical consumers page at 100 nodes, still batching every active
    repository and never falling back to a request per PR. Every document asks
    for its own ``rateLimit.cost`` so the measured spend belongs to the query
    that produced it.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("PR scan limit must be an integer")
    if limit <= 0:
        raise ValueError("PR scan limit must be positive")
    unique_repos = sorted(set(repos))
    if not unique_repos:
        return BatchedPRRead({}, {}, {}, {})

    rows_by_repo: Dict[str, List[Dict[str, object]]] = {
        repo: [] for repo in unique_repos
    }
    branch_refs_by_repo: Dict[str, Set[str]] = {
        repo: set() for repo in unique_repos
    }
    pr_truncated_by_repo: Dict[str, bool] = {
        repo: False for repo in unique_repos
    }
    branches_truncated_by_repo: Dict[str, bool] = {
        repo: False for repo in unique_repos
    }
    active = list(unique_repos)
    cursors: Dict[str, Optional[str]] = {repo: None for repo in unique_repos}
    page_number = 0

    while active:
        first = {
            repo: min(
                PR_GRAPHQL_PR_PAGE_SIZE,
                limit + 1 - len(rows_by_repo[repo])
                if limit < PR_GRAPHQL_PR_PAGE_SIZE
                else PR_GRAPHQL_PR_PAGE_SIZE,
            )
            for repo in active
        }
        # A page is never empty: a limit below 100 still requests one extra
        # row to preserve the old truncation signal.
        first = {repo: max(1, value) for repo, value in first.items()}
        query, aliases = _batched_pr_query(
            active,
            first,
            states,
            include_comments=include_comments,
            include_reviews=include_reviews,
            include_closing_refs=include_closing_refs,
            include_refs=include_refs and page_number == 0,
            include_body=include_body,
        )
        variables = {
            "cursor{}".format(index): cursors[repo]
            for index, repo in enumerate(active)
            if cursors[repo] is not None
        }
        data = gh_graphql(query, **variables)
        if not isinstance(data, dict):
            raise GitHubError("batched PR response was not an object")

        next_active: List[str] = []
        for repo in active:
            alias = aliases[repo]
            repository = data.get(alias)
            if not isinstance(repository, dict):
                raise GitHubError(
                    "could not read repository {} in batched PR response".format(
                        repo
                    )
                )
            pull_requests = repository.get("pullRequests")
            if not isinstance(pull_requests, dict):
                raise GitHubError(
                    "invalid pull-request response for {}".format(repo)
                )
            nodes = pull_requests.get("nodes")
            page_info = pull_requests.get("pageInfo")
            if not isinstance(nodes, list) or not isinstance(page_info, dict):
                raise GitHubError(
                    "invalid pull-request page for {}".format(repo)
                )
            rows_by_repo[repo].extend(
                row for row in (_normalise_pr_node(node) for node in nodes)
                if row is not None
            )

            if page_number == 0 and include_refs:
                refs = repository.get("refs")
                if not isinstance(refs, dict) or not isinstance(
                    refs.get("nodes"), list
                ):
                    raise GitHubError(
                        "invalid ticket-branch response for {}".format(repo)
                    )
                for ref_node in refs["nodes"]:
                    if not isinstance(ref_node, dict):
                        continue
                    branch = str(ref_node.get("name") or "")
                    ref = ticket_ref_from_branch(repo, branch)
                    if ref:
                        branch_refs_by_repo[repo].add(ref)
                branches_truncated_by_repo[repo] = bool(
                    (refs.get("pageInfo") or {}).get("hasNextPage")
                )

            has_next = bool(page_info.get("hasNextPage"))
            if len(rows_by_repo[repo]) >= limit:
                pr_truncated_by_repo[repo] = has_next or len(
                    rows_by_repo[repo]
                ) > limit
                rows_by_repo[repo] = rows_by_repo[repo][:limit]
                continue
            if not has_next:
                continue
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise GitHubError(
                    "pull-request page for {} has no next cursor".format(repo)
                )
            cursors[repo] = cursor
            next_active.append(repo)
        active = next_active
        page_number += 1

    return BatchedPRRead(
        rows_by_repo={repo: tuple(rows) for repo, rows in rows_by_repo.items()},
        branch_refs_by_repo=branch_refs_by_repo,
        pr_truncated_by_repo=pr_truncated_by_repo,
        branches_truncated_by_repo=branches_truncated_by_repo,
    )


def _latest_verdict_from_comments(comments: object) -> Optional[Dict]:
    """Return the newest structured verdict from an already-read comment tail."""
    if not isinstance(comments, list):
        return None
    for row in reversed(comments):
        if not isinstance(row, dict):
            continue
        found = _verdict_from_comment(row)
        if found:
            return found
    return None


def _pr_rows_for_ref(
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]],
    ref: str,
) -> Tuple[Dict[str, object], ...]:
    """Return every row for a ticket branch, with legacy-map compatibility."""
    if pr_facts is None:
        return ()
    rows_by_ref = getattr(pr_facts, "rows_by_ref", None)
    if isinstance(rows_by_ref, Mapping) and ref in rows_by_ref:
        return tuple(
            row for row in rows_by_ref[ref] if isinstance(row, dict)
        )
    fact = pr_facts.get(ref)
    # A fact with neither a PR number nor a state is a branch-only record: a
    # truncated PR scan or a pushed branch with no PR. It is not a PR row, and
    # reading it as one made every command-center ticket look awaiting review
    # (#968). Older fixture maps carry ``state`` without ``number``.
    if isinstance(fact, dict) and (
        fact.get("number") is not None or fact.get("state")
    ):
        return (fact,)
    return ()


def _row_verdict(row: Mapping[str, object], repo: str) -> Optional[Dict]:
    """Use the batch's comment tail, falling back for old fixture maps."""
    if "verdict" in row:
        value = row.get("verdict")
        return value if isinstance(value, dict) else None
    number = row.get("number")
    return latest_verdict(repo, number) if number is not None else None


def _pr_fact_for_number(
    repo: str, number: int, *, include_comments: bool = True
) -> Optional[Dict[str, object]]:
    """Read one repository batch and select a PR by number for explicit gates."""
    snapshot = _read_batched_pr_snapshots(
        [repo],
        states=("OPEN", "CLOSED", "MERGED"),
        limit=PR_GRAPHQL_PAGE_SIZE,
        include_comments=include_comments,
        include_reviews=False,
        include_refs=False,
    )
    for row in snapshot.rows_by_repo.get(repo, ()):
        if row.get("number") == number:
            return dict(row)
    return None


def ticket_pr_index(
    repo: str, limit: int = MERGED_PR_SCAN_LIMIT, *,
    include_comments: bool = False,
) -> Tuple[Dict[str, Dict], bool]:
    """Every `ticket/<n>` PR in one repo, indexed by ticket ref.

    One bounded GraphQL page for the whole repository, so a caller pays once
    however many tickets it is about to ask about. Returns the index and whether
    the scan was truncated, because a truncated scan cannot tell "no PR" from
    "PR older than the window" and only the caller knows which answer is safe.

    ``limit`` defaults to the small bound used by the brief. The outcome walker
    passes a larger bound for its full-history read, but still uses this helper
    so it cannot regress to one PR lookup per ticket. The returned mapping also
    exposes the rows in the scan as ``all_rows`` for consumers that need to
    count more than one PR on a branch. ``include_comments`` is an opt-in for
    the outcome backfill: the normal funnel path does not pay to fetch PR
    comment history, while the backfill can derive verdicts from this same
    repository-wide scan instead of doing one ``gh pr view`` per PR.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("ticket PR scan limit must be an integer")
    if limit <= 0:
        raise ValueError("ticket PR scan limit must be positive")
    snapshot = _read_batched_pr_snapshots(
        [repo],
        states=("OPEN", "CLOSED", "MERGED"),
        limit=limit,
        include_comments=include_comments,
        include_reviews=include_comments,
        include_closing_refs=include_comments,
        include_refs=False,
    )
    bounded_rows = list(snapshot.rows_by_repo.get(repo, ()))
    truncated = bool(snapshot.pr_truncated_by_repo.get(repo))
    index = TicketPRIndex(all_rows=bounded_rows)
    for row in bounded_rows:
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
    # Branch presence protects stale-claim recovery and WIP ownership. Never
    # serve it from the response cache: a stale branch read can release work
    # that is still being pushed or preserve a claim that should be recovered.
    rows = _gh_api_json(
        "repos/{}/git/matching-refs/heads/ticket?per_page={}".format(
            repo, MERGED_PR_SCAN_LIMIT
        ),
        cache=False,
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

    One bounded GraphQL batch per active page covers every member repository's
    PR rows, comment tails, CI contexts, and ticket branches — never one lookup
    per ticket or PR. The per-ticket PR form was the single largest GraphQL
    consumer in the system: 68 requests on the board of 2026-09-08, 93 of a
    full brief's 110 points, and it grew with the board (#272).

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
    facts: TicketPRFacts = TicketPRFacts()
    repos = sorted({item.repo for item in wanted.values()})
    if not repos:
        return facts

    snapshot = _read_batched_pr_snapshots(
        repos,
        states=("OPEN", "CLOSED", "MERGED"),
        limit=MERGED_PR_SCAN_LIMIT,
        include_comments=True,
        include_reviews=False,
        include_closing_refs=False,
        include_refs=True,
        include_body=True,
    )
    rows_by_ref: Dict[str, List[Dict[str, object]]] = {}
    for repo in repos:
        for row in snapshot.rows_by_repo.get(repo, ()):
            ref = ticket_ref_from_branch(repo, row.get("headRefName") or "")
            if ref:
                rows_by_ref.setdefault(ref, []).append(row)

    # Verdicts are derived from the same bounded comment tails. This keeps the
    # current-head rules intact while making the cost visible on the batch
    # response instead of issuing one ``gh pr view`` per open PR.
    for rows in rows_by_ref.values():
        for row in rows:
            if str(row.get("state") or "").upper() == "OPEN":
                row["verdict"] = _latest_verdict_from_comments(
                    row.get("comments")
                )

    facts = TicketPRFacts(rows_by_ref=rows_by_ref)
    for ref, item in wanted.items():
        repo_rows = rows_by_ref.get(ref, [])
        repo = item.repo
        truncated = bool(snapshot.pr_truncated_by_repo.get(repo))
        branches_truncated = bool(
            snapshot.branches_truncated_by_repo.get(repo)
        )
        fact: Optional[Dict[str, object]]
        if repo_rows:
            fact = dict(repo_rows[0])
        elif not truncated:
            fact = None
        else:
            # PR absence is unknown beyond the bounded history, but a complete
            # branch scan can still establish branch absence.
            fact = {}

        if ref in snapshot.branch_refs_by_repo.get(repo, set()):
            if fact is None:
                fact = {"headRefName": "ticket/{}".format(item.number)}
            fact["branch_exists"] = True
        elif not branches_truncated:
            if fact is not None:
                fact["branch_exists"] = False
        elif fact:
            # Preserve useful PR diagnostics while explicitly withholding the
            # branch-absence conclusion from stale-lock detection.
            fact["branch_exists"] = None

        if fact is not None:
            facts[ref] = fact
        elif not branches_truncated:
            # A complete branch scan established both absences. With a
            # truncated branch page, omitting the key keeps stale-lock and
            # stranded consumers from treating unknown as no branch.
            facts[ref] = None

    return facts


def review_queue(
    items: Sequence[Item], tier: Optional[str] = None,
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]] = None,
    output_stream: Optional[IO[str]] = None,
) -> List[Dict]:
    """Open ticket PRs that need a review, best-first.

    A PR needs review when no verdict covers its **current head**. That covers
    three cases with one rule: never reviewed, reviewed and then pushed to, and
    reviewed-and-rejected then fixed. The last is what hands a rejected PR back
    to a reviewer once the engineer has acted on it.

    `tier` filters by the *ticket's* risk, not the PR's size. A reviewer is
    matched to the work the same way an engine is: the expensive judgement is
    spent where the ticket says the stakes are, and nowhere else.

    A PR whose checks are still running is not offered. A ticket branch that
    GitHub reports as conflicting is rejected mechanically at its current
    head and never offered for model review; repeated queue reads leave that
    canonical rejection in place until the engineer pushes a new head. Red CI
    and a normal empty rollup remain visible to the review pre-check.
    """
    if pr_facts is None:
        pr_facts = ticket_pr_facts(items)
    found: List[Dict] = []
    for ticket in items:
        if str(getattr(ticket, "state", "OPEN") or "OPEN").upper() != "OPEN":
            continue
        for row in _pr_rows_for_ref(pr_facts, ticket.ref):
            repo = ticket.repo
            if str(row.get("state") or "OPEN").upper() != "OPEN":
                continue
            head = row.get("headRefName") or ""
            if not head.startswith("ticket/"):
                continue
            conflict = _conflicting_branch_blocker(row)
            if conflict is not None:
                # Conflict is a complete, deterministic rejection. Record it
                # against this snapshot's head before it can reach a model
                # reviewer. The gate helper makes repeat ticks idempotent and
                # replaces any less-specific verdict on the same head.
                head_sha = row.get("headRefOid")
                number = row.get("number")
                if head_sha and number is not None:
                    _record_unmergeable_rejection(
                        repo,
                        number,
                        pr_fact=row,
                        candidate_verdict={
                            "verdict": "rejected",
                            "ci": "unknown",
                            "head_sha": head_sha,
                        },
                        output_stream=output_stream,
                    )
                continue
            if checks_still_running(row.get("statusCheckRollup")):
                # The checks have not reported yet, so the only answer a
                # reviewer could record is "CI not green (state unknown)" —
                # and that rejection then covers this head, locking the PR
                # out of re-review once CI turns green (#900). Wait instead:
                # the next tick reconsiders, because nothing was recorded.
                continue
            verdict = _row_verdict(row, repo)
            if verdict_covers_head(
                verdict, row.get("headRefOid"), row.get("comments")
            ):
                continue  # this exact diff has already been judged
            recorded_risk = getattr(ticket, "risk", None)
            needed = (
                recorded_risk if recorded_risk in RISK_OPTIONS else "escalated"
            )
            if tier and needed != tier:
                continue
            candidate = {"pr": row.get("number"), "repo": repo,
                         "ref": ticket.ref,
                         "tier": needed, "url": ticket.url,
                         "title": ticket.title,
                         "opened": row.get("createdAt") or ""}
            found.append(candidate)
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

    Each tier shapes its own ideas: an escalated run is offered the
    first escalated-tier idea, a standard run the first standard-tier
    one (#1135 reverses #1026's escalated suppression while the Claude
    routine is off).
    """
    import usage

    if not usage.shaping_allowed(reading):
        return None

    for item in ideas(items):
        if "needs-shaping" not in getattr(item, "labels", ()):
            continue
        recorded_risk = getattr(item, "risk", None)
        needed = (
            recorded_risk if recorded_risk in RISK_OPTIONS else "escalated"
        )
        if tier is not None and needed != tier:
            continue
        return item
    return None


def shaped_self_approvable(item: Item,
                           by_ref: Dict[str, Item]) -> bool:
    """Re-evaluate one Shaped plan with the existing self-approval rule."""
    body = _loaded_item_body(item)
    override = parse_origin_override(body)
    override_target = override["target"] if override is not None else None
    return self_approval_eligible(
        effective_class(item, by_ref),
        item.origin,
        override_target,
        needs_nate=item.needs == "human",
        escalated=item.risk != "standard",
        state=item.state,
    )


def sweep_shaped_self_approvals(
    items: Sequence[Item], now: datetime,
    run: Optional[str] = None, agent: Optional[str] = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Advance stranded Shaped plans using the normal Status and marker writes.

    The Shaped gate is re-checked by the same predicate used when a plan is
    first written. Other live questions, such as an unblock question, remain
    owned by their existing gate and are not swept.
    """
    by_ref = {item.ref: item for item in items}
    advanced: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for item in items:
        if item.state != "OPEN" or item.status != "Shaped":
            continue
        question = gate_question(item)
        if question is not None and question != GATES["Shaped"]:
            continue
        if not shaped_self_approvable(item, by_ref):
            continue

        body = _loaded_item_body(item)
        origin_voice = item.origin
        klass = effective_class(item, by_ref)
        owner_basis = (
            "origin agent" if origin_voice == "agent"
            else "origin override to agents"
        )
        reason = "needs_nate all null; class {} self-approvable; {}".format(
            klass, owner_basis
        )

        try:
            status_error = _write_status(item, "Ready", now)
        except (OSError, subprocess.SubprocessError, GitHubError) as exc:
            status_error = str(exc)
        if status_error is not None:
            errors.append({"ref": item.ref, "error": status_error})
            continue

        basis = "{}; no escalated risk".format(reason)
        authority_signals = needs_nate_signals(body)
        if authority_signals:
            basis += "; authority signals: {}".format(
                ", ".join(authority_signals)
            )
        try:
            comment = _run_gh(
                ["gh", "issue", "comment", str(item.number),
                 "--repo", item.repo,
                 "--body", self_approval_comment(
                     basis, at=now, run=run, agent=agent
                 )],
                capture_output=True, text=True,
            )
        except (OSError, subprocess.SubprocessError, GitHubError) as exc:
            errors.append({
                "ref": item.ref,
                "error": (
                    "Ready was written but the Self-approved marker failed: {}"
                    .format(exc)
                ),
            })
            continue
        if comment.returncode != 0:
            errors.append({
                "ref": item.ref,
                "error": (
                    "Ready was written but the Self-approved marker failed: {}"
                    .format(comment.stderr.strip())
                ),
            })
            continue
        advanced.append({"ref": item.ref, "status": "Ready"})
    return advanced, errors


def approved_merge_candidates(
    items: Sequence[Item],
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Find open ticket PRs whose latest verdict approves their current head."""
    tickets = {
        item.ref: item
        for item in items
        if (getattr(item, "state", "OPEN") or "OPEN").upper() == "OPEN"
    }
    if not tickets:
        return []
    if pr_facts is None:
        pr_facts = ticket_pr_facts(items)

    candidates: List[Dict[str, object]] = []
    for ref, ticket in tickets.items():
        for row in _pr_rows_for_ref(pr_facts, ref):
            repo = ticket.repo
            if str(row.get("state") or "OPEN").upper() != "OPEN":
                continue
            branch = row.get("headRefName") or ""
            row_ref = ticket_ref_from_branch(repo, branch)
            if row_ref != ref or row.get("number") is None:
                continue
            verdict = _row_verdict(row, repo)
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


def reconcile_abandoned_claims(
    items: Sequence[Item], now: datetime,
    pr_facts: Optional[Dict[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Release heartbeat-bound claims that never produced a ticket branch.

    A slow ``begin`` can outlive the Codex exec tool's first output wait. If
    the caller walks away, that process may still claim and bind a ticket but
    no implementation ever reaches the deterministic remote branch. The bind
    identifies which open heartbeat start owns the claim; explicit branch
    absence after the normal 30-minute grace makes the abandoned work safe to
    release. Unknown branch state fails closed.

    The abandoned start is finished as ``errored`` as part of the same
    reconciliation. Otherwise the claim would recover while the watchdog kept
    reporting a start with no matching finish. Both writes are append-only or
    derived from GitHub state, and a later pass is idempotent because the
    Project claim is empty.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import heartbeat
    except Exception:
        return []

    by_ref = {item.ref: item for item in items}
    candidates: List[Tuple[str, Dict, str, Item]] = []
    for agent in sorted(heartbeat.PROVIDERS):
        if agent in heartbeat.RETIRED_AGENTS:
            continue
        try:
            records = heartbeat.read(agent)
        except Exception:
            continue
        bound = heartbeat.bindings(records)
        for start in heartbeat.open_starts(records):
            run = start.get("run")
            binding = bound.get(run)
            if not binding or binding.get("do") != "ticket":
                continue
            ref = str(binding.get("work"))
            item = by_ref.get(ref)
            if (
                item is None
                or item.state != "OPEN"
                or item.in_motion_since is None
                or now - item.in_motion_since < CLAIM_BRANCH_GRACE
            ):
                continue
            candidates.append((agent, start, ref, item))

    if not candidates:
        return []
    if pr_facts is None:
        try:
            pr_facts = ticket_pr_facts(
                [item for _, _, _, item in candidates]
            )
        except GitHubError:
            return []

    reconciled: List[Dict[str, object]] = []
    released: Set[str] = set()
    for agent, start, ref, item in candidates:
        if ref in released or _ticket_branch_exists(item, pr_facts) is not False:
            continue
        write_lock(item, "")
        item.in_motion_since = None
        released.add(ref)

        run = start.get("run")
        record = {
            "run": run,
            "agent": agent,
            "phase": "finish",
            "ts": int(now.timestamp()),
            "outcome": "errored",
            "note": (
                "reconciled: released {} after no ticket branch activity "
                "within 30 minutes"
            ).format(ref),
            "reconciled_claim": ref,
        }
        result: Dict[str, object] = {
            "run": run,
            "agent": agent,
            "ref": ref,
            "result": "released",
        }
        try:
            result["kept"] = heartbeat.append(agent, record)
        except Exception as exc:
            # Instrumentation cannot put the already-released GitHub claim
            # back. Report the missing finish explicitly in begin's JSON.
            result["finish_error"] = str(exc)
        reconciled.append(result)
    return reconciled


def reconcile_approved_merges(
    items: List[Item], now: datetime,
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Retry the merge gate for every approved current-head ticket PR.

    ``cmd_merge`` owns all merge conditions and the actual writes. This wrapper
    only finds the half-applied approval sequence, keeps its human-readable
    output on stderr so ``begin`` remains JSON, and records the result for the
    caller. A refusal is a normal reconciliation result, not a queue failure.
    """
    results: List[Dict[str, object]] = []
    by_ref = {item.ref: item for item in items}
    for candidate in approved_merge_candidates(items, pr_facts=pr_facts):
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                fact = None
                if pr_facts is not None:
                    for row in _pr_rows_for_ref(pr_facts, candidate["ref"]):
                        if row.get("number") == candidate["pr"]:
                            fact = row
                            break
                code = _call_with_optional_keywords(
                    cmd_merge,
                    items,
                    now,
                    candidate["repo"],
                    candidate["pr"],
                    True,
                    pr_fact=fact,
                    output_stream=sys.stderr,
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


def _start_begin_heartbeat(agent: str,
                           tier: Optional[str] = None) -> Optional[str]:
    """Start the run used by ``begin`` and remember it for error recovery.

    The tier goes on the start record so a reader can tell one lane's runs
    from another's; the Codex empty-run share is taken over the standard
    lane alone (#1320).
    """
    global _ACTIVE_HEARTBEAT_RUN, _ACTIVE_HEARTBEAT_AGENT

    command = [sys.executable,
               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "heartbeat.py"), "start", "--agent", agent]
    if tier in TIERS:
        command += ["--tier", tier]
    run = _run_bounded_subprocess(command, capture_output=True, text=True)
    run_id = ((run.stdout or "").strip().splitlines()[-1]
              if run.stdout else None)
    _ACTIVE_HEARTBEAT_RUN = run_id
    _ACTIVE_HEARTBEAT_AGENT = agent
    return run_id


def _finish_begin_budget_exhausted(
    agent: str, run: Optional[str], remaining: int, reset_at: str
) -> None:
    """Close a begin run with the structured exhausted-budget outcome.

    Heartbeat is instrumentation, so a failure to append this diagnostic must
    not replace the original begin error or change its exit status.
    """
    if not run:
        return
    note = "{} remaining={} resetAt={}".format(
        BEGIN_BUDGET_EXHAUSTED_OUTCOME, remaining, reset_at
    )
    command = [
        sys.executable,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "heartbeat.py"),
        "finish",
        "--agent", agent,
        "--run", str(run),
        "--outcome", BEGIN_BUDGET_EXHAUSTED_OUTCOME,
        "--note", note,
    ]
    try:
        proc = _run_bounded_subprocess(command, capture_output=True, text=True)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        print(
            "funnel: could not record budget-exhausted heartbeat: {}".format(
                detail
            ),
            file=sys.stderr,
        )
        return
    if getattr(proc, "returncode", 0) != 0:
        detail = (
            getattr(proc, "stderr", None)
            or getattr(proc, "stdout", None)
            or "heartbeat finish exited {}".format(proc.returncode)
        )
        print(
            "funnel: could not record budget-exhausted heartbeat: {}".format(
                str(detail).strip()
            ),
            file=sys.stderr,
        )


def implementation_packet(repo: str, number: int, agent: str) -> Dict:
    """Read one implementation packet through the standalone engine.

    ``engine.implement`` imports this module for the established GitHub reads,
    so importing it here would reverse the dependency. The process boundary
    keeps the package one-way while giving ``begin`` the same packet exposed by
    the public ``implement-packet`` entry point.
    """
    command = [
        sys.executable,
        str(CHECKOUT_ROOT / "implement-packet"),
        str(number),
        "--repo",
        repo,
        "--agent",
        agent,
    ]
    proc = _run_bounded_subprocess(
        command, capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()
        raise GitHubError("could not assemble implement packet: {}".format(detail))
    try:
        packet = json.loads(proc.stdout)
    except (TypeError, ValueError) as exc:
        raise GitHubError(
            "implement-packet returned invalid JSON: {}".format(exc)
        )
    if not isinstance(packet, dict):
        raise GitHubError("implement-packet returned a non-object JSON value")
    return packet


def begin_detail_candidates(
    items: Sequence[Item], breakdown: bool = False,
) -> List[Item]:
    """Return non-ticket begin candidates whose ordering needs history."""
    found: Dict[str, Item] = {}
    if breakdown:
        for item in items:
            if (
                item.state == "OPEN"
                and item.status == "Ready"
                and not item.children_total
                and not item.is_blocked
            ):
                found[item.ref] = item
    for item in items:
        if (
            item.state == "OPEN"
            and item.status == "Ideas"
            and "needs-shaping" in item.labels
        ):
            found[item.ref] = item
    return [item for item in items if item.ref in found]


def _local_time(now: datetime) -> datetime:
    """``now`` on this Mac's clock, the zone the weekly reset is kept in."""
    return now.astimezone()


def claude_window_refusal(local: datetime) -> Optional[str]:
    """Why a Claude run may not start now, or None inside the window."""
    hour, minute = CLAUDE_WINDOW_LAST_START
    if local.weekday() != CLAUDE_WINDOW_WEEKDAY:
        return ("Claude works Saturdays only, before {:02d}:{:02d} "
                "(#1557); it is {}".format(hour, minute,
                                           local.strftime("%A %H:%M")))
    if (local.hour, local.minute) >= (hour, minute):
        return ("Claude starts no work at or after {:02d}:{:02d} on "
                "Saturday, before the noon reset (#1557); it is {}".format(
                    hour, minute, local.strftime("%H:%M")))
    return None


def _begin_preflight(
    now: datetime, agent: str, idle: bool, tier: Optional[str] = None
) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
    """Start a run and apply the local gates before reading Project state.

    ``load_items`` is the expensive part of an otherwise empty poll.  Usage
    and idle are local facts, so a refusal must happen before the Project list
    query; ``#655`` measured the old baseline at 42 API calls and 47 GraphQL
    points.  The selected run keeps the exact same reading for later shaping
    decisions.  ``None`` for the reading means the preflight produced a stop
    envelope, not that a caller may guess at headroom.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import usage

    out: Dict[str, object] = {"agent": agent}
    out["run"] = _call_with_optional_keyword(
        _start_begin_heartbeat, "tier", tier, agent)

    if agent == "codex":
        # Before the usage read: a run on the wrong model or with a wider
        # sandbox must not reach anything, including the budget it would
        # spend (#1316).
        settings = _codex_settings_check()
        if not settings.get("ok"):
            why = settings.get("why") or "Codex run settings could not be checked"
            _record_begin_config_drift(agent, out["run"], why)
            out.update(gate="config", do="stop", why=why)
            return out, None
        out["effective"] = settings.get("effective")
        out["memory_reset"] = _codex_memory_reset(settings.get("automation"))
        automation = settings.get("automation")
        if isinstance(automation, str) and out.get("run"):
            job = pathlib.Path(automation).name
            if job:
                try:
                    import heartbeat

                    heartbeat.record_job(agent, str(out["run"]), job)
                except Exception:
                    # The identity record is diagnostic. Its absence can only
                    # leave a later event wait uncleared; it must not stop work.
                    pass

    if agent == "claude":
        # The Saturday lane runs until the clock or the provider's own limit
        # stops it. By Nate's direction it reads and estimates no budget
        # (2026-09-25, #1557), so the window is its only local gate.
        refusal = claude_window_refusal(_local_time(now))
        if refusal is not None:
            out.update(gate="time", do="stop", why=refusal)
            return out, None
        out.update(gate="ok", unmetered=True)
        return out, {"source": "claude", "captured_at": now.timestamp(),
                     "unmetered": True, "windows": {}}

    reading = usage.read_agent(agent, now.timestamp())
    if reading is None:
        out.update(gate="unknown", do="stop",
                   why="usage could not be read; a run that cannot read its "
                       "budget does not work")
        return out, None

    verdict = usage.pace(reading, now.timestamp(),
                         provider=usage.provider_of(agent))
    idle_verdict = usage.idle_verdict(agent, reading) if idle else None
    if verdict.get("over_pace") or (idle_verdict or {}).get("over"):
        out.update(gate="over", do="stop",
                   why=(idle_verdict or {}).get("why") or "over pace")
        return out, None

    out["gate"] = "ok"
    band = verdict.get("band")
    if band:
        out["budget_band"] = band
        out["budget"] = next(
            ({key: window.get(key) for key in (
                "used_percent", "projected_percent",
                "daily_rate_dollars", "runs_out_at")}
             for window in verdict.get("windows", ()) if window.get("band")),
            {},
        )
        if band == "tight":
            # #1269 (Nate, 2026-09-21): a tight budget stops every lane, the
            # same as `over`. The ladder decides only what goes first once the
            # projection falls back under the cap or the window resets. #1199
            # had read `tight` as an ordering rule and let Broken, Maintenance
            # and pinned work through; on a board that is mostly Broken that
            # was no brake at all, measured at $50 a day either side of it.
            out.update(gate="tight", do="stop",
                       why=_tight_budget_why(out, now))
            return out, None
    if reading.get("unmetered"):
        # Say so rather than letting ``gate: ok`` imply a budget was checked.
        # Preserve the generic future-provider exception explicitly rather than
        # implying that a successful budget reading was performed.
        out["unmetered"] = True
    return out, reading


def _begin_api_reserve_preflight(
    agent: str,
    tier: Optional[str],
    caller_role: Optional[str],
    response: Optional[Mapping[str, object]] = None,
) -> Optional[Dict[str, object]]:
    """Check headroom from the first real begin query's GraphQL response.

    ``member_repos`` is the first live read in a normal begin and its document
    already carries ``rateLimit``.  Reuse that response instead of spending a
    standalone probe before the funnel-state load.  A structured zero-budget
    response can carry both ``rateLimit`` and GraphQL errors; ``gh_graphql``
    preserves that block in ``graphql_spend()`` before it raises.
    """
    remaining = None
    if isinstance(response, Mapping):
        block = response.get("rateLimit")
        if isinstance(block, Mapping):
            remaining = block.get("remaining")
    if remaining is None:
        # The production GraphQL helper records the same block centrally. This
        # fallback also keeps the decision correct for partial/error responses.
        remaining = graphql_spend().get("remaining")

    lane = "engineering" if begin_uses_ticket_path(
        agent, tier, caller_role
    ) else "review"
    loads = (
        ENGINEERING_RESERVE_LOADS
        if lane == "engineering" else REVIEW_RESERVE_LOADS
    )
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        return {
            "gate": "reserve",
            "do": "stop",
            "why": "GraphQL budget could not be read; a run that cannot "
                   "read its budget does not work",
        }

    floor = _reserve_floor(loads, BEGIN_PROJECT_LOAD_COST)
    if remaining < floor:
        return {
            "gate": "reserve",
            "do": "stop",
            "why": "GraphQL budget {} is below the {} floor of {} "
                   "({} loads at {} points; ceiling {} points)".format(
                       remaining, lane, floor, loads,
                       BEGIN_PROJECT_LOAD_COST,
                       GRAPHQL_RESERVE_POINT_CEILING),
        }
    return None


def _record_queue_empty(agent: str, run: Optional[str],
                        tier: Optional[str]) -> None:
    """Record, from ``begin`` itself, that this run found no work waiting.

    The routine files every stop that is not a named budget gate as
    ``nothing-to-do``: a GitHub failure, the WIP cap and a held lock
    included. #1216 measured 160 such finishes during a 504 outage, the same
    picture an empty funnel makes. This event is written only on the branch
    where the queue for the tier was genuinely empty, so readers that need
    "was there work?" never have to trust the finish (#1320).
    """
    if not run:
        return
    try:
        import heartbeat

        heartbeat.record_event(agent, run, "nothing-to-do", queue="empty",
                               tier=tier)
    except Exception:
        # Instrumentation must not gate the thing it instruments.
        pass


def _codex_settings_check() -> Dict[str, object]:
    """Whether this Codex run is the one ``codex_run.py`` describes.

    One seam, so the suite's shared fixture can stand in for a machine's
    real rollouts; the check itself is tested against fixture rollouts in
    ``tests/test_codex_run.py``. An exception inside the check is a refusal,
    not a pass: it fails closed like everything else it guards.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import codex_run

        return codex_run.check()
    except Exception as exc:  # pragma: no cover - defensive; see docstring
        return {"ok": False,
                "why": "Codex run settings could not be checked: {}".format(
                    str(exc) or type(exc).__name__)}


def _codex_memory_reset(directory: object) -> str:
    """Reset the launching automation's memory file; say what happened.

    The app tells every automation run to read that file first and to write
    a summary into it before returning, so model-written notes steered
    nearly every later run (#1317). A failure is reported in the envelope
    and never stops the run: the settings check has already passed, and
    memory is not a safety boundary.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import codex_run

        return codex_run.reset_memory(
            directory if isinstance(directory, str) else None)
    except Exception as exc:
        return "failed: {}".format(str(exc) or type(exc).__name__)


def _record_begin_config_drift(agent: str, run: Optional[str],
                               why: object) -> None:
    """Record a settings refusal the way the reserve gate records its own.

    An event, not a finish: the routine owns the terminal finish for a
    stopped ``begin`` and maps gate ``config`` to ``config-drift``, and a
    second finish would be refused. The event makes the refusal
    machine-readable even if a stale copy of the routine files the finish
    under another outcome. The name is deliberately not ``skipped-*``: the
    watchdog treats those as the system working, and a drifted run is not.
    """
    import heartbeat

    note = str(why)
    try:
        heartbeat.record_event(agent, run, "config-drift", note=note)
    except Exception as exc:  # instrumentation must not unblock the gate
        print("funnel: could not record config-drift: {}".format(
            str(exc) or type(exc).__name__), file=sys.stderr)
    print("funnel: config-drift: {}".format(note), file=sys.stderr)


def _record_begin_reserve(agent: str, run: Optional[str], why: object) -> None:
    """Record and log a clean reserve stand-down without closing the run.

    The wrapper owns the terminal heartbeat finish for a stopped begin.  The
    event is still recorded here so a direct or session-backed begin leaves a
    durable, machine-readable reason, and the stderr line stays visibly
    distinct from a begin fault.
    """
    import heartbeat

    note = str(why)
    heartbeat.record_event(agent, run, "skipped-api-reserve", note=note)
    print("funnel: skipped-api-reserve: {}".format(note), file=sys.stderr)


def _tight_budget_why(out: Mapping[str, object], now: datetime) -> str:
    """One line a person can act on: the numbers, and what still runs."""
    budget = out.get("budget") or {}
    parts = []
    if budget.get("used_percent") is not None:
        parts.append("{:g}% used".format(budget["used_percent"]))
    if budget.get("projected_percent") is not None:
        rate = budget.get("daily_rate_dollars")
        parts.append("projected {:g}%{}".format(
            budget["projected_percent"],
            " at ${:g}/day".format(round(float(rate), 2)) if rate else ""))
    runs_out_at = budget.get("runs_out_at")
    if runs_out_at:
        parts.append("runs out {}".format(
            datetime.fromtimestamp(float(runs_out_at), timezone.utc)
            .strftime("%Y-%m-%d %H:%MZ")))
    return (
        "tight: {}; every lane waits until the rate falls or the window "
        "resets, then the ladder decides what goes first".format(
            ", ".join(parts) or "budget")
    )


def _queue_candidate(entries: Sequence[object],
                     class_of: Callable[[object], Optional[str]]):
    """The entry a queue puts forward for cross-stage ranking, or ``None``.

    Each queue keeps its own order, oldest first, and cross-stage ranking
    reads one entry from each. The first entry in a preempting class is the
    one to put forward when there is one, because it is the entry the ranking
    key favours; with none, the head stands for the queue as before. Order
    within a class group is unchanged either way.
    """
    for entry in entries:
        if class_of(entry) in PREEMPTING_CLASSES:
            return entry
    return entries[0] if entries else None


def cmd_begin(items: List[Item], now: datetime, agent: str, tier: Optional[str],
              idle: bool, breakdown: bool = False,
              repo_readiness: Optional[
              Mapping[str, MemberRepoReadiness]
              ] = None,
              caller_role: Optional[str] = None,
              _detail_loader: Optional[Callable[[Sequence[Item]], None]] = None,
              _preflight: Optional[
                  Tuple[Dict[str, object], Optional[Dict[str, object]]]
              ] = None,
              timings: Optional[Dict[str, object]] = None,
              _pr_facts: Optional[
                  Mapping[str, Optional[Dict[str, object]]]
              ] = None,
              _pr_facts_error: Optional[GitHubError] = None,
              _pr_facts_elapsed: Optional[float] = None,
              _phase_started: Optional[float] = None,
              ) -> int:
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

    phase_started = (
        _phase_started if _phase_started is not None else time.perf_counter()
    )
    if _preflight is None:
        _preflight = _begin_preflight(now, agent, idle, tier)
    out, reading = _preflight
    if timings is not None:
        out["timings"] = timings
    if reading is None:
        print(json.dumps(out, indent=2))
        return 0
    role_refusal = _begin_role_refusal(agent, tier, caller_role)
    if role_refusal is not None:
        out.update(role_refusal)
        print(json.dumps(out, indent=2))
        return 0

    ticket_path = begin_uses_ticket_path(agent, tier, caller_role)

    def review_phase_boundary(phase: str) -> None:
        if not ticket_path:
            _report_begin_phase_boundary(phase, phase_started)

    review_phase_boundary("detail_hydration")
    if _detail_loader is not None and not ticket_path:
        candidates = begin_detail_candidates(items, breakdown)
        if candidates:
            _detail_loader(candidates)
    # Reconcile first, and never fatally. A step that cannot reach GitHub
    # records its failure and selection proceeds without it; only a failure
    # in selection's own reads stops the run (#732, #823).
    reconcile_errors: List[Dict[str, object]] = []

    def attempt_reconcile(step, func, *args):
        try:
            return func(*args)
        except GitHubError as exc:
            text, transient = _github_error_text(exc)
            reconcile_errors.append({
                "step": step,
                "error": text,
                "transient": transient,
            })
            return []

    # One PR snapshot feeds merge reconciliation and whichever queue follows.
    # Keeping it here prevents the approved-merge pass, engineer hand-back, and
    # reviewer queue from each paying for the same repository fan-out.
    try:
        if not ticket_path:
            review_phase_boundary("ticket_pr_facts")
        if _pr_facts_error is not None:
            raise _pr_facts_error
        if _pr_facts is not None:
            pr_facts = _pr_facts
        else:
            pr_facts = _begin_load_timed(
                timings, "ticket_pr_facts", lambda: ticket_pr_facts(items)
            )
    except GitHubError as exc:
        out.update(
            do="stop",
            gate="error",
            why="could not establish ticket branch facts: {}".format(exc),
        )
        print(json.dumps(out, indent=2))
        return 0
    finally:
        if _pr_facts_elapsed is not None:
            _record_begin_load_phase(
                timings, "ticket_pr_facts", _pr_facts_elapsed
            )

    review_phase_boundary("reconcile_approved_merges")
    reconciled_merges = attempt_reconcile(
        "approved_merges", reconcile_approved_merges, items, now, pr_facts)
    if reconciled_merges:
        out["reconciled_merges"] = reconciled_merges

    review_phase_boundary("reconcile_auto_closeable_projects")
    auto_closed = attempt_reconcile(
        "auto_closeable_projects", reconcile_auto_closeable_projects, items)
    if auto_closed:
        out["auto_closed"] = auto_closed

    review_phase_boundary("reconcile_closed_items")
    reconciled_statuses = attempt_reconcile(
        "closed_items", reconcile_closed_items, items)
    if reconciled_statuses:
        out["reconciled_statuses"] = reconciled_statuses
    review_phase_boundary("reconcile_parked_wakes")
    woke_parked = attempt_reconcile(
        "parked_wakes", reconcile_parked_wakes, items, now)
    if woke_parked:
        out["woke_parked"] = woke_parked
    review_phase_boundary("reconcile_closed_claims")
    released_claims = attempt_reconcile(
        "closed_claims", reconcile_closed_claims, items)
    if released_claims:
        out["released_claims"] = released_claims
    review_phase_boundary("reconcile_orphaned_starts")
    orphaned = attempt_reconcile(
        "orphaned_starts", reconcile_orphaned_starts, items, now)
    if orphaned:
        out["reconciled_starts"] = orphaned
    if reconcile_errors:
        out["reconcile_errors"] = reconcile_errors

    if begin_uses_ticket_path(agent, tier, caller_role):
        cleared = clear_satisfied_blocks(
            items, now, run=out.get("run"), agent=agent
        )
        if cleared:
            out["cleared_blocks"] = cleared
        abandoned = attempt_reconcile(
            "abandoned_claims", reconcile_abandoned_claims,
            items, now, pr_facts,
        )
        if abandoned:
            out["reconciled_claims"] = abandoned
        blocked = _call_with_optional_keyword(
            awaiting_review, "pr_facts", pr_facts, items
        )
        # Keep the normal open-PR exclusion as the default. Only the
        # machine-readable approved-plus-conflicting state hands ownership back
        # to the engineer; the supplied PR snapshot is also the one used by the
        # claim/WIP checks below.
        blocked.difference_update(approved_conflicting_refs(pr_facts))
        blocked.update(finished_by_comments(items))
        # A merge attempt that errored may still have merged remotely, so the
        # ticket stays open locally with an unknown remote state (#732). It is
        # not startable until the next run re-reads GitHub; the recorded error
        # above says why it was passed over.
        blocked.update(
            str(entry["ref"]) for entry in reconciled_merges
            if entry.get("result") == "error" and entry.get("ref")
        )
        begin_backed_off = _backed_off_work(items, now)
        ticket = next_ticket_for_tier(
            items, now, tier=tier, blocked=blocked,
            agent=agent,
            repo_readiness=repo_readiness,
            pr_facts=pr_facts,
            backed_off=begin_backed_off,
        )
        recently_claimed: Set[str] = set()
        while ticket is not None:
            try:
                current_claim = read_lock(ticket)
            except GitHubError as exc:
                out.update(
                    do="stop",
                    gate="error",
                    why="could not re-read {} claim: {}".format(ticket.ref, exc),
                )
                ticket = None
                break
            if (
                current_claim is None
                or now - current_claim >= BEGIN_CLAIM_COLLISION_WINDOW
            ):
                break

            # This process has not claimed its selected ticket yet, so a new
            # value in the live Project view belongs to another begin. Keep the
            # local view honest for the WIP check and pass over it without
            # changing the shared ordering.
            ticket.in_motion_since = current_claim
            recently_claimed.add(ticket.ref)
            ticket = next_ticket_for_tier(
                items, now, tier=tier, blocked=blocked,
                excluded=recently_claimed,
                agent=agent,
                repo_readiness=repo_readiness,
                pr_facts=pr_facts,
                backed_off=begin_backed_off,
            )
        held = held_claims_before(
            items,
            now,
            ticket,
            blocked=blocked,
            agent=agent,
            repo_readiness=repo_readiness,
            pr_facts=pr_facts,
        )
        if held:
            out["held"] = held
        if ticket is None and begin_backed_off:
            # Never a silent hold: an empty poll that is really a backoff says
            # so, with the count and the condition that releases it.
            withheld_rows = [
                row for row in begin_backed_off.values()
                if row["ref"] in {i.ref for i in items if i.state == "OPEN"}
            ]
            if withheld_rows:
                out["backed_off"] = [
                    {"ref": row["ref"], "failures": row["failures"],
                     "until": row["until"].isoformat(),
                     "reason": row["reason"]}
                    for row in sorted(withheld_rows,
                                      key=lambda row: str(row["ref"]))
                ]
        if ticket is None:
            holder = lock_holder(items, now, pr_facts=pr_facts)
            withheld = readiness_blockers(
                items, repo_readiness=repo_readiness, awaiting_review=blocked
            )
            frozen = freeze_withheld(
                items, repo_readiness=repo_readiness, awaiting_review=blocked,
                agent=agent,
            )
            if withheld:
                out["withheld"] = withheld
            if frozen:
                out["freeze_withheld"] = frozen
            # Empty means nothing can start because the work is done or is
            # waiting on someone else. The lane's own brakes holding work it
            # has not implemented, the WIP cap, a backoff, readiness, the
            # freeze or an earlier stop, are not an empty queue (#1320).
            # A claim in motion alone is not a brake below the cap.
            queue_empty = not (
                out.get("why") or out.get("backed_off") or withheld
                or frozen or at_capacity(items, now, pr_facts=pr_facts))
            if out.get("why"):
                why = str(out["why"])
            elif holder is not None:
                why = "nothing — lock held by {} (claimed {} ago)".format(
                    holder.ref, humanise(now - holder.in_motion_since)
                )
            elif withheld:
                why = "nothing — {}".format(
                    _readiness_blocker_summary(withheld)
                )
            elif frozen:
                why = "nothing — {}".format(
                    _freeze_withheld_summary(frozen)
                )
            elif tier:
                why = "nothing — no {} work waiting".format(tier)
            else:
                why = "nothing to do"
            out.update(do="stop", why=why)
            if queue_empty:
                out["queue"] = "empty"
                _record_queue_empty(agent, out.get("run"), tier)
        else:
            if _detail_loader is not None:
                _detail_loader([ticket])
            refusal = claim_ticket(items, now, ticket, pr_facts=pr_facts)
            if refusal is not None:
                out.update(do="stop", why=refusal)
            else:
                out.update(
                    do="ticket",
                    work=item_json(ticket, now, {i.ref: i for i in items}),
                )
                if agent in IMPLEMENT_VENDORS:
                    # Bind immediately after the claim, before the packet's
                    # slower ticket/plan/verdict reads. A process abandoned
                    # during that load is then attributable and recoverable by
                    # the next begin's reconciliation.
                    _bind_run(agent, out)
                    try:
                        out["packet"] = implementation_packet(
                            ticket.repo, ticket.number, agent
                        )
                    except (GitHubError, OSError, subprocess.SubprocessError) as exc:
                        # A packet-less implementation run is not actionable.
                        # Undo the claim before returning a stop envelope so the
                        # next poll can recover without waiting for the TTL.
                        try:
                            write_lock(ticket, None)
                        except GitHubError as release_exc:
                            out["release_error"] = str(release_exc)
                        out.pop("work", None)
                        out.update(
                            do="stop",
                            gate="error",
                            why="could not assemble implementation packet: {}".format(
                                exc
                            ),
                        )
                    else:
                        out["vendor"] = IMPLEMENT_VENDORS[agent]
        if "bound" not in out:
            _bind_run(agent, out)
        print(json.dumps(out, indent=2))
        return 0

    review_phase_boundary("review_queue")
    queue = _call_with_optional_keywords(
        review_queue, items, tier, pr_facts=pr_facts,
        output_stream=sys.stderr,
    )
    # The fixed job order remains the tiebreak within a class group, but a
    # finite preempting class can cross stages. Build one candidate for each
    # queue: the next run gets the next item if this run preempts it.
    #
    # The candidate is the first preempting entry, not the head (#1222). The
    # review queue and `awaiting_breakdown` are oldest first, so offering the
    # head let its class speak for everything behind it: on 2026-09-21 one old
    # New-class PR ranked below every Broken breakdown and shape, and seven
    # green Broken reviews waited behind it for five hours. `ideas()` already
    # puts Broken first, so the shape queue needs no such pick.
    by_ref = {item.ref: item for item in items}

    def review_class_of(entry):
        entry_item = by_ref.get(entry.get("ref"))
        if entry_item is None:
            return None
        return effective_class(entry_item, by_ref)

    # Finish plans that already qualify for unattended approval before the
    # shape queue looks for another Idea. Updating the shared Items first also
    # lets the Ready plan enter this pass's breakdown queue.
    review_phase_boundary("self_approvals")
    self_approved, self_approval_errors = sweep_shaped_self_approvals(
        items, now, run=out.get("run"), agent=agent
    )
    if self_approved:
        out["shaped_self_approvals"] = self_approved
    if self_approval_errors:
        out["shaped_self_approval_errors"] = self_approval_errors

    # The repeated-failure backoff covers breakdown and shape jobs as well as
    # tickets (#1581): the review lane never read it, so #1195's shape was
    # retried on every escalated fire through eleven straight failures. The
    # binding's work for these jobs is the issue ref, so the same heartbeat
    # count applies unchanged. The heartbeat read costs about two seconds of
    # a reply budget #1591 is already short of, so it runs only when there is
    # an issue job to filter.
    review_phase_boundary("breakdown_queue")
    pending = awaiting_breakdown(items) if breakdown else []
    review_backed_off: Dict[str, Dict[str, object]] = {}
    if pending or any(
        "needs-shaping" in getattr(entry, "labels", ()) for entry in items
    ):
        review_backed_off = _backed_off_work(items, now)
    pending = [entry for entry in pending
               if entry.ref not in review_backed_off]
    review_phase_boundary("shape_queue")
    shape_item = shapeable_idea(
        [entry for entry in items if entry.ref not in review_backed_off],
        tier, reading,
    )
    withheld_issue_jobs = [
        row for row in review_backed_off.values()
        if row["ref"] in {
            entry.ref for entry in items
            if entry.state == "OPEN"
            and (
                "needs-shaping" in getattr(entry, "labels", ())
                or entry.status == "Ready"
            )
        }
    ]
    if withheld_issue_jobs:
        # Never a silent hold, as on the ticket path.
        out["backed_off"] = [
            {"ref": row["ref"], "failures": row["failures"],
             "until": row["until"].isoformat(),
             "reason": row["reason"]}
            for row in sorted(withheld_issue_jobs,
                              key=lambda row: str(row["ref"]))
        ]
    review = _queue_candidate(queue, review_class_of)
    breakdown_item = _queue_candidate(
        pending, lambda entry: getattr(entry, "klass", None))
    candidates: List[Tuple[int, int, str, object]] = []

    review_phase_boundary("candidate_selection")
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
        if tier == "escalated" and review is not None:
            # #1135: the escalated schedule shapes escalated ideas while the
            # Claude routine is off, but review still comes first — a Broken
            # idea never preempts a waiting review there.
            job, payload = "review", review
        else:
            _, _, job, payload = min(
                candidates, key=lambda candidate: candidate[:2]
            )
        if job == "review":
            out.update(do="review", work=payload)
        elif job == "breakdown":
            item = payload
            work = {
                "ref": item.ref,
                "url": item.url,
                "title": item.title,
                "access_signals": access_signals(
                    _loaded_item_body(item)
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

    review_phase_boundary("reserve_gate")
    reserve = _reserve_verdict(out.get("do"))
    if reserve is not None:
        out.update(reserve)
        _record_begin_reserve(agent, out.get("run"), out["why"])
    review_phase_boundary("run_binding")
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
    repo = work.get("repo") if do == "review" else None
    out["bound"] = {"do": do, "work": str(subject)}
    if repo:
        out["bound"]["repo"] = str(repo)
    try:
        import heartbeat

        heartbeat.record_binding(agent, str(run), str(do), str(subject),
                                 repo=str(repo) if repo else None)
    except Exception:
        # Instrumentation must not gate the thing it instruments.
        pass


def _github_error_text(error: GitHubError) -> Tuple[str, bool]:
    """Human-readable text plus the transient flag for a GitHub failure.

    Shared by the fatal ``begin`` envelope and the non-fatal reconcile
    records: both name a transient GraphQL response and both carry the
    request id that identifies it, so one shape covers either outcome.
    """
    transient = bool(getattr(error, "transient", False))
    request_id = getattr(error, "request_id", None)
    if not request_id:
        request_id = _graphql_request_id(str(error))
    text = str(error).strip() or type(error).__name__
    if transient:
        text = "transient GraphQL response: {}".format(text)
    if request_id and request_id not in text:
        text += " (GraphQL request ID {})".format(request_id)
    return text, transient


def _begin_error_envelope(agent: str, error: GitHubError) -> None:
    """Print a parseable failed ``begin`` result and preserve its heartbeat."""
    global _ACTIVE_HEARTBEAT_RUN, _ACTIVE_HEARTBEAT_AGENT

    run = _ACTIVE_HEARTBEAT_RUN
    start_error = None
    if not run:
        try:
            run = _start_begin_heartbeat(agent)
        except Exception as exc:
            # The error envelope is the last useful contract if heartbeat
            # startup itself is unavailable. Keep the original GitHub failure
            # visible and say why no run id could be attached.
            start_error = str(exc).strip() or type(exc).__name__
            _ACTIVE_HEARTBEAT_AGENT = agent

    why, transient = _github_error_text(error)
    if start_error:
        why += "; heartbeat start failed: {}".format(start_error)

    signal = _budget_exhaustion_signal()
    if signal is not None:
        _finish_begin_budget_exhausted(agent, run, *signal)

    print(json.dumps({
        "agent": agent,
        "run": run,
        # No budget or queue gate completed. `unknown` preserves the existing
        # begin schema while the explicit flag distinguishes this from an
        # unreadable usage record.
        "gate": "unknown",
        "do": "stop",
        "why": why,
        "transient": transient,
    }, indent=2))


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

    if not spend.get("calls"):
        # No GraphQL call was made, so there is no budget question to answer —
        # distinct from a call whose rate-limit block was unreadable. In a real
        # CLI begin, the rate-limit preflight asks before `load_items`; this can
        # still happen for a direct or embedded `cmd_begin` caller. Failing
        # closed here would refuse on the absence of a question rather than on
        # the absence of an answer.
        return None
    return _reserve_verdict_for_remaining(
        do, spend.get("remaining"), spend.get("cost") or 0
    )


def _reserve_verdict_for_remaining(
    do: object, remaining: object, load_cost: object
) -> Optional[Dict[str, object]]:
    """Apply a reserve floor to one already-read remaining-points value."""
    if do not in ("review", "breakdown", "shape"):
        return None
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        # A call was made and its block could not be read. Fail closed,
        # matching the unreadable-usage branch above.
        return {
            "gate": "reserve",
            "do": "stop",
            "why": "GraphQL budget could not be read; a run that cannot "
                   "read its budget does not work",
        }

    loads = (REVIEW_RESERVE_LOADS if do == "review"
             else ENGINEERING_RESERVE_LOADS)
    floor = _reserve_floor(loads, load_cost)
    if remaining < floor:
        return {
            "gate": "reserve",
            "do": "stop",
            "why": "GraphQL budget {} is below the {} floor of {} "
                   "({} loads at {} points; ceiling {} points)".format(
                       remaining, "review" if do == "review"
                       else "engineering", floor, loads, load_cost,
                       GRAPHQL_RESERVE_POINT_CEILING),
        }
    return None


def cmd_next_review(
    items: List[Item], tier: Optional[str],
    pr_facts: Optional[Mapping[str, Optional[Dict[str, object]]]] = None,
) -> int:
    """The single PR this reviewer should read, or nothing."""
    if pr_facts is None:
        pr_facts = ticket_pr_facts(items)
        cache = _ACTIVE_BRIEF_CACHE.get()
        if cache is not None:
            # A session may follow this read with ``brief``. Retain the fresh
            # snapshot so that later diagnostic sections reuse the same query,
            # while the session's normal non-brief invalidation still clears
            # observations from an earlier command before this one starts.
            cache._pr_facts = pr_facts
    queue = _call_with_optional_keyword(
        review_queue, "pr_facts", pr_facts, items, tier
    )
    if not queue:
        print("nothing — no {}review waiting".format(
            (tier + " ") if tier else ""), file=sys.stderr)
        return 1
    print(json.dumps(queue[0], indent=2))
    return 0


def cmd_review(repo: Optional[str], pr: int, verdict: str, ci: str,
               blocking: List[str], note: Optional[str],
               run: Optional[str] = None, agent: Optional[str] = None,
               items: Optional[Sequence[Item]] = None) -> int:
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

    if verdict == "rejected" and ci == "unknown":
        fact = _pr_fact_for_number(repo, pr, include_comments=True) or {}
        if (
            fact.get("state") == "OPEN"
            and fact.get("headRefOid") == sha
            and _conflicting_branch_blocker(fact) is not None
        ):
            _record_unmergeable_rejection(
                repo,
                pr,
                pr_fact=fact,
                candidate_verdict={
                    "verdict": verdict,
                    "ci": ci,
                    "head_sha": sha,
                },
                items=items,
            )
            return 0

    return _write_verdict(
        repo, pr, sha, verdict, ci, blocking, note, run=run, agent=agent
    )


def _write_verdict(repo: str, pr: int, sha: str, verdict: str, ci: str,
                   blocking: List[str], note: Optional[str],
                   run: Optional[str] = None,
                   agent: Optional[str] = None,
                   output_stream: Optional[IO[str]] = None) -> int:
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
        verdict, pr, sha[:12], repo),
        file=output_stream if output_stream is not None else sys.stdout)
    return 0


def _conflicting_branch_blocker(
    data: Mapping[str, object],
) -> Optional[str]:
    """The mechanical conflict reason, or None for every other branch state."""
    mergeable = str(data.get("mergeable") or "").upper()
    merge_state = str(
        data.get("mergeStateStatus") or data.get("merge_state_status") or ""
    ).upper()
    if mergeable != "CONFLICTING" and merge_state != "DIRTY":
        return None
    return "branch {!r}{}".format(
        data.get("headRefName") or "", CONFLICTING_BRANCH_SUFFIX
    )


def _is_conflicting_branch_blocker(reason: str) -> bool:
    return reason.startswith("branch ") and reason.endswith(
        CONFLICTING_BRANCH_SUFFIX
    )


def _record_unmergeable_rejection(
    repo: str, pr: int, pr_fact: Optional[Mapping[str, object]] = None,
    *, candidate_verdict: Optional[Mapping[str, object]] = None,
    items: Optional[Sequence[Item]] = None,
    output_stream: Optional[IO[str]] = None,
) -> None:
    """Record a deterministic rejection for a conflicting current head.

    The merge gate and review queue use this path when the conflict itself is
    the complete mechanical answer, replacing a less-specific verdict or
    recording one before review. The optional incoming verdict lets
    ``cmd_review`` supply the current head before it writes a weaker rejection.
    In every case the current head and conflict state come from the same PR
    fact used to write the canonical blocker.
    """
    data = dict(pr_fact) if isinstance(pr_fact, Mapping) else None
    if data is None:
        data = _pr_fact_for_number(repo, pr, include_comments=True) or {}
    if data.get("state") != "OPEN":
        return
    sha = data.get("headRefOid")
    reason = _conflicting_branch_blocker(data)
    if not sha or reason is None:
        return

    current = _row_verdict(data, repo)
    verdict = (
        dict(candidate_verdict)
        if isinstance(candidate_verdict, Mapping) else current
    )
    if not isinstance(verdict, dict) or verdict.get("head_sha") != sha:
        return
    eligible = verdict.get("verdict") == "approved" or (
        verdict.get("verdict") == "rejected"
        and verdict.get("ci") == "unknown"
    )
    if not eligible:
        return

    already_canonical = (
        isinstance(current, dict)
        and current.get("verdict") == "rejected"
        and current.get("ci") == "unknown"
        and current.get("head_sha") == sha
        and current.get("blocking") == [reason]
    )
    if not already_canonical:
        _call_with_optional_keywords(
            _write_verdict,
            repo, pr, sha, "rejected", "unknown", [reason], None,
            agent=MERGE_GATE_AGENT,
            output_stream=output_stream,
        )

    # Hand the ticket back when this head is rejected for its conflict. If a
    # canonical rejection was written by an earlier run, clear only the claim
    # that predates it; a newer claim belongs to the engineer rebasing the PR.
    if items is not None:
        ref = ticket_ref_from_branch(repo, str(data.get("headRefName") or ""))
        ticket = next((item for item in items if item.ref == ref), None)
        if ticket is not None and ticket.in_motion_since is not None:
            prior_rejection = None
            if already_canonical:
                stamp = current.get("reviewed_at") if isinstance(current, dict) else None
                if isinstance(stamp, str):
                    try:
                        prior_rejection = datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass
                    if prior_rejection is not None and prior_rejection.tzinfo is None:
                        prior_rejection = prior_rejection.replace(tzinfo=timezone.utc)
                # Lock timestamps have one-second precision. Treat a claim in
                # the same second as the rejection as newer, since its order is
                # ambiguous and clearing it could interrupt a rebase.
                if (
                    prior_rejection is None
                    or ticket.in_motion_since >= prior_rejection.replace(microsecond=0)
                ):
                    return
            write_lock(ticket, "")
            ticket.in_motion_since = None


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
    """Whether an item has earned the funnel's unattended close.

    ``funnel merge`` sees the Project summary before GitHub closes the ticket,
    so it supplies the post-merge child count. Every other caller uses the
    count already loaded on the item.
    """
    completed = item.children_done if children_done is None else children_done
    return (
        item.state == "OPEN"
        and item.status == "Building"
        and _could_carry_closed_itself_marker(
            item, children_done=completed
        )
    )


def _close_auto_closeable_project(items: Sequence[Item], project: Item,
                                  *, children_done: Optional[int] = None
                                  ) -> bool:
    """Move one eligible item to Done, close it, and record its marker."""
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
    """Close every eligible item with finished children before queue selection."""
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


def reconcile_closed_claims(items: Sequence[Item]) -> List[str]:
    """Release claims left on tickets which GitHub already says are closed."""
    released: List[str] = []
    candidates = sorted(
        (
            item for item in items
            if item.state == "CLOSED" and item.in_motion_since is not None
        ),
        key=lambda item: (item.repo, item.number),
    )
    for item in candidates:
        write_lock(item, "")
        item.in_motion_since = None
        released.append(item.ref)
    return released


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

    The scan is one batched GraphQL request across member repositories,
    independent of the number of tickets. ``pageInfo`` reports truncation, so
    the pure detector receives the intersected ticket refs rather than querying
    GitHub itself.
    """
    open_ticket_refs = {
        item.ref for item in items
        if item.state == "OPEN" and item.parent
    }
    repos = sorted({item.repo for item in items if item.ref in open_ticket_refs})
    merged_ticket_refs: Set[str] = set()
    truncated = False

    snapshot = _read_batched_pr_snapshots(
        repos,
        states=("MERGED",),
        limit=MERGED_PR_SCAN_LIMIT,
        include_comments=False,
        include_reviews=False,
        include_closing_refs=False,
        include_refs=False,
    )
    for repo in repos:
        truncated = truncated or bool(
            snapshot.pr_truncated_by_repo.get(repo)
        )
        for row in snapshot.rows_by_repo.get(repo, ()):
            ref = ticket_ref_from_branch(repo, row.get("headRefName") or "")
            if ref in open_ticket_refs:
                merged_ticket_refs.add(ref)

    return MergedPRFacts(frozenset(merged_ticket_refs), truncated)


def merge_blockers(
    repo: str, pr: int, items: List[Item], now: datetime,
    pr_fact: Optional[Mapping[str, object]] = None,
) -> List[str]:
    """Every reason this PR may not be merged. Empty means it may.

    Deliberately a list rather than a bool: a gate that says only "no" makes the
    caller guess, and the reviewer needs to know which condition to fix.
    """
    why: List[str] = []
    data = dict(pr_fact) if isinstance(pr_fact, Mapping) else None
    if data is None:
        data = _pr_fact_for_number(repo, pr, include_comments=True) or {}
    if not data:
        return ["PR #{} could not be read".format(pr)]

    # The stop is read here, from the same loaded items as the binding check,
    # so merging never waits on a reporting run. Until #801 the review
    # routines ran the full 35-section reporting read before every merge just
    # to read this counter, and a slow read blocked the merge by timing out
    # (#830). The gate is fail-closed: a tripped counter refuses.
    counter = rejected_merges(items, now)
    if counter["stop_auto_merging"]:
        why.append("auto-merging is stopped: {} rejected merges in the last "
                   "{} days ({})".format(
                       counter["count"], counter["window_days"],
                       ", ".join(str(r) for r in counter["refs"])))

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
            if not ticket.parent:
                # A parent-less branch item is its own parent: the project
                # itself must be Building (#1042).
                if ticket.status != "Building":
                    why.append(
                        "{} names a project with no parent, and the "
                        "project itself is not Building — move the "
                        "project to Building, or move the fix to a "
                        "ticket/<n> branch under a filed "
                        "ticket".format(ref))
            else:
                parent = next(
                    (i for i in items if i.ref == ticket.parent), None)
                if parent is None or parent.status != "Building":
                    why.append("{}'s project is not Building".format(ref))

    checks = [check for check in (data.get("statusCheckRollup") or [])
              if isinstance(check, Mapping)]
    ci_state = ci_rollup_state(checks)
    if ci_state == CI_COULD_NOT_RUN:
        why.append(
            "CI could not run: {}".format(
                ci_could_not_run_reason(checks)
                or "startup or account failure"
            )
        )
    else:
        failed = [c.get("name") or c.get("context") for c in checks
                  if (c.get("conclusion") or c.get("state")) not in
                  CI_SUCCESS_CONCLUSIONS + (None,)]
        if failed:
            why.append("CI not green: " + ", ".join(str(f) for f in failed))
        elif not checks:
            why.append("no CI checks reported — refusing to merge unverified work")

    verdict = _row_verdict(data, repo)
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


def cmd_merge(
    items: List[Item], now: datetime, repo: Optional[str], pr: int,
    confirmed: bool, pr_fact: Optional[Mapping[str, object]] = None,
    *, output_stream: Optional[IO[str]] = None,
) -> int:
    """Merge a PR, but only when every condition holds.

    The model decides *approval*; this decides *merge*. A model adds value
    judging whether a diff matches the plan. It adds none by being the component
    that types `gh pr merge`, and being that component is what makes an
    unattended merge impossible to audit — which is why v0 is still unaccepted.
    """
    repo = resolve_repo(repo)
    supports_fact = _accepts_keyword(merge_blockers, "pr_fact")
    if pr_fact is None and supports_fact:
        pr_fact = _pr_fact_for_number(repo, pr, include_comments=True)
    gate_fact = pr_fact if pr_fact is not None else {}
    why = _call_with_optional_keyword(
        merge_blockers, "pr_fact", gate_fact, repo, pr, items, now
    )
    if why:
        if any(_is_conflicting_branch_blocker(reason) for reason in why):
            _record_unmergeable_rejection(
                repo, pr, pr_fact=gate_fact, items=items,
                output_stream=output_stream,
            )
        print("refusing to merge PR #{}:".format(pr), file=sys.stderr)
        for reason in why:
            print("  - " + reason, file=sys.stderr)
        return 1

    if not confirmed:
        print("PR #{} in {} passes every merge condition.".format(pr, repo))
        print("Nothing was changed. Re-run with --yes to merge.")
        return 0

    # The batch read already carries the branch and title. Reading them again
    # immediately before the merge recreated the per-PR fan-out and could also
    # make the gate reason differ from the merge subject. A legacy injected
    # merge-blocker seam has no batch fact, so retain its old test/library path.
    if supports_fact:
        view = dict(pr_fact) if isinstance(pr_fact, Mapping) else {}
    else:
        view = _gh_json("gh", "pr", "view", str(pr), "--repo", repo,
                        "--json", "headRefName,title") or {}
    branch = view.get("headRefName") or ""
    ref = ticket_ref_from_branch(repo, branch)

    argv = ["gh", "pr", "merge", str(pr), "--repo", repo, "--squash",
            "--delete-branch"]
    # Name the squash after the PR. Left to GitHub, a one-commit PR takes that
    # commit's subject, and finish-ticket's "WIP #N: tests failing" commit put
    # exactly that on FF-Weekly-Start-Sit's main for a green PR (#951).
    title = str(view.get("title") or "").strip()
    if title:
        argv += ["--subject", "{} (#{})".format(title, pr)]
    out = _run_gh(argv, capture_output=True, text=True)
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
        if ticket.in_motion_since is not None:
            write_lock(ticket, "")
            ticket.in_motion_since = None
            print("released {}".format(ref))
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
        indexes: Dict[str, Tuple[Dict[str, Dict], bool]] = {}
        for child in children:
            mark = "x" if child["state"] == "CLOSED" else " "
            print("  [{}] #{} {}".format(mark, child["number"], child["title"]))
            child_repo = child["repository"]["nameWithOwner"]
            if child_repo not in indexes:
                indexes[child_repo] = ticket_pr_index(child_repo)
            index, truncated = indexes[child_repo]
            pr = index.get(
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
                if truncated:
                    print("        PR state unknown -- ticket-PR scan truncated "
                          "at {} rows".format(MERGED_PR_SCAN_LIMIT))
                else:
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

    adoption = (
        proposed_class_for_approval(item.body)
        if verb == "approve" and item.klass not in LADDER
        else None
    )

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
        if adoption is not None:
            adopted_class, source_line = adoption
            print(
                "would adopt Class {} from source line `{}`; {}".format(
                    adopted_class, source_line, CLASS_ADOPTION_OVERRIDE_NOTE
                )
            )
        print("would move {} from {} to {} ({})".format(
            item.ref, expected, nxt, meaning))
        if no_tickets:
            print("accepting a project with no tickets — its work shipped "
                  "outside the pipeline")
        if verb == "accept":
            print("and close it as completed")
        print("\nNothing was changed. Re-run with --yes to answer the gate.")
        return 1

    if adoption is not None:
        adopted_class, source_line = adoption
        gh_graphql(
            SET_FIELD,
            project=PROJECT_ID,
            item=item.item_id,
            field=CLASS_FIELD_ID,
            option=_option_id(CLASS_FIELD_ID, adopted_class),
        )
        item.klass = adopted_class
        print(
            "adopted Class {} from source line `{}`; {}".format(
                adopted_class, source_line, CLASS_ADOPTION_OVERRIDE_NOTE
            )
        )

    # The guard, not the whole writer: this path has its own confirmation and
    # local-state handling, and the closed-issue rule is one predicate shared
    # by every Status write rather than a second implementation.
    refusal = status_write_refusal(item, nxt)
    if refusal is not None:
        _post_status_refusal(item, refusal)
        print(refusal, file=sys.stderr)
        return 1

    gh_graphql(SET_FIELD, project=PROJECT_ID, item=item.item_id,
               field=STATUS_FIELD_ID, option=_option_id(STATUS_FIELD_ID, nxt))

    if adoption is not None:
        adopted_class, source_line = adoption
        comment = _run_gh(
            [
                "gh", "issue", "comment", str(item.number), "--repo", item.repo,
                "--body", class_adoption_comment(
                    adopted_class, source_line, at=now
                ),
            ],
            capture_output=True, text=True,
        )
        if comment.returncode != 0:
            raise GitHubError(
                "moved {} to Ready and adopted Class {}, but could not record "
                "the class adoption comment: {}".format(
                    item.ref, adopted_class, comment.stderr.strip()
                )
            )

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


def _parking_wake_date(value: str) -> date:
    """Require a future calendar date before loading or writing to GitHub."""
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise argparse.ArgumentTypeError("wake date must use YYYY-MM-DD")
    try:
        wake_date = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "wake date must be a valid YYYY-MM-DD calendar date"
        )
    if wake_date <= _block_condition_date():
        raise argparse.ArgumentTypeError(
            "wake date must be after today's UTC date"
        )
    return wake_date


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
    # The implementation engine imports this module for the established
    # GitHub and lock operations.  Keep that dependency one-way by forwarding
    # this public spelling to its standalone process instead of importing it.
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if raw_argv and raw_argv[0] == "finish-ticket":
        proc = subprocess.run(
            [sys.executable, str(CHECKOUT_ROOT / "finish-ticket")]
            + raw_argv[1:]
        )
        return proc.returncode

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
    sub.add_parser(
        "snapshot",
        help="newest published brief snapshot, without running a live brief",
    )
    sub.add_parser("ideas", help="captured ideas, flagged ones first")
    sub.add_parser(
        "doctor", help="check the local install and report actionable failures")
    main_ci = sub.add_parser(
        "main-ci",
        help="each member repo's red main, as an infrastructure stop or a "
             "real failure")
    main_ci.add_argument(
        "--retry", action="store_true",
        help="rerun an infrastructure stop once per SHA; never a real "
             "failure, and never a SHA already retried",
    )
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
    capture.add_argument(
        "--caused-by", dest="caused_by", action="append", default=None,
        metavar="REF",
        help="earlier PR or ticket that caused this idea; repeat for more",
    )
    claim = sub.add_parser("claim", help="take the single-in-motion lock on a ticket")
    claim.add_argument("ref", help="issue number, owner/repo#number, or URL")
    release = sub.add_parser("release", help="give up the lock on a ticket")
    release.add_argument("ref", help="issue number, owner/repo#number, or URL")
    for verb, help_text in (
        ("pin", "pin a project: it leads Nate's queue at its gate and its tickets lead the engineers' queue"),
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
        "--wake-date", type=_parking_wake_date, default=None,
        help="future YYYY-MM-DD date to restore the prior Project Status",
    )
    park.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    park.add_argument(
        "--agent", default=None,
        help="agent that wrote the comment; otherwise read the heartbeat spool",
    )
    answer_gates = sub.add_parser(
        "answer-gates",
        help="record an answered Gates question in a plan body",
    )
    answer_gates.add_argument(
        "ref", help="issue number, owner/repo#number, or URL")
    answer_gates.add_argument(
        "--answer", required=True,
        help="the instruction verbatim, not a paraphrase of it",
    )
    answer_gates.add_argument(
        "--decider", default="Nate",
        help="who answered; defaults to Nate, who owns this gate",
    )
    answer_gates.add_argument(
        "--run", default=None,
        help="heartbeat run id; otherwise infer a unique open local start",
    )
    answer_gates.add_argument(
        "--agent", default=None,
        help="agent that heard the answer; otherwise read the heartbeat spool",
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
        "--role", dest="caller_role", choices=BEGIN_CALLER_ROLES, default=None,
        help="caller role: review or implement; omitted preserves agent-based "
             "routing",
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
    if args.command == "main-ci":
        rows = (
            main_ci_retries() if args.retry else main_ci_json()
        )
        if not rows:
            print("nothing — every member repo's main is green or still running")
            return 0
        for row in rows:
            line = "{} {} {} — {}".format(
                row["repo"], (row.get("sha") or "?")[:12],
                row["verdict"], row["reason"],
            )
            if row.get("job"):
                line += " (job {})".format(row["job"])
            print(line)
            if "retry" in row:
                print("  {}: {}".format(row["retry"], row["retry_reason"]))
        return 0

    if args.command == "doctor":
        return cmd_doctor()
    # The published snapshot is a local read by design: runners and routines
    # must get it without a Project load, which is the slow read this
    # command exists to avoid.
    if args.command == "snapshot":
        return cmd_snapshot()

    # ``begin`` can refuse on local usage or presence facts without consulting
    # the Project. Keep that gate ahead of the shared loader; an ordinary poll
    # must not spend the full Project read merely to learn that it cannot run.
    begin_preflight = None
    begin_phase_started: Optional[float] = None
    begin_timings: Optional[Dict[str, object]] = None
    begin_member_repo_names: Optional[List[str]] = None
    if args.command == "begin":
        begin_phase_started = time.perf_counter()
        begin_preflight = _begin_preflight(
            now, args.agent, args.idle, args.tier)
        if begin_preflight[1] is None:
            print(json.dumps(begin_preflight[0], indent=2))
            return 0
        role_refusal = _begin_role_refusal(
            args.agent, args.tier, args.caller_role)
        if role_refusal is not None:
            begin_preflight[0].update(role_refusal)
            print(json.dumps(begin_preflight[0], indent=2))
            return 0
        begin_timings = {}
        reserve_response_seen = {"value": False}

        def check_first_member_response(response):
            reserve_response_seen["value"] = True
            reserve = _begin_api_reserve_preflight(
                args.agent, args.tier, args.caller_role, response
            )
            if reserve is not None:
                raise BeginReserveStop(reserve)

        try:
            begin_member_repo_names = _begin_load_timed(
                begin_timings,
                "member_repos",
                lambda: _call_with_optional_keyword(
                    member_repos,
                    "after_first_response",
                    check_first_member_response,
                ),
            )
            if not reserve_response_seen["value"]:
                raise BeginReserveStop(_begin_api_reserve_preflight(
                    args.agent, args.tier, args.caller_role
                ) or {
                    "gate": "reserve",
                    "do": "stop",
                    "why": "GraphQL budget could not be read; a run that "
                           "cannot read its budget does not work",
                })
        except BeginReserveStop as stop:
            begin_preflight[0].update(stop.result)
            begin_preflight[0]["timings"] = begin_timings
            _record_begin_reserve(
                args.agent,
                begin_preflight[0].get("run"),
                begin_preflight[0]["why"],
            )
            print(json.dumps(begin_preflight[0], indent=2))
            return 0
        except GitHubError as exc:
            if _budget_exhaustion_signal() is not None:
                begin_reserve = _begin_api_reserve_preflight(
                    args.agent, args.tier, args.caller_role
                )
                if begin_reserve is not None:
                    begin_preflight[0].update(begin_reserve)
                    begin_preflight[0]["timings"] = begin_timings
                    _record_begin_reserve(
                        args.agent,
                        begin_preflight[0].get("run"),
                        begin_preflight[0]["why"],
                    )
                    print(json.dumps(begin_preflight[0], indent=2))
                    return 0
            _begin_error_envelope(args.agent, exc)
            return 2

    brief_timings: Optional[Dict[str, object]] = (
        {} if args.command == "brief" else None
    )
    brief_load_token = (
        _ACTIVE_BRIEF_TIMINGS.set(brief_timings)
        if brief_timings is not None else None
    )
    brief_load_started = (
        time.perf_counter() if brief_timings is not None else None
    )
    begin_detail_loader: Optional[Callable[[Sequence[Item]], None]] = None
    try:
        if _items is not None:
            items = _items
        elif _items_loader is not None:
            if args.command == "begin":
                items = _call_with_optional_keywords(
                    _items_loader,
                    include_details=False,
                    member_repo_names=begin_member_repo_names,
                    timings=begin_timings,
                )
            else:
                items = _items_loader()
        elif args.command == "begin":
            # `begin` selects one job after its cheap gates. Keep the initial
            # Project scan compact; cmd_begin hydrates only the candidates it
            # actually needs to order or hand out.
            items = _call_with_optional_keywords(
                load_items,
                include_details=False,
                member_repo_names=begin_member_repo_names,
                timings=begin_timings,
            )

            def hydrate_begin_candidates(candidates):
                return _begin_load_timed(
                    begin_timings,
                    "item_details",
                    lambda: hydrate_item_details(items, candidates),
                )

            begin_detail_loader = hydrate_begin_candidates
        else:
            items = load_items()
        if args.command == "begin" and begin_detail_loader is None:
            def hydrate_begin_candidates(candidates):
                return _begin_load_timed(
                    begin_timings,
                    "item_details",
                    lambda: hydrate_item_details(items, candidates),
                )

            begin_detail_loader = hydrate_begin_candidates
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
        if args.command == "begin":
            _begin_error_envelope(args.agent, exc)
            return 2
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2
    finally:
        if brief_timings is not None:
            brief_timings["project_load"] = round(
                max(0.0, time.perf_counter() - brief_load_started), 6
            )
            _ACTIVE_BRIEF_TIMINGS.reset(brief_load_token)

    try:
        repo_readiness = None
        begin_pr_facts = None
        begin_pr_facts_error = None
        begin_pr_facts_elapsed = None
        if args.command in ("next", "queue"):
            repo_readiness = _begin_load_timed(
                begin_timings,
                "repo_readiness",
                lambda: repo_readiness_for_items(items),
            )
        elif (
            args.command == "begin"
            and begin_uses_ticket_path(
                args.agent, args.tier, args.caller_role
            )
        ):
            repos = sorted({item.repo for item in items})
            pool_size = min(BEGIN_FETCH_POOL_SIZE, len(repos) + 1)
            with ThreadPoolExecutor(max_workers=pool_size) as executor:
                pr_facts_future = _submit_begin_read(
                    executor, _timed_begin_pr_facts, items
                )
                repo_readiness = _begin_load_timed(
                    begin_timings,
                    "repo_readiness",
                    lambda: _call_with_optional_keyword(
                        repo_readiness_for_items,
                        "_executor",
                        executor,
                        items,
                    ),
                )
                (
                    begin_pr_facts,
                    begin_pr_facts_error,
                    begin_pr_facts_elapsed,
                ) = pr_facts_future.result()
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
                            args.run, args.agent, args.wake_date)
        if args.command == "answer-gates":
            return cmd_answer_gates(items, now, args.ref, args.answer,
                                    args.decider, args.run, args.agent)
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
                               args.run, args.agent, args.origin, args.klass,
                               args.caused_by)
        if args.command == "begin":
            begin_kwargs = {
                "repo_readiness": repo_readiness,
                "caller_role": args.caller_role,
            }
            if begin_phase_started is not None and _accepts_keyword(
                cmd_begin, "_phase_started"
            ):
                begin_kwargs["_phase_started"] = begin_phase_started
            if begin_detail_loader is not None:
                begin_kwargs["_detail_loader"] = begin_detail_loader
            begin_kwargs["_preflight"] = begin_preflight
            if begin_timings is not None and _accepts_keyword(
                cmd_begin, "timings"
            ):
                begin_kwargs["timings"] = begin_timings
            if begin_pr_facts_elapsed is not None:
                begin_kwargs.update(
                    _pr_facts=begin_pr_facts,
                    _pr_facts_error=begin_pr_facts_error,
                    _pr_facts_elapsed=begin_pr_facts_elapsed,
                )
            return cmd_begin(
                items, now, args.agent, args.tier, args.idle,
                args.breakdown, **begin_kwargs
            )
        if args.command == "next-review":
            return cmd_next_review(items, args.tier)
        if args.command == "review":
            return cmd_review(args.repo, args.pr, args.verdict, args.ci,
                              args.blocking, args.note, args.run, args.agent,
                              items=items)
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
            timings = brief_timings if brief_timings is not None else {}
            degraded: List[Dict[str, object]] = []
            deadline = time.perf_counter() + BRIEF_TOTAL_BUDGET_SECONDS
            cache = _ACTIVE_BRIEF_CACHE.get() or BriefCache()
            cache_token = _ACTIVE_BRIEF_CACHE.set(cache)
            brief_timing_token = _ACTIVE_BRIEF_TIMINGS.set(timings)

            try:
                pr_facts_error: List[str] = []

                def read_pr_facts():
                    try:
                        return cache.get_pr_facts(items)
                    except BriefSectionTimeout:
                        # One retry sharing the section deadline (#1210):
                        # the section state set by _brief_timed still
                        # bounds the second attempt, so no extra budget
                        # is granted. Only retry when time remains.
                        state = _BRIEF_SECTION_STATE.get()
                        if (
                            state is not None
                            and float(state[1]) - time.monotonic() <= 0
                        ):
                            raise
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
                pr_facts_missing = bool(pr_facts_error)
                if pr_facts is _BRIEF_UNAVAILABLE:
                    pr_facts_missing = True
                    # A missing PR/branch scan must not turn a stale claim into a
                    # false diagnostic. An empty mapping says those facts are
                    # unavailable, so the pure consumers preserve the safe side.
                    pr_facts = {}
                    if not pr_facts_error:
                        error = "could not read ticket branch facts: brief section read timed out"
                        for section in BRIEF_PR_FACT_SECTIONS:
                            missing.append({"section": section, "error": error})
                outcome_signals = _brief_timed(
                    "outcome_signals",
                    lambda: _read_outcome_signals(now),
                    timings,
                    degraded,
                    deadline=deadline,
                )
                if outcome_signals is _BRIEF_UNAVAILABLE:
                    outcome_signals = None
                portfolio_metrics = _brief_timed(
                    "portfolio_metrics",
                    lambda: _read_portfolio_metrics(items, now),
                    timings,
                    degraded,
                    deadline=deadline,
                )
                if portfolio_metrics is _BRIEF_UNAVAILABLE:
                    portfolio_metrics = None
                decline_routing = _brief_timed(
                    "decline_routing",
                    lambda: _brief_read(
                        "decline_routing",
                        lambda: decline_routing_metric(items, now),
                        missing,
                    ),
                    timings,
                    degraded,
                    deadline=deadline,
                )
                if decline_routing is _BRIEF_UNAVAILABLE:
                    decline_routing = None
                # Live read, computed here rather than inside cmd_brief for the
                # same reason as the two above: the renderer stays pure over its
                # arguments, so a fixture brief needs no network and reports
                # `main_ci` as null — unknown — instead of an empty list.
                main_ci = _brief_timed(
                    "main_ci",
                    lambda: _brief_read("main_ci", main_ci_json, missing),
                    timings,
                    degraded,
                    deadline=deadline,
                )
                if main_ci is _BRIEF_UNAVAILABLE:
                    main_ci = None
                # Live per-repo read, computed here for the same reason as the
                # three above: cmd_brief stays pure over its arguments and a
                # fixture brief reports null — unknown — rather than an empty
                # list that would read as "nothing outside the Project".
                orphans = _brief_timed(
                    "member_issues_without_project_items",
                    lambda: _brief_read(
                        "member_issues_without_project_items",
                        lambda: member_issues_without_project_items(items),
                        missing,
                    ),
                    timings,
                    degraded,
                    deadline=deadline,
                )
                if orphans is _BRIEF_UNAVAILABLE:
                    orphans = None
                # Keep the existing brief JSON as the command's stdout. The
                # display snapshot is a separate, best-effort side effect and
                # must not change what callers parse or whether the command
                # succeeds. Capturing here also lets us spool the exact brief
                # object without adding a field to its /funnel contract.
                brief_stdout = io.StringIO()
                try:
                    with contextlib.redirect_stdout(brief_stdout):
                        brief_code = cmd_brief(
                            items,
                            now,
                            pr_facts=pr_facts,
                            missing=missing,
                            timings=timings,
                            degraded=degraded,
                            deadline=deadline,
                            brief_cache=cache,
                            outcome_signals=outcome_signals,
                            portfolio_metrics=portfolio_metrics,
                            decline_routing=decline_routing,
                            main_ci=main_ci,
                            orphan_issues=orphans,
                        )
                finally:
                    output = brief_stdout.getvalue()
                    sys.stdout.write(output)

                if brief_code != 0 or not output.strip():
                    return brief_code

                try:
                    brief_payload = json.loads(output)
                    if not isinstance(brief_payload, dict):
                        raise ValueError("brief output was not a JSON object")
                    generated_at = brief_payload.get("generated_at")
                    if not isinstance(generated_at, str) or not generated_at:
                        generated_at = now.isoformat()
                    authoring_pr_agents = {}
                    if any(
                        isinstance(fact, Mapping)
                        and str(fact.get("state") or "").upper() == "OPEN"
                        for fact in pr_facts.values()
                    ):
                        authoring_pr_agents = _dashboard_authoring_pr_agents()
                    try:
                        # The same holds `cmd_next` honours, from the local
                        # heartbeat; the board is not worth failing over them.
                        backed_off = backoff_withheld(_backoff_rows(), now)
                    except Exception:
                        backed_off = {}
                    write_dashboard_snapshot(
                        brief_payload,
                        dashboard_board(
                            items, now, pr_facts=pr_facts,
                            pr_facts_known=not pr_facts_missing,
                            authoring_pr_agents=authoring_pr_agents,
                            backed_off=backed_off,
                        ),
                        generated_at,
                        usage={
                            "muse": _dashboard_muse_usage(
                                now.timestamp()
                            ),
                        },
                    )
                except Exception as exc:
                    # The dashboard is downstream instrumentation. A missing
                    # or unwritable spool must never gate the live brief.
                    print(
                        "funnel: could not spool dashboard brief: {}".format(exc),
                        file=sys.stderr,
                    )
                return brief_code
            finally:
                _ACTIVE_BRIEF_TIMINGS.reset(brief_timing_token)
                _ACTIVE_BRIEF_CACHE.reset(cache_token)
        if args.command == "queue":
            return cmd_queue(items, now, repo_readiness=repo_readiness)
        return cmd_brief(items, now, pr_facts=ticket_pr_facts(items))
    except GitHubError as exc:
        if args.command == "begin":
            _begin_error_envelope(args.agent, exc)
            return 2
        print("funnel: {}".format(exc), file=sys.stderr)
        return 2


SESSION_ENV = "FUNNEL_SESSION"
SESSION_SERVER_ENV = "FUNNEL_SESSION_SERVER"
# Measured 2026-09-11: one Project load takes 31.7 s on the board of that day
# and `begin` does more, so the 30 s this started at (#581) timed every Muse
# run out at `begin` and no PR was reviewed for hours (#595). Three minutes
# fits the slowest honest command with room for the API's slow hours; the
# bound itself stays -- a hung child still fails loud and releases the session.
SESSION_TIMEOUT_SECONDS = 180
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

# A fork can fail before a child exists when the host is briefly at its
# process limit.  Retry only that launch-time signature; a non-zero child exit
# or an ordinary OSError must keep its existing one-shot behaviour.  The
# backoff schedule leaves room below the total window even at its maximum
# jitter, while reaching a third retry near the observed 30–40 second recovery
# range.
FORK_RETRY_WINDOW_SECONDS = 60.0
FORK_RETRY_MAX_ATTEMPTS = 4
FORK_RETRY_BACKOFF_SECONDS = (5.0, 15.0, 30.0)
FORK_RETRY_JITTER = 0.10


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


def _is_transient_fork_error(error: BaseException) -> bool:
    """Return whether an OSError means the process launch can be retried."""
    if not isinstance(error, OSError):
        return False
    if error.errno in {
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        35,  # macOS reports this as ``os error 35`` in the launch failure.
    }:
        return True
    message = str(error).lower()
    return "resource temporarily unavailable" in message and (
        "fork" in message or "createprocess" in message
    )


def _fork_retry_delay(retry_number: int) -> float:
    """Return one jittered delay after ``retry_number`` launch failures."""
    index = min(retry_number - 1, len(FORK_RETRY_BACKOFF_SECONDS) - 1)
    base = FORK_RETRY_BACKOFF_SECONDS[index]
    return base * random.uniform(
        1.0 - FORK_RETRY_JITTER,
        1.0 + FORK_RETRY_JITTER,
    )


def _host_process_count() -> Optional[int]:
    """Read a process count without launching another child process.

    The retry path exists because the host may refuse ``fork`` itself, so a
    diagnostic must not invoke ``ps`` or another subprocess.  macOS exposes
    the current user's process table through libproc; Linux's procfs gives us
    a useful host-wide count.  Other platforms report that the source is not
    available rather than masking the launch failure.
    """
    try:
        if sys.platform == "darwin":
            import ctypes

            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            listpids = libproc.proc_listpids
            listpids.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            listpids.restype = ctypes.c_int

            # PROC_UID_ONLY from <sys/proc_info.h>.  The doctor uses the same
            # scope because the macOS failure being diagnosed is per-user.
            pid_type = ctypes.c_int
            pid_size = ctypes.sizeof(pid_type)
            needed = listpids(4, os.getuid(), None, 0)
            if needed < 0:
                return None
            if needed == 0:
                return 0

            slots = max(1, (needed + pid_size - 1) // pid_size)
            pids = (pid_type * slots)()
            returned = listpids(
                4,
                os.getuid(),
                ctypes.cast(pids, ctypes.c_void_p),
                ctypes.sizeof(pids),
            )
            if returned < 0:
                return None
            return returned // pid_size

        procfs = pathlib.Path("/proc")
        return sum(
            1 for entry in procfs.iterdir()
            if entry.name.isdigit()
        )
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _host_diagnostics(
    failures: Sequence[Tuple[int, Tuple[str, ...], OSError]],
) -> str:
    """Render best-effort host facts for an exhausted fork retry."""
    try:
        loads = tuple(float(value) for value in os.getloadavg())
        load_text = ", ".join("{:.2f}".format(value) for value in loads)
    except (AttributeError, OSError, TypeError, ValueError):
        load_text = "unavailable"

    process_count = _host_process_count()
    process_text = (
        str(process_count) if process_count is not None else "unavailable"
    )

    recent = []
    for attempt, failed_command, error in failures[-FORK_RETRY_MAX_ATTEMPTS:]:
        command_text = " ".join(str(part) for part in failed_command)
        command_text = " ".join(command_text.split())
        if len(command_text) > 160:
            command_text = command_text[:157] + "..."
        detail = str(error).strip() or type(error).__name__
        recent.append(
            "attempt {} {} ({})".format(attempt, command_text, detail)
        )

    failures_text = "; ".join(recent) if recent else "none recorded"
    return (
        "host diagnostics: load average (1m, 5m, 15m) {}; process count {}; "
        "recent launch failures: {}"
    ).format(load_text, process_text, failures_text)


def _fork_retry_error(
    command: Sequence[str], attempts: int, elapsed: float, cause: OSError,
    failures: Optional[Sequence[Tuple[int, Tuple[str, ...], OSError]]] = None,
) -> OSError:
    """Describe an exhausted transient launch retry in actionable terms."""
    executable = str(command[0]) if command else "the command"
    message = (
        "could not launch {} after {} attempts over {:.1f}s: host process "
        "pressure remained temporarily unavailable; retry on the next "
        "scheduled run or close idle processes"
    ).format(executable, attempts, elapsed)
    observed_failures = failures or [
        (attempts, tuple(str(part) for part in command), cause)
    ]
    message += "; " + _host_diagnostics(observed_failures)
    if cause.errno is None:
        return OSError(message)
    return OSError(cause.errno, message)


def _run_bounded_subprocess(command: Sequence[str], **kwargs):
    """Run a child process with the current session command's deadline.

    Process pressure can make ``Popen`` fail before a child exists.  Only that
    transient fork signature gets a bounded retry; once a child starts, its
    return code and every other launch error retain the normal one-shot path.
    """
    requested_timeout = kwargs.get("timeout")
    retry_started = time.monotonic()
    retry_deadline = retry_started + FORK_RETRY_WINDOW_SECONDS
    attempts = 0
    last_fork_error: Optional[OSError] = None
    launch_failures: List[Tuple[int, Tuple[str, ...], OSError]] = []

    while True:
        if last_fork_error is not None and (
            attempts >= FORK_RETRY_MAX_ATTEMPTS
            or time.monotonic() >= retry_deadline
        ):
            raise _fork_retry_error(
                command,
                attempts,
                time.monotonic() - retry_started,
                last_fork_error,
                failures=launch_failures,
            ) from last_fork_error

        attempt_kwargs = dict(kwargs)
        remaining = _session_command_remaining()
        if remaining is not None:
            if requested_timeout is None:
                attempt_kwargs["timeout"] = remaining
            else:
                attempt_kwargs["timeout"] = min(
                    float(requested_timeout), remaining
                )

        attempts += 1
        try:
            return subprocess.run(command, **attempt_kwargs)
        except subprocess.TimeoutExpired as exc:
            state = _SESSION_COMMAND_STATE.get()
            if state is not None:
                raise SessionCommandTimeout(state[0]) from exc
            raise
        except OSError as exc:
            if not _is_transient_fork_error(exc):
                raise
            last_fork_error = exc
            launch_failures.append(
                (attempts, tuple(str(part) for part in command), exc)
            )
            if attempts >= FORK_RETRY_MAX_ATTEMPTS:
                continue

            remaining_window = retry_deadline - time.monotonic()
            if remaining_window <= 0:
                continue
            delay = min(
                _fork_retry_delay(attempts),
                remaining_window,
            )
            session_remaining = _session_command_remaining()
            if session_remaining is not None:
                delay = min(delay, session_remaining)
            if delay <= 0:
                continue
            time.sleep(delay)


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
        # ``main`` refreshes the process-level provenance for every forwarded
        # command.  A session is the durable boundary for one Muse run, so
        # remember the run created by its first begin instead of trying to
        # infer it again after overlapping sessions have opened.
        self._heartbeat_run: Optional[str] = None
        self._heartbeat_agent: Optional[str] = None
        self._heartbeat_tier: Optional[str] = None

    def _load_items(
        self,
        include_details: bool = True,
        member_repo_names: Optional[Sequence[str]] = None,
        timings: Optional[Dict[str, object]] = None,
    ) -> List[Item]:
        if self.items is None:
            self.items = _call_with_optional_keywords(
                self._loader,
                include_details=include_details,
                member_repo_names=member_repo_names,
                timings=timings,
            )
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
            if argv and argv[0] == "begin":
                self._heartbeat_tier = _argv_value(argv, "--tier")
            caller_token = _ACTIVE_GRAPHQL_CALLER.set(
                graphql_caller_for_command(
                    argv, tier=self._heartbeat_tier
                )
            )
            try:
                # Session servers are created before begin, but tests and
                # embedded callers can reuse this module after another main()
                # invocation.  Do not let that stale process-level value look
                # like the run this session is about to start.
                if self._heartbeat_run is None and argv and argv[0] == "begin":
                    global _ACTIVE_HEARTBEAT_RUN, _ACTIVE_HEARTBEAT_AGENT
                    _ACTIVE_HEARTBEAT_RUN = None
                    _ACTIVE_HEARTBEAT_AGENT = None
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
                if self._heartbeat_run is None and argv and argv[0] == "begin":
                    self._heartbeat_run = _ACTIVE_HEARTBEAT_RUN
                    self._heartbeat_agent = _ACTIVE_HEARTBEAT_AGENT
                if self._heartbeat_run:
                    report_api_cost(
                        run=self._heartbeat_run,
                        agent=self._heartbeat_agent,
                    )
                else:
                    report_api_cost()
                report_graphql_spend()
                _ACTIVE_BRIEF_CACHE.reset(cache_token)
                _ACTIVE_GRAPHQL_CALLER.reset(caller_token)
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

        measured = api_cost()
        measured["graphql_by_caller"] = graphql_caller_spend()
        heartbeat.record_api_cost(agent, run, measured)
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
    caller_token = _ACTIVE_GRAPHQL_CALLER.set(
        graphql_caller_for_command(sys.argv[1:])
    )
    try:
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
    finally:
        _ACTIVE_GRAPHQL_CALLER.reset(caller_token)
