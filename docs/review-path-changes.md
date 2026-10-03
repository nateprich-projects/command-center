# Review-path change discipline

When a change updates `routines/muse-review.md` or the review engine, the author
replays both known-verdict packets: the The-League PR #237 must-reject packet and
the must-approve packet. The machine-local, owner-only corpus lives in
`command-center-review-corpus/`, relative to the runtime root.

Run `review-replay` once per packet with three runs and its known verdict:

```sh
python3 review-replay "$PACKET" --runs 3 --expected-verdict "$EXPECTED_VERDICT"
```

A packet passes only when all three replay verdicts match its known verdict.
Post a verdict-only PR comment with each packet name, `N=3`, and `pass` or
`fail`. Do not attach packet contents or diffs. The corpus never enters Git or
issues. This is a posted review discipline; it adds no CI replay or mechanical
merge gate. The reviewer checks that the PR has verdicts for both packets.

## Versioned reviewer trial variants

`engine/review_variants/` holds the versioned baseline, R1, R2 and R4
prompt rules. The manifest fixes Round 1 to baseline plus each single rule and
Round 2 to baseline plus at most one Round 1 winner selected under #2072's
method. The active selector stays on baseline while `trial_enabled` is false;
the review engine and replay command use the same loader for both the reviewer
question and judge guidance.

After a trial, restore the pre-trial prompt and check both known verdicts with
the owner-local corpus:

```sh
python3 review-variant restore --must-reject "$MUST_REJECT_PACKET" --must-approve "$MUST_APPROVE_PACKET"
```

This sets the active variant to baseline, disables trial selection, and replays
each packet three times. It prints verdicts and pass/fail only; packet paths and
contents stay local. Post the two verdict-only results to the trial's PR.
