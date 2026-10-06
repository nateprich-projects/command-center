# Ticket #2273 premise evidence

The current implementation separates the optional owner-panel feed from the
meter and gates that control work. This confirms the ticket's premise and
keeps the change contingent on that separation:

- `funnel._dashboard_muse_usage` builds the display row from `usage.read_muse`
  and sends that same local reading to `usage.pace` before reading owner
  comments. The adjusted panel value is used only for the dashboard row.
- `tests/test_dashboard_spool.py::test_dashboard_muse_owner_feed_keeps_pace_on_local_meter`
  verifies that pace receives the local `$72.00` reading even when the display
  estimate adjusts to `$75.50`.
- `tests/test_muse.py::test_muse_reader_prices_provider_calls` and
  `test_reproduction_mixed_model_gate_uses_the_own_card_sum` cover the existing
  provider pricing and gated-spend calculation. `usage.pace` remains the
  canonical pace calculation.
- `engine/replay.py::check_budget` reads the canonical Muse meter and applies
  the existing pace decision before model calls. `tests/test_begin.py::test_a_tight_budget_stops_every_lane_before_reading_the_project` and
  `test_an_ok_band_offers_everything_as_before` cover existing lane admission.

The failure path therefore retains the last published estimate and records
diagnostics only in the dashboard's local runtime buffer/snapshot. It does not
feed panel readings or failures into pricing, pace, replay admission, or the
brief and doctor output.
