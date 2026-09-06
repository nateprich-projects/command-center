# Model-routing migration — working plan

**Temporary.** Delete this file when the last phase is done and the outcome is recorded in
`plan.md`. It exists so a compacted session can pick up mid-migration without re-deriving
anything.

**Started:** 2026-09-06. **Funnel process deliberately overridden by Nate** for this work:
it is not going through gates or tickets, because the funnel cannot reliably run until it
is finished. Recorded here so the exception is visible rather than looking like drift.

## Where this came from

Sol produced `command-center-model-routing-handoff.md` (in `~/Downloads`). Its architecture
read is correct and its stage map is broadly right. Four corrections were agreed before
starting:

1. **Phase 0 is not skipped, but it is not a project either.** Sol framed "prove the
   pipeline end to end" as blocking work. It is now nearly free: Codex reaches GitHub, the
   sandbox is scoped, and `funnel next` returns #19. It happens on its own the first time a
   five-hour window opens untouched. It blocks no engineering below — it only blocks
   *trusting* the results.
2. **`plan.md:675-701` already rejected splitting jobs across models**, and its stated
   precondition still holds: *"Nothing in this system has yet completed a single end-to-end
   cycle; that is the wrong moment to double its moving parts."* The rejection expires on
   its own terms once one cycle completes. Overturning it needs a deliberate `plan.md`
   revision, not quiet adoption.
3. **Sol's §6 is half right.** Per-provider budget state is needed. Separating "presence"
   from quota is not: the five-hour rule is a *contention* gate, not a presence gate. It
   asks "is Nate using the OpenAI pool right now" and declines to compete for that pool.
   That generalises correctly — GLM work never touches the OpenAI window, and if Nate stops
   using ChatGPT there is nothing to protect, so running is right rather than a failure.
4. **Sol's cost case for Z.ai is weaker than stated.** `plan.md:683` measured these runs at
   ~2,700 output tokens; breakdown and review are light. Z.ai buys *decoupling* from Nate's
   interactive pool, not money. The observed bottleneck — a run refusing at 25.1% against
   25.0% — is a constant, and changing it is free. Try that first.

Sol's §4 — splitting model review from a deterministic merge gate — is the most valuable
item in the document and was underweighted at position four. It is promoted to P2 here.

## Phases

Owner is **C** (Claude, this session) or **N** (Nate). A phase blocks only what its
"blocks" line says.

### P0 — Foundations · C · no dependencies

- [x] **0.1 Heartbeat telemetry.** — done, `tests/test_telemetry.py`, smoke-tested live. Add `provider`, `model`, `reasoning_effort`,
      `attempt`, `escalated_from`, `ci_green`, `review_result`,
      `human_intervention_required` to run records. Must land **before** any routing
      change, or the first multi-model runs are unmeasurable and routing goes back to
      being decided by public benchmarks.
      *Blocks: P3, P5.*
- [ ] **0.2 Per-provider budgets in `usage.py`.** `read_agent` and the gate hardcode
      `claude` and `codex` (`usage.py:568-592`). Generalise to a provider registry so a
      third pool has its own budget state. This is the half of Sol's §6 worth keeping, and
      it is what answers `plan.md`'s original objection that a cheaper routine would be
      "invisible to the gate governing it".
      *Blocks: P3b, P4.*
- [ ] **0.3 Raise `WEEKLY_FLOOR`.** Currently 25.0. A scheduled Claude run refused at
      25.1% on 2026-09-06 — one tenth of a point — because interactive use had crossed it.
      One constant, reversible, and it tells us whether contention was ever the real
      problem before any subscription is bought. *Needs N's number, or his say-so to pick.*
      *Blocks: nothing. Unblocks Claude routines immediately.*
- [ ] **0.4 Make the drift check blocking.** `scripts/sync_codex_automations.py --check`
      exists but nothing runs it. Add it to CI and to the Claude routine's review step.
      Sol's §5 asked for "loud and blocking"; today it is neither.

### P1 — Prove the pipeline · N + wait · parallel to everything

- [ ] Codex claims **#19**, opens a `ticket/19` PR, Claude reviews and merges it.
- [ ] `funnel show 2` then shows a real merged PR rather than "closed with no ticket/* PR".

Not a work item. It happens when a five-hour window opens untouched. **N's part: leave the
ChatGPT window alone for one window.** *Blocks: trusting P3's results, and accepting #2.*

### P2 — Deterministic merge gate · C · after P0.1

- [ ] **2a. Structured review artifact.** The reviewing model writes a verdict — ticket
      satisfied y/n, CI status, blocking findings, non-blocking findings, risk class,
      approve/reject — rather than prose plus an action.
- [ ] **2b. Merge gate in Python.** Checks branch matches ticket, CI green, review artifact
      exists and approves, lock state valid, no policy gate violated. Then it merges.

A model judging a change is doing what only a model can. A model typing `gh pr merge` is a
model trusted with an irreversible act for no reason. This also directly addresses what
keeps #2 unaccepted — unattended merges that cannot be audited.

### P3 — Routing · needs N's account work

- [ ] **3a. Codex engineer routing.** Default engineer becomes a cheaper model, with
      deterministic escalation to `gpt-5.6-sol` for: auth, security, migrations,
      destructive ops, concurrency, large multi-component changes, weak acceptance
      criteria — and after one failed attempt. **The model must not decide its own
      escalation**; `funnel.py` or ticket metadata does.
      **BLOCKED:** the only model ids present in Codex's state are `gpt-5.6-sol`,
      `gpt-5-6-thinking`, `gpt-6-astra`. **No Luna.** N must confirm what is actually
      selectable and its exact id.
- [ ] **3b. Claude-side routine work on a separate pool.** Move breakdown and routine
      review off Opus onto an independent quota pool inside the Claude Code harness.
      **BLOCKED:** needs N's decision on a Z.ai Lite subscription, and confirmation that
      Claude Code can actually run GLM here.

### P4 — Record it · C · after P3

- [ ] Revise `plan.md:675-701`. The old rejection is overturned honestly *because* per-
      provider budgets (0.2) remove its core objection — not because it became inconvenient.
      Keep the rejected alternatives; add the new ones.
- [ ] Update `STATUS.md`, and `LEARNINGS.md` for anything measured.
- [ ] Delete this file.

### P5 — Measure · after real runs

Optimise for **green approved PRs completed without human intervention per unit of
subscription quota**. Derived: first-attempt success, CI-green rate, review pass rate,
escalation rate, human-intervention rate, quota per successful PR. Do not tune routing from
benchmarks once this dataset exists.

## Nate's list, front-loaded

Ordered by how much they unblock, most first.

1. **Leave the ChatGPT five-hour window alone for one window.** Zero effort, unblocks P1
   and the evidence #2 needs. Everything else can proceed while this happens.
2. **Open the Codex model picker and report what is selectable, with exact ids.** P3a is
   blocked on this and I cannot see it. If there is no Luna, P3a becomes "is
   `gpt-5-6-thinking` the cheaper default" instead.
3. **Decide Z.ai Lite: buy or not.** If yes, subscribe and confirm Claude Code can run
   GLM-5.3 Max here. If no, P3b is dropped and 0.3 carries the contention fix alone.
4. **Give a `WEEKLY_FLOOR` number, or say "you pick".** Recommendation: 40.0. It clears the
   observed refusal with margin; the proportional line still governs later in the week. The
   trade-off is that routines may spend more of the week early.

Items 2 and 3 are the only true blockers. 1 and 4 are cheap and unblock immediately.
