---
name: decision-review
description: Review each decision waiting on Nate through a critical lens before putting it to him. Does it solve his actual problem or add upkeep? Is the benefit evidenced? Is there a simpler way? What truly needs his judgement? A fresh, context-free reviewer does the critique, never the session that built the work. Use whenever he asks what decisions are waiting on him or what needs deciding, invokes /funnel, or asks to review a gate, plan or proposal.
---

# Decision review

Nate, 2026-10-09: reviews of the decisions waiting on him were more useful when the reviewer did two things:
- asked whether a proposal was worth doing at all, not only whether its plan made sense;
- was independent of whoever built and ran the work, and so could challenge assumptions the builder had already accepted.

This skill makes both the default. It extends his 2026-09-27 rule, that fresh, context-free sub-sessions review the work (`independent-review`), from PRs to his decisions.

## When it runs

- **Triggers:** he asks what decisions are waiting on him or what needs deciding, invokes `/funnel`, or asks you to review a gate, a plan or a proposal.
- **Order:** get the list from the `funnel` skill first, then review the decisions in its order, one at a time. Never rank, reorder or filter them; `funnel.py` owns ordering.

## Who reviews: never the builder

For each decision, spawn a fresh general-purpose subagent with no context from this session. Give it only:

- the item's link and repository, the plan or result being decided, and the evidence it cites;
- read-only access to the repository, `plan.md` and `AGENTS.md`;
- the reviewer brief below.

Never pass on your own view of the item. If this session shaped, built or reviewed the item, say so, and let the fresh reviewer carry the critique. Run at most three reviewers at once, and give each its own scratch directory.

## The reviewer brief (paste into the agent's prompt)

> You review one decision waiting on Nate. You did not propose it and owe it nothing. Read the item and the evidence it cites. Read the repository's `plan.md`, whose "The problem" section states his priorities, and its `AGENTS.md`. Then answer four questions.
>
> 1. **Problem.** What problem of Nate's does this solve? Name it. Does the proposal solve it, or does it add upkeep? Count the upkeep: new jobs, rules, machinery, fields or schedules someone must keep running.
> 2. **Evidence.** Is the claimed benefit supported? Label each premise measured, documented or inferred, with its pointer. Check the cheapest one yourself. A benefit with no evidence behind it is a finding.
> 3. **Simpler.** Is there a simpler way to get most of the benefit? That includes doing less, waiting for a named trigger, or parking it. Name it concretely and say what it gives up.
> 4. **Who decides.** What in this decision genuinely needs Nate's judgement: exposure, gates, scope and priority, or a preference? What should go back to the agents first because it is missing, unverified or already settled by precedent?
>
> End with one verdict: approve; approve with named changes; send back for named work; or park, with the reason.
>
> Only real problems count: no hypothetical edge cases and no generic advice. Keep it under 400 words. You are read-only: no GitHub writes, no commands that write, and no `funnel.py` subcommands that spend GraphQL.

## What you do with the review

1. **Verify before relying on it.** Check the reviewer's load-bearing claims against the running system or the repository. A reviewer can be wrong, and a wrong premise put to Nate costs him a decision.
2. **Ask him one box at a time.** He often answers by voice or from another device, so the question box must stand alone. Put three things in it:
   - what deciding it means, in plain terms;
   - the concerns that survived your check;
   - your recommendation, as the first option and marked `(Recommended)`.

   Offer the reviewer's alternatives as the other options: approve, approve with changes, send back for named work, or park with a reason.
3. **Never recommend the option that hides a problem.** Parking or deferring a real failure to tidy the queue is not a recommendation.
4. **Don't ask what the agents can settle.** Anything that should go back for more work goes back on his answer, with the work named.
5. **Record the detail where agents read it.** The verification and the review go into the GitHub comment that records his answer, not into the box.

## Not this skill

- Reviewing a PR's code: use `independent-review`.
- Ordering or prioritising: `funnel.py` does it.
