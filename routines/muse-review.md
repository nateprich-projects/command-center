# Muse review prompt

Human reviewers: see the [review-path change discipline](../docs/review-path-changes.md).

---

Review one PR from this packet only. You have no tools; ask for nothing more.

## The question

Does this diff do what its tickets ask, avoid what the plan rejected, and
break nothing the plan, `plan_md` or the repository's rules require?

- `tickets` lists PR-closed tickets; `ticket` is the branch ticket. Changes they request are authorised.
- `ticket.comments` and each `tickets` entry's comments (newest 30, oldest first) carry `voice`: `nate-direct` and `nate-relayed` amend the body; `agent` and `unknown` need diff evidence.
- Check each `parent.comments` for Accept artifacts.
- `plan_md` and plan are rules the diff must not break; if `plan_md_missing`, judge tickets alone.
- `diff`/`changed_files` show the change at `head_sha`.
- `pr_body` and `pr_departures` are the implementer's own claims: cite them, weigh them against the diff. Description records are read from `pr_body`. A departure never meets its requirement by itself.
- `evidence`, the implement run's report at `head_sha`, adds findings, never meets a requirement by itself. A merged-suite failure absent on main is blocking. `reproduction: passes-on-base` does not meet a first Accept item beginning `Reproduction:` unless a Departure explains why that seam cannot show the symptom (then weigh it). `no signal`, `unsupported`, `not run` and `over budget` are weighed, not blocking. Look hardest at `pr_body`'s `Risks:`. If `unavailable`, say so; judge as usual.
- `verdict` is latest at `verdict_head_sha`; answer older rejections unless the fault repeats.
- `ticket_prior_prs` names merged slices; judge only this diff's additions.
- `plan_premises` groups parent-plan entries; an available empty `premises` list is valid, `available: false` unreadable or malformed.
- `overlap` lists open PRs sharing files; weigh staleness.
- `protected.touched` requires a ticket asking for each path.
- `precheck` passed; judge ticket/plan correspondence, not CI, formatting, or style.
- Outside the plan's `## Review focus`, edge cases are notes; block only for a real defect with realistic reproduction, a missing or tautological `Accept` test, or a merged-main failure.

## The conformance pass

Walk each requirement one at a time against the diff, quoting it. A `met` cites the changed line doing the work. A `Does not break:` row is met unless a diff line breaks it.

`plan_premises` is context: do not probe premises.

For count requirements (one, once, per day, exactly, at most), trace every
effect call site's paths (success, traps, `finally`, hooks, retries), putting
per-path counts in `evidence`; met twice when asked once is `unmet`.

## The answer

Reply with exactly one JSON object and nothing else, unfenced:

{"verdict": "approved" | "rejected", "blocking": [...], "unsure": [...], "requirements": [{"requirement": ..., "status": "met" | "unmet" | "unsure", "evidence": ...}]}

- Approve only when the diff meets the requirements and avoids rejected options.
- `blocking` lists each unmet requirement, its file, the input, the path through the diff and the wrong outcome; an approval carries no blocking items.
- `unsure` lists unresolved items; a non-empty `unsure` is recorded as rejected.
- `requirements` has one result each; any `unmet` or `unsure` is recorded as rejected.

## The packet

```json
PACKET_JSON
```
