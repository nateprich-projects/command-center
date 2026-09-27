"""Private-packet review replay stays verdict-only and fail-closed."""

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


def test_render_prompt_substitutes_the_packet_without_emitting_it(tmp_path):
    packet = private_packet(tmp_path, body="private diff text")
    prompt = replay.render_prompt(packet, routine(tmp_path))

    assert prompt.startswith("Judge this packet:")
    assert '"private diff text"' in prompt
    assert prompt.count("PACKET_JSON") == 0


def test_render_prompt_requires_one_placeholder(tmp_path):
    packet = private_packet(tmp_path)
    prompt_file = routine(tmp_path)
    prompt_file.write_text("---\nNo placeholder\n", encoding="utf-8")

    with pytest.raises(replay.ReplayError):
        replay.render_prompt(packet, prompt_file)


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


def test_malformed_answer_retries_once_then_fails_closed():
    prompts = []
    answers = iter(["not json", answer()])

    def call(prompt):
        prompts.append(prompt)
        return next(answers)

    assert replay.replay_one("base prompt", call) == "approved"
    assert len(prompts) == 2
    assert "could not be parsed" in prompts[1]


def test_final_malformed_answer_scores_as_rejected():
    assert replay.replay_one("base prompt", lambda _prompt: "not json") == "rejected"


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


def test_budget_gate_reserves_one_session_per_possible_call(monkeypatch):
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


def test_cli_prints_only_verdicts_and_pass_result(tmp_path, monkeypatch, capsys):
    packet = private_packet(tmp_path, body="NEVER PRINT THIS PRIVATE DIFF")
    routine_path = routine(tmp_path)
    verdicts = iter(["rejected", "rejected", "rejected"])
    monkeypatch.setattr(replay, "replay_one", lambda _prompt, _call: next(verdicts))
    monkeypatch.setattr(replay, "check_budget", lambda _runs: None)

    result = replay.main([
        str(packet), "--routine", str(routine_path), "--runs", "3",
        "--expected-verdict", "rejected", "--runtime-root", str(tmp_path),
    ])
    output = capsys.readouterr()

    assert result == 0
    assert json.loads(output.out) == {
        "expected": "rejected",
        "pass": True,
        "verdicts": ["rejected", "rejected", "rejected"],
    }
    assert "NEVER PRINT THIS PRIVATE DIFF" not in output.out
    assert "private" not in output.out
    assert output.err == ""


def test_replay_reserves_for_initial_calls_and_retries(
        tmp_path, monkeypatch):
    packet = private_packet(tmp_path)
    routine_path = routine(tmp_path)
    reservations = []
    monkeypatch.setattr(replay.agent_health, "quota_hold_until", lambda: None)
    monkeypatch.setattr(replay, "check_budget", reservations.append)
    monkeypatch.setattr(
        replay, "_model_call", lambda *_args, **_kwargs: answer())

    summary = replay.replay(
        packet, routine_path, runs=3, expected="approved",
        runtime_root=tmp_path, model="model", muse_bin="muse-test",
        timeout_seconds=5,
    )

    assert reservations == [6]
    assert summary["pass"] is True


def test_model_invocation_uses_live_max_no_tools_flags(tmp_path, monkeypatch):
    calls = []
    terminal_answer = answer()
    stream = json.dumps({
        "payload_type": "run.terminal.completed",
        "payload": {"terminal": "completed", "text": terminal_answer},
    }) + "\n"

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return replay.subprocess.CompletedProcess(command, 0, stream, "")

    monkeypatch.setattr(replay.subprocess, "run", run)

    raw = replay._model_call(
        "prompt", runtime_root=tmp_path, model="muse-spark-1.3",
        muse_bin="muse-test", timeout_seconds=5,
    )

    command, kwargs = calls[0]
    assert raw == terminal_answer
    assert command[0:5] == ["muse-test", "exec", "--model", "muse-spark-1.3", "--json"]
    assert command[command.index("--reasoning-effort") + 1] == "max"
    assert "--disable-shell" in command
    assert "--disable-write" in command
    assert "--disable-web-tools" in command
    assert command[command.index("--workspace") + 1].startswith(str(tmp_path))
    assert kwargs["timeout"] == 5


def test_cli_entry_point_exists_and_is_executable():
    entry = ROOT / "review-replay"

    assert entry.exists()
    assert entry.stat().st_mode & stat.S_IXUSR
