# Claude routine: reviews, escalated shaping

**Claude Code Routine**, never headless:
**review, then breakdown, then shaping**.

## Begin

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent claude --tier escalated
```

- `"do": "stop"`: finish with the outcome its `gate` names —
  `over`/`unknown`/`reserve` →
  `skipped-over-pace`/`skipped-usage-unknown`/`skipped-api-reserve`,
  else `nothing-to-do`.
- `"do": "review"`: `work` names the PR; `"do": "shape"`: an idea.

Pass the run id printed by this run's `begin` output as
`--run <id>`, never an id from an earlier `begin` in the same session. If
`heartbeat finish` refuses a run/work mismatch, use that id in `--run` and
retry. Never wrap the id in `RUN=$(...)`.

## Review

Reconcile: approved-but-unmerged, merged-but-ticket-open. Read `plan.md`
**first**, then the whole diff: **never a diff of the diff.** Bar: **does it
do what the plan says, and avoid what the plan rejected?** Tests must pass.
Touching `.claude/settings.json`, `routines/`, `skills/`, `AGENTS.md`,
`plan.md` fails unless the ticket asked.

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py review <pr> --verdict approved --ci green
python3 /Users/nateprich/.claude/command-center-run/funnel.py merge <pr> --yes
```

`merge` re-checks everything at the approved head. Rejections carry
`--blocking`; **unsure means do not merge.** Never change `Status` or
`Class`: the gate reads the rejected-merge counter itself.

## 3. Shape one escalated idea

While this Claude routine is off, the escalated Muse schedule carries escalated idea
shaping.

**Do not grill**: settle what precedent covers, cite
the source, open questions in `Needs you`:

```text
- Exposure: nothing outstanding. No new credentials or reachable surface.
- Gates: nothing outstanding. No gate ownership changes.
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.
```

When all four are clear, a self-approvable class with `agent` origin
advances to `Ready` with a `Self-approved:` marker that `funnel brief`
shows; anything else stays at `Shaped`, with the reason printed. **Shaped
is not approval.** If the capture origin is `agent` and Class is unset,
pass `--class <Broken|Maintenance|Improve|New|Replace>`; otherwise add a
`Proposed class: <one ladder name>` line and no `--class`. When Nate
explicitly authorises `approve --yes` while Class is unset, the
command adopts one exact whole-line `Proposed class:` value before the
Status write. Adoption fills the field but is not the plan-good decision
(#59 brake: no `Shaped` gate, no auto-advance to `Ready`).

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py shaped <ref> --plan <file>
```


## Capture

When this run observes a defect (broken behaviour, a failing command, or a
misbehaving run — evidence, not speculation), record it before finishing
with `funnel capture`. Put the observed evidence in the note, choose its
class at capture using `skills/shape`'s "Class it when you file it" rule,
and say why. Agents class their own captures, never his existing issues.

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py capture "<title>" --repo nateprich-projects/command-center --origin agent --class <Broken|Maintenance|Improve|New|Replace> --note "<evidence>"
```

This is the sanctioned exception to the review rule to act only on the PR
you were given: capture records the observed defect; it does not act on the
thing observed.

## Finish

```bash
python3 /Users/nateprich/.claude/command-center-run/funnel.py snapshot | jq '.brief.awaiting_breakdown'
python3 /Users/nateprich/.claude/command-center-run/heartbeat.py finish --agent claude --run <id> --outcome done --merged <pr> --note "<what shipped>"
```

`--merged` is the PR number, never a count. Nothing landed:
`skipped-blocked`. Breakage: `errored`.
