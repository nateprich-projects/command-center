---
name: shape
description: Decision-record rules for shaping an idea into a plan. Use when grilling, writing a plan, or checking a record.
---

# Shaping ideas

The decision-record rules only. The runner records the plan and applies the
shaping decision; nothing here is a command to run. **Shape what precedent
covers, and never invent a decision that is his.**

## Decide first whether this needs him at all

**Most shaping does not.** Most Ideas apply a written pattern; grilling him
about settled precedent spends the attention this system protects. Use the
[capability boundary](../../AGENTS.md#capability-boundary) closed-world
test; unnamed gaps count.

## The decision record

Three separate sections; keep these names stable:

- **Decided from precedent** — anything `plan.md` or `AGENTS.md` rules on,
  a sibling convention, or an obvious technical choice.
- **Decided by the agent** — the agent's own judgement with reasoning and
  rejected alternative. `decided: SQLite` is not a record.
- **Needs Nate** (also **Needs you**) — questions only Nate may answer;
  must be empty for a plan to self-approve.

Never fold the agent's judgement into precedent, and never use decision
sections for open questions.

## Needs Nate

Leave to Nate, explicitly rather than guessing: anything unreachable, any
exposure change, any gate change, scope and priority, any encoded
preference. Put the answer first on each category line; when clear, use this
exact form:

```text
- Exposure: nothing outstanding. No new credentials or reachable surface.
- Gates: nothing outstanding. No gate ownership changes.
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.
```

The bare answer is `nothing outstanding`; elaboration follows a period. An
open category carries the question in one sentence; an absent category is
unanswered, not clear. The verifier checks these claims against the plan.

When all four are clear, a self-approvable Class with `agent` origin
advances to `Ready` with a `Self-approved:` marker; anything else stays at
`Shaped` with the reason printed. Never bypass an open question: `Shaped`
records a plan, not approval.

## Grilling

If an idea turns on his judgement, **grill him** while it is cheap. Settled
precedent never enters the frontier: look it up, cite it, do not ask it.
Produce: **what the thing is** concretely enough to ticket; **what was
rejected, and why**; **what is still undecided** — naming it is a result,
inventing an answer is a defect.

## Class it when you file it

Class a capture with the ladder names — `Investigate`, `Broken`,
`Maintenance`, `Improve`, `New`, `Replace` — and say why; stated doubt beats
confident error. `Investigate` asks whether a defect exists and delivers
evidence, never the fix: follow-ups are new ideas, classed for the work and
linked back. **Still his:** the class on anything *he* raises, and any
reclassification. Propose, do not set.
