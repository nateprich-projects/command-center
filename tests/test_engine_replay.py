"""Private-packet review replay stays verdict-only and fail-closed.

Each replay runs the review engine's replay entry (#1730); the tests below
stand a stub engine in its place and pin what replay hands it and what it
makes of the answer. The entry itself is tested with the engine, in
tests/test_muse_review_engine.py.
"""

from __future__ import annotations

import json
import pathlib
import stat
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
#: where its answer path stood, prints private text on both streams, and then
#: answers, fails, or exits 0 with no answer, per run.
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
    "if [[ \"${STUB_ENGINE_FAIL_RUN:-}\" == \"$n\" ]]; then exit 1; fi\n"
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
                       "pass": False}
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
    "failure",
    [
        {"STUB_ENGINE_FAIL_RUN": "2"},
        {"STUB_ENGINE_SILENT_RUN": "2"},
        {"STUB_ENGINE_ANSWER_2": "not json"},
    ],
    ids=["non-zero exit", "no answer", "unscorable answer"],
)
def test_an_engine_failure_fails_the_whole_replay(
        tmp_path, monkeypatch, capfd, stub_engine, failure):
    for name, value in failure.items():
        monkeypatch.setenv(name, value)

    result = _replay_cli(tmp_path)
    output = capfd.readouterr()

    assert result == 1
    assert output.out == ""
    assert output.err == "review-replay: replay failed\n"
    # The first failure ends the replay: no third run, and no verdicts.
    assert len(stub_engine()) == 2


def test_cli_prints_only_verdicts_and_pass_result(tmp_path, capfd, stub_engine):
    result = _replay_cli(tmp_path, expected="approved")
    output = capfd.readouterr()

    assert result == 0
    assert json.loads(output.out) == {
        "expected": "approved",
        "pass": True,
        "verdicts": ["approved", "approved", "approved"],
    }
    # The engine printed private text on both streams; none of it is passed on.
    assert "NEVER PRINT THIS PRIVATE DIFF" not in output.out
    assert "private" not in output.out
    assert output.err == ""


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
