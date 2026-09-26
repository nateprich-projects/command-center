"""The /funnel rendering template (ticket #825, plan #794 Phase 6).

``skills/funnel/SKILL.md`` invokes this module instead of carrying the
rendering section itself, so the per-section wording lives in one place
and cannot drift from the brief ``funnel.py`` emits. Print it with::

    python3 /Users/nateprich/.claude/command-center/funnel_render.py

``RENDER_TEMPLATE`` documents every top-level brief key; the skill keeps
only the field table and the render order. A test pins that every key
``funnel.py`` emits is named here, which is the producer/consumer split
(#147) that the skill test guards against from the other side.
"""

from __future__ import annotations

import subprocess
import sys

RENDER_TEMPLATE = """\
Render the newest published snapshot's .brief as follows. The snapshot age
comes from `generated_at`: older than about fifteen minutes is weak evidence
that nothing is waiting.

Lead with the count and the ordered list. For each item: its Class, a pin
marker when `pinned` is `true`, the question (`waiting_on`), its waiting
reason when `waiting_reason` is present, the repo and issue title as a link,
and how long it has `waited`. Keep it scannable.

If `missing` is non-empty, say the brief is partial and name every missing
section and its error before interpreting any other empty or null value. A
missing `items` section means the Project could not be read; it is not
evidence that nothing is waiting.

Then `working_tree_touched`, whenever non-empty, as its own short list. For
each grouped HEAD transition show `before.head` to `after.head` and every
`observers` entry as its `agent`/`run`; distinguish one observer from
multiple observers and do not collapse the list to a count. For a dirty-only
row show its `agent`/`run` and the before/after dirty counts. Plain checkout
change, not a crime: Nate committing mid-run looks the same.

Then `human_steps`, whenever non-empty, as its own short list with each
item's `reason`. Never fold it into the decision list and never count it in
`total_needing_nate`: not what he must decide but what is waiting on Nate to
go and do. Nothing else surfaces it; no agent can be handed one.

Then `machine_local_steps`, whenever non-empty, likewise with `reason`: what
is waiting on a Claude Code session to go and do, not what Nate must decide.
Keep it separate from `human_steps` because a session can take these.

Then `blocked_human_steps` and `blocked_machine_local_steps`, whenever
either is non-empty, as advisory lists with `reason`, `blocked_reason` and
`blockers`. Waiting work, not actionable work: never fold them into the
queues above or the total.

Then `blocked`, whenever non-empty: show each ticket's block reason and
conditions. For an `event_condition`, name its agent, job and outcome, and
render `event_wait` as the elapsed time since its `after` timestamp.

Then `event_block_inconsistencies`, only when non-empty: each row shows
`ref`, `title`, `needs`, and the mismatch direction; include
`event_condition` when present, and omit the section when the list is empty.
These rows are diagnostics for disagreement between the event spec and
`Needs: external-event`.

Then `closed_itself`, newest first, with title and closed-at time. Say
"closed itself with drift" naming every drift signal when `drift` is
non-empty; say "closed itself cleanly" when empty.

Then `cleared_blocks`, newest first: ticket, conditions found closed, clear
time. A mechanical record, never added to the total.

Then `pending_wakes`, whenever non-empty: each parked issue as a link, its
`wake_date`, and its recorded `wake_status`. Items that have resumed are no
longer parked and therefore no longer appear here.

Then `unattended_approvals`, newest first: issue, transition time, stated
`basis`. A record to read at will, not a notification or review request.

Then `outcome_signals` as three independent named signals: cost per merged
PR by lane with its unit, then rework and intervention rates with sample
sizes. `insufficient_data` and `partial` are unknown: say what is missing,
never turn them into zero. No combined score; raw token counts are not
dollars.

Then `recorded_cause_regressions` and `command_center_ticket_pr_share` as
portfolio signals: Broken numerator and denominator; ticket-branch numerator,
merged-PR denominator and percentage. Then `decline_routing`: the four
30-day counts for declines that became a native edge, closed as a proven
defer, routed to review, or stayed blocked. Its window starts at classifier
PR #1447's merge when that is newer than 30 days ago. `unavailable`,
`partial` and `insufficient_data` are unknown, never zero; an unclassified
decline is shown separately rather than assigned to a route.

Then `main_ci`, whenever non-empty: each red member-repo `main` with its
`repo`, `sha`, failing `job` and `verdict`. An `infra` verdict is a run that
never really ran and is worth a rerun; `real` needs a person. A `null`
section means the live read was not made — unknown, never green.

Then `status_state_mismatches`, whenever non-empty: each `ref` with its
GitHub `state`, Project `status` and the `mismatch` in words. A closed item
at a non-terminal Status surfaces here and in no lane list.

Then `member_issues_without_project_items`, whenever its `issues` list is
non-empty: each `ref` and `title`. Detection only; nothing is added at
`Ideas`, because being outside the Project can be deliberate. A `degraded`
status is an unread scan, never zero.

Then the gate counts (`counts_by_gate`) on one line. Then anything unusual,
and only if present: `prose_dependencies`, `suspected_human_steps`,
`unclassed_captures`, `needs_class`, `stale_locks_taken_over`, `stranded`,
`event_block_inconsistencies`, `in_motion` with `wip_limit`,
`awaiting_breakdown`, `unattended_merges`, `run_summary`, `agent_health`,
`resend_ratio`, `rejected_merges`, `degraded`,
`closed_with_access_vocabulary`. A suspected human step is report-only: do
not clear its `blocked` label, restate it, or split it here.

Show `run_summary` per agent: starts, ordinary finishes, same-session
re-begins kept visibly separate; a `skipped-blocked` row is a re-begin only
with `re_begun_by`.

For `unattended_merges`, call out `self_reviewed: true` as self-reviewed,
derived from authoring versus reviewing run agents, never from the account.

Use `timings` diagnostically when slow: name the largest stage including
Project load and each `graphql.<operation>` aggregate. A session reply
timeout means the session was busy.

When `degraded` is non-empty, name the over-budget sections: the brief is
partial. Never merge on a partial or missing `rejected_merges` value; the
merge gate reads the counter itself.

For `unclassed_captures`, show each Idea's origin: agent-origin is the
shaping agent's to class; Nate-origin and unknown are his. Diagnostic only.

`prose_dependencies` is report-only: ticket `ref`, named open issue refs,
the `sentence`. Unnumbered rows carry empty `names`: a person reads the
sentence before choosing an edge. Never write a native edge here.

Say "Nothing is waiting on you" when `total_needing_nate` is 0, then stop.
Name parking for long-waited items (`funnel.py park <ref> --reason "<why>"`;
Parked requires a reason). Flag `maintenance_load` `upkeep_share` above ~0.5
as a plates-spinning signal, never a tuning task. `upkeep_share` null is
unknown, not healthy. Show `disposal` `finished_vs_abandoned` beside it with
`done`, `parked`, `net_open_growth`, no targets. Three stale takeovers in a
week means runs are dying; one is noise.

Offer the `launch` command for the top item. Do not run it.
"""


def render_template() -> str:
    """Return the rendering template the /funnel skill applies."""
    return RENDER_TEMPLATE


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], ["--list"]):
        print("usage: funnel_render.py [--list]", file=sys.stderr)
        return 2
    sys.stdout.write(RENDER_TEMPLATE if RENDER_TEMPLATE.endswith("\n")
                     else RENDER_TEMPLATE + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
