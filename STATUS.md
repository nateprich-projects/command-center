# Status

**Last updated: 2026-09-05**

v0 in progress. Target: shipped in under two weeks from 2026-09-05.

## Built and verified

**Deliverable #1 — GitHub setup.** Complete.

- **Project: [Command Center](https://github.com/users/nateprich/projects/2)** (user-owned,
  `#2`, private). Not Project 1 — see `plan.md`, Verification status item 7.
- `Status` single-select: `Ideas → Shaped → Ready → Building → Done → Parked`, in funnel
  order, each with its meaning as the option description.
- `Class` single-select: `Broken → Maintenance → Improve → New → Replace`, in ladder order.
- Labels `needs-shaping` and `blocked` on this repo, and GitHub's ten stock labels
  deleted. That is the complete label set.
- Topic `command-center` on this repo — the funnel's membership gate.

**Time-at-gate is real.** `ProjectV2ItemStatusChangedEvent` observed firing on
`#1` with a usable `createdAt`. The issue-number tiebreak stand-in was never needed.
Evidence in `LEARNINGS.md`.

**Deliverable #2 — `funnel.py`.** `queue`, `next` and `brief` all run against live
GitHub. 38 fixture tests on the ordering rules, mutation-checked: reversing the bottom-up
order, letting an unset `Class` rank as `Broken`, and making `children_all_closed`
vacuously true for a childless item each fail a test.

Stdlib only, Python 3.9. Authentication is delegated to the `gh` CLI, so the program
never reads, stores or passes a token.

**The single-in-motion lock.** `In motion since` text field on the Project, written by
`funnel claim` / `funnel release`. Full cycle verified live: claim, `next` and a second
claim both refuse, release, `next` resumes.

**Deliverable #4 — `statusline.sh`.** Renders the status line and caches both rate-limit
windows. 8 tests covering the absence cases: a missing window is omitted rather than shown
as `0%`, an absent `rate_limits` renders "usage unknown" and does not erase a previous
reading, and a present payload replaces the file wholesale so an expired window cannot
linger. Written atomically via `mktemp` + `mv`.

**Installed 2026-09-05** via `scripts/install.sh`. `funnel.py`, `statusline.sh` and the
`funnel` skill are symlinked into `~/.claude`, and `statusLine` is merged into
`settings.json` (backup at `settings.json.bak.20260905-011724`). Re-running is a no-op.
Verified from the installed paths, not the checkout.

**Deliverable #3 — `/funnel` skill.** `skills/funnel/SKILL.md`. Shells out to
`funnel brief` and renders it; explicitly forbidden from ranking, reordering or filtering.

**`scripts/install.sh`.** Idempotent, symlink-based, `--dry-run` supported. Symlinks
`funnel.py`, `statusline.sh` and the skill into `~/.claude`, and merges `statusLine` into
`settings.json` key-by-key with a timestamped backup. Refuses to clobber a real file that
is not already a symlink, and refuses to touch a `settings.json` it cannot parse.

## Built, not verified

- **The `/funnel` skill end to end.** Installed and discoverable, but it has not been
  invoked in a fresh session yet — skills load at session start.
- **`funnel.py` at scale.** Exercised against a Project holding one item. Pagination,
  multi-repo membership and the 30-day maintenance window have fixture coverage but no
  live data behind them yet.

## Not started

| # | Deliverable | Blocked on |
|---|---|---|
| 3 | `/funnel` skill | #2 |
| 4 | `statusline.sh` — renders the status line, caches both rate-limit windows | — |
| 5 | Codex routine and Claude review routine | #2, #4 |
| 6 | GitHub Actions heartbeat — records outcome, not just liveness | #5 |
| 7 | Onboard this repo as the first member; file remaining work as issues | partly done |

## Blocked

Nothing.

## Open questions

- **"Share of runs" has no run log in v0.** `maintenance_load` derives it from issues
  closed in the window, by `Class`. The heartbeat (#6) is the first thing that records
  runs; this should probably read from it once it exists.
- **Codex's usage signal.** Its equivalent of Claude's `rate_limits` payload is unknown.
  The early-exit gate pattern holds regardless; v0 ships that gate as an explicit
  fail-closed stub.
- **Dependabot** across repos other than `workbench`. A one-time sweep during each repo's
  onboarding.

## Notes

- The v2 design record was never committed to `workbench` — the housekeeping deletion
  `plan.md` called for is void. Confirmed 2026-09-05.
