#!/usr/bin/env python3
"""Replay a private review packet through the live review engine.

Each replay runs ``scripts/muse-review-engine``'s replay entry from this
checkout: the same lister, judges and derived answer the review lanes run,
on the given packet and active versioned prompt (or an explicitly supplied
routine), recording nothing (#1730). Replay used to
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
from engine import review_packets  # noqa: E402


DEFAULT_RUNS = 3
EXPECTED_VERDICTS = ("approved", "rejected")
RUNTIME_ROOT = pathlib.Path(funnel.CLAUDE_DIR)
#: Nearest-rank p90 per Muse session from 3,992 sessions and 4,120 provider
#: usage records in the trailing 72-hour window, measured 2026-10-04 at the
#: configured model's own-card rates. Keep this scoped to the bounded review
#: evaluation; standalone replays continue to reserve a full Muse session.
REVIEW_EVALUATION_REPLAY_P90_DOLLARS = 0.00619
#: The checkout this module runs from. The engine runs from it with
#: MUSE_REVIEW_ENGINE_REPO pointing at it, so a branch replays with the
#: branch's engine, routine and judges rather than the maintained clone's.
CHECKOUT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = CHECKOUT / "scripts" / "muse-review-engine"
MAIN_POOL_SCHEMA = 1
MAIN_POOL_RELATIVE_PATH = pathlib.Path("eval-replay") / "main-pool.json"
PACKET_EXPECTED_VERDICTS = {
    "must_reject": "rejected",
    "must_approve": "approved",
}
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
PACKET_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
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


def check_budget(runs: int, now: Optional[float] = None, *,
                 replay_run_p90_dollars: Optional[float] = None,
                 trial_spent_dollars=None) -> None:
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

    evaluation_batch = (
        replay_run_p90_dollars is not None or trial_spent_dollars is not None
    )
    reading_for_replays = dict(reading)
    windows_for_replays = dict(windows)
    seven_for_replays = dict(seven_day)
    policy = dict(seven_for_replays.get("policy") or {})
    if evaluation_batch:
        replay_cost = usage._finite_nonnegative_dollars(
            replay_run_p90_dollars)
        weekly_cap = usage._finite_nonnegative_dollars(
            seven_day.get("cap_dollars"))
        used_percent = usage._finite_nonnegative_dollars(
            seven_day.get("used_percent"))
        if (replay_cost is None or replay_cost <= 0 or weekly_cap is None
                or weekly_cap <= 0 or used_percent is None):
            raise ReplayError("review evaluation budget could not be read")
        reserve_dollars = usage._finite_nonnegative_dollars(replay_cost * runs)
        if reserve_dollars is None:
            raise ReplayError("review evaluation budget could not be read")
        trial = usage.muse_trial_counter_read(
            trial_spent_dollars, current, reserve_dollars=reserve_dollars)
        if (not isinstance(trial, dict) or trial.get("known") is not True
                or trial.get("stop") is not False):
            raise ReplayError("Muse trial budget gate is closed")
        weekly_reserve = usage._finite_nonnegative_dollars(
            100.0 * reserve_dollars / weekly_cap)
        if weekly_reserve is None:
            raise ReplayError("review evaluation budget could not be read")
        policy["weekly_reserve"] = weekly_reserve
    else:
        # `begin` reserves one Muse session per lane run, and each replay is
        # one engine run: its lister, judges and retries are that run's calls,
        # as they are a lane's. Keep that full-session reserve for standalone
        # replay callers (#1730).
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


def _engine_run(packet_path: pathlib.Path,
                routine_path: Optional[pathlib.Path], *,
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
                MUSE_REVIEW_ENGINE_REPLAY_ANSWER=str(answer_path),
            )
            if routine_path is not None:
                env["MUSE_REVIEW_ENGINE_REPLAY_ROUTINE"] = str(routine_path)
            else:
                env.pop("MUSE_REVIEW_ENGINE_REPLAY_ROUTINE", None)
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


def replay(packet: str | pathlib.Path,
           routine: Optional[str | pathlib.Path] = None, *,
           runs: int = DEFAULT_RUNS, expected: str,
           runtime_root: str | pathlib.Path = RUNTIME_ROOT,
           replay_run_p90_dollars: Optional[float] = None,
           trial_spent_dollars=None) -> dict:
    """Replay one packet N times and return verdict-only summary data.

    With no explicit routine, the engine loads the active versioned reviewer
    prompt through the same adapter as a live review.

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
    if (replay_run_p90_dollars is None
            and trial_spent_dollars is None):
        check_budget(runs)
    else:
        check_budget(
            runs, replay_run_p90_dollars=replay_run_p90_dollars,
            trial_spent_dollars=trial_spent_dollars,
        )
    root = pathlib.Path(runtime_root).expanduser().resolve()
    packet_path = resolve_packet_path(packet, root)
    routine_path = (
        resolve_routine_path(routine) if routine is not None else None
    )

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


def _checkout_revision(checkout: str | pathlib.Path
                       ) -> tuple[pathlib.Path, str]:
    """Resolve a checkout and read its full HEAD commit SHA."""
    try:
        path = pathlib.Path(checkout).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ReplayError("review checkout must be a directory")
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplayError("review checkout could not be read") from exc
    commit = result.stdout.strip()
    if result.returncode != 0 or not COMMIT_SHA.fullmatch(commit):
        raise ReplayError("review checkout has no full commit SHA")
    return path, commit


def _empty_main_pool(main_commit_sha: str) -> dict:
    return {
        "schema": MAIN_POOL_SCHEMA,
        "main_commit_sha": main_commit_sha,
        "packets": {},
    }


def _main_pool_key(version: str, packet_name: str, packet_sha256: str) -> str:
    return "{}/{}/{}".format(version, packet_name, packet_sha256)


def load_main_pool(main_commit_sha: str, *,
                   runtime_root: str | pathlib.Path = RUNTIME_ROOT) -> dict:
    """Load only main-side runs recorded for this exact full main commit SHA.

    A missing, malformed, or older-SHA pool is a cache miss. The pool file
    holds only sanitized verdict records; packet contents stay in the
    versioned repository fixture and are never copied into the pool.
    """
    if not isinstance(main_commit_sha, str) or not COMMIT_SHA.fullmatch(
            main_commit_sha):
        raise ReplayError("main commit SHA must contain 40 lowercase hex digits")
    empty = _empty_main_pool(main_commit_sha)
    root = pathlib.Path(runtime_root).expanduser().resolve()
    pool_dir = root / MAIN_POOL_RELATIVE_PATH.parent
    pool_path = root / MAIN_POOL_RELATIVE_PATH
    try:
        if (pool_dir.is_symlink() or not pool_dir.is_dir()
                or pool_dir.stat().st_mode & 0o077
                or pool_path.is_symlink() or not pool_path.is_file()
                or pool_path.stat().st_mode & 0o077):
            return empty
        stored = json.loads(pool_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return empty
    if (not isinstance(stored, dict)
            or stored.get("schema") != MAIN_POOL_SCHEMA
            or stored.get("main_commit_sha") != main_commit_sha
            or not isinstance(stored.get("packets"), dict)):
        return empty

    for key, records in stored["packets"].items():
        if not isinstance(key, str) or not isinstance(records, list):
            return empty
        for record in records:
            if not isinstance(record, dict):
                return empty
            packet_name = record.get("packet")
            version = record.get("version")
            digest = record.get("packet_sha256")
            failed_parts = record.get("failed_parts")
            if (not isinstance(packet_name, str) or not packet_name
                    or not isinstance(version, str) or not version
                    or not isinstance(digest, str)
                    or not PACKET_DIGEST.fullmatch(digest)
                    or record.get("main_commit_sha") != main_commit_sha
                    or record.get("verdict") not in EXPECTED_VERDICTS
                    or not isinstance(failed_parts, list)
                    or not all(isinstance(part, str) for part in failed_parts)
                    or key != _main_pool_key(version, packet_name, digest)):
                return empty
    return stored


def _save_main_pool(pool: dict,
                    runtime_root: str | pathlib.Path = RUNTIME_ROOT) -> None:
    """Atomically replace the owner-only pool with its current main SHA."""
    root = pathlib.Path(runtime_root).expanduser().resolve()
    if not root.is_dir():
        raise ReplayError("review runtime root is unavailable")
    pool_dir = root / MAIN_POOL_RELATIVE_PATH.parent
    try:
        pool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if pool_dir.is_symlink() or not pool_dir.is_dir():
            raise ReplayError("main replay pool directory is unavailable")
        pool_dir.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".main-pool-", dir=pool_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(pool, stream, separators=(",", ":"), sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, root / MAIN_POOL_RELATIVE_PATH)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise ReplayError("main replay pool could not be written") from exc


def _run_checkout_replay(checkout: pathlib.Path, packet_path: pathlib.Path,
                         runs: int, expected: str,
                         runtime_root: pathlib.Path, *,
                         replay_run_p90_dollars: Optional[float] = None,
                         trial_spent_dollars=None) -> dict:
    """Run this checkout's replay CLI without exposing captured diagnostics."""
    entry = checkout / "engine" / "replay.py"
    if not entry.is_file():
        raise ReplayError("review checkout has no replay entry point")
    try:
        command = [
            sys.executable, str(entry), str(packet_path), "--runs",
            str(runs), "--expected-verdict", expected,
            "--runtime-root", str(runtime_root),
        ]
        if replay_run_p90_dollars is not None:
            command.extend(("--replay-run-p90-dollars",
                            str(replay_run_p90_dollars)))
        if trial_spent_dollars is not None:
            command.extend(("--trial-spent-dollars",
                            str(trial_spent_dollars)))
        result = subprocess.run(
            command,
            cwd=str(checkout), capture_output=True, text=True, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplayError("review replay could not run in a checkout") from exc
    if result.returncode != 0:
        raise ReplayError("review replay failed in a checkout")
    try:
        summary = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReplayError("review replay returned invalid output") from exc
    return summary


def _summary_runs(summary: dict, *, expected: str, count: int) -> list[dict]:
    """Validate replay output and retain only per-run verdict evidence."""
    if not isinstance(summary, dict) or summary.get("expected") != expected:
        raise ReplayError("review replay returned an unexpected summary")
    verdicts = summary.get("verdicts")
    failed_parts = summary.get("failed_parts")
    if (not isinstance(verdicts, list) or len(verdicts) != count
            or not all(verdict in EXPECTED_VERDICTS for verdict in verdicts)
            or not isinstance(failed_parts, list)
            or len(failed_parts) != count
            or not all(isinstance(parts, list)
                       and all(isinstance(part, str) for part in parts)
                       for parts in failed_parts)):
        raise ReplayError("review replay returned invalid run records")
    return [
        {"verdict": verdict, "failed_parts": list(parts)}
        for verdict, parts in zip(verdicts, failed_parts)
    ]


def replay_packet(packet_name: str, head_checkout: str | pathlib.Path,
                  main_checkout: str | pathlib.Path, *,
                  runs: int = DEFAULT_RUNS, version: str = "v1",
                  runtime_root: str | pathlib.Path = RUNTIME_ROOT,
                  head_runs: Optional[Sequence[dict]] = None,
                  replay_run_p90_dollars: Optional[float] = None,
                  trial_spent_dollars=None) -> dict:
    """Replay one frozen packet on PR head and current main.

    Main-side runs are reused only for the same 40-character main SHA. A new
    main SHA starts with an empty pool and replaces the older pool on its
    first saved run. ``head_runs`` lets a bounded evaluation pass its earlier
    samples so only the next batch is replayed on head. This helper reports
    run records only; it does not compare head and main or apply thresholds.
    """
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        raise ReplayError("runs must be a positive integer")
    if not isinstance(packet_name, str):
        raise ReplayError("versioned review packet name is invalid")
    try:
        expected = PACKET_EXPECTED_VERDICTS[packet_name]
        packet = review_packets.load_packet(packet_name, version)
        packet_sha256 = review_packets.verify_checksums(version)[packet_name]
    except (KeyError, review_packets.PacketSetError) as exc:
        raise ReplayError("versioned review packet is unavailable") from exc

    head_path, head_commit_sha = _checkout_revision(head_checkout)
    main_path, main_commit_sha = _checkout_revision(main_checkout)
    root = pathlib.Path(runtime_root).expanduser().resolve()
    if not root.is_dir():
        raise ReplayError("review runtime root is unavailable")

    packet_bytes = json.dumps(
        packet, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    with tempfile.TemporaryDirectory(
            prefix="command-center-review-packet-", dir=root) as temporary:
        packet_path = pathlib.Path(temporary) / "packet.json"
        packet_path.write_bytes(packet_bytes)
        packet_path.chmod(0o600)

        cached_head_runs = list(head_runs or [])
        if len(cached_head_runs) > runs:
            cached_head_runs = cached_head_runs[:runs]
        for record in cached_head_runs:
            if (not isinstance(record, dict)
                    or record.get("head_commit_sha") != head_commit_sha
                    or record.get("packet") != packet_name
                    or record.get("version") != version
                    or record.get("packet_sha256") != packet_sha256
                    or record.get("verdict") not in EXPECTED_VERDICTS
                    or not isinstance(record.get("failed_parts"), list)
                    or not all(isinstance(part, str)
                               for part in record["failed_parts"])):
                raise ReplayError(
                    "cached head replay runs do not match this head")
        missing_head_runs = runs - len(cached_head_runs)
        if missing_head_runs:
            budget_kwargs = {}
            if (replay_run_p90_dollars is not None
                    or trial_spent_dollars is not None):
                budget_kwargs = {
                    "replay_run_p90_dollars": replay_run_p90_dollars,
                    "trial_spent_dollars": trial_spent_dollars,
                }
            head_summary = _run_checkout_replay(
                head_path, packet_path, missing_head_runs, expected, root,
                **budget_kwargs)
            new_head_runs = _summary_runs(
                head_summary, expected=expected, count=missing_head_runs)
            cached_head_runs.extend(
                {"packet": packet_name, "version": version,
                 "packet_sha256": packet_sha256,
                 "head_commit_sha": head_commit_sha, **run}
                for run in new_head_runs
            )

        pool = load_main_pool(main_commit_sha, runtime_root=root)
        key = _main_pool_key(version, packet_name, packet_sha256)
        cached_runs = list(pool["packets"].get(key, []))
        if len(cached_runs) < runs:
            missing = runs - len(cached_runs)
            budget_kwargs = {}
            if (replay_run_p90_dollars is not None
                    or trial_spent_dollars is not None):
                budget_kwargs = {
                    "replay_run_p90_dollars": replay_run_p90_dollars,
                    "trial_spent_dollars": trial_spent_dollars,
                }
            main_summary = _run_checkout_replay(
                main_path, packet_path, missing, expected, root,
                **budget_kwargs)
            new_runs = _summary_runs(
                main_summary, expected=expected, count=missing)
            for run in new_runs:
                cached_runs.append({
                    "packet": packet_name,
                    "version": version,
                    "packet_sha256": packet_sha256,
                    "main_commit_sha": main_commit_sha,
                    **run,
                })
            pool["packets"][key] = cached_runs
            _save_main_pool(pool, runtime_root=root)

        main_runs = cached_runs[:runs]
    return {
        "packet": packet_name,
        "version": version,
        "packet_sha256": packet_sha256,
        "head": {
            "commit_sha": head_commit_sha,
            "runs": cached_head_runs,
        },
        "main": {
            "commit_sha": main_commit_sha,
            "runs": main_runs,
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay a private review packet and print only its "
                    "verdicts and failed part names")
    parser.add_argument("packet", help="packet path relative to the runtime root")
    parser.add_argument("--routine", default=None,
                        help="explicit review routine path (default: active variant)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help="number of replays (default: 3)")
    parser.add_argument("--expected-verdict", required=True,
                        choices=EXPECTED_VERDICTS,
                        help="known verdict required for every replay")
    parser.add_argument("--runtime-root", default=str(RUNTIME_ROOT),
                        help=argparse.SUPPRESS)
    parser.add_argument("--replay-run-p90-dollars", type=float,
                        default=None, help=argparse.SUPPRESS)
    parser.add_argument("--trial-spent-dollars", type=float,
                        default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be a positive integer")
    try:
        summary = replay(
            args.packet, args.routine, runs=args.runs,
            expected=args.expected_verdict, runtime_root=args.runtime_root,
            replay_run_p90_dollars=args.replay_run_p90_dollars,
            trial_spent_dollars=args.trial_spent_dollars,
        )
    except ReplayError:
        print("review-replay: replay failed", file=sys.stderr)
        return 1
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
