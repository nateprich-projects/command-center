#!/usr/bin/env python3
"""Read per-run token usage from the agents' own session transcripts.

The heartbeat quota reading is a provider meter, not a run meter.  This module
reads the usage events the harness wrote for the session that heartbeat bound to
the ticket.  It deliberately returns ``None`` when a required observation is
missing: an unknown token count must not become a zero-cost run.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


TOKEN_KINDS = (
    "fresh_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


SESSION_GLOBS = {
    "claude": "~/.claude/projects/*/*.jsonl",
    "codex": "~/.codex/sessions/*/*/*/*.jsonl",
    "zcode": "~/.zcode/cli/rollout/*.jsonl",
    "muse": "~/.local/share/muse/sessions/.msp-view-v1/*/*",
}


class SessionUsageError(RuntimeError):
    """A transcript could not be read safely."""


def _number(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _first_number(mapping: Mapping[str, object], names: Sequence[str]) -> Optional[int]:
    for name in names:
        if name in mapping:
            return _number(mapping.get(name))
    return None


def _usage(
    agent: str,
    raw: Mapping[str, object],
) -> Optional[Dict[str, Optional[int]]]:
    """Normalize one harness usage object into the four shared token kinds."""
    input_tokens = _first_number(raw, ("input_tokens", "inputTokens"))
    fresh = _first_number(raw, ("fresh_input_tokens", "freshInputTokens"))
    cache_read = _first_number(raw, (
        "cached_input_tokens", "cache_read_input_tokens", "cacheReadTokens",
        "cachedTokens",
    ))
    cache_write = _first_number(raw, (
        "cache_write_input_tokens", "cache_creation_input_tokens",
        "cacheWriteTokens", "cacheCreationInputTokens",
    ))
    output = _first_number(raw, ("output_tokens", "outputTokens"))

    # Claude exposes uncached input directly.  Codex, Muse and the retired
    # zcode reader expose total input plus cache-read input, so derive only the
    # fresh portion that the source makes observable.
    if fresh is None and input_tokens is not None:
        if agent == "claude":
            fresh = input_tokens
        elif cache_read is not None and cache_read <= input_tokens:
            fresh = input_tokens - cache_read
        elif cache_read is None:
            # A source that gives only one input number cannot tell us whether
            # it includes cached input.  Keep the kind unknown rather than
            # silently pricing the whole input as fresh.
            fresh = None

    if input_tokens is None and fresh is not None and cache_read is not None:
        input_tokens = fresh + cache_read

    return {
        "fresh_input_tokens": fresh,
        "cache_read_input_tokens": cache_read,
        "cache_write_input_tokens": cache_write,
        "output_tokens": output,
    }


def _timestamp(value: object) -> Optional[datetime]:
    if isinstance(value, datetime):
        found = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            found = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if found.tzinfo is None:
        found = found.replace(tzinfo=timezone.utc)
    return found.astimezone(timezone.utc)


def _row_timestamp(row: Mapping[str, object]) -> Optional[datetime]:
    for key in ("timestamp", "ts", "recordedAt", "recorded_at", "createdAt"):
        found = _timestamp(row.get(key))
        if found is not None:
            return found
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        for key in ("timestamp", "ts", "recordedAt", "recorded_at"):
            found = _timestamp(payload.get(key))
            if found is not None:
                return found
    return None


def _inside(
    at: Optional[datetime],
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
) -> bool:
    # Muse's journal records carry their time in an outer binary envelope, not
    # in the embedded JSON event.  A session id is already an exact boundary,
    # so timestamp-less events are accepted for that matched session.
    if at is None:
        return True
    if started_at is not None and at < started_at:
        return False
    if finished_at is not None and at > finished_at:
        return False
    return True


def _identity(agent: str, row: Mapping[str, object], usage: Mapping[str, object]) -> str:
    payload = row.get("payload")
    message = row.get("message")
    params = row.get("params")
    candidates = []
    for source in (payload, message, params, row):
        if not isinstance(source, Mapping):
            continue
        for key in (
            "turn_id", "turnId", "message_id", "messageId", "id", "uuid",
            "requestId", "record_event_id",
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
    if candidates:
        return agent + ":" + candidates[0]
    encoded = json.dumps(dict(usage), sort_keys=True, separators=(",", ":"))
    return agent + ":" + hashlib.sha256(
        ((str(_row_timestamp(row)) + ":" + encoded).encode("utf-8"))
    ).hexdigest()


def _codex_events(row: Mapping[str, object]) -> Iterable[Tuple[Mapping[str, object], Mapping[str, object]]]:
    if row.get("type") != "token_usage_record":
        return ()
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return ()
    usage = payload.get("usage")
    return ((row, usage),) if isinstance(usage, Mapping) else ()


def _claude_events(row: Mapping[str, object]) -> Iterable[Tuple[Mapping[str, object], Mapping[str, object]]]:
    if row.get("type") != "assistant":
        return ()
    message = row.get("message")
    if not isinstance(message, Mapping):
        return ()
    usage = message.get("usage")
    return ((row, usage),) if isinstance(usage, Mapping) else ()


def _jsonl_events(
    agent: str,
    path: str,
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
) -> List[Tuple[Mapping[str, object], Mapping[str, object]]]:
    found: List[Tuple[Mapping[str, object], Mapping[str, object]]] = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, Mapping):
                    continue
                candidates = (
                    _codex_events(row) if agent == "codex" else
                    _claude_events(row) if agent == "claude" else
                    ()
                )
                for event_row, raw in candidates:
                    if _inside(_row_timestamp(event_row), started_at, finished_at):
                        found.append((event_row, raw))
    except (OSError, UnicodeError):
        return []
    return found


def _balanced_json_objects(content: bytes, marker: bytes) -> Iterable[Dict[str, object]]:
    """Extract JSON-RPC objects embedded in Muse's binary journal."""
    offset = 0
    while True:
        start = content.find(marker, offset)
        if start < 0:
            return
        depth = 0
        in_string = False
        escaped = False
        end = None
        for index in range(start, len(content)):
            char = content[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == 92:  # backslash
                    escaped = True
                elif char == 34:  # quote
                    in_string = False
                continue
            if char == 34:
                in_string = True
            elif char == 123:  # left brace
                depth += 1
            elif char == 125:  # right brace
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            return
        try:
            row = json.loads(content[start:end].decode("utf-8"))
        except (UnicodeError, ValueError):
            offset = start + len(marker)
            continue
        if isinstance(row, dict):
            yield row
        offset = end


def _muse_events(path: str) -> List[Tuple[Mapping[str, object], Mapping[str, object]]]:
    try:
        with open(path, "rb") as handle:
            content = handle.read()
    except OSError:
        return []
    if path.endswith(".bin"):
        rows = _balanced_json_objects(content, b'{"method"')
        found = []
        for row in rows:
            if row.get("method") != "session/tokenUsage":
                continue
            params = row.get("params")
            usage = params.get("usage") if isinstance(params, Mapping) else None
            if isinstance(params, Mapping) and isinstance(usage, Mapping):
                found.append((params, usage))
        return found

    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, ValueError):
        return []
    found = []

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            usage = node.get("usage")
            if isinstance(usage, Mapping) and (
                "inputTokens" in usage or "outputTokens" in usage
            ):
                found.append((node, usage))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(document)
    return found


def _paths(agent: str) -> List[str]:
    pattern = os.path.expanduser(SESSION_GLOBS.get(agent, ""))
    paths = glob.glob(pattern)
    if agent == "muse":
        paths = [
            path for path in paths
            if path.endswith(".bin") or path.endswith(".json")
        ]
    return sorted(paths, key=lambda path: os.path.getmtime(path), reverse=True)


def _matches(agent: str, path: str, session_id: str) -> bool:
    if session_id not in path:
        return False
    return True


def _events(
    agent: str,
    session_id: str,
    started_at: Optional[datetime],
    finished_at: Optional[datetime],
) -> List[Tuple[Mapping[str, object], Mapping[str, object]]]:
    if not session_id:
        return []
    found = []
    for path in _paths(agent):
        if not _matches(agent, path, session_id):
            continue
        if path.endswith(".jsonl"):
            found.extend(_jsonl_events(agent, path, started_at, finished_at))
        elif agent == "muse":
            found.extend(_muse_events(path))
    return found


def usage_for_session(
    agent: str,
    session_id: Optional[str],
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> Optional[Dict[str, Optional[int]]]:
    """Return normalized token totals for one heartbeat-bound session."""
    if not session_id:
        return None
    totals = {kind: 0 for kind in TOKEN_KINDS}
    readable = {kind: True for kind in TOKEN_KINDS}
    seen = set()
    event_count = 0
    for row, raw in _events(agent, session_id, started_at, finished_at):
        usage = _usage(agent, raw)
        if usage is None:
            continue
        identity = _identity(agent, row, raw)
        if identity in seen:
            continue
        seen.add(identity)
        event_count += 1
        for kind in TOKEN_KINDS:
            value = usage.get(kind)
            if value is None:
                readable[kind] = False
            elif readable[kind]:
                totals[kind] += value
    if not event_count:
        return None
    return {
        kind: totals[kind] if readable[kind] else None
        for kind in TOKEN_KINDS
    }
