# Learnings

Durable findings only — newest first. A platform constraint hit in practice, a vendor
behaviour that contradicts its documentation, a measurement that overturned an
assumption, a debugging trap that cost real time.

Label confidence honestly: `measured` means observed with the evidence quoted,
`documented` means a vendor claims it and it was not verified, `inferred` means it could
be wrong. Mislabelling `inferred` as `measured` is how a wrong belief becomes permanent.

## GitHub

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
