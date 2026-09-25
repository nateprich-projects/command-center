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
RESET_AT = (datetime.datetime.now(datetime.timezone.utc) +
            datetime.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

REFUSAL = (
    "run ended with Failed: API error 429 "
    "[request_id=ddc4fbdf-8557-442b-afae-4ab7a8a3b3e9]: Subscription quota "
    "exhausted. Your usage window resets at {}. (rate_limit_error)\n"
).format(RESET_AT)

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


def _next_weekly_reset():
    now = datetime.datetime.now(datetime.timezone.utc)
    days = (7 - now.weekday()) % 7
    reset = (now + datetime.timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += datetime.timedelta(days=7)
    return reset


def _write_spool(spool, records):
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / "muse.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    return path


def _quota_events(spool):
    path = spool / "muse.jsonl"
    if not path.exists():
        return []
    return [
        record for line in path.read_text().splitlines()
        if line.strip()
        for record in [json.loads(line)]
        if record.get("phase") == "quota_hit"
    ]


def _paired_window(reset, start_spent=4.0, finish_spent=5.25):
    reset_stamp = reset.timestamp() if hasattr(reset, "timestamp") else reset
    window = {"resets_at": reset_stamp, "spent_dollars": start_spent}
    end_window = dict(window, spent_dollars=finish_spent)
    return [
        {"run": "paired-run", "agent": "muse", "phase": "start",
         "ts": 10, "usage": {"seven_day": window}},
        {"run": "paired-run", "agent": "muse", "phase": "finish",
         "ts": 20, "usage": {"seven_day": end_window}},
    ]


def test_a_refusal_records_the_reset_the_provider_named(tmp_path):
    capture = tmp_path / "stderr"
    capture.write_text("some earlier line\n" + REFUSAL)
    proc, hold_file = _helper(
        tmp_path, "muse_quota_record {}".format(capture))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == RESET_AT
    assert hold_file.read_text().strip() == RESET_AT


def test_weekly_lattice_hit_records_the_matching_paired_window_total(tmp_path):
    reset = _next_weekly_reset()
    stamp = reset.strftime("%Y-%m-%dT%H:%M:%SZ")
    spool = tmp_path / "heartbeat-spool"
    _write_spool(spool, _paired_window(reset))
    capture = tmp_path / "stderr"
    refusal = REFUSAL.replace("2026-09-21T00:00:00Z", stamp)
    capture.write_text(refusal)

    proc, _ = _helper(
        tmp_path, "muse_quota_record {} quota-run".format(capture),
        env={"COMMAND_CENTER_HEARTBEAT_SPOOL": str(spool)})

    assert proc.returncode == 0, proc.stderr
    [event] = _quota_events(spool)
    assert event["run"] == "quota-run"
    assert event["reset_stamp"] == stamp
    assert 0 < event["seconds_to_reset"] <= 7 * 86400
    assert event["weekly_lattice"] is True
    assert event["classification"] == "weekly"
    assert event["raw_refusal"] == refusal.strip()
    assert event["anchored_window_total_dollars"] == 1.25
    assert event["anchored_window_runs"] == 1
    assert event["degraded_note"] is None


def test_off_lattice_five_hour_shape_stays_unclassified(tmp_path):
    reset = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
    reset = reset.replace(microsecond=0)
    stamp = reset.strftime("%Y-%m-%dT%H:%M:%SZ")
    spool = tmp_path / "heartbeat-spool"
    capture = tmp_path / "stderr"
    capture.write_text(REFUSAL.replace("2026-09-21T00:00:00Z", stamp))

    proc, _ = _helper(
        tmp_path, "muse_quota_record {} quota-run".format(capture),
        env={"COMMAND_CENTER_HEARTBEAT_SPOOL": str(spool)})

    assert proc.returncode == 0, proc.stderr
    [event] = _quota_events(spool)
    assert 0 < event["seconds_to_reset"] < 5 * 3600
    assert event["weekly_lattice"] is False
    assert event["classification"] == "unclassified"
    assert event["anchored_window_total_dollars"] is None
    assert "off the Monday 00:00 UTC lattice" in event["degraded_note"]


def test_malformed_reset_stamp_is_recorded_as_degraded_not_weekly(tmp_path):
    spool = tmp_path / "heartbeat-spool"
    capture = tmp_path / "stderr"
    capture.write_text(
        "API error 429: Subscription quota exhausted. Your usage window "
        "resets at not-a-timestamp. (rate_limit_error)\n")

    proc, hold_file = _helper(
        tmp_path, "muse_quota_record {} quota-run".format(capture),
        env={"COMMAND_CENTER_HEARTBEAT_SPOOL": str(spool),
             "MUSE_QUOTA_FALLBACK_SECONDS": "900"})

    assert proc.returncode == 0, proc.stderr
    assert hold_file.exists(), "the existing fallback hold still parks the lane"
    [event] = _quota_events(spool)
    assert event["reset_stamp"] == "not-a-timestamp"
    assert event["seconds_to_reset"] is None
    assert event["weekly_lattice"] is False
    assert event["classification"] == "unclassified"
    assert event["anchored_window_total_dollars"] is None
    assert "malformed" in event["degraded_note"]


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
    spool = tmp_path / "heartbeat-spool"
    proc, hold_file = _helper(
        tmp_path, "muse_quota_record {}".format(capture),
        env={"MUSE_QUOTA_FALLBACK_SECONDS": "900",
             "COMMAND_CENTER_HEARTBEAT_SPOOL": str(spool)})
    assert proc.returncode == 0, proc.stderr
    written = datetime.datetime.strptime(
        hold_file.read_text().strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    ahead = (written - datetime.datetime.now(datetime.timezone.utc))
    assert datetime.timedelta(seconds=600) < ahead <= datetime.timedelta(
        seconds=900)
    [event] = _quota_events(spool)
    assert event["reset_stamp"] is None
    assert event["seconds_to_reset"] is None
    assert event["weekly_lattice"] is False
    assert event["classification"] == "unclassified"
    assert "missing" in event["degraded_note"]


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
    assert RESET_AT in heartbeat
    assert any(call.startswith("release") for call in _calls(repo, "funnel")), (
        "the claim must go back")
    hold_file = tmp_path / ".claude" / "command-center-muse-quota-hold"
    assert hold_file.read_text().strip() == RESET_AT
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
