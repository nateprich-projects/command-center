"""scripts/muse-review-engine judges one PR against its packet, then stops.

Phase 1 of #794: the runner executes the protocol and the model answers one
question. begin claims the PR, review-packet assembles the evidence, and a
failing precheck is applied as rejected with no model call at all. Otherwise
the model sees the judgement prompt with the packet inline and no tools, its
one JSON answer is validated, retried once on a parse error, and handed to
review-apply. --shadow records the answer on the PR and the finish note
instead of applying: the shadow period before the cutover.

The harness below stubs the funnel, heartbeat, packet, apply, gh, and muse
binaries; the routine text is the real file, so the prompt-substitution and
word-count tests pin the artifact that ships.
"""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "muse-review-engine"
ROUTINE = ROOT / "routines" / "muse-review.md"

REPO = "owner/repo"
PR = 7
HEAD = "abc123def456"


def _begin(**overrides):
    begin = {
        "agent": "muse",
        "run": "engine-run",
        "gate": "ok",
        "do": "review",
        "work": {"pr": PR, "repo": REPO, "ref": "owner/repo#6",
                 "tier": "escalated"},
    }
    begin.update(overrides)
    return begin


def _packet(**overrides):
    packet = {
        "repo": REPO,
        "pr": PR,
        "head_sha": HEAD,
        "ticket": {"ref": "owner/repo#6", "number": 6,
                   "title": "Do the thing", "body": "Risk: escalated",
                   "parent": {"number": 1}},
        "plan_md": "# Plan\n",
        "plan_md_missing": False,
        "diff": "diff --git a/thing.py b/thing.py\n+print('the thing')\n",
        "changed_files": ["thing.py"],
        "ci": {"state": "green", "checks": []},
        "verdict": None,
        "verdict_head_sha": None,
        "overlap": [],
        "merged_overlap": [],
        "protected": {"touched": [], "rules": [],
                      "resolved_path_spelling": False},
        "stop_auto_merging": {"stop_auto_merging": False},
        "precheck": {"pass": True, "reasons": []},
    }
    packet.update(overrides)
    return packet


def _answer(**overrides):
    data = {"verdict": "approved", "blocking": [], "unsure": []}
    data.update(overrides)
    return json.dumps(data)


FUNNEL_STUB = (
    "import pathlib, sys\n"
    "root = pathlib.Path(__file__).parent\n"
    "command = sys.argv[1] if len(sys.argv) > 1 else ''\n"
    "with (root / 'funnel.calls').open('a') as fh:\n"
    "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    "if command == 'session-server':\n"
    "    print('127.0.0.1:1:stub', flush=True)\n"
    "elif command == 'begin':\n"
    "    print((root / 'begin.json').read_text(), end='')\n"
    "elif command == 'session-stop':\n"
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
    "    sys.stderr.write('could not read PR #{} in {}\\n'.format(sys.argv[1], 'x'))\n"
    "    raise SystemExit(1)\n"
    "sys.stdout.write((root / 'packet.json').read_text())\n"
)

# Emulates the review-apply contract the runner depends on: strict validation
# with exit 3 on a retryable malformed answer, the unsure flip, the
# "recorded <verdict> on PR" report, and the errored-outcome marker on a
# malformed final answer. A real apply drops an APPLIED marker file, so the
# shadow tests can prove nothing was applied by its absence.
APPLY_STUB = (
    "import json, os, pathlib, sys\n"
    "root = pathlib.Path(__file__).parent\n"
    "with (root / 'apply.calls').open('a') as fh:\n"
    "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    "args = sys.argv[1:]\n"
    "def flag(name):\n"
    "    if name in args and args.index(name) + 1 < len(args):\n"
    "        return args[args.index(name) + 1]\n"
    "    return None\n"
    "attempt = int(flag('--attempt') or '1')\n"
    "validate_only = '--validate-only' in args\n"
    "source = flag('--answer')\n"
    "raw = sys.stdin.read() if source == '-' else pathlib.Path(source).read_text()\n"
    "(root / 'apply.answer').write_text(raw)\n"
    "def malformed(reason):\n"
    "    sys.stderr.write('review-apply: malformed answer (attempt {}): {}\\n'.format(attempt, reason))\n"
    "    if validate_only or attempt < 2:\n"
    "        raise SystemExit(3 if attempt < 2 else 1)\n"
    "    sys.stdout.write('review-apply: recorded rejected on PR #{}; run outcome: errored\\n'.format(args[0]))\n"
    "    raise SystemExit(1)\n"
    "try:\n"
    "    answer = json.loads(raw)\n"
    "except ValueError as exc:\n"
    "    malformed('invalid JSON: {}'.format(exc))\n"
    "verdict = answer.get('verdict') if isinstance(answer, dict) else None\n"
    "blocking = answer.get('blocking') if isinstance(answer, dict) else None\n"
    "unsure = answer.get('unsure') if isinstance(answer, dict) else None\n"
    "if verdict not in ('approved', 'rejected'):\n"
    "    malformed('verdict must be approved or rejected')\n"
    "if not isinstance(blocking, list) or any(not isinstance(v, str) for v in blocking):\n"
    "    malformed('blocking must be a list of strings')\n"
    "if not isinstance(unsure, list) or any(not isinstance(v, str) for v in unsure):\n"
    "    malformed('unsure must be a list of strings')\n"
    "if verdict == 'approved' and blocking:\n"
    "    malformed('approved verdict must not carry blocking items')\n"
    "if unsure:\n"
    "    verdict = 'rejected'\n"
    "    blocking = blocking + ['unsure: ' + item for item in unsure]\n"
    "if validate_only:\n"
    "    print(json.dumps({'verdict': verdict, 'blocking': blocking, 'note': None}, sort_keys=True))\n"
    "    raise SystemExit(0)\n"
    "if os.environ.get('APPLY_REFUSE', ''):\n"
    "    sys.stderr.write('review-apply: packet head {} is not the current head deadbeef; re-collect the packet\\n'.format(flag('--head')))\n"
    "    raise SystemExit(1)\n"
    "(root / 'applied.marker').write_text('applied')\n"
    "print('recorded {} on PR #{} against {} in {}'.format(verdict, args[0], flag('--head'), flag('--repo')))\n"
)

MUSE_STUB = (
    "#!/bin/bash\n"
    "count_file=\"$MUSE_COUNT\"\n"
    "n=1\n"
    "if [[ -f \"$count_file\" ]]; then n=$(($(cat \"$count_file\") + 1)); fi\n"
    "printf '%s' \"$n\" > \"$count_file\"\n"
    "printf '%s\\n' \"$@\" > \"$MUSE_ARGS.$n\"\n"
    "prompt_file=\"\"\n"
    "previous=\"\"\n"
    "for argument in \"$@\"; do\n"
    "  if [[ \"$previous\" == '--prompt-file' ]]; then prompt_file=\"$argument\"; fi\n"
    "  previous=\"$argument\"\n"
    "done\n"
    "cp \"$prompt_file\" \"$MUSE_PROMPT.$n\"\n"
    "if [[ -n \"${MUSE_SLEEP:-}\" ]]; then exec sleep \"$MUSE_SLEEP\"; fi\n"
    "varname=\"MUSE_ANSWER_$n\"\n"
    "answer=\"${!varname:-$MUSE_ANSWER}\"\n"
    "printf '%s' \"$answer\"\n"
    "if [[ -n \"${MUSE_STDERR:-}\" ]]; then printf '%s' \"$MUSE_STDERR\" >&2; fi\n"
    "exit \"${MUSE_STATUS:-0}\"\n"
)

GH_STUB = (
    "#!/bin/bash\n"
    "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
    "previous=\"\"\n"
    "for argument in \"$@\"; do\n"
    "  if [[ \"$previous\" == '--body-file' ]]; then\n"
    "    cat \"$argument\" >> \"$GH_BODY\"\n"
    "    printf '\\n---\\n' >> \"$GH_BODY\"\n"
    "  fi\n"
    "  previous=\"$argument\"\n"
    "done\n"
    "exit \"${GH_STATUS:-0}\"\n"
)


def _executable(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _stubbed_runner(tmp_path, begin, packet, *, args=(), answers=(),
                    routine_body=None, bound_seconds=20, extra_env=None,
                    timeout=40):
    """Run the engine against stub funnel/heartbeat/packet/apply/gh/muse."""
    repo = tmp_path / "repo"
    (repo / "routines").mkdir(parents=True)
    (repo / "routines" / "muse-review.md").write_text(
        routine_body if routine_body is not None else ROUTINE.read_text()
    )
    (repo / "begin.json").write_text(json.dumps(begin))
    if isinstance(packet, str):
        (repo / "packet.json").write_text(packet)
    else:
        (repo / "packet.json").write_text(json.dumps(packet))
    (repo / "funnel.py").write_text(FUNNEL_STUB)
    (repo / "heartbeat.py").write_text(HEARTBEAT_STUB)
    (repo / "review-packet").write_text(PACKET_STUB)
    (repo / "review-apply").write_text(APPLY_STUB)
    muse = tmp_path / "muse"
    _executable(muse, MUSE_STUB)
    gh = tmp_path / "gh"
    _executable(gh, GH_STUB)
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        MUSE_REVIEW_ENGINE_REPO=str(repo),
        MUSE_BIN=str(muse),
        GH_BIN=str(gh),
        MUSE_REVIEW_ENGINE_BOUND_SECONDS=str(bound_seconds),
        MUSE_COUNT=str(repo / "muse.count"),
        MUSE_ARGS=str(repo / "muse.args"),
        MUSE_PROMPT=str(repo / "muse.prompt"),
        GH_LOG=str(repo / "gh.log"),
        GH_BODY=str(repo / "gh.body"),
    )
    for index, answer in enumerate(answers, 1):
        env["MUSE_ANSWER_{}".format(index)] = answer
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT)] + list(args),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc, repo


def _heartbeat(repo):
    log = repo / "heartbeat.log"
    return log.read_text() if log.exists() else ""


def _muse_calls(repo):
    count = repo / "muse.count"
    return int(count.read_text()) if count.exists() else 0


def _apply_calls(repo):
    calls = repo / "apply.calls"
    return calls.read_text().splitlines() if calls.exists() else []


# -- the prompt ---------------------------------------------------------------

def test_the_review_prompt_is_judgement_text_under_500_words():
    """#794's Phase 1 bar for the routine file: the question, the schema,
    the packet placeholder — and no protocol, because the model has no tool
    to execute one with."""
    body = ROUTINE.read_text()
    assert len(body.split()) < 500
    assert "\n---\n" in body, "the runner splits the prompt on the --- separator"
    prompt = body.split("\n---\n", 1)[1]
    assert prompt.count("PACKET_JSON") == 1
    normalized = " ".join(prompt.split()).lower()
    assert "does this diff do what the ticket and the plan say" in normalized
    assert "avoid what the plan rejected" in normalized
    assert '"verdict": "approved" | "rejected"' in prompt
    assert "exactly one json object and nothing else" in normalized
    assert "non-empty `unsure` is recorded as rejected" in normalized
    assert "an approval carries no blocking items" in normalized
    for protocol in ("funnel.py", "heartbeat.py", "review-apply",
                     "review-packet", "```bash"):
        assert protocol not in prompt, (
            "judgement text only: {!r} is unreachable without tools".format(
                protocol))


def test_the_runner_reads_the_routine_at_run_time():
    """The prompt is the routine file, not a copy: the drift surface #52
    exists for must not come back in the engine."""
    runner = SCRIPT.read_text()
    assert "routines/muse-review.md" in runner
    assert "PACKET_JSON" in runner


def test_the_runner_uses_the_model_without_the_data_sharing_notice():
    runner = SCRIPT.read_text()
    body = "\n".join(
        line for line in runner.splitlines() if not line.strip().startswith("#"))
    assert "--model muse-spark-1.3" in body
    assert "contributor" not in body


def test_the_runner_disables_every_model_tool():
    """The plan's "impossible" for review: with no shell, no writes, and no
    web tools the model cannot run funnel.py or gh itself."""
    runner = SCRIPT.read_text()
    body = "\n".join(
        line for line in runner.splitlines() if not line.strip().startswith("#"))
    assert "--disable-shell" in body
    assert "--disable-write" in body
    assert "--disable-web-tools" in body
    # No network sandbox flag: no tools remain that need it. No --json: the
    # runner needs the raw answer on stdout, not a JSONL event stream. No
    # approval mode: the on-request default stays, so a future tool outside
    # the three disables could never be silently auto-allowed here.
    assert "--sandbox-network" not in body
    assert "--json" not in body
    assert "--approval-mode" not in body
    # The prompt travels by file: a diff can outgrow the argument limit.
    assert "--prompt-file" in body


def test_the_runner_rejects_a_bad_tier_effort_and_flag(tmp_path):
    proc, _ = _stubbed_runner(tmp_path, _begin(), _packet(), args=("nonsense",))
    assert proc.returncode == 1
    assert "escalated or standard" in proc.stderr

    proc, _ = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("escalated", "minimal"))
    assert proc.returncode == 1
    assert "max or high" in proc.stderr

    proc, _ = _stubbed_runner(tmp_path, _begin(), _packet(), args=("--live",))
    assert proc.returncode == 1
    assert "unknown flag" in proc.stderr

    proc, _ = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("escalated", "max", "extra"))
    assert proc.returncode == 1
    assert "usage:" in proc.stderr


def test_a_missing_routine_refuses_before_begin(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, MUSE_REVIEW_ENGINE_REPO=str(repo),
               HOME=str(tmp_path), TMPDIR=str(tmp_path))
    proc = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 1
    assert "refusing to run without a prompt" in proc.stderr
    assert not (repo / "funnel.calls").exists()


def test_a_routine_without_the_packet_placeholder_is_refused(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        routine_body="# Review\n\n---\n\nJudge this.\n")
    assert proc.returncode == 1
    assert "PACKET_JSON exactly once" in proc.stderr
    assert not (repo / "muse.prompt.1").exists()


# -- opening ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("gate", "outcome"),
    (("over", "skipped-over-pace"),
     ("unknown", "skipped-usage-unknown"),
     ("ok", "nothing-to-do")),
)
def test_a_stop_finishes_without_launching_anything(tmp_path, gate, outcome):
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "stop-run", "gate": gate, "do": "stop"},
        _packet(),
    )

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert not (repo / "packet.calls").exists()
    assert _heartbeat(repo) == (
        "finish --agent muse --run stop-run --outcome {}\n".format(outcome)
    )


def test_an_unexpected_begin_job_finishes_the_started_run(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "unexpected-run", "gate": "ok",
         "do": "ticket", "work": {"ref": "owner/repo#6"}},
        _packet(),
    )

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _heartbeat(repo) == (
        "finish --agent muse --run unexpected-run --outcome errored "
        "--note funnel begin returned unknown job 'ticket'\n"
    )


def test_a_packet_failure_finishes_errored_without_a_model_call(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), extra_env={"PACKET_STATUS": "1"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome errored "
        "--note review-packet failed for PR #7 in owner/repo: "
        "could not read PR #7 in x\n"
    )


def test_an_invalid_packet_finishes_errored_without_a_model_call(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), "not json{")

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "invalid JSON" in heartbeat


# -- failing precheck: rejected with no model call ------------------------------

def _failing_packet():
    return _packet(
        ci={"state": "red", "checks": [{"name": "tests", "conclusion": "FAILURE",
                                        "state": None, "status": None}]},
        precheck={"pass": False,
                  "reasons": ["ci: CI not green (state red): tests",
                              "stop: stop_auto_merging set"]},
    )


def test_a_failing_precheck_applies_rejected_without_calling_muse(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), _failing_packet())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert calls[0].split()[:2] == ["7", "--repo"]
    assert "--answer" in calls[0] and "--attempt 1" in calls[0]
    assert "--ci red" in calls[0]
    assert "--head {}".format(HEAD) in calls[0]
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied == {"verdict": "rejected",
                       "blocking": ["ci: CI not green (state red): tests",
                                    "stop: stop_auto_merging set"],
                       "unsure": []}
    assert (repo / "applied.marker").exists()
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: rejected without a "
        "model call (2 precheck reason(s))\n".format(HEAD)
    )


def test_a_refused_precheck_rejection_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _failing_packet(),
        extra_env={"APPLY_REFUSE": "1"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "review-apply refused the precheck rejection" in heartbeat
    assert "not the current head" in heartbeat


# -- live review: one question, one answer -------------------------------------

def test_an_approval_is_applied_and_finished_done(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=(_answer(),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    prompt = (repo / "muse.prompt.1").read_text()
    assert "Does this diff do what the ticket and the plan say" in prompt
    assert "PACKET_JSON" not in prompt
    assert "print('the thing')" in prompt
    assert '"head_sha": "{}"'.format(HEAD) in prompt
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--attempt 1" in calls[0]
    assert "--ci green" in calls[0]
    assert (repo / "applied.marker").exists()
    assert json.loads((repo / "apply.answer").read_text())["verdict"] == \
        "approved"
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: approved\n".format(HEAD)
    )


def test_a_rejection_records_the_model_blocking_list(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=(_answer(verdict="rejected",
                         blocking=["the diff ignores the plan"]),))

    assert proc.returncode == 0, proc.stderr
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied["blocking"] == ["the diff ignores the plan"]
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: rejected\n".format(HEAD)
    )


def test_the_model_call_carries_the_exact_no_tool_shape(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("standard", "high"),
        answers=(_answer(),))

    assert proc.returncode == 0, proc.stderr
    invoked = (repo / "muse.args.1").read_text().splitlines()
    assert invoked[0] == "exec"
    assert invoked[invoked.index("--model") + 1] == "muse-spark-1.3"
    assert invoked[invoked.index("--reasoning-effort") + 1] == "high"
    assert "--disable-shell" in invoked
    assert "--disable-write" in invoked
    assert "--disable-web-tools" in invoked
    assert invoked[invoked.index("--max-model-steps") + 1] == "60"
    assert "--no-foreign-personal-context" in invoked
    assert "--workspace" in invoked
    assert "--prompt-file" in invoked
    assert "--sandbox-network" not in invoked
    assert "--json" not in invoked
    assert "--approval-mode" not in invoked
    calls = (repo / "funnel.calls").read_text()
    assert "begin --agent muse --tier standard --role review" in calls


def test_a_malformed_first_answer_retries_once_with_the_parse_error(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=("{not json", _answer()))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 2
    retry_prompt = (repo / "muse.prompt.2").read_text()
    assert "Your previous answer could not be parsed" in retry_prompt
    assert "invalid JSON" in retry_prompt
    assert "Reply again with exactly one JSON object" in retry_prompt
    calls = _apply_calls(repo)
    assert len(calls) == 2
    assert "--attempt 1" in calls[0]
    assert "--attempt 2" in calls[1]
    assert (repo / "applied.marker").exists()
    assert _heartbeat(repo).endswith(": approved\n")


def test_a_malformed_final_answer_records_rejected_and_errors(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=("{not json", "still not"))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 2
    assert len(_apply_calls(repo)) == 2
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome errored "
        "--note recorded rejected on PR #7 in owner/repo after a malformed "
        "final answer\n"
    )


def test_a_muse_failure_finishes_errored_without_applying(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=(_answer(),),
        extra_env={"MUSE_STATUS": "1", "MUSE_STDERR": "provider outage"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 1
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "muse exec failed (exit 1)" in heartbeat
    assert "provider outage" in heartbeat


def test_a_moved_head_refusal_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=(_answer(),),
        extra_env={"APPLY_REFUSE": "1"})

    assert proc.returncode == 1
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "review-apply failed on PR #7" in heartbeat
    assert "not the current head" in heartbeat


def test_a_run_past_the_bound_is_killed_and_finished_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=(_answer(),),
        bound_seconds=1, extra_env={"MUSE_SLEEP": "30"}, timeout=60)

    assert proc.returncode == 124
    assert "killing run after 1s" in proc.stderr
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "killed after 0 minutes" in heartbeat
    assert "#392" in heartbeat


# -- shadow: record the answer, apply nothing ----------------------------------

def test_a_shadow_run_applies_nothing_and_records_its_answer(tmp_path):
    """The ticket's accept line: --shadow validates the answer, posts it as
    an agent comment on the PR, and carries it on the finish note — while
    review-apply never runs without --validate-only."""
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("--shadow",),
        answers=(_answer(),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--validate-only" in calls[0]
    assert not (repo / "applied.marker").exists(), \
        "a shadow run must not apply"
    gh_log = (repo / "gh.log").read_text()
    assert "pr comment 7 --repo owner/repo --body-file" in gh_log
    body = (repo / "gh.body").read_text()
    assert "<!-- command-center-shadow-review -->" in body
    assert "not applied" in body
    assert "engine-run" in body
    assert HEAD in body
    assert '"verdict": "approved"' in body
    heartbeat = _heartbeat(repo)
    assert heartbeat.startswith(
        "finish --agent muse --run engine-run --outcome done "
        "--note shadow review of PR #7 in owner/repo at {}: approved; "
        "answer: ".format(HEAD)
    )
    assert '"verdict": "approved"' in heartbeat


def test_shadow_accepts_the_flag_in_any_position(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("standard", "--shadow", "high"),
        answers=(_answer(),))

    assert proc.returncode == 0, proc.stderr
    assert _heartbeat(repo).startswith(
        "finish --agent muse --run engine-run --outcome done "
        "--note shadow review of PR #7"
    )
    invoked = (repo / "muse.args.1").read_text().splitlines()
    assert invoked[invoked.index("--reasoning-effort") + 1] == "high"


def test_a_shadow_precheck_failure_calls_no_model(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _failing_packet(), args=("--shadow",))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    body = (repo / "gh.body").read_text()
    assert "No model was called" in body
    assert "CI not green" in body
    heartbeat = _heartbeat(repo)
    assert "--outcome done" in heartbeat
    assert "shadow review of PR #7 in owner/repo" in heartbeat
    assert "rejected" in heartbeat


def test_an_unsure_answer_is_decided_rejected_in_shadow(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("--shadow",),
        answers=(_answer(unsure=["not sure about the migration"]),))

    assert proc.returncode == 0, proc.stderr
    heartbeat = _heartbeat(repo)
    assert ": rejected; answer: " in heartbeat
    body = (repo / "gh.body").read_text()
    assert "Decision: **rejected**" in body


def test_a_shadow_retry_records_the_second_answer(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("--shadow",),
        answers=("{not json", _answer()))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 2
    calls = _apply_calls(repo)
    assert len(calls) == 2
    assert all("--validate-only" in call for call in calls)
    assert not (repo / "applied.marker").exists()
    assert _heartbeat(repo).startswith(
        "finish --agent muse --run engine-run --outcome done "
        "--note shadow review of PR #7 in owner/repo at {}: approved; "
        "answer: ".format(HEAD)
    )


def test_a_shadow_malformed_final_answer_records_raw_and_errors(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("--shadow",),
        answers=("{not json", "still not"))

    assert proc.returncode == 1
    assert not (repo / "applied.marker").exists()
    body = (repo / "gh.body").read_text()
    assert "could not be parsed" in body
    assert "still not" in body
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "final answer unparseable" in heartbeat
    assert "still not" in heartbeat


def test_a_shadow_comment_failure_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("--shadow",),
        answers=(_answer(),), extra_env={"GH_STATUS": "1"})

    assert proc.returncode == 1
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "gh pr comment failed" in heartbeat
    assert '"verdict": "approved"' in heartbeat
