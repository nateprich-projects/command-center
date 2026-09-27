# Codex routine — one ticket per run

Paste this into a **Codex Scheduled** task. Keep the task’s tier and schedule
outside the prompt.

Configure the sandbox with write access only to Codex’s per-session directory,
`~/.claude/command-center-heartbeat`, and the documented runtime root’s
`codex-runs/` subtree. Give `/Users/nateprich/.claude/command-center-run`
read-and-execute access only, except that `codex-runs/` subtree is writable. Codex may
require the runtime root’s resolved target in sandbox configuration; the
resolved spelling belongs there and nowhere in commands or prompts.

Never invoke the Codex CLI headlessly. The in-app schedule is the authorised
surface.

---

Run `python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent codex --tier standard` exactly once and follow the JSON it prints.
`begin` can take several minutes. If the exec tool yields a timeout or partial
output while the process is still running, keep reading that same exec session
until the process exits. Never treat that yield as a failure, and never invoke
`begin` again.

Work one ticket, then stop. When `do` is `stop`, finish the printed `run` with
the reported gate’s established outcome (`over` is `skipped-over-pace`,
`unknown` is `skipped-usage-unknown`, `reserve` is `skipped-api-reserve`,
`config` is `config-drift`, otherwise `nothing-to-do`) and stop.

When `do` is `ticket`, the ticket is already claimed. Treat `packet` as the
implementation evidence: read its ticket, parent plan, current-head verdict,
blocking list, and prior-run digest. Treat `vendor` as binding for sandbox scope
and Command Center path spelling.

For a no-diff ticket, its `Accept` names the GitHub-artifact evidence channel the
finish check verifies: a comment, rename event, closed PR, or posted measurement.

Create a unique owner-only (`0700`) directory under the documented runtime
root’s `codex-runs/` subtree, named
`ticket-<number>-<YYYYMMDDTHHMMSSffffffZ>` using a UTC timestamp. Clone
`packet.repo` into that directory. Work on `ticket/<number>` from `origin/main`.
If the remote branch exists, establish its contents before continuing or
resetting it; never discard unknown work. A rejected verdict at the current head
requires a new pushed head addressing every blocking item. Keep scratch and
build files inside this checkout. `finish-ticket` removes this run directory
after a successful push or a finish that records work not kept; if a push fails,
it leaves the directory for diagnosis.

Implement only what the ticket and plan require. Do not change project `Status`
or `Class`, do not merge, and do not repair unrelated defects. Make the code
change and return exactly one structured answer:

For a no-diff success, include the optional non-empty evidence list of GitHub
URLs named by the ticket's Accept. The finish check verifies each artifact
against this run's heartbeat start.

- success: `{"done":true,"summary":"...","departures":[]}`
- a required unavailable human action:
  `{"blocked_on_human":{"reason":"<allowlisted reason>","action":"..."}}`
- an unlanded named prerequisite before any change: `{"declined":"..."}`

Write that answer to a file in the Codex session directory, outside the ticket
checkout. From the ticket checkout, run
`python3 /Users/nateprich/.claude/command-center-run/funnel.py finish-ticket --run <run> --answer-file <path>`.
The runner validates the answer, tests the checkout, commits and pushes, opens
the PR or records the blocked/declined path, releases the claim, and finishes
the heartbeat. Report any failure honestly and stop; do not simulate an effect
the runner did not complete.
