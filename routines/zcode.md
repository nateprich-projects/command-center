# zcode routine — RETIRED 2026-09-09

No longer runs; history in LEARNINGS.md. Protocol stays pinned below.

**zcode scheduled task**, Mac mini, never headless. One job per run:
**review, then breakdown, then shaping**.

## 1. Start

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent zcode --tier standard --breakdown
```

- `"do": "stop"` — finish with the outcome its `gate` names:
  `over`/`unknown`/`reserve` →
  `skipped-over-pace`/`skipped-usage-unknown`/`skipped-api-reserve`,
  else `nothing-to-do`. Then stop.
- `"do": "review"` — one PR. `"do": "breakdown"` — one project.
  `"do": "shape"` — one idea; section 4.

**Keep `run`.** Pass the run id printed by this run's `begin` output as
`--run <id>` — never an id from an earlier `begin` in the same session. If
`heartbeat finish` refuses a run/work mismatch, use that id in `--run` and
retry. Never wrap the id in `RUN=$(...)`.

## 2. Review against `plan.md`

Reconcile: approved-but-unmerged, merged-but-ticket-open. Read `plan.md`
**first**, then the whole diff — **never a diff of the diff.** Bar:
**does this do what the plan says, and does it avoid what the plan
rejected?** Read-only: no clone, no checkout, no writes beyond the
spool. CI green as reported by `gh pr checks`, or reject. Touching
`.claude/settings.json`, `routines/`, `skills/`, `AGENTS.md`, `plan.md`
fails unless the ticket asked.

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py review <pr> --verdict approved --ci green
python3 /Users/nateprich/.claude/command-center-run/funnel.py merge <pr> --yes
```

That PR only: never touch another. Rejections carry a `--blocking`
note; **unsure means do not merge.** Do not change `Status` or `Class`:
the gate reads the rejected-merge counter itself and refuses while
auto-merging is stopped.

## 3. Breakdown, only with no PR

Take the oldest ticketless plan from the snapshot — never a live brief:

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py snapshot | jq '.brief.awaiting_breakdown'
```

One ticket is one engineer run ending in a PR; split by behaviour. Every
body carries `Risk: standard` or `Risk: escalated`. **Do not create
repositories.** Vague plans get questions posted, not inventions.

## 4. Shape one standard-tier idea

`begin` named it. **Do not grill** — settle what precedent covers, cite
the source, open questions in `Needs you`:

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py shaped <ref> --plan <file>
```

Moving to `Shaped` records a plan; **Shaped is not approval**. Do not set
`Ready`, `Status`, or `Class`.

## Capture defects

When this run observes a defect (broken behaviour, a failing command, or a
misbehaving run — evidence, not speculation), record it before finishing
with `funnel capture`. Put the observed evidence in the note, choose its
class at capture using `skills/shape`'s "Class it when you file it" rule,
and say why. Agents class their own captures, never his existing issues.

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py capture "<title>" --repo nateprich-projects/command-center --note "<evidence>"
```

This is the sanctioned exception to the review rule to act only on the PR
you were given: capture records the observed defect; it does not act on the
thing observed.

## Finish

```bash
python3 /Users/nateprich/.claude/command-center-run/heartbeat.py finish --agent zcode --run <id> --outcome done --merged <the PR number, e.g. 96> --note "<what shipped>"
```

`--merged` is a PR number, not a count. Unlanded ground, no change:
`skipped-blocked`. Breakage: `errored`.
