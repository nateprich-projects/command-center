# Split review acceptance replay for #1243

Source: the completed replay comment on Command Center #1243, recorded at
2026-09-25 11:52:28 UTC after the human step in #1363 ran on the Mac mini.
The replay used a fresh clone of `origin/main` at `07eab60b4`, pinned each
packet to its historical head, cut comments, merged PRs, and CI to the original
review time, and read `plan.md` at the historical merge base. It used the
runner's lister and judge headers, routine prompt, and exact `muse exec` flags
with `muse-spark-1.3` at `max`; judges ran in parallel in groups of at most
three requirements, and `derive_judge_answer` produced the verdict.

This was a replay of the historical review, so it recorded no verdict and did
not run `begin`, `review-apply`, a heartbeat, or a quota hold.

The completed replays were reported on #1243 after human step #1363. Codex
reused that report in this run and did not call Muse again. The original
The-League rejection was checked against the source review comment at
2026-09-18 17:40 UTC; its detailed implementation evidence stays in the
private source record.

## Split implementation present in the replay base

The implementation prerequisite, #1242, had already landed as PR #1352 on
2026-09-23. It was therefore present in the replay base at `07eab60b4` and is
in the current `origin/main` base as well.

- `scripts/muse-review-engine` assembles one cached packet, runs the single
  lister phase (`list_requirements`), splits the canonical list, and runs
  `run_judge_chunk` for each slice. A second lister call is only the malformed
  answer retry.
- `engine/review.py` caps each judge chunk at three requirements and derives
  `rejected` for any unmet or unsure result; missing or malformed judge results
  become unsure rather than disappearing.
- `tests/test_muse_review_engine.py` covers one packet fetch and refusal to
  assemble it twice (`test_the_packet_is_fetched_exactly_once_whatever_the_job`,
  `test_a_second_assembly_is_refused_rather_than_silently_refetched`), as well
  as the lister phase and its parse retry (`test_the_lister_asks_for_requirements_before_the_judge_is_asked`,
  `test_a_malformed_requirement_list_retries_once_and_then_lists`).
  `test_seven_requirements_reach_three_parallel_max_judges_once_each`
  verifies every listed requirement reaches exactly one judge.
- `tests/test_engine_review_judges.py` covers chunk boundaries, all-met
  approval, unmet and unsure rejection, and failed or timed-out chunks failing
  closed.

## Command Center #1226 at `bb3d865c`

- Elapsed: **189 s**.
- Lister: **97 s**, 48 requirements.
- Judges: 16 calls, each **23–92 s**.
- Derived verdict: **rejected** — 44 met, 3 unmet, 1 unsure.
- Unmet requirements:
  1. `AGENTS.md` omits Nate's instruction, “If that policy sucks then make a good one.”
  2. `AGENTS.md` marks the policy as confirmed by Nate instead of an agent rule
     until he confirms it.
  3. Accept (c) lacks a test for the ticket's exact `$100 / 2 days / $180 → 110%`
     case; the existing test used `$130 / $120 → 105%`.
- Unsure requirement: “Depends on #1190, must land first.” The historical packet
  omitted dependency merge status; #1190 had merged as #1192 before this head.

The replay report says both original faults from the 2026-09-21 18:52 UTC
rejection returned in the same words. The extra unsure row reflects the missing
dependency status in the packet, not a fault in the diff; the derived verdict
would still reject without it.

## The-League #237 at `38df2d69`

- Elapsed: **133 s**.
- Lister: **73 s**, 18 requirements.
- Judges: 6 calls, each **27–60 s**.
- Derived verdict: **rejected** — 17 met, 1 unmet.
- Blocking list:
  - Unmet: the successful snapshot path must append exactly one daily summary
    line per date; the replay found two `daily_summary` records for the same
    date on a clean success path.
- This privacy-safe summary matches the single unmet requirement in the
  original 2026-09-18 17:40 UTC rejection. The source report's detailed private
  implementation evidence remains in The-League's review record.

Both replays completed and rejected, satisfying the acceptance outcomes in
#1233 and #1243. The durations are the total replay times, including the
parallel judge calls.

## Verification

```sh
python3 -m pytest tests/test_muse_review_engine.py tests/test_engine_review_judges.py -q
```

Result: **140 passed** in 144.01 seconds using Python 3.9.6.
