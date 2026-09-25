"""Implementation packet and runner effects (#815, #816, Phase 3 of #794)."""

from __future__ import annotations

import json
import pathlib
import shlex
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

import heartbeat

ROOT = pathlib.Path(__file__).resolve().parent.parent
NO_DIFF_FINISH = (
    pathlib.Path(__file__).parent / "fixtures" /
    "no_diff_done_finish.json"
)
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import implement  # noqa: E402


REPO = "owner/repo"
ACCEPT_BODY_CONFLICT_REASON = (
    "The requested edit to routines/muse-implement.md is blocked by the "
    "plan's active #794 routine freeze. Only tickets under #794 or #1044 "
    "are exempt; #1257 is under #1251, and the current verdict confirms "
    "the conflict. No change was made; wait for #794 to land."
)


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
    # The packet now also carries the target repo's AGENTS.md (#1256); this
    # test is about the plan and verdict reads, so stub that one.
    monkeypatch.setattr(
        implement, "fetch_agents_md", lambda repo: ("# Rules\n", False, False)
    )

    found = implement.collect(REPO, 42)
    assert found["plan"]["ref"] == REPO + "#7"
    assert found["verdict"]["blocking"] == ["fix the fixture"]
    assert found["prior_run"] == {"ok": True}
    assert found["agents_md"] == "# Rules\n"


def cross_repo_ticket():
    found = ticket()
    found["parent"] = {
        "number": 1054,
        "title": "point every member repo at the runners",
        "url": "https://github.com/owner/hub/issues/1054",
    }
    return found


def test_fetch_plan_reads_a_parent_in_another_repository(monkeypatch):
    # command-center#1054 parents tickets in every member repo. Reading it
    # from the ticket's repo failed every packet and looped Codex on #129.
    seen = []

    def fake_json(*args):
        seen.append(args)
        return {"number": 1054, "title": "plan", "body": "# Plan"}

    monkeypatch.setattr(funnel, "_gh_json", fake_json)
    found = implement.fetch_plan(REPO, cross_repo_ticket())
    assert seen[0][5] == "owner/hub"
    assert found["ref"] == "owner/hub#1054"


def test_pr_body_names_a_parent_in_another_repository():
    found = implement.render_pr_body(
        cross_repo_ticket(), answer(), continued=False, tests=[])
    assert "Part of owner/hub#1054." in found


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
        {"done": True, "summary": "x", "departures": [], "evidence": []},
        {"done": True, "summary": "x", "departures": [],
         "evidence": "https://github.com/nateprich-projects/repo/issues/1"},
        {"done": True, "summary": "x", "departures": [],
         "evidence": [""]},
        {"done": True, "summary": "x", "departures": [],
         "evidence": [None]},
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


def test_done_answer_accepts_optional_nonempty_evidence_list():
    found = implement.parse_answer(json.dumps({
        **answer(),
        "evidence": [
            " https://github.com/nateprich-projects/repo/issues/12 ",
        ],
    }))

    assert found["evidence"] == [
        "https://github.com/nateprich-projects/repo/issues/12",
    ]


def test_no_diff_with_verified_evidence_closes_and_finishes(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.setattr(
        heartbeat, "read_github",
        lambda agent: [{"run": "run-42", "phase": "start", "ts": 1000}],
    )
    evidence = [
        "https://github.com/nateprich-projects/project/issues/12"
        "#issuecomment-91",
        "https://github.com/nateprich-projects/project/pull/8",
        "https://github.com/nateprich-projects/project/issues/9",
    ]
    responses = {
        "repos/nateprich-projects/project/issues/comments/91": {
            "id": 91,
            "url": "https://api.github.com/repos/nateprich-projects/"
                   "project/issues/comments/91",
            "html_url": evidence[0],
            "created_at": "1970-01-01T00:16:40Z",
        },
        "repos/nateprich-projects/project/pulls/8": {
            "number": 8,
            "url": "https://api.github.com/repos/nateprich-projects/"
                   "project/pulls/8",
            "html_url": evidence[1],
            "state": "closed",
            "closed_at": "1970-01-01T00:16:41Z",
        },
        "repos/nateprich-projects/project/issues/9": {
            "number": 9,
            "url": "https://api.github.com/repos/nateprich-projects/"
                   "project/issues/9",
            "html_url": evidence[2],
            "state": "closed",
            "closed_at": "1970-01-01T00:16:42Z",
        },
    }
    reads = []

    def read_api(endpoint):
        reads.append(endpoint)
        return responses.get(endpoint)

    monkeypatch.setattr(funnel, "_gh_api_json", read_api)
    effects = {"closed": [], "released": [], "finished": []}

    def close(repo, number, *, cwd):
        effects["closed"].append((repo, number, cwd))

    result = implement.finish_done(
        {**answer(), "evidence": evidence},
        run="run-42",
        repo=REPO,
        cwd=clone,
        test_commands=[[sys.executable, "-c", "pass"]],
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        pr_effect=lambda *args: pytest.fail("no-diff completion must not open a PR"),
        close_effect=close,
    )

    assert reads == list(responses)
    assert result == {
        "number": 42,
        "url": ticket(42)["url"],
        "closed": True,
    }
    assert effects["closed"] == [(REPO, 42, clone)]
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][:3] == ("codex", "run-42", "done")
    assert effects["finished"][0][3] == json.loads(
        NO_DIFF_FINISH.read_text()
    )["note"]
    for url in evidence:
        assert url in effects["finished"][0][3]


def test_no_diff_without_evidence_fails_named_without_effects(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.setattr(
        heartbeat, "read_github",
        lambda agent: pytest.fail("missing evidence must fail before the heartbeat read"),
    )
    effects = {"closed": [], "released": [], "finished": []}

    with pytest.raises(
        implement.ImplementError,
        match="done answer produced no change and named no evidence",
    ):
        implement.finish_done(
            answer(),
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "pass"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            close_effect=lambda *args, **kwargs: effects["closed"].append(args),
        )

    assert effects == {"closed": [], "released": [], "finished": []}


def test_no_diff_fails_closed_when_heartbeat_start_cannot_be_read(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.setattr(heartbeat, "read_github", lambda agent: [])
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint: pytest.fail("unreadable start must fail before artifact reads"),
    )
    url = "https://github.com/nateprich-projects/project/issues/12#issuecomment-91"
    effects = {"closed": [], "released": [], "finished": []}

    with pytest.raises(
        implement.ImplementError,
        match="could not read heartbeat start for run run-42",
    ):
        implement.finish_done(
            {**answer(), "evidence": [url]},
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "pass"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            close_effect=lambda *args, **kwargs: effects["closed"].append(args),
        )

    assert effects == {"closed": [], "released": [], "finished": []}


def test_no_diff_rejects_evidence_created_before_run_start(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.setattr(
        heartbeat, "read_github",
        lambda agent: [{"run": "run-42", "phase": "start", "ts": 2000}],
    )
    url = (
        "https://github.com/nateprich-projects/project/issues/12"
        "#issuecomment-91"
    )
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint: {
            "id": 91,
            "url": "https://api.github.com/repos/nateprich-projects/"
                   "project/issues/comments/91",
            "html_url": url,
            "created_at": "1970-01-01T00:16:40Z",
        },
    )
    effects = {"closed": [], "released": [], "finished": []}

    with pytest.raises(
        implement.ImplementError,
        match="evidence URL .* was created before this run started",
    ) as raised:
        implement.finish_done(
            {**answer(), "evidence": [url]},
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "pass"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            close_effect=lambda *args, **kwargs: effects["closed"].append(args),
        )

    assert url in str(raised.value)
    assert effects == {"closed": [], "released": [], "finished": []}


def test_no_diff_fails_closed_when_evidence_cannot_be_read(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.setattr(
        heartbeat, "read_github",
        lambda agent: [{"run": "run-42", "phase": "start", "ts": 1000}],
    )
    url = "https://github.com/nateprich-projects/project/issues/12#issuecomment-91"
    monkeypatch.setattr(funnel, "_gh_api_json", lambda endpoint: None)
    effects = {"closed": [], "released": [], "finished": []}

    with pytest.raises(
        implement.ImplementError,
        match="could not read evidence URL",
    ) as raised:
        implement.finish_done(
            {**answer(), "evidence": [url]},
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "pass"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            close_effect=lambda *args, **kwargs: effects["closed"].append(args),
        )

    assert url in str(raised.value)
    assert effects == {"closed": [], "released": [], "finished": []}


def test_done_with_diff_ignores_evidence(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.setattr(
        heartbeat, "read_github",
        lambda agent: pytest.fail("the diff path must ignore evidence"),
    )
    monkeypatch.setattr(
        funnel, "_gh_api_json",
        lambda endpoint: pytest.fail("the diff path must ignore evidence"),
    )
    prs = []
    effects = {"released": [], "finished": []}

    result = implement.finish_done(
        {**answer(), "evidence": ["https://example.com/ignored"]},
        run="run-42",
        repo=REPO,
        cwd=clone,
        test_commands=[[sys.executable, "-c", "pass"]],
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        pr_effect=lambda repo, context, found_ticket, body: (
            prs.append(body) or {"number": 91, "url": "https://github.com/owner/repo/pull/91"}
        ),
    )

    assert result["number"] == 91
    assert prs and "Summary:\nAdded the bounded implementation runner." in prs[0]
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][:3] == ("codex", "run-42", "done")


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


def test_pre_pr_stray_check_names_answer_and_never_opens_a_pr(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    (clone / "implemented.txt").write_text("done\n")
    (clone / "answer.json").write_text(
        '{"done":true,"summary":"private run summary"}\n'
    )
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    effects = {"prs": [], "released": [], "finished": []}

    with pytest.raises(implement.StrayFileError, match="answer.json"):
        implement.finish_done(
            answer(),
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "pass"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            pr_effect=lambda *args: effects["prs"].append(args),
        )

    assert effects["prs"] == []
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][:3] == (
        "codex", "run-42", "errored",
    )
    assert "answer.json" in effects["finished"][0][3]
    assert run_git("diff", "--cached", "--name-only", cwd=clone).stdout.splitlines() == [
        "answer.json", "implemented.txt",
    ]
    assert "refs/heads/ticket/42" not in run_git(
        "--git-dir", str(tmp_path / "origin.git"), "show-ref"
    ).stdout


@pytest.mark.parametrize("name", ("packet.json", "scratch.txt", "run-output.json"))
def test_pre_pr_stray_check_catches_other_run_scratch(tmp_path, name):
    _, clone = make_clone(tmp_path)
    (clone / name).write_text("scratch\n")

    with pytest.raises(implement.StrayFileError, match=name):
        implement._commit_if_needed(clone, 42, "Added the implementation.")


def test_pre_pr_stray_check_passes_a_clean_checkout(tmp_path):
    _, clone = make_clone(tmp_path)

    implement._check_no_run_scratch(clone)


def test_pre_pr_stray_check_sees_scratch_already_on_the_ticket_branch(tmp_path):
    remote, clone = make_clone(tmp_path)
    (clone / "answer.json").write_text("old run\n")
    run_git("add", "answer.json", cwd=clone)
    run_git("commit", "--quiet", "-m", "old run", cwd=clone)
    run_git("push", "--quiet", "-u", "origin", "ticket/42", cwd=clone)

    with pytest.raises(implement.StrayFileError, match="answer.json"):
        implement._check_no_run_scratch(clone)

    assert "refs/heads/ticket/42" in run_git(
        "--git-dir", str(remote), "show-ref"
    ).stdout


def test_staging_uses_an_explicit_sorted_file_list(monkeypatch, tmp_path):
    seen = []
    (tmp_path / "new.txt").write_text("new\n")
    (tmp_path / "answer.json").write_text("{}\n")

    def fake_run(command, **kwargs):
        seen.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(implement, "_run", fake_run)
    implement._stage_explicit_paths(
        tmp_path, ["new.txt", "answer.json", "new.txt"]
    )

    assert seen[-1] == ["git", "add", "--", "answer.json", "new.txt"]


def test_commit_stages_a_git_rm_deletion_without_failing(tmp_path):
    """#1214: `git add` cannot match a path `git rm` removed from the index."""
    _, clone = make_clone(tmp_path)
    (clone / "removed-with-git-rm.txt").write_text("gone\n")
    (clone / "removed-with-rm.txt").write_text("also gone\n")
    (clone / "edited.txt").write_text("before\n")
    run_git("add", "-A", cwd=clone)
    run_git("commit", "--quiet", "-m", "files to remove", cwd=clone)

    run_git("rm", "--quiet", "removed-with-git-rm.txt", cwd=clone)
    (clone / "removed-with-rm.txt").unlink()
    (clone / "edited.txt").write_text("after\n")
    (clone / "added.txt").write_text("new\n")

    assert implement._commit_if_needed(clone, 1214, "remove the old routine")

    shipped = set(run_git(
        "show", "--name-status", "--format=", "HEAD", cwd=clone,
    ).stdout.split())
    assert {"D", "M", "A"} <= shipped
    assert "removed-with-git-rm.txt" in shipped
    assert "removed-with-rm.txt" in shipped
    assert "edited.txt" in shipped
    assert "added.txt" in shipped
    assert run_git("status", "--porcelain", cwd=clone).stdout == ""


def test_addable_paths_keeps_only_what_git_add_can_match(tmp_path):
    _, clone = make_clone(tmp_path)
    (clone / "staged-deletion.txt").write_text("gone\n")
    (clone / "kept.txt").write_text("kept\n")
    run_git("add", "-A", cwd=clone)
    run_git("commit", "--quiet", "-m", "two files", cwd=clone)
    run_git("rm", "--quiet", "staged-deletion.txt", cwd=clone)
    (clone / "untracked.txt").write_text("new\n")

    found = implement._addable_paths(
        clone, ["staged-deletion.txt", "kept.txt", "untracked.txt"]
    )

    assert found == ["kept.txt", "untracked.txt"]


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
    assert funnel.main([
        "finish-ticket", "--answer-file", "answer.json", "--run", "r"
    ]) == 7
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


@pytest.mark.parametrize("reason", implement.BLOCKED_ON_HUMAN_REASONS)
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


@pytest.mark.parametrize("reason", implement.BLOCKED_ON_HUMAN_REASONS)
def test_human_step_body_carries_the_reason_as_prose(reason):
    body = implement.render_human_step_body(
        parent_number=7, ticket_number=42, reason=reason,
        action="Approve the OAuth app",
    )
    assert "Human step: {}".format(reason) in body.splitlines()
    assert body.startswith("Part of #7; discovered while implementing #42.")
    assert "Risk:" not in body
    assert body.rstrip().endswith("Action Nate must perform: Approve the OAuth app.")
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
               "finished": [], "needs": []}

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
        needs_effect=lambda url, ref: effects["needs"].append((url, ref)),
    )

    assert result == {"ticket": REPO + "#42",
                      "human_step": {"number": 43,
                                     "ref": REPO + "#43",
                                     "url": "https://github.com/{}/issues/43".format(REPO)}}
    (repo, parent, title, body), = effects["created"]
    assert repo == REPO and parent == 7
    assert title == "Human step: Approve the OAuth app"
    assert "Human step: entering a credential" in body.splitlines()
    assert effects["needs"] == [
        ("https://github.com/{}/issues/43".format(REPO), REPO + "#43")
    ]
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

    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "needs": [], "human_needs": []}

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
        needs_effect=lambda *args: pytest.fail(
            "an unknown decline must not stay in the agent lane"),
        human_needs_effect=lambda url, ref: effects["human_needs"].append(
            (url, ref)),
    )

    assert result == {"ticket": REPO + "#42",
                      "declined": "prerequisite has not landed"}
    ((_, number), kwargs), = effects["blocked"]
    assert number == 42 and kwargs.get("blocked_by") is None
    (comment_args, _), = effects["comments"]
    assert comment_args[2] == "**Declined:** prerequisite has not landed"
    assert effects["released"] == [REPO + "#42"]
    assert effects["needs"] == []
    assert effects["human_needs"] == [
        ("https://github.com/{}/issues/42".format(REPO), REPO + "#42")]
    assert effects["finished"] == [
        ("codex", "run-42", "skipped-blocked",
         "declined: prerequisite has not landed", REPO + "#42")
    ]

    refs = run_git("--git-dir", str(remote), "show-ref").stdout
    assert "ticket/42" not in refs
    dirty = run_git("status", "--porcelain", cwd=clone).stdout.strip()
    assert "halfway.txt" in dirty


@pytest.mark.parametrize(("reason", "prerequisite"), [
    (
        "Unlanded prerequisite #165 (Effective-dated rate table prices each "
        "outcome record in dollars) is still open.",
        REPO + "#165",
    ),
    (
        "Ticket #705 names connector skeleton #702 as a prerequisite; #702 "
        "remains open and its scaffold is absent from origin/main.",
        REPO + "#702",
    ),
])
def test_finish_declined_open_prerequisite_records_only_native_edge(
        tmp_path, monkeypatch, reason, prerequisite):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = {"looked_up": [], "edges": [], "comments": [],
               "released": [], "finished": []}

    result = implement.finish_declined(
        reason,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: pytest.fail(
            "an open prerequisite must not add the blocked label"),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda *args: pytest.fail(
            "a prerequisite edge must leave Needs unchanged"),
        prerequisite_open_effect=lambda ref: (
            effects["looked_up"].append(ref) or True),
        prerequisite_edge_effect=lambda repo, number, ref, **kwargs:
            effects["edges"].append((repo, number, ref, kwargs)),
    )

    assert result == {"ticket": REPO + "#42", "declined": reason}
    assert effects["looked_up"] == [prerequisite]
    assert effects["edges"][0][:3] == (REPO, 42, prerequisite)
    (comment_args, _), = effects["comments"]
    assert comment_args[2] == "**Declined:** {}".format(reason)
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "skipped-blocked",
         "declined: {}".format(reason), REPO + "#42")
    ]


def test_accept_body_conflict_branch_skips_blocked_label_and_routes_to_review(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "needs": []}

    result = implement.finish_declined(
        ACCEPT_BODY_CONFLICT_REASON,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda url, ref: effects["needs"].append(
            ("agent", url, ref)),
        human_needs_effect=lambda *args: pytest.fail(
            "a pointed Accept conflict must not ask Nate"),
        prerequisite_open_effect=lambda ref: pytest.fail(
            "an Accept conflict must not be treated as a prerequisite"),
    )

    assert result == {"ticket": REPO + "#42",
                      "declined": ACCEPT_BODY_CONFLICT_REASON}
    # The Accept-body-conflict branch skips the `blocked` label and human hold.
    assert effects["blocked"] == []
    assert effects["needs"] == [
        ("agent", ticket()["url"], REPO + "#42")]
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][:3] == (
        "codex", "run-42", "skipped-blocked")

    assert len(effects["comments"]) == 2
    decline_comment = effects["comments"][0][0][2]
    assert decline_comment == "{} {}".format(
        funnel.DECLINED_PREFIX, ACCEPT_BODY_CONFLICT_REASON)
    route_comment = effects["comments"][1][0][2]
    assert route_comment.startswith(
        "**Review routing: Accept/body conflict**\n\n"
        + implement.DECLINE_REVIEW_ROUTING_MARKER
        + "\n```json\n")
    payload = json.loads(route_comment.split("```json\n", 1)[1].split(
        "\n```", 1)[0])
    assert payload == {
        "type": "accept-body-conflict",
        "decline_excerpt": ACCEPT_BODY_CONFLICT_REASON,
        "conflict_pointer": "plan.md#routine-freeze-while-794-lands",
    }


def test_finish_declined_unparseable_conflict_pointer_asks_nate_and_blocks(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    reason = (
        "The ticket Accept contradicts the current repo rules, but the "
        "conflict pointer is not parseable."
    )
    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "needs": [], "human_needs": []}

    implement.finish_declined(
        reason,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda *args: pytest.fail(
            "an unparseable decline must not stay in the agent lane"),
        human_needs_effect=lambda url, ref: effects["human_needs"].append(
            ("human", url, ref)),
    )

    assert len(effects["blocked"]) == 1
    assert effects["blocked"][0][0][1] == 42
    assert effects["needs"] == []
    assert effects["human_needs"] == [
        ("human", ticket()["url"], REPO + "#42")]
    assert effects["released"] == [REPO + "#42"]
    assert len(effects["comments"]) == 1
    assert effects["comments"][0][0][2] == "{} {}".format(
        funnel.DECLINED_PREFIX, reason)
    assert effects["finished"][0][:3] == (
        "codex", "run-42", "skipped-blocked")


def test_finish_declined_failed_review_handoff_falls_back_to_blocked(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "human_needs": []}

    def comment_effect(repo, number, body, **kwargs):
        effects["comments"].append(body)
        if len(effects["comments"]) == 2:
            raise funnel.GitHubError("could not post the review handoff")

    implement.finish_declined(
        ACCEPT_BODY_CONFLICT_REASON,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=comment_effect,
        needs_effect=lambda *args: None,
        human_needs_effect=lambda url, ref: effects["human_needs"].append(
            ("human", url, ref)),
    )

    assert len(effects["blocked"]) == 1
    assert effects["blocked"][0][0] == (REPO, 42)
    assert effects["comments"][0] == "{} {}".format(
        funnel.DECLINED_PREFIX, ACCEPT_BODY_CONFLICT_REASON)
    assert implement.DECLINE_REVIEW_ROUTING_MARKER in effects["comments"][1]
    assert effects["human_needs"] == [
        ("human", ticket()["url"], REPO + "#42")]
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][2] == "skipped-blocked"
    assert "review routing failed; ticket left blocked" in effects["finished"][0][3]


def defer_note_proof_ticket(number=42):
    found = ticket(number)
    found["body"] = (
        "Only if ticket 0 shaping instruction edit mechanism also covers it "
        "with no widened scope: stop narrative repeating runner-owned "
        "structured sections as in #1268 to single rendering. If not "
        "covered, make no code change and record deferral as separate idea "
        "in close note. Proof: single rendering with no scope widening, or "
        "explicit defer note with no code.\n\nRisk: standard"
    )
    return found


DEFER_NOTE_REASON = (
    "Defer narrative dedup as a separate idea in this close note. The named "
    "condition for including it—coverage by the same shaping edit—is absent "
    "from merged #1369, whose change reviews Scope and escalated-risk signals "
    "but does not change narrative rendering. No code changed."
)


def test_finish_declined_closes_allowed_defer_note_proof_as_completed(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: defer_note_proof_ticket(number))
    effects = {"closed": [], "blocked": [], "comments": [], "needs": [],
               "released": [], "finished": []}

    result = implement.finish_declined(
        DEFER_NOTE_REASON,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda *args: effects["needs"].append(args),
        defer_note_close_effect=lambda repo, number, reason, **kwargs:
            effects["closed"].append((repo, number, reason, kwargs)),
    )

    assert result == {"ticket": REPO + "#42", "declined": DEFER_NOTE_REASON}
    assert effects["closed"][0][:3] == (REPO, 42, DEFER_NOTE_REASON)
    assert effects["closed"][0][3] == {
        "run": "run-42", "agent": "codex", "cwd": clone,
    }
    assert effects["blocked"] == []
    assert effects["comments"] == []
    assert effects["needs"] == []
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "done",
         "closed as completed: allowed defer-note proof", REPO + "#42")
    ]


def test_finish_declined_blocks_same_reason_when_accept_rejects_defer_note(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    rejected = ticket()
    rejected["body"] = (
        "Accept: a defer note is not accepted as proof of completion."
    )
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: rejected)
    effects = {"closed": [], "blocked": [], "comments": [], "needs": [],
               "human_needs": [], "released": [], "finished": []}

    implement.finish_declined(
        DEFER_NOTE_REASON,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda *args: effects["needs"].append(args),
        human_needs_effect=lambda url, ref: effects["human_needs"].append(
            (url, ref)),
        defer_note_close_effect=lambda *args, **kwargs:
            effects["closed"].append((args, kwargs)),
    )

    assert effects["closed"] == []
    assert len(effects["blocked"]) == 1
    assert effects["needs"] == []
    assert effects["human_needs"] == [(rejected["url"], REPO + "#42")]
    assert effects["comments"][0][0][2] == "**Declined:** {}".format(
        DEFER_NOTE_REASON)
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][2] == "skipped-blocked"


def test_close_declined_defer_note_proof_uses_completed_closing_comment(
        monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        funnel, "_run_gh",
        lambda argv, **kwargs: calls.append((argv, kwargs))
        or subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    )

    implement.close_declined_defer_note_proof(
        REPO, 42, DEFER_NOTE_REASON, run="run-42", agent="codex",
        cwd=tmp_path,
    )

    argv, kwargs = calls[0]
    assert argv[:8] == [
        "gh", "issue", "close", "42", "--repo", REPO,
        "--reason", "completed",
    ]
    assert argv[8] == "--comment"
    assert argv[9].startswith("**Declined:** {}".format(DEFER_NOTE_REASON))
    assert "command-center-provenance" in argv[9]
    assert kwargs["cwd"] == str(tmp_path)


@pytest.mark.parametrize(("reason", "open_state", "edge_fails"), [
    ("Unlanded prerequisite #165 is closed.", False, False),
    ("Unlanded prerequisite #165 is still open.", True, True),
    ("I need Nate to choose whether this scope is acceptable.", None, False),
    ("Prerequisite details are missing from the ticket.", None, False),
])
def test_finish_declined_falls_back_to_blocked_for_non_prerequisite_cases(
        tmp_path, monkeypatch, reason, open_state, edge_fails):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = {"looked_up": [], "edges": [], "blocked": [], "comments": [],
               "released": [], "finished": [], "needs": [],
               "human_needs": []}

    def edge_effect(repo, number, ref, **kwargs):
        effects["edges"].append((repo, number, ref))
        if edge_fails:
            raise funnel.GitHubError("edge write failed")

    implement.finish_declined(
        reason,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda *args: pytest.fail(
            "a blocked decline without a machine condition must ask Nate"),
        human_needs_effect=lambda url, ref: effects["human_needs"].append(
            (url, ref)),
        prerequisite_open_effect=lambda ref: (
            effects["looked_up"].append(ref) or open_state),
        prerequisite_edge_effect=edge_effect,
    )

    assert len(effects["blocked"]) == 1
    ((_, number), kwargs), = effects["blocked"]
    assert number == 42 and kwargs == {"cwd": clone}
    assert effects["needs"] == []
    assert effects["human_needs"] == [(ticket()["url"], REPO + "#42")]
    assert effects["comments"][0][0][2] == "**Declined:** {}".format(reason)
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "skipped-blocked",
         "declined: {}".format(reason), REPO + "#42")
    ]
    if open_state is None:
        assert effects["looked_up"] == []
        assert effects["edges"] == []
    elif open_state is False:
        assert effects["looked_up"] == [REPO + "#165"]
        assert effects["edges"] == []
    else:
        assert effects["looked_up"] == [REPO + "#165"]
        assert effects["edges"] == [(REPO, 42, REPO + "#165")]


@pytest.mark.parametrize(("reason", "ticket_repo", "expected"), [
    ("Unlanded prerequisite #165 is still open.", REPO,
     ("prerequisite-ticket", REPO + "#165")),
    ("The reason names other/tools#9 as a prerequisite.", REPO,
     ("prerequisite-ticket", "other/tools#9")),
    ("The account setting still needs a human decision.", REPO,
     ("unknown", None)),
])
def test_classify_decline_reason_uses_only_named_prerequisite_reference(
        reason, ticket_repo, expected):
    assert implement.classify_decline_reason(reason, ticket_repo) == expected


@pytest.mark.parametrize(("reason", "expected"), [
    (
        ACCEPT_BODY_CONFLICT_REASON,
        ("accept-body-conflict", "plan.md#routine-freeze-while-794-lands"),
    ),
    (
        "The ticket Accept contradicts current repo rules; conflicting text "
        "is at plan.md#routine-freeze-while-794-lands.",
        ("accept-body-conflict", "plan.md#routine-freeze-while-794-lands"),
    ),
    (
        "The ticket Accept contradicts current repo rules, but its conflict "
        "pointer is not parseable.",
        ("unknown", None),
    ),
])
def test_classify_decline_reason_routes_only_pointed_accept_conflicts(
        reason, expected):
    assert implement.classify_decline_reason(reason, REPO) == expected


@pytest.mark.parametrize(("payload", "expected"), [
    ({"number": 165, "state": "OPEN"}, True),
    ({"number": 165, "state": "CLOSED"}, False),
    ({"number": 166, "state": "OPEN"}, False),
    (None, False),
])
def test_declined_prerequisite_verification_requires_open_matching_issue(
        monkeypatch, payload, expected):
    calls = []
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: calls.append(args) or payload,
    )

    assert implement.declined_prerequisite_is_open(REPO + "#165") is expected
    assert calls == [(
        "gh", "issue", "view", "165", "--repo", REPO,
        "--json", "number,state",
    )]


@pytest.mark.parametrize(("prerequisite", "edge_value"), [
    (REPO + "#165", "165"),
    ("other/tools#9", "https://github.com/other/tools/issues/9"),
])
def test_add_declined_prerequisite_edge_uses_native_blocked_by(
        tmp_path, monkeypatch, prerequisite, edge_value):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(funnel, "_run_gh", fake_run)
    implement.add_declined_prerequisite_edge(
        REPO, 42, prerequisite, cwd=tmp_path)

    (args, kwargs), = calls
    assert args == [
        "gh", "issue", "edit", "42", "--repo", REPO,
        "--add-blocked-by", edge_value,
    ]
    assert kwargs["cwd"] == str(tmp_path)


def test_finish_main_routes_blocked_and_declined_answers(
        tmp_path, monkeypatch, capsys):
    routed = {}
    monkeypatch.setattr(implement, "_recover_answer_error", lambda *a, **k: False)

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
        ["--answer-file", str(path), "--run", "run-42",
         "--agent", "muse", "--repo", REPO]) == 0
    assert routed["blocked"][0] == blocked()["blocked_on_human"]
    assert routed["blocked"][1]["run"] == "run-42"
    assert routed["blocked"][1]["agent"] == "muse"
    assert json.loads(capsys.readouterr().out)["human_step"]["number"] == 43

    path.write_text(json.dumps({"declined": "stale"}))
    assert implement.finish_main(
        ["--answer-file", str(path), "--run", "run-42"]) == 0
    assert routed["declined"][0] == "stale"
    assert json.loads(capsys.readouterr().out)["declined"] == "stale"

    path.write_text(json.dumps({"unknown": True}))
    assert implement.finish_main(
        ["--answer-file", str(path), "--run", "run-42"]) == 1


def test_finish_main_accepts_inline_json(monkeypatch, capsys):
    routed = {}

    def fake_done(found, **kwargs):
        routed["done"] = (found, kwargs)
        return {"number": 91}

    monkeypatch.setattr(implement, "finish_done", fake_done)
    raw = json.dumps(answer())
    assert implement.finish_main([
        "--answer", raw, "--run", "run-42", "--repo", REPO,
    ]) == 0
    assert routed["done"][0] == answer()
    assert routed["done"][1]["run"] == "run-42"
    assert json.loads(capsys.readouterr().out) == {"number": 91}


def test_finish_main_still_accepts_answer_on_stdin(monkeypatch, capsys):
    routed = {}
    monkeypatch.setattr(
        implement.sys, "stdin",
        SimpleNamespace(read=lambda: json.dumps(answer())),
    )
    monkeypatch.setattr(
        implement, "finish_done",
        lambda found, **kwargs: routed.update(answer=found) or {"number": 91},
    )

    assert implement.finish_main([
        "--answer", "-", "--run", "run-42", "--repo", REPO,
    ]) == 0
    assert routed["answer"] == answer()
    assert json.loads(capsys.readouterr().out) == {"number": 91}


@pytest.mark.parametrize(
    ("answer_text", "error"),
    ((None, "cannot read answer"), ("not json", "answer is not valid JSON")),
)
def test_unreadable_answer_keeps_dirty_work_releases_and_finishes_errored(
        tmp_path, monkeypatch, capsys, answer_text, error):
    remote, clone = make_clone(tmp_path)
    (clone / "implemented.txt").write_text("done\n")
    answer_path = tmp_path / "handoff.json"
    if answer_text is not None:
        answer_path.write_text(answer_text)
    effects = {"released": [], "finished": []}
    monkeypatch.chdir(clone)
    monkeypatch.setattr(implement, "release_claim", effects["released"].append)
    monkeypatch.setattr(
        implement, "finish_heartbeat",
        lambda *args: effects["finished"].append(args),
    )

    assert implement.finish_main([
        "--answer-file", str(answer_path), "--run", "run-42",
        "--repo", REPO,
    ]) == 1

    assert error in capsys.readouterr().err
    assert effects["released"] == [REPO + "#42"]
    (finished,) = effects["finished"]
    assert finished[:3] == ("codex", "run-42", "errored")
    assert "answer error: " + error in finished[3]
    assert "work kept on ticket/42" in finished[3]
    assert finished[4] == REPO + "#42"
    assert run_git(
        "--git-dir", str(remote), "show", "ticket/42:implemented.txt"
    ).stdout == "done\n"
    assert run_git(
        "--git-dir", str(remote), "log", "-1", "--format=%s", "ticket/42"
    ).stdout.strip() == "WIP #42: answer unreadable"


# --- Per-repo test command resolution (#871) ---


def write_workflow(root, body, name="tests.yml"):
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(body)
    return workflows / name


def test_pyproject_override_selects_the_configured_command(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n'
        '[tool.command-center]\ntest = "make test"\n'
    )
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["make", "test"]]
    assert source == "pyproject.toml [tool.command-center] test"


def test_pyproject_override_beats_the_ci_workflow(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.command-center]\ntest = "make test"\n'
    )
    write_workflow(
        tmp_path,
        "jobs:\n  t:\n    steps:\n"
        "      - name: Run pytest\n"
        "        run: python3 -m pytest -q\n",
    )
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["make", "test"]]
    assert source == "pyproject.toml [tool.command-center] test"


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ('[tool.command-center]\ntest = "make test"\n', "make test"),
        ("[tool.command-center]\ntest = 'make test'\n", "make test"),
        ('[tool.command-center]\ntest = "make test"  # trailing\n',
         "make test"),
        ('[tool.other]\ntest = "nope"\n\n'
         '[tool.command-center]\ntest = "make test"\n', "make test"),
        ('# test = "nope"\n[tool.command-center]\n'
         '# test = "nope"\ntest = "make test"\n', "make test"),
        ('[tool.command-center]\ntest = "make \\"quoted\\""\n',
         'make "quoted"'),
        ("[tool.command-center]\nother = 1\n", None),
        ("[tool.command-center]\ntest = 123\n", None),
        ('[tool.command-center]\ntest = ""\n', None),
        ('[tool.command-center-extra]\ntest = "nope"\n', None),
        ('[project]\nname = "x"\n', None),
    ),
)
def test_pyproject_reader_reads_one_string_key(tmp_path, body, expected):
    (tmp_path / "pyproject.toml").write_text(body)
    assert implement.pyproject_test_command(tmp_path) == expected


def test_pyproject_reader_returns_none_without_a_file(tmp_path):
    assert implement.pyproject_test_command(tmp_path) is None


@pytest.mark.parametrize("name", ("tests.yml", "tests.yaml"))
def test_ci_workflow_step_named_tests_selects_its_run_line(tmp_path, name):
    """The FF-Weekly-Start-Sit shape: a `make test` suite behind a step
    named `tests`, which the hardcoded pytest could never run."""
    write_workflow(
        tmp_path,
        "name: tests\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: tests\n"
        "        run: make test\n",
        name=name,
    )
    (tmp_path / "Makefile").write_text("test:\n\techo ok\n")
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["make", "test"]]
    assert source == 'CI .github/workflows/{} step "tests"'.format(name)


def test_ci_workflow_selects_the_step_that_runs_pytest(tmp_path):
    write_workflow(
        tmp_path,
        "jobs:\n"
        "  pytest:\n"
        "    steps:\n"
        "      - name: Install pytest\n"
        "        run: python3 -m pip install --quiet pytest\n"
        "      - name: Run the suite\n"
        "        run: python3 -m pytest tests/ -q\n",
    )
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["python3", "-m", "pytest", "tests/", "-q"]]
    assert source == 'CI .github/workflows/tests.yml step "Run the suite"'


@pytest.mark.parametrize(
    ("name", "run", "matched"),
    (
        ("tests", "make test", True),
        ("Run tests", "make test", True),
        ("Run the test suite", "make test", True),
        ("Testing", "make test", True),
        ("Run pytest", "make test", True),
        ("Install pytest", "python3 -m pip install pytest", False),
        ("Install test dependencies", "pip install -r req.txt", False),
        ("Set up the test database", "initdb", False),
        ("Run the suite", "python3 -m pytest -q", True),
        ("Suite", "pytest -q", True),
        ("Suite", "python -m pytest -q", True),
        ("Suite", "uv run pytest -q", True),
        ("Suite", "cd sub && python3 -m pytest -q", True),
        ("Suite", "uv pip install pytest", False),
        ("Suite", "pip install pytest", False),
        ("Publish to TestPyPI", "twine upload dist/*", False),
        ("Lint", "ruff check .", False),
        ("", "make check test", True),
        ("", "npm test", True),
        ("", "make lint", False),
        ("", "pip install -r test-requirements.txt", False),
    ),
)
def test_test_step_matching(name, run, matched):
    assert implement._is_test_step(name, run) is matched


def test_ci_workflow_selects_an_unnamed_make_test_step(tmp_path):
    """FF-Weekly-Start-Sit's suite is an unnamed ``make check test`` step;
    missing it fell back to pytest, which that repo does not use."""
    write_workflow(
        tmp_path,
        "jobs:\n"
        "  offline:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "      - run: make check test\n",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_runner.py").write_text("import unittest\n")
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["make", "check", "test"]]
    assert source == 'CI .github/workflows/tests.yml step "make check test"'


def test_ci_workflow_tolerates_env_blocks_and_uses_steps(tmp_path):
    write_workflow(
        tmp_path,
        "jobs:\n"
        "  check:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Run tests\n"
        "        env:\n"
        "          FOO: bar\n"
        "        run: make test\n",
    )
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["make", "test"]]
    assert source == 'CI .github/workflows/tests.yml step "Run tests"'


def test_ci_workflow_without_a_test_step_falls_through(tmp_path):
    write_workflow(
        tmp_path,
        "jobs:\n  build:\n    steps:\n"
        "      - name: Lint\n"
        "        run: ruff check .\n",
    )
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["npm", "test"]]
    assert source is None


def test_default_pytest_names_its_source(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_runner.py").write_text("def test_ok(): pass\n")
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    ]
    assert source == "default pytest"


def test_multiline_run_lines_run_under_sh(tmp_path):
    write_workflow(
        tmp_path,
        "jobs:\n  t:\n    steps:\n"
        "      - name: tests\n"
        "        run: |\n"
        "          make lint\n"
        "          make test\n",
    )
    commands, source = implement.default_test_plan(tmp_path)
    assert commands == [["sh", "-c", "make lint\nmake test"]]
    assert source == 'CI .github/workflows/tests.yml step "tests"'


def test_piped_run_lines_run_under_sh(tmp_path):
    write_workflow(
        tmp_path,
        "jobs:\n  t:\n    steps:\n"
        "      - name: tests\n"
        "        run: make test 2>&1 | tee test.log\n",
    )
    commands, _ = implement.default_test_plan(tmp_path)
    assert commands == [["sh", "-c", "make test 2>&1 | tee test.log"]]


def test_finish_done_records_the_test_source_in_pr_body_and_note(
        tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    passing = shlex.join([sys.executable, "-c", "pass"])
    (clone / "pyproject.toml").write_text(
        '[tool.command-center]\ntest = "{}"\n'.format(passing)
    )
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))

    effects = {"prs": [], "released": [], "finished": []}

    def open_pr(repo, context, found_ticket, body):
        effects["prs"].append(body)
        return {"number": 91, "url": "https://github.com/owner/repo/pull/91"}

    result = implement.finish_done(
        answer(),
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        pr_effect=open_pr,
    )

    assert result["number"] == 91
    (body,) = effects["prs"]
    assert "Test command source:\npyproject.toml [tool.command-center] test" in body
    assert effects["finished"] == [
        ("codex", "run-42", "done",
         "PR #91 (tests: pyproject.toml [tool.command-center] test)",
         REPO + "#42")
    ]


def test_finish_done_keeps_work_when_the_resolved_command_fails(
        tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    failing = shlex.join([sys.executable, "-c", "raise SystemExit(3)"])
    (clone / "pyproject.toml").write_text(
        '[tool.command-center]\ntest = "{}"\n'.format(failing)
    )
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
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            pr_effect=no_pr,
        )

    assert effects["released"] == [REPO + "#42"]
    (finished,) = effects["finished"]
    assert finished[:3] == ("codex", "run-42", "errored")
    assert finished[3].startswith("tests failed: ")
    assert "work kept on ticket/42" in finished[3]

    pushed = run_git(
        "--git-dir", str(remote), "show", "ticket/42:implemented.txt"
    ).stdout
    assert pushed == "done\n"


def test_dry_run_prints_the_resolved_plan_without_side_effects(
        tmp_path, monkeypatch, capsys):
    remote, clone = make_clone(tmp_path)
    (clone / "pyproject.toml").write_text(
        '[tool.command-center]\ntest = "make test"\n'
    )
    monkeypatch.chdir(clone)
    assert implement.finish_main(["--dry-run"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found == {
        "branch": "ticket/42",
        "number": 42,
        "test_commands": ["make test"],
        "test_source": "pyproject.toml [tool.command-center] test",
    }
    refs = run_git("--git-dir", str(remote), "show-ref").stdout
    assert "ticket/42" not in refs


def test_dry_run_refuses_a_non_ticket_branch(tmp_path, monkeypatch, capsys):
    _, clone = make_clone(tmp_path)
    run_git("switch", "--quiet", "main", cwd=clone)
    monkeypatch.chdir(clone)
    assert implement.finish_main(["--dry-run"]) == 1
    assert "ticket/<number>" in capsys.readouterr().err


def test_finish_main_requires_answer_and_run_without_dry_run():
    with pytest.raises(SystemExit):
        implement.finish_main([])


def test_a_rebased_ticket_branch_pushes_as_a_fast_forward_keeping_the_run_tree(tmp_path):
    """#890: a stale-PR rebase rewrote ticket/42; the push must still land."""
    remote, clone = make_clone(tmp_path)
    (clone / "old.txt").write_text("first attempt\n")
    run_git("add", "old.txt", cwd=clone)
    run_git("commit", "--quiet", "-m", "first attempt", cwd=clone)
    run_git("push", "--quiet", "-u", "origin", "ticket/42", cwd=clone)

    # main moves on, and the engineer rebases the ticket branch onto it.
    other = tmp_path / "other"
    run_git("clone", "--quiet", str(remote), str(other))
    run_git("config", "user.name", "Fixture", cwd=other)
    run_git("config", "user.email", "fixture@example.test", cwd=other)
    (other / "main.txt").write_text("main moved\n")
    run_git("add", "main.txt", cwd=other)
    run_git("commit", "--quiet", "-m", "main moved", cwd=other)
    run_git("push", "--quiet", "origin", "main", cwd=other)
    run_git("fetch", "--quiet", "origin", cwd=clone)
    run_git("rebase", "--quiet", "origin/main", cwd=clone)
    (clone / "old.txt").write_text("second attempt\n")
    run_git("commit", "--quiet", "-am", "second attempt", cwd=clone)
    local_tree = run_git("rev-parse", "HEAD^{tree}", cwd=clone).stdout.strip()

    implement._push_ticket_branch(clone, "ticket/42")

    remote_tree = run_git("--git-dir", str(remote), "rev-parse",
                          "ticket/42^{tree}").stdout.strip()
    assert remote_tree == local_tree
    shown = run_git("--git-dir", str(remote), "show", "ticket/42:old.txt").stdout
    assert shown == "second attempt\n"


def test_a_resolved_python_command_runs_under_this_interpreter(tmp_path, monkeypatch):
    """#890: CI says `python -m pytest`; the Mac has no bare `python`."""
    _, clone = make_clone(tmp_path)
    seen = []
    real_run = implement._run

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return real_run([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(implement, "_run", spy)
    implement.run_tests(clone, [["python", "-m", "pytest", "-q"]])
    assert seen[0][0] == sys.executable
    assert seen[0][1:] == ["-m", "pytest", "-q"]


def test_a_resolved_make_command_gets_this_interpreter_as_python(
        tmp_path, monkeypatch):
    """#953: FF's `PYTHON ?= python3` became Apple's sandboxed python3 under
    Codex, and compileall failed on its cache three runs in a row."""
    _, clone = make_clone(tmp_path)
    seen = []
    real_run = implement._run

    def spy(argv, **kwargs):
        seen.append((list(argv), kwargs.get("env") or {}))
        return real_run([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(implement, "_run", spy)
    implement.run_tests(clone, [["make", "check", "test"]])
    argv, env = seen[0]
    assert argv == ["make", "check", "test"]
    assert env["PYTHON"] == sys.executable
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def _spy_run(monkeypatch):
    seen = []
    real_run = implement._run

    def spy(argv, **kwargs):
        seen.append((list(argv), kwargs.get("env") or {}))
        return real_run([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(implement, "_run", spy)
    return seen


def _fake_python(directory, version):
    """An executable named python<version> that reports that version."""
    path = directory / "python{}".format(version)
    path.write_text("#!/bin/sh\necho {}\n".format(version))
    path.chmod(0o755)
    return path


def test_an_unpinned_checkout_runs_under_this_interpreter(tmp_path, monkeypatch):
    """#1024: with no .python-version, #953's choice stands."""
    _, clone = make_clone(tmp_path)
    seen = _spy_run(monkeypatch)
    implement.run_tests(clone, [["make", "test"], ["python3", "-m", "pytest"]])
    assert seen[0][1]["PYTHON"] == sys.executable
    assert seen[1][0][0] == sys.executable


def test_a_pin_this_interpreter_meets_keeps_it(tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    (clone / ".python-version").write_text(
        "{}.{}.9\n".format(*sys.version_info[:2]))
    monkeypatch.setattr(implement, "PINNED_PYTHON_DIRS", ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    seen = _spy_run(monkeypatch)
    implement.run_tests(clone, [["make", "test"]])
    assert seen[0][1]["PYTHON"] == sys.executable


def test_a_pin_this_interpreter_misses_picks_a_matching_one(
        tmp_path, monkeypatch):
    """The-League pins 3.12; Codex started finish-ticket under Apple's 3.9."""
    _, clone = make_clone(tmp_path)
    (clone / ".python-version").write_text("7.4\n")
    on_path = tmp_path / "bin"
    on_path.mkdir()
    fake = _fake_python(on_path, "7.4")
    monkeypatch.setattr(implement, "PINNED_PYTHON_DIRS", ())
    monkeypatch.setenv("PATH", str(on_path))
    seen = _spy_run(monkeypatch)
    implement.run_tests(clone, [["make", "test"], ["python", "-m", "pytest"]])
    assert seen[0][1]["PYTHON"] == str(fake)
    assert seen[1][0] == [str(fake), "-m", "pytest"]


def test_a_pinned_interpreter_off_path_is_found_in_homebrew_dirs(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    (clone / ".python-version").write_text("7.4.1\n")
    brew = tmp_path / "brew"
    brew.mkdir()
    fake = _fake_python(brew, "7.4")
    monkeypatch.setattr(implement, "PINNED_PYTHON_DIRS", (str(brew),))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert implement.pinned_interpreter(clone) == str(fake)


def test_an_unsatisfiable_pin_falls_back_to_this_interpreter(
        tmp_path, monkeypatch):
    """No match: keep sys.executable so the repo's own check names it."""
    _, clone = make_clone(tmp_path)
    (clone / ".python-version").write_text("7.4\n")
    liar = tmp_path / "bin"
    liar.mkdir()
    # Named python7.4 but reports another version: not a match.
    path = liar / "python7.4"
    path.write_text("#!/bin/sh\necho 7.5\n")
    path.chmod(0o755)
    monkeypatch.setattr(implement, "PINNED_PYTHON_DIRS", ())
    monkeypatch.setenv("PATH", str(liar))
    seen = _spy_run(monkeypatch)
    implement.run_tests(clone, [["make", "test"]])
    assert seen[0][1]["PYTHON"] == sys.executable


def test_a_failed_command_reports_stdout_as_well_as_stderr(tmp_path):
    """compileall prints on stdout; make's stderr said only "Error 1"."""
    # Joined at run time, so the error's echo of the command cannot match.
    script = ("import sys; print('*** Permission' + 'Error: cache'); "
              "sys.stderr.write('make: *** [check] ' + 'Error 1'); sys.exit(2)")
    with pytest.raises(implement.ImplementError) as caught:
        implement._run([sys.executable, "-c", script], cwd=tmp_path)
    assert "PermissionError: cache" in str(caught.value)
    assert "[check] Error 1" in str(caught.value)


@pytest.mark.parametrize("url", [
    "https://github.com/nateprich-projects/The-League.git",
    "https://github.com/nateprich-projects/The-League",
    "git@github.com:nateprich-projects/The-League.git",
    "ssh://git@github.com/nateprich-projects/The-League.git",
])
def test_checkout_repo_resolves_from_origin_without_gh(tmp_path, monkeypatch, url):
    """A GitHub 504 on `gh repo view` stranded a finished ticket (#1030)."""
    run_git("init", "-q", str(tmp_path))
    run_git("remote", "add", "origin", url, cwd=tmp_path)

    def gh_down(*args):
        raise AssertionError("origin names the repo; gh must not be asked")

    monkeypatch.setattr(funnel, "_gh_json", gh_down)
    assert implement.resolve_checkout_repo(tmp_path, None) == (
        "nateprich-projects/The-League"
    )


def test_checkout_repo_falls_back_to_gh_for_a_non_github_origin(
        tmp_path, monkeypatch):
    run_git("init", "-q", str(tmp_path))
    run_git("remote", "add", "origin", str(tmp_path / "origin.git"),
            cwd=tmp_path)
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: {"nameWithOwner": REPO})
    assert implement.resolve_checkout_repo(tmp_path, None) == REPO
