# Codex routine — one ticket per run

Paste this into a **Codex Scheduled** task. Keep the task’s tier and schedule
outside the prompt.

Configure the sandbox with write access only to Codex’s per-session directory
and `~/.claude/command-center-heartbeat`. Give
`/Users/nateprich/.claude/command-center-run` read-and-execute access only. Codex
may require that read-only root’s resolved target in sandbox configuration; the
resolved spelling belongs there and nowhere in commands or prompts.

Never invoke the Codex CLI headlessly. The in-app schedule is the authorised
surface.

---

Run `python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent codex --tier standard` exactly once and follow the JSON it prints.

Work one ticket, then stop. When `do` is `stop`, finish the printed `run` with
the reported gate’s established outcome (`over` is `skipped-over-pace`,
`unknown` is `skipped-usage-unknown`, otherwise `nothing-to-do`) and stop.

When `do` is `ticket`, the ticket is already claimed. Treat `packet` as the
implementation evidence: read its ticket, parent plan, current-head verdict,
blocking list, and prior-run digest. Treat `vendor` as binding for sandbox scope
and Command Center path spelling.

Clone `packet.repo` inside the current per-session workspace. Work on
`ticket/<number>` from `origin/main`. If the remote branch exists, establish its
contents before continuing or resetting it; never discard unknown work. A
rejected verdict at the current head requires a new pushed head addressing every
blocking item. Keep all checkout, scratch, and build files inside the workspace.

Implement only what the ticket and plan require. Do not change project `Status`
or `Class`, do not merge, and do not repair unrelated defects. Make the code
change and return exactly one structured answer:

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
