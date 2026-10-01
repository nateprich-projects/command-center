# Funnel dashboard

The Worker is a read-only display of funnel data. Every API route first validates
Cloudflare Access's `Cf-Access-Jwt-Assertion`; static assets do not bypass the Worker.
`GET /api/snapshot` reads the `snapshot` KV key, and `GET /api/metrics` reads the
separate `metrics` key. `POST /api/refresh` writes an ISO timestamp to
`refresh-requested`, which the publisher in #652 polls. A publishable brief
clears the key.
After an exit-0 refresh brief that cannot publish a snapshot, the publisher
preserves that request timestamp and records the attempt time in the same KV
value. This delays only the next refresh-driven brief; the standing cadence
regeneration can still run.

`wrangler.toml` deliberately disables `workers.dev` and names only
`funnel.nateprich.com`. The #653 launchd job creates the production KV namespace
on its first tick and patches the real id into its own deploy worktree copy of
`wrangler.toml`; the local-only placeholder on `main` stays untouched and the
job never commits. After that it redeploys only when `main` changes under
`dashboard/`. The declared route attaches as part of each deploy, and DNS needs
no work.

When a live PR scan is incomplete, the board can show prior PR facts for up to
24 hours with a visible stale age. This is display-only; review and merge keep
using live facts. The reader uses each spool entry's top-level `generated_at`
and `board.columns[].items[].tickets[]` fields `ref`, `pr`, and `pr_number`.
Carry-forward is enabled by default. To restore the board's existing live/unknown
display immediately, create an empty
`disable-pr-carry-forward` file in the dashboard spool directory. The default
path is `~/.claude/command-center-dashboard-spool/disable-pr-carry-forward`;
when `COMMAND_CENTER_DASHBOARD_SPOOL` is set, place the file in that directory.
Remove the file to re-enable carry-forward.

## Source verification (2026-10-01)

The issue's `funnel.py:11939` anchor has moved and no longer names board code on
current `main`. The existing fallback flows from `cmd_brief` through
`dashboard_board` to `_dashboard_ticket` (`pr: "unknown"` for an open ticket)
and `_dashboard_pip_state` (`unknown`).
`tests/test_dashboard_board.py::test_a_failed_pr_scan_reads_unknown_rather_than_no_pr`
covers that behavior.

This run found parseable published briefs in
`~/.claude/command-center-dashboard-spool/`. Their capture timestamp is the
top-level `generated_at` field; ticket PR facts are under
`board.columns[].items[].tickets[]`. The decision paths remain live: `review_queue`
loads `ticket_pr_facts`, while `cmd_merge` reads a current PR row through
`_pr_fact_for_number`.

Run the JS checks with:

```bash
npm test
```

Run the page locally against the committed fixture with:

```bash
npm run serve:fixture
```

Then open `http://127.0.0.1:8787`. The fixture server signs a local-only Access JWT
and passes requests through the production Worker handler, including the auth check;
it never uses a Cloudflare credential.
