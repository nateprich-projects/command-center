#!/usr/bin/env python3
"""Capture each Muse exec session id and its final answer from JSONL output."""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Dict, Iterable, List, Optional, Tuple


def _events(path: pathlib.Path) -> Iterable[Tuple[int, Dict]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    found = []
    for index, line in enumerate(lines):
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            found.append((index, event))
    return found


def result(path: pathlib.Path) -> Tuple[Optional[str], str]:
    """Return the one session id and terminal answer, tolerating late events."""
    session_ids = set()
    configured_ids = set()
    terminal_text = []

    for index, event in _events(path):
        stream = event.get("stream")
        if isinstance(stream, dict) and stream.get("kind") == "session":
            candidate = stream.get("id")
            if isinstance(candidate, str) and candidate.strip():
                session_ids.add(candidate)
                if event.get("payload_type") == "run.model.configured":
                    configured_ids.add(candidate)

        if event.get("payload_type") != "run.terminal.completed":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("terminal") != "completed":
            continue
        text = payload.get("text")
        if isinstance(text, str):
            sequence = event.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                sequence = index
            terminal_text.append((sequence, index, text))

    candidates = configured_ids if len(configured_ids) == 1 else session_ids
    session_id = next(iter(candidates)) if len(candidates) == 1 else None
    answer = max(terminal_text)[2] if terminal_text else ""
    return session_id, answer


def capture(raw_path: pathlib.Path, answer_path: pathlib.Path,
            capture_path: pathlib.Path) -> None:
    """Write the plain answer and one JSON session id (null means uncaptured)."""
    session_id, answer = result(raw_path)
    answer_path.write_text(answer, encoding="utf-8")
    if session_id is None:
        # The first call's id is already known from the session bound to the
        # run. Preserve that value if its result omits the stream id; later
        # calls start with null and remain explicitly uncaptured.
        try:
            prior = json.loads(capture_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            prior = None
        if isinstance(prior, str) and prior.strip():
            session_id = prior
    capture_path.write_text(
        json.dumps(session_id, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def summarize(run_dir: pathlib.Path) -> Optional[Dict[str, object]]:
    """Collect every invocation marker, preserving failed captures as null."""
    first = run_dir / "muse-call-first"
    first_fallbacks = sorted(run_dir.glob("muse-call-first.*"))
    later = sorted(run_dir.glob("muse-call-later.*"))
    first_path = first if first.exists() else (
        first_fallbacks[0] if first_fallbacks else None
    )
    paths = ([first_path] if first_path is not None else []) + later
    if not paths:
        return None

    session_ids = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            value = None
        if value is not None and (not isinstance(value, str) or not value.strip()):
            value = None
        session_ids.append(value)
    return {"session_ids": session_ids, "calls_made": len(session_ids)}


def main(argv: List[str]) -> int:
    if len(argv) == 4 and argv[0] == "capture":
        capture(pathlib.Path(argv[1]), pathlib.Path(argv[2]),
                pathlib.Path(argv[3]))
        return 0
    if len(argv) == 2 and argv[0] == "summarize":
        record = summarize(pathlib.Path(argv[1]))
        if record is not None:
            print(json.dumps(record, separators=(",", ":"), sort_keys=True))
        return 0
    print("usage: muse_call.py capture RAW ANSWER CAPTURE | summarize RUN_DIR",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
