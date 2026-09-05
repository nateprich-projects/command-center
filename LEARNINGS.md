# Learnings

Durable findings only — newest first. A platform constraint hit in practice, a vendor
behaviour that contradicts its documentation, a measurement that overturned an
assumption, a debugging trap that cost real time.

Label confidence honestly: `measured` means observed with the evidence quoted,
`documented` means a vendor claims it and it was not verified, `inferred` means it could
be wrong. Mislabelling `inferred` as `measured` is how a wrong belief becomes permanent.

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
