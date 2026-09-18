# Post-fix comparison accrual for #1073

This note records the post-#1072 accrual measurement for the shadow cutover
plan in #1044. It does not change #806 or #813.

## Coverage replay

The source is the `muse.jsonl` file on the `heartbeat` branch. The replay uses
the #1071 window and the same `engine.shadow_report` pairing/counting logic:

`2026-09-17T08:55:06Z` through `2026-09-17T20:50:00Z`.

The #1072 fix makes the eligible live verdicts durable. Replaying that window
with those verdicts attached gives:

| measurement | after #1072 replay |
| --- | ---: |
| shadow jobs | 41 |
| live jobs | 46 |
| matched pairs | 35 |
| compared pairs | 33 |
| compared rate | 94.3% |

The 33 compared-pair finish times span 11.5914 hours (`08:58:19Z` through
`20:33:48Z`). Using the report's first-to-last completion rate:

```text
33 / 11.5914 hours = 2.8469 compared jobs/hour
50 / 2.8469 = 17.56 hours to reach 50 compared jobs
```

The plan baseline was 0.42 compared jobs/hour, or roughly 120 hours to reach
50. The post-fix replay is therefore about 6.8 times faster and reaches the
sample in about 17.6 hours, within the existing 48-hour observation window.

This is a coverage/accrual replay, not an agreement result: it does not claim
that the 33 shadow and live judgements agree. The two excluded pairs remain
shadow-side missing-verdict cases, not live-path omissions.

## Current live observation

At the heartbeat refresh ending `2026-09-18T14:36:35Z`, the interval after the
#1072 merge (`2026-09-18T13:48:57Z`, commit `8681820`) had no completed shadow
review and therefore no matched or compared pair. That queue-empty interval
cannot establish a new wall-clock rate; it is not substituted for the replay
rate above.

## Outcome

The sample now projects to 50 compared jobs in hours rather than roughly 120
hours. No minimum-sample bound restatement is needed. Keep the 2026-09-19 hold
and make no edit to #806 or #813 from this ticket.
