---
name: breakdown
description: How to break an approved Command Center plan into tickets Codex can work one at a time. Use when breaking down a plan, decomposing an approved plan into sub-issues, sizing tickets for an agent run, or when the Claude routine reaches its job-two breakdown step. Also use when judging whether an existing ticket is too large for one run.
---

# Breaking a plan into tickets

This exists because **fifty unattended breakdowns with no shared memory will
drift**, in exactly the way `plan.md` says two agents implementing rules from
prose will drift. The model can do this well once, watched. The point of writing
it down is that it is done the same way the fiftieth time.

## The unit

**One ticket is one Codex run: work that ends in a pull request.**

A run is killed by the budget gate before it starts, or by a rate limit while it
works. A ticket too big to finish in one run does not fail cleanly — it dies
partway, leaves a half-finished branch, and has to be resumed by a later run
reading its predecessor's transcript. That is expensive and lossy. **Erring small
costs an extra PR. Erring large costs a dead run.**

## Sizing

These are **starting guesses, not measurements.** No one-ticket run had ever
happened when they were written. The heartbeat records usage at run start and
finish, so replace them with the measured p90 once there is real data —
`heartbeat.py read --agent codex` — and update this file when you do.

A ticket is about right if:

- it touches **one concern** — a module, a behaviour, a workflow
- the diff would be **a few hundred lines at most**
- it has **one way to tell it worked**: a test, a command with expected output, a
  visible behaviour
- you could write its PR description **before** doing the work

A ticket is too big if you catch yourself writing "and" in its title, or if the
plan's own paragraph about it has sub-bullets that each need their own tests.

Split by **behaviour, not by layer.** "Add the parser" then "add the tests" then
"wire it up" produces three tickets of which only the last one works. Prefer
"parse X, with tests" — a slice that is complete and verifiable on its own.

## Ordering and independence

Codex works **one ticket per run** and the ladder decides what to start, so
tickets should be workable in any order where possible.

Where order genuinely matters, say so in the ticket body — "depends on #N" — and
put the dependency in the body rather than inventing a label. The label set is
closed at two on purpose.

## The capability boundary

Use the [capability boundary](../capability-boundary.md) as the closed-world test
when deciding whether a planned step is work an agent can take on. Ask whether the
step requires anything outside that boundary; the named access cases are examples,
not an exhaustive checklist. Keep this test separate from ticket sizing: a step can
be small and still be outside the agent's reach.

If a ticket cannot start until another finishes, that is fine. If *every* ticket
is chained, the plan has not really been broken up: it has been sliced into
stages, and the funnel will process them one hourly run at a time with no
parallelism gained. Look for a different cut.

## Capability boundary

When deciding whether a plan step is within an agent's reach, use the
[capability boundary in `AGENTS.md`](../../AGENTS.md#capability-boundary) as a
closed-world test: does the step require anything outside what an agent can reach?
The access cases named there are examples, not an exhaustive list, so an unnamed
requirement outside the boundary counts too.

## Boundary checklist at breakdown

Run this checklist after reading the plan and before creating its tickets. It is a
recall step: do not wait for a human step to announce itself. Start with every plan
heading and trace its stated outcome to a usable end state, including setup,
credentials, account configuration, and registration or connection steps that the
plan forgot to name.

For every concrete action or missing prerequisite found in that pass, answer each
prompt below explicitly in the parent coverage comment. Write `no`; silence is not
an all-clear.

- **Application or browser UI:** does the action require a UI or other surface the
  agent cannot reach? `yes`/`no`.
- **Credential:** must a credential be created, entered, retrieved, or stored
  outside the checkout? `yes`/`no`.
- **Account or billing setting:** must an account, billing, or service setting be
  changed? `yes`/`no`.
- **Physical access:** must someone touch or access a machine or device? `yes`/`no`.
- **Other boundary gap:** does it require anything else beyond the agent's shell,
  `gh` and its token, and the checkout filesystem? `yes`/`no`; name it if `yes`.

These prompts are examples, not a replacement for the closed-world test in
`AGENTS.md`. A `no` means the action is reachable with the stated agent
capabilities, not merely that the plan did not mention it. Difficulty, uncertainty,
unfamiliarity, or a model's lack of skill is never a human-step reason. If an answer
is `yes`, make that one action its own human-step ticket; do not mark a mixed
engineering ticket in place. Record the ticket and its dependency edge in the same
comment.

Use this shape for the parent coverage comment so a later reader can see what was
considered rather than only what was claimed:

```text
Boundary checklist

- Plan action or missing prerequisite: <one action>
  - Application/browser UI: yes/no
  - Credential: yes/no
  - Account or billing setting: yes/no
  - Physical access: yes/no
  - Other boundary gap: yes/no — <name if yes>
  - Result: agent ticket #N / human-step ticket #N / no ticket

Coverage by plan heading: <plan heading> -> <ticket refs>
Outcome coverage:
- <plan's stated outcome or purpose>: <ticket refs> -> <usable end state>
Human-step dependencies: <ticket> depends on #N
Deliberately left out: <omission and why, or “none”>
```

The checklist must be concrete enough to catch the known failure in #25. Its
walkthrough must surface all three of these, even though the third was absent from
the original plan: Cloudflare tunnel setup requires an account setting; the
fine-grained token requires credential creation or entry; and registering the
connector in Nate's account requires an application or account UI. Each is a
separate human action to record and ticket, not an assumption hidden in an
engineering ticket.

## Contradiction check for an all-clear

The JSON from `funnel begin --breakdown` carries `work.access_signals`, the
canonical scan of the parent plan's access-shaped vocabulary from `funnel.py`.
Do not copy the vocabulary into this skill. The scan is a detector beside the
checklist, not a second set of human-step categories:

- If the signal list is non-empty and every boundary answer is `no`, flag an
  all-clear contradiction before creating tickets. Re-read each matching plan
  passage and trace it to a concrete action or missing prerequisite.
- If a matching passage names an action outside the boundary, split that action
  into its own human-step ticket. If it is only a rejected alternative, an
  example, or an action already reachable to the agent, keep the all-clear and
  record why the signal was cleared. A vocabulary match alone never creates a
  human-step ticket.
- If the signal list is empty, record that the contradiction check was not
  triggered; an empty scan is not proof that the plan has no human step.

Add these lines to the parent coverage comment alongside the checklist:

```text
Access vocabulary: <signals from work.access_signals, or “none”>
All-clear contradiction: <not triggered / flagged — resolution>
```

The #25 walkthrough must surface at least `tunnel` and `token`; those signals
are evidence to inspect, not proof that every mention requires Nate.

## Coverage

Together, the tickets must cover the plan's stated outcome, not only its
headings or ticket shape. A set of tickets can mirror every heading and still
leave the thing unusable. Before finishing:

- reread the plan's own headings and check each one is represented
- state the plan's purpose and trace the ticket set from setup through its
  dependencies, registration or connection steps, and final usable state
- in the parent coverage comment, map each stated outcome to the tickets that
  deliver it end to end. Include the usable end state, not only the component
  that each ticket builds
- use #25 as the worked failure: its six tickets can cover a running server and
  connector while missing the outcome — reaching the funnel from general chat —
  if no ticket registers the connector in Nate's account. That breakdown is
  shape-complete but outcome-incomplete. Surface the missing registration as a
  human-step ticket under the boundary checklist, or record the concrete reason
  it is deliberately left out; do not claim coverage because every
  implementation heading has a ticket
- **anything you deliberately left out, say so in a comment on the parent** —
  silent omission is how a project looks finished while missing a third of itself

## Rules that are not negotiable

- **Every project gets at least one ticket**, even trivial work. A parentless
  item is a project, never a ticket; allowing something to be both is what once
  put the same issue in Nate's queue and Codex's queue at the same time.
- **Create sub-issues of the parent**, so they join the Project automatically.
- **Do not set `Status` or `Class` on tickets.** They inherit. Blank fields on a
  child are correct.
- **Do not create repositories**, apply topics, or transfer issues. If the work
  needs its own repo, say so in a comment and leave it for Nate.
- **Do not change the parent's `Status`.** Nate's writing `Ready` was his gate;
  your breakdown is what makes `Ready` true.

## When the plan will not decompose

Some plans cannot be broken up honestly, and forcing it produces tickets that
look like work and are not.

If the plan is too vague to size — it says what to achieve but not what to build
— **do not invent the missing decisions.** Comment on the issue saying precisely
what is undecided, and leave it. It needs another grilling pass, which is
interactive and not yours to do. A ticket built on an invented decision is worse
than no ticket, because someone will implement it.

If the plan is genuinely one indivisible piece of work, make it one ticket and
say why in a comment.

## A worked example

Plan: *"Add a `funnel park` command. It should require a written reason, set
Status to Parked and state_reason to not_planned, and refuse if no reason is
given. The reason is the artifact that makes re-encountering the idea in four
months a 30-second decision."*

**Good** — two tickets, each verifiable alone:

1. *`funnel park <ref> --reason` — set Parked and not_planned, refuse without a reason.* Fixture tests for the refusal and for both fields being written.
2. *Surface parked items and their reasons in `funnel brief`.* Test that a parked item's reason appears and that it is excluded from the gate counts.

**Bad** — split by layer, so only the last one works:

1. Add the argument parsing
2. Add the GitHub mutation
3. Add the tests

**Also bad** — one ticket: *"Add park support to funnel and the brief and the
skill and document it."* Four concerns and an "and" in the title.
