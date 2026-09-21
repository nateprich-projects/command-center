---
name: funnel
description: Show what is waiting on Nate in the Command Center funnel — decisions by gate, and stuck work. Use when he asks what needs deciding, what is stuck, or invokes /funnel.
---

# /funnel

Render the Command Center funnel readably.

## Run this

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py snapshot
```

Use the absolute path, not `~` — it matches the `Bash(python3
/Users/nateprich/.claude/command-center/*)` allow rule; a tilde would not.

It prints the newest published snapshot: `{"brief": ..., "board": ...,
"generated_at": ...}`. The publisher owns brief generation on its own
cadence — never run a live brief yourself. Render `.brief` through the code
template below, and say the snapshot age from `generated_at` past ~15
minutes: stale is weak evidence nothing waits.

**Do not rank, reorder, filter or re-prioritise.** `funnel.py` computes all
ordering. If it looks wrong, say so — do not quietly fix it.

If the command fails, show the error. Do not query GitHub yourself.

## What the fields mean

| Field | Meaning |
|---|---|
| `total_needing_nate` | Decisions waiting; `Ideas` excluded, never pressure |
| `human_steps` | Open tickets waiting on Nate to go and do the declared `reason`. Work he owes, not a decision — outside `total_needing_nate`; `startable()` excludes them, so this list is their only surface |
| `machine_local_steps` | Open tickets waiting on a Claude Code session to go and do the declared `reason`; never folded into decisions, `human_steps`, or the total |
| `unattended_merges` | Agent merges without him, with `pr`, `at`, `note`, `agent` (retired agents excluded); `self_reviewed: true` means author and reviewer were the same agent — call it self-reviewed |

The code template documents the rest: `generated_at`, `counts_by_gate`,
`items`, `waiting_on`, `waited`, `class`, `pinned`, `needs_class`,
`unclassed_captures`, `in_motion`, `wip_limit`, `stale_locks_taken_over`,
`stranded`, `working_tree_touched`, `maintenance_load`, `disposal`,
`recorded_cause_regressions`, `command_center_ticket_pr_share`,
`resend_ratio`, `outcome_signals`, `blocked`, `blocked_human_steps`,
`blocked_machine_local_steps`, `suspected_human_steps`, `parked`,
`closed_itself`, `cleared_blocks`, `awaiting_breakdown`,
`prose_dependencies`, `unattended_approvals`, `run_summary`, `agent_health`,
`rejected_merges`, `closed_with_access_vocabulary`, `missing`, `timings`,
`degraded`.

## How to render it

Lead with the count and ordered list: Class, pin, question, repo link, wait.
When `missing` is non-empty, say partial first and name each section and
error — never read null as all-clear.

Then `working_tree_touched` when non-empty: `before.head → after.head` with every `observers` entry as `agent`/`run`; one observer versus multiple observers explicit, never a bare count; dirty-only rows show counts.

Then `human_steps` when non-empty — what is waiting on Nate to go and do — and `machine_local_steps` — what is waiting on a Claude Code session to go and do. Never folded into the decision list or the total.

Full per-section wording lives in code; render through it:

```bash
python3 /Users/nateprich/.claude/command-center/funnel_render.py
```

Offer the top `launch` command. Do not run it.

## Answering a gate

`funnel approve` / `accept` are dry runs unless `--yes` is passed. Run one
only on his explicit instruction naming item and gate — never inferred,
never as a test — then comment who decided and what moved, since the
Project never records that. Moving to `Shaped` records a plan, not approval.
