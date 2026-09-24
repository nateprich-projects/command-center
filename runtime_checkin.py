#!/usr/bin/env python3
"""Verify the deployed member runtimes without changing their checkouts."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import career_deploy
import ff_deploy
import league_deploy


POLL_INTERVAL_SECONDS = 10 * 60
# Allow one missed launchd interval before the check-in calls a poller stale.
STALE_AFTER_SECONDS = 2 * POLL_INTERVAL_SECONDS
SUCCESS_STATUSES = {"current", "deployed", "repaired"}


@dataclass(frozen=True)
class Runtime:
    name: str
    checkout: Path
    record: Path


@dataclass(frozen=True)
class Checkin:
    runtime: str
    state: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def runtimes(runtime_root: str | Path | None = None) -> tuple[Runtime, ...]:
    """Resolve the three runtime checkouts and their shared deploy records."""
    ff_checkout, _bin_root, ff_record = ff_deploy.resolve_paths(runtime_root)
    league_checkout, league_record = league_deploy.resolve_paths(runtime_root)
    career_checkout, career_record = career_deploy.resolve_paths(runtime_root)
    return (
        Runtime("FF", ff_checkout, ff_record),
        Runtime("League", league_checkout, league_record),
        Runtime("Career", career_checkout, career_record),
    )


def _latest_record(path: Path) -> Mapping[str, object]:
    """Read the final complete JSON object in an append-only JSONL log."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError("deploy record is unavailable: {}".format(exc)) from exc
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise ValueError("latest deploy record is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("latest deploy record is not an object")
        return value
    raise ValueError("deploy record is empty")


def _record_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("latest deploy record has no timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("latest deploy record has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("latest deploy record timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def _git_output(checkout: Path, *args: str) -> str:
    result = ff_deploy._run_process(["git", "-C", str(checkout), *args])
    if result.returncode != 0:
        raise ValueError("git {} failed ({})".format(args[0], result.returncode))
    return result.stdout.strip()


def _refusal(record: Mapping[str, object]) -> str:
    error = record.get("error") or record.get("error_code") or "deploy refused"
    return "recorded refusal: {}".format(str(error))


def check_runtime(runtime: Runtime, *, now: datetime | None = None) -> Checkin:
    """Check the latest deploy result, its checkout head, and current main.

    The only Git operations are ``rev-parse`` and ``ls-remote``. In
    particular, check-in never fetches, merges, resets, or pulls a runtime.
    """
    try:
        record = _latest_record(runtime.record)
    except ValueError as exc:
        return Checkin(runtime.name, "drift", str(exc))

    status = record.get("status")
    if status == "refused":
        return Checkin(runtime.name, "refusal", _refusal(record))
    if status not in SUCCESS_STATUSES:
        error = record.get("error") or record.get("error_code")
        detail = "recorded deploy status is {}".format(status or "missing")
        if error:
            detail += ": {}".format(error)
        return Checkin(runtime.name, "drift", detail)

    verify = record.get("verify_result")
    if not isinstance(verify, dict) or verify.get("status") != "passed":
        return Checkin(runtime.name, "drift", "latest deploy verification did not pass")

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("now must include a timezone")
    try:
        stamp = _record_time(record.get("timestamp"))
    except ValueError as exc:
        return Checkin(runtime.name, "drift", str(exc))
    age = (current_time.astimezone(timezone.utc) - stamp).total_seconds()
    if age < -60:
        return Checkin(runtime.name, "drift", "latest deploy record is dated in the future")
    if age > STALE_AFTER_SECONDS:
        return Checkin(
            runtime.name, "drift",
            "deploy poller is stale; latest record is {:.0f} minutes old".format(
                age / 60.0),
        )

    recorded_head = record.get("checkout_head_after")
    recorded_main = record.get("main_head")
    if not isinstance(recorded_head, str) or not recorded_head:
        return Checkin(runtime.name, "drift", "latest deploy record has no checkout head")
    if not isinstance(recorded_main, str) or not recorded_main:
        return Checkin(runtime.name, "drift", "latest deploy record has no main head")

    try:
        checkout_head = _git_output(runtime.checkout, "rev-parse", "HEAD")
    except ValueError as exc:
        return Checkin(runtime.name, "drift", str(exc))
    if checkout_head != recorded_head:
        return Checkin(
            runtime.name, "drift",
            "checkout head {} differs from recorded head {}".format(
                checkout_head[:12], recorded_head[:12]),
        )

    try:
        remote = _git_output(
            runtime.checkout, "ls-remote", "--heads", "origin", "refs/heads/main",
        )
    except ValueError as exc:
        return Checkin(runtime.name, "drift", "could not read origin/main: {}".format(exc))
    remote_head = remote.split()[0] if remote.split() else ""
    if remote_head != recorded_main:
        return Checkin(
            runtime.name, "drift",
            "poller head {} differs from origin/main {}".format(
                recorded_main[:12], remote_head[:12] or "unavailable"),
        )
    if checkout_head != remote_head:
        return Checkin(
            runtime.name, "drift",
            "checkout head {} is behind origin/main {}".format(
                checkout_head[:12], remote_head[:12]),
        )

    return Checkin(runtime.name, "ok", "current at {}".format(checkout_head[:12]))


def verify_all(items: Sequence[Runtime] | None = None, *,
               now: datetime | None = None) -> tuple[Checkin, ...]:
    return tuple(
        check_runtime(runtime, now=now)
        for runtime in (items if items is not None else runtimes())
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify deployed runtime checkouts without changing them.",
    )
    parser.add_argument(
        "--runtime-root", default=None,
        help="shared root overriding the runtime checkout and record paths",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    results = verify_all(runtimes(args.runtime_root))
    for result in results:
        print("{}: {} — {}".format(result.runtime, result.state, result.detail))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
