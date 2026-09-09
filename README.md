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

## Documentation

- [`plan.md`](plan.md) — the design record, including what was rejected and why
- [`AGENTS.md`](AGENTS.md) — instructions for any AI assistant working here
- [`STATUS.md`](STATUS.md) — where things actually stand
