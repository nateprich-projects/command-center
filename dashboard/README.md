# Funnel dashboard

The Worker is a read-only display of the latest funnel snapshot. Every route first
validates Cloudflare Access's `Cf-Access-Jwt-Assertion`; static assets do not bypass
the Worker. `GET /api/snapshot` reads the `snapshot` KV key. `POST /api/refresh`
writes an ISO timestamp to `refresh-requested`, which the publisher in #652 polls
and clears.

`wrangler.toml` deliberately disables `workers.dev` and names only
`funnel.nateprich.com`. The #653 launchd job creates the production KV namespace
on its first tick and patches the real id into its own deploy worktree copy of
`wrangler.toml`; the local-only placeholder on `main` stays untouched and the
job never commits. After that it redeploys only when `main` changes under
`dashboard/`. The declared route attaches as part of each deploy, and DNS needs
no work.

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
