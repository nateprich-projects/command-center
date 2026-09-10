# Status

**Last updated: 2026-09-09** — by Claude, on Nate's delegation, after a stabilisation
pass with every scheduled agent paused. The section below is where things stand now;
everything from "The command surface" onward is the 2026-09-05/06 trial record, kept
as history and not re-verified today.

## Where this stands, in one place

**The pipeline is unwedged and the automations are paused, waiting for Nate to turn
them back on.** 573 tests pass on the Mac mini from the run clone, including the three
machine-local install checks (`test_launchd_drift`, `test_automation_drift`).

**What was wedging it, and what fixed each:**

| Wedge | Fix | Verified by |
|---|---|---|
| Routines executed `funnel.py` from Nate's working tree, which sat on `ticket/346` — so they ran code predating #349 while `main` moved on (#171) | #204/#315 installed `~/.claude/command-center-run`, kept at `origin/main` every five minutes by launchd; #205 repointed every routine, `scripts/muse-review` and both Muse plists at it | `doctor`'s checkout-staleness check counts zero legacy invocations and retired itself |
| Nothing wrote `Building` after #287 deleted the `start` gate, so every `Ready` project was unstartable (#343) | #348: `startable()` admits `Ready` parents and the shared `claim_ticket()` promotes on first claim — for `funnel begin` and `funnel claim` alike | The merge gate refused #348 itself with "project is not Building" until a claim promoted #343; nine Ready projects now expose startable tickets |
| Four of five Codex automations ran the pre-#349 prompt, recording correct declines as `errored` — 34 of 72 errored runs in 24h were one prose dependency on #77 (#178) | #324 reconciled (nothing in the stale copies worth keeping), #325 synced all five from the run clone; `status = "PAUSED"` preserved | `sync_codex_automations.py --check`: 0 of 5 drifted, re-checked four minutes later |
| A merge refused on a conflicting branch stranded the ticket (#168) | #342: the gate writes its own `rejected` verdict so `awaiting_review` hands the ticket back | fixture tests in `test_merge_gate.py` |
| #204 and #315 blocked on each other; #207 and #205 carried blocks whose conditions had landed | cleared, with the reasoning on each issue | — |

**Watch for:** `plan.md` and the routines say the alert path for a dying agent is TickTick;
Nate overturned that on 2026-09-09 — #176 now routes watchdog conditions into the `/funnel`
brief as `agent_health` (#207, head of the standard queue). Until it lands, the Actions
watchdog still only writes #160, and it will keep alarming for a week on the 72 historical
errors.

**zcode is retired** (2026-09-09, #431): Muse's standard schedule carries every job it had;
`heartbeat.RETIRED_AGENTS` keeps its silence out of the watchdog and the brief.

**Waiting on Nate:** the accept gate for #171 (all tickets closed). #343 has one ticket
left (#347). The re-enable checklist is in the 2026-09-09 hand-off in this repo's
issue history; the one paste he owes is the zcode prompt, whose `--routine-sha` changed.

**Still open and deliberately untouched:** PR #335 (docs, his to merge), PR #341
(follow-up on a closed ticket, invisible to the review queue by construction), PR #13
(Dependabot), and #25's block, which he reconfirmed on 2026-09-08.

**Known cost:** `funnel begin` loads the Project before its budget gate, so a refused
five-minute poll still spends ~20 GraphQL points. Roughly 500-600 points an hour across
the four schedules, against a 5,000-point limit — fine, and the overnight exhaustion that
produced Muse's rate-limit errors was the #77 decline storm, not the polling.

---

## The command surface

Nate's, from any Claude Code session on this Mac:

| Command | What it does |
|---|---|
| `funnel brief` / `queue` | what is waiting, bottom-up |
| `funnel show <n>` | the evidence for one item's gate |
| `funnel approve\|start\|accept <n> --yes` | answer a gate — **his decision**, an agent may execute it on his explicit instruction |
| `funnel ideas` | captured ideas, flagged first |
| `funnel capture "<title>" --origin <nate-relayed|agent>` | take an idea down from chat with explicit origin |
| `funnel shaped <n> --plan <file>` | record a grilled plan, → Shaped |
| `funnel reject <pr>` | a merged PR was broken; undo and count it |

The agents': `funnel next`, `claim`, `release`, `usage.py gate`, `heartbeat`,
`prior_run`. **`park` is not built** — it is ticket #19, and it is what `funnel next`
currently hands Codex.

`scripts/sync_codex_automations.py --check` reports whether the five Codex automations
still match `routines/codex-work.md`. Worth running before trusting an overnight schedule:
the routine file is not what runs, and all five had drifted from it by 2026-09-06.

v0 in progress. Target: shipped in under two weeks from 2026-09-05.

## Verified on 2026-09-06, not merely built

Measured in the wild, not in fixtures. Each of these had never actually run before.

- **Codex reaches GitHub and records heartbeats.** Its history was empty since the system
  was built; run `f0474353cee2` is its first surviving start/finish pair.
- **The idle gate refuses correctly.** `37.0% of the five-hour window spent OVER — window
  already in use when it began — Nate is working`, exit 1.
- **The sandbox is scoped as intended.** The canonical checkout is not writable
  (`Operation not permitted`); the heartbeat spool is. Both were exactly inverted earlier
  the same day, and Codex had been doing ticket work directly in Nate's working tree.
- **A lost heartbeat says so.** Two records were lost silently that morning while GitHub
  was reachable, reported as "spooled locally". A record now goes straight to GitHub when
  the spool cannot be written, and is called lost only when both routes fail.
- **The run-id fix holds.** `#26` shipped outside the pipeline on Nate's explicit
  instruction — recorded on the issue as a deliberate exception, since work closed without
  a `ticket/*` PR is exactly what makes v0 hard to accept.

**Still unverified, and the reason #2 is open:** no Codex run has worked a ticket, no PR
has been reviewed, and nothing has been merged unattended. Breakdown is the only stage an
agent has genuinely completed.

## Built and verified

**Deliverable #1 — GitHub setup.** Complete.

- **Project: [Command Center](https://github.com/users/nateprich/projects/2)** (user-owned,
  `#2`, private). Not Project 1 — see `plan.md`, Verification status item 7.
- `Status` single-select: `Ideas → Shaped → Ready → Building → Done → Parked`, in funnel
  order, each with its meaning as the option description.
- `Class` single-select: `Broken → Maintenance → Improve → New → Replace`, in ladder order.
- Labels `needs-shaping` and `blocked` on this repo, and GitHub's ten stock labels
  deleted. That is the complete label set.
- Topic `command-center` on this repo — the funnel's membership gate.

**Time-at-gate is real.** `ProjectV2ItemStatusChangedEvent` observed firing on
`#1` with a usable `createdAt`. The issue-number tiebreak stand-in was never needed.
Evidence in `LEARNINGS.md`.

**Deliverable #2 — `funnel.py`.** `queue`, `next` and `brief` all run against live
GitHub. 38 fixture tests on the ordering rules, mutation-checked: reversing the bottom-up
order, letting an unset `Class` rank as `Broken`, and making `children_all_closed`
vacuously true for a childless item each fail a test.

Stdlib only, Python 3.9. Authentication is delegated to the `gh` CLI, so the program
never reads, stores or passes a token.

**The single-in-motion lock.** `In motion since` text field on the Project, written by
`funnel claim` / `funnel release`. Full cycle verified live: claim, `next` and a second
claim both refuse, release, `next` resumes.

**Deliverable #4 — `statusline.sh`.** Renders the status line and caches both rate-limit
windows. 8 tests covering the absence cases: a missing window is omitted rather than shown
as `0%`, an absent `rate_limits` renders "usage unknown" and does not erase a previous
reading, and a present payload replaces the file wholesale so an expired window cannot
linger. Written atomically via `mktemp` + `mv`.

**Installed 2026-09-05** via `scripts/install.sh`. `funnel.py`, `statusline.sh` and the
`funnel` skill are symlinked into `~/.claude`, and `statusLine` is merged into
`settings.json` (backup at `settings.json.bak.20260905-011724`). Re-running is a no-op.
Verified from the installed paths, not the checkout.

**Deliverable #3 — `/funnel` skill.** `skills/funnel/SKILL.md`. Shells out to
`funnel brief` and renders it; explicitly forbidden from ranking, reordering or filtering.

**`scripts/install.sh`.** Idempotent, symlink-based, `--dry-run` supported. Symlinks
`funnel.py`, `statusline.sh` and the skill into `~/.claude`, and merges `statusLine` into
`settings.json` key-by-key with a timestamped backup. Refuses to clobber a real file that
is not already a symlink, and refuses to touch a `settings.json` it cannot parse.

**Deliverables #5, #6, #7 — the routines and the heartbeat.**

- `heartbeat.py` — records run **start and finish separately**, each with a usage
  snapshot, to an orphan `heartbeat` branch via the Contents API (compare-and-swap on
  the blob sha, so a concurrent write is rejected rather than lost). Smoke-tested live.
- `.github/workflows/watchdog.yml` + `.github/scripts/watchdog.py` — hourly. Reports
  silence, dying runs, and repeated errors; deliberately silent on over-pace, locked and
  nothing-to-do, which are the system working. Full cycle verified live: opened an issue,
  detected recovery, closed it.
- `routines/codex-work.md` and `routines/claude-review.md` — the prompts to paste into
  Codex Scheduled and a Claude Code Routine.

## Built, not verified

- **The routines have never run.** The prompts are written and every command in them
  works, but neither has been scheduled in its app — that is a manual step in Codex
  desktop and Claude Code, and it is the last thing standing between v0 and done.
- **The watchdog has never run in Actions.** Verified by running the same script locally
  against the real heartbeat branch.
- **The `/funnel` skill end to end.** Installed and discoverable, but it has not been
  invoked in a fresh session yet — skills load at session start.
- **`funnel.py` at scale.** Exercised against a Project holding one item. Pagination,
  multi-repo membership and the 30-day maintenance window have fixture coverage but no
  live data behind them yet.

## Not started

| # | Deliverable | Blocked on |
|---|---|---|
| 3 | `/funnel` skill | #2 |
| 4 | `statusline.sh` — renders the status line, caches both rate-limit windows | — |
| 5 | Codex routine and Claude review routine | #2, #4 |
| 6 | GitHub Actions heartbeat — records outcome, not just liveness | #5 |
| 7 | Onboard this repo as the first member; file remaining work as issues | partly done |

## Blocked

Nothing.

## Trial run, started 2026-09-05

v0 is **not accepted**. Accepting something that has never executed is not much of
an accept, so it runs for a few days first and then the gate gets answered on evidence.

Three real items were placed at different stages to exercise the whole path:

| Item | Stage | Class | Exercises |
|---|---|---|---|
| [#16 `funnel park`](https://github.com/nateprich-projects/command-center/issues/16) | Ready | Improve | Breakdown, then Codex work. Highest on the ladder, so its tickets go first |
| [#17 `funnel doctor`](https://github.com/nateprich-projects/command-center/issues/17) | Ready | New | Breakdown, then Codex. Below #16, so it proves the ladder actually orders |
| [#18 multi-repo](https://github.com/nateprich-projects/command-center/issues/18) | Shaped | Improve | Nate's "is the plan good?" gate |
| [#15 self-improvement](https://github.com/nateprich-projects/command-center/issues/15) | Ideas | — | That Ideas stays silent and needs no Class |

All three are real work, not busywork: #16 is required by `plan.md` and has no
command, #17 checks an install whose parts have each already broken once, and #18
fixes a known ambiguity in `find()` that would claim the wrong ticket once a
second repo joins.

**Codex is gated until roughly Sun 6 Sep, 05:48.** Its weekly window is at 67%
used, and 67 + 15 reserved exceeds the pace line until the line rises to meet it.
Until then every Codex run should record `skipped-over-pace` and do nothing. That
is the budget gate working, not a failure — and it is itself the first thing
under test.

### Model and effort

**Pinned in `~/.claude/settings.json` for the trial:**

```json
{ "model": "claude-opus-5", "effortLevel": "high" }
```

The scheduled tasks themselves cannot set either — `create_scheduled_task` has no
parameter for model or effort, and nothing is recorded on the task, so a run
inherits the app default at the moment it fires. Pinning globally is the only
lever; there is no per-task override, and scheduled tasks do not pick up a repo's
project settings. **This pins interactive sessions too.**

The key is `effortLevel`, not `effort`, and its values are `low | medium | high |
xhigh` — the CLI flag spells the top level `max` where the setting spells it
`xhigh`.

Pinned to what was already running rather than to something better, deliberately:
the trial is measuring breakdown and review quality, and changing the model at the
same moment would make the results unattributable.

**Codex is pinned already**, in `~/.codex/config.toml`: `model = "gpt-5.6-sol"`,
`model_reasoning_effort = "high"`, confirmed across recent sessions including the
identity probe. Left unchanged for the same reason. A separate
`codex-auto-review` session runs at `low` effort alongside each real session —
Codex's own built-in pass, outside this system's control, drawing on the same
budget.

**The pin has a cost worth watching.** The rate-limit numbers are account-level,
so Nate's own interactive sessions and the routines draw on one weekly window.
Pinning interactive work to Opus at `high` raises his consumption, which raises
the number the routines gate on — trial fidelity bought at the price of the
routines being refused more often. Dropping to a cheaper model per session with
`--model` does not disturb the pin.

**Not verified:** whether changing models in the app UI writes back to
`settings.json` and silently un-pins this. `prior_run.py` reports the model and
effort each run used, so drift is detectable in the data rather than assumed
away.

Both are recoverable after the fact: Claude transcripts carry `effort` at the
record level and `model` inside the message, and `prior_run.py` now surfaces both
as `ran as: <model> (effort <level>)`. At the time the trial was set up the app
was on **`claude-opus-5`, effort `high`**.

Worth watching, because model and effort bear directly on the two jobs least
verified by tests: breakdown quality and review judgement.

### First run, 2026-09-05 — two findings

The first scheduled Claude run fired, recorded **both** heartbeats correctly, and
exited `skipped-usage-unknown`. The trial found two problems on night one.

**1. Permission prompts (fixed, twice — the first fix was incomplete).** The task
runs in `permissionMode: default`, so it prompted on tool use and waited for a
human. The folder grant Nate accepted persists (`hasTrustDialogAccepted: true`),
but the per-tool "allow once" clicks did not — `allowedTools` stayed empty.
`.claude/settings.json` in this repo was updated same-day to allow the Command
Center scripts, pytest, read-only git, and the `gh` verbs for issues and PRs.

That fix covered a bare `python3 /Users/nateprich/.claude/command-center/*`
invocation, but the routine actually runs `CC=/Users/nateprich/.claude/command-center;
python3 $CC/<script>.py ...` — a compound command that rule never matched, so
prompts continued on every step. Worse, `routines/claude.md` used `CC=~/...`
(a tilde in a variable assignment), and Claude Code will not offer "always allow"
at all for that shape — no fix to `allowedTools` could have closed the loop while
the tilde stayed. **2026-09-05, later the same day:** the routine docs now use
the absolute path, and `.claude/settings.json` has explicit rules for the
compound `CC=...; python3 $CC/heartbeat.py|usage.py|funnel.py|prior_run.py *`
shape. Not allowed: `git push`, `Edit`/`Write`, `gh repo *`.

**2. The Claude budget gate — resolved, not just worked around.** `usage.py gate
claude` originally read only the cache `statusline.sh` writes, and a scheduled
run never triggers that (the desktop app renders no status line for one), so the
gate failed closed on every run. Fixed same-day with `read_claude_local()`: when
the statusline cache is missing or stale, it estimates Opus usage directly from
Claude Code's own transcripts (`~/.claude/projects/*/*.jsonl`), which a scheduled
run writes just like any other session. Capacities are calibrated against the
real usage panel (see the constants and their comments in `usage.py`).

**Verified live, 2026-09-05 ~10:31 local:** `usage.py gate claude` returned real
numbers with no cache present — `five_hour 37.6%`, `seven_day 63.8%`, exit 0 —
and the weekly reset it computed (12:00 local) landed within a minute of the
actual subscription reset. The estimate cannot see claude.ai or mobile usage on the same subscription, so it
reads **lower** than reality — which is the *dangerous* direction, not a chosen
safety margin. Every other decision here errs the other way on the principle that
erring high costs a refused run while erring low costs the week.

Two things hold it in check. The capacities are calibrated from the real usage
panel using these same token counts, so the systematic part of the blind spot is
already absorbed into them — which is why `ESTIMATE_HAIRCUT` is 1.0 rather than a
multiplier stacked on top. And the reserves carry the remaining margin. What is
*not* covered is variance: a week with heavy claude.ai use would drift, and the
check for that is comparing `python3 usage.py claude` against the panel
occasionally. If they diverge, re-derive the capacities rather than reinstating a
haircut.

Residual gap: an account that has done *no* Claude Code work at all in the
trailing 5h/7d has no transcripts to estimate from, and the gate still fails
closed (exit 2) in that case. Narrow, and unlikely to bite given how much of this
work happens in Claude Code.

The original failure was the deadlock `plan.md` had already rejected in another
form — *"avoids the deadlock a fail-closed pre-session gate would create, where a
stale cache prevents the very run that would refresh it."* Fail-closed is a
safety property when the signal is usually available; when it is **never**
available it is an off switch. Codex was never affected: it writes `rate_limits`
into its own session rollout, so its gate always worked.

### Trial configuration, as actually set

| | Claude | Codex |
|---|---|---|
| Model | `claude-opus-5`, per task | `gpt-5.6-sol`, `~/.codex/config.toml` |
| Effort | `high`, global `effortLevel` | `high`, `model_reasoning_effort` |
| Permission mode | `auto`, per task | n/a |
| Schedule | 3 tasks, 39 runs/week | not yet scheduled |
| Budget source | local token estimate | its own `rate_limits` |

**Permission mode is `auto`, not `default`.** A scheduled task defaults to
`default`, which prompts on tool use and therefore waits for a human who is
asleep. `auto` lets a classifier pass routine work while still stopping
destructive or irreversible commands. `bypassPermissions` was rejected: this
agent runs unattended with repo write access and GitHub credentials, and the
routine prompt telling it what not to do is not a boundary — it is text the agent
is free to reason around.

Allow-rules in `.claude/settings.json` still cover the scripted steps, which are
deterministic by construction. They cannot cover the rest: an agent reviewing a
PR composes `gh search` calls, loops and scratch scripts that no rule can predict
in advance. That is what the mode is for.

### Trial assessment after three runs, 2026-09-05

**Runs.** 08:24 `skipped-usage-unknown` (the pre-fix failure). 10:18–10:31 `done`,
broke down #16. 10:42–10:51 `done`, broke down #17. Both start and finish records
paired with matching run ids; both gates passed on the local token estimate; no
permission stalls after the mode change to `auto`.

**Breakdown quality — good, with one caveat.** #17 produced three tickets split
by area of checking rather than by layer, each independently verifiable, with
#22 declaring its dependency on #21 **in the body** rather than inventing a label
— exactly as the `breakdown` skill instructs. The bodies cite `plan.md` as the
spec, reference existing code by file and line, and carry real judgement:
*"Nothing is written on a refusal — a half-applied park is worse than no park"*,
and *"distinguish not authenticated at all from authenticated without the scope;
they are different findings with different fixes."*

The caveat: **#16's breakdown is not independent evidence.** The `breakdown`
skill's worked example is literally "add a `funnel park` command", split two ways
— and #16 came back split the same two ways. That may be reasoning or it may be
copying. #17 is the trustworthy signal, because nothing in the skill resembles it.

**Bug found, fixed: Codex could jump the "start now?" gate.** `startable()`
accepted a parent at `Ready` as well as `Building`. `Ready` means "broken into
issues" and is still waiting on Nate — so the moment a breakdown finished, five
tickets became startable for work he had never authorised. Only `Building`
qualifies now; he answers the gate by moving the parent there. This was live and
would have fired on the next Codex run.

**Latent bug, documented not fixed.** `heartbeat finish` defaults to the run id
recorded by `start`, in one file per agent. Two overlapping runs clobber that
pointer — a manual smoke test at 10:45 landed inside run three's window and did
exactly that. No records were corrupted here, because the routine passed its id
explicitly, but the routine prompt no longer does. Mitigation for now: **do not
run `heartbeat` commands by hand while a routine may be active.** A proper fix is
to default to the most recent *unfinished* start, or to refuse when more than one
is open.

### Where the funnel can be used from

| Surface | Works? |
|---|---|
| Claude Code, session opened in this repo | **Yes** — skills load from `.claude/skills/`, allow-rules apply |
| Claude Code, session anywhere else on this Mac | **Yes**, but prompts — the allow-rules are project-scoped |
| Claude Code cloud session | **No.** Code and skills travel with the checkout, but there is no `gh` and GraphQL is blocked — see [LEARNINGS](LEARNINGS.md) |
| Desktop app general chat | **No** — see [#25](https://github.com/nateprich-projects/command-center/issues/25) |

Skills live at `skills/` and are symlinked from `.claude/skills/`, so they load
for any session with this repo checked out — local or cloud — while
`scripts/install.sh` additionally links them into `~/.claude/skills` so they work
in any local session. One source, two paths, no second copy to drift.

**The agent-side commands are local by nature and should stay that way.**
`usage.py gate` reads this machine's transcripts, `heartbeat` writes a local
spool, and `prior_run` reads local session files. The human surface — `brief`,
`ideas`, `show`, `capture`, `shaped`, and the three gate answers — was assumed
portable because it only talks to GitHub. Measurement says otherwise.

**That open question for cloud is now answered, and the answer is worse than the
question.** It was framed as token scope — whether the environment supplies a token
carrying `project`. It never reaches a scope check. A web session has no `gh` binary, so
every command dies at exec; and although `GH_TOKEN` is set, the session proxy serves only
a pinned set of GraphQL operations and refuses the rest, while ProjectV2 has no REST
surface to fall back to. Measured, with the output, in [LEARNINGS](LEARNINGS.md).

The consequence for the design: **the human surface is local-by-nature too**, alongside
`usage.py gate`, `heartbeat` and `prior_run` — not because of a missing credential, but
because it is written against `gh` and ProjectV2 GraphQL. Making it portable would mean a
second data path that GitHub does not offer. The recommendation — not a decision, since
`plan.md` does not make one — is to stop carrying cloud as a surface the human surface is
expected to work on, and to say so in `plan.md` rather than leaving it implied here.

### What would count as failure

- Breakdowns producing tickets too large to finish in one run, or built on
  decisions the plan never made
- The ladder not putting #16's tickets before #17's
- Two Codex runs working at once, or a stale claim never taken over
- A start recorded with no finish
- The watchdog firing on healthy outcomes, or staying silent through a real one
- A merged PR that does not match its plan

## Open questions

- **"Share of runs" has no run log in v0.** `maintenance_load` derives it from issues
  closed in the window, by `Class`. The heartbeat (#6) is the first thing that records
  runs; this should probably read from it once it exists.
- **Codex's usage signal.** Its equivalent of Claude's `rate_limits` payload is unknown.
  The early-exit gate pattern holds regardless; v0 ships that gate as an explicit
  fail-closed stub.
- **Dependabot** across repos other than `workbench`. A one-time sweep during each repo's
  onboarding.

## Notes

- The v2 design record was never committed to `workbench` — the housekeeping deletion
  `plan.md` called for is void. Confirmed 2026-09-05.
