"""A spent provider window parks the lanes instead of erroring a ticket.

Muse's subscription quota is invisible to the pace gate in ``usage.py``,
which prices the session journal against a dollar cap. When the provider's
own window empties, ``muse exec`` fails in seconds, and the implementer used
to read that as the claimed ticket's failure: release, finish ``errored``,
and take the next ticket ten minutes later. Measured on 2026-09-19: five
errored runs on FF#208 in an hour, with twenty-nine hours still to run
before the reset.

The hold below is the contract instead — whichever lane meets the refusal
records the reset the provider named, and every lane checks it before it
claims anything.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess

import pytest

from test_muse_implement import (  # noqa: E402  (shared fixture harness)
    BEGIN_REF,
    _begin,
    _calls,
    _heartbeat,
    _muse_calls,
    _stubbed_runner,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "muse-quota-hold.sh"

REFUSAL = (
    "run ended with Failed: API error 429 "
    "[request_id=ddc4fbdf-8557-442b-afae-4ab7a8a3b3e9]: Subscription quota "
    "exhausted. Your usage window resets at 2026-09-21T00:00:00Z. "
    "(rate_limit_error)\n"
)

#: A stub that refuses the way the provider does: the message on stderr, a
#: non-zero exit, and no answer left behind.
REFUSING_MUSE = (
    "#!/bin/bash\n"
    "count_file=\"$MUSE_COUNT\"\n"
    "n=1\n"
    "if [[ -f \"$count_file\" ]]; then n=$(($(cat \"$count_file\") + 1)); fi\n"
    "printf '%s' \"$n\" > \"$count_file\"\n"
    "printf '%s' " + repr(REFUSAL).replace("'", '"') + " >&2\n"
    "exit 1\n"
)


def _helper(tmp_path, script, *, hold=None, env=None):
    """Run one helper function with the hold file under tmp_path."""
    hold_file = tmp_path / "hold"
    if hold is not None:
        hold_file.write_text(hold)
    full = dict(os.environ, MUSE_QUOTA_HOLD_FILE=str(hold_file))
    full.update(env or {})
    proc = subprocess.run(
        ["/bin/bash", "-c", ". {} && {}".format(HELPER, script)],
        env=full, capture_output=True, text=True, timeout=30)
    return proc, hold_file


def _stamp(offset_seconds):
    moment = datetime.datetime.now(datetime.timezone.utc) + (
        datetime.timedelta(seconds=offset_seconds))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_refusal_records_the_reset_the_provider_named(tmp_path):
    capture = tmp_path / "stderr"
    capture.write_text("some earlier line\n" + REFUSAL)
    proc, hold_file = _helper(
        tmp_path, "muse_quota_record {}".format(capture))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "2026-09-21T00:00:00Z"
    assert hold_file.read_text().strip() == "2026-09-21T00:00:00Z"


def test_an_ordinary_failure_records_no_hold(tmp_path):
    capture = tmp_path / "stderr"
    capture.write_text("muse: the tests failed\n")
    proc, hold_file = _helper(
        tmp_path, "muse_quota_record {}".format(capture))
    assert proc.returncode != 0
    assert not hold_file.exists()


def test_a_refusal_without_a_readable_stamp_still_parks_the_lane(tmp_path):
    capture = tmp_path / "stderr"
    capture.write_text("API error 429: Subscription quota exhausted.\n")
    proc, hold_file = _helper(
        tmp_path, "muse_quota_record {}".format(capture),
        env={"MUSE_QUOTA_FALLBACK_SECONDS": "900"})
    assert proc.returncode == 0, proc.stderr
    written = datetime.datetime.strptime(
        hold_file.read_text().strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    ahead = (written - datetime.datetime.now(datetime.timezone.utc))
    assert datetime.timedelta(seconds=600) < ahead <= datetime.timedelta(
        seconds=900)


def test_a_live_hold_reads_back_and_a_passed_one_is_dropped(tmp_path):
    proc, hold_file = _helper(
        tmp_path, "muse_quota_hold_until", hold=_stamp(3600))
    assert proc.returncode == 0
    assert proc.stdout.strip() == hold_file.read_text().strip()

    proc, hold_file = _helper(
        tmp_path, "muse_quota_hold_until", hold="2020-01-01T00:00:00Z")
    assert proc.returncode != 0
    assert not hold_file.exists(), "a hold that has passed must not survive"


@pytest.mark.parametrize("hold", ["", "not a timestamp\n"])
def test_a_hold_it_cannot_read_lets_the_lane_run(tmp_path, hold):
    proc, hold_file = _helper(tmp_path, "muse_quota_hold_until", hold=hold)
    assert proc.returncode != 0, "an unreadable hold must not park a lane"
    assert not hold_file.exists()


def test_the_implementer_parks_the_ticket_instead_of_erroring_it(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), muse_body=REFUSING_MUSE)

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    heartbeat = _heartbeat(repo)
    assert "skipped-provider-quota" in heartbeat
    assert "errored" not in heartbeat, (
        "a spent provider window is not the ticket's failure")
    assert "2026-09-21T00:00:00Z" in heartbeat
    assert any(call.startswith("release") for call in _calls(repo, "funnel")), (
        "the claim must go back")
    hold_file = tmp_path / ".claude" / "command-center-muse-quota-hold"
    assert hold_file.read_text().strip() == "2026-09-21T00:00:00Z"
    assert BEGIN_REF in heartbeat
    # The model's stderr still reaches the launchd log.
    assert "Subscription quota exhausted" in proc.stderr


def test_a_recorded_hold_stops_the_next_fire_before_it_claims(tmp_path):
    hold_dir = tmp_path / ".claude"
    hold_dir.mkdir(parents=True, exist_ok=True)
    (hold_dir / "command-center-muse-quota-hold").write_text(_stamp(1800))

    proc, repo = _stubbed_runner(tmp_path, _begin())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0, "a parked lane must not call the model"
    assert _calls(repo, "funnel") == [], (
        "a parked lane must not open a funnel session or claim a ticket")
    assert "parked until" in proc.stderr


def test_a_hold_that_has_passed_lets_the_lane_work(tmp_path):
    hold_dir = tmp_path / ".claude"
    hold_dir.mkdir(parents=True, exist_ok=True)
    (hold_dir / "command-center-muse-quota-hold").write_text(_stamp(-60))

    proc, repo = _stubbed_runner(tmp_path, _begin())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    assert json.loads((repo / "finish.answer").read_text())["done"] is True
