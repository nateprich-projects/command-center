# Review-path change discipline

When a change updates `routines/muse-review.md` or the review engine, the author
replays both known-verdict packets: the The-League PR #237 must-reject packet and
the must-approve packet. The live machine-local, owner-only corpus lives in
`command-center-review-corpus/`, relative to the runtime root. Its replay use is
unchanged.

The two byte-for-byte v1 snapshots in `data/review_packets/v1/` are the
ticketed, versioned inputs shared by regression and paired evaluation. The live
owner-only corpus and replay outputs remain out of Git and issue comments; these
two snapshots are the explicit exception. Keep published versions immutable.
Packet additions or changes require a ticketed change, a new version, and a
changelog entry. The shared loader and checksum verifier are in
`engine/review_packets.py`; this dataset change adds no replay or scoring logic.

Run `review-replay` once per packet with three runs and its known verdict:

```sh
python3 review-replay "$PACKET" --runs 3 --expected-verdict "$EXPECTED_VERDICT"
```

A packet passes only when all three replay verdicts match its known verdict.
Post a verdict-only PR comment with each packet name, `N=3`, and `pass` or
`fail`. Do not attach packet contents or diffs. The corpus never enters Git or
issues. This is a posted review discipline; it adds no CI replay or mechanical
merge gate. The reviewer checks that the PR has verdicts for both packets.
