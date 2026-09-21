---
name: shape
description: The decision-record rules for shaping a Command Center idea into a plan — what precedent settles, what the agent decides itself, and what only Nate may answer. Use when grilling or thinking through an idea, when writing a plan, or when judging whether a plan's decision record is complete.
---

# Shaping ideas

Grilling is what feeds the whole funnel. Nothing reaches Codex that was not first
an idea, then a plan, then tickets — so an idea that never gets grilled is a
project that never happens.

This skill is the decision-record rules only. The runner records the plan and
applies the shaping decision; nothing here is a command to run.

**Shape what precedent covers, and never invent a decision that is his.**

## Decide first whether this needs him at all

**Most shaping does not.** Much of what reaches Ideas is applying an established pattern
to a new instance, and the precedent is already written down — in `plan.md`, in
`AGENTS.md`, or in a sibling project that solved the same problem. Grilling Nate about
something precedent already settles spends his attention on nothing.

Use the [capability boundary](../../AGENTS.md#capability-boundary) as a
closed-world test: does the plan require anything outside what an agent can
reach? Its three outcomes distinguish work any agent can do, work only Claude
Code can do in its local environment, and work no agent can do. The named access
cases are examples, not an exhaustive checklist; an unnamed capability gap still
counts.

## The decision record

The plan's decision record has three separate sections. Keep these names stable:

- **Decided from precedent** — cite a written source for anything `plan.md` or
  `AGENTS.md` already rules on, a sibling project's established convention, or a
  technical choice with an obvious answer given those conventions.
- **Decided by the agent** — record the agent's own engineering judgement when
  precedent does not settle a technical choice. Each entry must include its reasoning
  and the alternative it rejected, in the same shape as the plan's `Rejected` section.
  A bare line such as `decided: SQLite` is not a decision record.
- **Needs Nate** (also written **Needs you** in existing plans) — record the questions
  that only Nate may answer. This section must be empty for a plan to self-approve;
  use the stable heading spelling rather than inventing a synonym.

The middle section exists for the #31 anti-laundering principle one stage earlier:
the agent's own judgement must be marked as its own, never folded into the precedent
list where it would acquire authority it does not have. Nate can reverse a decision he
can see was made on his behalf; he cannot do that when it is disguised as a citation.

The plan must **separate the decision record from the open-question record**. "Decided
from precedent", "Decided by the agent", and "Needs Nate" (or the existing "Needs you")
are different sections; the latter is what he actually reads at the Shaped gate. Do not
merge either decision section into the other or use the decision headings for open
questions.

## Needs Nate

Leave to Nate, and say so explicitly rather than guessing:

- **anything the agent cannot reach under the capability boundary**
- **anything that changes his exposure** — how reachable the system becomes or what
  an unattended run may touch
- **anything touching a gate or who may write one**
- **scope and priority**: whether to build it at all, and how much
- anything where the plan would encode a preference rather than a deduction

"Needs you" (or the existing "Needs Nate" spelling) is **not** answered by an empty
section. Put the answer first on each category line. When all four categories are
clear, use this exact form:

```text
- Exposure: nothing outstanding. No new credentials or reachable surface.
- Gates: nothing outstanding. No gate ownership changes.
- Scope and priority: nothing outstanding. The scoped change is documented.
- Preference: nothing outstanding. No user-facing choice remains.
```

The bare answer must be `nothing outstanding`; any elaboration follows after a
period. When a category is open, replace that answer with the question itself in
one sentence, for example `- Gates: Who may write Ready for an all-clear plan?`.
An absent category is unanswered, not clear. These four answers are claims for the
verifier to check against the rest of the plan. If the plan implies a gate concern
that the section says is clear, the agent has made a **recorded wrong claim** rather
than a silent omission, which makes the miss findable afterwards. #84's own
"Needs Nate" section is the worked example: use its four category lines as the model.

When all four categories are clear, a self-approvable Class with `agent` origin
advances to `Ready` and gets a `Self-approved:` marker that `funnel brief` shows.
Any other case stays at `Shaped`, with the reason printed. An agent may never
bypass an open question: a plan held at `Shaped` waits on Nate's *is the plan
good?* gate, and moving to `Shaped` is recording a plan, not approval.

## Grilling

If an idea genuinely turns on his judgement, **grill him** rather than writing the
plan at him. Do not improvise a gentler version of the grilling method: the point
is to find the weaknesses now, while the idea is cheap to change, rather than
after Codex has built it.

**What you settle from precedent never enters the grilling frontier.** The grilling
method says work the tree until the frontier is empty and treats every decision as
his, but the frontier is only ever the decisions precedent does not cover. Anything
precedent settles is a fact you look up, settle, and cite — it is not a question,
and putting it to him anyway spends the resource this whole system protects.

What the grilling has to produce, because the next stages depend on it:

- **What the thing is**, concretely enough to be broken into tickets later. If it
  cannot be described that concretely, it is not ready — say so rather than
  padding it.
- **What was rejected, and why.** This is the part `AGENTS.md` calls the valuable
  one. A plan without its rejected alternatives gets re-litigated in a month, and
  the reviewer has no way to catch a diff that quietly reintroduces one.
- **What is still undecided.** Naming an open question is a result. Inventing an
  answer to it is a defect that gets implemented.

## Class it when you file it

Class a capture when you file it, using the ladder's own option names —
`Investigate`, `Broken`, `Maintenance`, `Improve`, `New`, `Replace`. Say why you
chose it, and say so plainly when you are unsure rather than guessing
confidently; he overrides freely and a stated doubt is cheaper to correct than a
confident error.

Use `Investigate` when the captured question is whether an observed defect exists,
or what the observed behaviour actually means, and the deliverable is evidence
that answers that question. It never holds the fix: once the evidence is posted
the investigation closes, and any work it calls for is captured as **new ideas**,
each classed for the work itself and linking back to the investigation.

**Still his:** the class on anything *he* raises, and any reclassification of
something already filed. Propose, do not set.
