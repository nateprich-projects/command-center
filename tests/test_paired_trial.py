from collections import Counter
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import paired_trial, review_packets, reviewer_b


# Independently pinned from the immutable v2 manifest at the PR #2261 merge
# commit (56cad0845f72bd09a54625e47ffa332924651790), matching the ticket's PR
# evidence table.
FIXED_CALIBRATION_PR_HEADS = {
    "bad_01": ("bad", 2191, "14c02eb93bb07ce7105daf105f0c56a86a65fd35"),
    "bad_02": ("bad", 2091, "6362f4c94c412029fda31d3fd905196af334b0a5"),
    "bad_03": ("bad", 2030, "2c085ea1debf4107f355424f4ec350a2768cfc1e"),
    "bad_04": ("bad", 2025, "20930b2f0b0e3e3d6c773a8c775a195fa7db3af0"),
    "bad_05": ("bad", 1973, "553b0b1e276a5bd69d19392b1f69189f58ee336a"),
    "bad_06": ("bad", 1958, "9ad94e935c41ecdaf1b20a65f1f50bcdd8103da6"),
    "bad_07": ("bad", 1791, "be01e5b5ceeb7e952cfbbf593ba1f8e906a9ae4f"),
    "bad_08": ("bad", 1873, "a6328d39545365a7628c05605cbe5225ccd45218"),
    "bad_09": ("bad", 1870, "f4d576d9b8cc962f2fb52426bd871f06368c154c"),
    "good_01": ("good", 1628, "d8566e254e611c2cad6797159ed8d3bd10868db5"),
    "good_02": ("good", 1614, "5b8af2be48c7d9b2fea4f01c30eb6a924be3ad3e"),
    "good_03": ("good", 1612, "b78dd4214f69aef44241ed42d4e281abbe2a48aa"),
    "good_04": ("good", 1583, "ee361bdea4cb2b4a7df0d868e06288e4b8fe0c0c"),
    "good_05": ("good", 1619, "daebc469bf57c5f8fbb23cd1bb91a382205d8266"),
    "good_06": ("good", 1580, "adffb6f203e781bd2b6f03f7f2f37e626949a62c"),
    "good_07": ("good", 1544, "6709970177e227c9cfb590375ede55c07266eef5"),
    "good_08": ("good", 1579, "6ce5e173819c8c263bc497e19fdc6ba8996d6f8e"),
    "good_09": ("good", 1573, "693f245b97213417febdce53835dbb73b5e0c358"),
}
FIXED_V1_CALIBRATION_CHECKSUMS = {
    "must_reject": (
        "d292e9981a1c7dba768c1c659e0ead270d3917d63e708826f6650e8589c521a4"),
    "must_approve": (
        "2e6c601ea1625d07fdc50075a26b1095c435a602bcd226dd0820d3feb5187eda"),
}


def _head(char):
    return char * 40


def _note(pair_id, head, *, kind, a, b, cost, latency, sample=None):
    tick = chr(96)
    label = "live" if kind == "live" else "calibration " + sample
    return "\n".join([
        "### Reviewer B shadow note ({})".format(label),
        "",
        "Nonblocking observation; this note does not approve, reject, or block merging.",
        "",
        "- Reviewer A: {}{}{}".format(tick, a, tick),
        "- Reviewer B: {}{}{}".format(tick, b, tick),
        "- Head SHA: {}{}{}".format(tick, head, tick),
        "- Reviewer B cost: ${} own-card usage delta from heartbeat records.".format(cost),
        "- Reviewer B latency: {} seconds from paired heartbeat timestamps.".format(latency),
        "",
        "<!-- reviewer-b-2250-v1 pair={} -->".format(pair_id),
    ])


def _reference(pair_id, head, *, kind, pr, sample=None,
               repo="owner/repo"):
    return {
        "pair_id": pair_id,
        "repo": repo,
        "pr": pr,
        "head_sha": head,
        "kind": kind,
        "sample_name": sample,
    }


def _comments_by_pr(*fixtures):
    comments = {}
    for reference, note in fixtures:
        comments.setdefault((reference["repo"], reference["pr"]), []).append(
            {"body": note})
    return comments


def test_v2_calibration_refs_match_the_fixed_bad_and_good_sources():
    packets = review_packets.load_packet_set("v2")

    assert set(packets) == set(reviewer_b.CALIBRATION_NAMES)
    assert set(packets) == (
        set(FIXED_CALIBRATION_PR_HEADS) |
        set(FIXED_V1_CALIBRATION_CHECKSUMS)
    )
    assert Counter(reviewer_b.CALIBRATION_SIDES.values()) == {
        "bad": 10,
        "good": 10,
    }
    for name, (side, pr, head_sha) in FIXED_CALIBRATION_PR_HEADS.items():
        packet = packets[name]
        assert reviewer_b.CALIBRATION_SIDES[name] == side
        assert packet["repo"] == "nateprich-projects/command-center"
        assert packet["pr"] == pr
        assert packet["head_sha"] == head_sha

    assert review_packets.verify_checksums("v1") == (
        FIXED_V1_CALIBRATION_CHECKSUMS)


def test_calibration_pair_reference_must_match_the_fixed_packet_head():
    repo = "nateprich-projects/command-center"
    side, pr, head_sha = FIXED_CALIBRATION_PR_HEADS["bad_01"]
    reference = _reference(
        "fixed-bad", head_sha, kind=side, pr=pr, sample="bad_01", repo=repo)
    note = _note(
        "fixed-bad", head_sha, kind=side, sample="bad_01",
        a="approved", b="rejected", cost="0.001000", latency="1.000")
    comment_reader = lambda _repo, _pr: [{"body": note}]

    assert len(paired_trial.collect_pair_notes(
        [reference], comment_reader=comment_reader)) == 1

    for field, wrong in (("repo", "nateprich-projects/other"),
                         ("pr", pr + 1),
                         ("head_sha", _head("f"))):
        mismatched = dict(reference)
        mismatched[field] = wrong
        with pytest.raises(paired_trial.PairedTrialError,
                           match="does not match fixed packet"):
            paired_trial.collect_pair_notes(
                [mismatched], comment_reader=comment_reader)


def test_aggregator_counts_same_head_notes_and_ignores_a_cross_sha_pair():
    repo = "nateprich-projects/command-center"
    fixtures = [
        (_reference("bad-catch", FIXED_CALIBRATION_PR_HEADS["bad_01"][2],
                    kind="bad", pr=2191, sample="bad_01", repo=repo),
         _note("bad-catch", FIXED_CALIBRATION_PR_HEADS["bad_01"][2],
               kind="bad", sample="bad_01",
               a="approved", b="rejected", cost="0.125000", latency="1.250")),
        (_reference("bad-overlap", FIXED_CALIBRATION_PR_HEADS["bad_02"][2],
                    kind="bad", pr=2091, sample="bad_02", repo=repo),
         _note("bad-overlap", FIXED_CALIBRATION_PR_HEADS["bad_02"][2],
               kind="bad", sample="bad_02",
               a="rejected", b="rejected", cost="0.050000", latency="2.500")),
        (_reference("bad-correlation", FIXED_CALIBRATION_PR_HEADS["bad_03"][2],
                    kind="bad", pr=2030, sample="bad_03", repo=repo),
         _note("bad-correlation", FIXED_CALIBRATION_PR_HEADS["bad_03"][2],
               kind="bad", sample="bad_03",
               a="approved", b="approved", cost="0.025000", latency="0.750")),
        (_reference("good-false-block", FIXED_CALIBRATION_PR_HEADS["good_01"][2],
                    kind="good", pr=1628, sample="good_01", repo=repo),
         _note("good-false-block", FIXED_CALIBRATION_PR_HEADS["good_01"][2],
               kind="good", sample="good_01",
               a="approved", b="rejected", cost="0.100000", latency="3.000")),
        (_reference("stale-pair", FIXED_CALIBRATION_PR_HEADS["bad_04"][2],
                    kind="bad", pr=2025, sample="bad_04", repo=repo),
         _note("stale-pair", _head("f"), kind="bad", sample="bad_04",
               a="approved", b="rejected", cost="9.000000", latency="99.000")),
    ]
    references = [reference for reference, _ in fixtures]
    comments = _comments_by_pr(*fixtures)

    pairs = paired_trial.collect_pair_notes(
        references,
        comment_reader=lambda repo, number: comments[(repo, number)],
    )
    result = paired_trial.aggregate_pairs(pairs)

    assert result["n"] == 4
    assert result["bad_n"] == 3
    assert result["good_n"] == 1
    assert result["live_n"] == 0
    assert result["bad_a_misses"] == 2
    assert result["bad_b_catches"] == 1
    assert result["bad_overlap"] == 1
    assert result["bad_correlated_misses"] == 1
    assert result["bad_joint_detection"] == 2
    assert result["good_a_false_blocks"] == 0
    assert result["good_b_false_blocks"] == 1
    assert result["good_joint_false_blocks"] == 1
    assert str(result["cost_dollars_total"]) == "0.300000"
    assert str(result["latency_seconds_total"]) == "7.500"
    assert "stale-pair" not in {row["pair_id"] for row in result["pairs"]}


def test_parent_table_updater_edits_the_single_rollup_comment_without_local_files(
        tmp_path):
    counts = paired_trial.aggregate_pairs([])
    calls = []
    old_body = "previous roll-up\n" + paired_trial.TABLE_MARKER
    at = datetime(2026, 10, 5, tzinfo=timezone.utc)

    def graphql(query, **variables):
        calls.append((query, variables))
        if "query ParentPairedTrialComments" in query:
            assert variables["owner"] == "nateprich-projects"
            assert variables["name"] == "command-center"
            assert variables["number"] == 2078
            return {
                "repository": {
                    "issue": {
                        "id": "parent-node",
                        "comments": {
                            "nodes": [{"id": "rollup-node", "body": old_body}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
            }
        if "updateIssueComment" in query:
            assert variables["id"] == "rollup-node"
            assert variables["body"].count(paired_trial.TABLE_MARKER) == 1
            assert "| Same-head paired observations (N) | 0 |" in variables["body"]
            assert "| Reviewer B cost total | $0.000000 |" in variables["body"]
            assert "| Reviewer B latency total | 0.000 seconds |" in variables["body"]
            assert "Wilson" not in variables["body"]
            assert "96%" not in variables["body"]
            return {
                "updateIssueComment": {
                    "issueComment": {"id": "rollup-node", "body": variables["body"]},
                },
            }
        raise AssertionError("unexpected GraphQL request")

    result = paired_trial.update_parent_issue_table(
        counts, run="run-123", at=at, graphql=graphql,
    )

    assert result == {"action": "updated", "comment_id": "rollup-node"}
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_parent_table_updater_creates_the_rollup_when_missing():
    counts = paired_trial.aggregate_pairs([])
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        if "query ParentPairedTrialComments" in query:
            return {
                "repository": {
                    "issue": {
                        "id": "parent-node",
                        "comments": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                    },
                },
            }
        if "addComment" in query:
            assert variables["subjectId"] == "parent-node"
            assert paired_trial.TABLE_MARKER in variables["body"]
            return {
                "addComment": {
                    "commentEdge": {"node": {"id": "new-rollup-node"}},
                },
            }
        raise AssertionError("unexpected GraphQL request")

    result = paired_trial.update_parent_issue_table(
        counts, run="run-456", at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        graphql=graphql,
    )

    assert result == {"action": "created", "comment_id": "new-rollup-node"}
    assert len(calls) == 2
