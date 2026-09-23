---
name: breakdown
description: The sizing standard for breaking an approved Command Center plan into tickets Codex can work one at a time. Use when breaking down a plan or judging whether a ticket is too large.
---

# Breaking a plan into tickets

The sizing standard only. The runner creates the tickets, writes the
`Risk:` lines, and records the dependency edges; nothing here is a command
to run. One shared standard keeps the fiftieth breakdown shaped like the first.

## The unit

**One ticket is one Codex run: work that ends in a pull request.**

A run can be killed by the budget gate or a rate limit mid-work. A ticket
too big for one run dies partway, leaves a half-finished branch, and forces
a lossy resume from a transcript. **Erring small costs an extra PR. Erring
large costs a dead run.**

A ticket is about right if it touches **one concern**, diffs **a few hundred lines at most**, has **one way to tell it worked**, and its PR description could be written **before** the work. It is too big if the title
needs "and", or the plan's paragraph about it has sub-bullets each needing
their own tests. Split by **behaviour, not by layer**: "parse X, with
tests" beats "add the parser" then "add the tests" then "wire it up".

## Ordering and independence

Tickets should be workable in any order: one ticket per run, and the ladder
decides what to start. Where order genuinely matters, record the dependency
on the ticket so the queue can enforce it. If *every* ticket is chained,
the plan was sliced into stages, not broken up — look for a different cut.

When a ticket cannot start before a calendar date, keep the plain-language
`Not before YYYY-MM-DD` sentence in its body, give it the `blocked` label,
and post a comment in the canonical form `**Blocked until YYYY-MM-DD:**`.
Set the comment date to one day after the date in the sentence. Use a native
`blocked-by` edge when a ticket is waiting for another ticket; represent a
calendar date with the label and comment instead.

**Example:** For #1138, whose body says `Not before 2026-10-03`, keep that
sentence in the body, add the `blocked` label, and comment
`**Blocked until 2026-10-04:**`.

## Coverage

Cover the plan's stated outcome, not its headings. Reread the plan, trace
setup through dependencies and registration to the usable end state, and map
each outcome to its tickets. **Anything deliberately left out, say so
plainly** — silent omission is how a project looks finished while missing a
third of itself.

## Capability

Keep capability separate from sizing: small can still be out of reach. Use
the [capability boundary](../../AGENTS.md#capability-boundary) as the
closed-world test. Difficulty or unfamiliarity never routes work away from
agents. **One human action per human-step ticket**: split mixed tickets so
agent work is not blocked inside a human wait.

## When the plan will not decompose

If the plan is too vague to size and no recorded answer settles it, **ask
the precise question with no tickets** rather than building on an invention.
If it is genuinely one indivisible piece, make it one ticket and say why.

## A worked example

*"Add `funnel park`: require a written reason, set Parked and not_planned,
refuse without one."* **Good** — two tickets, each verifiable alone: the
command with refusal tests, then surfacing parked reasons in the brief.
**Bad** — by layer (parsing, mutation, tests), so only the last one works;
or one ticket with "and" joining four concerns.
