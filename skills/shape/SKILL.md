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
exposure change, any gate change, scope and priority (except scheduling on
agent-origin plans; see below), any encoded preference. Render only
categories with an open question:

```text
- Gates: Who may write Ready?
```

The typed shaping answer still carries all four categories as null or a list;
the runner writes `Needs: human` when any list is open and `Needs: none` when
all are null.

### Scheduling is the project manager's decision

For an **agent-origin** `Investigate`, `Broken`, `Maintenance`, or `Improve`
plan, timing, priority, and sequencing are project-manager decisions. Never
emit a question solely about one of those decisions in `Needs Nate`; record the
chosen ordering under **Decided by the agent**. A clear choice to wait for a
named ticket belongs in `depends_on` as an `owner/repo#n` reference. If the
choice is to proceed without waiting, keep named tickets as context and leave
`depends_on` empty. For the recorded The-League#258 case, “land now” while
#165 and #174 are in flight meant no dependency: preserve that decision, keep
the refs as context, and do not turn the alternative “wait” into a hold.

Keep Exposure, Gates, and Preference questions in `Needs Nate`. Under Scope,
keep a concrete unresolved stakeholder tradeoff even if it mentions timing,
and keep a question whose category is unclear. These are the countercases to
the scheduling rule; classify by meaning, not keywords alone. Apply the rule
only to agent-origin plans in the four classes above. Leave Nate-origin plans
and `New` and `Replace` classes untouched.

The shared engine enforces this rule in `engine/shape.py`. Keep this mirror
aligned with its recorded `#258` replay and countercases in
`tests/test_engine_shape.py`: a clear wait for a named ticket becomes
`depends_on`; a real scope tradeoff, Exposure, Gates, Preference, and unclear
questions remain open; Nate-origin plans and `New` and `Replace` classes stay
untouched. The `#258` land-now decision advances without a dependency and
keeps its recorded ordering decision.

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
confident error. Choose the class for what the tickets will deliver: a study,
measurement, documentation recording, or product research with no
possible-defect question takes the class that fits its deliverable, never
`Investigate`. When that work changes no behaviour, include the analysis marker
above.

Use `Investigate` only to decide whether a possible defect exists. The engine
requires exactly one non-empty whole line of the form
`Possible defect: <statement>` in the plan, naming the defect the evidence will
decide. This is a thin mirror of `engine/shape.py`, which defines the check. The
recorded examples live in `tests/fixtures/investigate_shape_excerpts.json` and
are exercised by `tests/test_engine_shape.py`. An `Investigate` plan delivers
evidence, never the fix: follow-ups are new ideas, classed for the work and
linked back. **Still his:** the class on anything *he* raises, and any
reclassification. Propose, do not set.
