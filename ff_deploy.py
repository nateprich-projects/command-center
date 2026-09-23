#!/usr/bin/env python3.12
"""Deploy the FF runtime checkout when its main branch advances.

The documented FF layout shares ``~/.local`` as its root: the checkout is
under ``share/ff-weekly-start-sit/checkout`` and ``ff-operate`` is under
``bin``. ``--runtime-root`` and ``COMMAND_CENTER_FF_RUNTIME_ROOT`` override
that default so the same code can be exercised or relocated without edits.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

try:
    import tomllib
except ImportError:  # Python 3.9 runs the Command Center test suite on this host.
    tomllib = None


RUNTIME_ROOT_ENV = "COMMAND_CENTER_FF_RUNTIME_ROOT"
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
PYTHON = "python3.12"
CHECKOUT_RELATIVE = Path("share/ff-weekly-start-sit/checkout")
BIN_RELATIVE = Path("bin")
RECORD_RELATIVE = Path("share/ff-weekly-start-sit/deploy.jsonl")
PIN_PACKAGES = ("the-league", "afl-league")
COMMAND_TIMEOUT_SECONDS = 120
GIT_TIMEOUT_SECONDS = 300
PIP_TIMEOUT_SECONDS = 900
VERIFY_TIMEOUT_SECONDS = 120
VERIFY_CODE = (
    "from ff_weekly_start_sit.league_packages import verify_league_packages; "
    "verify_league_packages()"
)


class DeployError(RuntimeError):
    def __init__(self, message: str, *, code: str = "deploy_failed",
                 outcome: str = "failed") -> None:
        super().__init__(message)
        self.code = code
        self.outcome = outcome


@dataclass(frozen=True)
class Pin:
    url: str
    revision: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _run_process(argv: Sequence[str], *, cwd: Path | None = None,
                 timeout: int = COMMAND_TIMEOUT_SECONDS) -> CommandResult:
    try:
        result = subprocess.run(
            list(argv), cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, stderr="command timed out")
    except OSError:
        return CommandResult(127, stderr="command could not be started")
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _git(checkout: Path, args: Sequence[str], *, timeout: int = GIT_TIMEOUT_SECONDS,
         allow_failure: bool = False) -> CommandResult:
    result = _run_process(["git", "-C", str(checkout), *args], timeout=timeout)
    if result.returncode and not allow_failure:
        command = " ".join(args[:2])
        raise DeployError("git {} failed ({})".format(command, result.returncode))
    return result


def _git_text(checkout: Path, args: Sequence[str]) -> str:
    return _git(checkout, args).stdout.strip()


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).lower()


def _league_specs_without_tomllib(pyproject_text: str) -> list[str]:
    """Read the simple dependency array on Python versions before tomllib."""
    section = re.search(
        r"(?ms)^\[project\.optional-dependencies\]\s*$\n(.*?)(?=^\[|\Z)",
        pyproject_text,
    )
    if section is None:
        raise DeployError("pyproject.toml has no optional-dependencies table",
                          code="invalid_pins")
    assignment = re.search(r"(?m)^\s*leagues\s*=\s*\[", section.group(1))
    if assignment is None:
        raise DeployError("pyproject.toml has no leagues dependency group",
                          code="invalid_pins")
    body_start = assignment.end()
    body_end = section.group(1).find("]", body_start)
    if body_end < 0:
        raise DeployError("pyproject.toml leagues dependency group is invalid",
                          code="invalid_pins")

    specifications = []
    for line in section.group(1)[body_start:body_end].splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.endswith(","):
            value = value[:-1].rstrip()
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            raise DeployError("pyproject.toml leagues dependency group is invalid",
                              code="invalid_pins")
        if not isinstance(parsed, str):
            raise DeployError("pyproject.toml leagues dependency group is invalid",
                              code="invalid_pins")
        specifications.append(parsed)
    return specifications


def parse_pins(pyproject_text: str) -> dict[str, Pin]:
    """Read the two required, commit-pinned VCS dependencies from pyproject."""
    if tomllib is None:
        specifications = _league_specs_without_tomllib(pyproject_text)
    else:
        try:
            document = tomllib.loads(pyproject_text)
        except (tomllib.TOMLDecodeError, TypeError) as exc:
            raise DeployError("pyproject.toml could not be parsed",
                              code="invalid_pins") from exc
        try:
            specifications = document["project"]["optional-dependencies"]["leagues"]
        except (KeyError, TypeError):
            raise DeployError("pyproject.toml has no leagues dependency group",
                              code="invalid_pins")
        if not isinstance(specifications, list):
            raise DeployError("pyproject.toml leagues dependency group is invalid",
                              code="invalid_pins")

    pins: dict[str, Pin] = {}
    for specification in specifications:
        if not isinstance(specification, str):
            continue
        package, separator, url = specification.partition("@")
        name = _normalized_name(package)
        if name not in PIN_PACKAGES:
            continue
        if not separator or name in pins:
            raise DeployError("pyproject.toml has an invalid {} pin".format(name),
                              code="invalid_pins")
        url = url.strip()
        revision = re.search(r"@([0-9a-fA-F]{40})$", url)
        if not url.startswith("git+") or revision is None:
            raise DeployError("pyproject.toml {} pin is not a commit-pinned git URL".format(name),
                              code="invalid_pins")
        pins[name] = Pin(url=url, revision=revision.group(1).lower())

    missing = [name for name in PIN_PACKAGES if name not in pins]
    if missing:
        raise DeployError("pyproject.toml is missing required pins: {}".format(
            ", ".join(missing)), code="invalid_pins")
    return pins


def _pip_install(pin: Pin) -> CommandResult:
    return _run_process([
        PYTHON, "-m", "pip", "install", "--break-system-packages",
        "--no-deps", "--force-reinstall", pin.url,
    ], timeout=PIP_TIMEOUT_SECONDS)


def _rollback_pins(names: Sequence[str], old_pins: dict[str, Pin],
                   record: dict) -> None:
    outcomes = []
    for name in reversed(names):
        result = _pip_install(old_pins[name])
        outcomes.append({
            "package": name,
            "revision": old_pins[name].revision,
            "status": "restored" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
        })
    record["pin_rollback_result"] = {
        "status": "restored" if all(item["status"] == "restored" for item in outcomes)
        else "failed",
        "items": outcomes,
    }


def _install_changed_pins(old_pins: dict[str, Pin], new_pins: dict[str, Pin],
                          record: dict) -> list[str]:
    changed = [name for name in PIN_PACKAGES if old_pins[name].url != new_pins[name].url]
    if not changed:
        record["pin_reinstall_result"] = {"status": "not_needed", "items": []}
        return []

    attempted: list[str] = []
    items = []
    for name in changed:
        attempted.append(name)
        result = _pip_install(new_pins[name])
        item = {
            "package": name,
            "from_revision": old_pins[name].revision,
            "to_revision": new_pins[name].revision,
            "status": "installed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
        }
        items.append(item)
        record["pin_reinstall_result"] = {
            "status": "installed" if result.returncode == 0 else "failed",
            "items": items,
        }
        if result.returncode != 0:
            _rollback_pins(attempted, old_pins, record)
            raise DeployError(
                "installing the moved {} pin failed; refusing the deploy".format(name),
                code="pin_install_failed", outcome="refused",
            )
    record["pin_reinstall_result"] = {"status": "installed", "items": items}
    return attempted


def _wait_for_no_operator_tick() -> None:
    """Wait until no ff-operate tick is reading the script being replaced."""
    while True:
        result = _run_process(["pgrep", "-f", "ff-operate"], timeout=5)
        if result.returncode == 1:
            return
        if result.returncode == 0:
            time.sleep(1)
            continue
        raise DeployError("pgrep could not confirm that ff-operate is idle",
                          code="operator_guard_failed")


def swap_operator_script(source: Path, destination: Path) -> None:
    """Stage beside the target, wait for the current tick, then atomically mv."""
    if not source.is_file():
        raise DeployError("scripts/ff-operate is missing from the checkout",
                          code="operator_source_missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ff-operate.", dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            shutil.copyfileobj(original, target)
            target.flush()
            os.fsync(target.fileno())
        mode = stat.S_IMODE(source.stat().st_mode) | 0o111
        os.chmod(temporary, mode)
        _wait_for_no_operator_tick()
        moved = _run_process(["mv", "-f", str(temporary), str(destination)], timeout=30)
        if moved.returncode != 0:
            raise DeployError("atomic ff-operate swap failed ({})".format(moved.returncode),
                              code="operator_swap_failed")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _current_head(checkout: Path) -> str | None:
    try:
        return _git_text(checkout, ["rev-parse", "HEAD"])
    except DeployError:
        return None


def tick(checkout: Path, bin_root: Path, record_path: Path) -> int:
    """Run one deployment attempt and append its result, including refusals."""
    record = {
        "timestamp": _timestamp(),
        "checkout_head_before": None,
        "checkout_head_after": None,
        "main_head": None,
        "pin_reinstall_result": {"status": "not_run", "items": []},
        "operator_swap_result": {"status": "not_run"},
        "verify_result": {"status": "not_run"},
    }
    returncode = 1
    try:
        if not checkout.is_dir():
            raise DeployError("FF runtime checkout is missing", code="checkout_missing",
                              outcome="refused")
        branch = _git(checkout, ["symbolic-ref", "--quiet", "--short", "HEAD"],
                      allow_failure=True)
        if branch.returncode != 0 or branch.stdout.strip() != DEFAULT_BRANCH:
            raise DeployError("FF runtime checkout is not on main", code="checkout_branch_invalid",
                              outcome="refused")
        before = _git_text(checkout, ["rev-parse", "HEAD"])
        record["checkout_head_before"] = before

        fetch = _git(checkout, [
            "fetch", "--no-tags", DEFAULT_REMOTE,
            "+refs/heads/main:refs/remotes/origin/main",
        ], allow_failure=True)
        if fetch.returncode != 0:
            raise DeployError("fetching FF main failed ({})".format(fetch.returncode),
                              code="fetch_failed")
        main_head = _git_text(checkout, ["rev-parse", "refs/remotes/origin/main"])
        record["main_head"] = main_head

        status = _git(checkout, ["status", "--porcelain", "--untracked-files=normal"])
        if status.stdout.strip():
            raise DeployError("FF runtime checkout has local changes; refusing the deploy",
                              code="checkout_dirty", outcome="refused")

        old_pins = parse_pins(_git_text(checkout, ["show", "HEAD:pyproject.toml"]))
        new_pins = parse_pins(_git_text(checkout, ["show", "{}:pyproject.toml".format(main_head)]))
        attempted: list[str] = []
        if before != main_head:
            ancestor = _git(checkout, ["merge-base", "--is-ancestor", "HEAD", main_head],
                            allow_failure=True)
            if ancestor.returncode == 1:
                raise DeployError("FF main cannot fast-forward this checkout",
                                  code="non_fast_forward", outcome="refused")
            if ancestor.returncode != 0:
                raise DeployError("could not confirm FF fast-forward safety",
                                  code="ancestry_check_failed")
            attempted = _install_changed_pins(old_pins, new_pins, record)
            merged = _git(checkout, ["merge", "--ff-only", main_head], allow_failure=True)
            if merged.returncode != 0:
                if attempted:
                    _rollback_pins(attempted, old_pins, record)
                raise DeployError("fast-forwarding the FF checkout failed ({})".format(
                    merged.returncode), code="fast_forward_failed")
            record["checkout_head_after"] = _git_text(checkout, ["rev-parse", "HEAD"])
        else:
            record["pin_reinstall_result"] = {"status": "not_needed", "items": []}
            record["checkout_head_after"] = before

        source = checkout / "scripts" / "ff-operate"
        destination = bin_root / "ff-operate"
        needs_swap = not destination.is_file() or source.read_bytes() != destination.read_bytes()
        if needs_swap:
            swap_operator_script(source, destination)
            record["operator_swap_result"] = {"status": "updated"}
        else:
            record["operator_swap_result"] = {"status": "unchanged"}

        verification = _run_process([PYTHON, "-c", VERIFY_CODE], cwd=checkout,
                                    timeout=VERIFY_TIMEOUT_SECONDS)
        record["verify_result"] = {
            "status": "passed" if verification.returncode == 0 else "failed",
            "returncode": verification.returncode,
        }
        if verification.returncode != 0:
            raise DeployError("league_packages.verify_league_packages() failed",
                              code="verification_failed")

        if before != main_head:
            record["status"] = "deployed"
        elif needs_swap:
            record["status"] = "repaired"
        else:
            record["status"] = "current"
        returncode = 0
    except DeployError as exc:
        record["status"] = exc.outcome
        record["error_code"] = exc.code
        record["error"] = str(exc)
        print("FF deploy: {}".format(exc), file=sys.stderr)
    except (OSError, ValueError, TypeError) as exc:
        record["status"] = "failed"
        record["error_code"] = "unexpected_error"
        record["error"] = str(exc)[:240]
        print("FF deploy: {}".format(record["error"]), file=sys.stderr)

    if record["checkout_head_after"] is None:
        record["checkout_head_after"] = _current_head(checkout) if checkout.is_dir() else None
    try:
        _append_record(record_path, record)
    except OSError as exc:
        print("FF deploy: could not append deploy record ({})".format(exc), file=sys.stderr)
        returncode = 1
    if returncode == 0:
        print("FF deploy: {} at {}".format(record["status"], record["checkout_head_after"][:12]),
              file=sys.stderr)
    return returncode


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll and deploy the FF runtime checkout from origin/main.",
    )
    parser.add_argument("--runtime-root", default=None,
                        help="shared root for the checkout, operator bin, and deploy record")
    return parser.parse_args(argv)


def resolve_paths(runtime_root: str | Path | None = None) -> tuple[Path, Path, Path]:
    configured = runtime_root or os.environ.get(RUNTIME_ROOT_ENV)
    root = Path(configured).expanduser() if configured else Path.home() / ".local"
    return root / CHECKOUT_RELATIVE, root / BIN_RELATIVE, root / RECORD_RELATIVE


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    checkout, bin_root, record_path = resolve_paths(args.runtime_root)
    return tick(checkout, bin_root, record_path)


if __name__ == "__main__":
    sys.exit(main())
