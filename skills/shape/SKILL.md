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

## Analysis plans wait by default

A plan whose tickets change no behaviour and deliver a finding, measurement,
review, comparison, or recommendation is analysis. It waits for Nate's
acceptance whatever its Class or origin. Write this marker into the plan body
while shaping:

<!-- command-center-analysis -->

```json
{"analysis": true}
```

Apply the marker even to `Maintenance` and other upkeep classes; Class does not
decide whether analysis waits. If Nate expressly exempts a particular analysis,
write no marker and quote his exact words on the `Scope and priority` line in
`Needs you` as the audit trail. Do not infer an exemption.

## The decision record

Three separate sections; keep these names stable:

- **Decided from precedent** — anything `plan.md` or `AGENTS.md` rules on,
  a sibling convention, or an obvious technical choice.
- **Decided by the agent** — the agent's own judgement with reasoning and
  rejected alternative. `decided: SQLite` is not a record.
- **Needs Nate** — questions only Nate may answer. Omit the section when
  there are none; `Needs: none` is the canonical all-clear record.

Never fold the agent's judgement into precedent, and never use decision
sections for open questions.

## Factual premises

Record each factual claim the plan relies on as a premise, with its evidence
pointer and an honest label from the `LEARNINGS.md` scale. Evidence points to
something another reader can inspect: a `file:line`, a command together with
its output, or a rollout or record reference. Use `measured` for directly
observed evidence, `documented` for a vendor claim that has not been verified,
and `inferred` for a claim that could be wrong.

Premises are the structured `premises` answer field defined and validated by
`engine/shape.py`; the runner renders them into the plan body. Keep them out of
`plan_markdown` so the record has one copy. Use an empty list only when the
plan relies on no factual premises.

## Needs Nate

Leave to Nate, explicitly rather than guessing: anything unreachable, any
exposure change, any gate change, scope and priority, any encoded
preference. Render only categories with an open question:

```text
- Gates: Who may write Ready?
```

The typed shaping answer still carries all four categories as null or a list;
the runner writes `Needs: human` when any list is open and `Needs: none` when
all are null.

When all four are null, a self-approvable Class with `agent` origin and
`Risk: standard`
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
