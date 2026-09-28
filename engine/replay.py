#!/usr/bin/env python3
"""Replay a private review packet through the live review engine.

Each replay runs ``scripts/muse-review-engine``'s replay entry from this
checkout: the same lister, judges and derived answer the review lanes run,
on the given packet and routine, recording nothing (#1730). Replay used to
make one max call over the routine and packet, and on a large packet that
call went silent past Muse's stream-idle limit where the lanes' split would
not have (#1698).

Packet paths are relative to Command Center's documented runtime root unless
absolute. The model sees the routine and packet; stdout carries verdicts and
failed part names only. Of the engine's own output, only its fixed-format
timing lines are passed on, to stderr, so a failed replay can be diagnosed
without any packet content leaving the run (#1784).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from typing import Optional, Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import agent_health  # noqa: E402
import funnel  # noqa: E402
import usage  # noqa: E402
from engine import review_apply  # noqa: E402


DEFAULT_RUNS = 3
EXPECTED_VERDICTS = ("approved", "rejected")
RUNTIME_ROOT = pathlib.Path(funnel.CLAUDE_DIR)
#: The checkout this module runs from. The engine runs from it with
#: MUSE_REVIEW_ENGINE_REPO pointing at it, so a branch replays with the
#: branch's engine, routine and judges rather than the maintained clone's.
CHECKOUT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = CHECKOUT / "scripts" / "muse-review-engine"
ROUTINE_DEFAULT = (
    pathlib.Path(__file__).resolve().parents[1]
    / "routines" / "muse-review.md"
)
#: One engine timing line, exactly as `log_call_timing` in
#: scripts/muse-review-engine writes it (#1719):
#:
#:   muse-review-engine: timing judge.3 elapsed=412s calls=2 outcome=failed
#:
#: The part is the call's directory in the engine's run directory, and only
#: the names the engine gives those are accepted: `judge.N` for a judge
#: chunk, `shape.framer`, `shape.sibling.N`, `shape.decider.N` and
#: `shape.auditor` for the shape parts, and `lister`. The whole line must
#: match, case and all: the engine's other stderr lines can quote a failing
#: judge's diagnostic, which can quote a requirement drawn from a private
#: ticket, and a line that merely contains a timing line is one of those
#: (#1784). So a line that only looks like a timing line can carry nothing
#: but digits.
TIMING_LINE = re.compile(
    r"muse-review-engine: timing "
    r"(?P<part>judge\.[0-9]{1,4}"
    r"|shape\.(?:framer|auditor|(?:sibling|decider)\.[0-9]{1,4})"
    r"|lister) "
    r"elapsed=(?P<elapsed>[0-9]{1,9})s "
    r"calls=(?P<calls>[0-9]{1,4}) "
    r"outcome=(?P<outcome>done|retried-done|failed)"
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


def score_answer(raw: str) -> Optional[str]:
    """Score one answer with the same parser and decision rule as review-apply.

    ``None`` marks an answer review-apply would refuse. The engine derives
    every answer in code, so a replay fails on one rather than guessing.
    """
    try:
        parsed = review_apply.parse_answer(raw)
    except review_apply.AnswerError:
        return None
    verdict, _blocking, _note = review_apply.decide(parsed)
    return verdict


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

    # `begin` reserves one Muse session per lane run, and each replay is one
    # engine run: its lister, judges and their retries are that run's calls,
    # as they are a lane's. So apply the same per-run reserve once per
    # replay, N times in all, before the first engine run starts (#1730).
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


def timing_lines(stderr: bytes) -> list[dict]:
    """The engine's timing lines in its stderr, parsed; every other line dropped.

    Split on newlines only, as the engine writes them: a line that holds a
    timing line after a carriage return is still one line, and dropped.
    """
    found = []
    for line in stderr.decode("utf-8", "replace").split("\n"):
        match = TIMING_LINE.fullmatch(line)
        if match:
            found.append(match.groupdict())
    return found


def _pass_timing_lines(stderr: bytes, run: int) -> list[str]:
    """Print this run's timing lines to stderr; return its failed parts.

    Each line is rebuilt from the parsed fields rather than echoed, so
    nothing but a part name, two numbers and an outcome can reach the
    output (#1784).
    """
    failed_parts = []
    for timing in timing_lines(stderr):
        print("review-replay: run {}: timing {part} elapsed={elapsed}s "
              "calls={calls} outcome={outcome}".format(run, **timing),
              file=sys.stderr)
        if timing["outcome"] == "failed":
            failed_parts.append(timing["part"])
    return failed_parts


def _report_failed_run(run: int, status: int, failed_parts: Sequence[str],
                       why: str = "") -> None:
    """One stderr line for a run that failed the replay: its exit status,
    why when the engine exited 0, and its failed parts by name (#1784)."""
    print("review-replay: run {} failed: exit status {}{}; failed parts: {}"
          .format(run, status, ", " + why if why else "",
                  ", ".join(failed_parts) or "none"),
          file=sys.stderr)


def _engine_run(packet_path: pathlib.Path, routine_path: pathlib.Path, *,
                runtime_root: pathlib.Path, run: int = 1
                ) -> tuple[str, list[str]]:
    """Run the engine's replay entry once; return its answer and failed parts.

    The answer path sits in a fresh owner-only directory under the runtime
    root, and the engine refuses a path that already exists, so what is read
    back can only be this run's. Its stdout and stderr are captured and
    dropped: the stderr of a failing judge can quote a requirement drawn from
    a private ticket. The one exception is the engine's timing lines, which
    name parts and outcomes only; they go to stderr under the run number, so
    a failed replay says which part failed (#1784). A non-zero exit or a
    missing answer is a failed replay, reported with its exit status.
    """
    try:
        with tempfile.TemporaryDirectory(
                prefix="command-center-review-replay-", dir=runtime_root) as tmp:
            answer_path = pathlib.Path(tmp) / "answer.json"
            env = dict(
                os.environ,
                MUSE_REVIEW_ENGINE_REPO=str(CHECKOUT),
                MUSE_REVIEW_ENGINE_REPLAY_PACKET=str(packet_path),
                MUSE_REVIEW_ENGINE_REPLAY_ROUTINE=str(routine_path),
                MUSE_REVIEW_ENGINE_REPLAY_ANSWER=str(answer_path),
            )
            # The escalated tier at max, which is Muse's whatever the z.ai
            # cutoff says; /bin/bash because launchd runs the lanes with it.
            result = subprocess.run(
                ["/bin/bash", str(ENGINE), "escalated", "max"],
                env=env, stdin=subprocess.DEVNULL, capture_output=True,
                check=False,
            )
            failed_parts = _pass_timing_lines(result.stderr, run)
            if result.returncode != 0:
                _report_failed_run(run, result.returncode, failed_parts)
                raise ReplayError("the review engine failed")
            try:
                return answer_path.read_text(encoding="utf-8"), failed_parts
            except OSError as exc:
                _report_failed_run(run, 0, failed_parts, "no answer")
                raise ReplayError("the review engine wrote no answer") from exc
            except UnicodeError as exc:
                _report_failed_run(run, 0, failed_parts, "unreadable answer")
                raise ReplayError(
                    "the review engine wrote an unreadable answer") from exc
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ReplayError("the review engine could not run") from exc


def replay(packet: str | pathlib.Path, routine: str | pathlib.Path, *,
           runs: int = DEFAULT_RUNS, expected: str,
           runtime_root: str | pathlib.Path = RUNTIME_ROOT) -> dict:
    """Replay one packet N times and return verdict-only summary data.

    Any run that fails, or leaves an answer review-apply would refuse, fails
    the whole replay with no verdicts: a lister that went silent records no
    verdict on a live PR either, and counting it as rejected would turn that
    stall into a plausible-looking must-approve failure (#1698).

    A run can also finish with a part failed: a judge chunk that failed
    closed reads `unsure`, and so rejected. ``failed_parts`` lists each
    run's by name, in run order beside ``verdicts``, so a surprising verdict
    says which part produced it (#1784).
    """
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise ReplayError("runs must be a positive integer")
    if expected not in EXPECTED_VERDICTS:
        raise ReplayError("expected verdict must be approved or rejected")
    check_budget(runs)
    root = pathlib.Path(runtime_root).expanduser().resolve()
    packet_path = resolve_packet_path(packet, root)
    routine_path = resolve_routine_path(routine)

    verdicts = []
    failed_parts = []
    for run in range(1, runs + 1):
        raw, failed = _engine_run(packet_path, routine_path,
                                  runtime_root=root, run=run)
        verdict = score_answer(raw)
        if verdict is None:
            _report_failed_run(run, 0, failed, "unscorable answer")
            raise ReplayError("the review engine wrote an unscorable answer")
        verdicts.append(verdict)
        failed_parts.append(failed)
    return {
        "expected": expected,
        "verdicts": verdicts,
        "failed_parts": failed_parts,
        "pass": all_match(verdicts, expected),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a private review packet and print only its "
                    "verdicts and failed part names")
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
