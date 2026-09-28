# Codex routine — one ticket per run

Paste this into a **Codex Scheduled** task; its tier and schedule stay outside
the prompt.

Configure the sandbox with write access only to Codex’s per-session directory
and `~/.claude/command-center-heartbeat`. Give
`/Users/nateprich/.claude/command-center-run` read-and-execute access only,
except that its `codex-runs/` subtree is writable. The runtime root’s resolved
target may go in sandbox configuration, nowhere else.

Never invoke the Codex CLI headlessly. The in-app schedule is the authorised
surface.

---

Run `python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent codex --tier standard` exactly once and follow the JSON it prints.
`begin` can take several minutes. If the exec tool yields a timeout or partial
output mid-run, keep reading that same exec session until the process exits.
Never treat that yield as a failure, and never invoke `begin` again.

Work one ticket, then stop. When `do` is `stop`, finish the printed `run` with
the gate’s outcome (`over` is `skipped-over-pace`, `unknown` is
`skipped-usage-unknown`, `reserve` is `skipped-api-reserve`,
`config` is `config-drift`, otherwise `nothing-to-do`) and stop.

When `do` is `ticket`, the ticket is already claimed. Treat `packet` as the
implementation evidence: read its ticket, parent plan, current-head verdict,
blocking list, and prior-run digest. Treat `vendor` as binding for sandbox scope
and Command Center path spelling.

Clone `packet.repo` into a new owner-only (`0700`) directory under the runtime
root’s `codex-runs/` subtree, named `ticket-<number>-<YYYYMMDDTHHMMSSffffffZ>`
(UTC). Work on `ticket/<number>` from `origin/main`. If the remote branch
exists, establish its contents before continuing or resetting it; never discard
unknown work. A rejected verdict at the current head requires a new pushed head
addressing every blocking item. Keep scratch and build files inside this
checkout. `finish-ticket` removes it; if a push fails, it leaves the directory
for diagnosis.

Implement only what the ticket and plan require. Do not change project `Status`
or `Class`, do not merge, and do not repair unrelated defects.

- Test at the ticket's named seams (else the public interface), one behaviour each.
- A `Reproduction:` first Accept item: write that test first and see it fail.
- Expected values: from an independent source, never recomputed the code's way.
- No refactoring beyond the ticket; a needed one is a departure and its own ticket.
- Run the affected test files while working; `finish-ticket` runs the full suite.

Return exactly one structured answer. A no-diff success adds the non-empty
`evidence` list of GitHub URLs its Accept names (comment, rename event, closed PR
or posted measurement), each verified against this run's heartbeat start.

- success: `{"done":true,"summary":"...","departures":[]}`; optional `risks`:
  where review should look hardest
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
