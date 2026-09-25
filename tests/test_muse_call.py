import json
import pathlib

import muse_call


def _event(payload_type, session_id=None, *, sequence, payload=None):
    stream = {"kind": "session"}
    if session_id is not None:
        stream["id"] = session_id
    return {
        "record_type": "event",
        "payload_type": payload_type,
        "sequence": sequence,
        "stream": stream,
        "payload": payload or {},
    }


def _write_events(path, events):
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def test_result_reads_id_and_answer_when_async_event_arrives_late(tmp_path):
    raw = tmp_path / "call.jsonl"
    _write_events(raw, [
        _event(
            "run.terminal.completed", "session-a", sequence=2,
            payload={"terminal": "completed", "text": "answer"},
        ),
        _event("run.background.result", "session-a", sequence=3),
        _event("run.model.configured", "session-a", sequence=1),
    ])

    assert muse_call.result(raw) == ("session-a", "answer")


def test_capture_and_summary_keep_uncaptured_calls_in_the_count(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    raw = run_dir / "first.jsonl"
    answer = run_dir / "first.answer"
    first_capture = run_dir / "muse-call-first"
    _write_events(raw, [
        _event(
            "run.terminal.completed", sequence=1,
            payload={"terminal": "completed", "text": "first answer"},
        ),
    ])
    muse_call.capture(raw, answer, first_capture)
    (run_dir / "muse-call-later.000001").write_text('"session-b"\n')
    (run_dir / "muse-call-later.000002").write_text('"session-c"\n')

    assert answer.read_text(encoding="utf-8") == "first answer"
    assert muse_call.summarize(run_dir) == {
        "session_ids": [None, "session-b", "session-c"],
        "calls_made": 3,
    }


def test_single_call_summary_has_one_id_and_count(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "muse-call-first").write_text('"session-one"\n')

    assert muse_call.summarize(run_dir) == {
        "session_ids": ["session-one"],
        "calls_made": 1,
    }


def test_summary_reads_first_call_fallback_marker(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "muse-call-first.killed.ABC123").write_text('"session-one"\n')
    (run_dir / "muse-call-later.ABC124").write_text("null\n")

    assert muse_call.summarize(run_dir) == {
        "session_ids": ["session-one", None],
        "calls_made": 2,
    }


def test_capture_preserves_known_first_id_when_result_omits_it(tmp_path):
    raw = tmp_path / "call.jsonl"
    answer = tmp_path / "answer.txt"
    capture = tmp_path / "muse-call-first"
    _write_events(raw, [
        _event(
            "run.terminal.completed", sequence=1,
            payload={"terminal": "completed", "text": "answer"},
        ),
    ])
    capture.write_text('"session-known"\n', encoding="utf-8")

    muse_call.capture(raw, answer, capture)

    assert answer.read_text(encoding="utf-8") == "answer"
    assert json.loads(capture.read_text(encoding="utf-8")) == "session-known"
