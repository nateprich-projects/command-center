# Codex routine — one ticket per run

Paste into a **Codex Scheduled** task; keep tier and schedule external.

Sandbox writes are limited to Codex’s per-session directory and
`~/.claude/command-center-heartbeat`. Grant
`/Users/nateprich/.claude/command-center-run` read-and-execute access only;
its `codex-runs/` subtree is writable. Only sandbox configuration may name
the runtime root’s resolved target.

Never invoke the Codex CLI headlessly.

---

Run `python3 /Users/nateprich/.claude/command-center-run/funnel.py begin --agent codex --tier standard` exactly once and follow the JSON it prints.
`begin` can take several minutes. If the exec tool yields a timeout or partial
output, keep reading that same exec session until the process exits.
Never treat that yield as a failure, and never invoke `begin` again.

After `begin`, run the SSD archive helper only when its JSON has `gate: "ok"`:

```sh
python3 /Users/nateprich/.claude/command-center-run/session_log_archive.py
```

Skip it for every other gate. It runs only 03:00–03:59 local; unmounted SSD
skips cleanly. Keep it app-hosted, never
in the launchd run-keeper.

Work one ticket, then stop. When `do` is `stop`, finish `run`: `over`→
`skipped-over-pace`; `unknown`→`skipped-usage-unknown`; `reserve`→
`skipped-api-reserve`; `config`→`config-drift`; else `nothing-to-do`.

When `do` is `ticket`, the ticket is already claimed. Treat `packet` as the
implementation evidence: read ticket, parent plan, current-head verdict,
blocking list, and prior-run digest. Treat `vendor` as binding.

Clone `packet.repo` into an owner-only (`0700`) directory under runtime root’s
`codex-runs/` subtree, named `ticket-<number>-<YYYYMMDDTHHMMSSffffffZ>` (UTC stamp:
`python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))'`).
Work on `ticket/<number>` from `origin/main`. Inspect `origin/ticket/<number>`
before resetting or continuing; preserve unknown work. A rejected current-head
verdict needs a new pushed head addressing every blocker. Keep scratch/build
files inside. `finish-ticket` removes it; if a push fails, it leaves the
directory for diagnosis.

Implement only ticket/plan scope. Do not change project `Status`/`Class`, merge,
or fix unrelated defects.

- Test at the ticket's named seams (else the public interface), one behaviour each.
- A `Reproduction:` first Accept item: write that test first and see it fail.
- Expected values: from an independent source, never recomputed the code's way.
- No refactoring beyond the ticket; a needed one is a departure and its own ticket.
- Run the affected test files while working; `finish-ticket` runs the full suite.

Return exactly one structured answer. A no-diff success adds the non-empty
`evidence` list of GitHub URLs its Accept names, each verified against this
run's heartbeat start.

- success: `{"done":true,"summary":"...","departures":[]}`; optional
  `"risks":["..."]`, where review should look hardest
- an action you cannot take:
  `{"blocked_on_human":{"reason":"...","action":"..."}}`, `reason` one of
  `a Claude Code environment` (Mac work a Claude session can do), or Nate's
  `an app UI with no API`, `entering a credential`,
  `an account or billing setting`, `physical access to a machine`
- an unlanded named prerequisite before any change: `{"declined":"..."}`

Write it to a file in the Codex session directory, outside the ticket
checkout. From the ticket checkout, run
`python3 /Users/nateprich/.claude/command-center-run/funnel.py finish-ticket --run <run> --answer-file <path>`.
The runner validates the answer, tests the checkout, commits and pushes, opens
the PR or records the blocked/declined path, releases the claim, and finishes
the heartbeat. Report any failure honestly and stop; never simulate an effect
the runner did not complete.
