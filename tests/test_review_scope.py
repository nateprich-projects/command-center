"""Function-grouped scope evidence with the per-PR unrequested count.

Ticket #2027, plan #1840 phase 4 (runner ticket): the diff's hunks land
under exactly one scope group each, the judge mapping is
completeness-checked the way requirement rows are, and the outcomes
record holds the count plus the judged head SHA (or a deferred reason).
"""

import json
import re

import pytest

from engine.review import (
    DIFF_LIMIT_BYTES,
    DIFF_TOO_LARGE_REASON,
    ReviewJudgeError,
    _diff_sections,
)
from engine.review_scope import (
    FILE_SCOPE_LABEL,
    REVIEW_PATH_REPLAY_COMMAND,
    REVIEW_PATH_SEEDS,
    SCOPE_JUDGE_QUESTION,
    SCOPE_NOTE_PREFIX,
    UNREQUESTED,
    ScopeMappingError,
    count_unrequested,
    parse_scope_mapping,
    render_scope_deferred_note,
    render_scope_note,
    render_scope_rows,
    scope_evidence_lines,
    scope_group_id,
    scope_groups,
    scope_note_for_mapping,
    scope_note_for_record,
    scope_skip_reason,
)
from outcomes import (
    OutcomeError,
    deferred_scope_outcome,
    derive_scope_outcome,
)


HEAD_SHA = "a" * 40
REPO = "nateprich-projects/command-center"

#: A fixture PR mixing function and non-function hunks: a body change
#: inside ``double`` (heading names it), a new ``triple`` whose
#: definition outranks the stale heading, a bare module-level change,
#: and two bare hunks in a prose file sharing one file-scope group.
DIFF = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,3 @@ def double(x):
 def double(x):
-    return x * 2
+    return x * 3
@@ -10,0 +11,3 @@ def double(x):
+def triple(x):
+    return x * 3
+
@@ -20,2 +23,3 @@
 LIMIT = 10
+EXTRA = 1
 SCALE = 2
diff --git a/notes.txt b/notes.txt
--- a/notes.txt
+++ b/notes.txt
@@ -1,2 +1,3 @@
 context
+added line
 more
@@ -8,1 +9,2 @@
 tail
+another line
"""


def test_every_hunk_lands_under_exactly_one_group():
    sections = _diff_sections(DIFF)
    expected_spans = sorted(
        [section_index, hunk_index]
        for section_index, section in enumerate(sections)
        for hunk_index in range(len(section["hunks"])))

    groups = scope_groups(DIFF)
    actual_spans = sorted(
        span for group in groups for span in group["hunks"])

    assert actual_spans == expected_spans
    assert [group["id"] for group in groups] == [
        "calc.py::double",
        "calc.py::triple",
        "calc.py::{}".format(FILE_SCOPE_LABEL),
        "notes.txt::{}".format(FILE_SCOPE_LABEL),
    ]
    assert [group["hunks"] for group in groups] == [
        [[0, 0]], [[0, 1]], [[0, 2]], [[1, 0], [1, 1]],
    ]


def test_group_rows_carry_stable_file_function_span_identity():
    groups = scope_groups(DIFF)

    assert groups[0] == {
        "id": "calc.py::double",
        "file": "calc.py",
        "function": "double",
        "hunks": [[0, 0]],
    }
    assert groups[2]["function"] is None
    assert scope_group_id("calc.py", None) == \
        "calc.py::{}".format(FILE_SCOPE_LABEL)


def test_heading_names_containing_function_and_class_defines_group():
    diff = ("diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -4,2 +4,3 @@ def helper(arg):\n"
            " body\n"
            "+more\n"
            "@@ -9,0 +10,2 @@\n"
            "+class Cache:\n"
            "+    pass\n")

    groups = scope_groups(diff)

    assert [group["id"] for group in groups] == [
        "f.py::helper", "f.py::Cache",
    ]


def test_scope_rows_render_as_evidence_fact_lines():
    rows = render_scope_rows(scope_groups(DIFF))

    assert len(rows) == 4
    assert all(row.startswith("- scope: ") for row in rows)
    assert rows[0] == "- scope: calc.py::double (hunks 0.0)"
    assert rows[3] == "- scope: notes.txt::{} (hunks 1.0, 1.1)".format(
        FILE_SCOPE_LABEL)


def _mapping_answer(ids, target="requirement one"):
    entries = [{"group": group_id, "requirement": target,
                "evidence": "diff line"}
               for group_id in ids]
    return json.dumps({"scope": entries})


def test_complete_mapping_passes_and_returns_group_order():
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    raw = _mapping_answer(list(reversed(ids)))

    entries = parse_scope_mapping(raw, groups)

    assert [entry["group"] for entry in entries] == ids


def test_unrequested_groups_are_counted_per_group():
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    entries = [
        {"group": ids[0], "requirement": "requirement one",
         "evidence": "diff line"},
        {"group": ids[1], "requirement": UNREQUESTED,
         "evidence": "nothing asks for this"},
        {"group": ids[2], "requirement": UNREQUESTED,
         "evidence": "nothing asks for this either"},
        {"group": ids[3], "requirement": "requirement two",
         "evidence": "diff line"},
    ]

    assert count_unrequested(entries) == 2
    assert count_unrequested([]) == 0


def test_dropped_group_fails_naming_it():
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    raw = _mapping_answer(ids[:-1])

    with pytest.raises(ScopeMappingError,
                       match=re.escape(ids[-1])):
        parse_scope_mapping(raw, groups)


def test_invented_group_fails_naming_it():
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    raw = _mapping_answer(ids + ["calc.py::invented"])

    with pytest.raises(ScopeMappingError,
                       match=re.escape("calc.py::invented")):
        parse_scope_mapping(raw, groups)


def test_swapped_group_fails_naming_the_dropped_one():
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    raw = _mapping_answer(ids[:-1] + ["calc.py::invented"])

    with pytest.raises(ScopeMappingError,
                       match=re.escape(ids[-1])):
        parse_scope_mapping(raw, groups)


@pytest.mark.parametrize("raw", [
    "not json",
    json.dumps({"groups": []}),
    json.dumps({"scope": "nope"}),
    json.dumps({"scope": []}),
    json.dumps({"scope": [
        {"group": "calc.py::double", "requirement": "req"},
    ]}),
    json.dumps({"scope": [
        {"group": "calc.py::double", "requirement": "req",
         "evidence": "line", "extra": 1},
    ]}),
    json.dumps({"scope": [
        {"group": "calc.py::double", "requirement": "",
         "evidence": "line"},
    ]}),
    json.dumps({"scope": [
        {"group": "calc.py::double", "requirement": "req",
         "evidence": "  "},
    ]}),
    json.dumps({"scope": [
        {"group": "calc.py::double", "requirement": "req",
         "evidence": "line"},
        {"group": "calc.py::double", "requirement": "req",
         "evidence": "line"},
    ]}),
])
def test_malformed_mappings_fail_the_evidence_check(raw):
    with pytest.raises(ScopeMappingError):
        parse_scope_mapping(raw, ["calc.py::double"])


def test_scope_mapping_error_is_not_a_review_block():
    assert issubclass(ScopeMappingError, ValueError)
    assert not issubclass(ScopeMappingError, ReviewJudgeError)


def test_scope_record_holds_count_plus_head_sha():
    record = derive_scope_outcome(REPO, 4242, HEAD_SHA, 2, 4)

    assert record["pr"] == "{}#4242".format(REPO)
    assert record["pr_number"] == 4242
    assert record["head_sha"] == HEAD_SHA
    assert record["status"] == "mapped"
    assert record["group_count"] == 4
    assert record["unrequested_count"] == 2
    assert record["reason"] is None
    assert record["schema_version"] == 1
    assert json.dumps(record)


def test_scope_record_rejects_incoherent_inputs():
    with pytest.raises(OutcomeError):
        derive_scope_outcome(REPO, 4242, HEAD_SHA, 5, 4)
    with pytest.raises(OutcomeError):
        derive_scope_outcome(REPO, 4242, HEAD_SHA[:12], 0, 0)
    with pytest.raises(OutcomeError):
        derive_scope_outcome(REPO, 0, HEAD_SHA, 0, 0)
    with pytest.raises(OutcomeError):
        derive_scope_outcome("", 4242, HEAD_SHA, 0, 0)


def test_deferred_record_holds_reason_and_no_count():
    record = deferred_scope_outcome(
        REPO, 4242, HEAD_SHA, DIFF_TOO_LARGE_REASON)

    assert record["pr"] == "{}#4242".format(REPO)
    assert record["head_sha"] == HEAD_SHA
    assert record["status"] == "deferred"
    assert record["reason"] == DIFF_TOO_LARGE_REASON
    assert record["unrequested_count"] is None
    assert record["group_count"] is None
    assert json.dumps(record)
    with pytest.raises(OutcomeError):
        deferred_scope_outcome(REPO, 4242, HEAD_SHA, "  ")


def test_skip_gate_defers_oversized_and_unmappable_diffs():
    assert scope_skip_reason("x" * (DIFF_LIMIT_BYTES + 1)) == \
        DIFF_TOO_LARGE_REASON
    assert scope_skip_reason("") == "diff unavailable: nothing to map"
    assert scope_skip_reason(None) == "diff unavailable: nothing to map"
    binary_only = ("diff --git a/logo.png b/logo.png\n"
                   "Binary files a/logo.png and b/logo.png differ\n")
    assert scope_skip_reason(binary_only) == "diff has no mappable hunks"
    assert scope_skip_reason(DIFF) is None


def test_count_and_record_agree_end_to_end():
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    raw = json.dumps({"scope": [
        {"group": ids[0], "requirement": "requirement one",
         "evidence": "implements it"},
        {"group": ids[1], "requirement": UNREQUESTED,
         "evidence": "nothing asks for this"},
        {"group": ids[2], "requirement": "requirement one",
         "evidence": "supports it"},
        {"group": ids[3], "requirement": UNREQUESTED,
         "evidence": "drive-by prose"},
    ]})

    entries = parse_scope_mapping(raw, groups)
    record = derive_scope_outcome(
        REPO, 4242, HEAD_SHA, count_unrequested(entries), len(entries))

    assert record["unrequested_count"] == 2
    assert record["group_count"] == 4


def test_skip_reason_and_deferred_record_agree():
    reason = scope_skip_reason("x" * (DIFF_LIMIT_BYTES + 1))
    assert reason is not None
    record = deferred_scope_outcome(REPO, 4242, HEAD_SHA, reason)

    assert record["status"] == "deferred"
    assert record["reason"] == reason


def test_evidence_lines_emit_one_row_per_group():
    lines = scope_evidence_lines(DIFF)

    assert lines == render_scope_rows(scope_groups(DIFF))
    assert len(lines) == 4
    assert all(line.startswith("- scope: ") for line in lines)
    assert lines[0] == "- scope: calc.py::double (hunks 0.0)"


def test_evidence_lines_defer_with_reason():
    oversized = scope_evidence_lines("x" * (DIFF_LIMIT_BYTES + 1))
    assert oversized == [
        "- scope: deferred ({})".format(DIFF_TOO_LARGE_REASON)]

    assert scope_evidence_lines("") == [
        "- scope: deferred (diff unavailable: nothing to map)"]
    assert scope_evidence_lines(None) == [
        "- scope: deferred (diff unavailable: nothing to map)"]
    binary_only = ("diff --git a/logo.png b/logo.png\n"
                   "Binary files a/logo.png and b/logo.png differ\n")
    assert scope_evidence_lines(binary_only) == [
        "- scope: deferred (diff has no mappable hunks)"]

    for lines in (oversized, scope_evidence_lines("")):
        assert len(lines) == 1
        assert json.dumps(lines)


def test_scope_rows_ride_the_runner_block_into_packet_evidence():
    """The #1783 seam: the writer's rows are the packet's facts."""
    from engine import implement
    from engine.review import packet_evidence, split_evidence_block

    block = implement.render_evidence_block(
        sha=HEAD_SHA, merged=None,
        reproduction={"line": "reproduction: not run", "tests": []},
        repo=REPO, scope_lines=scope_evidence_lines(DIFF))
    body, inner = split_evidence_block(
        "Implements #4242.\n\n" + block)
    evidence = packet_evidence(inner, HEAD_SHA)

    assert evidence != "unavailable"
    for row in scope_evidence_lines(DIFF):
        assert row in evidence.split("\n")


def test_runner_block_omits_scope_section_when_not_computed():
    from engine import implement

    block = implement.render_evidence_block(
        sha=HEAD_SHA, merged=None,
        reproduction={"line": "reproduction: not run", "tests": []},
        repo=REPO)

    assert "- scope:" not in block


def test_scope_lines_evidence_diffs_the_branch(tmp_path):
    """The runner call site: branch diff against its merge-base."""
    import pathlib
    import subprocess

    from engine import implement

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        proc = subprocess.run(
            ["git"] + list(args), cwd=str(root), capture_output=True,
            text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (root / "calc.py").write_text(
        '"""Calc helpers."""\n\nCONSTANT = 1\n\n\ndef double(x):\n'
        "    result = x * 2\n    result = result + CONSTANT\n"
        "    result = result - CONSTANT\n    result = result + 0\n"
        "    return result\n\nFILLER = [\n"
        "    1,\n    2,\n    3,\n    4,\n    5,\n    6,\n    7,\n    8,\n]\n")
    git("add", "calc.py")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "topic")
    (root / "calc.py").write_text(
        '"""Calc helpers."""\n\nCONSTANT = 1\n\n\ndef double(x):\n'
        "    result = x * 2\n    result = result + CONSTANT\n"
        "    result = result - CONSTANT\n    result = result + 0\n"
        "    return x * 3\n\nFILLER = [\n"
        "    1,\n    2,\n    3,\n    4,\n    5,\n    6,\n    7,\n    8,\n]\n\n\ndef triple(x):\n    return x * 3\n")
    git("commit", "-am", "work")
    head = git("rev-parse", "HEAD")

    lines = implement._scope_lines_evidence(
        pathlib.Path(str(root)), base, head)

    assert lines == [
        "- scope: calc.py::double (hunks 0.0)",
        "- scope: calc.py::triple (hunks 0.1)",
    ]


def test_scope_lines_evidence_names_a_missing_diff():
    import pathlib

    from engine import implement

    assert implement._scope_lines_evidence(
        pathlib.Path("/nonexistent-root"), "main", "head") == [
            "- scope: not run"]


def test_verify_first_records_both_corpus_seeds_and_the_replay_command():
    """#2028 verify-first: the replay discipline from
    docs/review-path-changes.md, recorded before the prompt change."""
    assert REVIEW_PATH_SEEDS == (
        "command-center-review-corpus/the-league-237-must-reject.json",
        "command-center-review-corpus/command-center-1959-must-approve.json",
    )
    assert "review-replay" in REVIEW_PATH_REPLAY_COMMAND
    assert "--runs 3" in REVIEW_PATH_REPLAY_COMMAND
    assert "--expected-verdict" in REVIEW_PATH_REPLAY_COMMAND


def test_scope_judge_question_is_present_and_bounded():
    """#2028 prompt builder: one mapping per scope group, exactly once,
    against ticket plan text plus parent plan, never blocking."""
    flat = " ".join(SCOPE_JUDGE_QUESTION.split())
    for sentence in (
        "Map each `- scope:` evidence row",
        "or to `unrequested`",
        "ticket plan text plus its parent plan",
        "one mapping per scope group",
        "every group exactly once",
        "no dropped groups, no invented groups",
        "malformed mapping fails the evidence check, never the review",
        "deferred outcomes entry with the reason",
        "never blocks and never enters the verdict",
        "real defect with a realistic reproduction",
        "missing or tautological Accept test",
        "merged-main failure blocks",
    ):
        assert sentence in flat, sentence


def test_scope_note_states_the_per_pr_unrequested_count():
    assert render_scope_note(2, 4) == "Scope: 2 unrequested of 4 groups"
    assert render_scope_note(0, 3) == "Scope: 0 unrequested of 3 groups"
    assert render_scope_note(0, 1) == "Scope: 0 unrequested of 1 group"
    assert render_scope_note(1, 1) == "Scope: 1 unrequested of 1 group"
    assert render_scope_note(2, 4).startswith(SCOPE_NOTE_PREFIX)
    assert "\n" not in render_scope_note(2, 4)
    with pytest.raises(ScopeMappingError):
        render_scope_note(5, 4)
    with pytest.raises(ScopeMappingError):
        render_scope_note(-1, 4)
    with pytest.raises(ScopeMappingError):
        render_scope_note(True, 4)


def test_deferred_scope_note_carries_the_reason():
    assert render_scope_deferred_note(DIFF_TOO_LARGE_REASON) == (
        "Scope: deferred ({})".format(DIFF_TOO_LARGE_REASON))
    assert render_scope_deferred_note("  diff unavailable: nothing to map ") == (
        "Scope: deferred (diff unavailable: nothing to map)")
    with pytest.raises(ScopeMappingError):
        render_scope_deferred_note("  ")


def test_note_and_outcomes_record_agree_on_the_same_mapping():
    """#2028 Accept: the review note count matches outcomes."""
    groups = scope_groups(DIFF)
    ids = [group["id"] for group in groups]
    raw = json.dumps({"scope": [
        {"group": ids[0], "requirement": "requirement one",
         "evidence": "implements it"},
        {"group": ids[1], "requirement": UNREQUESTED,
         "evidence": "nothing asks for this"},
        {"group": ids[2], "requirement": "requirement one",
         "evidence": "supports it"},
        {"group": ids[3], "requirement": UNREQUESTED,
         "evidence": "drive-by prose"},
    ]})

    entries = parse_scope_mapping(raw, groups)
    record = derive_scope_outcome(
        REPO, 4242, HEAD_SHA, count_unrequested(entries), len(entries))

    assert record["unrequested_count"] == 2
    assert record["group_count"] == 4
    assert scope_note_for_mapping(entries) == "Scope: 2 unrequested of 4 groups"
    assert scope_note_for_record(record) == "Scope: 2 unrequested of 4 groups"


def test_deferred_note_and_deferred_record_agree():
    reason = scope_skip_reason("x" * (DIFF_LIMIT_BYTES + 1))
    assert reason is not None
    record = deferred_scope_outcome(REPO, 4242, HEAD_SHA, reason)

    assert scope_note_for_record(record) == render_scope_deferred_note(reason)
    assert scope_note_for_record(record).startswith("Scope: deferred (")
    with pytest.raises(ScopeMappingError):
        scope_note_for_record({"status": "mapped"})
    with pytest.raises(ScopeMappingError):
        scope_note_for_record({"status": "unknown"})
