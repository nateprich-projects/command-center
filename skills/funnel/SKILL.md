---
name: funnel
description: Show what is waiting on Nate in the Command Center funnel — what needs a decision, at which gate, and for how long. Use when he asks what needs deciding, what is waiting on him, what to work on next, what is stuck or blocked, or invokes /funnel by name. Also use when he asks whether the funnel is healthy or whether maintenance is crowding out new work.
---

# /funnel

Render the Command Center funnel readably.

## Run this

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py brief
```

Use the absolute path, not `~` — it matches the existing `Bash(python3
/Users/nateprich/.claude/command-center/*)` always-allow rule exactly; a tilde would not.

It prints JSON. Render it as described below.

**Do not rank, reorder, filter or re-prioritise anything.** `funnel.py` computes all
ordering, and both agents act on its output. If the order looks wrong, say so — do not
quietly fix it. A second opinion on ordering is how two agents drift apart while both
produce plausible-looking lists.

_(confirmed by Nate 2026-09-05)_

If the command fails, show the error. Do not fall back to querying GitHub yourself.

## What the fields mean

| Field | Meaning |
|---|---|
| `total_needing_nate` | How many decisions are waiting |
| `counts_by_gate` | Open items at each gate. `Ideas` is deliberately excluded — it is unbounded and guilt-free, and counting it turns it into pressure |
| `items` | The decisions, **already ordered**. Bottom-up: closest to shipping first |
| `waiting_on` | The question being asked. `Accept it?` · `Is the plan good?` · `Unblock or park?` |
| `waited` | Time at the current gate |
| `class` | The item's Project `Class`; tickets inherit their parent's Class |
| `pinned` | Present as `true` when Nate pinned the Project within its current gate; absent otherwise |
| `needs_class` | Items with no `Class` set. Invalid and not startable — a one-word fix in the Project |
| `in_motion` | Tickets currently claimed, as a list. `wip_limit` is how many may run at once — the cap is policy, the per-ticket claim is correctness |
| `stale_locks_taken_over` | Claims past the 2-hour TTL that were taken over |
| `stranded` | Open items for which no current agent or gate can make progress. Diagnostic only; it does not add to `total_needing_nate` |
| `working_tree_touched` | Runs during which Nate's own checkout changed. No routine should write it — engineers use their own clones, reviewers are read-only. Reports a *change*, not a crime: him committing mid-run looks the same. Say it plainly when present |
| `maintenance_load` | `upkeep_share` is the fraction of work closed in the last 30 days that was `Broken` or `Maintenance` |
| `human_steps` | Open tickets only Nate can do, with the `reason` each declares. **Work he owes, not a decision he owes** — deliberately outside `total_needing_nate`, the same distinction that keeps `blocked` out. No agent can pick these up: `startable()` excludes them, so this list is the only place they surface |
| `suspected_human_steps` | Blocked child tickets whose block has no machine-readable condition but whose reason matches the known human-step vocabulary. Diagnostic only: leave the ticket blocked and let Nate decide whether to restate or split it |
| `parked` | Stopped items with the written reason each carries. The reason is the artifact that makes re-encountering an idea a 30-second decision |
| `closed_itself` | Projects the funnel closed in the recent named window, newest first, with the drift recorded at close |
| `awaiting_breakdown` | Approved plans with no tickets yet. Claude owes these a breakdown; they are not startable until it happens |
| `prose_dependencies` | Open tickets whose dependency sentence names an open issue without a matching native `blocked_by` edge. Each row carries the ticket `ref`, named issue `names`, and original `sentence`; diagnostic only |
| `unattended_merges` | Merges an agent made without him, read from heartbeat records. `plan.md` makes these appearing in the brief a condition of unattended merging being allowed at all |
| `agent_health` | Raised watchdog conditions, each with the heartbeat agent and the watchdog's condition wording. Empty when all agents are healthy |
| `rejected_merges` | Merges he checked and found broken, over `window_days`. `stop_auto_merging` true means three in a week — auto-merging stops until he fixes the review bar |
| `closed_with_access_vocabulary` | Projects that closed with access-shaped words in the plan and no human-step ticket. The detective backstop for when every preventive layer missed one |

## How to render it

Lead with the count and the ordered list. For each item: its Class, a pin marker when
`pinned` is `true`, the question, the repo and issue title as a link, and how long it has
waited. Keep it scannable — this is read to decide, not to browse.

**Then `human_steps`, whenever it is non-empty**, as its own short list with each item's
reason. Never fold it into the decision list and never count it in the total: it answers a
different question — not *what do you have to decide* but *what is waiting on you to go
and do*. It needs its own line precisely because nothing else surfaces it; no agent can be
handed one, so an unrendered human step is invisible everywhere.

Then `closed_itself`, whenever it is non-empty, as its own short list, newest first. For
each project show the title and closed-at time. Say **"closed itself with drift"** and
name every drift signal when `drift` is non-empty; say **"closed itself cleanly"** when
the list is empty. This distinction is the point of the section — do not collapse a
drifted close into a generic completion line.

Then the gate counts on one line. Then anything unusual, and only if present:
`prose_dependencies`, `suspected_human_steps`, `needs_class`, `stale_locks_taken_over`, `stranded`, `in_motion`,
`awaiting_breakdown`, `unattended_merges`, `agent_health`, `rejected_merges`, and
`closed_with_access_vocabulary`. A suspected human step is report-only: do not clear its
`blocked` label, restate it, or split it while rendering the brief.

`prose_dependencies` is also report-only. For each row, show the ticket ref, the named
open issue refs, and the sentence that produced them. Do not write a native edge while
rendering the brief; the separate backfill ticket owns that action.

Offer the `launch` command for the top item. Do not run it.

## Interrogating before answering

A gate answered without context is a coin toss, so when he asks about an item —
or before he answers one — show him the evidence:

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py show <issue>
```

It assembles what that gate actually needs: the plan at `Shaped`, the tickets at
`Ready`, and at `Building` what shipped, which PRs merged without him, what the
reviewer said, and the rejected-merge count.

**"closed with no ticket/* PR" is a real finding, not a formatting quirk** — it
means work was closed without going through review, and at the accept gate that
is exactly what he should know before saying yes.

## Answering a gate

`funnel approve` / `accept` answer the two gates. They are **dry runs
unless `--yes` is passed**.

**The decision is always his. The typing does not have to be.** Nate authorised agents
to run both on his explicit instruction (2026-09-05), replacing an earlier rule
here that no agent may run them at all. He often works these sessions by voice while
driving, where handing him a command means the decision he already made goes
unrecorded — approved gates sat unexecuted for a whole session that way.
_(confirmed by Nate 2026-09-05)_

That earlier rule was not his; an agent wrote it after accidentally running an approval,
catching it, and walking it back. The accident was real, so the caution is earned — but
what it should have forbidden was an agent *deciding*, not an agent *typing*.

Run one only when **all** of these hold:

- He gave an explicit instruction naming the item and the gate. Not "sounds good", not
  "that makes sense", not inferred from approval of your analysis.
- **The target is named, not inferred.** If you are working out which items he meant,
  say your reading and wait. Confirm before moving anything, not after.
- Never on your own initiative, and **never as a test.** The dry run exists for that.

_(confirmed by Nate 2026-09-05)_

Then **post a comment recording that he decided and you executed**, naming the gate and
the move. The Project shows only that `Status` changed, never who decided it — see
issue #31. Without that comment an agent-run gate is indistinguishable from his own,
which is the failure this permission would otherwise create.
_(confirmed by Nate 2026-09-05)_

## Say these things when they are true

**"Nothing is waiting on you"** when `total_needing_nate` is 0. Say it plainly and stop.
Do not pad the answer with the ladder, the gate counts, or what Codex is doing.

**Parking is a real option, every time.** The bottom-up ordering exists to force disposal,
so the longest-waiting item is surfaced first precisely because it is the most likely
park candidate. When something has waited a long time, say so and name parking as a
choice. Record that choice with `python3 /Users/nateprich/.claude/command-center/funnel.py
park <ref> --reason "<why>"`; `Parked` requires a written reason, and that reason is the
artifact that makes re-encountering the idea in four months a 30-second decision.

**Flag the portfolio signal, do not tune it.** If `upkeep_share` is above roughly 0.5, or
`days_since_anything_new_started` is large, that is not a scheduling problem to fix. It
is the signal to reassess how many plates are spinning.

**Three stale takeovers in a week means runs are dying.** One is noise.

`upkeep_share` is `null` when nothing closed in the window. That is *unknown*, not
healthy — do not report it as zero.

## Do not

- Do not add TODOs to TickTick. TickTick is the do-list; funnel items there would compete
  with real tasks and erode its trustworthiness. It receives operational failure alerts
  only.
- Do not change `Status` or `Class` unless he asks. Those are his gates.
  _(confirmed by Nate 2026-09-05)_
- Do not open, close, or comment on issues as part of rendering a brief.
