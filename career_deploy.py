#!/usr/bin/env python3.12
"""Deploy career-toolset/main to its nightly runtime checkout."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Sequence

import ff_deploy as core
import runtime_deploy


RUNTIME_ROOT_ENV = "COMMAND_CENTER_CAREER_RUNTIME_ROOT"
CHECKOUT_RELATIVE = Path("share/career-agent/checkout")
RECORD_RELATIVE = Path("share/career-agent/deploy.jsonl")
RUNTIME_ENTRYPOINT_RELATIVE = Path("scripts/nightly.sh")
RUNTIME_NAME = "Career"
#: Runs each ``test_*`` function in the smoke file with the runtime interpreter.
#: The nightly never imports pytest, and the Mac's Homebrew python3.12 does not
#: carry it (#1406), so the check must not depend on a test runner.
SMOKE_RUNNER = (
    "import runpy\n"
    "ns = runpy.run_path('tests/test_smoke.py')\n"
    "tests = [f for n, f in ns.items() if n.startswith('test_') and callable(f)]\n"
    "assert tests, 'tests/test_smoke.py defines no test_ functions'\n"
    "for test in tests:\n"
    "    test()\n"
)


def resolve_paths(runtime_root: str | Path | None = None) -> tuple[Path, Path]:
    return runtime_deploy.resolve_paths(
        runtime_root, RUNTIME_ROOT_ENV, CHECKOUT_RELATIVE, RECORD_RELATIVE,
    )


def runtime_health_check(checkout: Path) -> core.CommandResult:
    """Check the nightly shell and run the repo's offline smoke test."""
    nightly = core._run_process(
        ["/bin/sh", "-n", "scripts/nightly.sh"], cwd=checkout,
        timeout=core.VERIFY_TIMEOUT_SECONDS,
    )
    if nightly.returncode != 0:
        return nightly

    python = (os.environ.get("CAREER_AGENT_PYTHON")
              or shutil.which("python3.12")
              or shutil.which("python3"))
    if python is None:
        return core.CommandResult(127, stderr="Python 3.12 or python3 is unavailable")
    return core._run_process(
        [python, "-c", SMOKE_RUNNER],
        cwd=checkout,
        timeout=core.GIT_TIMEOUT_SECONDS,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll and deploy the career runtime checkout from origin/main."
    )
    parser.add_argument("--runtime-root", default=None,
                        help="shared root for the checkout and deploy record")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    checkout, record = resolve_paths(args.runtime_root)
    return runtime_deploy.tick(
        checkout, record, runtime_health_check,
        runtime_name=RUNTIME_NAME,
        runtime_entrypoint=RUNTIME_ENTRYPOINT_RELATIVE,
    )


if __name__ == "__main__":
    raise SystemExit(main())
