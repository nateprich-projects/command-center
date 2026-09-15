"""Implementation packet and runner effects (#815, #816, Phase 3 of #794)."""

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


def blocked(reason="entering a credential", action="Approve the OAuth app"):
    return {"blocked_on_human": {"reason": reason, "action": action}}


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
    ("argv", "agent"),
    (
        (["42", "--repo", REPO], "codex"),
        (["42", "--repo", REPO, "--agent", "muse"], "muse"),
        (["42", "--repo", REPO, "--agent", "claude"], "claude"),
    ),
)
def test_packet_main_passes_the_agent_to_the_prior_run_digest(
        monkeypatch, capsys, argv, agent):
    """The digest must read the runner's own sessions: a Muse packet carrying
    a Codex session's intent is confident evidence about the wrong run."""
    seen = {}

    def fake_collect(repo, number, **kwargs):
        seen.update(repo=repo, number=number, **kwargs)
        return {"ticket": {"number": number}}

    monkeypatch.setattr(implement, "collect", fake_collect)
    assert implement.packet_main(argv) == 0
    assert seen == {"repo": REPO, "number": 42, "agent": agent}
    assert json.loads(capsys.readouterr().out)["ticket"]["number"] == 42


@pytest.mark.parametrize(
    "value",
    (
        [],
        {},
        {"done": False, "summary": "x", "departures": []},
        {"done": True, "summary": "", "departures": []},
        {"done": True, "summary": "x", "departures": "none"},
        {"done": True, "summary": "x", "departures": [], "extra": True},
        {"blocked_on_human": {"reason": "it is hard", "action": "x"}},
        {"blocked_on_human": {"reason": "Entering a Credential",
                              "action": "x"}},
        {"blocked_on_human": {"reason": "entering a credential",
                              "action": "  "}},
        {"blocked_on_human": {"reason": "entering a credential"}},
        {"blocked_on_human": {"reason": "entering a credential",
                              "action": "x", "extra": True}},
        {"blocked_on_human": "entering a credential"},
        {"declined": ""},
        {"declined": "  "},
        {"declined": 42},
        {"declined": "stale", "extra": True},
        {"done": True, "summary": "x", "departures": [],
         "declined": "stale"},
        {"blocked_on_human": {"reason": "entering a credential",
                              "action": "x"},
         "declined": "stale"},
        {"unknown": True},
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

    assert "work kept on ticket/42" in finished[3]

    # The work survives the failure (#877): pushed on the ticket branch as a
    # WIP commit, with no PR opened.
    pushed = run_git(
        "--git-dir", str(remote), "show", "ticket/42:implemented.txt"
    ).stdout
    assert pushed == "done\n"
    subject = run_git("--git-dir", str(remote), "log", "-1", "--format=%s",
                      "ticket/42").stdout.strip()
    assert subject == "WIP #42: tests failing"


def test_a_failure_note_names_the_failing_tests():
    output = (
        "python3 -m pytest -q failed: ....F..E [ 4%]\n"
        "FAILED tests/test_a.py::test_one - AssertionError\n"
        "ERROR tests/test_b.py::test_two - Failed: boom\n"
        "FAILED tests/test_a.py::test_one - AssertionError\n"
        "==== 1 failed, 90 passed, 1 error in 3.2s ====\n"
    )
    note = implement._failure_note(implement.ImplementError(output), "work kept on ticket/9")
    assert "tests/test_a.py::test_one; tests/test_b.py::test_two" in note
    assert "1 failed, 90 passed, 1 error in 3.2s" in note
    assert note.endswith("work kept on ticket/9")


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


@pytest.mark.parametrize("reason", funnel.HUMAN_STEP_REASONS)
def test_blocked_answer_accepts_each_allowlisted_reason(tmp_path, reason):
    path = tmp_path / "answer.json"
    path.write_text(json.dumps(blocked(reason=reason, action="  Do it  ")))
    assert implement.read_answer(str(path)) == {
        "blocked_on_human": {"reason": reason, "action": "Do it"}
    }


def test_declined_answer_returns_the_trimmed_reason(tmp_path):
    path = tmp_path / "answer.json"
    path.write_text(json.dumps({"declined": "  prerequisite has not landed  "}))
    assert implement.read_answer(str(path)) == {
        "declined": "prerequisite has not landed"
    }


@pytest.mark.parametrize("reason", funnel.HUMAN_STEP_REASONS)
def test_human_step_body_carries_the_exact_marker_line(reason):
    body = implement.render_human_step_body(
        parent_number=7, ticket_number=42, reason=reason,
        action="Approve the OAuth app",
    )
    assert funnel.HUMAN_STEP_LINE.search(body) is not None
    assert funnel.parse_human_step(body) == reason
    assert "Human step: {}".format(reason) in body.splitlines()
    assert body.startswith("Part of #7; discovered while implementing #42.")
    assert body.rstrip().endswith("Risk: standard")
    assert implement.render_human_step_title("Approve the OAuth app") == (
        "Human step: Approve the OAuth app"
    )


def test_parse_created_number_reads_the_issue_url():
    assert implement.parse_created_number(
        "https://github.com/owner/repo/issues/99\n") == 99
    with pytest.raises(funnel.GitHubError):
        implement.parse_created_number("nothing useful\n")


def test_finish_blocked_on_human_files_blocks_comments_and_finishes(
        tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    (clone / "halfway.txt").write_text("not finished\n")
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    effects = {"created": [], "blocked": [], "comments": [], "released": [],
               "finished": []}

    def create(repo, parent, title, body, **kwargs):
        effects["created"].append((repo, parent, title, body))
        return {"number": 43,
                "ref": "{}#43".format(repo),
                "url": "https://github.com/{}/issues/43".format(repo)}

    result = implement.finish_blocked_on_human(
        blocked()["blocked_on_human"],
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        create_effect=create,
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
    )

    assert result == {"ticket": REPO + "#42",
                      "human_step": {"number": 43,
                                     "ref": REPO + "#43",
                                     "url": "https://github.com/{}/issues/43".format(REPO)}}
    (repo, parent, title, body), = effects["created"]
    assert repo == REPO and parent == 7
    assert title == "Human step: Approve the OAuth app"
    assert funnel.parse_human_step(body) == "entering a credential"
    ((_, number), kwargs), = effects["blocked"]
    assert number == 42 and kwargs["blocked_by"] == 43
    (comment_args, comment_kwargs), = effects["comments"]
    assert comment_args[1] == 42
    assert comment_kwargs["run"] == "run-42"
    assert comment_args[2] == (
        "**Blocked on #43:** Complete the human step before resuming "
        "this ticket."
    )
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "skipped-human-step",
         "stopped: human step filed as #43; ticket blocked; no PR opened",
         REPO + "#42")
    ]

    refs = run_git("--git-dir", str(remote), "show-ref").stdout
    assert "ticket/42" not in refs
    dirty = run_git("status", "--porcelain", cwd=clone).stdout.strip()
    assert "halfway.txt" in dirty


def test_finish_blocked_on_human_requires_a_parent(tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    orphan = ticket(42)
    del orphan["parent"]
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: orphan)

    effects = {"released": [], "finished": []}
    with pytest.raises(implement.ImplementError, match="parent"):
        implement.finish_blocked_on_human(
            blocked()["blocked_on_human"],
            run="run-42",
            repo=REPO,
            cwd=clone,
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
        )
    assert effects == {"released": [], "finished": []}


def test_finish_blocked_on_human_names_what_exists_when_comment_fails(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    def fail_comment(*args, **kwargs):
        raise funnel.GitHubError("comment failed")

    with pytest.raises(funnel.GitHubError, match="already created"):
        implement.finish_blocked_on_human(
            blocked()["blocked_on_human"],
            run="run-42",
            repo=REPO,
            cwd=clone,
            release=lambda ref: (_ for _ in ()).throw(
                AssertionError("a partial failure must hold the claim")),
            heartbeat_finish=lambda *args: (_ for _ in ()).throw(
                AssertionError("a partial failure must not finish")),
            create_effect=lambda *args, **kwargs: {
                "number": 43, "ref": REPO + "#43",
                "url": "https://github.com/{}/issues/43".format(REPO)},
            block_effect=lambda *args, **kwargs: None,
            comment_effect=fail_comment,
        )


def test_finish_declined_labels_comments_releases_and_finishes(
        tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    (clone / "halfway.txt").write_text("not finished\n")
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    effects = {"blocked": [], "comments": [], "released": [], "finished": []}

    result = implement.finish_declined(
        "prerequisite has not landed",
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
    )

    assert result == {"ticket": REPO + "#42",
                      "declined": "prerequisite has not landed"}
    ((_, number), kwargs), = effects["blocked"]
    assert number == 42 and kwargs.get("blocked_by") is None
    (comment_args, _), = effects["comments"]
    assert comment_args[2] == "**Declined:** prerequisite has not landed"
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "skipped-blocked",
         "declined: prerequisite has not landed", REPO + "#42")
    ]

    refs = run_git("--git-dir", str(remote), "show-ref").stdout
    assert "ticket/42" not in refs
    dirty = run_git("status", "--porcelain", cwd=clone).stdout.strip()
    assert "halfway.txt" in dirty


def test_finish_main_routes_blocked_and_declined_answers(
        tmp_path, monkeypatch, capsys):
    routed = {}

    def fake_blocked(blocked_answer, **kwargs):
        routed["blocked"] = (blocked_answer, kwargs)
        return {"ticket": REPO + "#42", "human_step": {"number": 43}}

    def fake_declined(reason, **kwargs):
        routed["declined"] = (reason, kwargs)
        return {"ticket": REPO + "#42", "declined": reason}

    monkeypatch.setattr(implement, "finish_blocked_on_human", fake_blocked)
    monkeypatch.setattr(implement, "finish_declined", fake_declined)

    path = tmp_path / "answer.json"
    path.write_text(json.dumps(blocked()))
    assert implement.finish_main(
        ["--answer", str(path), "--run", "run-42",
         "--agent", "muse", "--repo", REPO]) == 0
    assert routed["blocked"][0] == blocked()["blocked_on_human"]
    assert routed["blocked"][1]["run"] == "run-42"
    assert routed["blocked"][1]["agent"] == "muse"
    assert json.loads(capsys.readouterr().out)["human_step"]["number"] == 43

    path.write_text(json.dumps({"declined": "stale"}))
    assert implement.finish_main(
        ["--answer", str(path), "--run", "run-42"]) == 0
    assert routed["declined"][0] == "stale"
    assert json.loads(capsys.readouterr().out)["declined"] == "stale"

    path.write_text(json.dumps({"unknown": True}))
    assert implement.finish_main(
        ["--answer", str(path), "--run", "run-42"]) == 1
