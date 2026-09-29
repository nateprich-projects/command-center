# Finish-ticket subprocess bounds

## Baseline verification for #1339

The parent plan calls for checking the premise against the pre-change `origin/main`.
At `034913e` (`origin/main` for this run), `engine/implement.py::_run` invoked
`subprocess.run` with captured text output and no `timeout` argument. Its Git and
test-command callers were therefore unbounded. The same file had two separate
direct subprocess paths: `fetch_prior_run` already used `timeout=30`, and the
Python-version probe already used `timeout=20`. The verified finding is that
`_run` was unbounded, not that every subprocess in the module was unbounded.

The audit command was:

```text
git show origin/main:engine/implement.py | rg -n "def _run|subprocess\\.run|timeout="
```

## Finish-path command inventory

The AST inventory test pins **19 bounded Git callsites** in `engine/implement.py`:

| Callsite | Git commands |
| --- | --- |
| `checkout_context` | `git rev-parse --show-toplevel`; `git branch --show-current`; `git remote get-url origin` |
| `resolve_checkout_repo` | `git remote get-url origin` |
| `_remote_branch_exists`; `remote_ticket_head` | `git ls-remote --exit-code --heads origin refs/heads/<branch>` |
| `_git_name_paths` | One bounded command template, expanded by the path-list callers below |
| `_stage_explicit_paths` | `git add -- <selected paths>` |
| `_commit_if_needed` | `git commit -m <summary>`; `git rev-list --count origin/main..HEAD` |
| `_push_ticket_branch` | `git fetch origin <ticket refspec>`; `git merge-base --is-ancestor origin/<branch> HEAD`; conditional `git merge -s ours`; `git push --set-upstream origin <branch>` |
| `_keep_work` | `git commit -m <WIP reason>`; `git rev-list --count origin/main..HEAD` |
| `_checkpoint_work` | `git commit -m <WIP checkpoint>` |
| `_run_finish_tests` | `git fetch origin +refs/heads/main:refs/remotes/origin/main` (#1804) |
| `finish_done` | `git rev-parse HEAD` after the push, the SHA the PR's evidence block is keyed to (#1805) |

`_git_name_paths` expands to these five distinct command forms; the cached diff
form is reused by two callers:

```text
git diff --name-only -z
git diff --cached --name-only -z
git ls-files --others --exclude-standard -z
git diff --name-only -z origin/main...HEAD
git ls-files -z -- <selected paths>
```

Since #1804 the test commands run on the head merged with origin/main, through
`scripts/review_evidence.py`. Its own Git calls (`rev-parse`, `worktree add`,
`merge`, `diff`, `worktree remove`, `worktree prune`) go through `_run` with the
local Git bound and are outside this file's inventory; its test commands go
through `run_tests` with the bounds below. Since #1805 the finish then runs the
same script's added-test classification on the merge's base, whose pytest runs
share one four-minute budget (`REPRODUCTION_BUDGET_SECONDS`) instead of the
45-minute test bound.

Test commands come from the repository's CI test slot. For this checkout,
`finish-ticket --dry-run` resolved the `Run the suite` step in
`.github/workflows/tests.yml` to a `funnel.py` syntax compile and
`python3 -m pytest tests/ -q`; there is no `make` invocation in this repository's
resolved plan. Repositories whose CI step invokes `make` are bounded as test
commands too. The fixtures cover direct and `sh -c` wrapped `make` and `pytest`
commands. The Python version probe is a separate bounded subprocess.

## Measurements and selected bounds

Measurements were taken on this checkout on 2026-09-28. Local Git inventory
commands each completed within 0.0054 seconds across three runs. In a disposable
local remote, add, commit, and merge each completed within 0.0216 seconds; push
completed within 0.0402 seconds. Against GitHub, `ls-remote` took 0.9282 seconds
and fetch took 1.0166 seconds. The syntax compile took at most 0.0800 seconds
across seven runs, and `compileall` took 0.1176 seconds. The Python version probe
took at most 0.0160 seconds across seven runs. The latest green CI run
`#36331008053` spent 6m54s in pytest.

| Command family | Bound | Measurement used |
| --- | ---: | --- |
| Local Git | 2 minutes | 0.0054s live inventory maximum; 0.0216s maximum for local add/commit/merge |
| Remote Git | 10 minutes | 1.0166s GitHub fetch maximum; 0.0402s local push |
| Test commands (`make`, `pytest`, including shell wrappers) | 45 minutes | 6m54s latest green pytest run |
| Syntax compile and `compileall` | 2 minutes | 0.0800s syntax compile; 0.1176s `compileall` |
| Python version probe | 20 seconds | 0.0160s maximum across seven runs |
| Unclassified helper fallback | 20 seconds | Shares the measured Python probe cap; no unclassified command site occurs on the finish path |

Every bound is below the 2-hour claim TTL. `test_every_subprocess_callsite_has_an_explicit_timeout`
checks that every `_run` caller supplies one. The Git AST inventory test pins its
18 static callsites and the dynamic path-list command forms documented above.
Timeout fixtures assert that a never-returning child is killed without a wait,
the run finishes `errored` with work not kept, no PR effect occurs, and a slow
call that finishes under its bound succeeds.
