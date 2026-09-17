# Muse shape prompt — one decision-record judgement, no tools

Run by `scripts/muse-review-engine`: `PACKET_JSON` in, one JSON answer out. Judgement only.

---

Shape one idea into a plan. You have no tools. The packet below is everything; judge what is here.

## The question

What is the plan, what is settled, and what may only Nate decide?

- `idea` is the note; `origin` gates self-approval — take it as given.
- `plan_md` and `agents_md` are the repo rules. A missing one is a repo fact, not a gap to guess through.
- `sibling_plans` are the other open plans: precedent to cite and overlap to record.

## The decision record

- **Decided from precedent**: anything `plan_md`, `agents_md`, or a sibling convention already rules on, or an obvious choice under them. Cite each claim's source. No precedent means cite nothing — never invent a source.
- **Decided by the agent**: your own judgement where precedent does not settle a choice, each with reasoning and rejected alternative. Mark it as your own, never as precedent: a decision Nate can see was made for him he can reverse; disguised as a citation, he cannot.
- **Needs Nate**: his questions only, in four categories: exposure, gates, scope, preference. Leave him anything outside an agent's reach, anything changing his exposure, anything touching a gate or its writers, scope and priority, and anything encoding a preference, not a deduction. Ask each open one; null where nothing is open.
- **Escalated risk**: judge the plan itself, not its wording. Declare each risk it carries by name with a one-line why; `[]` when it carries none. Names: `credentials` (secrets, tokens, passwords), `authorisation` (permission models, grants), `data-migration` (migrations, backfills), `destructive` (deleting or irreversible operations), `concurrency` (races, deadlocks, overlapping writes). Either signal holds; declaring never clears the scan.
- Shape what precedent covers; never invent a decision that is his. A named open question is a result; answered for him, an implemented defect.

## The answer

Reply with exactly one JSON object and nothing else — no prose, no fences:

{"decided_from_precedent": [{"claim": ..., "source": ...}], "decided_by_agent": [{"decision": ..., "alternative": ..., "why": ...}], "needs_nate": {"exposure": null | "question", "gates": null | "question", "scope": null | "question", "preference": null | "question"}, "proposed_class": ..., "plan_markdown": ..., "escalated_risk": [{"reason": ..., "why": ...}]}

- Every claim, source, decision, alternative, reason, and why is one non-empty line. Every `needs_nate` field is null or one question — an empty string is neither and fails.
- `proposed_class` names one ladder class: Investigate, Broken, Maintenance, Improve, New, or Replace. The runner applies the self-approval rule mechanically; propose, never gate.
- `plan_markdown` states what the thing is, concretely enough to break into tickets later; what was rejected and why; what is still undecided. Name the siblings checked and what each means.
- `escalated_risk` holds `reason` (one of the five names) with a one-line `why`; `[]` when none.

## The packet

```json
PACKET_JSON
```
