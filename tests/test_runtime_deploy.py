"""League and career deploy adapters share the FF poll-and-fast-forward core."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import career_deploy  # noqa: E402
import ff_deploy  # noqa: E402
import league_deploy  # noqa: E402
import runtime_deploy  # noqa: E402


def git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def make_checkout(tmp_path: pathlib.Path):
    source = tmp_path / "source"
    remote = tmp_path / "origin.git"
    checkout = tmp_path / "runtime" / "checkout"
    source.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True,
                   text=True, check=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(source)],
                   capture_output=True, text=True, check=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "runtime.txt").write_text("version 1\n", encoding="utf-8")
    git(source, "add", "runtime.txt")
    git(source, "commit", "-m", "initial")
    git(source, "remote", "add", "origin", str(remote))
    git(source, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    )
    checkout.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)],
                   capture_output=True, text=True, check=True)
    before = git(checkout, "rev-parse", "HEAD")
    return source, checkout, before


def advance_main(source: pathlib.Path) -> str:
    (source / "runtime.txt").write_text("version 2\n", encoding="utf-8")
    git(source, "add", "runtime.txt")
    git(source, "commit", "-m", "advance main")
    git(source, "push", "origin", "main")
    return git(source, "rev-parse", "HEAD")


def read_record(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def test_runtime_tick_fast_forwards_verifies_and_writes_ff_schema(tmp_path):
    source, checkout, before = make_checkout(tmp_path)
    after = advance_main(source)
    record_path = tmp_path / "runtime" / "deploy.jsonl"
    verified = []

    def verify(path):
        verified.append(path)
        return ff_deploy.CommandResult(0)

    assert runtime_deploy.tick(
        checkout, record_path, verify, runtime_name="Test",
        runtime_entrypoint=pathlib.Path("runtime.txt"),
    ) == 0

    record = read_record(record_path)
    assert git(checkout, "rev-parse", "HEAD") == after
    assert verified == [checkout]
    assert record["status"] == "deployed"
    assert record["timestamp"].endswith("Z")
    assert record["checkout_head_before"] == before
    assert record["checkout_head_after"] == after
    assert record["main_head"] == after
    assert record["pin_reinstall_result"] == {
        "status": "not_applicable", "items": [],
    }
    assert record["operator_swap_result"] == {"status": "updated"}
    assert record["verify_result"] == {"status": "passed", "returncode": 0}
    assert (checkout / "runtime.txt").read_text(encoding="utf-8") == "version 2\n"


def test_runtime_tick_appends_current_health_record_without_moving_head(tmp_path):
    _source, checkout, head = make_checkout(tmp_path)
    record_path = tmp_path / "runtime" / "deploy.jsonl"

    assert runtime_deploy.tick(
        checkout, record_path, lambda _path: ff_deploy.CommandResult(0),
        runtime_name="Test", runtime_entrypoint=pathlib.Path("runtime.txt"),
    ) == 0

    record = read_record(record_path)
    assert record["status"] == "current"
    assert record["checkout_head_before"] == head
    assert record["checkout_head_after"] == head
    assert record["main_head"] == head
    assert record["operator_swap_result"] == {"status": "unchanged"}


def test_runtime_tick_records_health_failure_after_fast_forward(tmp_path):
    source, checkout, _before = make_checkout(tmp_path)
    after = advance_main(source)
    record_path = tmp_path / "runtime" / "deploy.jsonl"

    assert runtime_deploy.tick(
        checkout, record_path,
        lambda _path: ff_deploy.CommandResult(1, stderr="not healthy"),
        runtime_name="Test", runtime_entrypoint=pathlib.Path("runtime.txt"),
    ) == 1

    record = read_record(record_path)
    assert git(checkout, "rev-parse", "HEAD") == after
    assert record["status"] == "failed"
    assert record["error_code"] == "verification_failed"
    assert record["operator_swap_result"] == {"status": "updated"}
    assert record["verify_result"] == {"status": "failed", "returncode": 1}


def test_runtime_tick_refuses_dirty_checkout_and_records_the_failure(tmp_path):
    _source, checkout, head = make_checkout(tmp_path)
    (checkout / "local.txt").write_text("preserve me\n", encoding="utf-8")
    record_path = tmp_path / "runtime" / "deploy.jsonl"

    assert runtime_deploy.tick(
        checkout, record_path, lambda _path: ff_deploy.CommandResult(0),
        runtime_name="Test", runtime_entrypoint=pathlib.Path("runtime.txt"),
    ) == 1

    record = read_record(record_path)
    assert git(checkout, "rev-parse", "HEAD") == head
    assert (checkout / "local.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert record["status"] == "refused"
    assert record["error_code"] == "checkout_dirty"
    assert record["operator_swap_result"] == {"status": "not_run"}


@pytest.mark.parametrize(
    ("adapter", "checkout_relative", "record_relative", "runtime_entrypoint"),
    [
        (league_deploy, pathlib.Path("share/the-league/checkout"),
         pathlib.Path("share/the-league/deploy.jsonl"),
         pathlib.Path("scripts/daily-snapshot.sh")),
        (career_deploy, pathlib.Path("share/career-agent/checkout"),
         pathlib.Path("share/career-agent/deploy.jsonl"),
         pathlib.Path("scripts/nightly.sh")),
    ],
)
def test_adapter_paths_can_be_relocated(tmp_path, adapter,
                                        checkout_relative, record_relative,
                                        runtime_entrypoint):
    checkout, record = adapter.resolve_paths(tmp_path)

    assert checkout == tmp_path / checkout_relative
    assert record == tmp_path / record_relative
    assert adapter.RUNTIME_ENTRYPOINT_RELATIVE == runtime_entrypoint


def test_league_health_check_uses_venv_and_daily_snapshot_verifier(
    tmp_path, monkeypatch,
):
    calls = []

    def run(argv, *, cwd=None, timeout=ff_deploy.COMMAND_TIMEOUT_SECONDS):
        calls.append((argv, cwd, timeout))
        return ff_deploy.CommandResult(0)

    monkeypatch.setattr(league_deploy.core, "_run_process", run)
    assert league_deploy.runtime_health_check(tmp_path).returncode == 0

    python = str(tmp_path / ".venv" / "bin" / "python")
    assert calls[0][0] == [python, "scripts/check-python.py"]
    assert calls[1][0] == [
        python, "scripts/run-module.py", "lib.snapshot_io", "--verify",
        "data/fp_projections", "data/fp_snapshots",
    ]
    assert all(call[1] == tmp_path for call in calls)


def test_career_health_check_runs_script_syntax_and_offline_smoke(
    tmp_path, monkeypatch,
):
    calls = []

    def run(argv, *, cwd=None, timeout=ff_deploy.COMMAND_TIMEOUT_SECONDS):
        calls.append((argv, cwd, timeout))
        return ff_deploy.CommandResult(0)

    monkeypatch.setattr(career_deploy.core, "_run_process", run)
    monkeypatch.setattr(
        career_deploy.shutil, "which",
        lambda name: "/usr/bin/python3" if name == "python3.12" else None,
    )
    assert career_deploy.runtime_health_check(tmp_path).returncode == 0

    assert calls[0][0] == ["/bin/sh", "-n", "scripts/nightly.sh"]
    assert calls[1][0] == [
        "/usr/bin/python3", "-m", "pytest", "-q", "tests/test_smoke.py",
    ]
    assert all(call[1] == tmp_path for call in calls)
