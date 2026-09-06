# Status

**Last updated: 2026-09-05**

v0 in progress. Target: shipped in under two weeks from 2026-09-05.

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
| Claude Code cloud session | **Untested.** Code and skills travel with the checkout; needs a token with `project` scope |
| Desktop app general chat | **No** — see [#25](https://github.com/nateprich-projects/command-center/issues/25) |

Skills live at `skills/` and are symlinked from `.claude/skills/`, so they load
for any session with this repo checked out — local or cloud — while
`scripts/install.sh` additionally links them into `~/.claude/skills` so they work
in any local session. One source, two paths, no second copy to drift.

**The agent-side commands are local by nature and should stay that way.**
`usage.py gate` reads this machine's transcripts, `heartbeat` writes a local
spool, and `prior_run` reads local session files. Only the human surface —
`brief`, `ideas`, `show`, `capture`, `shaped`, and the three gate answers —
is portable.

**The open question for cloud** is whether the environment supplies a GitHub
token carrying `project` scope. Everything the human surface does beyond reading
issues writes Project fields, and without that scope it fails at the first write.
Untested; try `python3 funnel.py brief` in a cloud session and see.

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
