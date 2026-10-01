#!/bin/bash
# Does a subshell that sets its own TERM trap and then `exit 0`s run the
# parent's EXIT trap? Same shape as scripts/muse-implement's bound watcher.
cleanup() { [[ $BASHPID != "$TOP" ]] && echo "CHILD-RAN-CLEANUP" >&2; }
TOP=$BASHPID
trap cleanup EXIT
( sleep 30 & sp=$!; trap 'kill "$sp" 2>/dev/null; exit 0' TERM; wait "$sp" ) &
bp=$!
sleep "${DELAY:-0}"
kill "$bp" 2>/dev/null; wait "$bp" 2>/dev/null
