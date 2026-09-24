#!/usr/bin/env python3
"""Shared fast-forward and record handling for non-FF runtime adapters."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Callable

import ff_deploy as core


HealthCheck = Callable[[Path], core.CommandResult]


def resolve_paths(
    runtime_root: str | Path | None,
    root_env: str,
    checkout_relative: Path,
    record_relative: Path,
) -> tuple[Path, Path]:
    """Resolve one runtime's checkout and append-only record under ``~/.local``."""
    configured = runtime_root or os.environ.get(root_env)
    root = Path(configured).expanduser() if configured else Path.home() / ".local"
    return root / checkout_relative, root / record_relative


def copy_runtime_entrypoint(checkout: Path, relative_path: Path) -> None:
    """Atomically copy a checked-out job entrypoint onto the path launchd reads."""
    entrypoint = checkout / relative_path
    if not entrypoint.is_file():
        raise core.DeployError(
            "runtime entrypoint is missing: {}".format(relative_path),
            code="runtime_file_missing",
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(entrypoint.name), dir=str(entrypoint.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(entrypoint, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, entrypoint)
    except OSError as exc:
        raise core.DeployError(
            "copying runtime entrypoint {} failed ({})".format(relative_path, exc),
            code="runtime_file_copy_failed",
        ) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def tick(
    checkout: Path,
    record_path: Path,
    health_check: HealthCheck,
    *,
    runtime_name: str,
    runtime_entrypoint: Path,
) -> int:
    """Fast-forward, copy the runtime entrypoint, verify, and append the FF record."""
    record = core.new_deploy_record()
    record["pin_reinstall_result"] = {"status": "not_applicable", "items": []}
    returncode = 1
    try:
        moved = core.fast_forward_checkout(checkout, record)
        if moved:
            try:
                copy_runtime_entrypoint(checkout, runtime_entrypoint)
            except core.DeployError:
                record["operator_swap_result"] = {"status": "failed"}
                raise
            record["operator_swap_result"] = {"status": "updated"}
        else:
            record["operator_swap_result"] = {"status": "unchanged"}
        verification = health_check(checkout)
        record["verify_result"] = {
            "status": "passed" if verification.returncode == 0 else "failed",
            "returncode": verification.returncode,
        }
        if verification.returncode != 0:
            raise core.DeployError(
                "{} runtime health check failed".format(runtime_name),
                code="verification_failed",
            )
        record["status"] = "deployed" if moved else "current"
        returncode = 0
    except core.DeployError as exc:
        record["status"] = exc.outcome
        record["error_code"] = exc.code
        record["error"] = str(exc)
        print("{} deploy: {}".format(runtime_name, exc), file=sys.stderr)
    except (OSError, ValueError, TypeError) as exc:
        record["status"] = "failed"
        record["error_code"] = "unexpected_error"
        record["error"] = str(exc)[:240]
        print("{} deploy: {}".format(runtime_name, record["error"]), file=sys.stderr)

    if record["checkout_head_after"] is None:
        record["checkout_head_after"] = (
            core._current_head(checkout) if checkout.is_dir() else None
        )
    try:
        core._append_record(record_path, record)
    except OSError as exc:
        print("{} deploy: could not append deploy record ({})".format(
            runtime_name, exc), file=sys.stderr)
        returncode = 1
    if returncode == 0:
        print("{} deploy: {} at {}".format(
            runtime_name, record["status"], record["checkout_head_after"][:12]),
            file=sys.stderr)
    return returncode
