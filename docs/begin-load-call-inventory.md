# Begin load verification and call inventory

## Premise check

Before implementation, the fetched `origin/main` (`f6e645bf803ad336ad96ffc39be17140e8453c69`) was inspected. The inferred premise holds: `main()` runs the begin gates and reserve check, loads the member repositories and Project pages, then calls `cmd_begin()` in the same invocation. The review path hydrates candidate details and reads PR/branch facts before selecting work. `FunnelSession.dispatch()` invokes `main()`, and `_SessionServer.dispatch()` holds its command lock around the full dispatch, so these phases run serially in one session command.

## Inventory against the measured 33-call shape

The parent plan records one standard begin at about 33 serial `gh` calls and about 167 seconds under load average 14–18. It names the rate-limit probe, Project pages, item history, nodes, a multi-repository query, `gh issue view 780`, and branch facts. The saved measurement does not preserve exact counts per family or the pagination and candidate cardinalities. Only the approximate aggregate and family names are measured; per-family counts remain unavailable.

| Call family | Source-level shape | Count available from saved measurement |
| --- | --- | --- |
| Rate-limit probe | One standalone GraphQL query before the load; its response is now read from the first member-repository query. | 1 before; 0 standalone after |
| Member repositories | `REPO_QUERY` for each owner page. | Not separately recorded |
| Project items | `ITEM_QUERY` once per Project page. | Not separately recorded |
| Block comments | `_load_block_comment()` for each open blocked item. | Not separately recorded |
| Item details | Unique selected item IDs in batches of at most 100; each batch uses one GraphQL document. | Not separately recorded |
| Multi-repository query | Named in the observed trace; its source-level call-site mapping is not retained in the packet. | 1 observed in that trace |
| PR and branch facts | Bounded PR and branch snapshots reused by review selection. | Not separately recorded |
| `gh issue view 780` | Named in the observed trace; its specific call-site mapping is not retained in the packet. | 1 observed in that trace |

For detail batches with child-bearing items, `ITEM_DETAILS_QUERY` places timeline history and child timestamps in one document using two `nodes(ids: ...)` selections. Batches without child-bearing items use one timeline-only document so they do not request an empty child connection. The fixtures pin this document assembly and the candidate ID lists. `ITEM_QUERY` already carries `blockedBy`; `_from_node()` classifies that payload, so no separate dependency read is needed.

The historical exact family split cannot be reconstructed from the saved evidence. This table records the call shape without inventing per-family values.

## Late or partial session replies

`scripts/muse-review-engine` now logs the first 300 bytes of `BEGIN_JSON` on the invalid-JSON path. Source inspection found that a late or partial transport envelope is not forwarded as exit 0: the client decodes a complete newline-framed JSON response before writing any stdout and returns exit 2 on timeout or malformed data. A complete envelope carrying malformed `BEGIN_JSON` reaches the runner's parse check, which logs the prefix and exits 1. The server gives bounded child work a 179-second deadline under the client's 180-second transport budget; command timeout also clears captured stdout and returns exit 2. Tests cover timeout and partial-envelope handling. This is source and fixture evidence, not a live 180-second reproduction; this implementation run did not invoke `begin` again.
