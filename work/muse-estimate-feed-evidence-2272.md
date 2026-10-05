# #2272 Muse estimate feed premise evidence

Baseline inspected before implementation: `origin/main` at
`6404f19c6db12dca486c1ff399a8dd98b42917a6`.

## Existing calculation and measurement source

- `muse_measurements.py` already defines a versioned owner-reported measurement
  (`SCHEMA_VERSION = 1`, `PANEL_USED_PERCENT`) and validates supplied reports
  against `usage.read_muse` at report time. It preserves owner-reported source,
  provenance, approximate observation time, and the 60–120 second uncertainty;
  it does not claim an exact provider sample timestamp.
- `funnel.py::_dashboard_muse_usage` currently maps `usage.read_muse` into the
  displayed seven-day estimate (`spent_dollars`, `cap_dollars`, `used_percent`,
  `calls`) and keeps the meter capture time in `captured_at`. It does not yet
  consume the validated panel reading.
- `funnel.py::write_dashboard_snapshot` stamps the snapshot separately with
  `generated_at` and passes the usage object through unchanged. Thus publication
  time and the estimate's internal observation time can remain distinguishable.

## Existing display behavior and reproduction

- `dashboard/public/app.js::museEstimate` treats `captured_at` as a sample time
  and applies the 90-minute freshness cutoff. `renderUsage` renders `Stale` plus
  age text for an older capture, renders `Unavailable` when no estimate is
  supplied, and includes a sampled-age element for a fresh capture.
- The existing panel row and its #2123 refresh link are separate from the
  estimate row; #2272 changes the estimate row only.
- Added the reproduction test in `dashboard/test/page.test.js` before touching
  implementation. On baseline `6404f19c6`,
  `node --test --test-name-pattern='the Muse estimate bar keeps presenting an
  old capture as an estimate' dashboard/test/page.test.js` failed at the
  expected assertion: `.usage-fill` was absent for the 91-minute-old estimate.

## Source and scope evidence

- The current #2123 plan says the 36% reading is an optional calibration input,
  not an already-ingested or independently verified sample, and report time is
  acceptable as approximate observation time with 1–2 minutes of uncertainty:
  [source report](https://github.com/nateprich-projects/command-center/issues/2123#issuecomment-5974149725),
  [timing decision](https://github.com/nateprich-projects/command-center/issues/2123#issuecomment-5975698153).
- Nate approved the usage bar only; optional brief and doctor/diagnostic output
  are excluded: [scope decision](https://github.com/nateprich-projects/command-center/issues/2123#issuecomment-5978082443).
- The parent plan preserves the #2018 display as dashboard-only, keeps the
  #2125 panel link separate, and requires estimate adjustments from same-window
  own-card meter deltas without changing pricing, pace, lane admission, or
  spending behavior.
