#!/usr/bin/env bash
# install.sh — wire Command Center into ~/.claude.
#
# Idempotent: safe to re-run after every pull. The user-global entry points are
# symlinked back to the checkout, so editing the repo takes effect immediately
# and there is never a second copy to drift. The canonical checkout itself may
# already be ~/.claude/command-center, in which case that directory is the
# source rather than another link.
#
# Run with --dry-run to see what it would do.

set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CLAUDE="$HOME/.claude"
DRY=false
[ "${1:-}" = "--dry-run" ] && DRY=true

say() { printf '  %s\n' "$*"; }

link() {
  local src=$1 dst=$2
  if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
    say "ok: $dst"
    return
  fi
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    say "REFUSING: $dst exists and is not a symlink. Move it aside first."
    return 1
  fi
  if $DRY; then
    say "would link: $dst -> $src"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  ln -sfn "$src" "$dst"
  say "linked: $dst -> $src"
}

echo "Command Center → $CLAUDE"
$DRY && echo "  (dry run — nothing will be changed)"

# The checkout may live on an external volume. Everything below is symlinked to
# it, so if that volume is not mounted the status line renders nothing and the
# usage cache stops updating. That degrades safely — a routine that cannot read
# its budget fails closed — but it is silent, so say it once here.
case "$REPO" in
  /Volumes/*) echo "  note: this checkout is on $(echo "$REPO" | cut -d/ -f1-3)."
              echo "        If that volume is unmounted, the status line and /funnel stop working." ;;
esac

# The whole checkout, not individual scripts: the routines call funnel.py,
# usage.py, heartbeat.py and prior_run.py, and linking them one by one means a
# new script silently does not exist until someone remembers to add it here.
# After the checkout moved onto the internal disk, it may already be the
# canonical directory itself. That is healthy and must not trip the old
# non-empty-directory guard; only a different real directory is refused.
if [ "$REPO" = "$CLAUDE/command-center" ]; then
  say "ok: $CLAUDE/command-center is this checkout"
elif [ -d "$CLAUDE/command-center" ] && [ ! -L "$CLAUDE/command-center" ]; then
  if [ -z "$(find "$CLAUDE/command-center" -mindepth 1 ! -type l 2>/dev/null)" ]; then
    $DRY || rm -rf "$CLAUDE/command-center"
    say "replaced the old per-script directory with a single link"
  else
    say "REFUSING: $CLAUDE/command-center holds real files. Move it aside first."
    exit 1
  fi
fi
if [ "$REPO" != "$CLAUDE/command-center" ]; then
  link "$REPO"               "$CLAUDE/command-center"
fi
link "$REPO/statusline.sh" "$CLAUDE/statusline.sh"
# Every skill in the checkout, not a hardcoded list: naming them one by one is
# how a new skill silently does not exist until someone remembers this file.
for skill in "$REPO"/skills/*/; do
  [ -d "$skill" ] || continue
  link "${skill%/}" "$CLAUDE/skills/$(basename "$skill")"
done

# The broker is a private, versioned copy. The model user cannot traverse
# ~/.claude, so it cannot replace the code that receives its socket requests.
# The installed manifest detects local drift before the server binds a socket.
BROKER_SOURCE="$REPO/credential_broker.py"
BROKER_DIR="$CLAUDE/command-center-broker"
if $DRY; then
  say "would install and drift-check the credential broker: $BROKER_DIR"
else
  python3 - "$BROKER_SOURCE" "$BROKER_DIR" <<'PY'
import ast
import hashlib
import json
import os
import pathlib
import tempfile
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
if source.is_symlink() or not source.is_file():
    raise SystemExit("REFUSING: broker source is not a regular versioned file")
text = source.read_text(encoding="utf-8")
tree = ast.parse(text, filename=str(source))
version = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(item, ast.Name) and item.id == "BROKER_VERSION"
        for item in node.targets
    ):
        version = ast.literal_eval(node.value)
        break
if not isinstance(version, int) or version < 1:
    raise SystemExit("REFUSING: broker has no positive version")
if target.is_symlink() or (target.exists() and not target.is_dir()):
    raise SystemExit("REFUSING: broker install path is not a real directory")
target.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(target, 0o700)
destination = target / "credential_broker.py"
manifest = target / "manifest.json"
if destination.is_symlink() or manifest.is_symlink():
    raise SystemExit("REFUSING: broker install files cannot be symlinks")
digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
payload = json.dumps(
    {"version": version, "sha256": digest}, sort_keys=True, indent=2
) + "\n"

def replace_file(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix=".broker-install-", dir=str(target))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

replace_file(destination, text, 0o700)
replace_file(manifest, payload, 0o600)
hooks = target / "hooks-empty"
if hooks.is_symlink():
    raise SystemExit("REFUSING: empty hooks path cannot be a symlink")
hooks.mkdir(mode=0o700, exist_ok=True)
if not hooks.is_dir() or any(hooks.iterdir()):
    raise SystemExit("REFUSING: empty hooks path is not empty")
os.chmod(hooks, 0o700)
installed_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
installed_manifest = json.loads(manifest.read_text(encoding="utf-8"))
if (
    installed_hash != digest
    or installed_manifest.get("version") != version
    or installed_manifest.get("sha256") != installed_hash
):
    raise SystemExit("REFUSING: installed broker failed its drift check")
print("  installed and drift-checked credential broker v{}".format(version))
PY
fi

# launchd rejects symlinked plists, so keep copies of the schedules in the user
# LaunchAgents directory. Re-running the installer refreshes the keeper and all
# Muse schedule copies together.
#
# Copying is safe from an automation shell, but `launchctl bootstrap` is a
# console action: on the schedule Mac the managed automation shell can inspect
# the Aqua domain and boot out a job, yet launchd rejects its bootstrap request
# with error 5. The installer therefore prints the reload handoff instead of
# claiming that the copied files are loaded.
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LAUNCHD_PLISTS=(
  com.nateprich.command-center-muse-review.plist
  com.nateprich.command-center-muse-review-standard.plist
  com.nateprich.command-center-run-keeper.plist
  com.nateprich.command-center-funnel-publisher.plist
  com.nateprich.command-center-funnel-deploy.plist
  com.nateprich.command-center-ff-deploy.plist
  com.nateprich.command-center-league-deploy.plist
  com.nateprich.command-center-career-deploy.plist
  com.nateprich.command-center-outcomes-derive.plist
  com.nateprich.command-center-metrics-derive.plist
  com.nateprich.command-center-nightly-watch.plist
)
for name in "${LAUNCHD_PLISTS[@]}"; do
  src="$REPO/launchd/$name"
  dst="$LAUNCH_AGENTS/$name"
  if $DRY; then
    say "would copy: $dst"
  else
    mkdir -p "$LAUNCH_AGENTS"
    cp "$src" "$dst"
    say "copied: $dst"
  fi
done

echo
if $DRY; then
  echo "LaunchAgent files were not changed or reloaded (dry run)."
else
  echo "LaunchAgent copies were refreshed; launchd was not reloaded by this script."
fi
echo "Finish the reload from a Terminal in the logged-in console (Aqua) session."
echo "An automation shell may show Bootstrap failed: 5 even when launchctl print succeeds."
echo "For the keeper, run:"
echo "  launchctl bootout gui/\$(id -u)/com.nateprich.command-center-run-keeper"
echo "  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.nateprich.command-center-run-keeper.plist"
echo "  launchctl print gui/\$(id -u)/com.nateprich.command-center-run-keeper"

# ---------------------------------------------------------------------------
# settings.json — a user-global file that may hold unrelated settings, so it is
# merged key-by-key and backed up first, never rewritten from a template.
# ---------------------------------------------------------------------------
SETTINGS="$CLAUDE/settings.json"
if $DRY; then
  say "would: merge statusLine into $SETTINGS"
else
  python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys, time

path = sys.argv[1]
existing = {}
if os.path.exists(path):
    with open(path) as fh:
        text = fh.read().strip()
    if text:
        try:
            existing = json.loads(text)
        except json.JSONDecodeError:
            print("  REFUSING: %s is not valid JSON; leaving it alone" % path)
            raise SystemExit(1)

want = {"type": "command", "command": "~/.claude/statusline.sh"}
if existing.get("statusLine") == want:
    print("  ok: statusLine already configured")
    raise SystemExit(0)

if os.path.exists(path):
    backup = "%s.bak.%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    print("  backed up: %s" % backup)

if "statusLine" in existing:
    print("  replacing an existing statusLine: %s" % json.dumps(existing["statusLine"]))

existing["statusLine"] = want
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(existing, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)          # atomic; never leaves a half-written settings file
print("  merged: statusLine into %s" % path)
PY
fi

echo
echo "Done. The status line appears on the next Claude Code session; /funnel is"
echo "available immediately in a new session."
