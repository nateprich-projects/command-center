#!/usr/bin/env python3
"""Watch one Muse implementation run for a safe early handoff."""

from __future__ import annotations

import errno
import json
import math
import os
import pathlib
import sys
import time
from typing import Dict, Optional, Set


def _reject_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant: {}".format(value))


def _valid_answer(path: pathlib.Path) -> bool:
    try:
        answer = json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_constant
        )
    except (OSError, UnicodeError, TypeError, ValueError):
        return False
    return isinstance(answer, dict)


def _event_sequence(event: Dict, session_id: str) -> Optional[int]:
    if event.get("record_type") != "event":
        return None
    stream = event.get("stream")
    if not isinstance(stream, dict) or stream.get("kind") != "session" \
            or stream.get("id") != session_id:
        return None
    sequence = event.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return sequence


def _record(line: bytes, session_id: str,
            configured: Set[int], completed: Set[int]) -> None:
    try:
        event = json.loads(line.decode("utf-8"))
    except (UnicodeError, TypeError, ValueError):
        return
    if not isinstance(event, dict):
        return
    sequence = _event_sequence(event, session_id)
    if sequence is None:
        return
    # Muse JSONL acknowledges this invocation with model.configured. A
    # completed terminal must follow it on the same runner-issued session.
    if event.get("payload_type") == "run.model.configured":
        configured.add(sequence)
    elif event.get("payload_type") == "run.terminal.completed":
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("terminal") == "completed":
            completed.add(sequence)


def _paired(configured: Set[int], completed: Set[int]) -> bool:
    return any(start < end for start in configured for end in completed)


def watch(raw_path: pathlib.Path, session_id: str, answer_path: pathlib.Path,
          pid: int, timeout_seconds: float) -> int:
    """Return 0 for valid completion, 1 on normal exit, 2 at the time bound."""
    if pid <= 0 or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        return 3
    configured: Set[int] = set()
    completed: Set[int] = set()
    pending = b""
    deadline = time.monotonic() + timeout_seconds
    try:
        with raw_path.open("rb") as raw:
            while True:
                chunk = raw.read()
                if chunk:
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        if session_id:
                            _record(line, session_id, configured, completed)
                if session_id and _paired(configured, completed) \
                        and _valid_answer(answer_path):
                    return 0
                try:
                    os.kill(pid, 0)
                except OSError as exc:
                    if exc.errno == errno.ESRCH:
                        return 1
                    if exc.errno != errno.EPERM:
                        return 3
                if time.monotonic() >= deadline:
                    return 2
                time.sleep(0.2)
    except OSError:
        # Do not trust an unreadable stream for early handoff. Keep the bound
        # active so the runner can still release and finish the timed-out run.
        while True:
            try:
                os.kill(pid, 0)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return 1
                if exc.errno != errno.EPERM:
                    return 3
            if time.monotonic() >= deadline:
                return 2
            time.sleep(0.2)


def main(argv):
    if len(argv) == 6 and argv[0] == "watch":
        try:
            pid = int(argv[4])
            timeout = float(argv[5])
        except ValueError:
            return 3
        return watch(pathlib.Path(argv[1]), argv[2], pathlib.Path(argv[3]),
                     pid, timeout)
    print("usage: muse_implement_handoff.py watch RAW SESSION_ID ANSWER PID TIMEOUT",
          file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
