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

## Candidate wording and existing packet facts

The candidate wording was recovered from the reverted PR #2060 source using
`git show be86e2524:routines/muse-review.md` and
`git show be86e2524:scripts/muse-review-engine`. These are the exact source
sentences carried into the versioned variants:

```text
If `evidence` says the diff rewrites fix #N, confirm it still prevents that fix's failure; cite its test.
A `test_weakening` entry no ticket or Departure authorises is blocking.
So is `passes-on-base` on any other ticket.
If `evidence` has a `rewrites prior fix: #N` line, confirm the diff still prevents that fix's failure and cite its test; mark unmet each assigned requirement it bears on when it does not.
A deleted, skipped or weakened test in `test_weakening` that no ticket or Departure authorises is blocking: mark unmet each assigned requirement it bears on.
On any other ticket `reproduction: passes-on-base` is weighed, not blocking.
```

`tests/test_review_prompts.py::test_each_variant_carries_only_its_recovered_rule`
pins which rule each variant carries. Its source check,
`test_variant_rules_match_the_reverted_pr_2060_source`, compares every stored
question and judge rule directly with those files at `be86e2524`.
The excerpts above preserve the source wording and punctuation; the test
normalizes only source line wrapping before comparing them with JSON strings.
The source check starts from `git show` of the reverted source. Every checkout
compares the variants with the recovered exact sentences; a full clone also
compares them with the original files. A shallow checkout therefore runs the
wording check instead of treating a skip as confirmation.

The packet facts those rules use were already present, so this ticket leaves the
packet builder unchanged. #1850 prior-fix evidence passes through
`engine.review.packet_evidence` into `build_packet`'s `evidence`; #1851's
`test_weakening` field is assembled from the PR diff in `engine.review.build_packet`.
Existing coverage is in `tests/test_engine_review_packet.py`, including
`test_the_block_finish_writes_is_the_block_the_packet_reads`,
`test_the_packet_carries_either_form_of_a_prior_fix_line`,
`test_packet_lists_deleted_test_functions`,
`test_packet_lists_removed_assert_lines_from_test_files`,
`test_packet_lists_added_skip_xfail_markers_and_pytest_skip_calls`, and
`test_packet_carries_every_field`. This ticket adds
`test_reviewer_rules_receive_existing_1850_1851_packet_facts`, which assembles
both rule inputs in one packet without changing the packet builder.
