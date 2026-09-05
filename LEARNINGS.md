# Learnings

Durable findings only — newest first. A platform constraint hit in practice, a vendor
behaviour that contradicts its documentation, a measurement that overturned an
assumption, a debugging trap that cost real time.

Label confidence honestly: `measured` means observed with the evidence quoted,
`documented` means a vendor claims it and it was not verified, `inferred` means it could
be wrong. Mislabelling `inferred` as `measured` is how a wrong belief becomes permanent.

## GitHub

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
