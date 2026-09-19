# Windows-runner audit (#1109, parent #1076)

Date: 2026-09-19. Ticket: Audit the CI workflows of command-center and the
member repos for jobs that can run on the idle Windows runner, route only the
jobs that pass there, and leave everything else on hobby-linux. If few or no
jobs qualify, make no routing change and record that extra Linux processes are
the simpler path (Nate Shaped-gate decision 2026-09-18).

## Scope

This checkout is the command-center repo, so only its workflows could be
audited from their sources. Member-repo workflows (The-League,
FF-Weekly-Start-Sit, workbench, AFL, career-toolset, jeffy-finance-agent) live
in their own repos; routing them would need a PR in each repo, not a change
here. The POSIX criteria below apply to them unchanged.

## Findings

### `tests.yml` job `pytest` (runs on hobby-linux) — not routable

The job runs `python3 -m pytest tests/ -q` plus the dashboard Node checks in
one job. The pytest suite assumes a POSIX host throughout:

- `tests/conftest.py` builds the offline `gh` stub as an extensionless `gh`
  file with a `#!/bin/sh` shebang and `chmod 0o755`, then prepends it to
  `PATH`. Windows cannot execute a shebang script and does not resolve an
  extensionless name on `PATH` the same way.
- `tests/test_statusline.py` (10 tests) shells out via `["bash", ...]` with a
  POSIX `PATH` (`/usr/bin:/bin:/usr/local/bin`) to drive `statusline.sh`, a
  `#!/usr/bin/env bash` script.
- `tests/test_launchd_drift.py` (23 tests) runs
  `["/bin/bash", scripts/install.sh]` and asserts macOS launchd plist content
  (`/Users/nateprich/...`, `~/Library/LaunchAgents`, `launchctl`).
- `tests/test_doctor.py` (70 tests) and `tests/test_dashboard_deploy.py`
  (19 tests) rely on `os.symlink`, which needs elevated privileges on Windows
  and fails for ordinary runner accounts.
- Guardrail tests pin macOS-only path spellings
  (`/Users/nateprich/.claude/command-center`) that the routines require.

Porting this suite to Windows would mean rewriting the POSIX-bound fixtures,
not just relabelling the job.

### Dashboard Node checks (same job) — routable in isolation, not worth splitting

`dashboard/test/*.test.js` and the `node --check` invocations look
platform-neutral (no shell, symlink, or POSIX-path use found). But they run in
the same job as the pytest suite and take seconds, while pytest is the part
that occupies hobby-linux. Splitting them onto the Windows runner would leave
the queue unchanged and add a second runner dependency for no capacity gain.

### `watchdog.yml` job `check` (runs on ubuntu-latest) — no change needed

Already on GitHub-hosted runners, never touches hobby-linux, and has no queue
impact. No routing applies.

## Conclusion

No command-center job qualifies for the Windows runner, and member-repo jobs
cannot be routed from this repo. Per the ticket's fallback: **no routing
change was made; extra hobby-linux processes are the simpler path**
(Ticket #1108, pre-authorised to host headroom). The dedicated
command-center label remains the measured fallback, decided by the
post-capacity measurement in #1111.
