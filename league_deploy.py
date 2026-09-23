#!/usr/bin/env python3.12
"""Deploy the-league/main to its snapshot runtime checkout."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import ff_deploy as core
import runtime_deploy


RUNTIME_ROOT_ENV = "COMMAND_CENTER_LEAGUE_RUNTIME_ROOT"
CHECKOUT_RELATIVE = Path("share/the-league/checkout")
RECORD_RELATIVE = Path("share/the-league/deploy.jsonl")
RUNTIME_NAME = "League"


def resolve_paths(runtime_root: str | Path | None = None) -> tuple[Path, Path]:
    return runtime_deploy.resolve_paths(
        runtime_root, RUNTIME_ROOT_ENV, CHECKOUT_RELATIVE, RECORD_RELATIVE,
    )


def runtime_health_check(checkout: Path) -> core.CommandResult:
    """Run the snapshot job's offline manifest check with its own venv."""
    python = checkout / ".venv" / "bin" / "python"
    version = core._run_process(
        [str(python), "scripts/check-python.py"], cwd=checkout,
        timeout=core.VERIFY_TIMEOUT_SECONDS,
    )
    if version.returncode != 0:
        return version
    return core._run_process(
        [
            str(python), "scripts/run-module.py", "lib.snapshot_io", "--verify",
            "data/fp_projections", "data/fp_snapshots",
        ],
        cwd=checkout,
        timeout=core.VERIFY_TIMEOUT_SECONDS,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll and deploy the League runtime checkout from origin/main."
    )
    parser.add_argument("--runtime-root", default=None,
                        help="shared root for the checkout and deploy record")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    checkout, record = resolve_paths(args.runtime_root)
    return runtime_deploy.tick(
        checkout, record, runtime_health_check, runtime_name=RUNTIME_NAME,
    )


if __name__ == "__main__":
    raise SystemExit(main())
