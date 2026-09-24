"""The funnel check-in verifies deploy records without moving runtimes."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ff_deploy
import runtime_checkin


NOW = datetime(2026, 9, 24, 3, 0, tzinfo=timezone.utc)
HEAD = "a" * 40


def runtime(tmp_path, *, head=HEAD, record=None):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    record_path = tmp_path / "deploy.jsonl"
    if record is not None:
        record_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return runtime_checkin.Runtime("Test", checkout, record_path)


def deploy_record(*, timestamp=None, status="current", after=HEAD, main=HEAD):
    return {
        "timestamp": timestamp or NOW.isoformat(),
        "status": status,
        "checkout_head_after": after,
        "main_head": main,
        "verify_result": {"status": "passed", "returncode": 0},
    }


def patch_git(monkeypatch, *, checkout_head=HEAD, remote_head=HEAD):
    commands = []

    def run(argv, *, cwd=None, timeout=ff_deploy.COMMAND_TIMEOUT_SECONDS):
        commands.append(tuple(argv[4:]))
        if argv[4:] == ["rev-parse", "HEAD"]:
            return ff_deploy.CommandResult(0, checkout_head + "\n")
        if argv[4:] == ["ls-remote", "--heads", "origin", "refs/heads/main"]:
            return ff_deploy.CommandResult(
                0, remote_head + "\trefs/heads/main\n")
        return ff_deploy.CommandResult(2, stderr="unexpected git command")

    monkeypatch.setattr(runtime_checkin.ff_deploy, "_run_process", run)
    return commands


def test_matching_fresh_record_reports_ok_without_mutating_checkout(
    tmp_path, monkeypatch,
):
    item = runtime(tmp_path, record=deploy_record())
    commands = patch_git(monkeypatch)

    result = runtime_checkin.check_runtime(item, now=NOW)

    assert result.ok
    assert result.state == "ok"
    assert commands == [
        ("rev-parse", "HEAD"),
        ("ls-remote", "--heads", "origin", "refs/heads/main"),
    ]


def test_old_record_reports_stale_poller(tmp_path, monkeypatch):
    old = NOW - timedelta(seconds=runtime_checkin.STALE_AFTER_SECONDS + 1)
    item = runtime(tmp_path, record=deploy_record(timestamp=old.isoformat()))
    patch_git(monkeypatch)

    result = runtime_checkin.check_runtime(item, now=NOW)

    assert result.state == "drift"
    assert "poller is stale" in result.detail


def test_refusal_is_reported_as_refusal_not_poller_failure(tmp_path, monkeypatch):
    record = deploy_record(status="refused", timestamp="invalid")
    record["error"] = "pin installation failed"
    item = runtime(tmp_path, record=record)
    patch_git(monkeypatch)

    result = runtime_checkin.check_runtime(item, now=NOW)

    assert result.state == "refusal"
    assert "recorded refusal" in result.detail
    assert "pin installation failed" in result.detail


def test_checkout_head_mismatch_reports_drift(tmp_path, monkeypatch):
    item = runtime(tmp_path, record=deploy_record())
    patch_git(monkeypatch, checkout_head="b" * 40)

    result = runtime_checkin.check_runtime(item, now=NOW)

    assert result.state == "drift"
    assert "differs from recorded head" in result.detail


def test_origin_main_ahead_reports_stale_poller(tmp_path, monkeypatch):
    item = runtime(tmp_path, record=deploy_record())
    patch_git(monkeypatch, remote_head="b" * 40)

    result = runtime_checkin.check_runtime(item, now=NOW)

    assert result.state == "drift"
    assert "differs from origin/main" in result.detail


def test_missing_record_reports_drift(tmp_path):
    item = runtime(tmp_path)

    result = runtime_checkin.check_runtime(item, now=NOW)

    assert result.state == "drift"
    assert "unavailable" in result.detail
