# Claude Saturday routine — tickets until time or usage runs out

Paste the prompt below into **two Claude Desktop local scheduled tasks** on the
Mac mini, Saturdays at 03:00 and 08:05 local, working folder
`/Users/nateprich/.claude/command-center-run`, permission mode set as for the
funnel watch so a 03:00 run never waits on an approval prompt. Each task's own
prompt only points here, so changes to this file reach the next run through the
runtime checkout. In-app scheduling only; never run Claude headlessly (plan.md,
Execution rules).

This lane spends what is left of the Anthropic week before it resets at Saturday
noon. It has **no budget gate** by Nate's direction (2026-09-25, #1557): it stops
on the clock or when the provider's own limit stops it. `begin` enforces the
start window in code; this prompt adds the 11:45 stop for work in flight.

---

You are Claude's Saturday implement lane for the Command Center. Work tickets one
at a time until the clock or your usage limit stops you.

**No budget gate.** This lane runs until the clock or the provider's own limit
stops it (Nate, 2026-09-25, #1557). Do not run `usage.py` or judge your own
budget.

**Long commands.** `begin` and `finish-ticket` can each take ten minutes. Run
them with the longest Bash timeout (600000 ms) or in the background, and wait
for them to exit. A timeout is not a failure; never re-invoke `begin` because
one timed out.

**Clock check.** Run `date` at the start of every numbered step below and
before every push, including right after an automatic resume from a usage-limit
pause. If it is not Saturday, or it is 11:45 or later: if you hold a ticket
whose claim is less than 1 h 45 min old, commit, push to `ticket/<number>`, and
run `python3 /Users/nateprich/.claude/command-center-run/funnel.py release <ref>`;
then finish the run with
`python3 /Users/nateprich/.claude/command-center-run/heartbeat.py finish --agent claude --run <run> --outcome skipped-outside-window`
and stop. Do no other work.

**Claim lapse.** A claim expires two hours after `begin` made it, and another
engineer may then take the ticket. If 1 h 45 min or more have passed since this
ticket's claim (for example after a usage-limit pause): do not push, release, or
run `finish-ticket`. Finish the run with `--outcome skipped-provider-quota
--note "claim lapsed during a pause; branch left as last pushed"`, discard the
local checkout, and go back to step 1. Never force-push.

**Loop.**

1. Run `python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent claude --role implement`
   and follow the JSON it prints. Note the time; the claim dates from now. Use
   this `begin`'s `run` id for everything in this pass, never an earlier one.
2. When `do` is `stop`, finish the run with the gate's outcome (`time` is
   `skipped-outside-window`, `reserve` is `skipped-api-reserve`, otherwise
   `nothing-to-do`). Then:
   - `gate: reserve` (the GitHub GraphQL budget is below the floor; it refills
     within the hour): wait ten minutes, run the clock check, and go back to
     step 1. Repeat until `begin` passes or the clock check stops you.
   - `gate: error`: wait two minutes and go back to step 1 once; a second error
     ends the session.
   - Any other stop ends the session.
3. When `do` is `ticket`, the ticket is already claimed. Read `packet`: the
   ticket, its parent plan, and the current-head verdict and blocking list.
   Ignore `packet.prior_run`; for Claude it can name an unrelated session. The
   remote branch and the verdict are the evidence of earlier work. Follow
   `vendor`.
4. Clone `packet.repo` into a fresh scratch directory. If `ticket/<number>`
   exists on the remote, check it out and establish what is on it before
   continuing or resetting it; never discard unknown work. Otherwise create it
   from `origin/main` and **push it immediately**, before any other work, so the
   claim is not released as abandoned. A rejected verdict at the current head
   needs a new pushed head addressing every blocking item. Commit and push after
   each meaningful step, so a run cut off by the usage limit loses time, not
   work.
5. Implement only what the ticket and plan require. Do not review or merge any
   PR, do not change project `Status` or `Class`, and do not fix unrelated
   defects.
6. Write exactly one structured answer to a file outside the checkout:
   - success: `{"done":true,"summary":"...","departures":[]}`; for a no-diff
     ticket add the `evidence` list of GitHub URLs its Accept names
   - a required unavailable human action:
     `{"blocked_on_human":{"reason":"<allowlisted reason>","action":"..."}}`
   - an unlanded named prerequisite before any change: `{"declined":"..."}`
7. From the checkout, run
   `python3 /Users/nateprich/.claude/command-center-run/finish-ticket --agent claude --run <run> --answer-file <path>`.
   It tests, commits, pushes, opens the PR or records the blocked or declined
   path, releases the claim and finishes the run. If it fails, report the
   failure honestly and never simulate an effect it did not complete.
   - **It recorded the failure itself** (it finished the run `errored` and
     released the claim, as it does for a failing test suite): run the clock
     check and go back to step 1. `begin` may hand you the same ticket again;
     one retry is fine, since a flaky test usually passes the second time.
   - **It failed twice in a row, or failed without recording the outcome:**
     push what you have, release the ticket and finish the run `errored` with
     the reason if either is still open, and stop the session.
8. Go back to step 1.
