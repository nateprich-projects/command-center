import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import paired_trial


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


def _reference(pair_id, head, *, kind, pr, sample=None):
    return {
        "pair_id": pair_id,
        "repo": "owner/repo",
        "pr": pr,
        "head_sha": head,
        "kind": kind,
        "sample_name": sample,
    }


def _comments_by_pr(*notes):
    return {
        ("owner/repo", 1): [{"body": note} for note in notes],
    }


def test_aggregator_counts_same_head_notes_and_ignores_a_cross_sha_pair():
    fixtures = [
        (_reference("bad-catch", _head("a"), kind="bad", pr=1,
                    sample="bad_01"),
         _note("bad-catch", _head("a"), kind="bad", sample="bad_01",
               a="approved", b="rejected", cost="0.125000", latency="1.250")),
        (_reference("bad-overlap", _head("b"), kind="bad", pr=1,
                    sample="bad_02"),
         _note("bad-overlap", _head("b"), kind="bad", sample="bad_02",
               a="rejected", b="rejected", cost="0.050000", latency="2.500")),
        (_reference("bad-correlation", _head("c"), kind="bad", pr=1,
                    sample="bad_03"),
         _note("bad-correlation", _head("c"), kind="bad", sample="bad_03",
               a="approved", b="approved", cost="0.025000", latency="0.750")),
        (_reference("good-false-block", _head("d"), kind="good", pr=1,
                    sample="good_01"),
         _note("good-false-block", _head("d"), kind="good", sample="good_01",
               a="approved", b="rejected", cost="0.100000", latency="3.000")),
        (_reference("stale-pair", _head("e"), kind="bad", pr=1,
                    sample="bad_04"),
         _note("stale-pair", _head("f"), kind="bad", sample="bad_04",
               a="approved", b="rejected", cost="9.000000", latency="99.000")),
    ]
    references = [reference for reference, _ in fixtures]
    comments = _comments_by_pr(*(note for _, note in fixtures))

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
