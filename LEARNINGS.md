# Learnings

Durable findings only — newest first. A platform constraint hit in practice, a vendor
behaviour that contradicts its documentation, a measurement that overturned an
assumption, a debugging trap that cost real time.

Label confidence honestly: `measured` means observed with the evidence quoted,
`documented` means a vendor claims it and it was not verified, `inferred` means it could
be wrong. Mislabelling `inferred` as `measured` is how a wrong belief becomes permanent.

### Editing a Codex automation in the app can write back a stale prompt

**2026-09-06 · Codex desktop · measured**

**Changing a scheduled task's model in the Codex app reverted its prompt to the version
the app had loaded earlier**, discarding an out-of-band edit made minutes before.

Observed: `scripts/sync_codex_automations.py` wrote a new prompt to all five Command
Center automations. Nate then changed the model on four of them in the app. Three kept the
new prompt; `command-center-tickets-weekday-mornings` came back carrying the *previous*
wording of one section. Nothing else in that file changed.

So the app holds its own copy of the prompt and writes the whole automation on save. Any
edit made outside it — by the sync script, or by hand — is discarded if the app saves a
version loaded before that edit. **Only one of four reverted**, so it is not every save.

**It then reverted a second time**, same automation and same section, minutes after
being re-synced. So this is not a one-off race: the app appears to hold a live copy of
whichever automation is open in its editor and write it back periodically, overwriting
anything changed underneath. The mechanism is still `inferred`; the repeat is measured.

Practical: close the automation in the app before syncing, or expect to re-sync after.

**Practical rule: sync *after* editing automations in the app, never before.** And the
drift test is what makes this survivable — `tests/test_automation_drift.py` caught this
within minutes, on a change nobody would have noticed by eye, in a file only the running
schedules read.

### z.ai remaps model ids server-side, and a Claude family name lands on Flash

**2026-09-06 · z.ai Anthropic endpoint · measured**

**The model id you send is not the model that answers.** Probed directly against
`https://api.z.ai/api/anthropic/v1/messages`, reading the `model` field of each reply:

```
requested glm-4.7            ->  glm-5.3-flash
requested glm-5.1            ->  glm-5.3
requested glm-4.5-air        ->  glm-5.3-flash
requested glm-5.3            ->  glm-5.3
requested glm-5.3-flash      ->  glm-5.3-flash
requested claude-sonnet-4-5  ->  glm-5.3-flash
requested claude-opus-4-1    ->  glm-5.3-flash
requested glm-5.3[1m]        ->  ERROR [1214][modelCode: does not exist]
```

**Two things that matter for Command Center.**

`glm-5.3[1m]` **does not exist.** A handoff recommended it as the value for
`ANTHROPIC_DEFAULT_OPUS_MODEL` / `SONNET_MODEL`, and `scripts/claude-glm` shipped with
it before this was measured. The `[1m]` suffix is not part of a model id at this
endpoint.

**Anything that looks like a Claude model silently becomes Flash.** Both
`claude-opus-4-1` and `claude-sonnet-4-5` resolved to `glm-5.3-flash`. So a harness that
sends Claude family names — which is the default shape of the Anthropic protocol — gets
the cheap model for every tier, including work you believe is running on the full one.
Flash is roughly a third the credit cost (input 2.3 vs 6.9, output 8 vs 24), which is
fine when chosen and misleading when not. **Name the models explicitly.**

`glm-5.1` also resolves to full `glm-5.3`, so a mapping written against the older ids
still reaches the current model — by remapping, not because those models are present.

**How this was found:** the account's first key failed with 401 "token expired or
incorrect" on every model. The stored value was 32 characters, alphanumeric, no dot; a
working key is 49 characters containing a dot. The short value is the key's
**identifier**, copied from the list view rather than the secret shown once at creation.
A 401 that is identical across every model and both auth schemes is a credential-shape
problem, not a model or plan problem — a plan that does not cover the endpoint returns
403.

### Codex rejects a symlinked writable root, and the fix must not touch the command paths

**2026-09-06 · Codex desktop · documented**

**Codex refuses to start a session whose writable root is a symlink — it must be given the
link's real target.** Reported by Codex itself while its sandbox settings were being
corrected, not verified independently, hence `documented`.

That matters here because `~/.claude/command-center` **is** a symlink:

```
/Users/nateprich/.claude/command-center -> /Volumes/External SSD/Repositories/nateprich-projects_command-center
```

**The trap is the obvious fix.** Rewriting the routines to invoke the real target instead
would break the Claude side, because a Claude Code permission rule matches the literal
command string:

```
"Bash(python3 /Users/nateprich/.claude/command-center/*)"
```

A command beginning `/Volumes/External SSD/...` does not match it, so every heartbeat,
gate and funnel call in the Claude routine would prompt. That is the prompt storm that
produced `RUN=$(...)`'s removal and, downstream, the clobbered-pointer bug in #26.

**These are two different settings and only one changes.** The *writable root* is Codex
sandbox configuration and must be the real path. The *command strings* in the routines
stay on the symlink path, which both agents can still execute through. Twenty occurrences
of the symlink path exist across `routines/`, `skills/` and `.claude/settings.json`; none
of them should move.

**A second constraint falls out of this.** The real target is on `/Volumes/External SSD`,
so the entire system — funnel, heartbeat, usage, skills, and both agents' working copies —
depends on an external volume being mounted. Unattended overnight runs are exactly when a
volume is most likely to be asleep or unmounted, and the failure would look like a dead
agent rather than a missing disk.

### A Claude Code web session cannot run `funnel.py` at all

**2026-09-06 · Claude Code on the web · measured**

**The cloud row in `STATUS.md` asked whether the environment supplies a token with
`project` scope. It never gets that far — the human surface fails two steps earlier,
and neither failure is fixable from inside the session.**

First, there is no `gh` on the box:

```
$ python3 funnel.py brief
  File "funnel.py", line 504, in gh_graphql
    proc = subprocess.run(cmd, capture_output=True, text=True)
FileNotFoundError: [Errno 2] No such file or directory: 'gh'
$ command -v gh
gh: not found
```

The session's stated GitHub integration is the GitHub MCP server (`mcp__github__*`), not
the CLI. Every `gh` call in `funnel.py` dies at exec, so `brief`, `ideas`, `show`,
`capture`, `shaped` and the three gate answers are all equally dead.

Second — and this is the part a shim cannot route around — `GH_TOKEN` *is* set in the
environment, but the session's outbound proxy refuses arbitrary GraphQL:

```
$ curl -sS -X POST https://api.github.com/graphql \
    -H "Authorization: bearer $GH_TOKEN" -d '{"query":"{viewer{login}}"}'
403
{"message":"This GraphQL query is not enabled for this session — only the pinned set
of PR-review operations is served. Use REST via `gh api repos/{owner}/{repo}/...`
instead."}
```

REST does work (`mcp__github__get_me` returns `nateprich`). But **ProjectV2 is
GraphQL-only — it has no REST surface**, and `funnel.py` reads the board for everything:
`member_repos()`, `load_items()`, status, `Class`, the lock field, time-at-gate. The
proxy's suggested REST fallback does not exist for the data the funnel is made of.

Third, even setting both aside, GitHub access in a web session is scoped to an explicit
repo list — this one was `nateprich-projects/command-center` alone — while funnel
membership is *every* repo carrying the `command-center` topic across two owners. A
scoped session cannot see the funnel by construction, which is a direct conflict with
"repos opt in by carrying the topic, never an allowlist".

**So: skills and code travel with the checkout, and neither is the thing that was
missing.** Writing the human surface against `gh`/GraphQL is what makes it local-only.
Portability to cloud is not a token-scope fix; it would mean a second data path through
REST that ProjectV2 does not offer. Treat the human surface as local-by-nature, the same
way `usage.py gate`, `heartbeat` and `prior_run` already are.

### Codex desktop gives every session a fresh working directory

**2026-09-05 · Codex desktop · measured**

**A run's local work is not inherited by the next run. Push as you go, or the work is
effectively gone.** Codex desktop clones into `~/Documents/Codex/<date>/<slug>`, where the
slug is derived from the prompt, and a new session gets a new directory:

```
Codex Desktop   /Users/nateprich/Documents/Codex/2026-09-05/wh
Codex Desktop   /Users/nateprich/Documents/Codex/2026-09-05/ple
Codex Desktop   /Users/nateprich/Documents/Codex/2026-09-02/can
```

So a run killed mid-work leaves its uncommitted changes somewhere a resuming run will
never look. Committing and pushing after each meaningful step is what makes the work
survive, and it is why the branch name must be derivable from the ticket rather than
recorded anywhere.

The directories do persist, so stranded work is *rescuable* if you know where to look —
which is why `prior_run.py` reports the dead session's `cwd` along with how many
uncommitted files and unpushed commits are sitting in it.

The full transcript also survives, in `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`:
`session_meta`, every message, reasoning, and tool call with its output. That is what
makes a dead run's *intent* recoverable, which no diff can carry.

### Codex acts as the account holder, so assignment cannot identify it

**2026-09-05 · Codex desktop · measured**

**No GitHub write can distinguish a Codex run from Nate working by hand.** Codex desktop
shells out to the `gh` CLI using Nate's own credentials. It is not a GitHub App, has no
bot identity, and leaves no originating-app marker.

Measured with a deliberate probe (`command-center#10`), which asked Codex to assign the
issue, comment, and open a PR. Every write came back the same:

```
assigned   actor=nateprich  assignee=nateprich  app=none
comment    author=nateprich type=User           app=none
PR #11     author=nateprich branch=probe/identity-check
  commit   author=nateprich  Nate Rich <nateprich@outlook.com>
```

Codex's own comment: *"Acting via Codex desktop using the GitHub CLI, authenticated as
@nateprich."*

**A second GitHub account does not help**, because Codex is authorised by Nate's account
and can only ever act as it.

The observational method is sound — `github-project-automation[bot]` does appear as a
distinct actor in the same data, so a bot identity would have shown up. The prior history
was simply silent on Codex: 709 issue events and 197 PRs across these repos are all
`actor=nateprich`, which is equally consistent with "acts as the user" and "has never run
here". Only the probe separates those.

Consequence: the single-in-motion lock cannot be "an issue assigned to the agent". It
needs a marker only the agent writes.

## jq

### `// empty` inside object construction discards the whole object

**2026-09-05 · jq 1.7.1 · measured**

**Build the object, then filter — never `{key: (x // empty)}`.** In jq, `empty` is an
empty *stream*, not a missing value, so an `empty` anywhere in object construction makes
the entire object produce no output.

This bites precisely where the Claude Code status-line docs recommend `// empty`. That
advice is correct for reading a scalar (`jq -r '.rate_limits.seven_day.used_percentage //
empty'`) and wrong for assembling one, and the two look identical:

```
$ echo '{"rate_limits":{"seven_day":{"used_percentage":41}}}' \
    | jq '{five_hour: (.rate_limits.five_hour // empty), seven_day: .rate_limits.seven_day}'
                                                    # → no output at all
```

In `statusline.sh` that wrote a **zero-byte cache** whenever exactly one rate-limit window
was present — which is the normal state after a window expires, not an edge case. The
render still looked perfect, so only a test that read the file back caught it.

Use instead:

```
jq '{five_hour: .rate_limits.five_hour, seven_day: .rate_limits.seven_day}
    | with_entries(select(.value != null))'
```

## bash

### Tab as `IFS` silently collapses empty fields

**2026-09-05 · bash 3.2 / 5.x · measured**

**Never split positional fields on a tab. Use a non-whitespace separator such as US
(`0x1f`).** Space, tab and newline are *IFS whitespace*: bash treats runs of them as one
delimiter and discards leading ones. So a record with an empty field silently loses it and
every later value shifts left.

`statusline.sh` read six values out of one `jq ... | @tsv` call. With `five_hour` absent —
the normal state once that window resets — the two empty fields collapsed and the
**seven-day percentage rendered under the `5h` label**. The line looked entirely
plausible; it just reported the wrong window, in the direction that gets the account
locked out.

```
$ printf 'a\t\t\tb' | { IFS=$'\t' read -r one two three four; echo "[$one][$two][$three][$four]"; }
[a][b][][]          # wanted [a][][][b]

$ printf 'a\037\037\037b' | { IFS=$'\037' read -r one two three four; echo "[$one][$two][$three][$four]"; }
[a][][][b]          # correct
```

jq has no `@` operator for this; use `join("\u001f")` instead of `@tsv`.

## Codex

### Codex writes its rate limits into session rollout JSONL — as *used*, while the UI shows *remaining*

**2026-09-05 · Codex desktop · measured**

**There is no current-state file. Take the most recent `rate_limits` record across
`~/.codex/sessions/*/*/*/*.jsonl`.** The Codex desktop app records them mid-session, in the
same rollout files the CLI writes:

```json
"rate_limits": {"limit_id": "codex",
  "primary":   {"used_percent": 14.0, "window_minutes": 300,   "resets_at": 1788596742},
  "secondary": {"used_percent": 67.0, "window_minutes": 10080, "resets_at": 1788752667},
  "credits":   {"has_credits": false, "unlimited": false, "balance": "0"}}
```

`primary` is the 5-hour window and `secondary` the 7-day one — but **match on
`window_minutes`, not on the key name**, since primary/secondary are positional and a
vendor may renumber them.

**The field is `used_percent`; the ChatGPT UI shows usage *remaining*.** Verified against
the app's own panel at the same moment: `used_percent` 14.0 / 67.0 displayed as "5h 85%,
Weekly 33%", with both `resets_at` values matching the times shown to the minute. Reading
one as the other inverts the budget and would let a run proceed exactly when it must not.

Two further details:

- This maps onto Claude's `rate_limits` one-for-one, under different names:
  `five_hour`/`seven_day` and `used_percentage` there, `primary`/`secondary` and
  `used_percent` here. Both carry `resets_at` as epoch seconds.
- Like Claude's, the value is only written **by a running session**, so a reading taken
  before the session does any work is stale — and stale always reads *low*.

## GitHub

### A sub-issue joins its parent's Project automatically, with blank fields

**2026-09-05 · GitHub Projects v2 · measured**

**Do not add tickets to the Project by hand, and do not treat a blank `Status` on a child
as a missing value.** Linking an issue as a sub-issue via `addSubIssue` adds it to every
Project its parent belongs to, with all custom fields unset.

Observed: Project 2 held only `command-center#2`. After linking seven issues as its
sub-issues, the Project reported all eight, with `Status`/`Class` set only on the parent:

```
#2  Status=Building  Class=New
#3  Status=-         Class=-
#4  Status=-         Class=-      ... and so on
```

Two consequences. **A blank `Status` on a child is the normal, correct state** — the
"anything not in Ideas must carry a Class" rule has to exempt anything with a parent, or
every ticket is permanently invalid. And **a parentless item with no `Status` is a
different case entirely**: a project added and forgotten, which must still be flagged.

`subIssuesSummary.total` and Project membership both lag by a second or two after
`addSubIssue`. A read immediately afterwards can report the old count or omit the item;
it settles on its own.

### An issue does not see a Project owned by a different owner

**2026-09-05 · GitHub GraphQL · measured**

**Query the Project for its items; never query issues for their `projectItems`.** An
`Issue.projectItems` connection returns a user-owned Project only when the repo has the
same owner. For an org repo's issue in a user-owned Project it comes back **empty** — no
error, no partial result, just `[]`.

This funnel spans `nateprich` (user) and `nateprich-projects` (org) on purpose, and its
Project is user-owned, so a repo-first query would have silently reported no `Status` for
every org repo — half the system, failing plausibly.

Measured on one issue in each pairing, same token, same `project` scope:

```
org repo   → user project:  command-center#1        projectItems → []
user repo  → user project:  claude-second-brain#1   projectItems → [{project: {number: 1}}]
```

Both directions confirmed live: the org issue *is* in the Project (`Status=Ready`,
`Class=New` read back through `projectV2.items`), and its timeline carries
`ProjectV2ItemStatusChangedEvent` naming that project. Only `projectItems` is blind.

Consequence for `funnel.py`: **the Project is the query root.** Items come from
`projectV2.items`, the issue and its timeline are fetched inline through `content`, and
the `command-center` repo topic is applied as a filter afterwards rather than as the
discovery mechanism.

### The issue timeline carries timestamped Project status changes

**2026-09-05 · GitHub GraphQL · measured**

**Time-at-gate is queryable inline with the issue; no separate history call and no
`stage:*` label fallback is needed.** `ProjectV2ItemStatusChangedEvent` is a member of
the `IssueTimelineItems` union and carries `createdAt`, `previousStatus`, `status`,
`project`, `actor`, and `wasAutomated`. Both it and `ASSIGNED_EVENT` are valid
`IssueTimelineItemsItemType` filter values, so stage age and assignment-lock age come
back in the same query as the issue.

Observed on `nateprich-projects/command-center#1`, moved to `Ready` then `Building`:

```json
{"__typename": "ProjectV2ItemStatusChangedEvent", "createdAt": "2026-09-05T06:32:45Z",
 "previousStatus": "", "status": "Ready", "project": {"title": "Command Center"}}
{"__typename": "ProjectV2ItemStatusChangedEvent", "createdAt": "2026-09-05T06:32:49Z",
 "previousStatus": "Ready", "status": "Building", "project": {"title": "Command Center"}}
```

Three details that matter for `funnel.py`:

- **`previousStatus` is the empty string, not null**, on the first assignment of a Status.
- **Events carry `project`**, and an issue may belong to several Projects. Filter on it —
  time-at-gate computed across projects would be silently wrong.
- Events return in **ascending chronological order**; time-at-gate is `now - createdAt`
  of the last event whose `status` equals the item's current Status.
