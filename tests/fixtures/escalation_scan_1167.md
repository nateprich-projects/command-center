## What it is

`escalation_reasons` (`funnel.py:1140`) scans the whole of an item's title and body for risk vocabulary. That body includes the evidence a capture pastes in — log excerpts, error strings, job names, and the alternatives a plan explicitly rejects. So a report of a problem is scored on the words the problem used, not on what the work will do.

It is not a cosmetic mislabel. The tier decides which lane may take the item, and while command-center#1135 is open an escalated `needs-shaping` idea has no shaper at all — so a false match parks the work where nothing picks it up until a check-in notices.

**Measured, twice, the same hour.** FF-Weekly-Start-Sit#225 was captured with one line copied from an operator log and scored `['data-migration']`; the plan written for the same work, without the pasted line, scores `[]`. This issue — which is nothing but a report about the scan — scored `['authorisation', 'concurrency', 'data-migration', 'destructive']`, four matches on the four words in its own list of past false positives. Neither item has any of those properties.

**The family, from the last four days**, each verified false before it was overridden by hand: a match on `Resource deadlock avoided` quoted from an OS error; on the name of a scheduled job; on a sentence that negates the verb that matched ("authorises … no lineup change"); on an alternative a plan says it will *not* take; and on the two above. Every one cost an `approve --yes` plus a comment naming the matched line — an unattended judgement about whether a risk gate was real.

## The change

1. **Stop scanning text that is quoted rather than asserted.** Exclude fenced code blocks and block quotes from the region `escalation_reasons` searches. A line inside a fence is something that happened; it is not a statement about what this work will do. Four of the six recorded false positives were inside quoted evidence and would clear on this alone, with no weakening of the gate for any prose that actually describes the work.

2. **Report the matched line, not only the reason name.** `escalation_reasons` returns names like `data-migration`; deciding whether the match is real means re-deriving where it came from. Return the matching span alongside each reason and surface it wherever the reason is shown, so a reviewer — or a check-in that has to override one — sees the sentence rather than the keyword.

3. **Pin the behaviour with the two items that produced it.** This issue's own body and FF#225's capture are the fixture: assert the pre-change scores exactly as recorded above, and `[]` after.

**What this does not do.** It does not remove or soften a single pattern, and it does not touch the explicit `Risk:` marker, which already beats the regex in both directions (`funnel.py:1150-1156`) — an author's stated judgement outranking a pattern is the precedent this change follows, not an exception to it.

## Watch for

- **False negatives are the expensive direction.** The scan exists so that work with a real risk property does not reach the cheap lane. Any narrowing has to be to a region that cannot contain an assertion about the work, which is why the proposal is fences and block quotes rather than a keyword list.
- A plan that genuinely needs the escalated tier and describes its risk *inside* a code fence would become standard. Judge whether that shape occurs in practice before widening the exclusion beyond fences and quotes.
- `required_tier` and `plan_is_escalated` both read this function, so the change lands on the shaping gate, the tier routing, and the self-approval check at once. All three want the same correction, but the blast radius is all three.

## Overlap check

Checked: FF#86, FF#90, FF#91, FF#92, FF#93, FF#215, FF#225, The-League#163, The-League#165, The-League#174, The-League#244, career-toolset#186, command-center#25, #162, #794, #1076, #1077, #1125, #1126, #1135, #1144, #1148 and #1149 (the other open plans)

Candidates:
- nateprich-projects/command-center#1167 and nateprich-projects/command-center#1125 both touch `funnel.py`
- nateprich-projects/command-center#1167 and nateprich-projects/command-center#1126 both touch `funnel.py`
- nateprich-projects/command-center#1167 and nateprich-projects/command-center#1135 both touch `funnel.py`
- nateprich-projects/command-center#1167 and nateprich-projects/command-center#1148 both touch `funnel.py`
- nateprich-projects/command-center#1167 and nateprich-projects/command-center#25 both touch `funnel.py`
- nateprich-projects/command-center#1167 and nateprich-projects/command-center#794 both touch `funnel.py`

Conclusion:
- Every candidate is `funnel.py`, which almost every command-center plan touches; the file is not the unit of collision here. None of them reads or writes `escalation_reasons`, `ESCALATION_PATTERNS` or `required_tier`.
- #1135 and #1148 are the closest in subject and still do not overlap: #1135 gives escalated ideas a shaper, #1148 wakes dated parks, and this one stops the tier being wrong in the first place. #1135 is the reason a false match strands rather than merely mislabels, and #1158 (at Ideas) is the reason a stranded item is invisible. All three are worth having separately.
- #794 is the engine replacement and owns the routines, not the scan. Nothing here touches frozen ground: the change is in `funnel.py`'s matcher, not in `routines/`, `skills/` or a parser slated for deletion.

## Proposed class: Broken

## Needs you

- Exposure: nothing outstanding. No secret is read or written; the change is to which region of an issue body a regex searches.
- Gates: **open — this is the question.** The escalation scan is a safety gate, and `shaped_self_approvable` reads it. Narrowing what it scans means some items that park on you today would self-approve instead. The change proposed here is deliberately the smallest version of that — quoted evidence only, no pattern removed — but it is still a change to how much reaches you, and that is yours to say rather than mine. If you would rather the gate stay wide and the false matches keep being overridden by hand with a comment each time, say so and this closes: the cost is about one override a day and it is already being paid.
- Scope and priority: nothing outstanding.
- Preference: nothing outstanding.


<!-- command-center-provenance -->

```json
{
  "agent": "claude",
  "at": "2026-09-20T20:43:04.581140+00:00",
  "run": "bfd8abed09c7",
  "voice": "agent"
}
```

<!-- command-center-origin -->

```json
{
  "agent": null,
  "at": "2026-09-20T20:39:11.903346+00:00",
  "run": null,
  "voice": "agent"
}
```

