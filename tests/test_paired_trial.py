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


def _endpoint_counts():
    return {
        "n": 50,
        "live_n": 30,
        "live_a_approved": 20,
        "live_a_rejected": 10,
        "live_b_approved": 15,
        "live_b_rejected": 15,
        "bad_n": 10,
        "good_n": 10,
        "bad_a_misses": 6,
        "bad_b_catches": 4,
        "bad_overlap": 4,
        "bad_correlated_misses": 2,
        "bad_joint_detection": 8,
        "good_a_false_blocks": 1,
        "good_b_false_blocks": 2,
        "good_joint_false_blocks": 2,
        "cost_dollars_total": "12.345678",
        "latency_seconds_total": "456.789",
    }


def _built_endpoint_report():
    return paired_trial.build_trial_report(
        _endpoint_counts(), bound_closed=True, run="run-2252",
        at=datetime(2026, 10, 5, tzinfo=timezone.utc),
    )


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


def test_default_pair_note_reader_queries_pull_request_comments(monkeypatch):
    reference = _reference(
        "live-pr-note", _head("a"), kind="live", pr=2312,
        repo="nateprich-projects/command-center")
    note = _note(
        "live-pr-note", _head("a"), kind="live", a="approved",
        b="rejected", cost="0.125000", latency="1.250")
    calls = []

    def graphql(query, **variables):
        calls.append((query, variables))
        assert "pullRequest(number: $number)" in query
        assert "issue(number: $number)" not in query
        return {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "nodes": [{"id": "pr-comment", "body": note}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                },
            },
        }

    monkeypatch.setattr(paired_trial.funnel, "gh_graphql", graphql)
    result = paired_trial.collect_pair_notes([reference])

    assert len(result) == 1
    assert result[0]["pair_id"] == "live-pr-note"
    assert calls[0][1] == {
        "owner": "nateprich-projects",
        "name": "command-center",
        "number": 2312,
    }


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
    assert result["live_a_approved"] == 0
    assert result["live_a_rejected"] == 0
    assert result["live_b_approved"] == 0
    assert result["live_b_rejected"] == 0
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


def test_endpoint_report_includes_fixed_counts_verdicts_and_wilson_intervals():
    # Static Wilson values were calculated independently from the score
    # interval equation; keep these expectations separate from the helper.
    report = _built_endpoint_report()

    assert "30 live pairs plus 10 known-bad and 10 known-good" in report
    assert "| Trial endpoint | 30 live + 10 bad + 10 good | closed |" in report
    assert "| Same-head paired observations (N) | 50 | |" in report
    assert "| Reviewer A verdicts on live pairs | 20 approved / 10 rejected | |" in report
    assert "| Reviewer B verdicts on live pairs | 15 approved / 15 rejected | |" in report
    assert "| Reviewer A verdicts on known-bad | 6 approved / 4 rejected | |" in report
    assert "| Reviewer B verdicts on known-bad | 2 approved / 8 rejected | |" in report
    assert "| Reviewer A verdicts on known-good | 9 approved / 1 rejected | |" in report
    assert "| Reviewer B verdicts on known-good | 8 approved / 2 rejected | |" in report
    assert "4/6 | 66.7%; 95% Wilson score interval 30.0% to 90.3%" in report
    assert "4/10 | 40.0%; 95% Wilson score interval 16.8% to 68.7%" in report
    assert "2/10 | 20.0%; 95% Wilson score interval 5.7% to 51.0%" in report
    assert "8/10 | 80.0%; 95% Wilson score interval 49.0% to 94.3%" in report
    assert "total $12.345678; mean $0.246914 per pair" in report
    assert "total 456.789 seconds; mean 9.136 seconds per pair" in report


def test_endpoint_report_refuses_an_open_sampling_bound():
    with pytest.raises(paired_trial.PairedTrialError,
                       match="sampling bound is still open"):
        paired_trial.build_trial_report(
            _endpoint_counts(), bound_closed=False, run="run-2252")


def test_parent_table_updater_edits_the_single_rollup_comment_without_local_files(
        tmp_path):
    report = _built_endpoint_report()
    calls = []
    old_body = "previous roll-up\n" + paired_trial.TABLE_MARKER

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
            assert "| Same-head paired observations (N) | 50 | |" in variables["body"]
            assert "95% Wilson score interval" in variables["body"]
            assert variables["body"] == report
            return {
                "updateIssueComment": {
                    "issueComment": {"id": "rollup-node", "body": variables["body"]},
                },
            }
        raise AssertionError("unexpected GraphQL request")

    result = paired_trial.update_parent_issue_table(
        report, graphql=graphql,
    )

    assert result == {"action": "updated", "comment_id": "rollup-node"}
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == []


def test_parent_table_updater_creates_the_rollup_when_missing():
    report = _built_endpoint_report()
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
            assert variables["body"] == report
            return {
                "addComment": {
                    "commentEdge": {"node": {"id": "new-rollup-node"}},
                },
            }
        raise AssertionError("unexpected GraphQL request")

    result = paired_trial.update_parent_issue_table(
        report, graphql=graphql,
    )

    assert result == {"action": "created", "comment_id": "new-rollup-node"}
    assert len(calls) == 2


def test_live_report_does_not_read_or_post_before_the_sampling_bound_closes(
        monkeypatch):
    monkeypatch.setattr(paired_trial.heartbeat, "reviewer_b_state", lambda: {
        "trial_id": reviewer_b.TRIAL_ID,
        "sampling_open": True,
        "pairs": [],
    })
    monkeypatch.setattr(
        paired_trial, "collect_pair_notes",
        lambda _references: pytest.fail("open trial must not read PR notes"),
    )
    monkeypatch.setattr(
        paired_trial, "update_parent_issue_table",
        lambda _report: pytest.fail("open trial must not post a report"),
    )

    assert paired_trial.refresh_parent_issue_table(run="run-2252") is None
