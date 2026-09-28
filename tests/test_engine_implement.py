"""Implementation packet and runner effects (#815, #816, Phase 3 of #794)."""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import shlex
import signal
import stat
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
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
LIVE_1453_UNSATISFIABLE_ACCEPTANCE = (
    "The ticket's acceptance requires a heartbeat finish for agent fantasy-gm "
    "and job com.nateprich.ff-weekly-start-sit.daily, but fantasy-gm is not in "
    "heartbeat.PROVIDERS and the live FF#230 comment says that daily does not "
    "publish Command Center heartbeats. The live comment already has the "
    "requested canonical event form, so no in-scope change can make the "
    "specified event clear. Parent plan #1403 needs a supported event source "
    "or revised acceptance before this ticket can proceed. I left the existing "
    "ticket/1453 branch contents untouched."
)
LIVE_1497_PENDING_GATE_ANSWER = (
    "Unlanded prerequisite: Nate's “Is the plan good?” gate answer for Command "
    "Center issue 1195 (https://github.com/nateprich-projects/command-center/"
    "issues/1195) is pending. funnel.py show 1195 reports Shaped at that gate, "
    "so the escalated shape lane cannot yet produce the required post-fix "
    "re-shape. No source change was made."
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


def make_clone(tmp_path, clone_path=None):
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = (
        pathlib.Path(clone_path)
        if clone_path is not None else tmp_path / "clone"
    )
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
    if clone_path is not None:
        clone.parent.mkdir(parents=True, exist_ok=True)
        clone.mkdir(mode=0o700)
    run_git("clone", "--quiet", str(remote), str(clone))
    run_git("config", "user.name", "Fixture", cwd=clone)
    run_git("config", "user.email", "fixture@example.test", cwd=clone)
    run_git("switch", "--quiet", "-c", "ticket/42", cwd=clone)
    return remote, clone


def make_codex_run_clone(tmp_path, monkeypatch, number=42):
    runtime_root = tmp_path / "runtime"
    runs_root = runtime_root / "codex-runs"
    runs_root.mkdir(parents=True, mode=0o700)
    checkout = runs_root / (
        "ticket-{}-20260927T163000123456Z".format(number))
    monkeypatch.setattr(funnel, "CLAUDE_DIR", str(runtime_root))
    remote, clone = make_clone(tmp_path, clone_path=checkout)
    clone.chmod(0o700)
    return remote, clone


def _stub_claim_state(monkeypatch, state):
    monkeypatch.setattr(
        implement, "_claim_state",
        lambda ref, run, agent: (state, []),
    )


def test_claim_state_uses_the_latest_binding_after_the_claim(monkeypatch):
    ref = REPO + "#42"
    claim_time = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
    item = SimpleNamespace(
        ref=ref, item_id="PVTI_42", in_motion_since=claim_time,
    )
    monkeypatch.setattr(funnel, "load_project_items_by_refs",
                        lambda refs: [item])
    records = {
        "codex": [{
            "run": "old-run", "phase": "bind",
            "ts": int(claim_time.timestamp()) - 1,
            "do": "ticket", "work": ref,
        }],
        "claude": [{
            "run": "successor", "phase": "bind",
            "ts": int(claim_time.timestamp()) + 1,
            "do": "ticket", "work": ref,
        }],
    }
    monkeypatch.setattr(
        heartbeat, "read_github_strict", lambda agent: records.get(agent, []),
    )

    assert implement._claim_state(ref, "old-run", "codex")[0] == "other"
    assert implement._claim_state(ref, "successor", "claude")[0] == "owned"


def test_claim_state_fails_closed_without_a_binding_after_the_claim(monkeypatch):
    ref = REPO + "#42"
    claim_time = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
    item = SimpleNamespace(
        ref=ref, item_id="PVTI_42", in_motion_since=claim_time,
    )
    monkeypatch.setattr(funnel, "load_project_items_by_refs",
                        lambda refs: [item])
    monkeypatch.setattr(heartbeat, "read_github_strict", lambda agent: [])

    assert implement._claim_state(ref, "run-42", "codex")[0] == "unknown"


def test_claim_state_refuses_when_heartbeat_bindings_are_unreadable(monkeypatch):
    ref = REPO + "#42"
    claim_time = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)
    item = SimpleNamespace(
        ref=ref, item_id="PVTI_42", in_motion_since=claim_time,
    )
    monkeypatch.setattr(funnel, "load_project_items_by_refs",
                        lambda refs: [item])

    def unreadable(_agent):
        raise OSError("heartbeat is unavailable")

    monkeypatch.setattr(heartbeat, "read_github_strict", unreadable)

    with pytest.raises(implement.SupersededRunError,
                       match="heartbeat bindings could not be read"):
        implement._claim_state(ref, "run-42", "codex")


def test_release_claim_noops_when_the_claim_is_empty(monkeypatch):
    ref = REPO + "#42"
    _stub_claim_state(monkeypatch, "empty")
    monkeypatch.setattr(
        funnel, "cmd_release",
        lambda *args, **kwargs: pytest.fail("empty claim must not be written"),
    )

    implement.release_claim(ref, run="run-42")


@pytest.mark.parametrize("state", ("other", "unknown"))
def test_release_claim_refuses_other_or_unknown_holder(monkeypatch, state):
    ref = REPO + "#42"
    _stub_claim_state(monkeypatch, state)
    monkeypatch.setattr(
        funnel, "cmd_release",
        lambda *args, **kwargs: pytest.fail("refused claim must not be written"),
    )

    with pytest.raises(implement.SupersededRunError):
        implement.release_claim(ref, run="run-42")


@pytest.mark.parametrize("state", ("other", "unknown"))
def test_push_ticket_branch_refuses_other_or_unknown_holder(
        tmp_path, monkeypatch, state):
    remote, clone = make_clone(tmp_path)
    (clone / "change.txt").write_text("not pushed\n")
    _stub_claim_state(monkeypatch, state)

    with pytest.raises(implement.SupersededRunError):
        implement._push_ticket_branch(
            clone, "ticket/42", ref=REPO + "#42", run="run-42",
            agent="codex",
        )

    assert run_git("ls-remote", "--heads", "origin",
                   "refs/heads/ticket/42", cwd=clone).stdout == ""


@pytest.mark.parametrize("state", ("other", "unknown"))
def test_finish_refusal_records_superseded_without_keeping_work(
        tmp_path, monkeypatch, capsys, state):
    remote, clone = make_codex_run_clone(tmp_path, monkeypatch)
    (clone / "implemented.txt").write_text("unkept work\n")
    answer_path = tmp_path / "answer.json"
    answer_path.write_text(json.dumps(answer()))
    _stub_claim_state(monkeypatch, state)
    effects = {"released": [], "finished": []}
    monkeypatch.chdir(clone)
    monkeypatch.setattr(
        implement, "release_claim",
        lambda ref, **kwargs: effects["released"].append(ref),
    )
    monkeypatch.setattr(
        implement, "finish_heartbeat",
        lambda *args: effects["finished"].append(args),
    )
    monkeypatch.setattr(
        implement, "run_tests",
        lambda *args, **kwargs: pytest.fail("superseded work must not run tests"),
    )

    assert implement.finish_main([
        "--answer-file", str(answer_path), "--run", "run-42", "--repo", REPO,
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "ticket": REPO + "#42", "superseded": True, "work_kept": False,
    }
    assert effects["released"] == []
    (finished,) = effects["finished"]
    assert finished[:3] == ("codex", "run-42", "errored")
    assert "superseded" in finished[3]
    assert "work not kept" in finished[3]
    assert run_git(
        "--git-dir", str(remote), "for-each-ref", "--format=%(refname)",
        "refs/heads/ticket/42",
    ).stdout == ""
    assert not clone.exists()


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
    _stub_claim_state(monkeypatch, "empty")
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
    _stub_claim_state(monkeypatch, "empty")
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: ticket(number))
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
    _stub_claim_state(monkeypatch, "empty")
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


def test_finish_ticket_checkpoints_before_tests_and_refuses_later_superseded_push(
        tmp_path, monkeypatch, capsys):
    remote, clone = make_codex_run_clone(tmp_path, monkeypatch)
    (clone / "implemented.txt").write_text("checkpointed work\n")
    answer_path = tmp_path / "answer.json"
    answer_path.write_text(json.dumps(answer()))
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: ticket(number),
    )

    states = iter(("owned", "owned", "owned", "owned", "other"))
    monkeypatch.setattr(
        implement, "_claim_state",
        lambda ref, run, agent: (next(states), []),
    )
    effects = {"released": [], "finished": []}
    monkeypatch.setattr(
        implement, "release_claim",
        lambda ref, **kwargs: effects["released"].append(ref),
    )
    monkeypatch.setattr(
        implement, "finish_heartbeat",
        lambda *args: effects["finished"].append(args),
    )
    checkpoint = {}

    def inspect_checkpoint(root, commands):
        checkpoint["content"] = run_git(
            "--git-dir", str(remote), "show",
            "refs/heads/ticket/42:implemented.txt",
        ).stdout
        checkpoint["head"] = run_git(
            "--git-dir", str(remote), "rev-parse", "refs/heads/ticket/42",
        ).stdout.strip()
        return ["python3 -m pytest -q"], None

    monkeypatch.setattr(implement, "run_tests", inspect_checkpoint)
    monkeypatch.chdir(clone)

    assert implement.finish_main([
        "--answer-file", str(answer_path), "--run", "run-42",
        "--repo", REPO,
    ]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["superseded"] is True
    assert checkpoint["content"] == "checkpointed work\n"
    assert run_git(
        "--git-dir", str(remote), "rev-parse", "refs/heads/ticket/42",
    ).stdout.strip() == checkpoint["head"]
    assert effects["released"] == []
    (finished,) = effects["finished"]
    assert finished[:3] == ("codex", "run-42", "errored")
    assert "superseded" in finished[3]
    assert "work not kept" in finished[3]


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
    _stub_claim_state(monkeypatch, "owned")
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
    assert subject == "WIP #42: checkpoint implementation"


def test_finish_ticket_removes_owner_only_codex_run_checkout_after_push(
        tmp_path, monkeypatch):
    remote, clone = make_codex_run_clone(tmp_path, monkeypatch)
    _stub_claim_state(monkeypatch, "owned")
    sibling = clone.parent / "ticket-42-20260927T163001123456Z"
    sibling.mkdir(mode=0o700)
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: ticket(number))
    monkeypatch.chdir(clone)
    effects = {"released": [], "finished": []}

    def open_pr(repo, context, found_ticket, body):
        # The pushed branch is durable now, but gh still needs the local repo
        # as its working directory to create or update the PR.
        assert clone.is_dir()
        assert context["root"] == clone
        assert run_git("--git-dir", str(remote), "show-ref").stdout.find(
            "refs/heads/ticket/42") >= 0
        return {
            "number": 99,
            "url": "https://github.com/{}/pull/99".format(REPO),
        }

    def finish(*args):
        assert clone.is_dir()
        effects["finished"].append(args)

    result = implement.finish_done(
        answer(),
        run="run-42",
        repo=REPO,
        cwd=clone,
        test_commands=[[sys.executable, "-c", "pass"]],
        release=effects["released"].append,
        heartbeat_finish=finish,
        pr_effect=open_pr,
    )

    assert result["number"] == 99
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][:3] == ("codex", "run-42", "done")
    assert not clone.exists()
    assert sibling.is_dir()
    assert pathlib.Path.cwd() == clone.parent


def test_finish_ticket_removes_codex_run_checkout_after_recording_not_kept(
        tmp_path, monkeypatch):
    _, clone = make_codex_run_clone(tmp_path, monkeypatch)
    (clone / "unfinished.txt").write_text("not kept\n")
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = {"released": [], "finished": []}

    def finish(*args):
        assert clone.is_dir()
        assert (clone / "unfinished.txt").exists()
        effects["finished"].append(args)

    implement.finish_blocked_on_human(
        blocked()["blocked_on_human"],
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=finish,
        create_effect=lambda *args, **kwargs: {
            "number": 43,
            "ref": REPO + "#43",
            "url": "https://github.com/{}/issues/43".format(REPO),
        },
        needs_effect=lambda *args: None,
        block_effect=lambda *args, **kwargs: None,
        comment_effect=lambda *args, **kwargs: None,
        sub_issues_effect=lambda *args: [],
    )

    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][2] == "skipped-human-step"
    assert not clone.exists()


def test_finish_ticket_keeps_codex_run_checkout_when_push_fails(
        tmp_path, monkeypatch):
    _, clone = make_codex_run_clone(tmp_path, monkeypatch)
    _stub_claim_state(monkeypatch, "owned")
    (clone / "implemented.txt").write_text("done\n")
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: ticket(number))
    pushes = {"count": 0}

    def fail_the_post_test_push(*args, **kwargs):
        pushes["count"] += 1
        if pushes["count"] > 1:
            raise implement.ImplementError(
                "git push failed: remote unavailable")

    monkeypatch.setattr(implement, "_push_ticket_branch", fail_the_post_test_push)
    effects = {"released": [], "finished": []}

    with pytest.raises(implement.ImplementError, match="SystemExit"):
        implement.finish_done(
            answer(),
            run="run-42",
            repo=REPO,
            cwd=clone,
            test_commands=[[sys.executable, "-c", "raise SystemExit(3)"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            pr_effect=lambda *args: pytest.fail("failed tests must not open a PR"),
        )

    assert effects["released"] == [REPO + "#42"]
    assert "work NOT kept: git push failed" in effects["finished"][0][3]
    assert clone.is_dir()
    assert (clone / "implemented.txt").exists()


def test_codex_run_cleanup_leaves_checkout_outside_runtime_root(
        tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    (runtime_root / "codex-runs").mkdir(parents=True)
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(funnel, "CLAUDE_DIR", str(runtime_root))

    assert not implement._remove_codex_run_checkout(clone, 42, "codex")
    assert clone.is_dir()


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
               "finished": [], "needs": [], "siblings": []}

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
        sub_issues_effect=lambda repo, number: effects["siblings"].append(
            (repo, number)) or [],
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
    # The parent's steps were read before filing (#1726).
    assert effects["siblings"] == [(REPO, 7)]

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
            sub_issues_effect=lambda repo, number: [],
        )


# A human step Nate closed as not planned is never filed again for the same
# ticket (#1726). Synthetic fixtures: steps are sub-issues of parent #7, the
# ticket is #42, and the head is the pushed tip of ticket/42.
CLOSED_STEP_HEAD = "a" * 40


def human_step_row(number, *, ticket_number=42, state="CLOSED",
                   state_reason="NOT_PLANNED", repo=REPO):
    return {
        "ref": "{}#{}".format(repo, number),
        "repo": repo,
        "number": number,
        "state": state,
        "state_reason": state_reason,
        "body": implement.render_human_step_body(
            parent_number=7, ticket_number=ticket_number,
            reason="an account or billing setting",
            action="Turn on the dashboard toggle"),
    }


def run_blocked_with_siblings(clone, rows, effects, *, comments=(),
                              head=CLOSED_STEP_HEAD, run="run-42",
                              comment_effect=None):
    """Run the blocked finish with every read stubbed and every effect kept."""
    def keep_comment(repo, number, body, **kwargs):
        effects["comments"].append(body)

    return implement.finish_blocked_on_human(
        blocked("an account or billing setting",
                "Turn on the dashboard toggle again")["blocked_on_human"],
        run=run,
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        create_effect=lambda *args, **kwargs: effects["created"].append(
            args) or {"number": 43, "ref": REPO + "#43",
                      "url": "https://github.com/{}/issues/43".format(REPO)},
        block_effect=lambda *args, **kwargs: effects["blocked"].append(
            (args, kwargs)),
        comment_effect=comment_effect or keep_comment,
        needs_effect=lambda url, ref: effects["step_needs"].append(ref),
        sub_issues_effect=lambda repo, number: list(rows),
        comments_effect=lambda repo, number: effects["comment_reads"].append(
            number) or list(comments),
        head_effect=lambda root, branch: effects["head_reads"].append(
            branch) or head,
        route_needs_effect=lambda url, ref: effects["agent_needs"].append(ref),
        human_needs_effect=lambda url, ref: effects["human_needs"].append(ref),
    )


def closed_step_effects():
    return {"created": [], "blocked": [], "comments": [], "released": [],
            "finished": [], "step_needs": [], "comment_reads": [],
            "head_reads": [], "agent_needs": [], "human_needs": []}


def route_record(comment):
    assert implement.DECLINE_REVIEW_ROUTING_MARKER in comment
    return json.loads(comment.split("```json\n", 1)[1].split("\n```", 1)[0])


def as_posted(body, run):
    """What post_agent_comment actually leaves on the ticket."""
    return funnel.append_provenance(body, "agent", run=run, agent="codex")


def test_a_closed_not_planned_step_for_the_ticket_routes_without_filing(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = closed_step_effects()
    # An older closed step and the newest one: the newest is cited.
    rows = [human_step_row(50), human_step_row(57), human_step_row(44,
            state="OPEN", ticket_number=41)]

    result = run_blocked_with_siblings(clone, rows, effects)

    assert result == {"ticket": REPO + "#42",
                      "closed_human_step": REPO + "#57",
                      "routed": "review"}
    # Nothing filed, nothing blocked, no Needs for Nate.
    assert effects["created"] == []
    assert effects["step_needs"] == []
    assert effects["blocked"] == []
    assert effects["human_needs"] == []
    # Needs stays with the agents, and one comment cites the closed step.
    assert effects["agent_needs"] == [REPO + "#42"]
    (comment,) = effects["comments"]
    assert comment.startswith("**Review routing: Closed human step**\n\n")
    assert REPO + "#57" in comment.split(
        implement.DECLINE_REVIEW_ROUTING_MARKER, 1)[0]
    assert route_record(comment) == {
        "type": "closed-human-step",
        "human_step": REPO + "#57",
        "head_sha": CLOSED_STEP_HEAD,
        "requested_reason": "an account or billing setting",
        "requested_action": "Turn on the dashboard toggle again",
    }
    assert effects["head_reads"] == ["ticket/42"]
    assert effects["comment_reads"] == [42]
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"] == [
        ("codex", "run-42", "skipped-blocked",
         "human step not filed: {}#57 was closed as not planned; routed to "
         "review and shaping".format(REPO),
         REPO + "#42")
    ]


def test_a_second_run_at_an_unchanged_head_hands_the_ticket_to_nate(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    rows = [human_step_row(57)]
    first = closed_step_effects()
    run_blocked_with_siblings(clone, rows, first, run="run-1")
    posted = [as_posted(body, "run-1") for body in first["comments"]]

    second = closed_step_effects()
    result = run_blocked_with_siblings(
        clone, rows, second, comments=posted, run="run-2")

    assert result == {"ticket": REPO + "#42",
                      "closed_human_step": REPO + "#57",
                      "routed": "human"}
    # Neither files nor re-routes.
    assert second["created"] == []
    assert second["step_needs"] == []
    assert second["agent_needs"] == []
    assert second["human_needs"] == [REPO + "#42"]
    (comment,) = second["comments"]
    assert implement.DECLINE_REVIEW_ROUTING_MARKER not in comment
    assert comment.startswith(
        "**Needs Nate: closed human step {}#57**".format(REPO))
    assert CLOSED_STEP_HEAD[:12] in comment
    assert second["released"] == [REPO + "#42"]
    assert second["finished"] == [
        ("codex", "run-2", "skipped-blocked",
         "human step not filed: {}#57 was closed as not planned; already "
         "routed at this head; Needs human".format(REPO),
         REPO + "#42")
    ]


@pytest.mark.parametrize("change", ["new head", "other step", "not agent"])
def test_a_route_for_another_head_or_step_routes_again(
        tmp_path, monkeypatch, change):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    earlier = implement.render_closed_step_route(
        REPO + ("#50" if change == "other step" else "#57"),
        CLOSED_STEP_HEAD, blocked()["blocked_on_human"])
    # A relayed comment quoting the record is not the runner's route.
    posted = (funnel.append_provenance(earlier, "nate-relayed")
              if change == "not agent" else as_posted(earlier, "run-1"))
    effects = closed_step_effects()

    result = run_blocked_with_siblings(
        clone, [human_step_row(57)], effects, comments=[posted],
        head="b" * 40 if change == "new head" else CLOSED_STEP_HEAD)

    assert result["routed"] == "review"
    assert effects["agent_needs"] == [REPO + "#42"]
    assert effects["human_needs"] == []
    assert effects["created"] == []


def test_a_second_run_with_no_pushed_branch_also_hands_to_nate(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    rows = [human_step_row(57)]
    first = closed_step_effects()
    run_blocked_with_siblings(clone, rows, first, head=None, run="run-1")
    assert route_record(first["comments"][0])["head_sha"] is None

    second = closed_step_effects()
    result = run_blocked_with_siblings(
        clone, rows, second, head=None, run="run-2",
        comments=[as_posted(body, "run-1") for body in first["comments"]])

    assert result["routed"] == "human"
    assert second["human_needs"] == [REPO + "#42"]
    assert "no pushed branch" in second["comments"][0]


@pytest.mark.parametrize("row", [
    human_step_row(57, ticket_number=41),
    human_step_row(57, ticket_number=420),
    human_step_row(57, ticket_number=4),
    human_step_row(57, state="OPEN", state_reason=""),
    human_step_row(57, state_reason="COMPLETED"),
    human_step_row(57, repo="other/tools"),
], ids=["other ticket", "longer number", "shorter number", "open",
        "completed", "other repo"])
def test_a_step_for_another_ticket_or_open_or_completed_changes_nothing(
        tmp_path, monkeypatch, row):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = closed_step_effects()

    result = run_blocked_with_siblings(clone, [row], effects)

    assert result["human_step"]["ref"] == REPO + "#43"
    assert len(effects["created"]) == 1
    assert effects["step_needs"] == [REPO + "#43"]
    ((_, number), kwargs), = effects["blocked"]
    assert number == 42 and kwargs["blocked_by"] == 43
    assert effects["comments"] == [
        "**Blocked on #43:** Complete the human step before resuming "
        "this ticket."]
    assert effects["finished"][0][2] == "skipped-human-step"
    # The closed-step path's reads and routes never ran.
    assert effects["comment_reads"] == []
    assert effects["head_reads"] == []
    assert effects["agent_needs"] == []
    assert effects["human_needs"] == []


def test_a_failed_closed_step_route_falls_back_to_nate_and_blocked(
        tmp_path, monkeypatch):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = closed_step_effects()

    def fail_comment(*args, **kwargs):
        raise funnel.GitHubError("could not post the review handoff")

    result = run_blocked_with_siblings(
        clone, [human_step_row(57)], effects, comment_effect=fail_comment)

    assert result["routed"] == "blocked"
    assert effects["created"] == []
    assert effects["human_needs"] == [REPO + "#42"]
    ((_, number), kwargs), = effects["blocked"]
    assert number == 42 and kwargs.get("blocked_by") is None
    assert effects["finished"][0][2] == "skipped-blocked"
    assert effects["finished"][0][3].endswith(
        "review routing failed; ticket left blocked")


def test_read_parent_sub_issues_pages_every_step_with_its_close_reason(
        monkeypatch):
    pages = [
        {"totalCount": 2,
         "nodes": [{"number": 50, "state": "CLOSED",
                    "stateReason": "NOT_PLANNED", "body": "first",
                    "repository": {"nameWithOwner": REPO}}],
         "pageInfo": {"hasNextPage": True, "endCursor": "c1"}},
        {"totalCount": 2,
         "nodes": [{"number": 9, "state": "OPEN", "stateReason": None,
                    "body": None,
                    "repository": {"nameWithOwner": "other/tools"}}],
         "pageInfo": {"hasNextPage": False, "endCursor": "c2"}},
    ]
    calls = []

    def graphql(query, **variables):
        calls.append(variables)
        return {"repository": {"issue": {"subIssues": pages[len(calls) - 1]}}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)

    rows = implement.read_parent_sub_issues(REPO, 7)

    assert rows == [
        {"ref": REPO + "#50", "repo": REPO, "number": 50, "state": "CLOSED",
         "state_reason": "NOT_PLANNED", "body": "first"},
        {"ref": "other/tools#9", "repo": "other/tools", "number": 9,
         "state": "OPEN", "state_reason": "", "body": ""},
    ]
    assert calls == [
        {"owner": "owner", "name": "repo", "number": 7},
        {"owner": "owner", "name": "repo", "number": 7, "after": "c1"},
    ]


def test_read_parent_sub_issues_fails_closed_on_a_short_read(monkeypatch):
    monkeypatch.setattr(funnel, "gh_graphql", lambda query, **variables: {
        "repository": {"issue": {"subIssues": {
            "totalCount": 3, "nodes": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})

    with pytest.raises(funnel.GitHubError, match="read 0 of 3"):
        implement.read_parent_sub_issues(REPO, 7)


def test_remote_ticket_head_reads_the_pushed_tip_or_none(tmp_path):
    _, clone = make_clone(tmp_path)
    assert implement.remote_ticket_head(clone, "ticket/42") is None

    run_git("push", "--quiet", "origin", "ticket/42", cwd=clone)
    tip = run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    assert implement.remote_ticket_head(clone, "ticket/42") == tip

    # A local commit that was never pushed does not move the head.
    (clone / "local.txt").write_text("unpushed\n")
    run_git("add", "local.txt", cwd=clone)
    run_git("commit", "--quiet", "-m", "local", cwd=clone)
    assert implement.remote_ticket_head(clone, "ticket/42") == tip


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
        prerequisite_facts_effect=lambda ref: (
            effects["looked_up"].append(ref) or {
                "number": int(ref.rsplit("#", 1)[1]),
                "state": "OPEN",
                "children_total": 0,
                "children_completed": 0,
            }),
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


def test_finish_declined_requeues_false_ff_225_claim_with_evidence(
        tmp_path, monkeypatch, ff_225_landed_prerequisite_facts):
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    summaries = []

    def read_ticket_completion(query, *, owner, name, number):
        summaries.append((query, owner, name, number))
        return {
            "repository": {
                "issue": {
                    "number": number,
                    "state": "CLOSED",
                    "subIssuesSummary": {
                        "total": ff_225_landed_prerequisite_facts[
                            "children_total"],
                        "completed": ff_225_landed_prerequisite_facts[
                            "children_completed"],
                    },
                },
            },
        }

    monkeypatch.setattr(funnel, "gh_graphql", read_ticket_completion)
    reason = (
        "Named prerequisite nateprich-projects/Fantasy-GM#225 is unlanded: "
        "it self-closed after plan drift and a rejected review verdict, "
        "with no linked merge PR."
    )
    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "needs": [], "human_needs": [], "cleared": []}

    result = implement.finish_declined(
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
        needs_effect=lambda url, ref: effects["needs"].append((url, ref)),
        human_needs_effect=lambda url, ref: effects["human_needs"].append(
            (url, ref)),
        clear_block_effect=lambda repo, number, **kwargs:
            effects["cleared"].append((repo, number, kwargs)),
        prerequisite_edge_effect=lambda *args, **kwargs: pytest.fail(
            "a completed project must not become a blocked-by edge"),
    )

    assert result == {"ticket": REPO + "#42", "declined": reason}
    assert effects["blocked"] == []
    assert effects["human_needs"] == []
    assert len(summaries) == 1
    query, owner, name, number = summaries[0]
    assert (owner, name, number) == ("nateprich-projects", "Fantasy-GM", 225)
    assert "subIssuesSummary { total completed }" in query
    assert ff_225_landed_prerequisite_facts["parent_merge_pr"] is None
    assert ff_225_landed_prerequisite_facts["drift"]
    assert effects["cleared"] == [(REPO, 42, {"cwd": clone})]
    assert effects["needs"] == [(ticket()["url"], REPO + "#42")]
    posted = effects["comments"][0][0][2]
    assert posted.startswith("**Declined:** {}".format(reason))
    assert "Fantasy-GM#225" in posted
    assert "**False unlanded-prerequisite check:**" in posted
    assert "all 5 child tickets completed (5/5)" in posted
    assert effects["released"] == [REPO + "#42"]
    assert (
        "false unlanded-prerequisite claim disproved by closed child tickets; "
        "returned to agent queue"
    ) in (
        effects["finished"][0][3])


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
        prerequisite_facts_effect=lambda ref: pytest.fail(
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
        prerequisite_facts_effect=lambda ref: (
            effects["looked_up"].append(ref) or {
                "number": 165,
                "state": "OPEN" if open_state else "CLOSED",
                "children_total": 0,
                "children_completed": 0,
            }),
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


@pytest.mark.parametrize(("reason", "route"), [
    (LIVE_1453_UNSATISFIABLE_ACCEPTANCE, "agent"),
    (LIVE_1497_PENDING_GATE_ANSWER, "external-event"),
])
def test_finish_declined_routes_no_clearable_condition_shapes_without_blocking(
        tmp_path, monkeypatch, reason, route):
    """The live #1453/#1497 declines must not manufacture an Unblock gate."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "agent": [], "external": [], "cleared": []}

    result = implement.finish_declined(
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
        needs_effect=lambda url, ref: effects["agent"].append((url, ref)),
        human_needs_effect=lambda *args: pytest.fail(
            "a no-clearable-condition decline must not reach Nate"),
        external_event_needs_effect=lambda url, ref: effects["external"].append(
            (url, ref)),
        prerequisite_facts_effect=lambda ref: pytest.fail(
            "these decline shapes are not named prerequisite tickets"),
        clear_block_effect=lambda repo, number, **kwargs:
            effects["cleared"].append((repo, number, kwargs)),
    )

    assert result == {"ticket": REPO + "#42", "declined": reason}
    assert effects["blocked"] == []
    assert effects["cleared"] == [(REPO, 42, {"cwd": clone})]
    assert effects["agent"] == (
        [(ticket()["url"], REPO + "#42")] if route == "agent" else [])
    assert effects["external"] == (
        [(ticket()["url"], REPO + "#42")]
        if route == "external-event" else [])
    assert effects["released"] == [REPO + "#42"]
    assert effects["comments"][0][0][2] == "**Declined:** {}".format(reason)
    assert effects["finished"][0][:3] == (
        "codex", "run-42", "skipped-blocked")
    route_note = (
        "routed for reshaping" if route == "agent"
        else "waiting for the named gate answer"
    )
    assert route_note in effects["finished"][0][3]
    route_record = funnel._marked_json(
        effects["comments"][1][0][2],
        implement.DECLINE_REVIEW_ROUTING_MARKER,
    )
    if route == "agent":
        assert route_record["type"] == "unsatisfiable-acceptance"
        assert route_record["acceptance_digest"] == hashlib.sha256(
            ticket()["body"].encode("utf-8")
        ).hexdigest()
    else:
        assert route_record["type"] == "pending-gate-answer"
        assert route_record["gate_ref"] == "nateprich-projects/command-center#1195"


def test_unsatisfiable_decline_is_withheld_until_acceptance_changes():
    parent = funnel.Item(
        repo=REPO, number=7, title="the settled plan", url="https://example/7",
        state="OPEN", status="Ready", klass="Improve",
    )
    body = "Accept: a condition no agent can satisfy"
    item = funnel.Item(
        repo=REPO, number=42, title="implementation", url="https://example/42",
        state="OPEN", body=body, needs="agent", parent=parent.ref,
        decline_route={
            "type": "unsatisfiable-acceptance",
            "acceptance_digest": hashlib.sha256(
                body.encode("utf-8")
            ).hexdigest(),
        },
    )

    assert funnel.startable([parent, item]) == []

    item.body = body + "\n\nAccept revised after shaping."
    assert [row.ref for row in funnel.startable([parent, item])] == [item.ref]


def test_pending_gate_decline_waits_then_clears_needs(monkeypatch):
    parent = funnel.Item(
        repo=REPO, number=7, title="the settled plan", url="https://example/7",
        state="OPEN", status="Ready", klass="Improve",
    )
    gate = funnel.Item(
        repo="nateprich-projects/command-center", number=1195,
        title="answer the gate", url="https://example/1195",
        state="OPEN", body="Gates: is the plan good?",
    )
    item = funnel.Item(
        repo=REPO, number=42, title="implementation", url="https://example/42",
        state="OPEN", body="Accept: wait for the gate", needs="external-event",
        parent=parent.ref, item_id="project-item-42",
        decline_route={
            "type": "pending-gate-answer",
            "gate_ref": gate.ref,
        },
    )
    rows = [parent, gate, item]
    writes = []
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref:
            writes.append((item_id, field, value, ref)),
    )

    assert funnel.startable(rows) == []
    gate.body = "Gates: is the plan good?\n\n" + funnel.gates_answer_block(
        "the plan is good", "Nate",
    )

    assert funnel.clear_answered_decline_routes(rows) == [
        {"ref": item.ref, "gate_ref": gate.ref}
    ]
    assert writes == [
        ("project-item-42", "Needs", "none", item.ref)
    ]
    assert item.needs == "none"
    assert [row.ref for row in funnel.startable(rows)] == [item.ref]


def test_decline_route_comment_matches_the_latest_decline_run():
    reason = LIVE_1453_UNSATISFIABLE_ACCEPTANCE
    body = ticket()["body"]
    now = funnel.datetime.now(funnel.timezone.utc)
    earlier_decline = funnel.append_provenance(
        "**Declined:** an earlier reason", "agent", at=now,
        run="run-old", agent="codex",
    )
    earlier_route = funnel.append_provenance(
        implement._declined_unsatisfiable_acceptance_comment(
            "an earlier reason", hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ),
        "agent", at=now, run="run-old", agent="codex",
    )
    latest_decline = funnel.append_provenance(
        "**Declined:** {}".format(reason), "agent", at=now,
        run="run-new", agent="codex",
    )

    assert funnel.parse_decline_route_comment(
        [earlier_decline, earlier_route, latest_decline]
    ) is None

    latest_route = funnel.append_provenance(
        implement._declined_unsatisfiable_acceptance_comment(
            reason, hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ),
        "agent", at=now, run="run-new", agent="codex",
    )
    route = funnel.parse_decline_route_comment(
        [earlier_decline, earlier_route, latest_decline, latest_route]
    )
    assert route is not None
    assert route["type"] == "unsatisfiable-acceptance"


def test_write_declined_external_event_needs_uses_canonical_field(monkeypatch):
    from engine import breakdown as breakdown_engine

    calls = []
    monkeypatch.setattr(
        breakdown_engine, "add_to_project", lambda url: "project-item-id")
    monkeypatch.setattr(
        breakdown_engine, "write_needs",
        lambda item_id, needs, ref: calls.append((item_id, needs, ref)),
    )

    implement.write_declined_external_event_needs(
        ticket()["url"], REPO + "#42",
    )

    assert calls == [("project-item-id", "external-event", REPO + "#42")]


def test_finish_declined_keeps_a_clearable_human_condition_blocked(
        tmp_path, monkeypatch):
    """A real owner decision still gets the existing blocked/Unblock route."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket", lambda repo, number: ticket(number))
    reason = (
        "Nate can clear this by answering whether the requested scope is "
        "acceptable."
    )
    blocked_item = funnel.Item(
        repo=REPO,
        number=42,
        title="implement the bounded runner",
        url=ticket()["url"],
        state="OPEN",
        parent=REPO + "#7",
    )
    effects = {"blocked": [], "comments": [], "released": [], "finished": [],
               "human": []}

    def record_block(*args, **kwargs):
        effects["blocked"].append((args, kwargs))
        blocked_item.labels.append("blocked")

    def record_human_needs(url, ref):
        effects["human"].append((url, ref))
        blocked_item.needs = "human"

    implement.finish_declined(
        reason,
        run="run-42",
        repo=REPO,
        cwd=clone,
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        block_effect=record_block,
        comment_effect=lambda *args, **kwargs: effects["comments"].append(
            (args, kwargs)),
        needs_effect=lambda *args: pytest.fail(
            "a clearable human condition must not be routed to agents"),
        human_needs_effect=record_human_needs,
        external_event_needs_effect=lambda *args: pytest.fail(
            "a direct owner decision is not a pending gate event"),
    )

    assert len(effects["blocked"]) == 1
    assert effects["blocked"][0][0][1] == 42
    assert effects["human"] == [(ticket()["url"], REPO + "#42")]
    assert effects["released"] == [REPO + "#42"]
    assert effects["finished"][0][2] == "skipped-blocked"
    assert funnel.gate_question(blocked_item) == "Unblock?"


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


@pytest.mark.parametrize(("reason", "expected"), [
    (LIVE_1453_UNSATISFIABLE_ACCEPTANCE,
     ("unsatisfiable-acceptance", None)),
    (LIVE_1497_PENDING_GATE_ANSWER,
     ("pending-gate-answer", "nateprich-projects/command-center#1195")),
])
def test_classify_decline_reason_routes_unactionable_shapes(reason, expected):
    assert implement.classify_decline_reason(reason, REPO) == expected


@pytest.fixture
def ff_225_landed_prerequisite_facts():
    """The #225 shape: five child tickets closed before its self-close."""
    return {
        "number": 225,
        "state": "CLOSED",
        "closed_at": "2026-09-25T21:13:39Z",
        "children_total": 5,
        "children_completed": 5,
        "children": [
            {"number": 226, "state": "CLOSED", "merged_pr": 233,
             "merge_sha": "f97b156"},
            {"number": 227, "state": "CLOSED", "merged_pr": 235,
             "merge_sha": "453e640"},
            {"number": 228, "state": "CLOSED", "merged_pr": 236,
             "merge_sha": "f1efab1"},
            {"number": 229, "state": "CLOSED", "merged_pr": 241,
             "merge_sha": "2290f2d"},
            {"number": 230, "state": "CLOSED",
             "closed_at": "2026-09-25T21:11:53Z"},
        ],
        # This reproduction has no parent PR and carries drift/rejected-review
        # markers. Neither is part of the child-ticket completion predicate.
        "parent_merge_pr": None,
        "drift": ["plan edited after Ready", "review verdict rejected"],
    }


@pytest.fixture
def project_with_open_prerequisite_ticket_facts():
    return {
        "number": 225,
        "state": "CLOSED",
        "children_total": 5,
        "children_completed": 4,
        "children": [
            {"number": 226, "state": "CLOSED"},
            {"number": 227, "state": "CLOSED"},
            {"number": 228, "state": "CLOSED"},
            {"number": 229, "state": "CLOSED"},
            {"number": 230, "state": "OPEN"},
        ],
        "open_child": {"number": 230, "state": "OPEN"},
    }


def test_prerequisite_project_landed_reproduces_ff_225(
        ff_225_landed_prerequisite_facts):
    assert len(ff_225_landed_prerequisite_facts["children"]) == 5
    assert all(
        child["state"] == "CLOSED"
        for child in ff_225_landed_prerequisite_facts["children"]
    )
    assert implement.prerequisite_project_landed(
        ff_225_landed_prerequisite_facts) is True


def test_prerequisite_project_with_open_ticket_is_unlanded(
        project_with_open_prerequisite_ticket_facts):
    assert project_with_open_prerequisite_ticket_facts["open_child"] == {
        "number": 230, "state": "OPEN",
    }
    assert implement.prerequisite_project_landed(
        project_with_open_prerequisite_ticket_facts) is False


def test_clear_declined_ticket_block_removes_a_stale_blocked_label(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {
            "labels": [{"name": "blocked"}, {"name": "needs-shaping"}],
        },
    )

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(funnel, "_run_gh", fake_run)

    implement.clear_declined_ticket_block(REPO, 42, cwd=tmp_path)

    (args, kwargs), = calls
    assert args == [
        "gh", "issue", "edit", "42", "--repo", REPO,
        "--remove-label", "blocked",
    ]
    assert kwargs["cwd"] == str(tmp_path)


def test_read_declined_prerequisite_uses_github_ticket_completion_summary(
        monkeypatch):
    calls = []
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: calls.append((query, variables)) or {
            "repository": {
                "issue": {
                    "number": 225,
                    "state": "CLOSED",
                    "subIssuesSummary": {"total": 5, "completed": 5},
                },
            },
        },
    )

    facts = implement.read_declined_prerequisite(
        "nateprich-projects/Fantasy-GM#225")

    assert facts == {
        "number": 225,
        "state": "CLOSED",
        "children_total": 5,
        "children_completed": 5,
    }
    (query, variables), = calls
    assert variables == {
        "owner": "nateprich-projects", "name": "Fantasy-GM", "number": 225,
    }
    assert "subIssuesSummary { total completed }" in query
    assert "pullRequests" not in query
    assert "timelineItems" not in query


def test_read_declined_prerequisite_rejects_incomplete_or_wrong_issue(
        monkeypatch):
    payloads = [
        {"number": 166, "state": "OPEN",
         "subIssuesSummary": {"total": 0, "completed": 0}},
        {"number": 165, "state": "OPEN",
         "subIssuesSummary": {"total": 2, "completed": 3}},
        {"number": 165, "state": "OPEN"},
    ]

    for index, payload in enumerate(payloads):
        monkeypatch.setattr(
            funnel, "gh_graphql",
            lambda query, **variables: {"repository": {"issue": payload}},
        )
        facts = implement.read_declined_prerequisite(REPO + "#165")
        if index == 0:
            assert facts is None
        else:
            assert facts is not None and facts["state"] == "OPEN"
            assert implement.prerequisite_project_landed(facts) is None


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
    _stub_claim_state(monkeypatch, "empty")
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
    _stub_claim_state(monkeypatch, "empty")
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
    _stub_claim_state(monkeypatch, "empty")
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
    _stub_claim_state(monkeypatch, "owned")
    (clone / "implemented.txt").write_text("done\n")
    answer_path = tmp_path / "handoff.json"
    if answer_text is not None:
        answer_path.write_text(answer_text)
    effects = {"released": [], "finished": []}
    monkeypatch.chdir(clone)
    monkeypatch.setattr(
        implement, "release_claim",
        lambda ref, **kwargs: effects["released"].append(ref),
    )
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
    _stub_claim_state(monkeypatch, "empty")
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
    _stub_claim_state(monkeypatch, "owned")
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


def test_a_rebased_ticket_branch_pushes_as_a_fast_forward_keeping_the_run_tree(
        tmp_path, monkeypatch):
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

    _stub_claim_state(monkeypatch, "empty")
    implement._push_ticket_branch(
        clone, "ticket/42", ref=REPO + "#42", run="run-42", agent="codex",
    )

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
        if (len(argv) >= 3 and argv[1] == "-c" and
                "sys.version_info" in argv[2]):
            return real_run(argv, **kwargs)
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


def test_run_passes_a_measured_bound_to_each_command_kind(
        tmp_path, monkeypatch):
    seen = []

    class CompletedCommand:
        pid = 1
        returncode = 0
        stdin = stdout = stderr = None

        def __init__(self, command):
            self.command = command

        def communicate(self, input=None, timeout=None):
            seen.append((self.command, timeout))
            return "", ""

    monkeypatch.setattr(
        implement.subprocess, "Popen",
        lambda command, **kwargs: CompletedCommand(command),
    )
    # These fixtures pin timeout classification. The full Git callsite
    # inventory is checked below and recorded in docs/finish-subprocess-bounds.md.
    commands = [
        (["git", "rev-parse", "--show-toplevel"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "branch", "--show-current"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "remote", "get-url", "origin"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "diff", "--name-only", "-z"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "diff", "--cached", "--name-only", "-z"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "ls-files", "--others", "--exclude-standard", "-z"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "diff", "--name-only", "-z", "origin/main...HEAD"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "ls-files", "-z", "--", "engine/implement.py"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "add", "--", "engine/implement.py"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "commit", "-m", "Finish #42"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "rev-list", "--count", "origin/main..HEAD"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "merge", "-s", "ours", "--no-edit", "-m",
          "Record the previous ticket/42 tip before pushing the rebased branch",
          "origin/ticket/42"],
         implement.LOCAL_GIT_TIMEOUT_SECONDS),
        (["git", "ls-remote", "--exit-code", "--heads", "origin",
          "refs/heads/ticket/42"],
         implement.REMOTE_GIT_TIMEOUT_SECONDS),
        (["git", "fetch", "origin",
          "+refs/heads/ticket/42:refs/remotes/origin/ticket/42"],
         implement.REMOTE_GIT_TIMEOUT_SECONDS),
        (["git", "push", "--set-upstream", "origin", "ticket/42"],
         implement.REMOTE_GIT_TIMEOUT_SECONDS),
        (["make", "check", "test"],
         implement.TEST_COMMAND_TIMEOUT_SECONDS),
        ([sys.executable, "-m", "pytest", "-q"],
         implement.TEST_COMMAND_TIMEOUT_SECONDS),
        (["sh", "-c", "make check test"],
         implement.TEST_COMMAND_TIMEOUT_SECONDS),
        (["sh", "-c", "python3 -m pytest tests/ -q"],
         implement.TEST_COMMAND_TIMEOUT_SECONDS),
        ([sys.executable, "-m", "compileall", "engine"],
         implement.COMPILE_COMMAND_TIMEOUT_SECONDS),
        ([sys.executable, "-c",
          "from pathlib import Path; compile(Path('funnel.py').read_text(), "
          "'funnel.py', 'exec')"],
         implement.COMPILE_COMMAND_TIMEOUT_SECONDS),
        ([sys.executable, "-c",
          "import sys; print('%d.%d' % sys.version_info[:2])"],
         implement.INTERPRETER_PROBE_TIMEOUT_SECONDS),
    ]

    for command, _ in commands:
        implement._run(command, cwd=tmp_path)

    assert seen == commands
    assert all(timeout < implement.CLAIM_TTL_SECONDS
               for _, timeout in seen)


def test_every_subprocess_callsite_has_an_explicit_timeout():
    source = pathlib.Path(implement.__file__).read_text()
    tree = ast.parse(source)
    run_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run"
    ]
    direct_runs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    ]

    def has_timeout(call):
        return any(keyword.arg == "timeout" for keyword in call.keywords)

    assert run_calls
    assert [call.lineno for call in run_calls if not has_timeout(call)] == []
    # Packet collection is the sole direct subprocess.run path; it already has
    # its own 30-second cap and is outside finish-ticket's _run inventory.
    assert len(direct_runs) == 1
    assert all(has_timeout(call) for call in direct_runs)


def test_finish_git_invocation_sites_match_the_recorded_inventory():
    source = pathlib.Path(implement.__file__).read_text()
    tree = ast.parse(source)
    actual = Counter()
    for node in ast.walk(tree):
        if (not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id != "_run" or not node.args
                or not isinstance(node.args[0], ast.List)):
            continue
        command = node.args[0].elts
        if (not command or not isinstance(command[0], ast.Constant)
                or command[0].value != "git"):
            continue
        if len(command) < 2:
            actual[("<missing-subcommand>", "")] += 1
            continue
        subcommand = command[1]
        if isinstance(subcommand, ast.Constant):
            second = str(subcommand.value)
        elif isinstance(subcommand, ast.Starred):
            second = "<path-command>"
        else:
            second = "<dynamic>"
        following = ""
        if len(command) > 2 and isinstance(command[2], ast.Constant):
            following = str(command[2].value)
        actual[(second, following)] += 1

    expected = Counter({
        ("rev-parse", "--show-toplevel"): 1,
        ("branch", "--show-current"): 1,
        ("remote", "get-url"): 2,
        ("ls-remote", "--exit-code"): 2,
        ("<path-command>", ""): 1,
        ("add", "--"): 1,
        ("commit", "-m"): 3,
        ("rev-list", "--count"): 2,
        ("fetch", "origin"): 1,
        ("merge-base", "--is-ancestor"): 1,
        ("merge", "-s"): 1,
        ("push", "--set-upstream"): 1,
    })
    assert actual == expected

    inventory = (ROOT / "docs" / "finish-subprocess-bounds.md").read_text()
    assert "17 bounded Git callsites" in inventory
    for command in (
        "git diff --name-only -z",
        "git diff --cached --name-only -z",
        "git ls-files --others --exclude-standard -z",
        "git diff --name-only -z origin/main...HEAD",
        "git ls-files -z -- <selected paths>",
        "git fetch origin",
        "git merge-base --is-ancestor",
        "git merge -s ours",
        "git push --set-upstream origin",
        "make",
        "python3 -m pytest",
    ):
        assert command in inventory


def test_slow_command_finishes_before_its_bound(tmp_path):
    result = implement._run(
        [sys.executable, "-c",
         "import time; time.sleep(0.05); print('finished')"],
        cwd=tmp_path, timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "finished"


def test_run_tests_applies_test_bound_to_shell_wrapped_command(
        tmp_path, monkeypatch):
    seen = []

    def record(command, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(implement, "_run", record)
    implement.run_tests(tmp_path, [
        ["sh", "-c", "python3 -m pytest tests/ -q"],
        ["sh", "-c", "make check test"],
        [sys.executable, "-m", "unittest", "discover"],
        [sys.executable, "-c",
         "from pathlib import Path; compile(Path('funnel.py').read_text(), "
         "'funnel.py', 'exec')"],
    ])

    assert [call["timeout"] for call in seen] == [
        implement.TEST_COMMAND_TIMEOUT_SECONDS,
        implement.TEST_COMMAND_TIMEOUT_SECONDS,
        implement.TEST_COMMAND_TIMEOUT_SECONDS,
        implement.COMPILE_COMMAND_TIMEOUT_SECONDS,
    ]


def test_timed_out_test_keeps_checkpointed_work_and_finishes(
        tmp_path, monkeypatch):
    remote, clone = make_clone(tmp_path)
    (clone / "implemented.txt").write_text("unfinished\n")
    monkeypatch.setattr(
        implement, "fetch_ticket", lambda repo, number: ticket(number))
    _stub_claim_state(monkeypatch, "owned")
    effects = {"released": [], "finished": []}
    real_popen = subprocess.Popen
    spawned = []
    killed = []

    class NeverReturns:
        pid = 23456
        returncode = None
        stdin = stdout = stderr = None

        def __init__(self, command):
            self.command = command
            self.timeout = None
            self.waited = False
            self.killed = False

        def communicate(self, input=None, timeout=None):
            self.timeout = timeout
            raise subprocess.TimeoutExpired(self.command, timeout)

        def kill(self):
            self.killed = True

        def wait(self, *args, **kwargs):
            self.waited = True
            raise AssertionError("timeout handling must not wait for the child")

    def fake_popen(command, **kwargs):
        if command == ["make", "test"]:
            process = NeverReturns(command)
            spawned.append(process)
            return process
        return real_popen(command, **kwargs)

    monkeypatch.setattr(implement.subprocess, "Popen", fake_popen)
    if implement.os.name == "posix":
        monkeypatch.setattr(
            implement.os, "killpg",
            lambda pid, sig: killed.append((pid, sig)),
        )

    with pytest.raises(implement.CommandTimeoutError, match="make test timed out"):
        implement.finish_done(
            answer(), run="run-42", repo=REPO, cwd=clone,
            test_commands=[["make", "test"]],
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            pr_effect=lambda *args: pytest.fail(
                "a timed-out checkout must not open a PR"),
        )

    (process,) = spawned
    assert process.timeout == implement.TEST_COMMAND_TIMEOUT_SECONDS
    assert process.waited is False
    if implement.os.name == "posix":
        assert killed == [(process.pid, signal.SIGKILL)]
    else:
        assert process.killed is True
    assert effects["released"] == [REPO + "#42"]
    (finished,) = effects["finished"]
    assert finished[:3] == ("codex", "run-42", "errored")
    assert finished[3].endswith("work was checkpointed")

    # Restore the real process launcher before checking the bare remote.
    monkeypatch.setattr(implement.subprocess, "Popen", real_popen)
    refs = run_git("--git-dir", str(remote), "show-ref").stdout
    assert "refs/heads/ticket/42" in refs
    assert run_git(
        "--git-dir", str(remote), "show",
        "refs/heads/ticket/42:implemented.txt",
    ).stdout == "unfinished\n"
