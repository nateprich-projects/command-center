#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd "$SCRIPT_DIR/.." && pwd)
GH_ENV_FILE="$HOME/.config/command-center/connector.env"
CALLER_ENV_FILE="$HOME/.config/command-center/connector-auth.env"

for env_file in "$GH_ENV_FILE" "$CALLER_ENV_FILE"; do
    if [ ! -r "$env_file" ]; then
        printf 'Required credential env file is missing or unreadable: %s\n' "$env_file" >&2
        exit 1
    fi
done

docker build \
    --file "$REPO_ROOT/funnel-mcp-connector/Dockerfile" \
    --tag command-center-mcp:0.1.0 \
    "$REPO_ROOT"

# The connector is stateless; replace the container to apply the pinned image
# and current runtime settings. Its restart policy handles Colima restarts.
if docker container inspect command-center-mcp >/dev/null 2>&1; then
    docker container rm --force command-center-mcp
fi

docker run --detach \
    --name command-center-mcp \
    --restart unless-stopped \
    --env-file "$GH_ENV_FILE" \
    --env-file "$CALLER_ENV_FILE" \
    --env HOST=0.0.0.0 \
    --env PORT=3003 \
    --env PUBLIC_URL=https://funnel-mcp.nateprich.com \
    --publish 127.0.0.1:3003:3003 \
    command-center-mcp:0.1.0
