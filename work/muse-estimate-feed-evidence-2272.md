# #2272 Muse estimate feed evidence

## Premises and source

- `muse_measurements.py` provides the versioned, owner-reported percentage
  measurement from #2271. It pairs a report-time reading with the own-card
  meter, preserves source and provenance, uses report time with 1–2 minute
  uncertainty, and does not invent a provider sample timestamp. #2271 is closed.
- `funnel.py::_dashboard_muse_usage` supplies the seven-day display estimate;
  `write_dashboard_snapshot` publishes it with a separate `generated_at`.
- `dashboard/public/app.js` keeps the #2018 estimate row separate from its
  account-panel row and #2125 link. The approved scope is the bar only; brief
  and doctor/diagnostic output remain excluded.
- The live #2123 history read on this run found Nate's 36% report at
  `2026-10-03T22:23:07Z` as the only owner-supplied panel reading. Its paired
  meter window ends `2026-10-05T00:00:00Z`; it cannot adjust the active window,
  which ends `2026-10-12T00:00:00Z`. The display therefore stays on the local
  estimate until a reading from the active window is supplied.

## Reproduction

The regression test is `the Muse estimate remains a bar when its meter capture
is old`. Running that test against the current `origin/main` UI source at
`4b257ca1e` failed as expected: the 91-minute-old estimate had no `.usage-fill`.
The same test passes with this branch's render behavior. Existing panel and
Claude freshness behavior is unchanged.

## Implementation and verification

- The display feed selects the latest usable source comment, adjusts only when
  report and current meter belong to the same weekly window, and writes one
  source-linked pairing record. It leaves `usage.pace` on the unadjusted meter.
- Publication retains the internal report observation separately from snapshot
  `generated_at`; refresh is not represented as a new panel sample. The feed
  can be disabled with `COMMAND_CENTER_MUSE_ESTIMATE_FEED_DISABLED`.
- The estimate bar no longer renders Stale, Unavailable, sampled-age, or other
  freshness commentary. No #2273 recovery-buffer or failure-recording behavior
  is included in this ticket branch.
- `python3 -m pytest tests/test_muse_measurements.py tests/test_dashboard_spool.py tests/test_brief.py -q` passed: 126 tests.
- `npm test --prefix dashboard` passed: 87 tests.
- `git diff --check` passed. `finish-ticket` will run the full suite.
