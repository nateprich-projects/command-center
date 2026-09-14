"""The routine literal is stable and drift is visible without stopping work."""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
import usage  # noqa: E402


@pytest.fixture(autouse=True)
def _bindings_never_touch_the_real_spool(monkeypatch):
    """`begin` now writes a binding record through the heartbeat (#497). The
    subprocess stubs in these tests cover the push but not the local spool, so
    stub the writer itself; tests that care patch it again explicitly."""
    import heartbeat

    monkeypatch.setattr(heartbeat, "record_binding", lambda *args, **kwargs: "pushed")
    monkeypatch.setattr(funnel, "finished_by_comments", lambda items: set())
    monkeypatch.setattr(funnel, "reconcile_orphaned_starts", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        funnel,
        "implementation_packet",
        lambda repo, number, agent: {"repo": repo, "ticket": {"number": number}},
    )


NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
ACTUAL_SHA = "0b3c1a4822c616944742e306fbfe4130f8e1206527a278f392c2d2042f0cfc40"


@pytest.fixture(params=("claude", "zcode"))
def checked_in_routine(request):
    path = funnel.routine_path(request.param)
    command = next(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if "funnel.py begin" in line
    )
    match = re.search(r"--routine-sha\s+([0-9a-f]{64})(?:\s|$)", command)
    assert match, "{} must pin a 64-character routine sha".format(path)
    return path, match.group(1)


def test_checked_in_routine_literals_match_normalized_hash(checked_in_routine):
    path, literal = checked_in_routine

    assert literal == funnel.routine_sha(path)


def _allow_begin(monkeypatch):
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: {"over_pace": False},
    )
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: {})
    monkeypatch.setattr(funnel, "read_lock", lambda item: None)


def _routine(tmp_path, body, filename="zcode.md"):
    routines = tmp_path / "routines"
    routines.mkdir(parents=True)
    path = routines / filename
    path.write_text(body)
    return path


def _codex_ticket():
    return funnel.Item(
        repo="nateprich/example",
        number=19,
        title="Ticket 19",
        url="https://github.com/nateprich/example/issues/19",
        state="OPEN",
        body="Risk: standard",
        parent="nateprich/example#18",
        item_id="item-19",
    )


def test_routine_sha_is_unchanged_by_its_own_literal(tmp_path):
    body = """# routine

python3 funnel.py begin --agent zcode --tier standard
"""
    plain = _routine(tmp_path, body)
    with_literal = _routine(
        tmp_path / "literal",
        body.replace(
            "--tier standard",
            "--tier standard --routine-sha deadbeef",
        ),
    )

    assert funnel.routine_sha(plain) == funnel.routine_sha(with_literal)


def _replace_sha_characters(value, replacements):
    chars = list(value)
    for index, replacement in replacements.items():
        chars[index] = replacement
    return "".join(chars)


@pytest.mark.parametrize(
    ("literal", "expected"),
    (
        (ACTUAL_SHA, "ok"),
        ("  {}  ".format(ACTUAL_SHA.upper()), "ok"),
        (_replace_sha_characters(ACTUAL_SHA, {25: "e"}), "mismatch"),
        (_replace_sha_characters(ACTUAL_SHA, {25: "e", 26: "3"}), "mismatch"),
        (
            _replace_sha_characters(ACTUAL_SHA, {0: "f", 10: "a", 20: "d"}),
            "drift",
        ),
        (ACTUAL_SHA[:16], "ok"),
        (_replace_sha_characters(ACTUAL_SHA[:16], {5: "f"}), "mismatch"),
        ("f5c52a8712e10c85319a8fec6715a020ed23d4a4ce33ce2b7508269f75570a88", "drift"),
    ),
)
def test_routine_sha_classifier_has_exact_near_miss_and_drift_states(
    literal, expected
):
    assert funnel.routine_sha_status(literal, ACTUAL_SHA) == expected


@pytest.mark.parametrize(
    "literal",
    (
        "0b3c1a4822c616944742e306feebf4130f8e1206527a278f392c2d2042f0cfc40",
        "0b3c1a4822c616944742e306fe4130f8e1206527a278f392c2d2042f0cfc40",
        "0b3c1a4822c616944742e306fe3fbfe4130f8e1206527a278f392c2d2042f0cfc40",
    ),
)
def test_observed_prompt_literal_replays_are_near_misses(literal):
    assert funnel.routine_sha_status(literal, ACTUAL_SHA) == "mismatch"


@pytest.mark.parametrize(
    ("agent", "filename"),
    (("zcode", "zcode.md"), ("codex", "codex-work.md")),
)
def test_matching_routine_sha_reports_ok_and_does_not_record_drift(
    tmp_path, monkeypatch, capsys, agent, filename
):
    path = _routine(
        tmp_path,
        "python3 funnel.py begin --agent {} --tier standard\n".format(agent),
        filename,
    )
    monkeypatch.setattr(funnel, "CHECKOUT_ROOT", tmp_path)
    _allow_begin(monkeypatch)
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    items = []
    expected_do = "review"
    if agent == "codex":
        ticket = _codex_ticket()
        items = [ticket]
        expected_do = "ticket"
        monkeypatch.setattr(funnel, "clear_satisfied_blocks", lambda *args, **kwargs: [])
        monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
        monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: ticket)
        monkeypatch.setattr(funnel, "claim_ticket", lambda *args, **kwargs: None)
    events = []
    monkeypatch.setattr(
        heartbeat,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    assert funnel.cmd_begin(
        items, NOW, agent, "standard", False,
        routine_sha_literal=funnel.routine_sha(path),
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["routine_sha"]["status"] == "ok"
    assert result["routine_sha"]["actual"] == result["routine_sha"]["expected"]
    assert result["do"] == expected_do
    if agent == "codex":
        assert result["work"]["ref"] == ticket.ref
    else:
        assert result["work"] == work
    assert events == []


@pytest.mark.parametrize(
    ("agent", "filename"),
    (("zcode", "zcode.md"), ("codex", "codex-work.md")),
)
@pytest.mark.parametrize("near_miss", (False, True))
def test_mismatching_routine_sha_records_the_matching_event_and_keeps_working(
    tmp_path, monkeypatch, capsys, agent, filename, near_miss
):
    path = _routine(
        tmp_path,
        "python3 funnel.py begin --agent {} --tier standard\n".format(agent),
        filename,
    )
    monkeypatch.setattr(funnel, "CHECKOUT_ROOT", tmp_path)
    _allow_begin(monkeypatch)
    work = {"pr": 8, "repo": "nateprich/gamma", "ref": "nateprich/gamma#20"}
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    items = []
    expected_do = "review"
    if agent == "codex":
        ticket = _codex_ticket()
        items = [ticket]
        expected_do = "ticket"
        monkeypatch.setattr(funnel, "clear_satisfied_blocks", lambda *args, **kwargs: [])
        monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
        monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: ticket)
        monkeypatch.setattr(funnel, "claim_ticket", lambda *args, **kwargs: None)
    events = []
    monkeypatch.setattr(
        heartbeat,
        "record_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    literal = "0" * 64
    expected_status = "drift"
    expected_event = "prompt-drift"
    if near_miss:
        actual = funnel.routine_sha(path)
        replacement = "0" if actual[25] != "0" else "1"
        literal = actual[:25] + replacement + actual[26:]
        expected_status = "mismatch"
        expected_event = "prompt-mismatch"

    assert funnel.cmd_begin(
        items, NOW, agent, "standard", False,
        routine_sha_literal=literal,
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["routine_sha"]["status"] == expected_status
    assert result["routine_sha"]["actual"] == funnel.routine_sha(path)
    assert result["do"] == expected_do
    if agent == "codex":
        assert result["work"]["ref"] == ticket.ref
    else:
        assert result["work"] == work
    assert len(events) == 1
    assert events[0][0] == (agent, "run-id", expected_event)
    assert events[0][1]["routine_sha"] == result["routine_sha"]


def test_prompt_drift_event_is_non_terminal(monkeypatch):
    records = []
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append((agent, record)) or "pushed",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert heartbeat.record_event("zcode", "run-id", "prompt-drift") == "pushed"
    assert records[0][0] == "zcode"
    assert records[0][1]["run"] == "run-id"
    assert records[0][1]["phase"] == "event"
    assert records[0][1]["outcome"] == "prompt-drift"


def test_prompt_mismatch_is_a_valid_non_terminal_event(monkeypatch):
    records = []
    monkeypatch.setattr(
        heartbeat,
        "append",
        lambda agent, record: records.append((agent, record)) or "pushed",
    )
    monkeypatch.setattr(heartbeat, "_report", lambda kept: None)

    assert "prompt-mismatch" in heartbeat.OUTCOMES
    assert heartbeat.record_event("codex", "run-id", "prompt-mismatch") == "pushed"
    assert records[0][1]["phase"] == "event"
    assert records[0][1]["outcome"] == "prompt-mismatch"
