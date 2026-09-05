#!/usr/bin/env bash
# install.sh — wire Command Center into ~/.claude.
#
# Idempotent: safe to re-run after every pull. Everything is symlinked back to
# the checkout, so editing the repo takes effect immediately and there is never
# a second copy to drift.
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
if [ -d "$CLAUDE/command-center" ] && [ ! -L "$CLAUDE/command-center" ]; then
  if [ -z "$(find "$CLAUDE/command-center" -mindepth 1 ! -type l 2>/dev/null)" ]; then
    $DRY || rm -rf "$CLAUDE/command-center"
    say "replaced the old per-script directory with a single link"
  else
    say "REFUSING: $CLAUDE/command-center holds real files. Move it aside first."
    exit 1
  fi
fi
link "$REPO"               "$CLAUDE/command-center"
link "$REPO/statusline.sh" "$CLAUDE/statusline.sh"
# Every skill in the checkout, not a hardcoded list: naming them one by one is
# how a new skill silently does not exist until someone remembers this file.
for skill in "$REPO"/skills/*/; do
  [ -d "$skill" ] || continue
  link "${skill%/}" "$CLAUDE/skills/$(basename "$skill")"
done

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
