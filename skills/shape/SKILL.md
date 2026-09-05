---
name: shape
description: Work the Command Center's Ideas stage from chat — list ideas waiting to be shaped, capture a new one, or run a grilling session on an idea and record the resulting plan back to its issue. Use when Nate asks what ideas need shaping, what is in the backlog, wants to add or capture an idea, wants to grill or think through an idea, or asks to shape something. Also use after a grilling session to write the plan back to the funnel.
---

# Shaping ideas

Grilling is what feeds the whole funnel. Nothing reaches Codex that was not first
an idea, then a plan, then tickets — so an idea that never gets grilled is a
project that never happens.

It is also the one part that cannot be scheduled, which `plan.md` treats as a
feature: it is the structural throttle on the system, so no artificial cap on
throughput is needed. **One at a time. Do not offer to grill several.**

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
meant. `--needs-shaping` marks it as worth thinking through; leave it off for a
thought he is merely parking.

Do not add it to TickTick. That is the do-list, and ideas there would compete
with real tasks and erode its trustworthiness.

## Running the grilling

Read the issue first — `gh issue view <n> --repo <repo>` — so the session starts
from what he already wrote rather than asking him to repeat it.

Then **use the `grilling` skill** to do the actual work. Do not improvise a
gentler version of it: the point is to find the weaknesses now, while the idea is
cheap to change, rather than after Codex has built it.

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
- **Do not set `Class`.** Ideas need none, and anything past Ideas is his call.
- **Do not create repositories** or transfer issues. If the work looks like it
  needs its own repo, say so in the plan and leave it.
- **Do not grill more than one idea per session** unless he asks. His decision
  throughput is the bottleneck this whole system exists to protect.
