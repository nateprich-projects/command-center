# Claude routine — review a PR against plan.md, merge if it matches

Paste this into a **Claude Code Routine**. It requires Claude Code to be open on
the Mac mini.

**Never invoke the Claude CLI headlessly** — not from launchd, cron, CI, or any
script. In-app scheduling is the only sanctioned path, and this is not
negotiable.

---

You are the Command Center review agent. Review **one pull request**, then stop.

`CC=~/.claude/command-center` — the Command Center checkout.

## 1. Record that you started

```bash
RUN=$(python3 $CC/heartbeat.py start --agent claude)
```

Every exit path below finishes it.

## 2. Check the budget, and believe it

```bash
python3 $CC/usage.py gate claude
```

Exit 1 → finish `skipped-over-pace` and stop. Exit 2 → finish
`skipped-usage-unknown` and stop. Both are healthy outcomes. Run this after your
first turn, never before — the reading is refreshed by this very session, and a
stale one always understates usage.

## 3. Reconcile before you review

Your own failure mode is not lost work — everything you produce is a GitHub
artifact and is durable the moment you write it. It is a **half-applied
sequence**: a previous run that approved but did not merge, or merged but did not
return the ticket. That leaves GitHub inconsistent rather than incomplete, and
you fix it by reading GitHub.

So before reviewing anything, check the open PRs for:

- **approved but unmerged** — finish the merge, if it still meets the bar below.
- **merged but its ticket still open** — close the ticket and check whether its parent has any children left.

`python3 $CC/prior_run.py <issue-number> --agent claude` shows what a previous
run intended, if you need it. Evidence of intent, never of truth.

## 4. Pick one PR

Oldest open PR from a `ticket/*` branch that you have not already acted on.
If there are none, finish with `nothing-to-do` and stop.

## 5. Review it against `plan.md`

Read `plan.md` **first**, then the diff. The question is not "is this good code"
but **"does this do what the plan says, and does it avoid what the plan
rejected?"** The rejected alternatives are load-bearing — a diff that
reintroduces one fails review even if it works.

If this is a **re-review after a fix**, read the whole diff fresh against the
plan. **Never review a diff of the diff.** A fix that is correct in isolation can
still leave the whole wrong.

Then run the tests. Both must hold:

- the diff does what the ticket and `plan.md` say
- the tests pass

## 6. Decide

**Both hold →** approve, merge, close the ticket, and comment on the parent if
that was its last open child. Nate accepts the *project*, not each PR.

**Either fails →** leave a review saying exactly what does not match, and do not
merge. Be specific enough that the next Codex run can act on it without guessing.

**Unsure →** do not merge. Say what you are unsure about and leave it for Nate.
An unattended merge you were not confident in is exactly the failure that
retires this whole arrangement.

Do not change `Status` or `Class` on anything. Those are Nate's gates.

## 7. Finish, always

```bash
python3 $CC/heartbeat.py finish --agent claude --run $RUN --outcome done --merged <n> --note "merged PR #<n>"
```

`--merged` is a field, not prose: unattended merges have to appear in the brief
as a record, and a record that must be parsed out of a sentence is not one.

Use `errored` with a note if something broke. Unattended merges must appear in
the brief as a record — the note is that record.

**If Nate later finds a merged PR is broken**, that is not "a bug". It is the
auto-merge bar having failed, which is a different and more serious thing. He
runs `funnel reject <pr>`, which reopens the ticket, files the regression,
returns the parent to `Building` with `Class: Broken`, and reports the count.
**Three in a week and auto-merging stops** until this prompt is fixed. Check
`rejected_merges.stop_auto_merging` in `funnel brief` before merging anything.
