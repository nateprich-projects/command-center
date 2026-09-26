# Command Center MCP connector

This directory contains the authenticated HTTP MCP connector for Command Center. Its
read-only `brief`, `ideas`, `show`, and `queue` tools call the matching `funnel.py`
commands and return their output. Its gate tools `approve`, `start`, `accept`, and
`park` call the matching `funnel.py` gate commands and record Nate's verbatim
instruction in a `nate-relayed` provenance block. The connector does not reimplement
project queries or ordering, and it does not expose `next`, `claim`, `release`,
`gate`, heartbeat operations, or merge.

## Local development configuration

Copy `.env.example` to `.env` and replace the placeholder with a locally generated 64-character token:

```sh
cp .env.example .env
openssl rand -hex 32
```

Put the generated value after `INBOUND_STATIC_TOKEN=` in `.env`. Keep `.env` private; it is gitignored and excluded from Docker build context. This in-repository file is for local development only.

## Read tools

The connector exposes four read-only tools. Each calls the matching `funnel.py`
command and returns its stdout; errors include the command's diagnostic output.
The connector inherits GitHub CLI authentication from its environment, so
`funnel.py` remains the source for Project reads and ordering.
For deployment, `GH_TOKEN` comes from
`~/.config/command-center/connector.env` as described below.

- `brief` calls `funnel.py brief`.
- `ideas` calls `funnel.py ideas`.
- `show` accepts an item reference and calls `funnel.py show <ref>`.
- `queue` calls `funnel.py queue`.

## Gate tools

Each gate tool requires `instruction`: the words Nate used, verbatim. The
connector cannot verify that Nate said them; the record makes the claim
auditable afterwards. Each successful gate write posts a comment on the issue
whose provenance block has voice `nate-relayed` and carries the instruction
unchanged.

- `approve` calls `funnel.py approve <ref> --yes --instruction <text>`
  (Shaped to Ready).
- `start` calls `funnel.py start <ref> --yes --instruction <text>` (Ready to
  Building). `start` is a connector-only command, hidden from `funnel.py --help`;
  Ready projects still start on their own when a first ticket is claimed.
- `accept` calls `funnel.py accept <ref> --yes --instruction <text>`, adding
  `--no-tickets` when `no_tickets` is true (Building to Done).
- `park` calls `funnel.py park <ref> --reason <reason> --instruction <text>`.

## Deploy with Colima

The Mac mini keeps the GitHub credential from #707 in
`~/.config/command-center/connector.env`; that file contains only `GH_TOKEN` and
is outside the repository. Caller authentication is a separate credential: keep
`INBOUND_STATIC_TOKEN` in
`~/.config/command-center/connector-auth.env`, also outside the repository. Do
not reuse `GH_TOKEN` as the caller token. Both files should be readable only by
the account running Colima (`0600` files in a `0700` directory).

If there is already a caller token in the local development `.env`, copy the
same value into `connector-auth.env` so client credentials stay aligned. If no
caller token exists yet, create the file once; preserve its value for the later
chat connector registration:

```sh
mkdir -p "$HOME/.config/command-center"
chmod 700 "$HOME/.config/command-center"
if [ ! -e "$HOME/.config/command-center/connector-auth.env" ]; then
  (umask 077; printf 'INBOUND_STATIC_TOKEN=%s\n' "$(openssl rand -hex 32)" \
    > "$HOME/.config/command-center/connector-auth.env")
fi
chmod 600 "$HOME/.config/command-center/connector-auth.env"
```

From the repository root, with Colima's Docker context active, install or
refresh the version-pinned service:

```sh
./funnel-mcp-connector/install-colima-service.sh
```

The script replaces the stateless `command-center-mcp` container, binds the
published port to localhost, and sets Docker's `unless-stopped` restart policy.

The service restarts with Colima and publishes only on `127.0.0.1:3003`, where
the existing Cloudflare Tunnel forwards `funnel-mcp.nateprich.com`. Its public
resource metadata uses `https://funnel-mcp.nateprich.com`; caller requests still
require the separate static bearer token. Credentials are injected at runtime
from the two external env files and are not part of the Docker build context.

After starting, verify the forwarded host port, tunneled metadata and auth
boundary. The smoke test checks that requests without a token and with an invalid
token receive 401/403, then initializes an authenticated MCP session and confirms
the four read tools and the four gate tools are registered. Run the restart check after the initial smoke
test to confirm Colima brings the container and host port back:

```sh
colima restart
docker ps --filter name=command-center-mcp
lsof -iTCP:3003 -sTCP:LISTEN
curl -fsS https://funnel-mcp.nateprich.com/.well-known/oauth-protected-resource/mcp
docker exec command-center-mcp python /app/command-center/funnel-mcp-connector/smoke-test.py \
  http://127.0.0.1:3003
```

The smoke test exits non-zero if the token is missing, either unauthenticated
request is accepted, the authenticated MCP exchange fails, the tool list differs
from the four read tools and the four gate tools, or `/healthz` does not respond
as expected.
