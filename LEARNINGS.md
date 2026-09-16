# Learnings

Durable findings only — newest first. A platform constraint hit in practice, a vendor
behaviour that contradicts its documentation, a measurement that overturned an
assumption, a debugging trap that cost real time.

Label confidence honestly: `measured` means observed with the evidence quoted,
`documented` means a vendor claims it and it was not verified, `inferred` means it could
be wrong. Mislabelling `inferred` as `measured` is how a wrong belief becomes permanent.

### Non-gating REST reads use the GitHub CLI response cache

**2026-09-16 · GitHub CLI · measured in fixture coverage**

The #655 before number remains **42 API calls and 47 measured GraphQL points** for
`funnel doctor`. The cache seam is deliberately narrower than that whole command:
advisory labels, Dependabot configuration, and the heartbeat-branch health read use
`gh api --cache 5m`; CI workflow presence and ticket-branch presence stay live because
they can affect queue eligibility or claim recovery. GraphQL, PR, verdict, and merge
reads are also live.

The fixture cache replay made two identical advisory reads with one live CLI attempt,
a **50% reduction in live REST calls**. This is a repeated-read measurement, not a
claim about GraphQL points: the CLI does not expose a per-query cost for these reads.

### Per-PR fan-out is one measured GraphQL batch

**2026-09-16 · GitHub GraphQL · measured in fixture coverage**

The #655 before number was **42 API calls and 47 measured GraphQL points**. The
per-PR reads in the funnel now use one repository-batched GraphQL document with
its own `rateLimit { cost }` field. On a 30-open-PR fixture, the old shape would
make 31 reads (one PR list plus one comment read per PR); the new shape made one,
a saving of 30 fixture calls. That is a bounded call-count measurement, not a
live point attribution; the query's returned cost is the source for its own
GraphQL spend.

### Begin Project reads keep history to the candidate set

**2026-09-16 · GitHub GraphQL · measured in fixture coverage**

The #655 before number was **42 API calls and 47 measured GraphQL points** for
`funnel doctor`. The paged Project list now omits `subIssues` and
`timelineItems`; `begin` hydrates only the candidates that remain after its
cheap checks through one batched `nodes(ids: ...)` read. In the scaling fixture,
the list grew from one to 100 Project rows while the targeted history payload
stayed at one candidate id — a 99% reduction in nested rows. This is a fixture
measurement of payload boundedness, not a live-token point attribution.

### `funnel doctor` baseline: 42 calls and 47 measured GraphQL points

**2026-09-16 · GitHub GraphQL · measured**

On local `main` at `origin/main` commit `9f1a312`, the reproducible command
`PYTHONDONTWRITEBYTECODE=1 python3 funnel.py doctor` made **42 API calls**: 15
direct GraphQL calls and 27 `gh` CLI calls. The direct GraphQL responses summed to
**47 points** from their own `rateLimit.cost` fields.

The Project load accounted for 10 direct GraphQL reads: two member-repository pages
(2 points) and eight Project item pages (40 points), plus four blocked-comment
`gh issue view` reads. The merged-PR scan made four repo-wide `gh pr list
--state merged` calls, one per member repository; `GH_DEBUG=api` observed five
underlying GraphQL HTTP responses, but `gh` does not expose their per-query point
cost. The remaining doctor checks made five direct GraphQL points and 19 more CLI
reads.

CLI point cost remains explicitly unknown: the shared-token `remaining` delta is not
a valid attribution method. This is the before number for #656–#661; the full phase
table and exact output are recorded in parent issue #518.

### Routine-SHA outcomes must keep transcription separate from drift

**2026-09-13 · Command Center / #748 · measured**

`funnel begin --routine-sha` has three distinct outcomes: an exact hash is `ok`; a
near transcription miss is `prompt-mismatch`, an informational note that does not
require resync; and a genuinely stale or otherwise different routine literal is
`prompt-drift`, which requires resync before relying on the scheduled prompt.
Automation memory must not describe a `prompt-mismatch` as known drift or advise
ignoring `prompt-drift`.

### Repository transfers can 500 repeatedly and then succeed

**2026-09-13 · GitHub API / Jeffy onboarding · measured**

`POST /repos/nateprich/jeffy-finance-agent/transfer` to `nateprich-projects` returned a
bare `HTTP 500` with an empty body four times over twenty minutes; the web UI's
transfer button showed "Something went wrong!"; `githubstatus.com` reported all systems
operational; every other endpoint for the repository answered normally; the identical
call had moved `workbench` earlier the same day. The fifth API call returned `202` and
the transfer completed within a minute. Nothing about the repository was changed between
attempts (toggling `has_projects` off and on made no difference). Treat a transfer 500 as
retryable rather than as a broken repository, and do not fall back to mirroring into a
fresh repo, which would lose issues and PRs.

### A pinned repository name makes GitHub's transfer redirect insufficient

**2026-09-13 · Jeffy runtime / PR nateprich-projects/jeffy-finance-agent#55 · measured for the pin, documented for the redirect**

Jeffy's runtime pins its own repository name and SSH remote string in six places and
refuses configuration naming anything else, so after the transfer the deployed code
crash-looped (`runtime config has invalid fixed values for: JEFFY_GITHUB_REPOSITORY`,
112 watcher restarts) until the remote and config on the Mac mini were changed as
`jeffy`. Git and `GET` API calls to the old name follow GitHub's redirect; `requests`
turns a redirected `POST` into a `GET` (documented), so issue creation would not have
survived the redirect either. With a deploy-on-merge runtime, the code change and the
host-side identity change have to land in the same five-minute window.

### Batched dependency reads fail closed at Project load

**2026-09-10 · GitHub dependencies / ticket #330 · measured**

Before `ff35327`, the per-ticket REST helper raised `GitHubError` when the
`dependencies/blocked_by` response was unreadable or malformed. After `ff35327`,
`load_items()` reads `blockedBy` from the existing batched Project query and
classifies it in memory. An unreadable or malformed Project response therefore
fails earlier and as a whole, rather than failing in one ticket's REST helper.
The helper's narrow fail-closed contract is gone with the dead functions; the
active contract is fail-closed Project loading.

### Route-local rate-limit windows are not the `/rate_limit` counter

**2026-09-10 · GitHub API / ticket #532 · measured**

One credential was used for four probes, spaced roughly a minute apart. The raw
rate-limit headers were:

| probe (response `Date`) | `X-RateLimit-Resource` | `Limit` | `Used` | `Remaining` | `Reset` |
|---|---:|---:|---:|---:|---:|
| `GET /repos/nateprich-projects/command-center` (`2026-09-10T10:43:38Z`) | `core` | 5000 | 541 | 4459 | 1789037564 |
| `GET /repos/nateprich-projects/command-center/issues/38/dependencies/blocked_by` (`2026-09-10T10:44:50Z`) | `core` | 5000 | 18 | 4982 | 1789039856 |
| GraphQL `rateLimit` query (`2026-09-10T10:46:01Z`) | `graphql` | 5000 | 2936 | 2064 | 1789038143 |
| `GET /rate_limit` (`2026-09-10T10:47:08Z`) | `core` | 5000 | 0 | 5000 | 1789040828 |

The GraphQL response also contained its own in-query reading:
`{limit: 5000, cost: 1, remaining: 2064, resetAt: "2026-09-10T11:02:23Z", used: 2936}`.
The `/rate_limit` JSON body independently reported both `core` and `graphql` as
`5000/5000` with reset `1789040828`, so it did not match either the live REST route
headers or the live GraphQL reading.

This pass saw four distinct counter snapshots/windows: the ordinary REST route, the
dependency REST route, GraphQL, and `/rate_limit`. The two REST responses both named
`core` but exposed different reset epochs and usage; the GraphQL response named
`graphql`; and `/rate_limit` exposed a fresh `core` snapshot plus a body-level GraphQL
snapshot. The measurements establish route-local disagreement, not separate
principals: the headers do not identify why the windows differ, so a split-principal
explanation remains unconfirmed.

The constraint for #237 consumers is therefore call-local rate data: use the
`X-RateLimit-*` headers returned by the REST call being evaluated, or GraphQL's own
`rateLimit` block for a GraphQL call. Never use `GET /rate_limit` as a budget or
headroom pre-check.

### A disposable Muse session removes duplicate Project loads without stale claims

**2026-09-10 · GitHub GraphQL / ticket #291 · measured (full-run total derived)**

Before the change, the heartbeat branch recorded two-command Muse paths at **46 points**
(run `e6ab9fbeedad`: 23 + 23, with 17 and 11 `gh` calls) and a three-command path at
**69 points** (run `a23b74f4fa45`: 23 + 23 + 23, with 17, 11 and 12 `gh` calls). Each
invocation cold-loaded the same Project. The corrected load measurement from #274 is
~**12 points**, so the duplicated load component was ~12 points in the two-command path
and ~24 in the three-command path.

On `ticket/291`, the session harness dispatched multiple commands and measured exactly one
lazy `load_items()` call. Its first-command reset happens before that load, and later
commands reuse the live in-memory view; no cache or snapshot is involved. Holding the
non-load work constant, the measured replay is therefore **46 → ~34 points** for two
commands and **69 → ~45 points** for three. Applied to the corrected five-invocation
Muse estimate of ~175 points, the duplicate-load component predicts **~175 → ~127
points**; that whole-run number is arithmetic from measured components, not a shared-token
remaining delta. The first load remains at command time, so a claim still reads GitHub
immediately before it writes the lock.

### If Muse took the escalated coding lane

**2026-09-10 · planning · inferred**

Nate's question: what if Muse (spark 1.3 at max effort) covered the escalated coding jobs
Codex's Sol schedules do now? Worked through on the measured prices, recorded here so it
is not re-derived.

- **Escalated is a small slice of the work.** In the Codex cycle Sol merged 12 PRs of
  ~160 — 7–8% of tickets — for 22% of Codex's week, most of it idle: 42 of 54 Sol fires
  found nothing. A worked Sol session's token profile (72k uncached in, 2.2M cached,
  11k out) priced at Muse's contributor rate is ~0.45 points; at max effort on a hard
  ticket call it 0.5–1.0. Muse has never coded a ticket, so that is an estimate.
- **Per parent it adds ~15%**: 1.53 (shaping, breakdown, 3.2 reviews) + 0.2 escalated
  tickets × ~1.5 sessions × ~0.75 ≈ **1.75 points**.
- **Where the limit moves.** On the $15 plan the pipeline stays Muse-bound at ~51
  parents (~135 PRs) a week — what it clears now. On the $50 plan Muse (~170 parents)
  stops being the limit and Codex's Luna cadence is: ~75 parents a week at 20-minute
  fires with the cascade, ~125 with #245 fixed; with Sol's 22% handed back, Luna every
  10 minutes fits the same Codex budget and reaches ~200 parents before Codex walls.
- **The gain is the idle tax, not the coding.** Dropping the four Sol schedules
  recovers ~22% of the Codex week; Muse's extra cost for the same dozen PRs is ~4
  points of its week. The system gets cheaper either way.
- **What it needs**: an implementation routine for Muse and a Luna-style clone/branch
  sandbox — `scripts/muse-review` runs `--disable-write` in an empty workspace on
  purpose — plus a plist. Shaping-sized, not a config change. Not filed; Nate asked for
  it to be recorded here for now.

### What a Muse job costs its weekly window, and the cadence that meters it

**2026-09-10 · usage · measured (prices `inferred`)**

Muse exposes no usage reading, so this is the only way to meter it: price every
session from its own snapshots and journals under `~/.local/share/muse/sessions`
(prompt, cache-read and output tokens per turn; tier from the prompt; job from the
heartbeat finish record joined on `--run`), then scale to the panel figure Nate read.
The panel read **80% at Wed 23:30 PDT from a Sat 17:00 reset**, but the schedules did
not fire until Mon 18:00, so 80 points went in **53.6 hours over 486 sessions**.
Nate confirmed on 2026-09-10 that the Muse Code Personal Plan ($15/month) meters at
the **contributor** rate ($0.10 in / $0.002 cached / $0.20 out per 1M), so the cycle is
~$2.47 and a Muse week is ~$3.09 of API compute against a $3.46/week plan price — the
plan is priced at cost. The table below is at the standard-tier weighting it was first
computed with; at contributor rates the per-run prices become review 0.28, breakdown
0.23, shaping 0.40, **empty fire 0.12** (relatively dearer, because cache reads are
near-free and the routine's uncached read dominates), and a parent costs **1.53
points**: 59 parents a week on the $15 plan at 90%, 195 on the $50 plan (3.33× the
window), $0.059 per parent on either.

| job | runs | points each | points |
|---|---|---|---|
| review, approved and merged (standard, high) | 118 | 0.28 | 32.5 |
| review, approved and merged (escalated, max) | 6 | 0.43 | 2.6 |
| review, rejected | 7 | 0.26 | 2.0 |
| breakdown | 11 | 0.23 | 2.6 |
| shaping | 11 | **0.46** | 4.9 |
| errored (mostly `gh` 429 at begin) | 31 | 0.21 | 6.5 |
| **nothing to do** | **236** | **0.10** | **24.0** |

Two facts overturned the working assumptions. **An empty Muse fire is not cheap**:
139k prompt tokens over ~5 turns to run `funnel begin` and hear stop — a tenth of a
point, as much as a worked Codex Luna run — and 236 of them took 30% of the cycle.
And **shaping is the dearest job**, 1.5× a review, because it reads the idea, the plan
history and writes the plan; the two costliest runs of the night were shapings.

The stable 8 hours (Wed 15:24–23:20, queue full) are the number to plan on: 44 jobs
in 44 standard fires — 34 reviews, 10 shapings, 1 breakdown — for 16.5 points, about
**2 points an hour**. Reviews then cost 0.32, shapings 0.46. Of the 8 reviews that did
not merge, only 2 were rejections on substance; 6 were "approved on substance, merge
refused: branch conflicting", each re-reviewed 10–40 minutes later after Codex merged
main. That is #245's cascade from the reviewer's side: ~12% of the window on second
looks at PRs already approved. The review ratio it produced, **1.31 review runs per
merged PR**, is the one used below.

**Codex, for the same picture** (see the entry below for the per-lane prices): in the
same 8 hours every PR was written in one session and none was sent back by review,
but 45 of 64 ticket sessions were re-hands of a ticket whose PR was already up — the
rejection side of the same cascade (#245, #487) plus an Investigate ticket that can
never finish (#498). Codex's cost is re-verification; Muse's is re-review.

**The planning model** (Nate's assumptions, 2026-09-10): each shaped parent needs one
shaping, one breakdown, and one review per sub-issue at the observed ratio. Over the
whole Project history — every parent that was broken down, Parked and Done included,
82 of them — the mean is **2.62 sub-issues per parent** (median 2; 28 had one, four had
6–11). So a parent costs 0.46 + 0.26 + 3.4 × 0.32 = **1.82 points and 5.4 Muse jobs**,
and the average job costs 0.335 points whatever N is — N moves throughput, not
spacing. A 90-point week is **50 parents, 269 jobs, one job every 37 minutes**: on the
two schedules, standard every 45 minutes with escalated hourly (~92 on a full queue,
~17 of it escalated's idle fires), or standard every 40 with escalated every 2 hours
(~88). Codex's share of that plan is 26–44% of its own week, so its 20-minute Luna
cadence needs no change; **Muse's window is the throughput ceiling of the whole
funnel**, at roughly 50 parents or 130 PRs a week.

Not yet acted on: the Muse plists still fire standard every 5 minutes and escalated
hourly. Also worth its own idea: `scripts/muse-review` is our shell, so the
nothing-to-do check can run *before* `muse exec` is spawned, which makes empty fires
free and lets a fast cadence back — the same thing the Codex app will not let us do.

### What a Codex session costs the Plus weekly window, by lane

**2026-09-09 · usage · measured**

From the `rate_limits` readings in `~/.codex/sessions`, the weekly window reset at
2026-09-08 04:15Z and read 95% at 2026-09-10 05:37Z: 49.4 hours, 509 sessions, all but
two of them Command Center automations. Priced at API list rates (Luna $0.20 / $0.02
cached / $1.20 out; Sol $4 / $0.40 / $20 promo, per 1M) the cycle comes to ~$80, so
**one Plus week is worth roughly $84 of API compute** and 1% of it is ~$0.84.

| lane | class | sessions | $/session | % of week / session |
|---|---|---|---|---|
| standard (`tickets-hourly`, Luna/max) | worked | 357 | $0.086 | 0.10% |
| standard | fired, nothing to pick up | 97 (21%) | $0.030 | 0.04% |
| escalated (four windows, Sol/high) | worked | 30 | $1.37 | 1.63% |
| escalated | fired, nothing to pick up | 21 (41%) | $0.19 | 0.23% |

Two things the raw token counts hid. **A worked Sol ticket costs sixteen worked Luna
tickets**, so the 30 escalated jobs took more of the week (52%) than the 357 standard
ones (38%). And **an escalated fire that finds nothing is not free**: it re-reads
~216k cached tokens at Sol's cached rate, 0.23% of the week — more than two worked
Luna tickets — and 41% of escalated fires did exactly that. Blended per fire at those
no-op rates: standard 0.088%, escalated 1.06%.

The cadence that burned the window — standard every 5 minutes, escalated every 15
inside its windows — projects to ~360% of a week, which is why 95% went in 29% of one.
Set on 2026-09-09 (Nate's call, from a 20/30/15-minute frontier): **standard every 20
minutes, escalated hourly** inside the same windows (39 window-hours a week), ~85–88%
of the week. Written straight into each `automation.toml` `rrule` with a `.bak`
beside it, the same path `scripts/sync_codex_automations.py` uses for the prompt.

Attribution assumes the Plus meter weights compute like the API price list, which is
`inferred`; the per-session token counts and the 95% endpoint are measured. The
counter also mis-reported once, 69% → 44% → 69% across an hour on 2026-09-09 16:41Z
with the same `limit_id`, so hour-by-hour rates from these readings are noisy even
though the endpoints agree.

### zcode was retired, and what an empty poll actually costs each pool

**2026-09-09 · heartbeat · measured**

Nate retired zcode on 2026-09-09 (#431) after a day on which it did work in 18 of 93
runs and was refused on the z.ai pace line in 63 — one job in its last 34 runs — while
Muse's standard schedule, unmetered, did 98 jobs in 217 runs and had taken over
standard-tier shaping (#366). Its schedule (every 15 minutes at :08, :23, :38, :53,
from 2026-09-07 02:11Z) lived only in the zcode app and is recorded in
`routines/zcode.md`'s header. The routine, its records, `PROVIDERS` and the `zai`
policy all stay; `heartbeat.RETIRED_AGENTS` is what keeps the watchdog and
`agent_health` from reading the silence as a run that died.

**The measurement below was wrong, and is corrected here (2026-09-10).** The original
read: *62 refused polls cost +1.0 point in total, 10 empty runs cost 0, 18 working runs
cost +19 points — polling was free; the jobs were expensive.* Two defects sat under it.
First, `heartbeat.usage_snapshot()` records **Codex's** meter for every agent that is
not Claude (#514), so 137 of the 241 zcode records — every one from the afternoon of
2026-09-07 — carry Codex's `resets_at`, and the "+14-point review" was Codex's 09:41
counter glitch. Second, on the 104 runs with z.ai readings at both ends, **27 of 31
points landed between runs**, in the fifteen-minute gap after a finish, and only 4
inside one: z.ai's meter lags, so within-run deltas measure almost nothing.

Re-measured with lagged attribution (a run's cost is the rise from its start reading to
the next run's start reading; the weekly cap is 10,000 credits, read whole-percent, so
100 credits of resolution — stretches, not single runs):

| stretch | fires | credits | per refused fire |
|---|---|---|---|
| Mon 00:38–09:23, Nate asleep | 36 refusals | 400 | **~11** (five-hour window: ~17) |
| Mon 10:38–Wed 10:08, his own use mixed in | 44 refusals | 1,700 | 39 |

A refused fire costs **11–17 credits**, not zero — at every fifteen minutes that is
~8,000 of the 10,000 a week on runs that did nothing, and the `zai` pace gate was
self-starving: each refusal spent what the gate was waiting to recover. The jobs were
the cheap part: 12 breakdowns totalled ~200 credits, two shapings ~100, eight empty
runs ~200. Credits meter calls, not tokens. Nate's hypothesis was 3–4 credits per
refused fire; the coefficient was low by 3–4× and the direction was right. The
retirement (#431) stands on its stated reason — Muse is cheaper and its models are
better — not on the numbers above.

The Codex half of the original paragraph (working run ~0.5% of the week, errored run the
same, refused poll ~0) was taken from Codex's own rate-limit records, which are not
subject to #514, and is superseded by the finer per-lane prices in the entry below.
`measured` for the stretch figures; `inferred` for the per-job credits, which sit at
the meter's resolution.

### The routines ran a ticket branch's funnel.py for hours, because the canonical checkout is a working tree

**2026-09-09 · Mac mini · measured**

Every routine invoked `/Users/nateprich/.claude/command-center/funnel.py`, and that
checkout was on `ticket/346` — Nate's own fix for #343, unmerged. So for the hours it
sat there, every unattended run executed code that *predated* #349 (`funnel begin`'s
claim path) while `main` had it, and `funnel queue` from that tree reported 52 startable
tickets that `main` could not have offered. The queue looked healthy *because of* the bug
#171 describes.

Two consequences worth keeping:

- **A green suite from the wrong checkout is not evidence.** `test_launchd_drift.py` run
  from that tree passed 10 tests and exercised nothing about the keeper plist, because
  the tree predated the change that added it to the parametrised set. From the run
  clone it ran 15 and actually compared the installed file.
- **The fix had to be applied through the bug.** `funnel merge` refused PR #348 with
  "project is not Building" — the exact defect #348 fixes — until `funnel claim` was run
  from the ticket branch's own `funnel.py`, which already carried the promotion.

Resolved by #205: routines now execute `~/.claude/command-center-run`, a read-only clone
that launchd fast-forwards every five minutes. Nate's tree is his again.

### A brief's real cost was double the projection, because `gh pr list` was never counted

**2026-09-09 · GitHub GraphQL · measured**

Replacing `ticket_pr_facts`'s per-ticket PR lookup with one bounded scan per repo
(#272 / #297) cut a full `funnel brief`:

| | run 1 | run 2 |
|---|---|---|
| per-ticket loop | 230 points | 188 |
| one bounded scan | 60 points | 47 |

Roughly **209 -> 54, a 74 per cent cut**. Measured as `rateLimit { remaining }`
immediately before and after each brief, on commit ff1a2cd, with the Codex schedules
paused so the delta was attributable to nothing else on the shared token.

**The projection said 110 -> 18. Both halves were about half the truth**, and the reason
is the durable part: that figure came from `rateLimit { cost }` inside the Project query,
which counts only what goes through `gh_graphql`. `ticket_pr_facts` reaches GitHub
through `gh pr list` — the `gh` CLI — and that spend is invisible to an in-query `cost`
field even though it lands on the same 5,000/hour GraphQL budget.

So: **an in-query `rateLimit { cost }` measures the query it is in, not the command.**
Any command that also shells out to `gh` is under-reported by it, and the per-run counter
added in #295 inherits exactly this blind spot — it reported an identical "17 points" for
both the before and after branches while the true costs differed fourfold.

Attributing a whole command needs a `remaining` delta across it, which is only
trustworthy when nothing else is spending on the token. That makes it a bench
measurement with the schedules paused, not something a live run can do — and it is why
the rule against differencing `remaining` still stands everywhere else.

**Also confirmed: `reviews` comes back from a PR list query**, so no per-ticket round trip
is needed to read it.

### REST `/rate_limit` and GraphQL's own `rateLimit` are different counters, not a lag

**2026-09-08, again 2026-09-09 · GitHub API · measured**

The sibling finding below records three disagreeing `core` counters. This is the sharper
case: the REST endpoint's **`graphql`** resource does not lag the real GraphQL counter, it
is unrelated to it. Measured outside any outage, with budget to spare:

```
REST /rate_limit  ->  graphql: {limit: 5000, remaining: 5000, used: 0}
GraphQL rateLimit ->  {limit: 5000, remaining: 3174, used: 1826, resetAt: 03:52:06Z}
```

REST read a flat zero while 1,826 points were demonstrably spent. Measured again on
2026-09-09 03:45Z, from the other direction: `/rate_limit` reported `graphql: 5000/5000`
across all fifteen resources while the same token's next GraphQL call returned
`X-Ratelimit-Remaining: 0`, `X-Ratelimit-Used: 5012` — **`used` above `limit`** — and the
two disagreed on reset time by an interval. One reading is an anomaly; two, in opposite
directions, is a property.

**So `funnel doctor` can never source this from REST at all**, not even as a cheap
pre-check. The measurement has to come from inside the GraphQL response. Note the trap for
whoever reads the entry below and reaches for `-i` response headers: that works for a REST
route, but for GraphQL only the in-query `rateLimit { cost remaining resetAt }` gives a
per-query **cost**, and cost is the half you need to attribute spend.

**Never infer cost by differencing `remaining` between calls.** The token is shared — the
1,826 points above belonged to something other than the measuring session — so a delta
attributes other consumers' spend to the funnel.

### `rateLimit` appears to be a free field, but this is not established

**2026-09-08 · GitHub GraphQL · documented**

Three consecutive `rateLimit`-only queries showed `used` flat across the first two and up
by one on the third. On a shared token that is consistent with the field being free and the
increment belonging to another consumer — but it does not establish it.

Labelled `documented` deliberately, not `measured`. Upgrading it needs an isolated token,
which is a credential outside the agent capability boundary. Do not upgrade the wording
without one.

### `gh api rate_limit` can report full headroom while a route is fully exhausted

**2026-09-08 · GitHub API · measured**

Three calls on the same `gh` credential, seconds apart, reported three different `core`
counters — same 5,000 limit, three different resets:

| call | used | remaining | resets |
|---|---|---|---|
| `repos/nateprich-projects/command-center` | 684 / 5000 | 4316 | 13:45:29 PDT |
| `.../issues/38/dependencies/blocked_by` | 5000 / 5000 | 0 | 13:53:22 PDT |
| `gh api rate_limit` | 0 / 5000 | 5000 | 14:38:33 PDT |

All three carried `X-Ratelimit-Resource: core`. So **`gh api rate_limit` is not a reliable
read of the budget a given route is actually spending** — it answered "full" while the
route the funnel depended on was at zero and `funnel brief` was failing closed.

Read the reset and remaining from the *failing call's own response headers* (`gh api ... -i`),
not from the `rate_limit` endpoint.

Why there are several counters is **inferred, not measured**: GitHub windows are
per-credential and start on first use, so different windows imply the calls are counted
against different principals. Nobody has verified that. #239 tracks measuring it.

### GitHub GraphQL exposes `Issue.blockedBy`, so native dependencies need no REST call

**2026-09-08 · GitHub API · measured**

The native issue-dependency data is reachable from GraphQL as `Issue.blockedBy`, an
`IssueConnection` with the usual pagination args. It returns `state`, `stateReason` and
`repository { nameWithOwner }`, which is everything
`repos/{repo}/issues/{n}/dependencies/blocked_by` returns that the funnel uses — including
cross-repository blockers.

That matters because it can be selected inside a Project items query that is already
running, turning one REST call per open ticket into zero extra calls. Measured after
ff35327: 182 items load with 10 REST calls, down from ~88, with identical results.

**The two spellings disagree and silently produce wrong answers if you assume one.** REST
returns `state: "open"`, `state_reason`, `full_name`; GraphQL returns `state: "OPEN"`,
`stateReason`, `nameWithOwner`. Case and key both differ, and a missing `nameWithOwner`
fallback quietly attributes a cross-repo blocker to the wrong repo rather than failing.

### zcode reports `computer_asleep_or_app_not_running` for skips that are neither

**2026-09-06 · zcode · measured**

A scheduled run at 19:24 was recorded as `dispatch: skipped` with
`error: computer_asleep_or_app_not_running`. Neither held.

`pmset -g custom` shows `sleep 0` — the Mac is configured never to sleep — and
`pmset -g log` shows no sleep or wake events all day, only "Display is turned off/on".
The display was still on at 19:24; it did not go off until 19:30. ZCode's own process had
been up since 17:38.

The likeliest real cause is that the automation was **paused** at that moment. The point
is not the cause but the label: it names two conditions, both checkable, and both false —
so believing it sends you to power settings for a scheduling problem. Check `pmset -g log`
before acting on it.

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

**Confirmed with timestamps, 2026-09-06 21:01.** Drift forensics added to
`--check` record the file's mtime at the moment a mismatch is found. They caught a write
at 21:01:51 — twelve minutes after the last sync, on a Sunday evening, to a schedule that
only fires on weekday mornings. Nothing in `~/.codex/automations`, `.codex-global-state.json`,
`goals_1.sqlite` or `logs_2.sqlite` holds a second copy of the prompt, so `automation.toml`
*is* the store and the stale text came from the app's own memory being flushed over it.

An earlier reading of `updated_at` seemed to show no app write. That was wrong: the field
had been overwritten by the sync itself, so the evidence of the app's write was destroyed
by the act of repairing it. Measuring at the moment of detection is what settled it.

**Practical: quit the Codex app, sync, then relaunch.** While it is running with an
automation loaded, external edits to that file are unreliable — the app periodically
writes its in-memory copy over whatever is on disk. Re-syncing works but only until the
next flush.

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

**A second constraint falls out of this.** ~~The real target is on `/Volumes/External
SSD`, so the entire system — funnel, heartbeat, usage, skills, and both agents' working
copies — depends on an external volume being mounted.~~ **Superseded 2026-09-07:** the
checkout was moved onto the internal disk and `~/.claude/command-center` is now a real
directory rather than a symlink, so neither the mount dependency nor the
symlink-vs-real-target problem above applies any more. The reasoning is kept because it
explains why twenty occurrences of that path exist and why they were correct at the time.
See "A launchd job cannot read an external volume" below for what forced the move.

### The Claude routine was retired, and its schedules recorded

**2026-09-07 · scheduled-tasks · documented**

**Claude's three scheduled tasks were deleted** on Nate's instruction, once Muse took
both review tiers. The routine did escalated review and nothing else, and it had been
refused on budget continuously since 2026-09-06 10:40 — its pool is the one Nate
competes with, which is why the work moved.

The `SKILL.md` files remain at `~/.claude/scheduled-tasks/<id>/SKILL.md`; only the
schedules were removed. Recorded here because the cron expressions are not in the repo
and would otherwise have to be reconstructed from memory:

| task | cron | meaning |
|---|---|---|
| `command-center-claude-nights` | `0 22,23 * * 0-4` | 10pm and 11pm, Sun–Thu |
| `command-center-claude-weekdays` | `0 0,1,9,10,11 * * 1-5` | midnight, 1am, 9am, 10am, 11am, Mon–Fri |
| `command-center-claude-weekend-early` | `0 2,3 * * 0,6` | 2am and 3am, Sat/Sun |

**What this leaves.** Codex engineers on five app schedules; Muse reviews both tiers on
two launchd schedules; zcode reviews standard and breaks plans down, when its budget
allows. No Claude routine runs unattended at all — Claude is now only what Nate talks to.

### A launchd job cannot read an external volume

**2026-09-07 · macOS TCC · measured**

**A LaunchAgent gets no access to `/Volumes/External SSD`, and the failure is silent.**
Probed with a job on the internal disk: reading a file, running a script, and even `ls`
on the repository all returned `Operation not permitted`. The scheduled Muse reviewer
failed every interval with exit 126 and an empty log — nothing said *why*, and the error
reads as a broken script rather than an absent permission.

**Interactive sessions were never affected.** A process inherits the privacy grants of
whatever launched it. Terminal has them, so `muse` by hand read the same files fine. A
LaunchAgent has no parent to inherit from and cannot answer the prompt that would grant
access, so it gets nothing.

**This is why Codex and zcode never met it.** They are scheduled inside their own
applications, which hold grants Nate gave interactively. The never-headless rule in
`AGENTS.md` — written about vendor terms — had a second, accidental benefit nobody had
noticed: in-app scheduling inherits permissions that launchd does not.

**Two probes, and the first one lied.** An earlier check using `[ -r path ]` reported the
volume readable. `[ -r ]` answers from metadata without opening the file. Only a real
`head -c` showed the denial. A permission probe that does not perform the operation is
not a probe.

**Resolved by moving the checkout**, not by granting permission. `~/.claude/command-center`
is now a real directory on the internal disk rather than a symlink to the volume, and the
five other symlinks that pointed into the volume were repointed at that canonical path.
The alternative — Full Disk Access for `/bin/bash` — would have given every shell script
on the machine full disk access permanently, to save moving 6.6 MB.

**Member repositories did not have to move and never will.** Engineers clone fresh each
run (`gh repo clone`), reviewers read pull requests through the API, and adopting a repo
means applying a GitHub topic rather than putting anything on this machine. The constraint
touches the tooling only — as long as headless work stays API-shaped, which is already the
design.

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
