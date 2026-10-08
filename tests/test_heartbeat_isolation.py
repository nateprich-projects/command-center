"""Heartbeat writes from tests never reach the live spool (#876).

The check is by content, not by size: the live agents append to the live spool
concurrently, so a size comparison cannot tell a leak from ordinary operation.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid

from conftest import LIVE_SPOOL_DIR

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_shell_python_on_the_test_path_is_the_suite_wrapper(offline_bin):
    """A bare ``python3`` must not reach Apple's /usr/bin/python3 launcher.

    Under a fixture HOME that launcher consults Xcode on every start, which
    pushed the Mac suite past the 2700-second finish bound (#2387).
    """
    found = shutil.which("python3")
    assert found is not None
    assert pathlib.Path(found).parent == offline_bin


def test_shell_python_uses_the_suite_interpreter_with_a_fixture_home(tmp_path):
    result = subprocess.run(
        ["python3", "-c",
         "import json,sys; print(json.dumps([sys.executable,sys.prefix,sys.argv[1:]]))",
         "argument with spaces", "$literal"],
        env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True, text=True, check=True, timeout=10,
    )
    executable, prefix, arguments = json.loads(result.stdout)
    assert pathlib.Path(executable).resolve() == pathlib.Path(sys.executable).resolve()
    assert prefix == sys.prefix
    assert arguments == ["argument with spaces", "$literal"]
    assert result.stderr == ""


def test_shell_python_can_still_be_replaced_by_an_executable_double(tmp_path):
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nprintf 'test double\\n'\n")
    fake.chmod(0o755)
    result = subprocess.run(
        ["python3", "-c", "raise RuntimeError('must not run')"],
        env=dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"]),
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert result.stdout == "test double\n"


def test_a_fresh_interpreter_writes_to_the_inherited_test_spool(heartbeat_isolation):
    marker = "isolation-canary-" + uuid.uuid4().hex
    code = (
        "import heartbeat\n"
        "def offline(*a, **k):\n"
        "    raise heartbeat.HeartbeatError('offline')\n"
        "heartbeat.gh = offline\n"
        "print(heartbeat.SPOOL_DIR)\n"
        "heartbeat.append({!r}, {{'phase': 'start', 'run': {!r}}})\n"
    ).format(marker, marker)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
        capture_output=True, text=True, check=True,
    )

    assert result.stdout.splitlines()[0] == str(heartbeat_isolation)
    written = "".join(p.read_text() for p in heartbeat_isolation.glob("*.jsonl"))
    assert marker in written
    for live in LIVE_SPOOL_DIR.glob("*.jsonl"):
        assert marker not in live.read_text(errors="replace")


def test_funnel_reads_the_same_override(heartbeat_isolation):
    result = subprocess.run(
        [sys.executable, "-c", "import funnel; print(funnel.HEARTBEAT_SPOOL)"],
        cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == str(heartbeat_isolation)
