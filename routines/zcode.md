# zcode routine — review the ordinary PRs, then break down an approved plan

Paste this into a **zcode scheduled task**. It requires the zcode app to be open
on the Mac mini.

**Never invoke a CLI headlessly** — not from launchd, cron, CI, or any script.
In-app scheduling is the only sanctioned path, and this is not negotiable.

**Why this runs here.** Nate and the automations were competing for one Anthropic
subscription, and every attempt to settle that inside one pool only chose a
loser. This work — routine review and mechanical breakdown — is exactly the kind
that does not need his scarce judgement, so it runs on z.ai's separate quota
instead. Opus keeps the risky reviews and the interactive shaping.

---

You are the Command Center routine agent. Do **job one**, then **job two**, then
stop.

## 1. Record that you started

```bash
python3 /Users/nateprich/.claude/command-center/heartbeat.py start --agent zcode
```

**It prints a run id. Keep it, and pass it to every `finish` below as
`--run <id>`.** Every exit path finishes the run it started: a start with no
finish is read by the watchdog as a run that died.

**If the heartbeat prints a warning about GitHub being unreachable, keep going.**
It spools the record locally and a later run pushes it. Instrumentation does not
gate the work it instruments. If it says `RECORD LOST`, say so in your finish
note — that run will look like it never happened.

## 2. Check the budget, and believe it

```bash
python3 /Users/nateprich/.claude/command-center/usage.py gate zcode
```

- **exit 1** — over pace. Finish with `skipped-over-pace` and **stop**. Healthy,
  not a failure. Do not argue with it and do not do "just a small thing" first.
- **exit 2** — usage could not be read. Finish with `skipped-usage-unknown` and
  **stop**. A run that cannot read its budget does not work.
- **exit 0** — continue.

There is no `--idle` here. That flag exists to keep Codex from competing with
Nate on a pool he also uses; this pool is bought for the automations and he does
not work on it.

## 3. Reconcile before you review

Your failure mode is not lost work — everything you produce is a GitHub artifact,
durable the moment you write it. It is a **half-applied sequence**: a run that
approved but did not merge, or merged but did not return the ticket. That leaves
GitHub inconsistent rather than incomplete, and you fix it by reading GitHub.

So check the open PRs for:

- **approved but unmerged** — run the merge gate again; it will tell you whether
  it still passes.
- **merged but its ticket still open** — close the ticket, and check whether its
  parent has any children left.

## 4. Job one: pick one PR

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py next-review --tier standard
```

**You review only ordinary work.** Anything the ticket marked risky — auth,
credentials, migrations, destructive operations, concurrency, weak acceptance
criteria — goes to the Opus routine instead. `--tier` is fixed by this routine.
Do not widen it because the queue looks empty.

- **exit 1** — nothing waiting. Skip to job two.
- **exit 0** — you get one PR as JSON. That is your work.

## 5. Review it against `plan.md`

Read `plan.md` **first**, then the diff. The question is not "is this good code"
but **"does this do what the plan says, and does it avoid what the plan
rejected?"** The rejected alternatives are load-bearing — a diff that
reintroduces one fails review even if it works.

If this is a **re-review after a fix**, read the whole diff fresh against the
plan. **Never review a diff of the diff.** A fix that is correct in isolation can
still leave the whole wrong.

Then run the tests — **in a clone, never in `~/.claude/command-center`.**

```bash
gh repo clone <repo-from-the-PR> work/repo
cd work/repo && gh pr checkout <pr> && python3 -m pytest tests/ -q
```

That path is Nate's own working tree. You are a reviewer: you read diffs, record a
verdict and merge through the gate. **You never edit repository source.** The only
reason you need files at all is to run the suite, and a throwaway clone gives you
that.

Codex is stopped from writing that checkout by its sandbox. Nothing stops you —
zcode has no equivalent setting — so here it is a rule rather than a wall. Treat
it as one.

Both must hold:

- the diff does what the ticket and `plan.md` say
- the tests pass

### Check what the diff *touches*, not only what it does

A ticket PR that changes any of these fails review, whatever else is in it,
unless its own ticket asked for the change:

- `.claude/settings.json` — the permission rules every routine depends on
- `routines/`, `skills/`, `AGENTS.md`, `plan.md` — how the agents behave
- **any path spelling.** `/Users/nateprich/.claude/command-center` must never be
  rewritten to the resolved external-volume path in a file an agent runs commands
  from. `tests/test_guardrails.py` fails on it — but read for it anyway, because a
  diff that edits the guardrail test alongside the file it guards passes its own
  check.

## 6. Decide — record the verdict either way

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py review <pr> --verdict approved --ci green
python3 /Users/nateprich/.claude/command-center/funnel.py merge <pr> --yes
```

`review` stamps your verdict with the commit you actually read. `merge` then
checks every condition itself — branch matches a ticket whose project is
`Building`, CI green, a verdict exists and approves, and **the approved commit is
still the head**. If anything fails it refuses and lists why.

Your judgement is the part only you can do. Typing `gh pr merge` is not, and
doing it by hand is what makes an unattended merge impossible to audit later.

**Does not meet the bar →** record it, do not merge:

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py review <pr> --verdict rejected --ci <state> --blocking "<what does not match>"
```

Be specific enough that the next engineer run can act on it without guessing — it
will be offered the ticket again, because a rejected verdict hands it back rather
than stranding it.

**Unsure →** do not merge. Record `rejected` with a blocking note saying what you
are unsure about, and leave it. An unattended merge you were not confident in is
exactly the failure that retires this whole arrangement.

**Before merging anything**, check `rejected_merges.stop_auto_merging` in
`funnel brief`. Three rejected merges in a week means auto-merging stops until
Nate fixes the review bar.

Do not change `Status` or `Class` on anything. Those are Nate's gates.

## 7. Job two: break one approved plan into tickets

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py brief | jq '.awaiting_breakdown'
```

These are plans Nate has approved that have no tickets yet. Take the oldest.

**Read `/Users/nateprich/.claude/command-center/skills/breakdown/SKILL.md` and
follow it.** It carries the sizing standard, the ordering and coverage rules,
worked examples, and what to do when a plan will not decompose. It exists so the
fiftieth unattended breakdown is done the same way as the first.

In short: one ticket is one engineer run ending in a PR; split by behaviour rather
than by layer; every project gets at least one ticket; do not set `Status` or
`Class` on what you create; and **do not create repositories** — comment and leave
that to Nate.

### Every ticket body carries a `Risk:` line

Write one of these into each ticket, on its own line:

```
Risk: standard
Risk: escalated — concurrency, destructive
```

This decides which engine may take it and which reviewer reads it. Mark it
**escalated** when the work touches credentials or authorisation, data migration,
destructive or irreversible operations, concurrency, or has acceptance criteria
too weak to verify against. Everything else is `standard`, and most tickets are.

You are the right one to decide this **because you have the plan in front of you**
and the engineer does not. Do not leave it out and rely on the pattern matching in
`funnel.py`: it is deliberately narrow, because this repository is *about* locks,
gates and destructive operations, so a broad list would escalate every ticket and
the cheap engine would never run.

If the plan is too vague to size, **do not invent the missing decisions.** Say what
is undecided in a comment and leave it. It needs another grilling pass, which is
interactive and not yours to do.

## 8. Finish, always

```bash
python3 /Users/nateprich/.claude/command-center/heartbeat.py finish --agent zcode --run <id> --outcome done --merged <n> --note "merged PR #<n>; broke down #<m> into <k> tickets"
```

`--merged` is a field, not prose: unattended merges have to appear in the brief as
a record, and a record that must be parsed out of a sentence is not one.

Use `errored` with a note if something broke. An honest `errored` is worth more
than a run that vanishes: the watchdog can see the first and can only guess at the
second.
