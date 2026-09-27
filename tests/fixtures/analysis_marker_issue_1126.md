# Make the analysis-waits-for-Nate rule mechanical

## What this is
Plan.md rule 5 (open PR #1124) says an analysis project waits at Accept it whatever its Class. Today _can_close_itself() in funnel.py reads Class and origin alone, so an analysis classed Maintenance still closes itself exactly as #685 did. This plan mechanizes the rule: a machine-readable marker set at shaping, a predicate that reads it and fails closed, and a brief and dashboard surface showing why an analysis project is waiting.

## The work
1. Marker in the command-center marker family: a new HTML-comment marker (for example <!-- command-center-analysis -->) carrying a fenced JSON block {"analysis": true}, written by the shaper into the plan body at shaping and read with _marked_json, the same helper behind ORIGIN_OVERRIDE_MARKER. Update skills/shape/SKILL.md to write it for analysis plans, as the follow-up #1124 prose anticipates.
2. Predicate: _can_close_itself() checks the marker first. Present and valid means return False before the class and origin rules. A marker block that is present but malformed means wait, failing closed toward Nate per the default-on rule. Absent means the existing rules 1 to 4 apply unchanged.
3. Opt-out: when Nate expressly exempts an analysis, the shaper writes no marker and quotes him in the plan Needs you scope line as the audit trail.
4. Surface: the brief rendering of Building items awaiting acceptance carries the waiting reason (analysis review versus ordinary accept), following the closed_itself_json sibling pattern, with the same reason on the dashboard.
5. Tests: Maintenance with marker waits; Maintenance without marker still closes itself; malformed marker waits; agent-origin Improve without marker still closes itself.

## Decided from precedent
- Marker shape and reader: _marked_json and _marked_json_blocks with HTML-comment markers, per ORIGIN_OVERRIDE_MARKER and parse_origin_override in funnel.py.
- Fail-closed direction: missing or malformed readings resolve toward Nate, per parse_origin and the _can_close_itself docstring.
- Default-on and the no-behaviour-change test for what counts as analysis: plan.md rule 5 and AGENTS.md Analysis waits for Nate, confirmed by Nate 2026-09-19 in open PR #1124.
- This plan is not itself analysis: its tickets change funnel.py behaviour, so the waits-for-Nate rule does not self-apply.
- No Class is set here: the capture origin is nate-relayed, so the class is proposed only.

## Decided by the agent
- New marker rather than reusing command-center-origin-override with target nate. Reason: the override records shape ownership, while analysis-ness is orthogonal to who shaped the item, since an agent-shaped cost review still waits. Rejected the reuse because it overloads ownership semantics with an unrelated property.
- Malformed marker means wait. Reason: the safe direction is the one where Nate sees the findings; ignoring a malformed marker would silently close an analysis unread and repeat #685. Rejected ignore-on-malformed for that reason.
- The findings-posted precondition for reaching Accept stays ticket-level via the breakdown skill Accept line from #1124. Reason: a mechanical comment scan needs a brittle definition of what counts as findings, while the decision half of #685 is fully covered by the marker plus predicate. Rejected a comment-scanning gate for this change.
- No keyword scan of plan prose for words like review or recommendation. Reason: that is inference, and a standing instruction is not a substitute for detection per AGENTS.md and the #97 family. The marker is a declaration, not a guess.

## Rejected
- Scoping the mechanical rule to the Investigate class, which needs no new marker. It would have missed #685, a Maintenance, which is the exact case that prompted the rule. Inherited from rule 5.
- Keeping self-close and merely requiring findings on the parent. That fixes visibility and leaves the decision broken, since a closed project asks nothing. Inherited from rule 5.
- A digest or batch review surface instead of a per-project waiting reason. Batching restores the review-queue shape the design record rejects. Inherited from rule 5.

## Overlap check
Checked: #25, #794, #1077, #1125 (open command-center plans), #1043, FF-Weekly-Start-Sit #192 and #86 (open ideas), FF-Weekly-Start-Sit #90, #91, #92 and #93, The-League #165, jeffy-finance-agent #62 (advisory candidates), and open PR #1124.

Candidates:
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#90 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#90 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#91 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#91 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#92 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#92 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#93 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/FF-Weekly-Start-Sit#93 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/The-League#165 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#1077 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#1125 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#1125 both touch funnel.py
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#1125 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#1125 both reference #685
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#25 both touch funnel.py
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#25 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#794 both touch AGENTS.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#794 both touch funnel.py
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#794 both touch plan.md
- nateprich-projects/command-center#1126 and nateprich-projects/command-center#794 both reference #685
- nateprich-projects/command-center#1126 and nateprich-projects/jeffy-finance-agent#62 both touch AGENTS.md

Conclusion:
- FF-Weekly-Start-Sit #90 through #93, The-League #165 and jeffy-finance-agent #62 share citations of AGENTS.md and plan.md only, in other repos on unrelated axes. No narrowing.
- #25 (funnel from general chat, blocked) and #794 (deterministic runner engine) touch funnel.py on different axes from the close predicate. Keep all as written.
- #1077 (pay-per-use API experiment) is an AGENTS.md citation only.
- #1125 (second funnel-watch cost review) is an analysis project and the first consumer of this marker, complementary rather than duplicative. PR #1124 is the prose half of the same rule and this plan is the mechanical half; #1124 stays the cited source until merged.

Proposed class: Improve

<!-- command-center-provenance -->

```json
{
  "agent": null,
  "at": "2026-09-19T13:31:55.413860+00:00",
  "run": null,
  "voice": "agent"
}
```

