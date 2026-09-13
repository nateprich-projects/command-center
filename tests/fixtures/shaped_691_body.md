# The escalated Muse reviewer never reviews: begin sends it ticket jobs it cannot run, so escalated PRs wait and each claim is abandoned

Class: Broken (already set at Ideas; this plan does not change it).

## What the thing is

The hourly escalated reviewer (com.nateprich.command-center-muse-review, scripts/muse-review escalated) has reviewed nothing since #617 landed (b17a633, 2026-09-11 08:40 PDT). Its err log carries seven copies of muse-review: funnel begin returned unknown job "ticket".

Cause, read from code on d215cfc and recorded in the issue: AGENTS_BY_ROLE["implement"]["muse"] holds escalated (funnel.py:824). In cmd_begin, agent_has_role(agent, "implement", tier) is checked before the review queue is built, and that branch always returns do="ticket" or do="stop". So funnel begin --agent muse --tier escalated can never reach review_queue(items, "escalated"). scripts/muse-review accepts only review|breakdown|shape (line 236) and exits 1, leaving a heartbeat start with no finish.

Consequences already measured: escalated PRs #621 (ticket #563, open since 2026-09-11 01:09 PDT) and #625 (ticket #564, open since 2026-09-12 13:25 PDT), both green and CLEAN, zero reviews; each ticket job is a real claim then abandoned, feeding the muse started and never finished health line and the #677 stale-lock takeover; the implementation consumer (scripts/muse-implement, PR #625) is not on main and its plist is not installed, so nothing on this Mac can act on a Muse ticket job.

After this lands, funnel begin from the review runner offers an open escalated PR when one waits, the implement runner keeps the ticket path that test_begin.py:601 pins, and an unexpected job from begin finishes honestly instead of abandoning a claim.

## The work

1. Give cmd_begin an explicit caller role so the two Muse escalated callers diverge: the review runner stays on the review path and the implement runner stays on the ticket path. Keep the default path unchanged for existing callers that do not pass a role, so Codex and current tests keep current behavior until they opt in.
2. Update scripts/muse-review (escalated and standard schedules) to pass the review role on its begin call, and to finish the heartbeat on any unexpected job instead of bare exit 1 after start (errored or skipped-blocked with a finish, never start without finish).
3. Pin both directions in tests: begin from the reviewer role with an open escalated PR returns do="review" naming that PR; begin from the implement role keeps claiming a ticket as test_begin.py:601 pins; the muse-review unknown-job path calls finish rather than abandoning the run.
4. No manual cleanup of the abandoned claims named in the issue (#564 runs 276919d144b5, 6f316e7136ee, 99778fd3b335, ec2094b7977e, a01a7f0236a0; #677 runs 716937a65385, f2597f03c84e). Existing stale-lock and heartbeat expiry reconciles them; the first review run after the fix confirms the queue moves and the health line clears.

## Decided from precedent

- Muse keeps every review, breakdown and shaping, and implements escalated-tier tickets only under the #529 lane, sequenced after #410 with retirement of Sol after the first Muse escalated merge. This fix restores the review half of that split without advancing the implement half, which stays behind its #529 sequence and its uninstalled plist.
- funnel begin keys the ticket path on role, not agent name (#529, Decided by the agent). An explicit caller role is that decision applied to the reviewer versus implementer collision the issue records.
- One ticket per run and scope is the ticket (routines/codex-work.md convention cited in #529). The reviewer claiming a ticket it cannot run violates that convention; keeping the reviewer on the review path restores it.
- test_begin.py:601 pins the implement behavior. The new test pins the reviewer direction alongside it rather than replacing it, cited from the issue measurement, not re-derived.

## Decided by the agent

- Explicit caller role on begin rather than reordering cmd_begin to try the review queue first, because review-first merely mirrors the starvation: once the #529 implement lane is live, an implement runner would be handed review jobs it cannot run, and a reviewer with no open PRs would still need a defined fallback. The role makes the caller intent explicit at the call site where the two schedules already differ. Rejected: review-before-ticket reorder in cmd_begin.
- Default-preserving role (absent role keeps current agent_has_role behavior) rather than a required flag on every caller, because Codex callers and existing tests use begin today and a hard requirement would break them at the gate. Rejected: mandatory role on all begin calls.
- Honest finish on unexpected job in scripts/muse-review rather than mapping unknown jobs to stop or nothing-to-do, because a ticket job sent to a reviewer is a routing defect that must stay visible as errored, not vanish as idle. Rejected: treating unknown job as a quiet skip with outcome nothing-to-do.

## Rejected

- Reordering cmd_begin to always prefer review for agent muse tier escalated. That fixes today by breaking the #529 implement lane tomorrow.
- A mode switch on scripts/muse-review carrying both safety shapes. The review script stays read-only under --disable-write; the implement runner keeps its own script and writable clone root per #529. One script carrying both risks a flag error turning a reviewer into a writer.
- Adding or agent == muse inline where agent == codex is today. That repeats the literal trap #529 rejected in favor of a role registry.
- Manually closing or force-finishing the abandoned heartbeat runs named in the issue. Expiry and stale-lock takeover already own that path; manual writes would hide whether the fix stops new abandonments.
- Unblocking or installing the muse-implement consumer here. PR #625 and its plist stay on the #529 sequence; this plan only stops the reviewer from claiming work meant for that lane.

## Needs you

- Exposure: nothing outstanding. No new credentials or reachable surface.
- Gates: nothing outstanding. No gate ownership changes.
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.

## Overlap check

Checked: #25, #646, #685 (the other open Shaped items in funnel brief), #529 (the governing Muse lanes plan), and #130, #131, #490, #561, #614, #643, #667, #675 named by the advisory scan.

Candidates:
- nateprich-projects/command-center#691 and nateprich-projects/FF-Weekly-Start-Sit#25 both name shape()
- nateprich-projects/command-center#691 and nateprich-projects/command-center#130 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#131 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#25 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#490 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#529 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#529 both touch routines/codex-work.md
- nateprich-projects/command-center#691 and nateprich-projects/command-center#529 both reference #410
- nateprich-projects/command-center#691 and nateprich-projects/command-center#561 both name shape()
- nateprich-projects/command-center#691 and nateprich-projects/command-center#614 both name shape()
- nateprich-projects/command-center#691 and nateprich-projects/command-center#614 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#643 both name name()
- nateprich-projects/command-center#691 and nateprich-projects/command-center#643 both touch funnel.py
- nateprich-projects/command-center#691 and nateprich-projects/command-center#646 both name Broken()
- nateprich-projects/command-center#691 and nateprich-projects/command-center#667 both name ticket()
- nateprich-projects/command-center#691 and nateprich-projects/command-center#675 both touch funnel.py

Conclusion:
- Keep the plan as written. The #529 lines record the governing split this fix follows: same funnel.py area and codex-work.md convention, same #410 sequence. The remaining funnel.py lines are the shared-file signal: this plan changes begin routing while siblings touch different paths. The shape, name, Broken, and ticket matches are incidental prose. #646 is the shaped write-path false report on a different transition.

<!-- command-center-provenance -->

```json
{
  "agent": "muse",
  "at": "2026-09-13T03:51:00.207161+00:00",
  "run": "8753e14931b1",
  "voice": "agent"
}
```

<!-- command-center-origin -->

```json
{
  "agent": "claude",
  "at": "2026-09-13T03:43:35.891306+00:00",
  "run": null,
  "voice": "agent"
}
```
