#!/usr/bin/env python3
"""SSD-backed scratch for every automated test run (#2530).

Pytest debris and bytecode caches filled the internal disk twice: 1.7 GiB
free on 2026-10-07, and 8.3 GiB on 2026-10-11 after the funnel watch cleared
another 12 GiB by hand. Housekeeping only sweeps up afterwards, so every
automated test run now writes its debris to the external volume from the
start: the model's runs inside the Muse implement and review lanes, and the
lanes' own invocations through ``engine.implement.run_tests``.

``TMPDIR`` roots pytest's basetemp (``pytest-of-<user>/``), so one variable
moves pytest debris plus every other tempfile write, without overriding a
repository's own ``--basetemp`` config the way ``PYTEST_ADDOPTS`` would.
``PYTHONPYCACHEPREFIX`` catches bytecode when anything unsets
``PYTHONDONTWRITEBYTECODE``. ``prepare`` returns all three.

A missing, unmounted, unwritable or unresponsive volume refuses loudly
(``TestEnvError``) — never a silent fallback to the internal disk.
``run_tests`` converts that into ``TestEnvironmentError`` so the finish
keeps the work and ends errored; the runners exit 1 before claiming.
``COMMAND_CENTER_TEST_SSD_ROOT`` overrides the volume for tests and for a
checkout on a machine without it; the override must exist but is trusted
without the mount check.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
import tempfile
import threading
from typing import Dict, Tuple

#: The external scratch volume every automated test run writes to.
DEFAULT_SSD_ROOT = "/Volumes/ClaudeScratch"

#: Override for tests and for machines without the volume. Trusted
#: without the mount check; it must still exist and accept writes.
SSD_ROOT_ENV_VAR = "COMMAND_CENTER_TEST_SSD_ROOT"

#: ``TMPDIR`` for test runs: pytest's ``pytest-of-<user>/`` lands here.
TMP_SUBDIR = "command-center-test-tmp"

#: ``PYTHONPYCACHEPREFIX`` for test runs.
PYCACHE_SUBDIR = "command-center-test-pycache"

#: How long the volume gets to answer before the run refuses. Matches
#: the local-probe precedent (``INTERPRETER_PROBE_TIMEOUT_SECONDS``):
#: a wedged volume must fail loudly, not wedge the lane with it.
PROBE_TIMEOUT_SECONDS = 20


class TestEnvError(RuntimeError):
    """SSD test scratch is unavailable; the run must refuse, not fall back."""


def _ssd_root() -> Tuple[str, bool]:
    """The scratch volume and whether it came from the override."""
    override = os.environ.get(SSD_ROOT_ENV_VAR, "").strip()
    if override:
        return override, True
    return DEFAULT_SSD_ROOT, False


def _refusing(reason: str) -> str:
    return ("{}; refusing to write test debris to the internal "
            "disk".format(reason))


def _prepare_under(root: str, *, override: bool) -> Dict[str, str]:
    """Create the scratch dirs and prove they accept writes."""
    if not os.path.isdir(root):
        raise TestEnvError(_refusing(
            "SSD test scratch is not mounted at {}".format(root)))
    if not override and not os.path.ismount(root):
        raise TestEnvError(_refusing(
            "{} is not a mounted volume".format(root)))
    tmpdir = os.path.join(root, TMP_SUBDIR)
    pycache = os.path.join(root, PYCACHE_SUBDIR)
    for path in (tmpdir, pycache):
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise TestEnvError(_refusing(
                "cannot create SSD test scratch at {}: {}".format(
                    path, exc.strerror or exc))) from None
        # Owner-only, like the per-user TMPDIR this replaces: test
        # output can carry private member-repo content.
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise TestEnvError(_refusing(
                "cannot protect SSD test scratch at {}: {}".format(
                    path, exc.strerror or exc))) from None
    try:
        fd, probe = tempfile.mkstemp(dir=tmpdir, prefix=".probe-")
        try:
            os.write(fd, b"writable\n")
        finally:
            os.close(fd)
            os.unlink(probe)
    except OSError as exc:
        raise TestEnvError(_refusing(
            "SSD test scratch at {} is not writable: {}".format(
                tmpdir, exc.strerror or exc))) from None
    return {"TMPDIR": tmpdir,
            "PYTHONPYCACHEPREFIX": pycache,
            "PYTHONDONTWRITEBYTECODE": "1"}


def prepare(timeout: float = PROBE_TIMEOUT_SECONDS) -> Dict[str, str]:
    """The SSD test environment, or ``TestEnvError`` when it refuses.

    The whole probe runs under ``timeout``: every call into a wedged
    volume can block, so even the mount check must not wedge the lane.
    """
    root, override = _ssd_root()
    outcome: Dict[str, object] = {}

    def work() -> None:
        try:
            outcome["env"] = _prepare_under(root, override=override)
        except Exception as exc:  # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TestEnvError(_refusing(
            "SSD test scratch at {} did not answer within {}s".format(
                root, timeout)))
    error = outcome.get("error")
    if error is not None:
        if isinstance(error, TestEnvError):
            raise error
        raise TestEnvError(_refusing(
            "SSD test scratch at {} failed: {}".format(root, error)))
    env = outcome.get("env")
    if not isinstance(env, dict):
        raise TestEnvError(_refusing(
            "SSD test scratch at {} failed without detail".format(root)))
    return env


def export_main(argv=None) -> int:
    """Print ``PREFIX_TMPDIR``/``PREFIX_PYCACHE`` assignments for the shell."""
    parser = argparse.ArgumentParser(
        description="prepare SSD test scratch and print its paths")
    parser.add_argument("--export", required=True, metavar="PREFIX",
                        help="variable prefix for the printed assignments")
    parser.add_argument("--timeout", type=float,
                        default=PROBE_TIMEOUT_SECONDS,
                        help="seconds the volume gets to answer")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.export):
        parser.error("PREFIX must be a shell variable name")
    try:
        env = prepare(timeout=args.timeout)
    except TestEnvError as exc:
        print("test_env: {}".format(exc), file=sys.stderr)
        return 1
    print("{}_TMPDIR={}".format(
        args.export, shlex.quote(env["TMPDIR"])))
    print("{}_PYCACHE={}".format(
        args.export, shlex.quote(env["PYTHONPYCACHEPREFIX"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(export_main())
