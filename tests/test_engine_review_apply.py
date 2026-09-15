"""Validate the model's review answer and perform its effects (#800).

The review runner shows the model a packet and nothing else; the model
answers with one JSON object and this step validates it and performs
every side effect. Each branch gets a fixture test here, and the
acceptance property — a malformed answer never produces an approved
verdict in any fixture — is asserted over the whole battery.
"""

from __future__ import annotations

import io
import json
import pathlib
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import review_apply  # noqa: E402
from engine.review_apply import AnswerError  # noqa: E402

REPO = "owner/repo"
SHA = "abc123def456"
OTHER_SHA = "7890fedcba98"


def answer(**kw):
    data = {"verdict": "approved", "blocking": [], "unsure": []}
    data.update(kw)
    return json.dumps(data)


# Every malformed shape the runner can hand over: not JSON, not an
# object, a wrong verdict, a missing or mistyped list, and the
# self-contradicting approval that names blockers.
MALFORMED = [
    "",
    "   \n  ",
    "{not json",
    '{"verdict": "approved", "blocking": [], "unsure": []} trailing',
    '["approved", [], []]',
    '"approved"',
    "3",
    "null",
    json.dumps({"blocking": [], "unsure": []}),
    json.dumps({"verdict": "Approved", "blocking": [], "unsure": []}),
    json.dumps({"verdict": "APPROVED", "blocking": [], "unsure": []}),
    json.dumps({"verdict": "maybe", "blocking": [], "unsure": []}),
    json.dumps({"verdict": None, "blocking": [], "unsure": []}),
    json.dumps({"verdict": 1, "blocking": [], "unsure": []}),
    json.dumps({"verdict": "rejected", "unsure": []}),
    json.dumps({"verdict": "rejected", "blocking": "nope", "unsure": []}),
    json.dumps({"verdict": "rejected", "blocking": [1], "unsure": []}),
    json.dumps({"verdict": "rejected", "blocking": [None], "unsure": []}),
    json.dumps({"verdict": "rejected",
                "blocking": ["fine", {"item": 1}], "unsure": []}),
    json.dumps({"verdict": "rejected", "blocking": []}),
    json.dumps({"verdict": "rejected", "blocking": [],
                "unsure": "not sure"}),
    json.dumps({"verdict": "rejected", "blocking": [],
                "unsure": [False]}),
    json.dumps({"verdict": "approved", "blocking": ["a blocker"],
                "unsure": []}),
]


# -- schema validation --------------------------------------------------------

def test_a_clean_approval_parses():
    found = review_apply.parse_answer(answer())
    assert found == {"verdict": "approved", "blocking": [], "unsure": []}


def test_a_rejection_with_blocking_parses():
    found = review_apply.parse_answer(
        answer(verdict="rejected", blocking=["the diff ignores the plan"]))
    assert found["verdict"] == "rejected"
    assert found["blocking"] == ["the diff ignores the plan"]


def test_unknown_keys_are_ignored():
    found = review_apply.parse_answer(answer(summary="looks good"))
    assert found["verdict"] == "approved"


@pytest.mark.parametrize("raw", MALFORMED)
def test_malformed_answers_fail_validation(raw):
    with pytest.raises(AnswerError):
        review_apply.parse_answer(raw)


def test_an_empty_answer_says_what_was_expected():
    with pytest.raises(AnswerError, match="expected a JSON object"):
        review_apply.parse_answer("")


def test_invalid_json_carries_the_parse_error():
    with pytest.raises(AnswerError, match="invalid JSON"):
        review_apply.parse_answer("{not json")


def test_a_wrong_verdict_names_the_allowed_words():
    with pytest.raises(AnswerError, match="approved.*rejected"):
        review_apply.parse_answer(
            answer(verdict="maybe"))


def test_an_approved_verdict_must_not_carry_blocking():
    with pytest.raises(AnswerError, match="must not carry blocking"):
        review_apply.parse_answer(
            answer(blocking=["but this is wrong"]))


# -- the decision -------------------------------------------------------------

def test_a_clean_approval_decides_approved_without_a_note():
    verdict, blocking, note = review_apply.decide(
        {"verdict": "approved", "blocking": [], "unsure": []})
    assert (verdict, blocking, note) == ("approved", [], None)


def test_a_rejection_keeps_its_blocking_list():
    verdict, blocking, note = review_apply.decide(
        {"verdict": "rejected", "blocking": ["fix this"], "unsure": []})
    assert (verdict, blocking, note) == ("rejected", ["fix this"], None)


def test_unsure_turns_an_approval_into_a_rejection():
    verdict, blocking, note = review_apply.decide(
        {"verdict": "approved", "blocking": [],
         "unsure": ["not sure about the migration"]})
    assert verdict == "rejected"
    assert blocking == ["unsure: not sure about the migration"]
    assert "unsure was non-empty" in note
    assert "approved" in note


def test_unsure_joins_existing_blocking_with_provenance():
    verdict, blocking, note = review_apply.decide(
        {"verdict": "rejected", "blocking": ["fix this"],
         "unsure": ["and maybe that"]})
    assert verdict == "rejected"
    assert blocking == ["fix this", "unsure: and maybe that"]
    assert note is not None


# -- the acceptance property --------------------------------------------------

@pytest.mark.parametrize("raw", MALFORMED)
def test_no_malformed_answer_can_produce_an_approval(raw):
    """The ticket's accept line: malformed input never approves."""
    with pytest.raises(AnswerError):
        decided = review_apply.decide(review_apply.parse_answer(raw))
        assert decided[0] != "approved"  # unreachable; parsing raises first


def test_only_a_clean_approval_decides_approved():
    raws = [
        answer(),
        answer(verdict="rejected", blocking=["no"]),
        answer(unsure=["hmm"]),
        answer(verdict="rejected", blocking=["no"], unsure=["hmm"]),
    ]
    verdicts = [review_apply.decide(review_apply.parse_answer(raw))[0]
                for raw in raws]
    assert verdicts == ["approved", "rejected", "rejected", "rejected"]


# -- the effects --------------------------------------------------------------

class Wiring:
    """Capture the existing-path calls instead of touching GitHub."""

    def __init__(self, monkeypatch, merge_code=0, head=SHA):
        self.reviews = []
        self.merges = []
        self.merge_code = merge_code
        self.head = head
        monkeypatch.setattr(funnel, "resolve_repo", lambda repo: REPO)
        monkeypatch.setattr(funnel, "load_items", lambda: ["items"])
        monkeypatch.setattr(review_apply, "current_head",
                            lambda repo, pr: self.head)
        monkeypatch.setattr(funnel, "cmd_review", self._review)
        monkeypatch.setattr(funnel, "cmd_merge", self._merge)

    def _review(self, repo, pr, verdict, ci, blocking, note,
                run=None, agent=None):
        self.reviews.append({"repo": repo, "pr": pr, "verdict": verdict,
                             "ci": ci, "blocking": blocking, "note": note,
                             "run": run, "agent": agent})
        return 0

    def _merge(self, items, now, repo, pr, confirmed):
        self.merges.append({"items": items, "repo": repo, "pr": pr,
                            "confirmed": confirmed})
        return self.merge_code


def run_cli(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return review_apply.main(argv)


def test_approved_records_then_merges_and_closes(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-"], stdin=answer())
    assert code == 0
    assert wiring.reviews == [{"repo": REPO, "pr": 7, "verdict": "approved",
                               "ci": "unknown", "blocking": [], "note": None,
                               "run": None, "agent": None}]
    # Confirmed: the gate merges and cmd_merge closes the ticket.
    assert wiring.merges == [{"items": ["items"], "repo": REPO, "pr": 7,
                              "confirmed": True}]


def test_approved_carries_the_packet_ci_state(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-", "--ci", "green"],
                   stdin=answer())
    assert code == 0
    assert wiring.reviews[0]["ci"] == "green"


def test_a_verdict_carries_the_run_and_agent_provenance(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(
        monkeypatch, capsys,
        ["7", "--repo", REPO, "--answer", "-", "--run", "run-897",
         "--agent", "muse"],
        stdin=answer(verdict="rejected", blocking=["fix this"]),
    )
    assert code == 0
    assert wiring.reviews[0]["run"] == "run-897"
    assert wiring.reviews[0]["agent"] == "muse"


def test_rejected_records_with_blocking_and_never_merges(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(
        monkeypatch, capsys, ["7", "--repo", REPO, "--answer", "-"],
        stdin=answer(verdict="rejected", blocking=["fix this"]))
    assert code == 0
    assert wiring.reviews == [{"repo": REPO, "pr": 7, "verdict": "rejected",
                               "ci": "unknown", "blocking": ["fix this"],
                               "note": None, "run": None, "agent": None}]
    assert wiring.merges == []


def test_unsure_records_a_rejection_and_never_merges(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(
        monkeypatch, capsys, ["7", "--repo", REPO, "--answer", "-"],
        stdin=answer(unsure=["not sure"]))
    assert code == 0
    assert wiring.reviews[0]["verdict"] == "rejected"
    assert wiring.reviews[0]["blocking"] == ["unsure: not sure"]
    assert "unsure was non-empty" in wiring.reviews[0]["note"]
    assert wiring.merges == []


def test_a_refused_merge_keeps_the_verdict_and_reports_failure(
        monkeypatch, capsys):
    wiring = Wiring(monkeypatch, merge_code=1)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-"], stdin=answer())
    assert code == 1
    assert wiring.reviews[0]["verdict"] == "approved"
    assert wiring.merges[0]["confirmed"] is True


def test_a_moved_head_records_nothing(monkeypatch, capsys):
    wiring = Wiring(monkeypatch, head=OTHER_SHA)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-",
                    "--head", SHA],
                   stdin=answer())
    assert code == 1
    assert wiring.reviews == []
    assert wiring.merges == []
    err = capsys.readouterr().err
    assert SHA in err
    assert OTHER_SHA in err


def test_a_matching_packet_head_proceeds(monkeypatch, capsys):
    wiring = Wiring(monkeypatch, head=SHA)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-",
                    "--head", SHA],
                   stdin=answer())
    assert code == 0
    assert wiring.reviews[0]["verdict"] == "approved"


def test_answer_from_a_file(monkeypatch, capsys, tmp_path):
    wiring = Wiring(monkeypatch)
    path = tmp_path / "answer.json"
    path.write_text(answer(verdict="rejected", blocking=["from a file"]))
    code = review_apply.main(
        ["7", "--repo", REPO, "--answer", str(path)])
    assert code == 0
    assert wiring.reviews[0]["blocking"] == ["from a file"]


def test_an_unreadable_answer_file_fails_without_recording(
        monkeypatch, capsys, tmp_path):
    wiring = Wiring(monkeypatch)
    code = review_apply.main(
        ["7", "--repo", REPO, "--answer", str(tmp_path / "missing.json")])
    assert code == 1
    assert wiring.reviews == []
    assert "cannot read the answer" in capsys.readouterr().err


# -- malformed input: retry once, then reject ---------------------------------

def test_first_malformed_answer_exits_3_with_the_parse_error(
        monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-"],
                   stdin="{not json")
    assert code == review_apply.RETRY_EXIT == 3
    assert wiring.reviews == []
    assert wiring.merges == []
    assert "invalid JSON" in capsys.readouterr().err


@pytest.mark.parametrize("raw", MALFORMED)
def test_every_malformed_shape_retries_without_recording(
        monkeypatch, capsys, raw):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-"], stdin=raw)
    assert code == 3
    assert wiring.reviews == []
    assert wiring.merges == []


def test_final_malformed_answer_records_rejected_with_raw_output(
        monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    raw = '{"verdict": "approved", "blocking": ["x"]'  # truncated JSON
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-",
                    "--attempt", "2"],
                   stdin=raw)
    assert code == 1
    assert len(wiring.reviews) == 1
    recorded = wiring.reviews[0]
    assert recorded["verdict"] == "rejected"
    assert recorded["note"] == raw
    assert recorded["run"] is None
    assert recorded["agent"] is None
    assert "could not be parsed" in recorded["blocking"][0]
    assert "invalid JSON" in recorded["blocking"][0]
    assert wiring.merges == []
    assert review_apply.ERRORED_OUTCOME in capsys.readouterr().out


def test_attempts_past_the_second_stay_final(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-",
                    "--attempt", "3"],
                   stdin="nope")
    assert code == 1
    assert wiring.reviews[0]["verdict"] == "rejected"
    assert review_apply.ERRORED_OUTCOME in capsys.readouterr().out


def test_a_zero_attempt_is_rejected(monkeypatch, capsys):
    Wiring(monkeypatch)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, capsys,
                ["7", "--repo", REPO, "--answer", "-",
                 "--attempt", "0"],
                stdin=answer())


def test_a_long_raw_output_is_capped_with_the_cut_marked():
    raw = "x" * (review_apply.MAX_RAW_NOTE + 500)
    note = review_apply.truncate_raw(raw)
    assert len(note) < len(raw)
    assert note.endswith("[truncated 500 chars]")
    assert review_apply.truncate_raw("short") == "short"


def test_a_github_failure_while_recording_exits_1(monkeypatch, capsys):
    Wiring(monkeypatch)

    def fail(*args, **kwargs):
        raise funnel.GitHubError("the network dropped")

    monkeypatch.setattr(funnel, "cmd_review", fail)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-"], stdin=answer())
    assert code == 1
    assert "the network dropped" in capsys.readouterr().err


# -- structural guarantees ----------------------------------------------------

def test_engine_imports_from_funnel_and_never_the_reverse():
    engine_source = (ROOT / "engine" / "review_apply.py").read_text()
    assert "import funnel" in engine_source
    funnel_source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in funnel_source
    assert "from engine" not in funnel_source


def test_writes_go_through_the_existing_paths_only():
    # review-apply performs effects, but it must not grow its own GitHub
    # mutating verbs: recording is funnel.cmd_review and merging is
    # funnel.cmd_merge. A direct verb here is a second definition of the
    # gate the plan keeps in funnel.py.
    source = (ROOT / "engine" / "review_apply.py").read_text()
    assert "funnel.cmd_review" in source
    assert "funnel.cmd_merge" in source
    mutating = ("pr merge", "pr comment", "pr close", "pr review",
                "pr edit", "pr create", "issue close", "issue create",
                "issue comment", "issue edit", "item-add", "item-edit",
                "item-delete", "--yes", "delete-branch")
    offenders = [verb for verb in mutating if verb in source]
    assert offenders == [], "review-apply mutates directly: {}".format(
        offenders)


def test_the_entry_point_is_executable():
    entry = ROOT / "review-apply"
    assert entry.exists()
    assert entry.stat().st_mode & stat.S_IXUSR


# -- validate-only: parse and decide without recording -------------------------

def test_validate_only_prints_the_decision_and_records_nothing(
        monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-", "--validate-only"],
                   stdin=answer())
    assert code == 0
    assert wiring.reviews == []
    assert wiring.merges == []
    decided = json.loads(capsys.readouterr().out)
    assert decided == {"verdict": "approved", "blocking": [], "note": None}


def test_validate_only_decides_unsure_as_rejected(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-", "--validate-only"],
                   stdin=answer(unsure=["not sure"]))
    assert code == 0
    assert wiring.reviews == []
    assert wiring.merges == []
    decided = json.loads(capsys.readouterr().out)
    assert decided["verdict"] == "rejected"
    assert decided["blocking"] == ["unsure: not sure"]
    assert decided["note"] is not None


def test_validate_only_keeps_the_retryable_exit_split(monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-", "--validate-only"],
                   stdin="{not json")
    assert code == review_apply.RETRY_EXIT == 3
    assert wiring.reviews == []
    assert "invalid JSON" in capsys.readouterr().err


def test_validate_only_final_malformed_exits_1_without_recording(
        monkeypatch, capsys):
    wiring = Wiring(monkeypatch)
    code = run_cli(monkeypatch, capsys,
                   ["7", "--repo", REPO, "--answer", "-", "--validate-only",
                    "--attempt", "2"],
                   stdin="{not json")
    assert code == 1
    assert wiring.reviews == []
    assert wiring.merges == []
    out = capsys.readouterr()
    assert "invalid JSON" in out.err
    assert review_apply.ERRORED_OUTCOME not in out.out
