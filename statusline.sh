#!/usr/bin/env bash
# statusline.sh — render Claude Code's status line, and cache subscription usage.
#
# Claude Code pipes session JSON to this script on stdin. For Pro and Max
# subscribers that JSON carries account-level rate-limit numbers, which include
# claude.ai and mobile usage — which is what makes them usable as a budget
# signal at all.
#
# The script does double duty: it renders the status line, and it writes the two
# windows to a cache that the scheduled routines read.
#
# The cache is a cache, never a source of truth. A cached value only ever
# *understates* usage, because used_percentage rises monotonically within a
# window and the file goes stale whenever Claude Code is not running. So the
# budget gate must live inside a session, where its own statusline write has
# just refreshed this file — never before one. See plan.md, "Reading usage".
#
# Install:
#   ~/.claude/settings.json → { "statusLine": { "type": "command",
#                                               "command": "~/.claude/statusline.sh" } }

set -uo pipefail

CACHE="${COMMAND_CENTER_USAGE_CACHE:-$HOME/.claude/command-center-usage.json}"

input=$(cat)

# ---------------------------------------------------------------------------
# Cache the usage windows.
#
# Three documented behaviours this has to survive, all of which look identical
# to a naive reader:
#   1. rate_limits is absent entirely until the first API response of a session,
#      and always for non-subscribers.
#   2. Each window is independently optional.
#   3. Claude Code drops a window once its resets_at has passed.
#
# So: when rate_limits is present it is authoritative and replaces the file
# wholesale — a window that vanished has genuinely expired and must not linger.
# When it is absent we leave the previous file alone rather than erasing it,
# because "not reported yet" is not the same as "no usage". Readers tell the
# difference with captured_at and fail closed on anything stale.
# ---------------------------------------------------------------------------
if printf '%s' "$input" | jq -e '.rate_limits' >/dev/null 2>&1; then
  mkdir -p "$(dirname "$CACHE")"
  tmp=$(mktemp "${CACHE}.XXXXXX") || tmp=""
  if [ -n "$tmp" ]; then
    # Build the object, then drop absent windows. NOT `{k: (x // empty)}` —
    # in jq an `empty` inside object construction collapses the whole object to
    # an empty stream, so one missing window writes an empty file rather than an
    # object with one key. `// empty` is right for reading a scalar and wrong
    # here.
    if printf '%s' "$input" | jq --argjson now "$(date +%s)" '{
          captured_at: $now,
          five_hour: .rate_limits.five_hour,
          seven_day: .rate_limits.seven_day
        } | with_entries(select(.value != null))' >"$tmp" 2>/dev/null; then
      mv -f "$tmp" "$CACHE"   # atomic: a reader never sees a half-written file
    else
      rm -f "$tmp"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Render.
# ---------------------------------------------------------------------------
DIM=$'\033[2m'; RESET=$'\033[0m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'

field() { printf '%s' "$input" | jq -r "$1 // empty" 2>/dev/null; }

model=$(field '.model.display_name')
cwd=$(field '.workspace.current_dir')
[ -n "$cwd" ] && cwd=$(basename "$cwd")

branch=""
if [ -n "$cwd" ]; then
  branch=$(git -C "$(field '.workspace.current_dir')" branch --show-current 2>/dev/null)
fi

# A window is rendered only if it is actually present. Never assume presence,
# and never render a missing window as 0% — absent means unknown, and unknown
# reading as "plenty left" is the exact failure the budget exists to prevent.
window() {
  local key=$1 label=$2 pct resets colour
  pct=$(printf '%s' "$input" | jq -r ".rate_limits.${key}.used_percentage // empty" 2>/dev/null)
  [ -z "$pct" ] && return 0

  colour=$GREEN
  awk -v p="$pct" 'BEGIN{exit !(p >= 75)}' && colour=$YELLOW
  awk -v p="$pct" 'BEGIN{exit !(p >= 90)}' && colour=$RED

  resets=$(printf '%s' "$input" | jq -r ".rate_limits.${key}.resets_at // empty" 2>/dev/null)
  local when=""
  if [ -n "$resets" ]; then
    local left=$(( resets - $(date +%s) ))
    if   [ "$left" -le 0 ];    then when=""
    elif [ "$left" -lt 3600 ]; then when=$(printf ' %dm' $(( left / 60 )))
    elif [ "$left" -lt 86400 ];then when=$(printf ' %dh' $(( left / 3600 )))
    else                            when=$(printf ' %dd' $(( left / 86400 )))
    fi
    [ -n "$when" ] && when="${DIM}↻${when}${RESET}"
  fi
  printf '%s%s %.0f%%%s %s' "$colour" "$label" "$pct" "$RESET" "$when"
}

parts=()
[ -n "$model" ]  && parts+=("${DIM}${model}${RESET}")
[ -n "$cwd" ]    && parts+=("$cwd")
[ -n "$branch" ] && parts+=("${DIM}${branch}${RESET}")

five=$(window five_hour "5h");  [ -n "$five" ] && parts+=("$five")
seven=$(window seven_day "7d"); [ -n "$seven" ] && parts+=("$seven")

# Say so explicitly rather than rendering a line that looks like a healthy zero.
if [ -z "$five$seven" ]; then
  parts+=("${DIM}usage unknown${RESET}")
fi

printf '%s' "$(IFS='  '; echo "${parts[*]}")"
