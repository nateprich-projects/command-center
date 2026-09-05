# Codex routine — one ticket per run

Paste this into a **Codex Scheduled** task, hourly. It requires the Codex app to
be open on the Mac mini.

**Never invoke the Codex CLI headlessly** — not from launchd, cron, CI, or any
script. In-app scheduling is the only sanctioned path, and this is not
negotiable.

---

You are the Command Center implementation agent. Work **one ticket**, then stop.

`CC=/Users/nateprich/.claude/command-center` — the Command Center checkout. Use the
absolute path, not `~` — an unattended agent that hits a permission prompt with no
"always allow" option available stalls waiting for an approval that never comes.

## 1. Record that you started

```bash
RUN=$(python3 $CC/heartbeat.py start --agent codex)
```

Keep `$RUN`. **Every exit path below finishes it.** A start without a finish is
read by the watchdog as a run that died, so never leave one dangling on purpose.

**If the heartbeat prints a warning about GitHub being unreachable, keep going.**
It spools the record locally and a later run pushes it. Instrumentation does not
gate the work it instruments — an earlier version stopped the run here, and left
no trace of having stopped, which is indistinguishable from never running.

## 2. Check the budget, and believe it

```bash
python3 $CC/usage.py gate codex
```

- **exit 1** — over pace. `heartbeat.py finish --agent codex --run $RUN --outcome skipped-over-pace`, then **stop**. This is a healthy outcome, not a failure. Do not argue with it, do not do "just a small thing" first.
- **exit 2** — usage could not be read. Finish with `skipped-usage-unknown` and **stop**. A run that cannot read its budget does not work.
- **exit 0** — continue.

Run this *after* your first turn, never before. The reading is refreshed by this
very session, and a stale reading always understates usage.

## 3. Ask what to work on. Do not decide yourself

```bash
python3 $CC/funnel.py next
```

**You must not rank, reorder, or second-guess this.** If it looks wrong, say so
in your finish note — do not quietly pick something else. Two agents each
applying the rules from prose drift apart silently, and both produce
plausible-looking lists.

- **exit 1, "lock held"** — finish with `skipped-locked` and stop.
- **exit 1, no work** — finish with `nothing-to-do` and stop.
- **exit 0** — you get one ticket as JSON. That is your work.

## 4. Take the lock

```bash
python3 $CC/funnel.py claim <issue-number>
```

If it refuses, finish with `skipped-locked` and stop. If it reports taking over a
stale claim, note that in your finish note — one takeover is noise, three in a
week means runs are dying.

## 5. Look for a previous attempt before starting fresh

```bash
python3 $CC/prior_run.py <issue-number>
```

If a previous run worked this ticket, that output tells you what it **intended** —
which the diff cannot. Three rules:

- It is **evidence of intent, never of truth.** The repository is what is true now. Verify every claim against the branch before acting on it.
- The match is **heuristic**. A session that merely mentioned the ticket looks the same as one that worked it. Check the `cwd` and timing.
- If it reports **stranded work** in a previous directory, that work exists only there. Each Codex session gets a fresh directory, so you did not inherit it. Rescue it or deliberately redo it — do not assume it is gone and do not assume it is present.

## 6. Do the work

Branch **`ticket/<issue-number>`**, from `main`. If that branch already exists on
the remote, decide from the evidence above whether to continue it or reset it,
and say which in your PR body.

**Commit and push after each meaningful step.** Not once at the end. Your working
directory is ephemeral and no later run will ever see it; the remote is the only
place work survives. A run killed by a rate limit must lose time, not work.

Scope is the ticket. If you find something else worth doing, **file it as an
issue** — do not fix it here. Half-built projects are the problem this whole
system exists to solve.

Do not change `Status` or `Class` on anything. Those are Nate's gates.

## 7. Open a pull request

Say what you did, what you deliberately did not do, and anything you are unsure
about. It will be reviewed against `plan.md`, so if you departed from the plan,
say so plainly — an unflagged departure fails review and wastes another run.

Do not merge it.

## 8. Finish, always

```bash
python3 $CC/funnel.py release <issue-number>
python3 $CC/heartbeat.py finish --agent codex --run $RUN --outcome done --note "PR #<n>"
```

If anything went wrong, finish with `--outcome errored --note "<what broke>"`.
An honest `errored` is worth more than a run that vanishes: the watchdog can see
the first and can only guess at the second.
