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

For the closed-world test of whether a plan or step needs Nate, an agent has:

- a shell
- `gh`
- a GitHub token available to `gh`
- the filesystem of its checkout

An agent does not have a browser session, application UIs, a credential store, account or
billing settings, physical access, or an identity of its own. It acts through Nate's
token. Ask whether the step requires anything outside this boundary. The named access
cases — an app UI with no API, entering a credential, an account or billing setting, or
physical access — are examples of the boundary, not an exhaustive checklist; an unnamed
capability gap still counts. _(confirmed by Nate 2026-09-07)_

## The rule that is easy to skip

**GitHub is the state.** No persistence, no state file, no locking, no journal. If you
find yourself adding a cache, a `.json` of record, or a lock file, you have taken a wrong
turn — go back and derive it from GitHub instead.

The one exception is the statusline rate-limit cache, which exists because the numbers
are only readable from inside a live session. It is a cache, never a source of truth.

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
  there, and the schedules are ~100 runs a day.**

  Muse ships no scheduler of its own, so a launchd job is the only way to schedule it —
  and it is a *better* surface than the alternative, because a plist is a file that can
  be versioned and drift-checked, unlike zcode's prompt, which lives in an app UI where
  nothing can see it (#52).

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

  **And it has none of Codex's gates.** No presence check, no budget gate — Muse reports no
  usage anywhere in its CLI — and no idle rule. A launchd job fires whenever it is due. The
  reviewer being read-only by construction is what makes that acceptable.
- **No Copilot automation.** Those are employer-provided tokens; personal use stays
  one-off and manual.
- **Every routine starts, reads fresh usage, and exits immediately if over the pace
  line.** The gate lives inside the session, never before it — see `plan.md`,
  "Reading usage".
- **Missing usage data fails closed.** A run that cannot read its budget does not work.

  **Muse is the one exception, and it is deliberate.** Nate's call, 2026-09-07: its limits
  are generous and he reviews consumption himself rather than through a gate.
  _(confirmed by Nate 2026-09-07.)_

  **Stated precisely, because the first version of this was wrong.** It said Muse reports
  no usage. It does: `/upgrade` in the TUI prints both windows in the shape this file
  wants — *"Current 1% used · Resets at 4:58 PM / Weekly 0% used · Resets Sep 13 at 5:00
  PM"*. What is true is narrower: **Muse exposes usage only to an interactive session,
  fetched at startup and held in memory.** It is written to no file — checked across
  `~/.local/share/muse` and `~/.config/muse` on 2026-09-07, where the only matches were
  Muse's own tool output echoing this repo's numbers back.

  So a scheduled run still has nothing to read, and the exception stands for that reason
  rather than the one first given. Codex writes `rate_limits` into its rollout files and
  z.ai answers a quota endpoint; Muse does neither.

  **The end-condition changes with it.** Not "if Muse ever exposes usage" — that is
  already met. End the exception when usage becomes *readable by a run*: a file it
  writes, or a documented endpoint. There are signs of the latter — a `/subscription`
  path and the status values `active / paused / blocked / usage_limited / budget_limited`
  appear in the binary — but that is inferred from strings, needs Nate's Keychain
  credential, and sits under terms nobody here has read. Three unknowns for a gate on a
  pool measured at 1% of five hours and 0% of a week.

  The exception is scoped to *reading* usage, not to the rest: a Muse run still records a
  heartbeat, and it still stops if the funnel has nothing for it. If Muse ever exposes
  usage, the exception should end rather than be grandfathered — an unmetered pool is a
  standing exception, not a design.

## Repository hygiene is yours, not his

_(Confirmed by Nate 2026-09-07 — "I'm not good at tracking drift and deciding when/how to
get everything lined up in the repo. I want to offload that thinking entirely to you
whenever and wherever you find it.")_

**Do not ask him whether to tidy the repository. Notice it and do it.** That covers
uncommitted work, unmerged branches, a live change sitting somewhere git does not record,
a config edit that has not reached `main`, and a checkout left on a branch. Report what you
did; do not put the decision to him.

The specific failure this guards against: the canonical checkout at
`~/.claude/command-center` is a **symlink to the working tree**, so an edit is live for
every routine the moment it is saved — and silently reverts if anyone checks out another
branch. A live config change on an unmerged branch is not a tidiness problem; it is an outage
waiting for a `git checkout`.

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

## Secrets

Credentials come from the environment or a gitignored `.env`. Never commit them, never
print them into a transcript, never paste them into a chat. When a task needs one, tell
Nate where to obtain it and have **him** put it in `.env`.

## Verification

- **Verify against the running system, not the documentation.**
- **Report failures plainly, with the output.** If something is untested, say so rather
  than implying coverage. "Built" is not "verified".
- Prefer evidence that would fail if the thing were broken.
