#!/usr/bin/env python3
"""Replay a private review packet through the read-only review path.

Packet paths are relative to Command Center's documented runtime root unless
absolute. The model sees the routine and packet; stdout contains verdicts only.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Callable, Optional, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import agent_health  # noqa: E402
import funnel  # noqa: E402
import muse_call  # noqa: E402
import muse_model  # noqa: E402
import usage  # noqa: E402
from engine import review_apply  # noqa: E402


DEFAULT_RUNS = 3
EXPECTED_VERDICTS = ("approved", "rejected")
REPO_REF = "nateprich-projects/command-center"
RUNTIME_ROOT = pathlib.Path(funnel.CLAUDE_DIR)
ROUTINE_DEFAULT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "routines" / "muse-review.md"
)


class ReplayError(ValueError):
    """A replay could not safely produce a verdict."""


def resolve_packet_path(packet: str | pathlib.Path,
                        runtime_root: str | pathlib.Path = RUNTIME_ROOT
                        ) -> pathlib.Path:
    """Resolve a packet inside the runtime root and require owner-only data."""
    root = pathlib.Path(runtime_root).expanduser().resolve()
    candidate = pathlib.Path(packet).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReplayError(
            "packet must be a readable file under the runtime root"
        ) from exc
    if not path.is_file():
        raise ReplayError(
            "packet must be a readable file under the runtime root"
        )
    try:
        if path.stat().st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
            raise ReplayError("packet and its directory must be owner-only")
    except OSError as exc:
        raise ReplayError(
            "packet must be a readable file under the runtime root"
        ) from exc
    return path


def resolve_routine_path(routine: str | pathlib.Path) -> pathlib.Path:
    """Resolve a routine from the current checkout or an explicit path."""
    path = pathlib.Path(routine).expanduser()
    if not path.is_absolute():
        path = pathlib.Path(__file__).resolve().parents[1] / path
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ReplayError("routine must be a readable file")
    except OSError as exc:
        raise ReplayError("routine must be a readable file") from exc
    return path


def render_prompt(packet_path: pathlib.Path,
                  routine_path: pathlib.Path) -> str:
    """Substitute the JSON packet into the routine's agent-facing prompt."""
    try:
        raw_packet = packet_path.read_text(encoding="utf-8")
        packet = json.loads(raw_packet)
        routine = routine_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReplayError("could not read replay inputs") from exc
    if not isinstance(packet, dict):
        raise ReplayError("packet must contain a JSON object")

    lines = routine.splitlines()
    try:
        separator = lines.index("---")
    except ValueError as exc:
        raise ReplayError("routine must contain an agent prompt after ---") from exc
    template = "\n".join(lines[separator + 1:])
    if not template.strip() or template.count("PACKET_JSON") != 1:
        raise ReplayError("routine must contain one PACKET_JSON placeholder")
    return template.replace("PACKET_JSON", raw_packet, 1)


def score_answer(raw: str) -> Optional[str]:
    """Score one answer with the same parser and decision rule as review-apply.

    ``None`` marks a malformed answer so the caller can make the one retry
    allowed by the live review path.
    """
    try:
        parsed = review_apply.parse_answer(raw)
    except review_apply.AnswerError:
        return None
    verdict, _blocking, _note = review_apply.decide(parsed)
    return verdict


def replay_one(prompt: str, call_model: Callable[[str], str]) -> str:
    """Make one replay, with the live path's retry for malformed output."""
    raw = call_model(prompt)
    verdict = score_answer(raw)
    if verdict is not None:
        return verdict

    retry = (
        prompt.rstrip()
        + "\n\nYour previous answer could not be parsed. Reply again with "
        "exactly one valid JSON object and nothing else."
    )
    verdict = score_answer(call_model(retry))
    # A malformed final answer fails closed as rejected in review-apply.
    return verdict or "rejected"


def all_match(verdicts: Sequence[str], expected: str) -> bool:
    """A known verdict passes only when every replay matches it."""
    return bool(verdicts) and all(verdict == expected for verdict in verdicts)


def check_budget(runs: int, now: Optional[float] = None) -> None:
    """Apply the shared Muse quota hold and pace brake before model calls."""
    current = time.time() if now is None else now
    hold_until = agent_health.quota_hold_until()
    if hold_until is not None and hold_until > current:
        raise ReplayError("Muse provider quota is held")

    reading = usage.read_agent("muse", current)
    if reading is None:
        raise ReplayError("Muse budget could not be read")

    windows = reading.get("windows")
    seven_day = windows.get("seven_day") if isinstance(windows, dict) else None
    if not isinstance(seven_day, dict):
        raise ReplayError("Muse budget could not be read")

    # `begin` reserves one Muse session. A replay reserves one per model call,
    # so apply the same per-session reserve N times before starting any calls.
    reading_for_replays = dict(reading)
    windows_for_replays = dict(windows)
    seven_for_replays = dict(seven_day)
    policy = dict(seven_for_replays.get("policy") or {})
    base_reserve = policy.get(
        "weekly_reserve",
        usage.policy("meta", "weekly_reserve", usage.WEEKLY_RESERVE),
    )
    policy["weekly_reserve"] = float(base_reserve) * runs
    seven_for_replays["policy"] = policy
    windows_for_replays["seven_day"] = seven_for_replays
    reading_for_replays["windows"] = windows_for_replays

    verdict = usage.pace(reading_for_replays, current, provider="meta")
    if (not verdict.get("known") or verdict.get("over_pace")
            or verdict.get("band") in ("tight", "over")):
        raise ReplayError("Muse budget gate is closed")


def _model_call(prompt: str, *, runtime_root: pathlib.Path,
                model: str, muse_bin: str,
                timeout_seconds: int) -> str:
    """Invoke Muse with the same max-effort, no-tools flags as live review."""
    try:
        with tempfile.TemporaryDirectory(
                prefix="command-center-review-replay-", dir=runtime_root) as tmp:
            private_dir = pathlib.Path(tmp)
            prompt_path = private_dir / "prompt.md"
            raw_path = private_dir / "muse.jsonl"
            workspace = private_dir / "workspace"
            workspace.mkdir(mode=0o700)
            prompt_path.write_text(prompt, encoding="utf-8")
            prompt_path.chmod(0o600)
            command = [
                muse_bin, "exec", "--model", model, "--json",
                "--reasoning-effort", "max",
                "--disable-shell", "--disable-write", "--disable-web-tools",
                "--max-model-steps", "60", "--no-foreign-personal-context",
                "--workspace", str(workspace), "--prompt-file", str(prompt_path),
            ]
            result = subprocess.run(
                command, capture_output=True, text=True, check=False,
                timeout=timeout_seconds,
            )
            if result.returncode != 0:
                raise ReplayError("Muse call failed")
            raw_path.write_text(result.stdout, encoding="utf-8")
            raw_path.chmod(0o600)
            _session_id, answer = muse_call.result(raw_path)
            return answer
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ReplayError("Muse call failed") from exc


def replay(packet: str | pathlib.Path, routine: str | pathlib.Path, *,
           runs: int = DEFAULT_RUNS, expected: str,
           runtime_root: str | pathlib.Path = RUNTIME_ROOT,
           model: Optional[str] = None, muse_bin: Optional[str] = None,
           timeout_seconds: Optional[int] = None) -> dict:
    """Replay one packet N times and return verdict-only summary data."""
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise ReplayError("runs must be a positive integer")
    if expected not in EXPECTED_VERDICTS:
        raise ReplayError("expected verdict must be approved or rejected")
    if timeout_seconds is None:
        try:
            timeout_seconds = int(os.environ.get(
                "MUSE_REVIEW_ENGINE_BOUND_SECONDS", "1200"))
        except ValueError as exc:
            raise ReplayError("invalid review timeout configuration") from exc
    if timeout_seconds < 1:
        raise ReplayError("invalid review timeout configuration")
    # Reserve for one initial answer and the live path's one malformed-answer
    # retry per replay. Successful runs usually spend only the first call.
    check_budget(runs * 2)
    root = pathlib.Path(runtime_root).expanduser().resolve()
    packet_path = resolve_packet_path(packet, root)
    routine_path = resolve_routine_path(routine)
    prompt = render_prompt(packet_path, routine_path)
    selected_model = model or muse_model.model_for(REPO_REF)
    selected_bin = muse_bin or os.environ.get(
        "MUSE_BIN", str(pathlib.Path.home() / ".local" / "bin" / "muse"))

    def call_model(current_prompt: str) -> str:
        return _model_call(
            current_prompt, runtime_root=root, model=selected_model,
            muse_bin=selected_bin, timeout_seconds=timeout_seconds,
        )

    verdicts = [replay_one(prompt, call_model) for _ in range(runs)]
    return {
        "expected": expected,
        "verdicts": verdicts,
        "pass": all_match(verdicts, expected),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a private review packet and print verdicts only")
    parser.add_argument("packet", help="packet path relative to the runtime root")
    parser.add_argument("--routine", default=str(ROUTINE_DEFAULT),
                        help="review routine path (default: live review routine)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="number of replays (default: 3)")
    parser.add_argument("--expected-verdict", required=True,
                        choices=EXPECTED_VERDICTS,
                        help="known verdict required for every replay")
    parser.add_argument("--runtime-root", default=str(RUNTIME_ROOT),
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be a positive integer")
    try:
        summary = replay(
            args.packet, args.routine, runs=args.runs,
            expected=args.expected_verdict, runtime_root=args.runtime_root,
        )
    except ReplayError:
        print("review-replay: replay failed", file=sys.stderr)
        return 1
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
