---
name: funnel
description: Show what is waiting on Nate in the Command Center funnel — decisions by gate, and stuck work. Use when he asks what needs deciding, what is stuck, or invokes /funnel.
---

# /funnel

## Run this

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py snapshot
```

Use the absolute path, not `~`: it matches the `Bash(python3
/Users/nateprich/.claude/command-center/*)` allow rule.

It prints the newest published snapshot: `{"brief": ..., "board": ...,
"generated_at": ...}`. The publisher owns brief generation; never run a
live brief yourself. Render `.brief` through the code template below, and
say the snapshot age past ~15 minutes: stale is weak evidence nothing waits.

**Do not rank, reorder, filter or re-prioritise.** `funnel.py` computes all
ordering. If it looks wrong, say so — do not quietly fix it.

If the command fails, show the error. Do not query GitHub yourself.

## What the fields mean

| Field | Meaning |
|---|---|
| `total_needing_nate` | Decisions waiting; `Ideas` excluded, never pressure |
| `human_steps` | Tickets waiting on Nate to go and do the declared `reason`; outside `total_needing_nate`, their only surface |
| `machine_local_steps` | Tickets waiting on a Claude Code session to go and do the declared `reason`; never folded into decisions or the total |
| `event_block_inconsistencies` | Blocked tickets whose event spec and Needs routing disagree |
| `unattended_merges` | Agent merges without him (`pr`, `at`, `note`, `agent`; retired agents excluded); call `self_reviewed: true` self-reviewed |

The code template documents these: `generated_at`, `counts_by_gate`,
`items`, `waiting_on`, `waited`, `class`, `pinned`, `needs_class`,
`unclassed_captures`, `in_motion`, `wip_limit`, `stale_locks_taken_over`,
`stranded`, `event_block_inconsistencies`, `working_tree_touched`,
`maintenance_load`, `disposal`,
`recorded_cause_regressions`, `command_center_ticket_pr_share`,
`resend_ratio`, `outcome_signals`, `blocked`, `blocked_human_steps`,
`blocked_machine_local_steps`, `suspected_human_steps`, `parked`,
`closed_itself`, `cleared_blocks`, `awaiting_breakdown`,
`prose_dependencies`, `unattended_approvals`, `run_summary`, `agent_health`,
`rejected_merges`, `status_state_mismatches`, `main_ci`,
`closed_with_access_vocabulary`, `member_issues_without_project_items`, `missing`, `timings`,
`degraded`.

For `event_block_inconsistencies`, each row shows `ref`, `title`, `needs` and
the mismatch direction; include `event_condition` when present, and omit the
section when the list is empty.

## How to render it

Lead with the count and ordered list: Class, pin, question, repo link, wait.
When `missing` is non-empty, say partial first and name each section and
error — never read null as all-clear.

Then `working_tree_touched` when non-empty: `before.head → after.head` with every `observers` entry as `agent`/`run`; one observer versus multiple observers explicit, never a bare count; dirty-only rows show counts.

Then `human_steps`, waiting on Nate to go and do, and
`machine_local_steps`, waiting on a Claude Code session to go and do.

Full per-section wording lives in code; render through it:

```bash
python3 /Users/nateprich/.claude/command-center/funnel_render.py
```

Offer the top `launch` command. Do not run it.

## Answering a gate

`funnel approve` / `accept` are dry runs unless `--yes` is passed; run one
only on his explicit instruction naming item and gate, then comment who
decided and what moved. Moving to `Shaped` records a plan, not approval.

### Adopt an explicit Proposed class at approval

With Class unset, `approve --yes` adopts exactly one non-empty whole-line
`Proposed class: <one ladder class>`, an exact match for `Investigate`,
`Broken`, `Maintenance`, `Improve`, `New` or `Replace`, and records the
source line. Missing, malformed, fuzzy or multiple proposals stay unset;
never infer a class from a title or body prose (Nate's rule, 2026-09-16,
overridable by him). Adoption fills a field; it never
satisfies the `Shaped` plan-good gate or auto-advances to `Ready` (#59
brake).
