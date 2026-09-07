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

- **In-app scheduling only.** Claude Code Routines and Codex Scheduled. **Never invoke
  either CLI headlessly** from launchd, cron, GitHub Actions, or any script. This is an
  account-suspension risk.
- **No Copilot automation.** Those are employer-provided tokens; personal use stays
  one-off and manual.
- **Every routine starts, reads fresh usage, and exits immediately if over the pace
  line.** The gate lives inside the session, never before it — see `plan.md`,
  "Reading usage".
- **Missing usage data fails closed.** A run that cannot read its budget does not work.

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
