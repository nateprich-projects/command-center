# Command Center MCP connector

This directory contains the authenticated HTTP MCP connector for Command Center.
It exposes four thin adapters over `funnel.py`: `approve`, `start`, `accept`, and
`park`. Each requires Nate's verbatim `instruction`; the `park` tool also
requires a reason. Successful gate requests record that instruction in a
`nate-relayed` provenance block. The `accept` tool supports the funnel's
explicit `no_tickets` override for work completed outside the ticket pipeline.

The connector does not expose queue selection, ticket claiming or release,
generic gate writes, or heartbeat operations.

## Configuration

Copy `.env.example` to `.env` and replace the placeholder with a locally generated 64-character token:

```sh
cp .env.example .env
openssl rand -hex 32
```

Put the generated value after `INBOUND_STATIC_TOKEN=` in `.env`. Keep `.env` private; it is gitignored and excluded from Docker build context.

## Build and run with Colima

From the repository root, with Colima's Docker context active:

```sh
docker build -f funnel-mcp-connector/Dockerfile -t command-center-mcp:0.1.0 .
docker run -d --name command-center-mcp --restart unless-stopped \
  --env-file funnel-mcp-connector/.env -p 3003:3003 command-center-mcp:0.1.0
```

Run the wire-protocol smoke test inside the container so it uses the pinned client dependencies and the same token configuration as the server. It first verifies that requests without a token and with an invalid token receive 401/403, then initializes an authenticated MCP session and confirms exactly the four intended gate tools are listed:

```sh
docker exec command-center-mcp python /app/command-center/funnel-mcp-connector/smoke-test.py \
  http://127.0.0.1:3003
```

The smoke test exits non-zero if the token is missing, either unauthenticated request is accepted, the authenticated MCP exchange fails, the tool list differs from `approve`, `start`, `accept`, and `park`, or `/healthz` does not respond as expected.
