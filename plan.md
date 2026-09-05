---
title: Command Center — Design Record
tags: [command-center, funnel, design-record, plan]
last_updated: 2026-09-05
status: Design settled. v0 in progress.
---

# Command Center — Design Record

**This file is the design record for this repository.** It was drafted in
`workbench`, where the v1 Command Center module lives and where the decision was
made, and moved here when this repo was created.

Per repository convention, the rejected options and the reasons for rejecting them
are the valuable part. They are recorded inline throughout.

## The problem

Not idea capture — that works. **Completion and disposal.**

Evidence from the state this replaces: 15 concurrent projects in
`claude-second-brain:projects/tracker.md` under `## Personal Systems & AI`;
36 tasks at `Not started`, some since July; exactly **1** task ever marked Parked,
out of 78. Nothing in the prior system was ever deliberately killed.

**The bottleneck is Nate's decision throughput**, not coding hours. Coding capacity
is bought by subscription and refills on a schedule he does not control. Judgment
does not.

_Rejected: framing this as an intake/refinement problem. Richer intake produces
beautifully specified unbuilt tools — strictly worse than half-built ones, because
the sunk effort makes them harder to kill._

## The funnel

`Ideas → Shaped → Ready → Building → Done`, plus `Parked`.

Status is **the last stage completed**, so an item is implicitly waiting at the next
gate unless an agent is actively working it.

| Status | Meaning | Needs Nate |
|---|---|---|
| Ideas | Captured, undecided. Unbounded and guilt-free. | No |
| Shaped | Grilled; a plan exists | **Is the plan good?** |
| Ready | Broken into issues | **Start now?** |
| Building | Codex is working it | Only when all children close |
| Done | Shipped and accepted (`state_reason: completed`) | No |
| Parked | Stopped, **written reason required** (`state_reason: not_planned`) | No |

Four gates, each requiring deliberate intent:
**worth shaping? → is the plan good? → start now? → accept it?**

Done and Parked are distinct and must never merge. Parked requires a reason; Done
does not. The reason line is the artifact that makes re-encountering an idea in four
months a 30-second decision rather than a re-derivation. Separating them is also the
only way to ever measure whether the gates are set right — finished-vs-abandoned is
the ratio that answers "is this working?".

_Rejected: the original six stages (idea → PRD → backlog → scoped → roadmap →
implementation). "Backlog" and "roadmap" both mean "yes, but not now", and in a
solo system the decider at both is the same person. A gate whose decision and
decider duplicate the previous gate's is the same room with a different sign._

_Rejected: auto-expiry / default-kill on stalled items. Parking is itself a clearing
action, and the ordering rule below forces the park decision. Auto-park would remove
the forcing function and, worse, write the reason line for you — losing the only
part with durable value._

### Vocabulary

Deliberately de-jargonised: some repos will be public, and a stranger should
understand a status or a label without a glossary.

| Label | Means |
|---|---|
| `needs-shaping` | This idea is worth thinking through |
| `blocked` | Cannot proceed; reason in a comment |

That is the complete label set. **GitHub's ten stock labels are deleted from each member
repo at onboarding** — `bug` duplicates `Class: Broken` and `enhancement` duplicates
`Class: New`, and the same fact recorded in two places is the dual-write problem this
design already refuses for stage.


_Rejected: the whole `wayfinder:*` vocabulary (`map`, `task`, `research`,
`prototype`, `grilling`) plus `grill-me`, `requires-nate`, `ready-for-human`,
`ready-for-agent`, `needs-info`, `resolution:implementation`,
`resolution:decision-only`. Each dropped for cause: `wayfinder:map` is replaced by
native sub-issues; `ready-for-*` is derivable from Status; `requires-nate` and
`grill-me` position the owner rather than describing the work, which reads badly on
a public repo; the ticket subtypes classify work nothing sorts by; and
`resolution:*` answers "does a prerequisite unblock on issue closure or on merged
integration" — a genuinely hard distinction that only arises when many people work
in parallel on shared code._

### The plan artifact

The plan lives in the **issue body** through Ideas and Shaped. When the work earns a
repo (at the Ready gate), it lands as that repo's `plan.md` and the issue transfers
with it. One transfer per project, at a boundary already being crossed.

This is not a new artifact type. `AGENTS.md` already defines `plan.md` as "the design
record… why it is shaped this way, and what was rejected and for what reason" — which
is exactly what a grilling session produces.

_Rejected: a `docs/prd/<slug>.md` file. Pre-repo items have no repo to put it in.
Rejected: the term "PRD" — office jargon for a home workshop._

## Two queues, two orderings

**Nate's decisions run bottom-up.** Clear the lowest-funnel decision before touching
anything above it. Park counts as clearing. This is what forces disposal and prevents
a stalled item from permanently plugging the queue.

**Codex's work runs the ladder:**

`Broken > Maintenance > Improve existing > Build new > Replace existing`

The ladder ranks what to **start**. Once a project is Building, its remaining tickets
finish first. Passing a gate is a commitment and nothing may silently un-commit it.

**Broken and Maintenance preempt in-flight work** — and this is only safe because both
are finite. The governing rule: **only classes that are finite may preempt.**

**Maintenance is defined tightly:** it is degrading, or it has a known date on which it
stops working. Expiring certs, a sunsetting API, a CVE, a service that keeps dying, an
OS update that broke a scheduled job. Cleanup, refactors, test coverage, dead-flag
removal, and non-failing dependency bumps are **Improve-existing**.

_Rejected: loose Maintenance ("anything on a shipped system that isn't a new
feature"). Loose maintenance is unbounded, and giving an unbounded class preemption
rights guarantees starvation of everything below it._

_Rejected: strict ladder applied to in-flight work. Improve-existing never runs out,
so a half-finished project would be preempted forever — producing exactly the
80%-complete "fix it in prod" situation the ladder exists to prevent._

**Portfolio signal:** if maintenance load ever blocks new work, that is not a tuning
problem. It is the signal to reassess how many plates are spinning. The brief carries
share-of-runs on Broken + Maintenance over 30 days, and days since anything new
started.

## The agents

| Actor | Does | When |
|---|---|---|
| **Claude** | Grilling (produces the plan); PR review against `plan.md` | Grilling interactive; review on a routine |
| **Codex** | Implementation, one ticket per run | Hourly poll, single-in-motion lock |
| **Nate** | The four gates; accepting a project as Done | When available |

Grilling is knowledge work and stays on Claude. It cannot be scheduled, which makes it
a structural throttle on the whole funnel — no artificial cap is needed.

**Claude reviews and merges.** When the diff meets the plan and tests pass, it approves
and merges. Two conditions: a re-review after a fix is a fresh read against the plan,
never a diff of the diff; and unattended merges appear in the brief as a record.

This moves the accept gate rather than deleting it — Nate accepts the **project**, not
each PR. Roughly one or two decisions a week instead of fourteen. Whether a diff matches
a written spec is checkable; whether the tool is worth keeping is not.

_Rejected: advisory-only review (every correct PR still lands on Nate — full budget,
full queue). Rejected: gatekeeper review that can fail but not approve — same problem._

**Rejected merges are their own class.** If Nate checks a merged PR and it is broken,
the information is not "there is a bug" — it is "the auto-merge bar failed". Three in a
week means stop auto-merging and fix the review prompt. One action from the brief
reopens the ticket, returns the parent to Building, files the regression against the
merged PR, and increments a visible counter.

### Execution rules

- **In-app scheduling only** — Claude Code Routines and Codex Scheduled. Both require
  their app open on the Mac mini. **Never invoke the CLIs headlessly** from launchd,
  cron, CI, or any script. This is an account-suspension risk and is non-negotiable.
- **No Copilot automation.** Those are employer-provided tokens; personal use stays
  one-off and manual. The v1 Copilot skill, canvas, and Run Map are turned off,
  archived, and marked cold storage.
- **Budget:** each agent gets a hard slice, enforced by an **early-exit gate inside the
  session** (see below). Target ~10% remaining Saturday morning (Claude) and Sunday
  morning (Codex). There is no monthly limit; only the 5-hour and weekly windows exist.

  **The gate reserves the cost of the run it authorises** — `used + reserve <= allowed`,
  not `used <= allowed`. Nothing can cap a session's spend once it begins, so a bare
  threshold check is a start check rather than a bound: it waves through a run that then
  blows past the line. Measured across 14 real Codex sessions, the median cost about 1% of
  the week and one cost 65%. Bootstrap reserves are 15% weekly and 30% five-hour,
  deliberately high, to be replaced by the measured p90 once the heartbeat has recorded
  real one-ticket runs.

- **A dying run must lose time, not work.** A session killed mid-run by a rate limit
  leaves a claimed lock and whatever it had done locally. So every run works on a branch
  named deterministically from its ticket (`ticket/<number>`) and **commits and pushes
  after each meaningful step** — the remote holds the work, which is why no state file is
  needed to find it again. A later run reads the ticket, inspects that branch, and decides
  to continue or reset; it treats the diff as untrusted, since it has no memory of the
  dead run's intent, and the review against `plan.md` catches a half-built diff either way.

  _Rejected: starting every run from `main`. It makes a dead run safe by discarding
  everything it had done — and what it spent doing that is the exact resource being
  rationed. A run dying at 90% complete would lose the most expensive part._

  The residual gap, accepted: for up to the 2-hour TTL after a mid-run death the claim
  still looks fresh, so nothing starts. Shortening the TTL trades that against killing
  legitimately slow runs, and with no agent identity a run cannot tell its own dead claim
  from another run in progress.
- **Stop-time buffer:** runs finish at least 5 hours before any window Nate might want
  the account back. The 5-hour rolling window, not the weekly one, is what actually
  locks him out.

### Reading usage (verified 2026-09-05)

Claude Code's `statusLine` command receives session JSON on stdin, and for Claude.ai Pro
and Max subscribers that JSON carries the real subscription numbers:

```json
"rate_limits": {
  "five_hour":  { "used_percentage": 23.5, "resets_at": 1738425600 },
  "seven_day":  { "used_percentage": 41.2, "resets_at": 1738857600 }
}
```

`used_percentage` runs 0-100; `resets_at` is Unix epoch seconds. These are account-level
figures, so they include claude.ai and mobile usage, not just Claude Code — which is what
makes them usable as a budget signal at all.

Configure in `~/.claude/settings.json`:

```json
{ "statusLine": { "type": "command", "command": "~/.claude/statusline.sh" } }
```

The script does double duty: it renders the status line **and** writes the two windows to
a cache file that the shared ranking program reads.

Caveats from the documentation, all of which the implementation must handle:

- `rate_limits` appears **only for Pro/Max subscribers, and only after the first API
  response in the session.** Absent before that.
- Each window is independently optional, and Claude Code **drops a window once its
  `resets_at` passes.** Read with `jq -r '.rate_limits.seven_day.used_percentage // empty'`
  or the equivalent; never assume presence.
- Updates are debounced at 300ms and only fire while a session is active, so a cached
  value goes stale whenever Claude Code isn't running.

**The gate therefore lives inside the session, not before it.** A stale cached value is
always *lower* than the truth — `used_percentage` only rises within a window — so gating
on the cache would systematically overestimate headroom, which is the exact failure the
budget exists to prevent. Instead: the routine starts, its own statusline write refreshes
the cache, the routine reads the fresh numbers, and exits immediately without doing work
if the pace line is exceeded. A no-op session costs nothing; the work is what costs. This
also avoids the deadlock a fail-closed pre-session gate would create, where a stale cache
prevents the very run that would refresh it.

_Rejected: estimating usage from `~/.claude/projects/**/*.jsonl` token counts. Those
record Claude Code's own consumption only, and Nate uses the same subscription on
claude.ai and mobile — so the estimate would undercount exactly in the direction that
burns his week._

**Codex's equivalent is not yet verified.** The same early-exit pattern applies whatever
the mechanism turns out to be: start, read, exit if over pace.

_Rejected: a launchd scheduler shelling out to `claude -p` / the Codex CLI. Sanctioned
in-app scheduling is the only acceptable path; account risk is not worth a nicer
scheduler. Rejected: GitHub Actions for agent work — it cannot reach subscription
logins and would bill metered API rates for capacity already purchased._

## Scope and membership

Repos **opt in via a topic**. Membership is itself a gate: in the funnel is a
commitment; out of it means still deciding what to do with it. Repos that Command
Center creates default to **on** — building something and losing track of it is the
failure being prevented.

**Onboarding is a ritual with an entry standard.** A repo joins only when (a) its work
is in its correct long-term home and (b) its backlog has been triaged. One repo at a
time.

The funnel spans both `nateprich` (public) and `nateprich-projects` (private) — the
owner split means a repo going public changes owners, and graduation must not eject
work from the system.

`braven112/mfl.football.v2` is explicitly **out**. It belongs to someone else, and no
agent-authored PRs land in a collaborator's repo.

_Rejected: an explicit allowlist (forget to add a repo and the funnel silently ignores
your work). Rejected: a denylist, which is what v1 `config.yml` does — forget to
exclude, and an agent with commit access wanders into personal writing or a kid's
soccer schedule._

_Rejected: migrating the existing backlog. Moving 36 unmade decisions into a new
system makes day one look identical to the mess it replaces. The per-repo onboarding
ritual replaces bulk migration entirely._

## Surfaces

`/funnel` is **separately invokable in any chat** and emits a fixed artifact: total
needing Nate, counts at each gate (Ideas excluded), ordered items with repo, link,
why it waits, how long, and a **launch command per item**. The morning brief is a
**consumer** of that artifact, not its owner.

_Rejected: building the funnel's visibility into the morning brief. The brief is
itself half-finished. Wiring a not-yet-working funnel into a not-yet-working briefing
is precisely the failure this system exists to prevent._

**Attention is derived, never maintained.** Stage is written explicitly at gates —
four writes per item lifetime, each attached to a decision already being made. "What
is waiting on Nate" is computed from observable facts. A maintained blocker list goes
stale; a derived one is correct by construction.

**Capture** is by chat ("add this idea to my command center backlog") or directly in
GitHub. **Not TickTick** — that is the do-list, and ideas there would compete with real
tasks and erode its trustworthiness. TickTick receives exactly one thing: operational
failure alerts, which are genuine tasks.

The cost, accepted knowingly: no hands-free capture. An idea in the car survives until
a keyboard or it doesn't. That is a filter.

## Architecture

**One shared Python program computes all ordering.** Both agents call it and act on its
output; neither ever ranks anything itself. This is the single principle harvested from
v1, whose ranking contract states it correctly: *"Ranking is deterministic,
lexicographic, and computed by the shared engine. AI may recommend source metadata
changes but MUST NOT supply a score, alter a rank key, or silently reorder items."*

Two different vendors' agents sharing no session, no memory, and no runtime **will**
drift if each implements the rules from prose — and the failure is silent, because both
produce plausible-looking lists. Shared code also makes the whole thing unit-testable
against fixtures.

**GitHub is the state.** No persistence, no locking, no journal — there is no shared
mutable state to protect.

**Local state, deliberately kept out of GitHub.** Two things live only on the Mac mini:
the statusline usage cache, because the numbers are readable only from inside a live
session, and the agents' own session transcripts, which `prior_run.py` reads to recover a
dead run's intent. Both are **recovery aids, never state of record** — lose them and the
funnel is unaffected.

**Retired from v1:** `$COPILOT_HOME` runtime state, checkouts, coordinators, Run Map,
the canvas extension, the operation engine with its claims and recovery, safety
validation, and the migration protocol. All of it exists to make concurrent mutation
safe across many sessions — not this situation.

**Watchdog:** a GitHub Actions cron checks heartbeats written by each run. It records
**outcome, not just liveness** — "ran, skipped, budget pace exceeded" is healthy;
"ran, errored" three times is not.

Each run records **start and end separately**, with a usage reading at each. A single
outcome line cannot detect a run that died before writing one — it is indistinguishable
from a run that never started, which is precisely the failure mode a rate limit produces.
The paired readings are also what measure real run cost, and so what replaces the
bootstrap reserves above. Actions is the right home precisely because the
watchdog cannot live inside the thing it watches: an app that quit is invisible to
every other signal on a machine that is otherwise fine.

## Sequencing

1. New private repo `nateprich-projects/command-center`.
2. Build v0 by hand: six Status options, two labels, the shared Python program, the
   `/funnel` skill, the Codex routine, the Claude review routine, the Actions heartbeat.
3. **Command Center is its own first member repo.** A new repo passes the entry standard
   trivially, so onboarding it tests the funnel without also testing the triage ritual.
4. `workbench` onboards second — that is where the triage ritual gets tested, on a repo
   with a real mess.
5. First ticket in `nateprich-projects/github-runners`: confirm runner isolation — no
   host mounts, no `.env` reach, no credential access. Agent-authored code runs in CI
   daily and is read by nobody; the argument that earned Colima its footprint for the
   Outlook server applies here with more force. Bare runners are accepted in the interim
   as a deliberate, ticketed decision.

**Visibility:** private, in the org. Extracting the tooling to a public repo later is a
morning's work; un-publishing a graveyard of half-formed ideas is not possible.

**The risk to watch:** the first thing this system builds is itself — the most seductive
kind of project. If v0 is still being polished a month from now, the system has failed
its own first test. By its own ladder, "Replace existing products" ranks last for
exactly this reason.

## Decisions settled at build kickoff (2026-09-05)

Four gaps surfaced when v0 was scoped for implementation. All four were decisions
deliberately deferred rather than oversights, and all four are now settled.

### Ladder class is a Project single-select field

An item's ladder class (`Broken`, `Maintenance`, `Improve`, `New`, `Replace`) is
**un-derivable** — it fails the same test `needs-shaping` passed, so it has to be
written down. It lives as a **`Class` single-select field on the Project**, alongside
`Status`.

**Nate sets it.** This is not a new decision, it is one dropdown on a gate he is
already passing. **Rule: anything not in `Ideas` must have a `Class`.** Unset is
invalid, not startable, and surfaces in the brief as a one-word fix.

**Unset must never default to `Broken`** — a forgotten field must not silently
acquire preemption rights.

The one case where `Class` is written by code rather than by Nate: the rejected-merge
flow sets `Class: Broken` mechanically.

_Rejected: five labels (`broken`, `maintenance`, `improve`, `new`, `replace`).
A single-select cannot be self-contradictory; five labels permit `broken` + `new` on
one issue, which forces validation rules — exactly the machinery the v1 wayfinder
contract carried and that was deleted with it. Single-select is also the same shape as
`Status`: two fields, one mental model. And it preserves the two-label decision, which
was justified by public readability — `Class` is an internal scheduling concern, and a
stranger browsing a public repo gains nothing from seeing `improve`._

### Stage stays in the Project Status field

`ProjectV2ItemStatusChangedEvent` exists on the issue timeline and carries `createdAt`,
`previousStatus`, `status`, and `project`. It is filterable inline via
`timelineItems(itemTypes: [PROJECT_V2_ITEM_STATUS_CHANGED_EVENT])`, so time-at-gate
comes back in the same query as the issue. Status therefore stays where it is.

_Rejected: `stage:*` labels with the Project demoted to a view. This was the fallback
had the timeline carried no timestamped field-change event — labels give timestamped
transitions for free via `labeled`/`unlabeled` events, a stable and boring API surface.
It is a genuinely good argument, and it loses only because the event does exist._

_Rejected unconditionally: dual-writing stage to both a field and a label. That is a
sync problem, and sync problems are how this system starts lying to you._

### Tiebreak within a gate: oldest-at-gate

Oldest-first, because **the longest-waiting item is the most likely park candidate** —
surfacing it first is what makes the ordering do disposal work rather than merely
sequencing. The same tiebreak applies in both queues.

The issue-number stand-in was **not needed**: `ProjectV2ItemStatusChangedEvent` was
confirmed firing on 2026-09-05, so v0 ships true time-at-gate. Time at the current gate
is `now - createdAt` of the most recent event whose `status` equals the item's current
Status, filtered to this Project.

### Single-in-motion: a timestamped claim on a Project field, with TTL takeover

**Superseded once, on evidence.** This was originally settled as an *assignment* lock —
an open issue assigned to Codex, timestamped by the `assigned` timeline event. A probe
(`command-center#10`, 2026-09-05) falsified the premise it rested on: **Codex desktop
shells out to the `gh` CLI as Nate**, with no bot identity and no originating-app marker,
so no GitHub write can distinguish a Codex run from Nate working by hand. A second GitHub
account does not help, because Codex is authorised by Nate's account and can only act as
it. Evidence in `LEARNINGS.md`.

The lock therefore needs a marker **only the agent writes**:

- **`In motion since`**, a text field on the Project holding an ISO-8601 UTC timestamp.
  Empty is free; set is held; **older than a 2-hour TTL is stale and takeable** — longer
  than any single ticket should honestly take.
- Written through **`funnel claim` and `funnel release`**, never by hand. The shared
  program owns the transition for the same reason it owns ranking: two vendors' agents
  implementing it from prose will drift, silently.
- A stale claim is taken over by the next run — unset it, claim it, continue.
  Self-correcting, no human in the loop.
- **Every takeover is a line in the brief.** One is noise; three in a week means runs are
  dying. **The watchdog owns that signal, not the lock.**
- A `Broken` ticket may take the lock before the TTL expires. That is the one sanctioned
  preemption, and it is only safe because `Broken` is finite.
- An unparseable or hand-edited value reads as **unlocked**. A garbled field must not
  wedge the queue until somebody notices.

_Rejected: a third label, `in-motion`. Its `labeled` event would give a GitHub-recorded
timestamp rather than a self-reported one, which is genuinely stronger. It loses because
the label set was closed at two on public-readability grounds, and a lock is more internal
than `Class` — which was kept off labels for exactly that reason. The weaker timestamp
costs little: the only writer is the agent, and a wrong value is bounded by the TTL._

_Rejected: keeping assignment and relying on Nate never self-assigning in a funnel repo.
A social contract enforcing a correctness property, which fails silently and stalls all
work for two hours. He has self-assigned before._

_Rejected: an open PR or a branch as the predicate — both appear too late to cover the
window between run start and PR creation, which is precisely when a run dies._

### A funnel item is a project; its sub-issues are the tickets

The four gates only make sense for a project. "Is the plan good?" and "start now?" are
decisions about *build the funnel*; they are nonsense about *write the tests*, which is a
step, not a decision. Codex works tickets, one per run.

So: **the project-level issue carries `Status` and `Class`. Its sub-issues are tickets and
carry neither** — they inherit `Class` for ladder ranking, and `Status` does not apply to
them. This is what keeps the gate count at roughly four decisions per project rather than
four per ticket.

GitHub adds a sub-issue to its parent's Project automatically, with its fields blank, so
this shape needs no maintenance. A parentless item with no `Status` at all is therefore
not a ticket — it is a project that was added and forgotten, and it is flagged as needing
a `Class`.

_Rejected: every issue carrying its own `Status`. Tried briefly and it is incoherent —
`command-center#1` appeared simultaneously in Nate's queue asking "start now?" and in
Codex's queue offering itself as work._

## Verification status

**Resolved 2026-09-05:**

1. **A user-owned Project v2 can hold issues from org repos.** Tested and works. Stage
   stays in the Project Status field; the `stage:*` label fallback is not needed.
2. **Subscription usage is readable** via the `statusLine` JSON payload — see
   [Reading usage](#reading-usage-verified-2026-09-05) above.
   ([docs](https://code.claude.com/docs/en/statusline#rate-limit-usage))
3. **`braven112/mfl.football.v2` does not opt in.** It stays outside the funnel; no agent
   PRs land in a collaborator's repo.

**Still open:**

5. Dependabot across repos other than `workbench` (none configured there). A one-time
   sweep during each repo's onboarding.
**Resolved 2026-09-05 (build kickoff):**

6. **Codex's usage is readable**, from `rate_limits` records in its session rollout JSONL —
   the same two window lengths as Claude's, under different names, and expressed as *used*
   where the UI shows *remaining*. `usage.py` normalises both vendors to one shape so the
   early-exit gate is identical for both routines. See `LEARNINGS.md`.
7. `ProjectV2ItemStatusChangedEvent` fires and carries a usable timestamp. Observed on
   `command-center#1`. Time-at-gate ships in v0; the issue-number stand-in is dropped.
8. **The funnel lives on its own Project**, `github.com/users/nateprich/projects/2`
   ("Command Center") — not Project 1, which was found to be in active use with 186 items
   on a `Todo`/`In Progress`/`Done` workflow across six un-onboarded repos. Sharing one
   Status field between two workflows would have made every gate count in the brief
   require a filter this design never specified.

   _Rejected: retrofitting Project 1 by adding the five new options alongside the legacy
   ones — a permanently eight-option field with `Done` meaning two different things.
   Rejected: replacing Project 1's options outright, which would have cleared Status on
   186 existing items — a migration, which this design refuses._
