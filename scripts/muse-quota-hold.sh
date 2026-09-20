# One lane's provider refusal parks every lane, until the reset it named.
#
# Muse's subscription quota is not the pace gate in usage.py. That gate prices
# the session journal against a dollar cap; the provider's own window is
# invisible to it. When the window empties, `muse exec` fails in a few seconds
# with
#
#   API error 429 ...: Subscription quota exhausted. Your usage window resets
#   at 2026-09-21T00:00:00Z. (rate_limit_error)
#
# and the runner used to read that as the ticket's failure: release, finish
# `errored`, and try the next ticket ten minutes later. On 2026-09-19 that
# errored FF#208 five times in an hour and would have kept going for the
# twenty-nine hours to the reset, spending a clone and a Project load each
# time and leaving a ticket that looks broken but never ran.
#
# So the first lane to see the refusal writes the reset stamp, and every lane
# checks for it before it claims anything. The hold is a single file holding
# one ISO-8601 stamp: it expires by itself, so nothing has to remember to
# clear it, and a lane started after the reset removes it on the way past.
#
# Sourced by scripts/muse-implement, scripts/muse-review and
# scripts/muse-review-engine. Keep it POSIX-plain; it is sourced, never run.

MUSE_QUOTA_HOLD_FILE="${MUSE_QUOTA_HOLD_FILE:-$HOME/.claude/command-center-muse-quota-hold}"

#: Fallback hold when the provider refuses without a stamp this can read.
#: Long enough to stop a ten-minute retry loop, short enough that a
#: misparse cannot park the lanes for a day.
MUSE_QUOTA_FALLBACK_SECONDS="${MUSE_QUOTA_FALLBACK_SECONDS:-3600}"

# Echo the reset stamp while a recorded hold is still in the future, and
# return non-zero when there is no live hold. A hold that has passed, or one
# that cannot be read, is removed rather than trusted: a lane that cannot
# understand its own hold must run, not park.
muse_quota_hold_until() {
  [ -f "$MUSE_QUOTA_HOLD_FILE" ] || return 1
  python3 - "$MUSE_QUOTA_HOLD_FILE" <<'PY'
import datetime
import os
import sys

path = sys.argv[1]


def drop():
    try:
        os.remove(path)
    except OSError:
        pass
    raise SystemExit(1)


try:
    with open(path) as handle:
        stamp = handle.read().strip()
except OSError:
    raise SystemExit(1)
if not stamp:
    drop()
try:
    resets = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
except ValueError:
    drop()
if resets.tzinfo is None:
    resets = resets.replace(tzinfo=datetime.timezone.utc)
if resets <= datetime.datetime.now(datetime.timezone.utc):
    drop()
print(stamp)
PY
}

# Scan a captured stderr file for the provider's refusal. On a match, record
# the hold and echo the stamp the lanes will wait for; otherwise return
# non-zero and leave any existing hold alone. A refusal whose stamp this
# cannot read still parks the lanes, for MUSE_QUOTA_FALLBACK_SECONDS.
muse_quota_record() {
  [ -f "${1:-}" ] || return 1
  MUSE_QUOTA_FALLBACK_SECONDS="$MUSE_QUOTA_FALLBACK_SECONDS" \
    python3 - "$1" "$MUSE_QUOTA_HOLD_FILE" <<'PY'
import datetime
import os
import re
import sys

capture, path = sys.argv[1], sys.argv[2]

try:
    with open(capture, errors="replace") as handle:
        text = handle.read()
except OSError:
    raise SystemExit(1)

if "Subscription quota exhausted" not in text:
    raise SystemExit(1)

stamp = None
match = re.search(
    r"usage window resets at\s+(\S+?)[\s.]*(?:\(|$)", text, re.MULTILINE)
if match:
    candidate = match.group(1).rstrip(".")
    try:
        parsed = datetime.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        stamp = parsed

if stamp is None:
    try:
        seconds = float(os.environ.get("MUSE_QUOTA_FALLBACK_SECONDS", "3600"))
    except ValueError:
        seconds = 3600.0
    stamp = datetime.datetime.now(datetime.timezone.utc) + (
        datetime.timedelta(seconds=seconds))

written = stamp.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
directory = os.path.dirname(path)
if directory:
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        raise SystemExit(1)
try:
    with open(path, "w") as handle:
        handle.write(written + "\n")
except OSError:
    raise SystemExit(1)
print(written)
PY
}
