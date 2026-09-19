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

---

# Member-repo audit (#1127, parent #1076)

Date: 2026-09-19. Ticket: finish the audit #1109 left out — read each of
the six member repos' `.github/workflows/*.yml` plus the test entry point
each job runs, apply the same POSIX criteria, and classify every job as
routable or not. No routing change in this ticket: a qualifying job is
named here and a follow-up ticket in that repo does the routing.

Method: each member repo has exactly one workflow file. Sources were read
from shallow read-only clones of remote HEAD (the implementer cannot run
`gh`, so `gh api .../contents/...` was replaced by clones of the same
sources; nothing was retained). SHAs: The-League `814a0f8`,
FF-Weekly-Start-Sit `b41819d`, workbench `48b97bc`, AFL `bee447f`,
career-toolset `63ebe9e`, jeffy-finance-agent `b181cb2`.

Same POSIX criteria as #1109: shebang/extensionless executables on `PATH`,
`bash`/`sh` subprocesses, launchd/plutil/macOS paths, `os.symlink`,
`sudo`/`apt`, and Unix-absolute tool paths.

## The-League — `ci.yml` job `offline` (hobby-linux) — not routable

`make check` is `ruff` (neutral) but `make test` runs
`python3 -m unittest discover -s tests -v`, and the suite is POSIX-bound:

- `tests/test_snapshot_schedule.py` drives `scripts/daily-snapshot.sh`
  via `["/bin/bash", ...]` and builds fake `python`/`git` executables as
  extensionless `#!/bin/sh` files with `chmod 0o755`.
- `tests/test_project_tooling.py` pins the macOS/Linux system interpreter
  at `/usr/bin/python3`.
- `tests/test_report_metadata.py` hard-codes `Path("/tmp/source-snapshot")`.

Porting would mean rewriting the shell-driven fixtures, not relabelling.

## FF-Weekly-Start-Sit — `ci.yml` — neither job routable

Job `offline` (hobby-linux) runs `make check test` into
`python3 -m unittest discover`. The suite is POSIX/macOS-bound:

- `tests/test_launchd.py` asserts plist arguments `["/bin/sh", "-c"]`,
  writes `#!/bin/sh` launchers with the exec bit set, pins
  `LOG_DIRECTORY = "/var/tmp/ff-weekly-start-sit"`, and shells
  `plutil -lint` when present.
- `tests/test_muse_research.py` writes an extensionless `muse` stub
  starting `#!/bin/bash`, `chmod`s it executable, and executes it.

Job `launchd` (hobby-macos) runs `plutil -lint launchd/*.plist` — a
macOS-only tool on the macOS runner. It never touches hobby-linux (no
queue impact) and cannot run on Windows.

## AFL — `ci.yml` job `offline` (hobby-linux) — ROUTABLE (follow-up owns it)

`make check test` runs `compileall` plus `python3 -m unittest discover`.
The suite is 9 tests in `tests/test_interface.py`, pure stdlib (`json`,
`pathlib`, `urllib`, `dataclasses`, `unittest`) reading JSON fixtures.
The `afl/` package imports no `os`, `subprocess`, or `sys`; the repo has
no `scripts/` directory and no `.sh` files; no subprocess, symlink,
shebang, or absolute-path use was found anywhere in `afl/` or `tests/`.

Caveats for the follow-up ticket in that repo: CI invokes the suite via
`make`, so the Windows runner must provide `make` (or the follow-up
inlines the two `python3 -m ...` commands), and `setup-python` must honour
`.python-version` (3.12) there. The suite is seconds long, so routing it
frees hobby-linux only briefly — worth doing, not a capacity fix.

## career-toolset — `ci.yml` job `test` (hobby-linux) — not routable

Runs bare `python3 -m pytest`, but the suite is shell- and
launchd-bound:

- `tests/test_nightly.py` executes `["/bin/sh", "scripts/nightly.sh"]`
  with a `#!/bin/sh` fake on `PATH` (`chmod 0o755`), and asserts launchd
  plist content spelling `$HOME/.local/bin` and
  `$HOME/.local/share/career-agent/checkout/scripts/nightly.sh`.
- `tests/test_score.py` and `tests/test_keychain.py` `chmod` executables
  and shell out to them.

## workbench — `tests.yml` job `tests` (hobby-linux) — not routable

Fails before the tests: the Install step runs
`command -v lsof || sudo apt-get install -y lsof`, which is Debian-Linux
only. The suite independently assumes POSIX:

- `colima-port-watch/test_colima_port_watch.py` writes `#!/bin/bash`
  stubs, sets a POSIX `PATH` (`...:/usr/bin:/bin:/usr/sbin:/sbin`), and
  runs `["/bin/bash", "colima-port-watch"]`.
- The chatgpt-messages-connector suite exercises macOS-only surface
  (`~/Library/Messages/chat.db`, `/usr/bin/osascript`).

## jeffy-finance-agent — `tests.yml` job `tests` (hobby-linux) — not routable

Runs `python -m pytest -q scripts/tests` (Python 3.9), and the collected
tests pin Unix/macOS tool paths throughout:

- `scripts/tests/test_effect_cli.py` shells `["/usr/bin/git", ...]`
  repeatedly.
- `scripts/tests/test_production_runtime.py` matches commands against
  `["/bin/launchctl", "print"]`; `test_launch_descriptors.py` reads and
  constrains `scripts/git-sync.sh`, a `#!/bin/bash` wrapper ending in
  `exec /usr/bin/python3 ...`.
- `scripts/tests/test_effect_boundary.py` pins `osascript`,
  `/usr/bin/git`, and `/usr/bin/curl` spellings.

## Conclusion over all seven repos (#1127 restatement)

| Repo | Job (runner) | Verdict |
|---|---|---|
| command-center (#1109) | `pytest` (hobby-linux) | not routable |
| command-center (#1109) | `check` (ubuntu-latest) | no queue impact |
| The-League | `offline` (hobby-linux) | not routable |
| FF-Weekly-Start-Sit | `offline` (hobby-linux) | not routable |
| FF-Weekly-Start-Sit | `launchd` (hobby-macos) | no queue impact, not routable |
| AFL | `offline` (hobby-linux) | **routable — follow-up ticket owns it** |
| career-toolset | `test` (hobby-linux) | not routable |
| workbench | `tests` (hobby-linux) | not routable |
| jeffy-finance-agent | `tests` (hobby-linux) | not routable |

Restated over all seven repos: **extra hobby-linux processes are still
the simpler path.** Six of the seven hobby-linux jobs fail the same POSIX
criteria (bash/sh-driven fixtures, launchd/macOS pins, apt/sudo,
Unix-absolute tool paths); the single qualifier, AFL `offline`, is a
seconds-long pure-stdlib suite whose move off the shared runner would not
dent the burst queue #1076 describes. No routing change was made here; the
AFL follow-up ticket in that repo does the routing (including the `make`
caveat above).
