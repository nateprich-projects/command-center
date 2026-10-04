# Versioned review packet sets

`v1/` freezes the current representative must-reject and must-approve packets as
byte-for-byte snapshots. `engine.review_packets` is the shared loader and
checksum verifier for regression and paired evaluation callers.

Keep the owner-only live corpus and replay outputs out of Git and issue
comments. The two named v1 snapshots are the explicit, ticketed exception.
Do not edit a published version in place. Add a new version directory, update
the loader only as needed, and record every packet addition, removal, or change
in this changelog as part of a ticketed change.
