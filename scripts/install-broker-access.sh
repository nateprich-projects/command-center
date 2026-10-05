#!/usr/bin/env bash
# Provision only the cross-user paths required by the credential broker.
# Run --apply from the #1999 cutover with administrative access; this ticket
# adds the versioned procedure but does not change this Mac's account ACLs.

set -euo pipefail

MODE="${1:-}"
if [[ "$MODE" != "--dry-run" && "$MODE" != "--apply" ]]; then
  echo "usage: $0 --dry-run | --apply" >&2
  exit 2
fi

DRY=false
[[ "$MODE" == "--dry-run" ]] && DRY=true
SHARED_DIR="/Users/Shared/command-center-broker"
CODEX_HOME="/Users/codex"
CODEX_CLAUDE="$CODEX_HOME/.claude"
CODEX_HEARTBEAT="$CODEX_CLAUDE/command-center-heartbeat"
CODEX_RUNS="$CODEX_HEARTBEAT/codex-runs"

say() { printf '  %s\n' "$*"; }

if ! $DRY && [[ "$(id -u)" != "0" ]]; then
  echo "install-broker-access: --apply requires root during the cutover" >&2
  exit 1
fi
if $DRY; then
  say "would create $SHARED_DIR owned by nateprich mode 0700"
  say "would grant codex search on $SHARED_DIR"
  say "would grant nateprich search only on the path to $CODEX_RUNS"
  say "would create $CODEX_RUNS owned by codex mode 0700"
  say "would grant nateprich inherited read, write, and search on $CODEX_RUNS"
  say "would grant codex inherited traversal and deletion on broker-created checkout descendants"
  exit 0
fi
if ! id codex >/dev/null 2>&1 || ! id nateprich >/dev/null 2>&1; then
  echo "install-broker-access: both codex and nateprich accounts are required" >&2
  exit 1
fi
CODEX_UID="$(id -u codex)"
if [[ "$CODEX_UID" != "506" ]]; then
  echo "install-broker-access: codex UID is $CODEX_UID; expected 506" >&2
  exit 1
fi

NATE_GROUP="$(id -gn nateprich)"
CODEX_GROUP="$(id -gn codex)"
/usr/bin/install -d -o nateprich -g "$NATE_GROUP" -m 0700 "$SHARED_DIR"
/bin/chmod 0700 "$SHARED_DIR"

ensure_acl() {
  local path="$1"
  local entry="$2"
  local rendered
  rendered="$(/bin/ls -lde "$path")"
  if [[ "$rendered" == *"$entry"* ]]; then
    return
  fi
  /bin/chmod +a "$entry" "$path"
}

ensure_acl "$SHARED_DIR" "codex allow search"

for path in "$CODEX_HOME" "$CODEX_CLAUDE" "$CODEX_HEARTBEAT"; do
  if [[ ! -d "$path" || -L "$path" ]]; then
    echo "install-broker-access: expected a real Codex directory at $path" >&2
    exit 1
  fi
  ensure_acl "$path" "nateprich allow search"
done

/usr/bin/install -d -o codex -g "$CODEX_GROUP" -m 0700 "$CODEX_RUNS"
/bin/chmod 0700 "$CODEX_RUNS"
ensure_acl "$CODEX_RUNS" "nateprich allow list,search,add_file,add_subdirectory,delete_child,read,write,readattr,writeattr,readextattr,writeextattr,readsecurity,file_inherit,directory_inherit"
# The checkout root is Codex-owned, but finish runs as Nate and can create
# Nate-owned directories below it. Inherit explicit Codex rights into those
# descendants so the model account can traverse and remove its checkout.
ensure_acl "$CODEX_RUNS" "codex allow list,search,add_file,add_subdirectory,delete,delete_child,read,write,readattr,writeattr,readextattr,writeextattr,readsecurity,file_inherit,directory_inherit"
say "installed broker socket and checkout access ACLs"
