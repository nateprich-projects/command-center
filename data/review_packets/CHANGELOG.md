# Review packet set changelog

## v1 — 2026-10-03

- Freeze the current must-reject packet from The-League PR #237 and the current
  must-approve packet from Command Center PR #1959.
- Record exact-file SHA-256 digests in `v1/manifest.json`.
- Future packet-set changes require a ticketed change and a new changelog entry;
  published version contents stay immutable.

## v2 — 2026-10-03

- Freeze ten must-reject and ten must-approve calibration packets. The two v1
  examples are referenced by version and SHA-256; their packet files are not
  copied, and v1 remains byte for byte unchanged.
- Select known-bad examples from the newest nine command-center merged PRs in
  fix_recurrence.py's seven-day examples whose changed lines a later
  Broken-project fix re-touched, attributed with git blame on the fix parent.
- Select known-good examples from the newest nine closed command-center ticket
  PRs in the preceding seven-day slice of the same 14-day history window, with
  no Broken-project line fix in a complete seven-day follow-up.
- Record each new PR number, exact approved head SHA, side, selection reason,
  and packet SHA-256 in v2/manifest.json. Packet payloads omit retrospective
  merge state, verdicts, review comments, post-approval comments, overlap
  history, and parent rejected excerpts for blinded review.
