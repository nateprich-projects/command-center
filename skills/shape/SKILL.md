---
name: shape
description: Work the Command Center's Ideas stage from chat — list ideas waiting to be shaped, capture a new one, or run a grilling session on an idea and record the resulting plan back to its issue. Use when Nate asks what ideas need shaping, what is in the backlog, wants to add or capture an idea, wants to grill or think through an idea, or asks to shape something. Also use after a grilling session to write the plan back to the funnel.
---

# Shaping ideas

Grilling is what feeds the whole funnel. Nothing reaches Codex that was not first
an idea, then a plan, then tickets — so an idea that never gets grilled is a
project that never happens.

Shaping is **on demand, never scheduled.** It starts new work, and the ladder says
in-flight work finishes first — a routine that shaped plans on a timer would spend the
same weekly budget that review and breakdown need for work already committed to. Nate
asks; it happens.

**Ideas and Shaped may grow without bound.** That is deliberate: bottom-up ordering and
the single-in-motion lock are what limit the system, not the size of a backlog. Do not
treat a long list as a problem to solve or apologise for.

`CC=/Users/nateprich/.claude/command-center`

## What's waiting

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py ideas
```

Flagged ones (`needs-shaping`) come first, then oldest. Show them and stop —
**do not start grilling until Nate picks one.** Ideas is deliberately unbounded
and guilt-free; a long list is not a backlog to apologise for, and it is
excluded from every count in `funnel brief` for that reason. Do not editorialise
about its length.

## Capturing a new one

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py capture "<title>" --note "<anything worth keeping>" --needs-shaping
```

Capture is cheap and is meant to be. Write down what he actually said rather than
a tidied version — the raw phrasing is often the part that reminds him what he
meant.

**Always pass `--needs-shaping`.** Every idea needs shaping, so the label is true of
all of them; Nate keeps it because it is what makes an idea recognisable when he is
browsing issues in GitHub. It is a stated fact, not a priority, and it is not yours to
withhold. Leaving it off does not mark something as merely parked — it silently sinks
that idea below every labelled one, because `funnel.py:217` still sorts on it. That is
how #15 came to sit last despite waiting longest. Tracked as #32, which removes the
flag entirely once it lands.

### Class it when you file it

_(Confirmed by Nate 2026-09-07 — "I want you to class these yourself when you
create them", replacing the 2026-09-05 rule that `Broken` was settable at entry
by him alone.)_

Class the item when you file it, using the ladder's own option names — `Broken`,
`Maintenance`, `Improve`, `New`, `Replace`. Say in the capture note why you chose
it, and say so plainly when you are unsure rather than guessing confidently; he
overrides freely and a stated doubt is cheaper to correct than a confident error.
`funnel.py` has no `class` command; use `SET_FIELD` with `CLASS_FIELD_ID` the way
`cmd_reject` does, and record who decided it in a comment — your own name now,
not his.

**Still his:** the class on anything *he* raises, and any reclassification of
something already filed. Propose, do not set.

**Why this became safe to delegate.** Nothing acts on `Class` without him today,
so setting it is a description rather than an authorisation. That changes when
#59 lands and `Broken`/`Maintenance`/`Improve` become classes an agent may shape
to `Ready` unattended — at which point an agent that both sets the class and acts
on it is self-authorising. #59's brake is the escalation re-tune in its ticket 1,
and it must land with that work, not after.

Do not add it to TickTick. That is the do-list, and ideas there would compete
with real tasks and erode its trustworthiness.

## Running the grilling

Read the issue first — `gh issue view <n> --repo <repo>` — so the session starts
from what he already wrote rather than asking him to repeat it.

### Decide first whether this needs him at all

**Most shaping does not.** Much of what reaches Ideas is applying an established pattern
to a new instance, and the precedent is already written down — in `plan.md`, in
`AGENTS.md`, or in a sibling project that solved the same problem. Grilling Nate about
something precedent already settles spends his attention on nothing.

So: **shape what precedent covers, and never invent a decision that is his.**

Decide from precedent, and cite where it comes from:

- anything `plan.md` or `AGENTS.md` already rules on
- conventions a sibling project established — deployment shape, file layout, testing
- technical choices with an obvious answer given those conventions

Leave to Nate, and say so explicitly rather than guessing:

- **anything that changes his exposure** — credentials, what is reachable from the
  internet, what an agent may do unattended
- **anything touching a gate or who may write one**
- **scope and priority**: whether to build it at all, and how much
- anything where the plan would encode a preference rather than a deduction

The plan must **separate the two**. "Decided from precedent (source)" and "Needs you"
are different sections, and the second is what he actually reads at the Shaped gate. If
the second list is empty, say so — that is a good outcome, not a suspicious one.

If an idea genuinely turns on his judgement, **grill him** rather than writing the
plan at him. Prefer his local `grilling` skill if it is installed
(`~/.claude/skills/grilling/`, the unscoped one); it is personal and deliberately
not carried in this repo, so on a fresh checkout it may be absent. Fall back to
`anthropic-skills:grilling` — but override its batching: the rules below and in
`~/.claude/CLAUDE.md` win over anything a skill says about asking several questions
at once. Do not improvise a gentler version
of it: the point is to find the weaknesses now, while the idea is cheap to change,
rather than after Codex has built it. Invoking it is not ceremony to be skipped
because you think you already have the question — that judgement is exactly what the
method exists to check.

**What you settle from precedent never enters the grilling frontier.** The two rules
above and the grilling method meet here, and the resolution is: `grilling` says work
the tree until the frontier is empty and treats every decision as his, but the
frontier is only ever the decisions in the *second* list above. Anything the first
list covers is a fact you look up, settle, and cite — it is not a question, and
putting it to him anyway spends the resource this whole system protects.

Ask **one question at a time**, in a question box, each carrying your recommendation.
That rule is his and lives in `~/.claude/CLAUDE.md`; the local `grilling` skill
follows it.

What the grilling has to produce, because the next stages depend on it:

- **What the thing is**, concretely enough to be broken into tickets later. If it
  cannot be described that concretely, it is not ready — say so rather than
  padding it.
- **What was rejected, and why.** This is the part `AGENTS.md` calls the valuable
  one. A plan without its rejected alternatives gets re-litigated in a month, and
  the reviewer has no way to catch a diff that quietly reintroduces one.
- **What is still undecided.** Naming an open question is a result. Inventing an
  answer to it is a defect that gets implemented.

## Recording the plan

Write the plan to a file, then:

```bash
python3 /Users/nateprich/.claude/command-center/funnel.py shaped <issue> --plan <file>
```

That writes the plan into the issue body, moves the item to `Shaped`, and clears
`needs-shaping`. The plan lives in the issue body through Ideas and Shaped; it
only becomes a repo's own `plan.md` at the Ready gate, if the work earns a repo.

**Moving to `Shaped` is not approval.** It records that a plan now exists. The
next gate — *is the plan good?* — is Nate's, and he answers it by moving the item
to `Ready`. Say that plainly at the end rather than implying the idea is now
greenlit.

## Do not

- **Do not set `Ready`.** That is his gate, and the breakdown routine treats
  `Ready` as his approval to create tickets. Setting it yourself starts work he
  never authorised.
- **Do not set a `Class` on anything *he* raised, and do not reclassify
  something already filed.** Propose it instead. Setting `Class` on your own
  captures is now expected — see "Class it when you file it" above.
- **Do not create repositories** or transfer issues. If the work looks like it
  needs its own repo, say so in the plan and leave it.
- **Do not grill more than one idea per session** unless he asks. His decision
  throughput is the bottleneck this whole system exists to protect.
