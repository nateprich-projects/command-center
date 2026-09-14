"""Implementation packet and runner effects (#815, Phase 3 of #794)."""

from __future__ import annotations

import json
import pathlib
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import implement  # noqa: E402


REPO = "owner/repo"


def ticket(number=42):
    return {
        "ref": "{}#{}".format(REPO, number),
        "number": number,
        "title": "implement the bounded runner",
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "body": "Parent: #7.\n\nWhat: do it.\n\nRisk: escalated",
        "parent": {
            "number": 7,
            "title": "the settled plan",
            "url": "https://github.com/{}/issues/7".format(REPO),
        },
    }


def answer():
    return {
        "done": True,
        "summary": "Added the bounded implementation runner.",
        "departures": [],
    }


def run_git(*args, cwd=None):
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, check=True,
        capture_output=True, text=True,
    )


def make_clone(tmp_path):
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    run_git("init", "--bare", "--quiet", str(remote))
    run_git("init", "--quiet", "-b", "main", str(seed))
    run_git("config", "user.name", "Fixture", cwd=seed)
    run_git("config", "user.email", "fixture@example.test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    run_git("add", "README.md", cwd=seed)
    run_git("commit", "--quiet", "-m", "seed", cwd=seed)
    run_git("remote", "add", "origin", str(remote), cwd=seed)
    run_git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    run_git("--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")
    run_git("clone", "--quiet", str(remote), str(clone))
    run_git("config", "user.name", "Fixture", cwd=clone)
    run_git("config", "user.email", "fixture@example.test", cwd=clone)
    run_git("switch", "--quiet", "-c", "ticket/42", cwd=clone)
    return remote, clone


def test_packet_carries_ticket_plan_verdict_blocking_and_prior_digest():
    prior = {"session": "one.jsonl", "messages": [{"role": "assistant"}]}
    verdict = {
        "pr": 9,
        "head_sha": "abc123",
        "verdict": "rejected",
        "verdict_head_sha": "abc123",
        "blocking": ["cover the empty case"],
    }
    found = implement.build_packet(
        repo=REPO,
        ticket=ticket(),
        plan={"number": 7, "body": "# Plan"},
        verdict=verdict,
        prior_run=prior,
    )
    assert found["repo"] == REPO
    assert found["ticket"]["body"].startswith("Parent: #7")
    assert found["plan"]["body"] == "# Plan"
    assert found["verdict"]["blocking"] == ["cover the empty case"]
    assert found["prior_run"] == prior
    json.dumps(found)


def test_collect_fetches_the_parent_plan_and_open_pr_verdict(monkeypatch):
    calls = []

    def fake_json(*args):
        calls.append(args)
        if args[1:3] == ("issue", "view") and args[3] == "42":
            return ticket()
        if args[1:3] == ("issue", "view") and args[3] == "7":
            return {"number": 7, "title": "plan", "body": "# Plan"}
        if args[1:3] == ("pr", "list"):
            return [{"number": 9, "headRefOid": "abc", "updatedAt": "2026"}]
        raise AssertionError(args)

    monkeypatch.setattr(funnel, "resolve_repo", lambda repo: REPO)
    monkeypatch.setattr(funnel, "_gh_json", fake_json)
    monkeypatch.setattr(
        funnel, "latest_verdict",
        lambda repo, pr: {
            "verdict": "rejected", "head_sha": "abc",
            "blocking": ["fix the fixture"],
        },
    )
    monkeypatch.setattr(implement, "fetch_prior_run", lambda number, agent: {"ok": True})

    found = implement.collect(REPO, 42)
    assert found["plan"]["ref"] == REPO + "#7"
    assert found["verdict"]["blocking"] == ["fix the fixture"]
    assert found["prior_run"] == {"ok": True}


@pytest.mark.parametrize(
    "value",
    (
        [],
        {},
        {"done": False, "summary": "x", "departures": []},
        {"done": True, "summary": "", "departures": []},
        {"done": True, "summary": "x", "departures": "none"},
        {"done": True, "summary": "x", "departures": [], "extra": True},
    ),
)
def test_answer_validation_fails_closed(tmp_path, value):
    path = tmp_path / "answer.json"
    path.write_text(json.dumps(value))
    with pytest.raises(implement.ImplementError):
        implement.read_answer(str(path))


def test_finish_ticket_pushes_opens_pr_releases_and_finishes(tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    effects = {"prs": [], "released": [], "finished": []}

    def open_pr(repo, context, found_ticket, body):
        effects["prs"].append((repo, context, found_ticket, body))
        return {"number": 91, "url": "https://github.com/owner/repo/pull/91"}

    result = implement.finish_done(
        answer(),
        run="run-42",
        repo=REPO,
        cwd=clone,
        test_commands=[[sys.executable, "-c", "raise SystemExit(0)"]],
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        pr_effect=open_pr,
    )

    assert result["number"] == 91
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "done", "PR #91", REPO + "#42")
    ]
    (repo, context, found_ticket, body), = effects["prs"]
    assert repo == REPO and context["branch"] == "ticket/42"
    assert found_ticket["number"] == 42
    assert "Summary:\nAdded the bounded implementation runner." in body
    assert "Departures:\n- None." in body
    assert "Created fresh from origin/main" in body

    pushed = run_git(
        "--git-dir", str(remote), "show", "ticket/42:implemented.txt"
    ).stdout
    assert pushed == "done\n"


def test_finish_ticket_releases_and_errors_when_tests_fail(tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    effects = {"released": [], "finished": []}

    def no_pr(repo, context, found_ticket, body):
        raise AssertionError("a failing checkout must not open a PR")

    with pytest.raises(implement.ImplementError, match="SystemExit"):
        implement.finish_done(
            answer(),
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "raise SystemExit(3)"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            pr_effect=no_pr,
        )

    assert effects["released"] == [REPO + "#42"]
    (finished,), = [effects["finished"]]
    assert finished[:3] == ("codex", "run-42", "errored")
    assert finished[3].startswith("tests failed: ")
    assert "SystemExit(3)" in finished[3]
    assert finished[4] == REPO + "#42"

    refs = run_git("--git-dir", str(remote), "show-ref").stdout
    assert "ticket/42" not in refs
    dirty = run_git("status", "--porcelain", cwd=clone).stdout.strip()
    assert "implemented.txt" in dirty


def test_finish_ticket_requires_the_deterministic_branch(tmp_path):
    _, clone = make_clone(tmp_path)
    run_git("switch", "--quiet", "main", cwd=clone)
    with pytest.raises(implement.ImplementError, match="ticket/<number>"):
        implement.checkout_context(clone)


def test_test_discovery_does_not_mistake_javascript_tests_for_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "runner.test.js").write_text("// test\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node --test"}}')
    assert implement.default_test_commands(tmp_path) == [["npm", "test"]]


def test_test_discovery_runs_both_suites_in_a_mixed_repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_runner.py").write_text("def test_ok(): pass\n")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node --test"}}')
    assert implement.default_test_commands(tmp_path) == [
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        ["npm", "test"],
    ]


def test_pr_template_lists_departures_and_verification():
    found = implement.render_pr_body(
        ticket(),
        {**answer(), "departures": ["Kept the old entry point for compatibility."]},
        continued=True,
        tests=["python3 -m pytest -q"],
    )
    assert "Part of #7." in found
    assert "- Kept the old entry point for compatibility." in found
    assert "Continued the existing remote ticket branch." in found
    assert "- `python3 -m pytest -q`" in found


def test_funnel_finish_ticket_forwards_without_importing_engine(monkeypatch):
    seen = []

    def fake_run(command):
        seen.append(command)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(funnel.subprocess, "run", fake_run)
    assert funnel.main(["finish-ticket", "--answer", "answer.json", "--run", "r"]) == 7
    assert seen[0][0] == sys.executable
    assert pathlib.Path(seen[0][1]).name == "finish-ticket"


def test_entry_points_are_executable():
    for name in ("implement-packet", "finish-ticket"):
        path = ROOT / name
        assert path.exists()
        assert path.stat().st_mode & stat.S_IXUSR


def test_engine_imports_funnel_and_funnel_does_not_import_engine():
    assert "import funnel" in (ROOT / "engine" / "implement.py").read_text()
    source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in source
    assert "from engine" not in source
