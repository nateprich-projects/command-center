# Command Center MCP connector

This directory contains the authenticated HTTP MCP connector for Command Center. Its
read-only `brief`, `ideas`, `show`, and `queue` tools call the matching `funnel.py`
commands and return their output. The connector does not reimplement project queries or
ordering.

## Configuration

Copy `.env.example` to `.env` and replace the placeholder with a locally generated 64-character token:

```sh
cp .env.example .env
openssl rand -hex 32
```

Put the generated value after `INBOUND_STATIC_TOKEN=` in `.env`. Keep `.env` private; it is gitignored and excluded from Docker build context.

The tools also need GitHub CLI authentication to read the live Project. Add the
existing classic token as `GH_TOKEN` in this runtime `.env` file. Keep that value
private as well.

## Read tools

- `brief` calls `funnel.py brief`.
- `ideas` calls `funnel.py ideas`.
- `show` accepts an item reference and calls `funnel.py show <ref>`.
- `queue` calls `funnel.py queue`.

Each tool returns the command's stdout. A non-zero command exit is returned as a tool
error with the command's diagnostic output.

## Build and run with Colima

From the repository root, with Colima's Docker context active:

```sh
docker build -f funnel-mcp-connector/Dockerfile -t command-center-mcp:0.1.0 .
docker run -d --name command-center-mcp --restart unless-stopped \
  --env-file funnel-mcp-connector/.env -p 3003:3003 command-center-mcp:0.1.0
```

Run the wire-protocol smoke test inside the container so it uses the pinned client dependencies and the same token configuration as the server. It first verifies that requests without a token and with an invalid token receive 401/403, then initializes an authenticated MCP session and confirms the four read tools are registered:

```sh
docker exec command-center-mcp python /app/command-center/funnel-mcp-connector/smoke-test.py \
  http://127.0.0.1:3003
```

The smoke test exits non-zero if the token is missing, either unauthenticated request is accepted, the authenticated MCP exchange fails, or `/healthz` does not respond as expected.
