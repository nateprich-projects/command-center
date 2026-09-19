# First-week funnel-watch cost review (#711, for #685)

Review of what the standing funnel watch (Opus 5, high effort, log on #684)
cost over its first week, against the setup estimate kept on #685. This is the
review artifact; the decision it informs lives on the parent. No
recommendation is made.

## Window and method

- Window: watch start 2026-09-12 through the heartbeat snapshot at 2026-09-18
  16:34 PDT (first 6.7 days). The nominal week completes 2026-09-19; the two
  evening slots on 2026-09-18 postdate the snapshot and are unobserved.
- Source: `claude.jsonl` on the `heartbeat` branch (the `heartbeat.py read
  --agent claude` record). Per-run cost is the finish-minus-start
  `seven_day.used_percent` delta, the #431 method. The seven-day window is
  anchored to the weekly reset (no mid-window reset: every paired run opens
  and closes in the cycle ending 2026-09-19 12:00 PDT), while the trailing
  five-hour window ages tokens out mid-run (one run shows a negative
  five-hour delta) and is not used for cost.
- Conversion: 100 points = 9,110,000 Opus output tokens (`WEEKLY_CAPACITY` in
  `usage.py`), so 1 point = 91,100 output tokens. All 28 measured runs read
  high effort; 27 read Opus 5 at start and one reads a model-detection
  artifact at start with Opus 5 at finish.
- Classification is from what each run's finish note says it did (fix, merge,
  unwedge, resume, takeover, cutover, or install performed = wedge-fixing;
  health/monitoring/reporting/captures with no fix performed = quiet), not
  from cost. The implement workspace has no GitHub access, so the #684
  comments themselves were not read; the heartbeat finish notes carry the
  same per-run summaries the watch posts to #684.
- Input tokens are not measured by the heartbeat (it counts output only), so
  the input-token half of the estimate has no measured counterpart below.

## Runs: 31 total, 28 measured

The watch ran three-hourly while #794 is open, not twelve-hourly as the plan
assumed, so the week holds 31 runs rather than 14. Unmeasured: the 2026-09-12
20:38 PDT first run (no heartbeat record), the 2026-09-13 16:19 run (start
with no finish), and the 2026-09-15 04:19 run (died on a schedule call with
nothing logged).

| start (PDT) | 7-day delta (pts) | est. output tokens | class | what the note says |
| --- | ---: | ---: | --- | --- |
| 09-13 01:48 | 0.545 | 49,635 | wedge | installed muse-implement; repaired 3 captures |
| 09-14 20:06 | 1.565 | 142,569 | wedge | 4 machine-local tickets done |
| 09-15 15:30 | 0.905 | 82,420 | wedge | unwedged the standard lane |
| 09-15 16:19 | 2.502 | 227,968 | wedge | filed+merged #900; activated FF agents |
| 09-15 19:19 | 2.622 | 238,904 | wedge | fixed #908; unwedged jeffy git-sync |
| 09-15 22:19 | 2.424 | 220,840 | quiet | shadow report + captures; no fixes |
| 09-16 01:19 | 1.127 | 102,713 | wedge | fixed #941, #942 |
| 09-16 04:19 | 3.559 | 324,266 | wedge | fixed #949, #951, #953 |
| 09-16 07:19 | 0.403 | 36,725 | quiet | healthy, no wedges |
| 09-16 10:19 | 0.389 | 35,443 | quiet | healthy and idle, nothing fixed or filed |
| 09-16 13:19 | 0.292 | 26,642 | quiet | healthy; conflict self-cleared by Codex |
| 09-16 16:19 | 2.228 | 202,947 | wedge | begin regression fixed; Codex resumed |
| 09-16 19:19 | 1.093 | 99,573 | wedge | fixed+installed #976, #979 (lane: no wedges) |
| 09-16 22:19 | 1.530 | 139,393 | wedge | FF runtime moved internal; filed #1006 |
| 09-17 01:19 | 2.338 | 212,998 | wedge | took #1005; fixed #1024, #1026 |
| 09-17 04:19 | 1.404 | 127,909 | wedge | League snapshot cut over; conflict cleared |
| 09-17 07:19 | 1.600 | 145,800 | wedge | fixed Codex 504 stall; repaired cutover |
| 09-17 10:19 | 1.389 | 126,527 | wedge | cleared red main; unstalled 4 tickets |
| 09-17 13:19 | 1.925 | 175,360 | wedge | #175 unwedged; conflicts resolved+merged |
| 09-17 16:19 | 1.179 | 107,389 | quiet | triage/labels + filed #1051; no fixes |
| 09-17 19:19 | 0.515 | 46,911 | quiet | projections recorded; no restart |
| 09-17 22:19 | 2.253 | 205,276 | wedge | fixed cross-repo packet; parked shadow job |
| 09-18 01:19 | 0.947 | 86,305 | wedge | cleared dead edges; unwedged jeffy#129 |
| 09-18 04:19 | 0.525 | 47,807 | quiet | closed 2 resolved items; no repairs |
| 09-18 07:19 | 0.479 | 43,594 | quiet | lanes healthy; filed 2 captures |
| 09-18 10:19 | 0.497 | 45,234 | wedge | merged main into ticket/187 to clear conflict |
| 09-18 13:19 | 0.641 | 58,402 | wedge | FF runtime deployed to main |
| 09-18 16:19 | 0.630 | 57,369 | wedge | took #1114 (merged); runtimes updated |

## Measured means and weekly total

| measure | quiet (n=8) | wedge-fixing (n=20) | weekly total (28 measured) |
| --- | ---: | ---: | ---: |
| mean output tokens | 70,669 | 142,578 | 3,416,919 |
| median output tokens | 45,253 | 133,651 | — |
| weekly-window points | — | — | 37.51 |
| at the estimate's own rates | — | — | ~$174 equivalent |

The quiet mean sits above the estimate's 30–50k because two long monitoring
runs (220.8k diagnostic, 107.4k triage) found no wedges; the quiet median
(45.3k) lands inside the estimated band. The wedge mean (142.6k) lands
inside the estimated 3–5x band. Wedge share is 20 of 28 measured runs (71%).

## Against the setup estimate

Setup estimate: quiet 150–250k input (mostly cached) + 30–50k output per
run; wedge-fixing 3–5x that; 3–4 of 14 runs wedge-fixing; ~$50/week at Opus 5
API rates, a few percent of a Max week.

- Per-run output is roughly as estimated in each class (quiet median 45.3k
  vs 30–50k; wedge mean 142.6k vs 90–250k).
- Run count is ~2x the estimate (28 measured of 31 vs 14): the watch has run
  three-hourly while #794 is open.
- Wedge share is ~2.9x the estimate (71% vs 21–29%).
- Weekly total is ~3.5x the estimate midpoint (3.42M vs 0.98M output
  tokens): 37.51 weekly-window points (~38% of a Max week, not a few
  percent), ~$174 at the estimate's own rates with the estimated input mix
  assumed. Input tokens were not measured, so the input half is
  uncompared. No repricing from vendor pages was built, per the plan.

## The three options, with measured cost beside each

- Keep Opus/high: the observed week cost 3,416,919 output tokens over 28
  measured runs (37.51 weekly-window points; ~$174 at the estimate's own
  rates), plus 3 unmeasured runs. Continuing the same cadence and mix costs
  that weekly.
- Drop to medium: no medium-effort watch runs exist in the data, so there is
  no measured medium cost to state.
- Change the cadence: the measured mean run costs 122,033 output tokens
  (1.34 weekly-window points). At twelve-hourly (14 runs/week) with the
  observed mix held, that arithmetic gives ~1.71M output tokens (~18.8
  points) per week — a projection from measured means, not a measurement.

No recommendation. The choice is Nate's.

## Limits of this review

- #684 was not read directly (no GitHub access from this workspace);
  classification rests on the heartbeat finish notes.
- Input and cached-input tokens are unmeasured; only output is compared.
- Three runs are unmeasured, and the last ~28 hours of the nominal week
  (after the 2026-09-18 16:34 PDT snapshot) are unobserved.
- Deltas attribute all Opus output on this Mac during each run to the run;
  concurrent interactive sessions would inflate them.
