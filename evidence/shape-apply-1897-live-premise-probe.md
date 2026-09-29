# Ticket #1897 live premise probe

Run: `f719f826233b`  
Observed: 2026-09-29 14:51:42 UTC  
Scope: read-only evidence for the two inferred premises; the reproduction used stubbed issue writes.

## Parent plan source

Read the live body of [parent issue #1752](https://github.com/nateprich-projects/command-center/issues/1752) with `gh issue view`. Its `Premises` section states:

- `shape-apply currently writes without re-checking Ideas status or sub-issues at apply time` (evidence pointer: #1752 body, “shape-apply does not re-check at apply time that item is still shapeable”).
- `A fresh GitHub re-read before write exposes current Status and sub-issue presence to gate the apply` (evidence pointer: #1752 suspected fix, “re-reads item and refuses when status moved past Ideas or it has sub-issues”).

## Premise A: current main lacks the apply-time re-read

Compared the tracked `origin/main` source at `a96997ec648c464d8e38e816932d9246df05caea` with the existing Reproduction test `tests/test_engine_shape.py::test_apply_refuses_when_fresh_status_has_left_ideas`. Temporarily loaded `origin/main:engine/shape.py` into the checkout, ran that single test, then restored `engine/shape.py` byte-for-byte from `HEAD`.

The test failed at the assertion that `gh issue edit` was never called: the baseline recorded two issue-edit calls and printed `owner/repo#42 → Ready`. This confirms the stale apply writes without the re-check. The fixture uses a fake `owner/repo#42`; no GitHub write was attempted.

## Premise B: the live Project read returns both gate fields

Called the same history-free Project loader used by the guard against the live parent item:

```python
funnel.load_project_items_by_refs(
    ["nateprich-projects/command-center#1752"],
    member_repo_names=["nateprich-projects/command-center"],
)
```

The live call returned this selected output:

```json
{
  "parent_ref": "nateprich-projects/command-center#1752",
  "status": "Building",
  "children_total": 1
}
```

The direct read confirms GitHub exposes both current Status and sub-issue count through the production Project loader. The result is a time-specific observation; tests continue to use independent fixtures.
