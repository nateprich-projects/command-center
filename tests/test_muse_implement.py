"""The Muse implement runner owns the protocol; the model judges one ticket.

Phase 3 of #794: begin claims the ticket, the runner clones it onto
ticket/<n> and assembles the packet with implement-packet, and the model
sees the judgement-only prompt with the packet inline. It changes files
and writes one structured answer to answer.json; the runner hands that
answer to finish-ticket, which performs every side effect.

The harness below stubs the funnel, heartbeat, packet, finish, gh, and
muse binaries with a real fixture git remote underneath; the routine text
is the real file, so the prompt-substitution tests pin the artifact that
ships.
"""

from __future__ import annotations

import json
import os
import pathlib
import shlex
import stat
import subprocess
import sys
import uuid

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "muse-implement"
ROUTINE = ROOT / "routines" / "muse-implement.md"

TICKET = 42
BEGIN_REF = "example/widgets#42"
BEGIN_REPO = "example/widgets"


def _begin(**overrides):
    begin = {
        "agent": "muse",
        "run": "writer-run",
        "gate": "ok",
        "do": "ticket",
        "work": {"ref": BEGIN_REF, "repo": BEGIN_REPO},
    }
    begin.update(overrides)
    return begin


def _packet(**overrides):
    packet = {
        "repo": BEGIN_REPO,
        "ticket": {"ref": BEGIN_REF, "number": TICKET,
                   "title": "Do the thing", "body": "Risk: escalated"},
        "plan": {"ref": "example/widgets#7", "number": 7,
                 "body": "# Plan\n"},
        "verdict": {"pr": None, "head_sha": None, "verdict": None,
                    "blocking": []},
        "prior_run": None,
        "marker": "packet-marker-817",
    }
    packet.update(overrides)
    return packet


ANSWER = json.dumps({"done": True, "summary": "Did the thing.",
                     "departures": ["Did not do the other thing."]})

FUNNEL_STUB = (
    "import os, pathlib, sys\n"
    "root = pathlib.Path(__file__).parent\n"
    "command = sys.argv[1] if len(sys.argv) > 1 else ''\n"
    "with (root / 'funnel.calls').open('a') as fh:\n"
    "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    "if command == 'session-server':\n"
    "    print('127.0.0.1:1:stub', flush=True)\n"
    "elif command == 'begin':\n"
    "    (root / 'begin.session_id').write_text(os.environ.get('MUSE_SESSION_ID', ''))\n"
    "    print((root / 'begin.json').read_text(), end='')\n"
    "elif command == 'session-stop':\n"
    "    pass\n"
    "elif command == 'release':\n"
    "    pass\n"
    "else:\n"
    "    raise SystemExit('unexpected funnel command: ' + command)\n"
)

HEARTBEAT_STUB = (
    "import pathlib, sys\n"
    "with (pathlib.Path(__file__).parent / 'heartbeat.log').open('a') as fh:\n"
    "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
)

PACKET_STUB = (
    "import os, pathlib, sys\n"
    "root = pathlib.Path(__file__).parent\n"
    "with (root / 'packet.calls').open('a') as fh:\n"
    "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    "if os.environ.get('PACKET_STATUS', '0') != '0':\n"
    "    sys.stderr.write(os.environ.get('PACKET_ERROR', 'packet failed'))\n"
    "    raise SystemExit(1)\n"
    "sys.stdout.write((root / 'packet.json').read_text())\n"
)

FINISH_STUB = (
    "import os, pathlib, sys\n"
    "root = pathlib.Path(__file__).parent\n"
    "with (root / 'finish.calls').open('a') as fh:\n"
    "    fh.write(os.getcwd() + ' :: ' + ' '.join(sys.argv[1:]) + '\\n')\n"
    "args = sys.argv[1:]\n"
    "answer = args[args.index('--answer-file') + 1]\n"
    "(root / 'finish.answer').write_text(pathlib.Path(answer).read_text())\n"
    "if os.environ.get('FINISH_STATUS', '0') != '0':\n"
    "    sys.stderr.write(os.environ.get('FINISH_ERROR', 'finish failed'))\n"
    "    raise SystemExit(1)\n"
    "print('{\"number\": 99, \"url\": "
    "\"https://github.com/example/widgets/pull/99\"}')\n"
)

GH_STUB = (
    "#!/bin/bash\n"
    "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
    "if [[ \"${GH_STATUS:-0}\" -ne 0 ]]; then exit \"$GH_STATUS\"; fi\n"
    "git clone --quiet \"$CLONE_SOURCE\" \"$4\"\n"
)

MUSE_STUB = (
    "#!/bin/bash\n"
    "count_file=\"$MUSE_COUNT\"\n"
    "n=1\n"
    "if [[ -f \"$count_file\" ]]; then n=$(($(cat \"$count_file\") + 1)); fi\n"
    "printf '%s' \"$n\" > \"$count_file\"\n"
    "printf '%s\\n' \"$@\" > \"$MUSE_ARGS.$n\"\n"
    "workspace=''\n"
    "prompt_file=''\n"
    "previous=''\n"
    "for argument in \"$@\"; do\n"
    "  if [[ \"$previous\" == '--workspace' ]]; then workspace=\"$argument\"; fi\n"
    "  if [[ \"$previous\" == '--prompt-file' ]]; then prompt_file=\"$argument\"; fi\n"
    "  previous=\"$argument\"\n"
    "done\n"
    "test -n \"$workspace\" && test -s \"$prompt_file\"\n"
    "cp \"$prompt_file\" \"$MUSE_PROMPT.$n\"\n"
    "if [[ -n \"${MUSE_SLEEP:-}\" ]]; then exec sleep \"$MUSE_SLEEP\"; fi\n"
    "git -C \"$workspace\" branch --show-current > \"$MUSE_BRANCH.$n\"\n"
    "git -C \"$workspace\" rev-parse --abbrev-ref '@{u}' > \"$MUSE_UPSTREAM.$n\" "
    "2>/dev/null || printf 'none' > \"$MUSE_UPSTREAM.$n\"\n"
    "if [[ -n \"${MUSE_REQUIRE_FILE:-}\" ]]; then "
    "test -f \"$workspace/$MUSE_REQUIRE_FILE\"; fi\n"
    "if [[ -z \"${MUSE_MISSING_ANSWER:-}\" ]]; then "
    "printf '%s' \"$MUSE_ANSWER\" > \"$workspace/answer.json\"; fi\n"
    "if [[ -n \"${MUSE_TOUCH:-}\" ]]; then "
    "printf 'changed' > \"$workspace/$MUSE_TOUCH\"; fi\n"
    "exit \"${MUSE_STATUS:-0}\"\n"
)


def _executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _python_without_session_id(tmp_path, mode):
    bin_dir = tmp_path / "python-bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "python3"
    exit_status = 1 if mode == "failure" else 0
    _executable(
        wrapper,
        "#!/bin/bash\n"
        "if [[ \"$1\" == \"-c\" && \"$2\" == \"import uuid; print(uuid.uuid4())\" ]]; then\n"
        "  exit {}\n"
        "fi\n"
        "exec {} \"$@\"\n".format(exit_status, shlex.quote(sys.executable)),
    )
    return {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
    }


def _run_git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True)


def _seed_remote(base, *, ticket_branch=False):
    """A fixture origin with a seed commit on main, cloned by the fake gh."""
    remote = base / "remote.git"
    seed = base / "seed"
    _run_git("init", "--bare", "--quiet", str(remote))
    _run_git("init", "--quiet", "-b", "main", str(seed))
    _run_git("config", "user.name", "Fixture", cwd=seed)
    _run_git("config", "user.email", "fixture@example.test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _run_git("add", "README.md", cwd=seed)
    _run_git("commit", "--quiet", "-m", "seed", cwd=seed)
    _run_git("remote", "add", "origin", str(remote), cwd=seed)
    _run_git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    _run_git("--git-dir", str(remote), "symbolic-ref", "HEAD",
             "refs/heads/main")
    if ticket_branch:
        _run_git("switch", "--quiet", "-c", "ticket/{}".format(TICKET),
                 cwd=seed)
        (seed / "continued-marker").write_text("earlier push\n")
        _run_git("add", "continued-marker", cwd=seed)
        _run_git("commit", "--quiet", "-m", "earlier work", cwd=seed)
        _run_git("push", "--quiet", "-u", "origin",
                 "ticket/{}".format(TICKET), cwd=seed)
    return remote


def _stubbed_runner(tmp_path, begin, *, packet=None, bound_seconds=20,
                    gh_status=0, muse_body=None, gh_body=None,
                    routine_body=None, ticket_branch=False, extra_env=None,
                    muse_model_body=None):
    """Run the implementer against stub funnel/heartbeat/packet/finish/gh/muse.

    The fixture remote is real git, so the runner's fetch, branch inspection,
    and checkout run for real; only the model and the GitHub effects are
    faked. The routine text defaults to the real file.
    """
    repo = tmp_path / "runner-repo"
    (repo / "routines").mkdir(parents=True)
    (repo / "routines" / "muse-implement.md").write_text(
        routine_body if routine_body is not None else ROUTINE.read_text()
    )
    (repo / "begin.json").write_text(json.dumps(begin))
    if isinstance(packet, str):
        (repo / "packet.json").write_text(packet)
    else:
        (repo / "packet.json").write_text(
            json.dumps(packet if packet is not None else _packet()))
    (repo / "funnel.py").write_text(FUNNEL_STUB)
    (repo / "heartbeat.py").write_text(HEARTBEAT_STUB)
    (repo / "implement-packet").write_text(PACKET_STUB)
    (repo / "finish-ticket").write_text(FINISH_STUB)
    # The real module, not a stub: the point of the model tests below is
    # that the runner's argv comes from the real allowlist.
    (repo / "muse_model.py").write_text(
        muse_model_body if muse_model_body is not None
        else (ROOT / "muse_model.py").read_text())

    remote = _seed_remote(tmp_path, ticket_branch=ticket_branch)

    gh = tmp_path / "gh"
    _executable(gh, gh_body or GH_STUB)
    muse = tmp_path / "muse"
    _executable(muse, muse_body or MUSE_STUB)

    workspace_root = tmp_path / "workspaces"
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        MUSE_IMPLEMENT_REPO=str(repo),
        MUSE_BIN=str(muse),
        GH_BIN=str(gh),
        MUSE_IMPLEMENT_BOUND_SECONDS=str(bound_seconds),
        MUSE_IMPLEMENT_WORKSPACE_ROOT=str(workspace_root),
        CLONE_SOURCE=str(remote),
        GH_STATUS=str(gh_status),
        GH_LOG=str(repo / "gh.log"),
        MUSE_COUNT=str(repo / "muse.count"),
        MUSE_ARGS=str(repo / "muse.args"),
        MUSE_PROMPT=str(repo / "muse.prompt"),
        MUSE_BRANCH=str(repo / "muse.branch"),
        MUSE_UPSTREAM=str(repo / "muse.upstream"),
        MUSE_ANSWER=ANSWER,
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), "escalated", "max"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=bound_seconds + 25,
    )
    return proc, repo


def _heartbeat(repo):
    log = repo / "heartbeat.log"
    return log.read_text() if log.exists() else ""


def _muse_calls(repo):
    count = repo / "muse.count"
    return int(count.read_text()) if count.exists() else 0


def _calls(repo, name):
    calls = repo / (name + ".calls")
    return calls.read_text().splitlines() if calls.exists() else []


def test_the_happy_path_runs_packet_model_and_finish_in_order(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    assert _calls(repo, "packet") == ["42 --repo example/widgets --agent muse"]

    # The model ran in the fresh clone, on a new ticket branch from main.
    workspace = pathlib.Path(
        (repo / "muse.args.1").read_text().splitlines()[
            (repo / "muse.args.1").read_text().splitlines().index(
                "--workspace") + 1]
    )
    assert (repo / "muse.branch.1").read_text().strip() == "ticket/42"
    assert (repo / "muse.upstream.1").read_text().strip() == "none"
    assert (repo / "gh.log").read_text() == "repo clone example/widgets {}\n".format(
        workspace
    )

    # The writer shape: a shell for tests, the prompt by file, no reviewer
    # flags — and no positional prompt, so a plan cannot outgrow argv.
    args = (repo / "muse.args.1").read_text().splitlines()
    assert args[0] == "exec"
    session_id = (repo / "begin.session_id").read_text()
    assert str(uuid.UUID(session_id)) == session_id
    assert args[args.index("--session-id") + 1] == session_id
    assert args[args.index("--model") + 1] == "muse-spark-1.3"
    assert args[args.index("--reasoning-effort") + 1] == "max"
    assert args[args.index("--sandbox-network") + 1] == "enabled"
    assert "--disable-sandbox" in args
    assert args[args.index("--approval-mode") + 1] == "never"
    assert args[args.index("--max-model-steps") + 1] == "200"
    assert "--no-foreign-personal-context" in args
    assert "--prompt-file" in args
    assert "--json" in args
    assert "--disable-write" not in args
    assert "--disable-shell" not in args
    assert "--yolo" not in args
    assert "Command Center implementer" not in " ".join(args)

    # The packet went into the prompt file, with no literal left behind.
    prompt = (repo / "muse.prompt.1").read_text()
    assert "packet-marker-817" in prompt
    assert "PACKET_JSON" not in prompt
    assert "begin --agent muse --tier escalated --role implement" in (
        repo / "funnel.calls"
    ).read_text()

    # finish-ticket received the model's answer from the checkout, ran there,
    # and owns the release and finish: the runner recorded no heartbeat.
    finish = _calls(repo, "finish")
    assert len(finish) == 1
    cwd, argv = finish[0].split(" :: ")
    assert cwd == str(workspace)
    answer_args = argv.split()
    answer_file = pathlib.Path(answer_args[answer_args.index(
        "--answer-file") + 1])
    assert answer_file != workspace / "answer.json"
    assert not answer_file.exists(), "the handoff scratch file must be cleaned up"
    assert answer_args[answer_args.index("--run") + 1:] == [
        "writer-run", "--agent", "muse", "--repo", "example/widgets",
    ]
    assert (repo / "finish.answer").read_text() == ANSWER
    assert _heartbeat(repo) == ""

    assert not workspace.exists(), "the disposable clone must be removed after the run"
    assert not list((tmp_path / "workspaces").iterdir())


@pytest.mark.parametrize("mode", ("failure", "empty"))
def test_session_id_setup_failure_does_not_gate_implementation(tmp_path, mode):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), extra_env=_python_without_session_id(tmp_path, mode)
    )

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    assert (repo / "begin.session_id").read_text() == ""
    args = (repo / "muse.args.1").read_text().splitlines()
    assert "--session-id" not in args
    assert len(_calls(repo, "finish")) == 1
    assert "continuing without session usage telemetry" in proc.stderr


def test_a_remote_ticket_branch_continues_in_place(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), ticket_branch=True,
        extra_env={"MUSE_REQUIRE_FILE": "continued-marker"},
    )

    assert proc.returncode == 0, proc.stderr
    assert (repo / "muse.branch.1").read_text().strip() == "ticket/42"
    assert (repo / "muse.upstream.1").read_text().strip() == "origin/ticket/42"


def test_the_runner_reads_the_routine_at_run_time():
    """The prompt is the routine file, not a copy: the drift surface #52
    exists for must not come back in the engine."""
    runner = SCRIPT.read_text()
    assert "routines/muse-implement.md" in runner
    assert "PACKET_JSON" in runner
    assert "PROMPT_TEMPLATE//BEGIN_JSON" not in runner


@pytest.mark.parametrize(
    ("gate", "outcome"),
    (
        ("over", "skipped-over-pace"),
        ("unknown", "skipped-usage-unknown"),
        ("reserve", "skipped-api-reserve"),
        ("ok", "nothing-to-do"),
    ),
)
def test_a_stop_finishes_without_a_clone_or_model(tmp_path, gate, outcome):
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "stop-run", "gate": gate, "do": "stop"},
    )

    assert proc.returncode == 0, proc.stderr
    assert not (repo / "gh.log").exists()
    assert _muse_calls(repo) == 0
    assert _calls(repo, "packet") == []
    assert _calls(repo, "finish") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run stop-run --outcome {}\n".format(outcome)
    )


def test_a_stop_with_a_why_records_the_note_and_names_it_on_stderr(tmp_path):
    why = "could not establish ticket branch facts: transient GraphQL response"
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "stop-run", "gate": "error",
         "do": "stop", "why": why},
    )

    assert proc.returncode == 0, proc.stderr
    assert not (repo / "gh.log").exists()
    assert _muse_calls(repo) == 0
    assert _calls(repo, "packet") == []
    assert _calls(repo, "finish") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run stop-run --outcome nothing-to-do "
        "--note {}\n".format(why)
    )
    assert "muse-implement: begin stopped: {}".format(why) in proc.stderr


def test_the_runner_refuses_an_unknown_tier_or_a_lowered_escalated_effort():
    bad_tier = subprocess.run(
        ["/bin/bash", str(SCRIPT), "urgent", "max"],
        capture_output=True,
        text=True,
    )
    bad_effort = subprocess.run(
        ["/bin/bash", str(SCRIPT), "escalated", "high"],
        capture_output=True,
        text=True,
    )
    unknown_effort = subprocess.run(
        ["/bin/bash", str(SCRIPT), "standard", "low"],
        capture_output=True,
        text=True,
    )

    assert bad_tier.returncode == 1
    assert "tier must be escalated or standard" in bad_tier.stderr
    assert bad_effort.returncode == 1
    assert "reasoning effort must be max on the escalated tier" in bad_effort.stderr
    assert unknown_effort.returncode == 1
    assert "reasoning effort must be max or high" in unknown_effort.stderr


def test_the_standard_tier_accepts_high_effort(tmp_path):
    """`standard high` is what the schedule runs (#1191). It must get past the
    argument guard; with no routine in the repo it then stops at the prompt
    check, before begin, which proves the guard let it through."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, MUSE_IMPLEMENT_REPO=str(repo),
               HOME=str(tmp_path), TMPDIR=str(tmp_path))
    proc = subprocess.run(["/bin/bash", str(SCRIPT), "standard", "high"],
                          env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1
    assert "reasoning effort" not in proc.stderr
    assert "refusing to run without a prompt" in proc.stderr


def test_a_missing_routine_refuses_before_begin(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, MUSE_IMPLEMENT_REPO=str(repo),
               HOME=str(tmp_path), TMPDIR=str(tmp_path))
    proc = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 1
    assert "refusing to run without a prompt" in proc.stderr
    assert not (repo / "funnel.calls").exists()


@pytest.mark.parametrize("placeholders", (0, 2))
def test_a_routine_without_exactly_one_packet_placeholder_is_refused(
        tmp_path, placeholders):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(),
        routine_body="# Implement\n\n---\n\nJudge this.\n"
        + "PACKET_JSON\n" * placeholders)
    assert proc.returncode == 1
    assert "PACKET_JSON exactly once" in proc.stderr
    assert _muse_calls(repo) == 0


@pytest.mark.parametrize("begin", (
    _begin(do="review", work={"pr": 7, "repo": BEGIN_REPO}),
    _begin(work={"ref": BEGIN_REF}),
    _begin(work={"ref": "example/widgets", "repo": BEGIN_REPO}),
    _begin(work={"ref": "example/widgets#0", "repo": BEGIN_REPO}),
))
def test_invalid_begin_work_finishes_errored_without_a_clone(tmp_path, begin):
    proc, repo = _stubbed_runner(tmp_path, begin)

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert not (repo / "gh.log").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "invalid" in heartbeat


def test_a_clone_failure_finishes_errored_and_never_launches_muse(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), gh_status=23)

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _calls(repo, "packet") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "could not clone example/widgets into the fresh implementation workspace\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()
    assert not list((tmp_path / "workspaces").iterdir())


def test_a_packet_failure_finishes_errored_without_a_model_call(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(),
        extra_env={"PACKET_STATUS": "1", "PACKET_ERROR": "could not read ticket"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _calls(repo, "finish") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "implement-packet failed for example/widgets#42: could not read ticket\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()


def test_an_invalid_packet_finishes_errored_without_a_model_call(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), packet="not json{")

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "implement-packet for example/widgets#42 printed invalid JSON\n"
    )


def test_a_model_failure_releases_and_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), extra_env={"MUSE_STATUS": "3"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 1
    assert _calls(repo, "finish") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "muse exec failed (exit 3) on example/widgets#42\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()


def test_a_missing_answer_releases_and_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), extra_env={"MUSE_MISSING_ANSWER": "1"})

    assert proc.returncode == 1
    assert _calls(repo, "finish") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "the model left no answer.json for example/widgets#42\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()


def test_a_finish_ticket_failure_releases_and_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(),
        extra_env={"FINISH_STATUS": "1",
                   "FINISH_ERROR": "answer is not valid JSON: boom"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 1
    assert len(_calls(repo, "finish")) == 1
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "finish-ticket failed for example/widgets#42: "
        "answer is not valid JSON: boom\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()


def test_the_wall_clock_bound_kills_the_process_group_and_finishes_errored(
        tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), bound_seconds=1,
        extra_env={"MUSE_SLEEP": "30"},
    )

    assert proc.returncode == 124
    assert "killing run after 1s" in proc.stderr
    assert _calls(repo, "finish") == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run writer-run --outcome errored --note "
        "killed after 0 minutes: escalated implementation run exceeded the "
        "wall-clock bound (#392)\n"
    )
    assert "release example/widgets#42" in (repo / "funnel.calls").read_text()
    assert not list((tmp_path / "workspaces").iterdir())


def test_a_fresh_workspace_resolves_uid_and_pushes_over_ssh(tmp_path):
    """The workspace boundary must preserve the uid and the provided SSH path.

    The fake Muse executable models the managed shell boundary: it refuses to
    enter the workspace probe unless the runner disables the nested sandbox.
    The probe then exercises the real uid lookup and Git SSH transport without
    requiring a network credential or a checkout-local URL rewrite. The model
    still answers, and finish-ticket still runs, so the full protocol holds.
    """
    probe = tmp_path / "workspace-probe.py"
    _executable(
        probe,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import pwd\n"
        "import subprocess\n"
        "import sys\n"
        "\n"
        "workspace = pathlib.Path(sys.argv[1])\n"
        "result = pathlib.Path(os.environ['MUSE_PROBE_RESULT'])\n"
        "ssh = os.environ['TEST_SSH']\n"
        "remote = pathlib.Path(os.environ['REMOTE_REPO'])\n"
        "uid = os.getuid()\n"
        "user = pwd.getpwuid(uid)\n"
        "assert user.pw_uid == uid\n"
        "origin = subprocess.check_output(\n"
        "    ['git', '-C', str(workspace), 'remote', 'get-url', 'origin'],\n"
        "    text=True,\n"
        ").strip()\n"
        "assert origin.startswith('ssh://'), origin\n"
        "instead_of = subprocess.run(\n"
        "    ['git', '-C', str(workspace), 'config', '--local',\n"
        "     '--get-regexp', r'^url\\..*\\.insteadOf$'],\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "assert instead_of.returncode == 1, instead_of.stdout\n"
        "handshake = subprocess.run(\n"
        "    [ssh, '-T', 'git@forge'], capture_output=True, text=True\n"
        ")\n"
        "assert handshake.returncode == 1, handshake\n"
        "assert 'authenticated' in handshake.stdout\n"
        "push_env = dict(os.environ, GIT_SSH_COMMAND=ssh)\n"
        "subprocess.run(\n"
        "    ['git', 'push', 'origin', 'HEAD:refs/heads/muse-regression'],\n"
        "    cwd=workspace, env=push_env, check=True,\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "present = subprocess.run(\n"
        "    ['git', '--git-dir', str(remote), 'show-ref', '--verify',\n"
        "     '--quiet', 'refs/heads/muse-regression']\n"
        ")\n"
        "assert present.returncode == 0\n"
        "subprocess.run(\n"
        "    ['git', 'push', 'origin', ':refs/heads/muse-regression'],\n"
        "    cwd=workspace, env=push_env, check=True,\n"
        "    capture_output=True, text=True,\n"
        ")\n"
        "removed = subprocess.run(\n"
        "    ['git', '--git-dir', str(remote), 'show-ref', '--verify',\n"
        "     '--quiet', 'refs/heads/muse-regression']\n"
        ")\n"
        "assert removed.returncode == 1\n"
        "result.write_text(json.dumps({\n"
        "    'uid': uid, 'user': user.pw_name, 'origin': origin,\n"
        "    'remote_ref_removed': True,\n"
        "}))\n",
    )

    ssh = tmp_path / "ssh"
    _executable(
        ssh,
        "#!/bin/bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$SSH_LOG\"\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$argument\" == '-T' ]]; then\n"
        "    printf '%s\\n' \"Hi regression! You have successfully authenticated.\"\n"
        "    exit 1\n"
        "  fi\n"
        "done\n"
        "request=\"${!#}\"\n"
        "case \"$request\" in\n"
        "  *git-receive-pack*) exec git-receive-pack \"$REMOTE_REPO\" ;;\n"
        "  *git-upload-pack*) exec git-upload-pack \"$REMOTE_REPO\" ;;\n"
        "  *) echo \"unexpected SSH request: $request\" >&2; exit 2 ;;\n"
        "esac\n",
    )

    # A real clone, re-pointed at the SSH origin the probe asserts: the
    # runner's own fetch and branch inspection travel the fake SSH transport.
    gh_body = (
        "#!/bin/bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
        "workspace=\"${4:?workspace argument missing}\"\n"
        "git clone --quiet \"$CLONE_SOURCE\" \"$workspace\"\n"
        "git -C \"$workspace\" remote set-url origin "
        "ssh://git@forge/muse-regression.git\n"
    )
    muse_body = (
        "#!/bin/bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$@\" > \"$MUSE_ARGS.1\"\n"
        "printf '%s' 1 > \"$MUSE_COUNT\"\n"
        "workspace=''\n"
        "has_disable_sandbox=0\n"
        "previous=''\n"
        "for argument in \"$@\"; do\n"
        "  if [[ \"$previous\" == '--workspace' ]]; then workspace=\"$argument\"; fi\n"
        "  if [[ \"$argument\" == '--disable-sandbox' ]]; then has_disable_sandbox=1; fi\n"
        "  previous=\"$argument\"\n"
        "done\n"
        "if [[ $has_disable_sandbox -ne 1 ]]; then\n"
        "  echo 'nested sandbox hid the system user database' >&2\n"
        "  exit 42\n"
        "fi\n"
        "\"$MUSE_WORKSPACE_PROBE\" \"$workspace\"\n"
        "printf '%s' \"$MUSE_ANSWER\" > \"$workspace/answer.json\"\n"
    )

    remote = _seed_remote(tmp_path / "ssh-remote")
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(run="ssh-workspace-run"),
        muse_body=muse_body,
        gh_body=gh_body,
        extra_env={
            "MUSE_WORKSPACE_PROBE": str(probe),
            "MUSE_PROBE_RESULT": str(
                tmp_path / "runner-repo" / "workspace-probe.json"
            ),
            "REMOTE_REPO": str(remote),
            "CLONE_SOURCE": str(remote),
            "TEST_SSH": str(ssh),
            "GIT_SSH_COMMAND": str(ssh),
            "SSH_LOG": str(tmp_path / "runner-repo" / "ssh.log"),
        },
    )

    assert proc.returncode == 0, proc.stderr
    observed = json.loads((repo / "workspace-probe.json").read_text())
    assert observed["uid"] == os.getuid()
    assert observed["user"]
    assert observed["origin"] == "ssh://git@forge/muse-regression.git"
    assert observed["remote_ref_removed"] is True

    ssh_calls = (repo / "ssh.log").read_text().splitlines()
    assert "-T git@forge" in ssh_calls
    assert sum("git-receive-pack" in call for call in ssh_calls) == 2
    assert any("git-upload-pack" in call for call in ssh_calls)
    assert "--disable-sandbox" in (repo / "muse.args.1").read_text().splitlines()
    assert len(_calls(repo, "finish")) == 1


# --- which model carries which repository (#1301) ------------------------


def _model_in_argv(repo_dir):
    args = (repo_dir / "muse.args.1").read_text().splitlines()
    assert "--model" in args, args
    return args[args.index("--model") + 1]


@pytest.mark.parametrize("repo,model", [
    ("nateprich-projects/command-center", "muse-spark-1.3-contributor"),
    ("nateprich-projects/The-League", "muse-spark-1.3-contributor"),
    ("nateprich-projects/career-toolset", "muse-spark-1.3"),
    ("nateprich-projects/jeffy-finance-agent", "muse-spark-1.3"),
])
def test_the_production_allowlist_reaches_argv(tmp_path, repo, model):
    """#1570 cleared tiers 1 and 3 for the contributor model on 2026-09-26;
    tier 2 stays on the private model."""
    proc, runner_repo = _stubbed_runner(
        tmp_path, _begin(work={"ref": repo + "#42", "repo": repo}))

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == model


def test_an_allowlisted_repo_reaches_argv_as_the_contributor_model(tmp_path, resolver_clearing):
    """The mechanism outlives the empty list: clear a repository in the
    resolver and the runner must carry that answer, not its fallback."""
    repo = "nateprich-projects/The-League"
    proc, runner_repo = _stubbed_runner(
        tmp_path, _begin(work={"ref": repo + "#42", "repo": repo}),
        muse_model_body=resolver_clearing("The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == "muse-spark-1.3-contributor"


@pytest.mark.parametrize("repo", [
    "nateprich-projects/jeffy-finance-agent",
    "nateprich-projects/workbench",
    "nateprich-projects/career-toolset",
])
def test_an_excluded_repo_runs_on_the_private_model(tmp_path, repo,
                                                   resolver_clearing):
    """The three he kept off Discounted Services. If this ever names the
    contributor model, confidential code is going somewhere he declined
    to send it. Run against #1299's allowlist, not the empty one, so the
    exclusion is tested rather than implied."""
    proc, runner_repo = _stubbed_runner(
        tmp_path, _begin(work={"ref": repo + "#42", "repo": repo}),
        muse_model_body=resolver_clearing("command-center", "FF-Weekly-Start-Sit", "The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == "muse-spark-1.3"


def test_a_repo_nobody_has_named_runs_on_the_private_model(tmp_path):
    proc, runner_repo = _stubbed_runner(tmp_path, _begin())

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == "muse-spark-1.3"


def test_a_broken_resolver_still_names_the_private_model(tmp_path):
    """An argv with no `--model` is the exact failure this change exists
    to prevent, so a resolver that cannot answer must not be able to
    produce one."""
    proc, runner_repo = _stubbed_runner(
        tmp_path,
        _begin(work={"ref": "nateprich-projects/The-League#42",
                     "repo": "nateprich-projects/The-League"}),
        muse_model_body="raise SystemExit('resolver is broken')\n")

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == "muse-spark-1.3"


def test_a_resolver_printing_junk_still_names_the_private_model(tmp_path):
    proc, runner_repo = _stubbed_runner(
        tmp_path,
        _begin(work={"ref": "nateprich-projects/The-League#42",
                     "repo": "nateprich-projects/The-League"}),
        muse_model_body="print('not-a-model-at-all')\n")

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == "muse-spark-1.3"


def test_a_resolver_printing_nothing_still_names_the_private_model(tmp_path):
    proc, runner_repo = _stubbed_runner(
        tmp_path,
        _begin(work={"ref": "nateprich-projects/The-League#42",
                     "repo": "nateprich-projects/The-League"}),
        muse_model_body="pass\n")

    assert proc.returncode == 0, proc.stderr
    assert _model_in_argv(runner_repo) == "muse-spark-1.3"


def test_the_runner_never_writes_a_model_id_except_the_fallback():
    """The allowlist lives in muse_model.py. A second copy here would be
    the drift that sends confidential code to a training tier."""
    body = "\n".join(
        line for line in SCRIPT.read_text().splitlines()
        if not line.strip().startswith("#"))
    assert '--model "$MUSE_MODEL"' in body
    assert "--model muse-spark-1.3" not in body
    assert "muse_model.py" in body
    # The exposure rule is which repository gets which model, and none of
    # it is written here. A runner naming a repository would be the copy
    # that drifts.
    # (`command-center` is not in the list: it is this repo, and its name
    # is structural here — log paths, the run checkout — not a routing
    # decision.)
    for repo in ("FF-Weekly-Start-Sit", "The-League",
                 "jeffy-finance-agent", "workbench", "career-toolset"):
        assert repo not in body, repo
    # One model id: the fail-closed anchor. The set of *valid* ids is not
    # written here either — it is read back from muse_model.py, so a
    # provider version bump cannot be rejected by a stale copy.
    assert body.count("muse-spark-1.3") == 1
    assert 'MUSE_FALLBACK_MODEL="muse-spark-1.3"' in body
    assert 'muse_model.py" models' in body
