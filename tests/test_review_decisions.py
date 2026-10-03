from engine.review_decisions import (
    EvaluationCounts,
    RateComparison,
    decide_outcome,
    next_batch,
)


def _counts(*, must_reject, must_approve, joint_false_blocks,
            bad_changes=120, a_misses=40, b_catches=20):
    return EvaluationCounts(
        must_reject=RateComparison(*must_reject),
        must_approve_false_blocks=RateComparison(*must_approve),
        joint_false_blocks=RateComparison(*joint_false_blocks),
        bad_changes=bad_changes,
        a_misses=a_misses,
        b_catches=b_catches,
    )


def test_decide_outcome_flags_a_clearly_unacceptable_must_reject_drop():
    result = decide_outcome(_counts(
        must_reject=(40, 120, 100, 120),
        must_approve=(5, 120, 10, 120),
        joint_false_blocks=(5, 120, 10, 120),
    ))

    assert result["outcome"] == "regression"
    assert result["must_reject"]["status"] == "regression"
    assert result["merge_blocking"] is False


def test_decide_outcome_passes_when_every_adverse_margin_is_ruled_out():
    result = decide_outcome(_counts(
        must_reject=(100, 120, 90, 120),
        must_approve=(5, 120, 10, 120),
        joint_false_blocks=(5, 120, 10, 120),
        a_misses=60,
        b_catches=30,
    ))

    assert result["outcome"] == "pass"
    assert result["must_reject"]["status"] == "safe"
    assert result["must_approve_false_blocks"]["status"] == "safe"
    assert result["joint_false_blocks"]["status"] == "safe"


def test_decide_outcome_keeps_overlapping_margin_evidence_inconclusive():
    result = decide_outcome(_counts(
        must_reject=(30, 40, 30, 40),
        must_approve=(4, 40, 4, 40),
        joint_false_blocks=(4, 40, 4, 40),
        bad_changes=40,
        a_misses=20,
        b_catches=4,
    ))

    assert result["outcome"] == "inconclusive"
    assert result["merge_blocking"] is False


def test_decide_outcome_marks_joint_lift_only_when_its_interval_clears_10pp():
    result = decide_outcome(_counts(
        must_reject=(100, 120, 90, 120),
        must_approve=(5, 120, 10, 120),
        joint_false_blocks=(5, 120, 10, 120),
        a_misses=60,
        b_catches=30,
    ))

    assert result["joint_detection_lift"]["status"] == "meaningful"
    assert result["joint_detection_lift"]["b_catch_rate_among_a_misses"] == 0.5


def test_next_batch_starts_at_40_and_adds_20_until_the_cap():
    assert next_batch(0) == 40
    assert next_batch(40, 0.10) == 20
    assert next_batch(100, 0.10) == 20
    assert next_batch(120, 0.10) == 0


def test_next_batch_stops_at_registered_decline_and_futility_boundaries():
    assert next_batch(40, 0.049) == 0
    assert next_batch(40, 0.301) == 0
    assert next_batch(40, 0.05) == 20
    assert next_batch(40, 0.30) == 20
