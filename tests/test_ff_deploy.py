"""The FF poller refuses partial pin deploys and records verified updates."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ff_deploy  # noqa: E402


THE_LEAGUE_URL = "git+https://github.com/nateprich/The-League.git@{}"
AFL_URL = "git+https://github.com/nateprich/AFL.git@{}"
OLD_THE_LEAGUE = "1" * 40
NEW_THE_LEAGUE = "2" * 40
AFL_REVISION = "3" * 40


def pyproject(the_league: str = OLD_THE_LEAGUE) -> str:
    return """[project]
name = "ff-weekly-start-sit"

[project.optional-dependencies]
leagues = [
  "the-league @ {the_league}",
  "afl-league @ {afl}",
]
""".format(the_league=THE_LEAGUE_URL.format(the_league),
           afl=AFL_URL.format(AFL_REVISION))


def git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def make_runtime(tmp_path: pathlib.Path):
    runtime_root = tmp_path / "user-local"
    checkout, bin_root, record_path = ff_deploy.resolve_paths(runtime_root)
    source = tmp_path / "source"
    remote = tmp_path / "origin.git"
    source.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True,
                   text=True, check=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(source)],
                   capture_output=True, text=True, check=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    write_source(source, OLD_THE_LEAGUE, "wrapper v1\n")
    git(source, "add", "pyproject.toml", "scripts/ff-operate")
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
    old_head = git(checkout, "rev-parse", "HEAD")
    return source, checkout, bin_root, record_path, old_head


def write_source(source: pathlib.Path, the_league: str, wrapper: str) -> None:
    (source / "pyproject.toml").write_text(pyproject(the_league), encoding="utf-8")
    scripts = source / "scripts"
    scripts.mkdir(exist_ok=True)
    operator = scripts / "ff-operate"
    operator.write_text(wrapper, encoding="utf-8")
    operator.chmod(0o755)


def advance_main(source: pathlib.Path, the_league: str, wrapper: str) -> str:
    write_source(source, the_league, wrapper)
    git(source, "add", "pyproject.toml", "scripts/ff-operate")
    git(source, "commit", "-m", "advance main")
    git(source, "push", "origin", "main")
    return git(source, "rev-parse", "HEAD")


def mock_runtime_commands(monkeypatch, *, failed_pin: str | None = None,
                          process_states: tuple[int, ...] = (1,)):
    original = ff_deploy._run_process
    events = []
    pip_urls = []
    states = iter(process_states)

    def run(argv, *, cwd=None, timeout=ff_deploy.COMMAND_TIMEOUT_SECONDS):
        args = list(argv)
        if args[:3] == ["python3.12", "-m", "pip"]:
            url = args[-1]
            pip_urls.append(url)
            events.append("pip")
            return ff_deploy.CommandResult(
                1 if failed_pin and failed_pin in url else 0,
                stderr="simulated pip result",
            )
        if args[:3] == ["python3.12", "-c", ff_deploy.VERIFY_CODE]:
            events.append("verify")
            return ff_deploy.CommandResult(0)
        if args[:3] == ["pgrep", "-f", "ff-operate"]:
            events.append("pgrep")
            return ff_deploy.CommandResult(next(states, 1))
        if args and args[0] == "mv":
            events.append("mv")
        return original(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(ff_deploy, "_run_process", run)
    return events, pip_urls


def read_record(path: pathlib.Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines
    return json.loads(lines[-1])


def test_parse_pins_reads_commit_pinned_league_dependencies():
    pins = ff_deploy.parse_pins(pyproject())

    assert pins["the-league"].revision == OLD_THE_LEAGUE
    assert pins["the-league"].url == THE_LEAGUE_URL.format(OLD_THE_LEAGUE)
    assert pins["afl-league"].revision == AFL_REVISION


def test_runtime_root_override_moves_checkout_bin_and_record_together(tmp_path):
    checkout, bin_root, record_path = ff_deploy.resolve_paths(tmp_path / "elsewhere")

    assert checkout == tmp_path / "elsewhere/share/ff-weekly-start-sit/checkout"
    assert bin_root == tmp_path / "elsewhere/bin"
    assert record_path == tmp_path / "elsewhere/share/ff-weekly-start-sit/deploy.jsonl"


def test_failed_moved_pin_keeps_checkout_at_old_head_and_records_refusal(
    tmp_path, monkeypatch
):
    source, checkout, bin_root, record_path, old_head = make_runtime(tmp_path)
    advance_main(source, NEW_THE_LEAGUE, "wrapper v2\n")
    events, pip_urls = mock_runtime_commands(monkeypatch, failed_pin=NEW_THE_LEAGUE)

    result = ff_deploy.tick(checkout, bin_root, record_path)

    assert result == 1
    assert git(checkout, "rev-parse", "HEAD") == old_head
    assert THE_LEAGUE_URL.format(NEW_THE_LEAGUE) in pip_urls
    assert THE_LEAGUE_URL.format(OLD_THE_LEAGUE) in pip_urls
    assert "pgrep" not in events
    record = read_record(record_path)
    assert record["status"] == "refused"
    assert record["error_code"] == "pin_install_failed"
    assert record["checkout_head_before"] == old_head
    assert record["checkout_head_after"] == old_head
    assert record["pin_reinstall_result"]["status"] == "failed"
    assert record["pin_rollback_result"]["status"] == "restored"
    assert record["verify_result"]["status"] == "not_run"


def test_success_fast_forwards_swaps_after_tick_exits_and_records_verification(
    tmp_path, monkeypatch
):
    source, checkout, bin_root, record_path, old_head = make_runtime(tmp_path)
    new_head = advance_main(source, NEW_THE_LEAGUE, "wrapper v2\n")
    events, pip_urls = mock_runtime_commands(
        monkeypatch, process_states=(0, 1),
    )
    monkeypatch.setattr(ff_deploy.time, "sleep", lambda _seconds: None)

    result = ff_deploy.tick(checkout, bin_root, record_path)

    assert result == 0
    assert git(checkout, "rev-parse", "HEAD") == new_head
    assert (bin_root / "ff-operate").read_text(encoding="utf-8") == "wrapper v2\n"
    assert pip_urls == [THE_LEAGUE_URL.format(NEW_THE_LEAGUE)]
    assert events.index("pgrep") < events.index("mv") < events.index("verify")
    assert events.count("pgrep") == 2
    record = read_record(record_path)
    assert record["status"] == "deployed"
    assert record["timestamp"].endswith("Z")
    assert record["checkout_head_before"] == old_head
    assert record["checkout_head_after"] == new_head
    assert record["main_head"] == new_head
    assert record["pin_reinstall_result"]["status"] == "installed"
    assert record["verify_result"] == {"status": "passed", "returncode": 0}


def test_pin_parser_rejects_missing_or_unpinned_required_package():
    malformed = pyproject().replace(THE_LEAGUE_URL.format(OLD_THE_LEAGUE),
                                    "git+https://github.com/nateprich/The-League.git@main")

    with pytest.raises(ff_deploy.DeployError, match="commit-pinned"):
        ff_deploy.parse_pins(malformed)
