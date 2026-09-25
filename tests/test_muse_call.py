"""The Muse wrapper captures every invocation from its returned JSONL."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import muse_call  # noqa: E402


def _event(sequence, session_id, payload_type, payload):
    event = {"sequence": sequence, "payload_type": payload_type,
             "payload": payload}
    if session_id is not None:
        event["stream"] = {"kind": "session", "id": session_id}
    return event


def test_result_keeps_a_late_session_event_and_terminal_answer(tmp_path):
    session_id = "session-late"
    events = [
        # The stream delivers its terminal answer before an earlier sequence
        # from an asynchronously delivered result.
        _event(9, session_id, "run.terminal.completed",
               {"terminal": "completed", "text": '{"ok":true}'}),
        _event(3, session_id, "run.model.configured", {"model": "test"}),
    ]
    raw = tmp_path / "answer.jsonl"
    raw.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    assert muse_call.result(raw) == (session_id, '{"ok":true}')


def test_capture_records_an_uncaptured_call_without_losing_its_answer(tmp_path):
    raw = tmp_path / "answer.jsonl"
    answer = tmp_path / "answer.txt"
    captured = tmp_path / "capture.json"
    raw.write_text(json.dumps(_event(
        2, None, "run.terminal.completed",
        {"terminal": "completed", "text": "usable answer"},
    )))

    muse_call.capture(raw, answer, captured)

    assert answer.read_text() == "usable answer"
    assert json.loads(captured.read_text()) is None


def test_summary_counts_all_calls_and_keeps_null_capture_slots(tmp_path):
    (tmp_path / "muse-call-first").write_text('"first-session"\n')
    (tmp_path / "muse-call-later.b").write_text("null\n")
    (tmp_path / "muse-call-later.a").write_text('"third-session"\n')

    assert muse_call.summarize(tmp_path) == {
        "session_ids": ["first-session", "third-session", None],
        "calls_made": 3,
    }


def test_summary_is_absent_when_no_muse_call_was_made(tmp_path):
    assert muse_call.summarize(tmp_path) is None


def test_summary_keeps_the_historical_shape_for_one_bound_call(tmp_path):
    (tmp_path / "muse-call-first").write_text('"bound-session"\n')

    assert muse_call.summarize(tmp_path, "bound-session") is None
    assert muse_call.summarize(tmp_path) == {
        "session_ids": ["bound-session"], "calls_made": 1,
    }
