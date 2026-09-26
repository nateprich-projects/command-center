# Repository Instructions

**This is the canonical instruction file for every AI assistant working here** — Claude,
ChatGPT/Codex, GitHub Copilot, or anything else. `CLAUDE.md` and
`.github/copilot-instructions.md` are one-line pointers to this file; do not let them
drift. If you change conventions, change them here.

## What this repo is

Command Center: a funnel that manages Nate's project portfolio through
`Ideas → Shaped → Ready → Building → Done`, plus `Parked`. Its purpose is **completion
and disposal**, not idea capture.

## Read `plan.md` first

[`plan.md`](plan.md) is the settled design record, produced by a long grilling session.
**The rejected alternatives in it are load-bearing.** Do not re-litigate a decision it
already records. If you believe one is wrong, say so and stop — do not quietly build
something else.

**Most of what looks like a gap in `plan.md` is a decision that was deliberately
deferred. Stop and ask rather than inventing one.**

## Rule authority and provenance

Agents may write normative rules, but an agent-written rule must carry the inline tag
`_(agent rule, unconfirmed — advisory)_`. It remains advice until Nate confirms it. No
agent may cite an unconfirmed agent rule to refuse Nate or block a gate.

When Nate confirms a rule, replace that tag with
`_(confirmed by Nate YYYY-MM-DD)_`; only then does the rule bind. The standing rule in
this section is the safeguard when an older rule has no tag: refusing Nate still requires
a confirmation that can be pointed to, rather than an assumption about who authored the
prose. _(confirmed by Nate 2026-09-06)_

## Capability boundary

For the closed-world test of whether a plan or step needs Nate, use the
[shared capability boundary](skills/capability-boundary.md). It distinguishes work
that is workable by any agent, workable only where the Claude Code environment is
present, and workable by no agent. The named examples are illustrative rather than
an exhaustive checklist; an unnamed capability gap still counts. _(confirmed by Nate
2026-09-07)_

## The rule that is easy to skip

**GitHub is the state.** No persistence, no state file, no locking, no journal. If you
find yourself adding a cache, a `.json` of record, or a lock file, you have taken a wrong
turn — go back and derive it from GitHub instead.

The one exception is the statusline rate-limit cache, which exists because the numbers
are only readable from inside a live session. It is a cache, never a source of truth.

**Project single-select mutations replace the option set rather than appending to it.** Any `updateProjectV2Field` call carrying `singleSelectOptions` must include the existing `id` for every option being kept; submitting an option with `name`, `description`, and `color` but no `id` mints a new option and silently orphans every existing assignment. A backup of the option set is **not** a backup of the assignments. _(agent rule, unconfirmed — advisory)_

## Ranking is not yours to do

**One shared program (`funnel.py`) computes all ordering. Both agents call it and act on
its output; neither ever ranks anything itself.** You may recommend that a `Status` or
`Class` value should change. You may not supply a score, alter a rank key, or reorder
items.

_(confirmed by Nate 2026-09-05)_

Two vendors' agents sharing no session, no memory, and no runtime *will* drift if each
implements the rules from prose — and the failure is silent, because both produce
plausible-looking lists.

Ordering rules get unit tests against fixtures. That is the part most likely to be subtly
wrong.

## Execution rules — non-negotiable

- **In-app scheduling only, for Claude Code and Codex.** Claude Code Routines and Codex
  Scheduled. **Never invoke either of those CLIs headlessly** from launchd, cron, GitHub
  Actions, or any script. This is an account-suspension risk.

  **The rule is vendor-specific, not a general principle** — it is about what Anthropic
  and OpenAI permit on these plans, and it has been misread as universal.
  _(confirmed by Nate 2026-09-07.)_

  **The rule bans unattended *prompt execution*, not a listener.** `claude rc`
  (`claude remote-control`) started from launchd is outside it: the server idles and
  issues no prompt of its own, consuming nothing until Nate sends a message from a
  device. What the clause in Anthropic's consumer terms prohibits is accessing the
  Services "through automated or non-human means"; a process that waits for a human is
  not that, and the terms carve out "where we otherwise explicitly permit it".
  _(confirmed by Nate 2026-09-09.)_

  What is established here, and it is first-party rather than inference: Anthropic ships
  auto-connect as a supported setting — `remoteControlAtStartup: true`, "connect
  automatically when an interactive session starts", exposed in `/config`, the desktop
  app, and the VS Code extension. Their Remote Control documentation describes server
  mode as a persistent server accepting multiple concurrent sessions, and tells you to
  run it under `tmux` or `screen` to survive a disconnect. The Usage Policy's automation
  prohibitions are all abuse shapes this is nowhere near: automation in account
  creation, spammy behaviour, bypassing guardrails, model scraping, ban circumvention.

  **The line to watch is who issues the prompts, not what starts the process.** If
  anything on this Mac ever drives the sessions the listener spawns — a channel, a
  scheduled task, cross-session messaging — then a machine is issuing prompts through
  it and the carve-out no longer applies. That would be the banned shape wearing the
  listener's clothes. Do not widen this paragraph to cover it.

  **Muse Code is exempt, on Nate's judgement.** Asked whether the schedules were within
  Meta's terms, he answered *"I think we're good."* That settles it as a decision and
  the schedules run. _(confirmed by Nate 2026-09-07.)_

  **It remains his risk call on an inference, not a verified reading of Meta's terms**
  — an earlier version of this section asserted sanction it had not established, and
  his answer accepts the exposure rather than closing the gap. Do not upgrade the
  wording below to "permitted" or "sanctioned" on the strength of this line.
  _(corrected 2026-09-07.)_

  What is actually established: `muse exec` is described in the CLI's own help as
  running a prompt non-interactively (headless), with `--json` for machine-readable
  output; Meta ships an SDK for controlling Muse Code programmatically; and the
  subscriptions documentation places no restriction on automated, scheduled or headless
  use, requiring only that a subscription is used *through the signed-in CLI*, which
  these schedules satisfy. Separate API keys bill pay-as-you-go, so the runner
  deliberately uses the account login and never sets `META_API_KEY`.

  What is **not** established: the Meta Model API Terms of Service and Acceptable Use
  Policy, which the subscriptions page names as the governing documents, returned no
  readable content when fetched on 2026-09-07. Nobody here has read them. "No
  restriction found" means "not in the pages that could be read", which is a weaker
  claim than it looks. **If a fair-use or automated-workload clause exists, it lives
  there, and the schedules are ~198 runs a day, against up to ~312 possible starts
  (two launchd jobs: `command-center-muse-review-standard` every five minutes,
  `command-center-muse-review` hourly at :07).** Measured from the heartbeat branch,
  over the 24-hour window ending 2026-09-08 14:20 PDT — 198 runs started, 195
  finished; outcomes 126 `nothing-to-do`, 63 `done`, 6 `errored`.
  _(confirmed by Nate 2026-09-07; figure corrected and re-confirmed 2026-09-08)_

  Muse ships no scheduler of its own, so a launchd job is the only way to schedule it —
  and it is a *better* surface than the alternative, because a plist is a file that can
  be versioned and drift-checked, unlike zcode's prompt, which lives in an app UI where
  nothing can see it (#52).

  **Codex implements both tiers and Muse judges, since 2026-09-22** (Nate, #1315).
  Codex works tickets from its in-app automations on GPT-6 Luna at `max`:
  `command-center-tickets-hourly` (standard, every ten minutes) and
  `command-center-tickets-weekday-mornings` (escalated, hourly; the keeper reads its
  `BYHOUR=` rule as the escalated tier). The other three escalated windows are retired
  and stay paused. Muse runs review, breakdown and shape on `muse-spark-1.3` at `max`
  (the standard tier runs on z.ai until 2026-10-06 09:00 PDT; see below) and
  implements nothing: `AGENTS_BY_ROLE` names Codex alone, and `begin` refuses an implement
  caller the roster does not name. Two checks watch the app-held state:
  - every Codex run checks its own model, effort and sandbox against `codex_run.py` and
    stops on any difference (`config-drift`);
  - `funnel doctor` checks the automation files themselves.

  The way back is a change in the repository, not a file copy:
  - put `muse` back on the roster;
  - restore the two implement plists to `launchd/` and `scripts/install.sh` from git
    history, and drop the test that pins their absence.

  `scripts/muse-implement` is unchanged. The copies in `~/Library/LaunchAgents-retired/`
  are only a local record: jobs copied back from there would not be refreshed or
  drift-checked by the keeper. From
  2026-09-18 to 2026-09-22 Muse implemented both tiers, because Codex's Plus week was
  nearly spent. _(confirmed by Nate 2026-09-22)_

  **z.ai judges the standard tier from 2026-09-23 until 2026-10-07 00:00 Beijing time
  (2026-10-06 09:00 PDT), ahead of the z.ai plan's expiry** (Nate, 2026-09-23; #1411).
  Muse keeps the escalated tier. Muse's week was nearly spent until its Sunday
  2026-09-27 17:00 PDT reset, and Nate's z.ai GLM Coding Plan (Lite: 2,000 credits per
  five hours, 10,000 per week) is cancelled but active until it expires on 2026-10-07,
  so its credits are use-it-or-lose-it. `scripts/muse-review-engine` routes on the
  clock: a `standard` run before 1791302400 runs as agent `zcode` and asks GLM-5.3
  through `scripts/zai-exec`, one Messages call with no tools offered to z.ai's
  Anthropic-compatible endpoint, refused unless `glm-5.3` is the model that answered.
  The same review, breakdown and shape questions, packets and apply steps serve both
  backends, except that z.ai's review judges run one at a time, because the Lite plan
  refuses concurrent requests. The plist is unchanged. At the cutoff the standard tier
  is Muse's again with nothing to undo, and `heartbeat.retired_agents` retires `zcode`
  at the same instant. The cutoff is the start of the expiry date in z.ai's own time
  zone (UTC+8), the earliest reading of "expires 2026-10-07": it gives up most of a day
  rather than risk runs erroring on an expired key.

  A spent z.ai window ends the run cleanly, whichever call meets it: a breakdown's or a
  shape's one question, a review's lister, or any review judge. The run records
  `skipped-provider-quota` and applies nothing — no rejection from a review nobody
  finished judging — and the next `begin` stops on z.ai's own reading until the window
  resets. It never falls back to Muse and never parks Muse's lanes. The `zai` pace
  line is off for the duration (floor and target 100, a one-run reserve in each
  window), and so is the 15% five-hour boundary that holds back unattended shaping,
  because credits left at the expiry are worth nothing.

  **It is his risk call on unread terms, as Muse's is.** This is headless use of a
  Coding Plan key from launchd, and z.ai's terms have not been read here. Nothing in
  this paragraph establishes that they permit it; do not upgrade the wording to
  "permitted" or "sanctioned". The caveat was stated in the option he answered, and
  the worst case named was losing a subscription he has already cancelled. The
  in-app-only rule above is specific to Anthropic and OpenAI and decides nothing here
  either way.

  **The exposure, plainly.** For the duration, every member repository's standard-tier
  packets — diffs, tickets, plans and repository rules — go to z.ai (Zhipu), under
  data-use terms nobody here has read. That includes private repositories such as
  `jeffy-finance-agent` and `career-toolset`, and `The-League`, which a collaborator can
  see. It bypasses the per-repository posture Muse keeps in
  `muse_model.CONTRIBUTOR_REPOS`, where sending a repository to a discounted tier is a
  deliberate edit per name. Asked exactly this, with those private repositories named,
  Nate chose "All repos" on 2026-09-23. _(Nate, 2026-09-23.)_

  **On Muse, tiers 1 and 3 run on the contributor model; tier 2 does not.**
  `muse_model.CONTRIBUTOR_REPOS` names `command-center`, `github-runners`, `workbench`,
  `Fantasy-GM`, `The-League` and `AFL`, so their Muse calls go to Meta's Discounted
  Services tier, which may train on them. `career-toolset` and `jeffy-finance-agent`
  stay on the private model, as does any repository not named. A test holds tier 2 off
  the list. The five private repositories on it are Nate's deliberate override of the
  §6.2 FAIL in `docs/meta-model-api-tos-aup-1095.md`. The gate still prices contributor
  calls at the standard card until a panel reading shows what they really cost.
  _(Nate, 2026-09-26, #1570: "So tier 1 and tier 3, but not tier 2.")_

  **`--approval-mode never` is not a guard.** Measured 2026-09-07: it does **not** fail
  closed. It means *never ask*, and it auto-approved a shell command with no prompt.

  **And `--disable-shell` is not the answer either** — that was written here first and was
  wrong. A reviewer *is* shell: `gh pr diff` to read the PR, `funnel review` to record the
  verdict, `funnel begin` to record the heartbeat. Disable shell and it cannot take a step.

  **The measured working shape is `--disable-write --sandbox-network enabled`.** The
  network flag is not optional and its absence is silent: the sandbox defaults to
  `proxy-only`, under which `gh` fails with `Post "https://api.github.com/graphql": context
  deadline exceeded` after about a minute. A run without it does not refuse — it times out
  and reports the PR as unreadable, which looks like a GitHub problem.

  Safety therefore comes from what the job is told to do and from `--disable-write`, not
  from an approval mode and not from the sandbox. That is the same place zcode ended up,
  reached by measurement rather than by an 82-minute incident.

  **Muse runs shell commands in the background and delivers output asynchronously** — a
  tool result returns `background_running` with explicit guidance not to poll, because the
  output "wakes you even after you end the turn". Codex and zcode are synchronous. A
  routine written for them will misread its own results here.

  **Muse's scheduled budget is now locally metered.** The CLI still does not expose a
  quota endpoint, but each session journal carries goal_usage_attribution provider
  records. usage.py prices their input, cached-input, and output tokens at the
  **standard** rate ($1.25 / $0.15 / $4.25 per 1M) and gates the total **since the
  provider's own weekly reset** — Monday 00:00 UTC, Sunday 17:00 PDT — against a $200
  cap. A missing or malformed record fails closed.

  **The total is anchored to that reset, not to a trailing seven days.** The cap is
  calibrated from one window's refusal, and a trailing week straddles two: measured
  2026-09-20, the rolling reading was $224.81 against the $200 cap — 112%, which would
  have stopped every lane for days — where the live window held $20.44. The anchored
  reading matched the account panel to within a quarter of a point (10.22% against the
  panel's 10%), which is the closest agreement any local meter has had with a vendor's
  own number here.

  **Those dollars are a pacing index, not a bill.** The plan is a flat $50/month Muse
  Code Power Usage subscription, so nothing the meter reports is money owed; the cap
  exists to stop the lanes shortly before the provider does. It was the contributor
  card ($0.10 / $0.002 / $0.20) against a $20 cap until 2026-09-20, when Nate checked
  the account and found the sessions run on the standard model. The two cards are not
  a flat multiple — contributor discounts a cache read to 2% of a fresh token, standard
  to 12% — so on this cache-heavy workload the old card read about 28x low.
  _(confirmed by Nate 2026-09-20.)_

  **The provider meters two windows, and the refusal text names neither.** The account
  panel shows a five-hour window whose reset floats with use and a weekly window that
  resets Sunday 17:00 PDT; a 429 says only `Your usage window resets at <ISO>`. A stamp
  on the Sunday lattice is the weekly wall, a floating stamp under five hours out is
  the five-hour one. Both have been hit: the weekly wall on 2026-09-19, and the
  five-hour wall on 2026-09-21, when 18 requests refused from 13:01 to 13:57 PDT named
  13:57:55 PDT.
  #1182 covers recording each hit so the windows can be calibrated from evidence.
  **Muse is paced by a projection, not by a line.** At the last 72 hours' spending
  rate, will the window last until its reset? `usage.pace` answers with a band: `ok`;
  `tight` when the projected end passes 100 percent; `over` when used plus the session
  reserve passes 100 percent, which is the flat ceiling. Both `tight` and `over` stop
  every lane before any Project read; the ladder decides only what goes first when
  the brake lifts (#1269, Nate 2026-09-21: "The brake should apply to ALL work. The
  ladder just determines what goes first when the brake is let up."). Shedding by
  class under `tight` was built and measured first (#1199) and was no brake on a
  board that is mostly Broken. The proportional line Claude and Codex use is deliberately
  not applied: it keeps room on a subscription Nate shares, and Muse's plan is flat and
  used by nothing else, so budget left at the reset is worth nothing. Three days rather
  than one because daily totals swing fourfold. A window with no spend in it yet reads
  as zero, not unknown: every window opens that way, and reading it as unknown would
  stop every lane at the reset with nothing left to make the first call.
  Nate decided on 2026-09-21 that Muse is paced rather than held to a flat ceiling
  alone, and said of the existing policy: "Don't just read the pace policy. If that
  policy sucks then make a good one." The design above is the agent's answer to that.
  _(agent rule, unconfirmed — advisory)_
  **The window that resets Sunday 2026-09-27 17:00 PDT runs under a dated override
  (#1341).** Its projection is reported but never bands `tight`, and its flat ceiling
  is priced from the account panel: 86% there against $120.91 here puts the provider's
  100% near $140.59, and the gate stops at 100% of it, less one session's reserve.
  `usage.MUSE_PACE_OVERRIDE` names the window, so the override lapses at that reset
  with nothing to clear. _(Nate, 2026-09-22.)_ #1396 re-took the pairing from a new
  panel reading on 2026-09-23, as #1341 anticipated. The first pairing, 70% against
  $89.38, was taken after four days of Muse implement runs, and by then it read 87%
  while the panel read 81%.
  #1409 re-took it again at 23:10 PDT the same day and raised the ceiling from 95 to
  100, because Nate uses no Muse himself ("It's all for these runs"). At 100 the
  last review before Meta's wall can post a fail-closed `rejected` on its PR, since
  a refused judge reads `unsure`; the provider-quota hold then parks every Muse lane, so
  that is at most one per window. _(Nate, 2026-09-23.)_
  At 21:39 PDT on Thursday 2026-09-24 the panel read 0% used, three days before the
  Sunday reset it had been showing, so the provider reopened the window early. The
  override's `reopened_at` makes the window count only spend after that moment. The
  cap keeps the 86% pairing, and the override still lapses on Sunday, when the panel
  shows whether the provider's week now ends Sunday or Thursday. _(Nate, 2026-09-24.)_
- **No Copilot automation.** Those are employer-provided tokens; personal use stays
  one-off and manual.
- **Every routine starts, reads fresh usage, and exits immediately if over the pace
  line.** The gate lives inside the session, never before it — see `plan.md`,
  "Reading usage".
- **Missing usage data fails closed.** A run that cannot read its budget does not work.
- **Exception: Claude's Saturday lane has no budget gate.** `routines/claude-saturday.md`
  runs until the clock or the provider's own limit stops it and never reads usage;
  `begin` enforces its Saturday-before-11:15 window instead. See `plan.md`, "Budget".
  _(Nate, 2026-09-25, #1557.)_

- **Analysis waits for Nate by default:** projects whose tickets change no behaviour never close themselves, whatever their class, and reach `Accept it?` only after findings are posted on the parent issue; see `plan.md`, "Building completion and automatic acceptance", rule 5.

## Repository hygiene is yours, not his

_(Confirmed by Nate 2026-09-07 — "I'm not good at tracking drift and deciding when/how to
get everything lined up in the repo. I want to offload that thinking entirely to you
whenever and wherever you find it.")_

**Do not ask him whether to tidy the repository. Notice it and do it.** That covers
uncommitted work, unmerged branches, a live change sitting somewhere git does not record,
a config edit that has not reached `main`, and a checkout left on a branch. Report what you
did; do not put the decision to him.

The specific failure this guards against: the canonical checkout at
`~/.claude/command-center` is **Nate's working tree**, not a disposable agent clone. An
edit or pull there is live for every routine that still invokes its `funnel.py`, and silently
disappears if Nate checks out another branch. A live config change on an unmerged branch is
not a tidiness problem; it is an outage waiting for a `git checkout`.

Until the routines move to the maintained run clone, `funnel doctor` reports when this
checkout falls behind `origin/main`; that check retires once no routine invokes the old path.

**Still ask before anything destructive**: rewriting history, force-pushing, discarding
work he has not seen, or deleting a branch that holds commits which are not on `main`.
Those destroy things rather than line them up, and the instruction above is about
lining up.

**This is a standing instruction, not a substitute for detection.** Relying on an agent
noticing is the same weak control this project rejected for human steps in #89 — see #97,
which tracks detecting repository drift mechanically.

## Project conventions

- **`plan.md`** — the design record. Why it is shaped this way, and *what was rejected
  and for what reason*. The rejections are the valuable part.
- **`STATUS.md`** — where things actually stand. Distinguish "built" from "verified"
  honestly.
- **`LEARNINGS.md`** — durable findings, newest first. A platform constraint you hit, a
  vendor behaviour that contradicts its documentation, a measurement that overturned an
  assumption. Label confidence honestly: `measured` means you observed it and quoted the
  evidence; `documented` means a vendor claims it and you did not verify; `inferred`
  means it could be wrong.

## Vocabulary

Deliberately de-jargonised — some repos in the funnel are public and a stranger should
understand a status or label without a glossary. The complete label set is
`needs-shaping` and `blocked`. Do not add a third without changing `plan.md` first.

## Scope

Repos opt in by carrying the topic `command-center`, across both `nateprich` and
`nateprich-projects`. Never an allowlist, never a denylist — see `plan.md`.

`braven112/mfl.football.v2` is explicitly out. No agent-authored PRs land in a
collaborator's repo.

## Member-repo entry standard

Onboarding is a one-repo-at-a-time ritual. A repo meets the entry standard only when
both of these **blocking** judgements are satisfied:

- **Correct long-term home — Nate's judgement.** Nate decides that the repo is the
  correct long-term home for its work. An agent must not infer or automate this
  decision.
- **Triaged backlog — Nate's judgement.** Nate decides that the repo's backlog has
  been triaged. Backlog triage is not machine-checkable; an agent must not fake a
  check for it.

The onboarding steps are:

- **`command-center` topic — blocking membership requirement.** The topic is the
  membership state in GitHub; there is no allowlist or denylist for repos.
- **CI workflow — blocking.** A member repo must have CI, because the merge gate
  refuses to merge work when no CI checks are reported.
- **CI on the self-hosted runners — blocking.** Every CI job runs on a `hobby-*`
  runner (`hobby-linux`, `hobby-windows` or `hobby-macos`), never a GitHub-hosted
  label such as `ubuntu-latest`. The org's Actions spending limit refuses hosted
  jobs for private repos before they start, so a hosted CI job reports a failure
  that ran no code (measured 2026-09-24 on `github-runners#2`). A repo arriving with
  hosted CI is switched over during onboarding. _(confirmed by Nate 2026-09-23)_
- **Stock GitHub labels deleted — advisory.** Remove the ten stock labels so they do
  not duplicate Command Center's vocabulary, but their presence does not block work.
- **Dependabot swept — advisory.** Sweep Dependabot during onboarding, but an
  outstanding sweep does not block work.

The two entry-standard judgements remain Nate's even when the checkable onboarding
steps are reported by tooling. _(confirmed by Nate 2026-09-08)_

## Secrets

Credentials come from the environment or a gitignored `.env`. Never commit them, never
print them into a transcript, never paste them into a chat. When a task needs one, tell
Nate where to obtain it and have **him** put it in `.env`.

## Verification

- **Verify against the running system, not the documentation.**
- **The canonical test command is `python3 -m pytest`; invoke it with Python rather than as a standalone executable.**
- **Report failures plainly, with the output.** If something is untested, say so rather
  than implying coverage. "Built" is not "verified".
- Prefer evidence that would fail if the thing were broken.
