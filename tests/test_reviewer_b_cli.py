"""reviewer_b.py runs as a script from muse-review-engine (#2329)."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from engine import review_packets, reviewer_b


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "engine" / "reviewer_b.py"


def test_calibration_runs_as_a_script_outside_the_repo(tmp_path):
    # muse-review-engine calls `python3 "$REPO/engine/reviewer_b.py"
    # calibration ...`, so sys.path[0] is engine/ and the cwd is elsewhere.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    output = tmp_path / "packet.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "calibration", "must_reject", str(output)],
        cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ModuleNotFoundError" not in proc.stderr
    expected = review_packets.load_packet(
        "must_reject", reviewer_b.CALIBRATION_VERSION)
    assert json.loads(output.read_text()) == expected
