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
now=$(date +%s)

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
    if printf '%s' "$input" | jq --argjson now "$now" '{
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
#
# This runs on every status-line update, debounced at 300ms, in every session.
# So it reads everything in ONE jq call and does the arithmetic in bash: a
# per-field jq and an awk per colour threshold cost about 50ms a render, which
# is a sixth of the debounce window burned on process spawns.
# ---------------------------------------------------------------------------
DIM=$'\033[2m'; RESET=$'\033[0m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'

# Joined on US (0x1f), NOT a tab. Tab is IFS *whitespace*, so bash collapses runs
# of them into one delimiter and drops empty leading fields — an absent five_hour
# would shift every later value left, and the line would render the seven-day
# number labelled "5h". Wrong in exactly the direction the budget exists to catch.
model=""; dir=""; five_pct=""; five_reset=""; seven_pct=""; seven_reset=""
IFS=$'\037' read -r model dir five_pct five_reset seven_pct seven_reset <<<"$(
  printf '%s' "$input" | jq -r '[
    (.model.display_name // ""),
    (.workspace.current_dir // ""),
    (.rate_limits.five_hour.used_percentage // ""),
    (.rate_limits.five_hour.resets_at // ""),
    (.rate_limits.seven_day.used_percentage // ""),
    (.rate_limits.seven_day.resets_at // "")
  ] | join("\u001f")' 2>/dev/null
)"

branch=""
if [ -n "$dir" ]; then
  branch=$(git -C "$dir" branch --show-current 2>/dev/null)
fi

# A window is rendered only if it is actually present. Never assume presence,
# and never render a missing window as 0% — absent means unknown, and unknown
# reading as "plenty left" is the exact failure the budget exists to prevent.
window() {
  local label=$1 pct=$2 resets=$3
  [ -z "$pct" ] && return 0

  # Round half-up rather than truncate: truncation always understates usage,
  # and understating usage is what burns the week.
  local whole=${pct%%.*} frac=""
  [ -z "$whole" ] && whole=0
  case $pct in *.*) frac=${pct#*.} ;; esac
  [ -n "$frac" ] && [ "${frac:0:1}" -ge 5 ] 2>/dev/null && whole=$(( whole + 1 ))
  local colour=$GREEN
  [ "$whole" -ge 75 ] && colour=$YELLOW
  [ "$whole" -ge 90 ] && colour=$RED

  local when=""
  if [ -n "$resets" ]; then
    local left=$(( resets - now ))
    if   [ "$left" -le 0 ];     then when=""
    elif [ "$left" -lt 3600 ];  then when=$(printf ' %dm' $(( left / 60 )))
    elif [ "$left" -lt 86400 ]; then when=$(printf ' %dh' $(( left / 3600 )))
    else                             when=$(printf ' %dd' $(( left / 86400 )))
    fi
    [ -n "$when" ] && when=" ${DIM}↻${when}${RESET}"
  fi
  printf '%s%s %d%%%s%s' "$colour" "$label" "$whole" "$RESET" "$when"
}

parts=()
[ -n "$model" ]  && parts+=("${DIM}${model}${RESET}")
[ -n "$dir" ]    && parts+=("$(basename "$dir")")
[ -n "$branch" ] && parts+=("${DIM}${branch}${RESET}")

five=$(window "5h" "$five_pct" "$five_reset");   [ -n "$five" ]  && parts+=("$five")
seven=$(window "7d" "$seven_pct" "$seven_reset"); [ -n "$seven" ] && parts+=("$seven")

# Say so explicitly rather than rendering a line that looks like a healthy zero.
if [ -z "$five$seven" ]; then
  parts+=("${DIM}usage unknown${RESET}")
fi

printf '%s' "$(IFS='  '; echo "${parts[*]}")"
