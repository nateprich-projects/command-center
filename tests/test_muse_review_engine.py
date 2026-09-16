"""scripts/muse-review-engine judges one job against its packet, then stops.

Phases 1 and 2 of #794: the runner executes the protocol and the model
answers one question. begin offers the work, the packet command assembles
the evidence, and a failing review precheck is applied as rejected with no
model call at all. Otherwise the model sees the job's judgement prompt with
the packet inline and no tools, its one JSON answer is validated, retried
once on a parse error, and handed to the apply command. --shadow records
the answer on the PR or issue and the finish note instead of applying: the
shadow period before the cutover.

The harness below stubs the funnel, heartbeat, packet, apply, gh, and muse
binaries; the routine text is the real files, so the prompt-substitution
and word-count tests pin the artifacts that ship.
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
ROUTINE_BREAKDOWN = ROOT / "routines" / "muse-breakdown.md"
ROUTINE_SHAPE = ROOT / "routines" / "muse-shape.md"

REPO = "owner/repo"
PR = 7
HEAD = "abc123def456"
BREAKDOWN_REF = "owner/repo#1"
SHAPE_NUM = 5
SHAPE_REF = "owner/repo#5"


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


def _issue_begin(job):
    ref = BREAKDOWN_REF if job == "breakdown" else SHAPE_REF
    return {"agent": "muse", "run": "engine-run", "gate": "ok",
            "do": job, "work": {"ref": ref, "title": "The {}".format(job)}}


def _issue_packet(job):
    if job == "breakdown":
        return {
            "project": {"ref": BREAKDOWN_REF, "number": 1,
                        "title": "The plan",
                        "body": "# Plan\n\nDeliver the thing end to end."},
            "siblings": [{"ref": "owner/repo#2", "title": "First slice"}],
            "sizing_standard": "One ticket is one run ending in one "
                               "pull request, holding one concern.",
            "collected_at": "2026-09-14T00:00:00+00:00",
        }
    return {
        "idea": {"ref": SHAPE_REF, "number": SHAPE_NUM, "title": "The idea",
                 "body": "Captured note: rotate the api-key monthly."},
        "origin": {"voice": "agent", "override_target": None},
        "plan_md": "# Plan\n\nThe repo's written rules.",
        "plan_md_missing": False,
        "agents_md": "# AGENTS\n\nThe rule book.",
        "agents_md_missing": False,
        "sibling_plans": [{"ref": "owner/repo#9", "title": "A sibling plan"}],
        "collected_at": "2026-09-14T00:00:00+00:00",
    }


def _issue_ticket(title, **overrides):
    ticket = {"title": title, "body": "What: {}.\n\nAccept: it is done.".format(title),
              "risk": "standard", "depends_on": [], "needs": "none"}
    ticket.update(overrides)
    return ticket


def _breakdown_answer(**overrides):
    data = {"tickets": [_issue_ticket("First slice"),
                        _issue_ticket("Second slice")],
            "needs_decision": None}
    data.update(overrides)
    return json.dumps(data)


def _shape_answer(**overrides):
    data = {
        "decided_from_precedent": [
            {"claim": "keys rotate monthly", "source": "plan.md policy"},
        ],
        "decided_by_agent": [
            {"decision": "rotate on the first",
             "alternative": "rotate on demand",
             "why": "a fixed date is auditable"},
        ],
        "needs_nate": {"exposure": None, "gates": None, "scope": None,
                       "preference": None},
        "proposed_class": "Improve",
        "plan_markdown": "# Plan\n\nRotate the api-key monthly.\n",
    }
    data.update(overrides)
    return json.dumps(data)


def _issue_answer(job, **overrides):
    if job == "breakdown":
        return _breakdown_answer(**overrides)
    return _shape_answer(**overrides)


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


def _issue_packet_stub(noun):
    """The packet stub for an issue job: the same read-only shape as the
    review one, with a failure message naming the job's packet."""
    return (
        "import os, pathlib, sys\n"
        "root = pathlib.Path(__file__).parent\n"
        "with (root / 'packet.calls').open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if os.environ.get('PACKET_STATUS', '0') != '0':\n"
        "    sys.stderr.write('could not read the " + noun + " packet for {}\\n'.format(sys.argv[1]))\n"
        "    raise SystemExit(1)\n"
        "sys.stdout.write((root / 'packet.json').read_text())\n"
    )


# Emulates the breakdown-apply contract the runner depends on: the
# --attempt retry protocol (exit 3 below attempt 2, exit 1 at it),
# --validate-only printing the normalized answer, and the live report with
# the created refs or the needs-decision question. A real apply drops an
# APPLIED marker file, so the shadow tests can prove nothing was applied by
# its absence.
BREAKDOWN_APPLY_STUB = (
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
    "raw = pathlib.Path(flag('--answer')).read_text()\n"
    "(root / 'apply.answer').write_text(raw)\n"
    "def malformed(reason):\n"
    "    sys.stderr.write('breakdown-apply: malformed answer (attempt {}): {}\\n'.format(attempt, reason))\n"
    "    raise SystemExit(3 if attempt < 2 else 1)\n"
    "try:\n"
    "    data = json.loads(raw)\n"
    "except ValueError as exc:\n"
    "    malformed('invalid JSON: {}'.format(exc))\n"
    "if not isinstance(data, dict):\n"
    "    malformed('the answer must be one JSON object')\n"
    "tickets = data.get('tickets')\n"
    "question = data.get('needs_decision')\n"
    "if tickets is None and question is None:\n"
    "    malformed('the answer needs tickets or a needs-decision question, not neither')\n"
    "if tickets is not None and question is not None:\n"
    "    malformed('the answer needs tickets or a needs-decision question, not both')\n"
    "checked = []\n"
    "if tickets is not None:\n"
    "    if not isinstance(tickets, list) or not tickets:\n"
    "        malformed('tickets must be a non-empty list')\n"
    "    for position, ticket in enumerate(tickets):\n"
    "        if not isinstance(ticket, dict):\n"
    "            malformed('ticket {} is not an object'.format(position))\n"
    "        for key in ('title', 'body'):\n"
    "            if not ticket.get(key) or not isinstance(ticket.get(key), str):\n"
    "                malformed('ticket {} needs a non-empty {}'.format(position, key))\n"
    "        if ticket.get('risk') not in ('standard', 'escalated'):\n"
    "            malformed('ticket {} risk must be standard or escalated'.format(position))\n"
    "        if ticket.get('needs') not in ('none', 'human', 'claude-code-environment'):\n"
    "            malformed('ticket {} needs an allowed needs value'.format(position))\n"
    "        if not isinstance(ticket.get('depends_on'), list):\n"
    "            malformed('ticket {} depends_on must be a list'.format(position))\n"
    "        checked.append({key: ticket[key] for key in ('title', 'body', 'risk', 'needs', 'depends_on')})\n"
    "if validate_only:\n"
    "    print(json.dumps({'tickets': checked, 'needs_decision': question}, sort_keys=True))\n"
    "    raise SystemExit(0)\n"
    "if os.environ.get('APPLY_REFUSE', ''):\n"
    "    sys.stderr.write('breakdown-apply: project {} is closed; nothing to break down\\n'.format(args[0]))\n"
    "    raise SystemExit(1)\n"
    "(root / 'applied.marker').write_text('applied')\n"
    "if tickets is None:\n"
    "    print(json.dumps({'project': args[0], 'needs_decision': question}, sort_keys=True))\n"
    "else:\n"
    "    created = [{'ref': 'owner/repo#{}'.format(101 + position)} for position in range(len(checked))]\n"
    "    print(json.dumps({'project': args[0], 'created': created}, sort_keys=True))\n"
)

# Emulates the shape-apply contract: the same --attempt protocol, a
# --validate-only report of the status the live path would record, and the
# live report with the ref-to-status line and the advanced/held line. Open
# Nate questions hold the idea at Shaped; a clean answer advances to Ready.
SHAPE_APPLY_STUB = (
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
    "raw = pathlib.Path(flag('--answer')).read_text()\n"
    "(root / 'apply.answer').write_text(raw)\n"
    "def malformed(reason):\n"
    "    sys.stderr.write('shape-apply: malformed answer (attempt {}): {}\\n'.format(attempt, reason))\n"
    "    raise SystemExit(3 if attempt < 2 else 1)\n"
    "try:\n"
    "    data = json.loads(raw)\n"
    "except ValueError as exc:\n"
    "    malformed('invalid JSON: {}'.format(exc))\n"
    "if not isinstance(data, dict):\n"
    "    malformed('the answer must be one JSON object')\n"
    "for key in ('decided_from_precedent', 'decided_by_agent', 'needs_nate', 'proposed_class', 'plan_markdown'):\n"
    "    if key not in data:\n"
    "        malformed('the answer needs {}'.format(key))\n"
    "needs = data.get('needs_nate')\n"
    "if not isinstance(needs, dict) or set(needs) != {'exposure', 'gates', 'scope', 'preference'}:\n"
    "    malformed('needs_nate must hold the four Nate questions')\n"
    "if data.get('proposed_class') not in ('Investigate', 'Broken', 'Maintenance', 'Improve', 'New', 'Replace'):\n"
    "    malformed('proposed_class must name one ladder class')\n"
    "if not data.get('plan_markdown') or not isinstance(data.get('plan_markdown'), str):\n"
    "    malformed('plan_markdown must be non-empty')\n"
    "asked = [name for name in ('exposure', 'gates', 'scope', 'preference') if needs[name]]\n"
    "if asked:\n"
    "    status, reason = 'Shaped', 'open questions for Nate: {}'.format(', '.join(asked))\n"
    "else:\n"
    "    status, reason = 'Ready', 'self-approved: agent idea, finite class, no open questions'\n"
    "if validate_only:\n"
    "    print(json.dumps({'status': status, 'reason': reason, 'answer': data}, sort_keys=True))\n"
    "    raise SystemExit(0)\n"
    "if os.environ.get('APPLY_REFUSE', ''):\n"
    "    sys.stderr.write('shape-apply: idea {} is not in the Project\\n'.format(args[0]))\n"
    "    raise SystemExit(1)\n"
    "(root / 'applied.marker').write_text('applied')\n"
    "ref = '{}#{}'.format(flag('--repo'), args[0])\n"
    "print('{0} \\u2192 {1}\\nhttps://github.com/{2}/issues/{3}'.format(ref, status, flag('--repo'), args[0]))\n"
    "if status == 'Ready':\n"
    "    print('advanced to Ready: {}'.format(reason))\n"
    "else:\n"
    "    print('held at Shaped: {}'.format(reason))\n"
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
    # exist_ok: the flag-rejection test drives the runner four times in one
    # directory; every file below is overwritten per call.
    (repo / "routines").mkdir(parents=True, exist_ok=True)
    (repo / "routines" / "muse-review.md").write_text(
        routine_body if routine_body is not None else ROUTINE.read_text()
    )
    # The engine checks every prompt before begin, so the stub repo carries
    # all three real routines: a run must never take work it cannot ask
    # about, whatever the job turns out to be.
    (repo / "routines" / "muse-breakdown.md").write_text(
        ROUTINE_BREAKDOWN.read_text()
    )
    (repo / "routines" / "muse-shape.md").write_text(
        ROUTINE_SHAPE.read_text()
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
    (repo / "breakdown-packet").write_text(_issue_packet_stub("breakdown"))
    (repo / "breakdown-apply").write_text(BREAKDOWN_APPLY_STUB)
    (repo / "shape-packet").write_text(_issue_packet_stub("shape"))
    (repo / "shape-apply").write_text(SHAPE_APPLY_STUB)
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


def _non_open_packet():
    return _packet(
        state="CLOSED",
        merged_at="2026-09-16T03:46:42Z",
        precheck={"pass": False,
                  "reasons": [
                      "pr_not_open state=CLOSED merged_at=2026-09-16T03:46:42Z",
                  ]},
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
    assert "--run engine-run" in calls[0]
    assert "--agent muse" in calls[0]
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied == {"verdict": "rejected",
                       "blocking": ["ci: CI not green (state red): tests",
                                    "stop: stop_auto_merging set"],
                       "unsure": []}
    assert (repo / "applied.marker").exists()
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: rejected without a "
        "model call (2 precheck reason(s)) --review-result rejected\n".format(HEAD)
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


def test_a_non_open_precheck_stops_without_a_verdict_or_blocking_note(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), _non_open_packet())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "pr_not_open state=CLOSED merged_at=2026-09-16T03:46:42Z" in heartbeat
    assert "no verdict recorded" in heartbeat
    assert "--review-result" not in heartbeat
    assert not (repo / "gh.log").exists()


def test_a_shadow_non_open_precheck_records_no_verdict(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _non_open_packet(), args=("--shadow",))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    body = (repo / "gh.body").read_text()
    assert "No model was called" in body
    assert "pr_not_open state=CLOSED merged_at=2026-09-16T03:46:42Z" in body
    assert "No verdict was recorded" in body
    heartbeat = _heartbeat(repo)
    assert "no decision" in heartbeat
    assert "--review-result" not in heartbeat


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
    assert "--run engine-run" in calls[0]
    assert "--agent muse" in calls[0]
    assert (repo / "applied.marker").exists()
    assert json.loads((repo / "apply.answer").read_text())["verdict"] == \
        "approved"
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: approved "
        "--review-result approved\n".format(HEAD)
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
        "--note reviewed PR #7 in owner/repo at {}: rejected "
        "--review-result rejected\n".format(HEAD)
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
    # Standard asks begin for breakdown work too (#811); escalated reviews
    # never wait behind it.
    assert "begin --agent muse --tier standard --breakdown --role review" \
        in calls


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
    assert _heartbeat(repo).endswith("--review-result approved\n")


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
    assert "--review-result approved" in heartbeat


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
    assert "--review-result rejected" in heartbeat


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


# -- the issue prompts ---------------------------------------------------------

def test_the_breakdown_prompt_is_judgement_text_under_500_words():
    """#794's Phase 2 bar for the breakdown routine: the sizing and coverage
    rules, the schema, the packet placeholder — and no protocol, because the
    model has no tool to execute one with."""
    body = ROUTINE_BREAKDOWN.read_text()
    assert len(body.split()) < 500
    assert "\n---\n" in body, "the runner splits the prompt on the --- separator"
    prompt = body.split("\n---\n", 1)[1]
    assert prompt.count("PACKET_JSON") == 1
    normalized = " ".join(prompt.split()).lower()
    assert "what tickets does this plan break into" in normalized
    assert "one run ending in one pull request" in normalized
    assert '"tickets"' in prompt
    assert '"needs_decision"' in prompt
    assert '"risk": "standard" | "escalated"' in prompt
    assert '"needs": "none" | "human" | "claude-code-environment"' in prompt
    assert "exactly one json object and nothing else" in normalized
    for protocol in ("funnel.py", "heartbeat.py", "breakdown-apply",
                     "breakdown-packet", "gh issue create", "```bash"):
        assert protocol not in prompt, (
            "judgement text only: {!r} is unreachable without tools".format(
                protocol))


def test_the_shape_prompt_is_judgement_text_under_500_words():
    """#794's Phase 2 bar for the shape routine: the decision-record rules,
    the schema, the packet placeholder — and no protocol."""
    body = ROUTINE_SHAPE.read_text()
    assert len(body.split()) < 500
    assert "\n---\n" in body, "the runner splits the prompt on the --- separator"
    prompt = body.split("\n---\n", 1)[1]
    assert prompt.count("PACKET_JSON") == 1
    normalized = " ".join(prompt.split()).lower()
    assert "what is the plan, what is settled" in normalized
    assert "decided_from_precedent" in prompt
    assert "decided_by_agent" in prompt
    assert '"needs_nate": {"exposure"' in prompt
    for category in ("exposure", "gates", "scope", "preference"):
        assert category in normalized
    assert '"proposed_class"' in prompt
    assert '"plan_markdown"' in prompt
    assert "exactly one json object and nothing else" in normalized
    for protocol in ("funnel.py", "heartbeat.py", "shape-apply",
                     "shape-packet", "gh issue", "```bash"):
        assert protocol not in prompt, (
            "judgement text only: {!r} is unreachable without tools".format(
                protocol))


def test_the_runner_reads_the_issue_routines_at_run_time():
    """The issue prompts are the routine files, not copies: the drift
    surface #52 exists for must not come back in the engine."""
    runner = SCRIPT.read_text()
    assert "routines/muse-breakdown.md" in runner
    assert "routines/muse-shape.md" in runner


# -- live issue jobs: one question, one answer ---------------------------------

def _issue_ref(job):
    return BREAKDOWN_REF if job == "breakdown" else SHAPE_REF


def test_standard_tier_asks_begin_for_breakdown(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path / "std", _issue_begin("breakdown"),
        _issue_packet("breakdown"), args=("standard",),
        answers=(_issue_answer("breakdown"),))

    assert proc.returncode == 0, proc.stderr
    calls = (repo / "funnel.calls").read_text()
    assert "begin --agent muse --tier standard --breakdown --role review" \
        in calls
    assert _heartbeat(repo).endswith(
        "broke down {}: created 2 tickets\n".format(BREAKDOWN_REF))


def test_escalated_tier_reviews_without_breakdown(tmp_path):
    """Breakdown rides the standard tier only, so rare risky reviews never
    wait behind it."""
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=(_answer(),))

    assert proc.returncode == 0, proc.stderr
    assert "--breakdown" not in (repo / "funnel.calls").read_text()


def test_a_breakdown_is_applied_and_finished_done(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        answers=(_issue_answer("breakdown"),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    prompt = (repo / "muse.prompt.1").read_text()
    assert "What tickets does this plan break into" in prompt
    assert "PACKET_JSON" not in prompt
    assert "one run ending in one pull request" in prompt
    assert BREAKDOWN_REF in prompt
    packet_calls = (repo / "packet.calls").read_text().splitlines()
    assert packet_calls == [BREAKDOWN_REF]
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert calls[0].split()[0] == BREAKDOWN_REF
    assert "--attempt 1" in calls[0]
    assert "--run engine-run" in calls[0]
    assert "--agent muse" in calls[0]
    assert "--validate-only" not in calls[0]
    assert (repo / "applied.marker").exists()
    applied = json.loads((repo / "apply.answer").read_text())
    assert [ticket["title"] for ticket in applied["tickets"]] == \
        ["First slice", "Second slice"]
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note broke down {}: created 2 tickets\n".format(BREAKDOWN_REF)
    )


def test_a_shape_is_applied_and_finished_done(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_issue_answer("shape"),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    prompt = (repo / "muse.prompt.1").read_text()
    assert "What is the plan, what is settled" in prompt
    assert "PACKET_JSON" not in prompt
    assert "rotate the api-key monthly" in prompt
    packet_calls = (repo / "packet.calls").read_text().splitlines()
    assert packet_calls == ["{} --repo {}".format(SHAPE_NUM, REPO)]
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert calls[0].split()[:3] == \
        [str(SHAPE_NUM), "--repo", REPO]
    assert "--attempt 1" in calls[0]
    assert "--run engine-run" in calls[0]
    assert "--agent muse" in calls[0]
    assert (repo / "applied.marker").exists()
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note shaped {}: Ready (self-approved: agent idea, finite "
        "class, no open questions)\n".format(SHAPE_REF)
    )


def test_a_breakdown_question_is_asked_not_created(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        answers=(_breakdown_answer(
            tickets=None,
            needs_decision="Which repo owns the schedule?"),))

    assert proc.returncode == 0, proc.stderr
    assert (repo / "applied.marker").exists()
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note broke down {}: asked a needs-decision "
        "question\n".format(BREAKDOWN_REF)
    )


def test_a_shape_with_open_questions_holds_at_shaped(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_shape_answer(needs_nate={
            "exposure": "May this touch credentials?",
            "gates": None, "scope": None, "preference": None}),))

    assert proc.returncode == 0, proc.stderr
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note shaped {}: Shaped (open questions for Nate: "
        "exposure)\n".format(SHAPE_REF)
    )


def test_the_issue_model_call_carries_the_no_tool_shape(tmp_path):
    """The issue jobs call the model with the same tool restrictions as
    review: no shell, no writes, no web tools."""
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        answers=(_issue_answer("breakdown"),))

    assert proc.returncode == 0, proc.stderr
    invoked = (repo / "muse.args.1").read_text().splitlines()
    assert invoked[0] == "exec"
    assert invoked[invoked.index("--model") + 1] == "muse-spark-1.3"
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


@pytest.mark.parametrize("job", ("breakdown", "shape"))
def test_a_malformed_issue_answer_retries_once_with_the_parse_error(
        tmp_path, job):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin(job), _issue_packet(job),
        answers=("{not json", _issue_answer(job)))

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
    assert "--outcome done" in _heartbeat(repo)


@pytest.mark.parametrize("job", ("breakdown", "shape"))
def test_a_malformed_final_issue_answer_records_nothing_and_errors(
        tmp_path, job):
    """A malformed final answer records nothing — the project stays
    awaiting breakdown, the idea keeps its label — so the next run asks
    again."""
    ref = _issue_ref(job)
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin(job), _issue_packet(job),
        answers=("{not json", "still not"))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 2
    assert len(_apply_calls(repo)) == 2
    assert not (repo / "applied.marker").exists()
    assert not (repo / "gh.log").exists(), \
        "a malformed final answer records nothing"
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "{}-apply failed on {}".format(job, ref) in heartbeat
    assert "attempt 2" in heartbeat


@pytest.mark.parametrize("job", ("breakdown", "shape"))
def test_an_issue_packet_failure_finishes_errored_without_a_model_call(
        tmp_path, job):
    ref = _issue_ref(job)
    packet_arg = ref if job == "breakdown" else str(SHAPE_NUM)
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin(job), _issue_packet(job),
        extra_env={"PACKET_STATUS": "1"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome errored "
        "--note {}-packet failed for {}: could not read the {} packet "
        "for {}\n".format(job, ref, job, packet_arg)
    )


@pytest.mark.parametrize("job", ("breakdown", "shape"))
def test_an_invalid_issue_packet_finishes_errored_without_a_model_call(
        tmp_path, job):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin(job), "not json{")

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "printed invalid JSON" in heartbeat


def test_an_unparseable_issue_ref_finishes_errored(tmp_path):
    begin = {"agent": "muse", "run": "engine-run", "gate": "ok",
             "do": "breakdown", "work": {"ref": "not-a-ref"}}
    proc, repo = _stubbed_runner(
        tmp_path, begin, _issue_packet("breakdown"))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _heartbeat(repo) == (
        "finish --agent muse --run engine-run --outcome errored "
        "--note funnel begin returned an unparseable ref 'not-a-ref'\n"
    )


def test_a_refused_breakdown_apply_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        answers=(_issue_answer("breakdown"),),
        extra_env={"APPLY_REFUSE": "1"})

    assert proc.returncode == 1
    assert len(_apply_calls(repo)) == 1, \
        "a refused apply is not a parse error: no retry"
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "breakdown-apply failed on {}".format(BREAKDOWN_REF) in heartbeat
    assert "is closed" in heartbeat


# -- issue shadow: record the answer, create nothing ----------------------------

def test_a_shadow_breakdown_records_its_answer_and_creates_nothing(tmp_path):
    """The #811 accept line: --shadow validates the answer, posts it as a
    comment on the project, and carries it on the finish note — creating
    no issues."""
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        args=("--shadow",), answers=(_issue_answer("breakdown"),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--validate-only" in calls[0]
    assert not (repo / "applied.marker").exists(), \
        "a shadow run must not apply"
    gh_log = (repo / "gh.log").read_text()
    assert "create" not in gh_log, "a shadow run creates no issues"
    assert "issue comment 1 --repo owner/repo --body-file" in gh_log
    assert len(gh_log.strip().splitlines()) == 1
    body = (repo / "gh.body").read_text()
    assert "<!-- command-center-shadow-breakdown -->" in body
    assert "not applied" in body
    assert "engine-run" in body
    assert BREAKDOWN_REF in body
    assert "Decision: **2 tickets**" in body
    assert '"needs_decision": null' in body
    heartbeat = _heartbeat(repo)
    assert heartbeat.startswith(
        "finish --agent muse --run engine-run --outcome done "
        "--note shadow breakdown of {}: 2 tickets; "
        "answer: ".format(BREAKDOWN_REF)
    )
    assert '"needs_decision": null' in heartbeat


def test_a_shadow_shape_records_its_answer_and_applies_nothing(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        args=("--shadow",), answers=(_issue_answer("shape"),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--validate-only" in calls[0]
    assert not (repo / "applied.marker").exists(), \
        "a shadow run must not apply"
    gh_log = (repo / "gh.log").read_text()
    assert "issue comment 5 --repo owner/repo --body-file" in gh_log
    body = (repo / "gh.body").read_text()
    assert "<!-- command-center-shadow-shape -->" in body
    assert "not applied" in body
    assert "engine-run" in body
    assert SHAPE_REF in body
    assert "Decision: **Ready**" in body
    assert '"proposed_class": "Improve"' in body
    heartbeat = _heartbeat(repo)
    assert heartbeat.startswith(
        "finish --agent muse --run engine-run --outcome done "
        "--note shadow shape of {}: Ready; answer: ".format(SHAPE_REF)
    )
    assert '"proposed_class": "Improve"' in heartbeat


def test_a_shadow_breakdown_retry_records_the_second_answer(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        args=("--shadow",),
        answers=("{not json", _issue_answer("breakdown")))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 2
    calls = _apply_calls(repo)
    assert len(calls) == 2
    assert all("--validate-only" in call for call in calls)
    assert not (repo / "applied.marker").exists()
    assert _heartbeat(repo).startswith(
        "finish --agent muse --run engine-run --outcome done "
        "--note shadow breakdown of {}: 2 tickets; "
        "answer: ".format(BREAKDOWN_REF)
    )


def test_a_shadow_breakdown_malformed_final_answer_records_raw_and_errors(
        tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        args=("--shadow",), answers=("{not json", "still not"))

    assert proc.returncode == 1
    assert not (repo / "applied.marker").exists()
    body = (repo / "gh.body").read_text()
    assert "could not be parsed" in body
    assert "still not" in body
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "final answer unparseable" in heartbeat
    assert "still not" in heartbeat


@pytest.mark.parametrize("job", ("breakdown", "shape"))
def test_a_shadow_issue_comment_failure_finishes_errored(tmp_path, job):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin(job), _issue_packet(job),
        args=("--shadow",), answers=(_issue_answer(job),),
        extra_env={"GH_STATUS": "1"})

    assert proc.returncode == 1
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "gh issue comment failed" in heartbeat
    assert "recorded no decision" in heartbeat
