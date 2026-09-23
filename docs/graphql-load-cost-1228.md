# Project-load GraphQL cost (#1228)

## Matched measurement

Measured 2026-09-23 UTC by running `funnel.load_items()` once from
`origin/main` and once from the ticket implementation, resetting the in-process
API counters before each read. Both runs read the same 1,181 Project rows over
12 Project-item pages.

| Code | GraphQL calls | GraphQL points | Total `gh` calls |
| --- | ---: | ---: | ---: |
| `origin/main` | 26 | 62 | 33 |
| Ticket implementation | 26 | 53 | 33 |

The ticket implementation used 9 fewer GraphQL points per full load (14.5%).
Request count did not fall: each run used 2 repository-topic queries, 12
Project-item pages, and 12 item-detail batches. The savings come from omitting
`subIssues` selections for Project items whose `subIssuesSummary.total` is zero.

## Heartbeat context

`heartbeat.py read --agent muse` showed 1,991 runs with API-cost events since
2026-09-17; the median was 78 GraphQL points and 32 `gh` calls per run. Those
are whole-run measurements, so they provide operating context rather than a
like-for-like load comparison.

The parent issue's pre-change acceptance baseline was 545
`skipped-api-reserve` outcomes over 2026-09-17 through 2026-09-20 (about 140
per day), with windows that reached zero remaining. This branch is not deployed,
so it has no post-change skip or brownout measurement; the matched load result
does not establish those operational acceptance conditions.
