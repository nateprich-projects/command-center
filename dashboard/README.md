# Funnel dashboard

The Worker is a read-only display of the latest funnel snapshot. Every route first
validates Cloudflare Access's `Cf-Access-Jwt-Assertion`; static assets do not bypass
the Worker. `GET /api/snapshot` reads the `snapshot` KV key. `POST /api/refresh`
writes an ISO timestamp to `refresh-requested`, which the publisher in #652 polls
and clears.

`wrangler.toml` deliberately disables `workers.dev` and names only
`funnel.nateprich.com`. Ticket #653 creates the production KV namespace, replaces
the local-only namespace id in the config, and performs the first deploy.

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
