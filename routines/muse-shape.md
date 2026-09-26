# Muse shape — no tools

`PACKET_JSON` → JSON.

---

What is the plan, what is settled, and what may only Nate decide?

- `idea` is note; `origin` gates self-approval.
- `plan_md`, `agents_md`, `sibling_plans`: rules and precedents; missing files are facts, not gaps.
- Read all `issue_thread` comments; their reasoning and `idea.body` corrections supersede premise labels. Do not repeat falsified mechanisms; put them in `Rejected` and cite the falsification.

## The decision record

- **Decided from precedent**: anything the packet's rules or a sibling convention settles. Cite each source; cite nothing when nothing settles it.
- **Decided by the agent**: judgement where precedent settles nothing, each with reasoning and rejected alternative, marked as yours and never as precedent.
- **Needs Nate**: keep only Nate-owned questions about exposure, gates and their writers, scope and priority, or preferences over deductions. Each category is null or a list of single questions.
- **Escalated risk**: judge the plan, not its wording. Declare each risk — `credentials` (secrets), `authorisation` (permission models), `data-migration` (backfills), `destructive` (deleting), `concurrency` (races) — with a one-line why; `[]` when none. Declaring never clears the scan.
- **Premises**: list each factual plan claim with a `file:line`, command/output, or rollout/record pointer and an honest label: `measured` (observed), `documented` (unverified vendor claim), or `inferred` (could be wrong). Use `[]` only when there are no factual premises. The runner renders them; omit them from `plan_markdown`.

## What never reaches him

- **Sequencing.** Put named-ticket order, pins, or activations in `depends_on` as `owner/repo#n`; whether to build stays Scope.
- **Machine-local paths** use the documented runtime root and stay out of Git. Give an owner-only subdirectory a reversible default and record its rollback as your decision.
- **Technical defaults are yours.** Choose one canonical source with thin adapters; record assumptions and rollback as decisions, not preference questions.
- **Check precedent first** against packet rules and siblings; cite exact matches instead of asking.
- **Ask atomically** so settled facts do not travel with open questions.

## The answer

Reply with exactly one JSON object and nothing else — no prose, no fences:

{"decided_from_precedent": [{"claim": ..., "source": ...}], "decided_by_agent": [{"decision": ..., "alternative": ..., "why": ...}], "needs_nate": {"exposure": null | ["question"], "gates": null | ["question"], "scope": null | ["question"], "preference": null | ["question"]}, "proposed_class": ..., "plan_markdown": ..., "escalated_risk": [{"reason": ..., "why": ...}], "depends_on": ["owner/repo#n"], "premises": [{"claim": ..., "evidence": ..., "label": "measured" | "documented" | "inferred"}]}

- Every claim, source, decision, alternative, reason, why, evidence, label, and question is one non-empty line. `needs_nate` fields are null or non-empty lists of single questions.
- `proposed_class` names one ladder class: Investigate, Broken, Maintenance, Improve, New, or Replace. Propose, never gate.
- `plan_markdown` states what it is, concretely enough to ticket; what was rejected and why; what is undecided; siblings checked and what each means.
- `escalated_risk` holds `reason` with `why`; `[]` when none. `depends_on` holds `owner/repo#n` refs; `[]` when the plan waits on nothing.

## The packet

```json
PACKET_JSON
```
