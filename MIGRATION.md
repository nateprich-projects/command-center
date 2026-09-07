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
4. ~~**Sol's cost case for Z.ai is weaker than stated.**~~ **Withdrawn 2026-09-06.** The
   original note said the bottleneck was a constant and changing it was free, so try that
   before subscribing. That was wrong, and the experiment proved it: raising `WEEKLY_FLOOR`
   to 50.0 did let the routines run, by spending Nate's own week on them. A floor does not
   add capacity, it chooses who goes without. Sol's cost framing was imprecise but its
   conclusion was right — **a second provider is the only thing that creates bandwidth**,
   and that is what P3b is for. `plan.md:683`'s "~2,700 output tokens" still stands and
   still means the money is small; the *quota* is what is scarce.

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
- [x] **0.2 Per-provider budgets in `usage.py`.** — done. `PROVIDERS` maps agent →
      pool, `agents_on` inverts it, an unregistered agent or a registered provider
      with no reader fails closed, and `DOWNSTREAM_RESERVE` holds back 20% of a
      **shared** pool so implementation cannot spend the credits its own review
      needs. Dormant today — neither agent shares a pool — and it is the shape
      z.ai requires, since GLM in Codex and GLM in Claude Code would draw one
      budget. `tests/test_providers.py`.

      **Still needed for z.ai:** a reader. `usage.py` can only gate a provider it
      can measure; whether z.ai exposes remaining credits is unconfirmed, and if
      it does not, its budget state degrades to permanently fail-closed.
      *Superseded:* `read_agent` and the gate hardcode
      `claude` and `codex` (`usage.py:568-592`). Generalise to a provider registry so a
      third pool has its own budget state. This is the half of Sol's §6 worth keeping, and
      it is what answers `plan.md`'s original objection that a cheaper routine would be
      "invisible to the gate governing it".
      *Blocks: P3b, P4.*
- [x] **0.3 `WEEKLY_FLOOR`: raised to 50.0, then REVERTED to 25.0 the same day.**
      It worked — the routines reviewed and merged PR #36, the first end-to-end
      cycle — and it was still the wrong fix. **It did not create bandwidth; it
      moved Nate's own weekly budget to the automations.** Same contention, pointed
      the other way. He does his real work on this subscription and the routines are
      not entitled to it. This number can never solve routine starvation, because
      moving it only decides who goes without. That makes P3b the critical path
      rather than an optional cost saving. Currently 25.0. A scheduled Claude run refused at
      25.1% on 2026-09-06 — one tenth of a point — because interactive use had crossed it.
      One constant, reversible, and it tells us whether contention was ever the real
      problem before any subscription is bought. *Needs N's number, or his say-so to pick.*
      *Blocks: nothing. Unblocks Claude routines immediately.*
- [x] **0.4 Make the drift check blocking.** — done. `tests/test_automation_drift.py`
      fails when any Codex automation no longer matches `routines/codex-work.md`,
      and skips where the automations do not exist (CI, a fresh clone) so it is a
      real check on the machine that runs the schedules. `routines/claude.md`'s
      merge bar already requires the tests to pass, so drift now blocks a merge
      without anyone remembering to look. CI cannot do this — the automations live
      in `~/.codex`, not the repo. Proved its worth immediately: all five had
      drifted again within hours of the last sync, silently, because the routine
      was edited afterwards.
      *Superseded:* `scripts/sync_codex_automations.py --check`
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
      **Unblocked 2026-09-06.** The earlier "no Luna" reading was wrong — it came from
      grepping session transcripts rather than `~/.codex/models_cache.json`, which is the
      actual model list. Selectable ids, with their max reasoning level:
      `gpt-6-astra` (ultra), `gpt-5.6-sol` (ultra), `gpt-5.6-terra` (ultra),
      `gpt-5.6-luna` (**max**, no ultra), `gpt-reserve` (max), `gpt-5.5`, `gpt-5.4-mini`.
      Luna and `gpt-reserve` share the description "Fast and affordable agentic coding
      model"; Sol is the "reliable agentic workhorse".

      **Default switched on N's instruction:** `gpt-5.6-sol`/`high` → `gpt-5.6-luna`/`max`
      ("Luna-maxing"). Previous config backed up alongside `~/.codex/config.toml`.

      **Escalation half built 2026-09-06** (`funnel.py`: `escalation_reasons`,
      `required_tier`, `funnel next --tier`). A ticket declares `Risk: standard`
      or `Risk: escalated — <why>` in its body, written by Claude at breakdown;
      the marker is authoritative and a deliberately narrow pattern list is the
      safety net for tickets written before markers existed. The patterns are
      narrow on purpose: this repo is *about* locks, gates and destructive
      operations, so a broad list escalates every ticket and the cheap engine
      never runs — the failure that looks like success. `tests/test_escalation.py`
      pins that with real ticket text.

      **Wired 2026-09-06.** `routines/codex-work.md` asks `funnel next --tier
      standard`, so the cheap default walks past work it may not take, with a new
      exit case for "every startable ticket needs the escalated engine" — a
      healthy skip, not an error. `routines/claude.md` now writes a `Risk:` line
      into every ticket at breakdown, with the criteria and the reason it is
      breakdown's job: it has the plan in front of it and the engineer does not.
      All five automations synced.

      **The one part that needs N:** a second Codex schedule on `gpt-5.6-sol`
      passing `--tier escalated`. Until it exists, escalated tickets are correctly
      refused by Luna and worked by nobody — safe, but stalled.

- [ ] **3b. Routine work on a separate pool — THE CRITICAL PATH.** *Reader done
      2026-09-06; scheduling and job assignment remain.*

      Not a cost saving. The automations and Nate compete for one Anthropic
      subscription, and settling that inside one pool only picks a loser: at
      `WEEKLY_FLOOR` 25 the routines starve, at 50 he does.

      **The harness is zcode, not the Claude Desktop gateway.** Nate chose z.ai
      but declined to route Claude Desktop through it — that would have made his
      interactive Opus work GLM too. zcode is a separate app with its own
      in-app scheduling, so `AGENTS.md:50` stands unamended and nothing runs
      headless. The Desktop gateway work is superseded; its findings survive in
      `LEARNINGS.md`.

      **Done:** `PROVIDERS` maps `zcode → zai`; `read_zai()` reads real quota
      from `https://api.z.ai/api/monitor/usage/quota/limit`; `detect_model`
      reads `~/.zcode/cli/rollout/*.jsonl`, which carries `model.modelId`,
      `model.role` and a full token breakdown including cache; heartbeat records
      `harness`; both CLIs take their agent list from `PROVIDERS`, so a provider
      brings its own surface.

      **Do not compute credits from z.ai's published multipliers.** Tried:
      `(fresh x 6.9 + cached x 1.7 + output x 24) / 10,000` scored a session at
      23.5 credits that the API scored at **3**. Roughly 8x wrong, in the
      direction that starves the pool for nothing. Read the API.

      **Measured, first data:** caching survives — 114,048 of 117,104 input
      tokens were cache reads, a 97% hit rate, so the 4x-burn fear is dead. Plan
      is `lite`: 2,000 credits per five hours, 10,000 weekly, and a whole
      exploratory session cost 3.

      **Remaining, and it needs N:** a zcode schedule that runs the Claude
      routine's jobs, and a decision about which jobs move — breakdown and
      routine review are the candidates, per Sol's stage map. Then `plan.md`
      675-701 is overturned honestly, because per-provider budgets now answer
      its objection that a cheaper routine would be "invisible to the gate
      governing it".

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
2. ~~**Open the Codex model picker and report what is selectable.**~~ Done 2026-09-06 —
   read from `~/.codex/models_cache.json` rather than needing N. Luna exists; default is
   now `gpt-5.6-luna`/`max`. What remains is engineering (escalation), not N's account work.
3. **Decide Z.ai Lite: buy or not.** If yes, subscribe and confirm Claude Code can run
   GLM-5.3 Max here. If no, P3b is dropped and 0.3 carries the contention fix alone.
4. **Give a `WEEKLY_FLOOR` number, or say "you pick".** Recommendation: 40.0. It clears the
   observed refusal with margin; the proportional line still governs later in the week. The
   trade-off is that routines may spend more of the week early.

Items 2 and 3 are the only true blockers. 1 and 4 are cheap and unblock immediately.
