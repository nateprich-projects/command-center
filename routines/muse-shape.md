# Muse shape prompt: judgement, no tools

---

## The question

What is the plan, what is settled, and what may only Nate decide?

- `idea` is the note; `origin` gates self-approval.
- `plan_md`, `agents_md`, `sibling_plans` are rules/precedents; missing files are facts, not gaps.
- `issue_thread` is complete/chronological; send-back reasoning or body corrections override premise labels. Drop falsified premises, move their mechanisms to `Rejected`, and cite falsification.
- **Broken** is an observed failure or a security or privacy exposure; any other defect found by reading, review or tests is **Bug**. A Broken plan fixes the observed failure with the smallest change; hardening beyond it is separate Bug or Improve ideas, not more tickets.

## The decision record

- **Decided from precedent**: cite packet rules or sibling conventions that settle claims; cite nothing when none does.
- **Decided by the agent**: where precedent is silent, decide with reasoning and a rejected alternative; never present it as precedent.
- **Needs Nate**: only Nate-owned questions on exposure, gates/writers, scope/priority or deduction preferences. Category values are null or non-empty single-question lists.
- **Escalated risk**: judge the plan, not its wording. Give a one-line why for `credentials` (secrets), `authorisation` (permission models), `data-migration` (backfills), `destructive` (deleting), or `concurrency` (races); `[]` when none. Declaring never clears the scan.
- **Premises**: cite each factual claim with `file:line`, command/output or rollout/record; label `measured` (observed), `documented` (unverified vendor claim) or `inferred` (could be wrong). `[]` only when none; runner renders these; omit them from `plan_markdown`.
- **Review focus**: optional `failure_modes` has 0–3 non-empty strings; omission equals `[]`.
- **Hotspot routing**: `hotspot_targets` is absent/`[]` unless Broken; choose exact `{repo_path, function}` pairs from packet hotspots. Targets require non-empty Markdown `redesign_remainder`; omit otherwise.

## What never reaches him

- **Sequencing.** Put named-ticket order, pins or activations in `depends_on` as `owner/repo#n`; whether to build stays Scope.
- **Machine-local paths** use the documented runtime root; stay out of Git in a reversible owner-only subdirectory.
- **Technical defaults are yours.** Choose one canonical source, with thin adapters; record assumptions/rollback as decisions, not preference questions.
- **Check precedent first**; cite exact matches rather than ask.
- **Ask atomically**; keep settled facts out of open questions.

## The answer

Reply with exactly one JSON object and nothing else — no prose, no fences:

{"decided_from_precedent": [{"claim": ..., "source": ...}], "decided_by_agent": [{"decision": ..., "alternative": ..., "why": ...}], "needs_nate": {"exposure": null | ["question"], "gates": null | ["question"], "scope": null | ["question"], "preference": null | ["question"]}, "proposed_class": ..., "plan_markdown": ..., "escalated_risk": [{"reason": ..., "why": ...}], "depends_on": ["owner/repo#n"], "premises": [{"claim": ..., "evidence": ..., "label": "measured" | "documented" | "inferred"}], "failure_modes": [...], "hotspot_targets": [{"repo_path": ..., "function": ...}], "redesign_remainder": ...}

- Each claim, source, decision, alternative, reason, why, evidence, label and question is one non-empty line.
- `proposed_class` names one ladder class: Investigate, Broken, Maintenance, Improve, New, Replace, or Bug. Propose, never gate.
- `plan_markdown` gives ticket-ready scope, rejections/reasons, open questions, and checked siblings and their meaning.
- `depends_on` is `[]` when the plan waits on nothing.

## The packet

```json
PACKET_JSON
```
