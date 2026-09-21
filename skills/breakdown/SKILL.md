---
name: breakdown
description: The sizing standard for breaking an approved Command Center plan into tickets Codex can work one at a time. Use when breaking down a plan, decomposing an approved plan into sub-issues, sizing tickets for an agent run, or when judging whether an existing ticket is too large for one run.
---

# Breaking a plan into tickets

This exists because **fifty unattended breakdowns with no shared memory will
drift**, in exactly the way `plan.md` says two agents implementing rules from
prose will drift. The model can do this well once, watched. The point of writing
it down is that it is done the same way the fiftieth time.

This skill is the sizing standard only. The runner creates the tickets, writes
the `Risk:` lines, and records the dependency edges; nothing here is a command
to run.

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
finish, so replace them with the measured p90 once there is real data.

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

Tickets should be workable in any order where possible: one ticket per run, and
the ladder decides what to start.

Where order genuinely matters, record the dependency on the ticket so the queue
can enforce it. If *every* ticket is chained, the plan has not really been
broken up: it has been sliced into stages, and the funnel will process them one
hourly run at a time with no parallelism gained. Look for a different cut.

## Coverage

Together, the tickets must cover the plan's stated outcome, not only its
headings or ticket shape. A set of tickets can mirror every heading and still
leave the thing unusable. Before finishing:

- reread the plan's own headings and check each one is represented
- state the plan's purpose and trace the ticket set from setup through its
  dependencies, registration or connection steps, and final usable state
- map each stated outcome to the tickets that deliver it end to end, naming the
  usable end state, not only the component each ticket builds

Use #25 as the worked failure: its tickets can cover a running server and
connector while missing the outcome — reaching the funnel from general chat —
if no ticket registers the connector in Nate's account. That breakdown is
shape-complete but outcome-incomplete. Surface a missing step as its own ticket,
or record the concrete reason it is deliberately left out; do not claim coverage
because every implementation heading has a ticket.

**Anything you deliberately left out, say so plainly** — silent omission is how
a project looks finished while missing a third of itself.

## Capability

Keep capability separate from ticket sizing: a step can be small and still be
outside an agent's reach. Use the
[capability boundary](../../AGENTS.md#capability-boundary) as the closed-world
test for whether a planned step is work an agent can take on. Difficulty,
uncertainty, unfamiliarity, or a model's lack of skill is never a reason to
route work away from agents.

**One human action per human-step ticket.** When a plan item mixes agent work
with a human step, split the human step out rather than marking the mixed ticket
in place: marking it blocks the agent work that could have proceeded, or hides
the human step inside a ticket that looks complete once closed. Account setup
and credential creation are two tickets, not one — a ticket holding two human
steps is half-done the moment one of them finishes, and half-done is
indistinguishable from done once it is closed.

## When the plan will not decompose

Some plans cannot be broken up honestly, and forcing it produces tickets that
look like work and are not.

If the plan is too vague to size — it says what to achieve but not what to build
— and no recorded answer settles the undecided decision, **do not invent the
missing decision.** Ask the precise question with no tickets rather than
building on an invention: a ticket built on an invented decision is worse than
no ticket, because someone will implement it.

If the plan is genuinely one indivisible piece of work, make it one ticket and
say why.

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
