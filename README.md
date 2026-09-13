# Command Center

A funnel that manages a personal project portfolio through
`Ideas → Shaped → Ready → Building → Done`, plus `Parked`.

Its purpose is **completion and disposal**, not idea capture. Idea capture already works;
the bottleneck is decision throughput, and the evidence is a backlog where exactly one
item out of 78 was ever deliberately killed.

## How it works

Repos opt into the funnel by carrying the topic `command-center`. Stage and scheduling
class live on a GitHub Project; **GitHub is the state** — there is no database.

One shared program, `funnel.py`, computes all ordering. Both agents call it; neither
ranks anything itself.

| Command | Answers |
|---|---|
| `funnel queue` | Everything, ordered |
| `funnel next` | The single next ticket to work, or nothing |
| `funnel brief` | What is waiting on a human decision, and for how long |
| `funnel doctor` | Check the local install and report actionable failures |
| `funnel park <ref> --reason "<why>"` | Stop a project and preserve the reason |
| `funnel pin <ref> [--yes]` | Pin a project within its current gate; dry-run by default |
| `funnel unpin <ref> [--yes]` | Clear a project's pin; dry-run by default |
| `funnel comment <ref> --body "<text>" --voice <voice>` | Post a comment with explicit provenance |
| `funnel comment <ref> --blocked-on N [--blocked-on M] --because "<reason>" --voice <voice>` | Post a canonical block comment |
| `python3 outcomes.py derive [--dry-run]` | Derive closed-ticket outcomes from GitHub; durable records append to `heartbeat:outcomes.jsonl` |
| `python3 outcomes.py read` | Read the durable derived outcome records |

`outcomes.py derive --dry-run --ticket N` is the read-only form for checking a
small live sample. The job scans pull requests once per repository, counts every
`ticket/<n>` PR as an attempt, and refuses to write if the evidence scan is
truncated. Missing review or check history remains unknown rather than being
reported as a successful or zero-value outcome. The intervention predicate is
documented beside its implementation and treats an unmarked Nate comment, or a
merge without a recorded Command Center approval, as human involvement.

The full-history walk also reads closed-ticket comments and repository issue
events in paginated batches, carries PR comments and closing references through
the shared PR index, and refuses an exhausted GraphQL route rather than issuing
one failing lookup per historical ticket. Its JSON summary reports both the
number appended and the top-level fields left null; a repeat walk derives the
same records but appends zero.

When a ticket has a durable heartbeat binding, the record also carries each
implementation run's detected model, effort, harness, provider, session time
and four token kinds captured on that run's heartbeat finish. Missing transcript
data stays null; new outcome derivation does not depend on local transcripts
surviving. The heartbeat quota meter is not used as a cost proxy. The
effective-dated rate table and notional dollar calculation are a separate
consumer of these observations.

## Documentation

- [`plan.md`](plan.md) — the design record, including what was rejected and why
- [`AGENTS.md`](AGENTS.md) — instructions for any AI assistant working here
- [`STATUS.md`](STATUS.md) — where things actually stand
