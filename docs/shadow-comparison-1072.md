# Live review comparison coverage for #1072

This note records the GitHub-derived before/after replay for the live review
finish fix. It does not change #806 or #813.

The source was the `muse.jsonl` file on the `heartbeat` branch, using the same
window and `engine.shadow_report` pairing/counting functions as #1071:

`2026-09-17T08:55:06Z` through `2026-09-17T20:50:00Z`.

| measurement | before | after replay |
| --- | ---: | ---: |
| shadow jobs | 41 | 41 |
| live jobs | 46 | 46 |
| matched pairs | 35 | 35 |
| compared pairs | 5 | 33 |
| compared rate | 14.3% | 94.3% |

Before the fix, the 30 matched-but-uncompared pairs were:

| exclusion reason | pairs |
| --- | ---: |
| live side missing a structured verdict; shadow side present | 28 |
| both sides missing a structured verdict | 1 |
| shadow side missing a structured verdict; live side present | 1 |

The live `muse-review` schedule reads `routines/muse.md` through
`scripts/muse-review`. The routine now requires every completed live review to
finish with `--review-result approved` or `--review-result rejected`, matching
the verdict written by `funnel review`. This is the durable field consumed by
the shadow report; non-review jobs and pre-verdict skips still omit it.

The after column is a coverage-only replay of the same GitHub rows with a valid
live `review_result` attached to each matched live finish. It measures whether
the pairs enter the denominator, not whether the two judgements agree. The two
remaining exclusions are the one pair missing a shadow verdict on both sides
and the one shadow-only missing verdict; those are not live-path omissions.
