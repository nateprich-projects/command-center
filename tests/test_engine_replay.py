"""Private-packet review replay stays verdict-only and fail-closed.

Each replay runs the review engine's replay entry (#1730); the tests below
stand a stub engine in its place and pin what replay hands it and what it
makes of the answer. The entry itself is tested with the engine, in
tests/test_muse_review_engine.py.
"""

from __future__ import annotations

import json
import pathlib
import re
import stat
import string
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import replay  # noqa: E402
from engine import review_apply  # noqa: E402


def answer(**overrides):
    found = {
        "verdict": "approved",
        "blocking": [],
        "unsure": [],
        "requirements": [{
            "requirement": "the replay stays private",
            "status": "met",
            "evidence": "engine/replay.py prints verdicts only",
        }],
    }
    found.update(overrides)
    return json.dumps(found)


def private_packet(tmp_path, *, body="private packet text"):
    corpus = tmp_path / "review-corpus"
    corpus.mkdir(mode=0o700)
    packet = corpus / "packet.json"
    packet.write_text(json.dumps({"diff": body}), encoding="utf-8")
    packet.chmod(0o600)
    return packet


def routine(tmp_path):
    path = tmp_path / "review.md"
    path.write_text(
        "Human note.\n\n---\nJudge this packet:\nPACKET_JSON\n",
        encoding="utf-8",
    )
    return path


def test_packet_path_resolves_under_runtime_root(tmp_path):
    packet = private_packet(tmp_path)

    assert replay.resolve_packet_path(
        "review-corpus/packet.json", tmp_path) == packet.resolve()


def test_packet_path_rejects_files_outside_runtime_root(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.json")
    outside.write_text("{}", encoding="utf-8")
    outside.chmod(0o600)
    try:
        with pytest.raises(replay.ReplayError):
            replay.resolve_packet_path(outside, tmp_path)
    finally:
        outside.unlink()


def test_packet_requires_private_file_and_directory(tmp_path):
    packet = private_packet(tmp_path)
    packet.chmod(0o644)

    with pytest.raises(replay.ReplayError):
        replay.resolve_packet_path(packet, tmp_path)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (answer(), "approved"),
        (answer(unsure=["unclear"]), "rejected"),
        (answer(requirements=[{
            "requirement": "the replay stays private",
            "status": "unmet",
            "evidence": "the output contains the packet",
        }]), "rejected"),
        (answer(verdict="rejected"), "rejected"),
    ],
)
def test_scoring_uses_review_apply(raw, expected):
    parsed = review_apply.parse_answer(raw)

    assert replay.score_answer(raw) == review_apply.decide(parsed)[0] == expected


@pytest.mark.parametrize(
    "verdicts,expected,passed",
    [
        (["rejected", "rejected", "rejected"], "rejected", True),
        (["approved", "approved", "approved"], "rejected", False),
        (["rejected", "approved", "rejected"], "rejected", False),
    ],
)
def test_known_verdict_requires_every_run_to_match(verdicts, expected, passed):
    assert replay.all_match(verdicts, expected) is passed


def test_budget_gate_reserves_one_session_per_engine_run(monkeypatch):
    reading = {"windows": {"seven_day": {"used_percent": 10.0,
                                          "rolling": True}}}
    observed = {}

    monkeypatch.setattr(replay.agent_health, "quota_hold_until", lambda: None)
    monkeypatch.setattr(replay.usage, "read_agent",
                        lambda agent, now: reading)

    def pace(found, now, provider):
        observed.update(reading=found, now=now, provider=provider)
        return {"known": True, "over_pace": False, "band": "ok"}

    monkeypatch.setattr(replay.usage, "pace", pace)
    replay.check_budget(6, now=100.0)

    assert observed["provider"] == "meta"
    assert observed["reading"]["windows"]["seven_day"]["policy"][
        "weekly_reserve"] == replay.usage.policy(
            "meta", "weekly_reserve", replay.usage.WEEKLY_RESERVE) * 6
    assert "policy" not in reading["windows"]["seven_day"]


@pytest.mark.parametrize(
    "result",
    [
        {"known": False, "over_pace": False, "band": None},
        {"known": True, "over_pace": True, "band": "over"},
        {"known": True, "over_pace": False, "band": "tight"},
    ],
)
def test_budget_gate_stops_unknown_over_and_tight(monkeypatch, result):
    monkeypatch.setattr(replay.agent_health, "quota_hold_until", lambda: None)
    monkeypatch.setattr(replay.usage, "read_agent", lambda _agent, _now: {
        "windows": {"seven_day": {"used_percent": 10.0,
                                    "rolling": True}},
    })
    monkeypatch.setattr(replay.usage, "pace", lambda *_args, **_kwargs: result)

    with pytest.raises(replay.ReplayError):
        replay.check_budget(1, now=100.0)


def test_budget_gate_stops_for_an_active_provider_quota_hold(monkeypatch):
    monkeypatch.setattr(replay.agent_health, "quota_hold_until", lambda: 200.0)
    monkeypatch.setattr(
        replay.usage, "read_agent",
        lambda *_args: pytest.fail("budget reading followed an active hold"),
    )

    with pytest.raises(replay.ReplayError):
        replay.check_budget(1, now=100.0)


#: Stands in for scripts/muse-review-engine. It logs what it was handed and
#: where its answer path stood, prints private text on both streams and any
#: stderr lines the test gives this run, and then answers, fails, or exits 0
#: with no answer, per run.
STUB_ENGINE = (
    "#!/bin/bash\n"
    "count=\"$STUB_ENGINE_COUNT\"\n"
    "n=1\n"
    "if [[ -f \"$count\" ]]; then n=$(($(cat \"$count\") + 1)); fi\n"
    "printf '%s' \"$n\" > \"$count\"\n"
    "python3 - \"$@\" >> \"$STUB_ENGINE_LOG\" <<'PY'\n"
    "import json, os, stat, sys\n"
    "answer = os.environ.get('MUSE_REVIEW_ENGINE_REPLAY_ANSWER', '')\n"
    "parent = os.path.dirname(answer)\n"
    "print(json.dumps({\n"
    "    'argv': sys.argv[1:],\n"
    "    'repo': os.environ.get('MUSE_REVIEW_ENGINE_REPO'),\n"
    "    'packet': os.environ.get('MUSE_REVIEW_ENGINE_REPLAY_PACKET'),\n"
    "    'routine': os.environ.get('MUSE_REVIEW_ENGINE_REPLAY_ROUTINE'),\n"
    "    'answer': answer,\n"
    "    'answer_existed': os.path.lexists(answer),\n"
    "    'answer_dir_mode': stat.S_IMODE(os.stat(parent).st_mode),\n"
    "}))\n"
    "PY\n"
    "echo 'NEVER PRINT THIS PRIVATE DIFF on stdout'\n"
    "echo 'NEVER PRINT THIS PRIVATE DIFF on stderr' >&2\n"
    "err=\"STUB_ENGINE_STDERR_$n\"\n"
    "if [[ -n \"${!err:-}\" ]]; then printf '%s\\n' \"${!err}\" >&2; fi\n"
    "if [[ \"${STUB_ENGINE_FAIL_RUN:-}\" == \"$n\" ]]; then\n"
    "  exit \"${STUB_ENGINE_FAIL_STATUS:-1}\"\n"
    "fi\n"
    "if [[ \"${STUB_ENGINE_SILENT_RUN:-}\" == \"$n\" ]]; then exit 0; fi\n"
    "var=\"STUB_ENGINE_ANSWER_$n\"\n"
    "printf '%s' \"${!var:-$STUB_ENGINE_ANSWER}\" > \"$MUSE_REVIEW_ENGINE_REPLAY_ANSWER\"\n"
)


@pytest.fixture
def stub_engine(tmp_path, monkeypatch):
    """Point replay at the stub engine; return a reader of its run log."""
    engine = tmp_path / "stub-engine"
    engine.write_text(STUB_ENGINE)
    log = tmp_path / "stub-engine.log"
    monkeypatch.setattr(replay, "ENGINE", engine)
    monkeypatch.setenv("STUB_ENGINE_COUNT", str(tmp_path / "stub-engine.count"))
    monkeypatch.setenv("STUB_ENGINE_LOG", str(log))
    monkeypatch.setenv("STUB_ENGINE_ANSWER", answer())
    monkeypatch.setattr(replay, "check_budget", lambda _runs: None)

    def runs():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return runs


def _replay_cli(tmp_path, runs=3, expected="approved"):
    packet = private_packet(tmp_path, body="NEVER PRINT THIS PRIVATE DIFF")
    return replay.main([
        str(packet), "--routine", str(routine(tmp_path)), "--runs", str(runs),
        "--expected-verdict", expected, "--runtime-root", str(tmp_path),
    ])


def test_each_run_is_one_engine_run_scored_through_review_apply(
        tmp_path, monkeypatch, stub_engine):
    raws = [
        answer(),
        # The derived verdict says approved, but review-apply's rule reads
        # the unsure entry as a rejection, and replay scores by that rule.
        answer(unsure=["the packet could not settle it"]),
        answer(verdict="rejected", blocking=["requirement unmet: x -- y"],
               requirements=[{"requirement": "x", "status": "unmet",
                              "evidence": "y"}]),
    ]
    for index, raw in enumerate(raws, 1):
        monkeypatch.setenv("STUB_ENGINE_ANSWER_{}".format(index), raw)
    packet = private_packet(tmp_path)

    summary = replay.replay(
        packet, routine(tmp_path), runs=3, expected="approved",
        runtime_root=tmp_path)

    expected = [review_apply.decide(review_apply.parse_answer(raw))[0]
                for raw in raws]
    assert expected == ["approved", "rejected", "rejected"]
    assert summary == {"expected": "approved", "verdicts": expected,
                       "failed_parts": [[], [], []], "pass": False}
    runs = stub_engine()
    assert len(runs) == 3
    # A fresh, owner-only directory under the runtime root for each run's
    # answer, nothing there before the engine writes, and nothing left after.
    assert len({run["answer"] for run in runs}) == 3
    for run in runs:
        assert pathlib.Path(run["answer"]).parent.parent == tmp_path.resolve()
        assert run["answer_existed"] is False
        assert run["answer_dir_mode"] == 0o700
        assert not pathlib.Path(run["answer"]).parent.exists()


def test_the_engine_runs_from_this_checkout_on_the_given_inputs(
        tmp_path, stub_engine):
    packet = private_packet(tmp_path)
    routine_path = routine(tmp_path)

    replay.replay(packet, routine_path, runs=1, expected="approved",
                  runtime_root=tmp_path)

    [run] = stub_engine()
    # The branch's own engine, routine and judges: not the maintained clone.
    assert replay.CHECKOUT == ROOT
    assert replay.ENGINE.name == "stub-engine"
    assert run["repo"] == str(ROOT)
    assert run["argv"] == ["escalated", "max"]
    assert run["packet"] == str(packet.resolve())
    assert run["routine"] == str(routine_path.resolve())


def test_the_default_engine_is_this_checkouts_runner():
    assert replay.ENGINE == ROOT / "scripts" / "muse-review-engine"
    assert replay.ENGINE.stat().st_mode & stat.S_IXUSR


@pytest.mark.parametrize(
    "failure,reported",
    [
        ({"STUB_ENGINE_FAIL_RUN": "2"}, "exit status 1"),
        ({"STUB_ENGINE_SILENT_RUN": "2"}, "exit status 0, no answer"),
        ({"STUB_ENGINE_ANSWER_2": "not json"},
         "exit status 0, unscorable answer"),
        # A lone 0xff byte in the answer file: not UTF-8 at all.
        ({"STUB_ENGINE_ANSWER_2": "\udcff"},
         "exit status 0, unreadable answer"),
    ],
    ids=["non-zero exit", "no answer", "unscorable answer",
         "unreadable answer"],
)
def test_an_engine_failure_fails_the_whole_replay(
        tmp_path, monkeypatch, capfd, stub_engine, failure, reported):
    for name, value in failure.items():
        monkeypatch.setenv(name, value)

    result = _replay_cli(tmp_path)
    output = capfd.readouterr()

    assert result == 1
    assert output.out == ""
    assert output.err == (
        "review-replay: run 2 failed: {}; failed parts: none\n"
        "review-replay: replay failed\n".format(reported))
    # The first failure ends the replay: no third run, and no verdicts.
    assert len(stub_engine()) == 2


# -- the engine's timing lines (#1784) ----------------------------------------

#: Text from a private ticket's requirement, as a failing judge's diagnostic
#: could quote it. None of it may leave the replay on either stream.
REQUIREMENT = "NEVER PRINT THIS PRIVATE REQUIREMENT: payouts round half-even"


def _timing(part, elapsed, calls, outcome):
    return "muse-review-engine: timing {} elapsed={}s calls={} outcome={}".format(
        part, elapsed, calls, outcome)


#: Engine stderr lines that must never pass, each with requirement text in it.
QUOTING_LINES = [
    # What model_failed prints for a judge's failed call.
    "muse-review-engine: muse exec failed (exit 1): the judge could not "
    "settle '{}'".format(REQUIREMENT),
    # The lister's stream-idle notice quotes the head of its diagnostic.
    "muse-review-engine: the lister went stream-idle; asking it once more: "
    + REQUIREMENT,
    # A later line of a multi-line diagnostic, with no engine prefix at all.
    REQUIREMENT,
    # A real timing line with requirement text after it, before it, or
    # ahead of a carriage return on the same line.
    _timing("judge.3", 412, 2, "failed") + " " + REQUIREMENT,
    REQUIREMENT + " " + _timing("judge.3", 412, 2, "failed"),
    REQUIREMENT + "\r" + _timing("judge.3", 412, 2, "failed"),
    # Requirement text where the part name or outcome goes.
    _timing(REQUIREMENT, 412, 2, "failed"),
    _timing("judge.3", 412, 2, "failed: " + REQUIREMENT),
    # Requirement text with no spaces, in every field. A part is only a
    # name the engine gives: capitals, `_`, `-` or `:` in it are refused,
    # and so is a lowercase word or dotted name the engine never uses.
    _timing("PayoutsRoundHalfEven", 412, 2, "failed"),
    _timing("judge.Payouts", 412, 2, "failed"),
    _timing("payouts_round_half_even", 412, 2, "failed"),
    _timing("payouts-round-half-even", 412, 2, "failed"),
    _timing("judge.3:payouts", 412, 2, "failed"),
    _timing("payouts", 412, 2, "failed"),
    _timing("judge.payouts", 412, 2, "failed"),
    _timing("shape.payouts.0", 412, 2, "failed"),
    # An outcome is one of the engine's three, in its own case...
    _timing("judge.3", 412, 2, "failed:payouts-round-half-even"),
    _timing("judge.3", 412, 2, "FAILED"),
    # ...and elapsed and calls are ASCII digits alone: no letters in either
    # case, no underscores, no hex, and no non-ASCII digits (which \\d takes).
    _timing("judge.3", "payouts-round-half-even", 2, "failed"),
    _timing("judge.3", 412, "payouts-round-half-even", "failed"),
    _timing("judge.3", "PayoutsRoundHalfEven", 2, "failed"),
    _timing("judge.3", "payouts_round", 2, "failed"),
    _timing("judge.3", "abc123", 2, "failed"),
    _timing("judge.3", "\u0664\u0661\u0662", 2, "failed"),
    _timing("judge.3", 412, "PayoutsRoundHalfEven", "failed"),
    _timing("judge.3", 412, "payouts", "failed"),
    _timing("judge.3", 412, "a1", "failed"),
    _timing("judge.3", 412, "\u0662", "failed"),
    # A shape part is framer, auditor, or a numbered sibling or decider, and
    # every index is ASCII digits: no other word, no letters in an index.
    _timing("shape.payouts", 412, 2, "failed"),
    _timing("shape.framerx", 412, 2, "failed"),
    _timing("judge.ab", 412, 2, "failed"),
    _timing("judge.1a", 412, 2, "failed"),
    _timing("shape.sibling.ab", 412, 2, "failed"),
    _timing("shape.decider.\u0663", 412, 2, "failed"),
    _timing("lister2", 412, 2, "failed"),
]


@pytest.mark.parametrize(
    "line,part,outcome",
    [
        (_timing("judge.3", 412, 2, "failed"), "judge.3", "failed"),
        (_timing("judge.0", 38, 1, "done"), "judge.0", "done"),
        (_timing("shape.decider.0", 412, 2, "retried-done"),
         "shape.decider.0", "retried-done"),
        (_timing("judge.12", 60, 1, "done"), "judge.12", "done"),
        (_timing("shape.framer", 300, 1, "done"), "shape.framer", "done"),
        (_timing("shape.sibling.2", 90, 1, "failed"),
         "shape.sibling.2", "failed"),
        (_timing("shape.auditor", 7, 1, "done"), "shape.auditor", "done"),
        (_timing("lister", 95, 3, "done"), "lister", "done"),
    ],
)
def test_timing_lines_parse_the_engines_fixed_format(line, part, outcome):
    [timing] = replay.timing_lines((line + "\n").encode())

    assert timing["part"] == part
    assert timing["outcome"] == outcome


@pytest.mark.parametrize(
    "part,answered,idle_retried,outcome",
    [
        ("judge.3", 1, 0, "done"),
        ("shape.decider.0", 1, 1, "retried-done"),
        ("judge.12", 0, 1, "failed"),
    ],
)
def test_the_pattern_reads_what_log_call_timing_writes(
        part, answered, idle_retried, outcome):
    # The engine's own function, run as the engine runs it: if its line
    # changes shape, replay would drop every timing line, and this fails.
    source = (ROOT / "scripts" / "muse-review-engine").read_text()
    function = re.search(r"^log_call_timing\(\) \{\n.*?^\}\n", source,
                         re.M | re.S).group(0)
    proc = subprocess.run(
        ["/bin/bash", "-c", function
         + 'log_call_timing "$1" "$(( $(date +%s) - 5 ))" 2 "$2" "$3"',
         "bash", part, str(answered), str(idle_retried)],
        capture_output=True, check=True,
    )

    [timing] = replay.timing_lines(proc.stderr)

    assert timing["part"] == part
    assert timing["calls"] == "2"
    assert timing["outcome"] == outcome


@pytest.mark.parametrize("line", QUOTING_LINES)
def test_timing_lines_drop_every_line_that_quotes_requirement_text(line):
    assert replay.timing_lines((line + "\n").encode()) == []


# Each field's accepted language, pinned by its whole edit neighbourhood: every
# one-character insert, delete or substitute of a valid value, and every swap
# of a word or an index for requirement-like text, is refused unless the spec
# below accepts it. Listing counterexamples one loosening at a time left a new
# batch of plausible widenings passing each time (#1784).

#: Every ASCII letter, digits, the punctuation a widening would let
#: through, a non-ASCII digit that \\d takes, and a space.
EDIT_ALPHABET = string.ascii_letters + "09_-.:\u0663 "
#: A widening that admits a separator and then a word, inserted anywhere.
EDIT_SEPARATORS = ["", "-", ".", ":", "_", "/", " ", "="]
EDIT_WORDS = ["a", "payouts", "payoutsroundhalfeven"]
VALID_PARTS = ["judge.3", "judge.12", "shape.framer", "shape.auditor",
               "shape.sibling.0", "shape.decider.2", "lister"]
VALID_OUTCOMES = ["done", "retried-done", "failed"]


def _edits(value):
    found = set()
    for i in range(len(value) + 1):
        for c in EDIT_ALPHABET:
            found.add(value[:i] + c + value[i:])
        for sep in EDIT_SEPARATORS:
            for word in EDIT_WORDS:
                found.add(value[:i] + sep + word + value[i:])
                found.add(value[:i] + word + sep + value[i:])
    for i in range(len(value)):
        found.add(value[:i] + value[i + 1:])
        for c in EDIT_ALPHABET:
            found.add(value[:i] + c + value[i + 1:])
    for word in ("payouts", "Payouts", "_payouts", "ab", "a0", "0a",
                 "\u0663"):
        found.add(re.sub(r"[a-z]+", word, value, count=1))
        found.add(re.sub(r"[0-9]+", word, value, count=1))
        found.add(value + word)
        found.add(word + value)
    return found


def _accepted(line):
    return replay.timing_lines((line + "\n").encode()) != []


def test_a_part_is_only_a_name_the_engine_gives():
    spec = re.compile(r"judge\.[0-9]+|shape\.(?:framer|auditor"
                      r"|(?:sibling|decider)\.[0-9]+)|lister")
    leaked = sorted(part for valid in VALID_PARTS for part in _edits(valid)
                    if not spec.fullmatch(part)
                    and _accepted(_timing(part, 412, 2, "failed")))

    assert leaked == []


def test_elapsed_and_calls_are_only_ascii_digits():
    leaked = []
    for valid in ("412", "2"):
        for number in _edits(valid):
            if number and all(c in "0123456789" for c in number):
                continue
            if _accepted(_timing("judge.3", number, 2, "failed")):
                leaked.append(("elapsed", number))
            if _accepted(_timing("judge.3", 412, number, "failed")):
                leaked.append(("calls", number))

    assert leaked == []


def test_an_outcome_is_only_one_of_the_three():
    leaked = sorted(outcome for valid in VALID_OUTCOMES
                    for outcome in _edits(valid)
                    if outcome not in VALID_OUTCOMES
                    and _accepted(_timing("judge.3", 412, 2, outcome)))

    assert leaked == []


def test_the_pattern_is_pinned_to_its_reviewed_source():
    # The pattern is a privacy boundary, and no finite set of counterexamples
    # catches every widening of it: four reviews each found a new batch that
    # passed. So its exact source is pinned too, and changing it means
    # changing this test in the same diff, where review sees it (#1784).
    assert replay.TIMING_LINE.pattern == (
        r"muse-review-engine: timing "
        r"(?P<part>judge\.[0-9]{1,4}"
        r"|shape\.(?:framer|auditor|(?:sibling|decider)\.[0-9]{1,4})"
        r"|lister) "
        r"elapsed=(?P<elapsed>[0-9]{1,9})s "
        r"calls=(?P<calls>[0-9]{1,4}) "
        r"outcome=(?P<outcome>done|retried-done|failed)"
    )
    assert replay.TIMING_LINE.flags == re.UNICODE


def test_every_valid_value_is_accepted():
    for part in VALID_PARTS:
        assert _accepted(_timing(part, 412, 2, "failed"))
    for outcome in VALID_OUTCOMES:
        assert _accepted(_timing("judge.3", 412, 2, outcome))


def test_timing_lines_pass_through_under_the_run_number(
        tmp_path, monkeypatch, capfd, stub_engine):
    monkeypatch.setenv("STUB_ENGINE_STDERR_1", "\n".join([
        _timing("judge.0", 38, 1, "done"),
        _timing("judge.1", 412, 2, "retried-done"),
    ]))
    monkeypatch.setenv("STUB_ENGINE_STDERR_2", _timing("judge.0", 41, 1, "done"))

    assert _replay_cli(tmp_path, runs=2) == 0
    output = capfd.readouterr()

    assert output.err.splitlines() == [
        "review-replay: run 1: timing judge.0 elapsed=38s calls=1 outcome=done",
        "review-replay: run 1: timing judge.1 elapsed=412s calls=2 "
        "outcome=retried-done",
        "review-replay: run 2: timing judge.0 elapsed=41s calls=1 outcome=done",
    ]
    assert "timing" not in output.out


def test_a_line_quoting_requirement_text_is_dropped(
        tmp_path, monkeypatch, capfd, stub_engine):
    monkeypatch.setenv("STUB_ENGINE_STDERR_1", "\n".join(
        QUOTING_LINES + [_timing("judge.3", 412, 2, "failed")]))

    assert _replay_cli(tmp_path, runs=1) == 0
    output = capfd.readouterr()

    # Only the one real timing line survives; the diagnostics around it,
    # and every line that merely contains a timing line, are dropped whole.
    assert output.err == (
        "review-replay: run 1: timing judge.3 elapsed=412s calls=2 "
        "outcome=failed\n")
    # Its part is the only failed part on stdout.
    assert json.loads(output.out)["failed_parts"] == [["judge.3"]]
    for stream in (output.out, output.err):
        assert "NEVER PRINT" not in stream
        assert "payouts" not in stream.lower()


def test_a_failed_run_reports_its_exit_status_and_failed_parts(
        tmp_path, monkeypatch, capfd, stub_engine):
    monkeypatch.setenv("STUB_ENGINE_FAIL_RUN", "2")
    monkeypatch.setenv("STUB_ENGINE_FAIL_STATUS", "7")
    monkeypatch.setenv("STUB_ENGINE_STDERR_1", _timing("judge.0", 38, 1, "done"))
    monkeypatch.setenv("STUB_ENGINE_STDERR_2", "\n".join([
        _timing("judge.0", 40, 1, "done"),
        "muse-review-engine: muse exec failed (exit 1): " + REQUIREMENT,
        _timing("judge.3", 412, 2, "failed"),
        _timing("judge.4", 380, 2, "retried-done"),
        _timing("judge.5", 181, 2, "failed"),
    ]))

    assert _replay_cli(tmp_path) == 1
    output = capfd.readouterr()

    assert output.out == ""
    assert output.err.splitlines() == [
        "review-replay: run 1: timing judge.0 elapsed=38s calls=1 outcome=done",
        "review-replay: run 2: timing judge.0 elapsed=40s calls=1 outcome=done",
        "review-replay: run 2: timing judge.3 elapsed=412s calls=2 "
        "outcome=failed",
        "review-replay: run 2: timing judge.4 elapsed=380s calls=2 "
        "outcome=retried-done",
        "review-replay: run 2: timing judge.5 elapsed=181s calls=2 "
        "outcome=failed",
        "review-replay: run 2 failed: exit status 7; "
        "failed parts: judge.3, judge.5",
        "review-replay: replay failed",
    ]
    assert len(stub_engine()) == 2


def test_cli_prints_only_verdicts_and_pass_result(
        tmp_path, monkeypatch, capfd, stub_engine):
    # Run 2 finishes with a judge failed closed, as the must-approve seed's
    # diagnostic run did; the others only retried or answered first time.
    monkeypatch.setenv("STUB_ENGINE_STDERR_1", _timing("judge.0", 38, 1, "done"))
    monkeypatch.setenv("STUB_ENGINE_STDERR_2", "\n".join([
        _timing("judge.3", 412, 2, "failed"),
        "muse-review-engine: muse exec failed (exit 1): " + REQUIREMENT,
        _timing("judge.6", 380, 2, "retried-done"),
    ]))
    monkeypatch.setenv("STUB_ENGINE_STDERR_3", _timing("judge.6", 90, 2,
                                                       "retried-done"))

    result = _replay_cli(tmp_path, expected="approved")
    output = capfd.readouterr()

    assert result == 0
    # Verdicts, and each run's failed parts by name: nothing else.
    assert json.loads(output.out) == {
        "expected": "approved",
        "failed_parts": [[], ["judge.3"], []],
        "pass": True,
        "verdicts": ["approved", "approved", "approved"],
    }
    assert output.out.count("\n") == 1
    # The engine printed private text on both streams; none of it is passed
    # on, and its timing lines go to stderr alone.
    assert "NEVER PRINT" not in output.out
    assert "private" not in output.out
    assert "timing" not in output.out
    assert "NEVER PRINT" not in output.err
    assert "review-replay: run 2: timing judge.3" in output.err


def test_replay_reserves_one_engine_run_per_replay(
        tmp_path, monkeypatch, stub_engine):
    packet = private_packet(tmp_path)
    routine_path = routine(tmp_path)
    reservations = []
    monkeypatch.setattr(replay, "check_budget", reservations.append)

    summary = replay.replay(
        packet, routine_path, runs=3, expected="approved",
        runtime_root=tmp_path,
    )

    assert reservations == [3]
    assert summary["pass"] is True


def test_a_closed_budget_starts_no_engine_run(tmp_path, monkeypatch,
                                              stub_engine):
    def closed(_runs):
        raise replay.ReplayError("Muse budget gate is closed")

    monkeypatch.setattr(replay, "check_budget", closed)

    assert _replay_cli(tmp_path) == 1
    assert stub_engine() == []


def test_cli_entry_point_exists_and_is_executable():
    entry = ROOT / "review-replay"

    assert entry.exists()
    assert entry.stat().st_mode & stat.S_IXUSR
