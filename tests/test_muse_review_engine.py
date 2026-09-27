"""scripts/muse-review-engine judges one job against its packet, then stops.

Phases 1 and 2 of #794: begin offers the work and the packet command
assembles the evidence. A passing review calls one lister and bounded judges
in parallel; the runner validates every chunk and derives the final verdict.
Breakdown uses one model answer; shape on Muse asks a framer, then sibling
checks, deciders and an auditor in parallel, and the runner merges them
(#1599); shape on z.ai uses one model answer. Malformed model output retries
once, and the apply command performs every side effect.

The harness below stubs the funnel, heartbeat, packet, apply, gh, and muse
binaries; the routine text is the real files, so the prompt-substitution
and word-count tests pin the artifacts that ship.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import shlex
import stat
import subprocess
import sys
import uuid

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


def _requirement(status="met", **overrides):
    entry = {"requirement": "do the thing", "status": status,
             "evidence": "thing.py cites the new line"}
    entry.update(overrides)
    return entry


def _answer(**overrides):
    data = {"verdict": "approved", "blocking": [], "unsure": [],
            "requirements": [_requirement()]}
    data.update(overrides)
    return json.dumps(data)


def _requirements_answer(*requirements):
    """The lister's answer (#1241): the canonical requirement list.

    Strings, not the judging schema's per-requirement objects: this call
    enumerates what the diff must do and judges none of it.
    """
    if not requirements:
        requirements = ("thing.py prints the thing the ticket asks for",)
    return json.dumps({"requirements": list(requirements)})


def _judge_answer(*requirements, status="met", evidence="thing.py:1"):
    if not requirements:
        requirements = ("thing.py prints the thing the ticket asks for",)
    return json.dumps({"requirements": [
        {"requirement": requirement, "status": status, "evidence": evidence}
        for requirement in requirements
    ]})


def _review_answers(*answers):
    """One live review's answers: the lister's list, then its judge chunks."""
    return (_requirements_answer(),) + tuple(answers)


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
        "premises": [{"claim": "keys rotate monthly",
                      "evidence": "plan.md:12",
                      "label": "documented"}],
    }
    data.update(overrides)
    return json.dumps(data)


def _issue_answer(job, **overrides):
    if job == "breakdown":
        return _breakdown_answer(**overrides)
    return _shape_answer(**overrides)


def _framer_answer(**overrides):
    """The shape framer's draft (#1599): the plan and its open points."""
    data = {"proposed_class": "Improve",
            "plan_markdown": "# Plan\n\nRotate the api-key monthly.\n",
            "decision_points": ["which day the key rotates"],
            "depends_on": []}
    data.update(overrides)
    return json.dumps(data)


def _muse_issue_answers(job):
    """A Muse issue job's scripted answers. Shape on Muse is split: the
    framer answers call 1, and the stub answers every later part from its
    own packet, so parallel parts need no call order (#1599)."""
    if job == "breakdown":
        return (_breakdown_answer(),)
    return (_framer_answer(),)


FUNNEL_STUB = (
    "import json, os, pathlib, sys\n"
    "CI_SUCCESS_CONCLUSIONS = ('SUCCESS', 'NEUTRAL', 'SKIPPED')\n"
    "CI_PENDING_STATES = ('EXPECTED', 'QUEUED', 'IN_PROGRESS', 'PENDING', 'WAITING')\n"
    # engine/shape.py's validators, which the shape split (#1599) runs.
    "LADDER = ['Investigate', 'Broken', 'Maintenance', 'Improve', 'New', 'Replace']\n"
    "ESCALATION_PATTERNS = dict.fromkeys(('credentials', 'authorisation', 'data-migration', 'destructive', 'concurrency'), '')\n"
    "if __name__ == '__main__':\n"
    "    root = pathlib.Path(__file__).parent\n"
    "    command = sys.argv[1] if len(sys.argv) > 1 else ''\n"
    "    with (root / 'funnel.calls').open('a') as fh:\n"
    "        fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
    "    if command == 'session-server':\n"
    "        print('127.0.0.1:1:stub', flush=True)\n"
    "    elif command == 'begin':\n"
    "        (root / 'begin.session_id').write_text(os.environ.get('MUSE_SESSION_ID', ''))\n"
    "        (root / 'begin.zcode_session_id').write_text(os.environ.get('ZCODE_SESSION_ID', ''))\n"
    "        print((root / 'begin.json').read_text(), end='')\n"
    "        status = int(os.environ.get('FUNNEL_STATUS', '0'))\n"
    "        if status:\n"
    "            begin = json.loads((root / 'begin.json').read_text())\n"
    "            with (root / 'heartbeat.log').open('a') as fh:\n"
    "                fh.write('start --agent {} --run {}\\n'.format(begin['agent'], begin['run']))\n"
    "            raise SystemExit(status)\n"
    "    elif command == 'session-stop':\n"
    "        pass\n"
    "    else:\n"
    "        raise SystemExit('unexpected funnel command: ' + command)\n"
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
# with exit 3 on a retryable malformed answer, the unsure and unmet-
# requirement flips, the "recorded <verdict> on PR" report, and the
# errored-outcome marker on a malformed final answer. A real apply drops
# an APPLIED marker file, so a test can prove whether an apply happened.
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
    "requirements = answer.get('requirements') if isinstance(answer, dict) else None\n"
    "if verdict not in ('approved', 'rejected'):\n"
    "    malformed('verdict must be approved or rejected')\n"
    "if not isinstance(blocking, list) or any(not isinstance(v, str) for v in blocking):\n"
    "    malformed('blocking must be a list of strings')\n"
    "if not isinstance(unsure, list) or any(not isinstance(v, str) for v in unsure):\n"
    "    malformed('unsure must be a list of strings')\n"
    "if not isinstance(requirements, list):\n"
    "    malformed('requirements must be a list')\n"
    "for entry in requirements:\n"
    "    if not isinstance(entry, dict):\n"
    "        malformed('each requirement must be an object')\n"
    "    if entry.get('status') not in ('met', 'unmet', 'unsure'):\n"
    "        malformed('requirement status must be met, unmet, or unsure')\n"
    "    for key in ('requirement', 'evidence'):\n"
    "        if not isinstance(entry.get(key), str) or not entry.get(key).strip():\n"
    "            malformed('requirement {} must be a non-empty string'.format(key))\n"
    "if verdict == 'approved' and blocking:\n"
    "    malformed('approved verdict must not carry blocking items')\n"
    "if verdict == 'approved' and not requirements:\n"
    "    malformed('approved verdict must record at least one requirement')\n"
    "if unsure:\n"
    "    verdict = 'rejected'\n"
    "    blocking = blocking + ['unsure: ' + item for item in unsure]\n"
    "unmet = [e for e in requirements if e.get('status') == 'unmet']\n"
    "req_unsure = [e for e in requirements if e.get('status') == 'unsure']\n"
    "if unmet or req_unsure:\n"
    "    verdict = 'rejected'\n"
    "    blocking = blocking + ['requirement unmet: {} -- {}'.format(e.get('requirement'), e.get('evidence')) for e in unmet]\n"
    "    blocking = blocking + ['requirement unsure: {} -- {}'.format(e.get('requirement'), e.get('evidence')) for e in req_unsure]\n"
    "if validate_only:\n"
    "    print(json.dumps({'verdict': verdict, 'blocking': blocking, 'note': None}, sort_keys=True))\n"
    "    raise SystemExit(0)\n"
    "if os.environ.get('APPLY_REFUSE', ''):\n"
    "    sys.stderr.write('review-apply: packet head {} is not the current head deadbeef; re-collect the packet\\n'.format(flag('--head')))\n"
    "    raise SystemExit(1)\n"
    "if os.environ.get('APPLY_LOCKED', ''):\n"
    "    sys.stdout.write('review-apply: PR #{} in {} was already merged at head {} under the approved verdict from run superseding-run (agent muse); this run recorded no verdict\\n'.format(args[0], flag('--repo'), flag('--head')))\n"
    "    sys.stdout.write('run outcome: skipped-locked\\n')\n"
    "    raise SystemExit(0)\n"
    "(root / 'applied.marker').write_text('applied')\n"
    "print('recorded {} on PR #{} against {} in {}'.format(verdict, args[0], flag('--head'), flag('--repo')))\n"
    "if os.environ.get('APPLY_ALREADY_MERGED'):\n"
    "    sys.stdout.write('review-apply: observed already-merged PR: {\"actor\":\"nate\",\"head\":\"abc123def456\",\"merged_at\":\"2026-09-25T02:43:19Z\",\"pr\":7}\\n')\n"
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
# APPLIED marker file, so a test can prove whether an apply happened.
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
    "for key in ('decided_from_precedent', 'decided_by_agent', 'needs_nate', 'proposed_class', 'plan_markdown', 'premises'):\n"
    "    if key not in data:\n"
    "        malformed('the answer needs {}'.format(key))\n"
    "needs = data.get('needs_nate')\n"
    "if not isinstance(needs, dict) or set(needs) != {'exposure', 'gates', 'scope', 'preference'}:\n"
    "    malformed('needs_nate must hold the four Nate questions')\n"
    "if data.get('proposed_class') not in ('Investigate', 'Broken', 'Maintenance', 'Improve', 'New', 'Replace'):\n"
    "    malformed('proposed_class must name one ladder class')\n"
    "if not data.get('plan_markdown') or not isinstance(data.get('plan_markdown'), str):\n"
    "    malformed('plan_markdown must be non-empty')\n"
    "if os.environ.get('APPLY_RETRYABLE', ''):\n"
    "    malformed('refused on request')\n"
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
    "count_lock=\"$count_file.lock\"\n"
    "while ! mkdir \"$count_lock\" 2>/dev/null; do sleep 0.01; done\n"
    "n=1\n"
    "if [[ -f \"$count_file\" ]]; then n=$(($(cat \"$count_file\") + 1)); fi\n"
    "printf '%s' \"$n\" > \"$count_file\"\n"
    "printf '%s\\n' \"$@\" > \"$MUSE_ARGS.$n\"\n"
    "rmdir \"$count_lock\"\n"
    "prompt_file=\"\"\n"
    "previous=\"\"\n"
    "for argument in \"$@\"; do\n"
    "  if [[ \"$previous\" == '--prompt-file' ]]; then prompt_file=\"$argument\"; fi\n"
    "  previous=\"$argument\"\n"
    "done\n"
    "cp \"$prompt_file\" \"$MUSE_PROMPT.$n\"\n"
    "judge_call=0\n"
    "if grep -q 'This is one judge call in a larger review' \"$prompt_file\"; then judge_call=1; fi\n"
    # #1599: which part of a split shape this call is, from the runner's
    # header; empty for every other call.
    "shape_part=''\n"
    "if grep -q '^This call is the shape framer' \"$prompt_file\"; then shape_part=framer\n"
    "elif grep -q '^This call is one shape sibling check' \"$prompt_file\"; then shape_part=sibling\n"
    "elif grep -q '^This call is one shape decider' \"$prompt_file\"; then shape_part=decider\n"
    "elif grep -q '^This call is the shape auditor' \"$prompt_file\"; then shape_part=auditor\n"
    "fi\n"
    "if [[ -n \"$shape_part\" && \"$shape_part\" == \"${MUSE_SHAPE_FAIL_PART:-}\" ]]; then\n"
    "  printf '%s' \"${MUSE_SHAPE_FAILURE:-shape part unavailable}\" >&2\n"
    "  exit \"${MUSE_SHAPE_FAIL_STATUS:-1}\"\n"
    "fi\n"
    "if [[ -n \"$shape_part\" && \"$shape_part\" == \"${MUSE_SHAPE_SLEEP_PART:-}\" ]]; then\n"
    "  exec sleep 30\n"
    "fi\n"
    "if (( judge_call )) && [[ -n \"${MUSE_JUDGE_FAIL_IF:-}\" ]] \\\n      && grep -Fq -- \"$MUSE_JUDGE_FAIL_IF\" \"$prompt_file\"; then\n"
    "  printf '%s' \"${MUSE_JUDGE_FAILURE:-judge unavailable}\" >&2\n"
    "  exit \"${MUSE_JUDGE_FAIL_STATUS:-1}\"\n"
    "fi\n"
    "if (( judge_call )) && [[ -n \"${MUSE_JUDGE_SLEEP_IF:-}\" ]] \\\n      && grep -Fq -- \"$MUSE_JUDGE_SLEEP_IF\" \"$prompt_file\"; then\n"
    "  exec sleep \"${MUSE_JUDGE_SLEEP_SECONDS:-30}\"\n"
    "fi\n"
    "parallel_call=$judge_call\n"
    "if [[ -n \"$shape_part\" && \"$shape_part\" != framer ]]; then parallel_call=1; fi\n"
    "if (( parallel_call )) && [[ \"${MUSE_JUDGE_BARRIER_COUNT:-0}\" =~ ^[1-9][0-9]*$ ]]; then\n"
    "  touch \"$MUSE_COUNT.judge.started.$n\"\n"
    "  expected=\"$MUSE_JUDGE_BARRIER_COUNT\"\n"
    "  for ((poll = 0; poll < 500; poll++)); do\n"
    "    started=$(find \"${MUSE_COUNT%/*}\" -maxdepth 1 -type f -name \"${MUSE_COUNT##*/}.judge.started.*\" | wc -l | tr -d ' ')\n"
    "    if (( started >= expected )); then break; fi\n"
    "    sleep 0.01\n"
    "  done\n"
    "  if (( started < expected )); then printf 'judge calls were not concurrent' >&2; exit 1; fi\n"
    "fi\n"
    "if (( judge_call )) && [[ -n \"${MUSE_JUDGE_DELAY_IF:-}\" ]] \\\n      && grep -Fq -- \"$MUSE_JUDGE_DELAY_IF\" \"$prompt_file\"; then\n"
    "  sleep \"${MUSE_JUDGE_DELAY_SECONDS:-1}\"\n"
    "fi\n"
    # #1411: the converse probe. A judge that finds another judge in flight
    # leaves an overlap marker; the z.ai path must never leave one.
    "if (( judge_call )) && [[ -n \"${MUSE_JUDGE_EXCLUSIVE:-}\" ]]; then\n"
    "  if mkdir \"$MUSE_COUNT.inflight\" 2>/dev/null; then\n"
    "    sleep 0.3\n"
    "    rmdir \"$MUSE_COUNT.inflight\"\n"
    "  else\n"
    "    touch \"$MUSE_COUNT.overlap\"\n"
    "  fi\n"
    "fi\n"
    # #1240: the only vantage point from inside a live run. The prompt file
    # sits in the run directory, so its parent is that directory; record the
    # directory's mode and contents before the EXIT trap removes it.
    "if [[ -n \"${MUSE_RUNDIR_PROBE:-}\" ]]; then\n"
    "  run_dir=\"${prompt_file%/*}\"\n"
    "  if (( judge_call )) || [[ -n \"$shape_part\" ]]; then run_dir=\"${run_dir%/*}\"; fi\n"
    "  {\n"
    "    printf 'dir %s\\n' \"$run_dir\"\n"
    # `find -perm 700` rather than `stat`: the mode flag is `-f` on BSD and
    # `-c` on GNU, and this suite runs on both a Mac and Linux CI.
    "    if [[ -n \"$(find \"$run_dir\" -maxdepth 0 -perm 700 2>/dev/null)\" ]]\n"
    "    then printf 'owner_only yes\\n'; else printf 'owner_only no\\n'; fi\n"
    "    for entry in \"$run_dir\"/*; do printf 'entry %s\\n' \"${entry##*/}\"; done\n"
    # #1241: what the lister left for the judges, as the judging call sees it.
    "    if [[ -r \"$run_dir/requirements.json\" ]]; then\n"
    "      printf 'requirements %s\\n' \"$(tr '\\n' ' ' < \"$run_dir/requirements.json\")\"\n"
    "    fi\n"
    "  } >> \"$MUSE_RUNDIR_PROBE\"\n"
    "fi\n"
    "dynamic_answer=\"\"\n"
    "if (( judge_call )) && [[ \"${MUSE_DYNAMIC_JUDGES:-0}\" == '1' ]]; then\n"
    "  dynamic_answer=\"$(python3 - \"$prompt_file\" \"${MUSE_JUDGE_UNMET:-}\" \\\n      \"${MUSE_JUDGE_UNSURE:-}\" <<'PY'\n"
    "import json, sys\n"
    "text = open(sys.argv[1]).read()\n"
    "marker = 'The assigned requirements are:\\n```json\\n'\n"
    "start = text.index(marker) + len(marker)\n"
    "end = text.index('\\n```', start)\n"
    "requirements = json.loads(text[start:end])\n"
    "rows = []\n"
    "for requirement in requirements:\n"
    "    status = 'met'\n"
    "    if requirement == sys.argv[2]: status = 'unmet'\n"
    "    if requirement == sys.argv[3]: status = 'unsure'\n"
    "    rows.append({'requirement': requirement, 'status': status,\n"
    "                 'evidence': 'thing.py:1'})\n"
    "print(json.dumps({'requirements': rows}))\n"
    "PY\n"
    "  )\"\n"
    "fi\n"
    # #1599: a shape part with no answer set for its call number answers
    # from its own packet, so parallel parts need no call order.
    "answer_var=\"MUSE_ANSWER_$n\"\n"
    "if [[ -n \"$shape_part\" && -z \"${!answer_var:-}\" ]]; then\n"
    "  dynamic_answer=\"$(python3 - \"$shape_part\" \"$prompt_file\" <<'PY'\n"
    "import json, os, sys\n"
    "part, prompt_path = sys.argv[1:3]\n"
    "text = open(prompt_path).read()\n"
    "retry = 'Your previous answer could not be parsed' in text\n"
    "if part == os.environ.get('MUSE_SHAPE_MALFORMED_PART') or (\n"
    "        part == os.environ.get('MUSE_SHAPE_MALFORMED_ONCE_PART')\n"
    "        and not retry):\n"
    "    print('{not json')\n"
    "    raise SystemExit(0)\n"
    "start = text.rindex('```json\\n') + len('```json\\n')\n"
    "packet = json.loads(text[start:text.index('\\n```', start)])\n"
    "if part == 'framer':\n"
    "    answer = json.loads(os.environ.get('MUSE_SHAPE_FRAMER') or json.dumps({\n"
    "        'proposed_class': 'Improve',\n"
    "        'plan_markdown': '# Plan\\n\\nRotate the api-key monthly.\\n',\n"
    "        'decision_points': ['which day the key rotates'],\n"
    "        'depends_on': []}))\n"
    "elif part == 'sibling':\n"
    "    relations = json.loads(os.environ.get('MUSE_SHAPE_RELATIONS') or '{}')\n"
    "    answer = {'siblings': [\n"
    "        {'ref': row['ref'], 'relation': relations.get(row['ref'], 'independent'),\n"
    "         'why': 'checked ' + row['ref']}\n"
    "        for row in packet['sibling_plans']]}\n"
    "elif part == 'decider':\n"
    "    decided = json.loads(os.environ.get('MUSE_SHAPE_DECISIONS') or '{}')\n"
    "    answer = {'decisions': [\n"
    "        dict({'kind': 'agent', 'decision': 'settle ' + point,\n"
    "              'alternative': 'leave it open', 'why': 'a fixed answer is auditable'}\n"
    "             if point not in decided else decided[point], point=point)\n"
    "        for point in packet['decision_points']]}\n"
    "else:\n"
    "    answer = {'premises': [{'claim': 'keys rotate monthly',\n"
    "                            'evidence': 'plan.md:12', 'label': 'documented'}],\n"
    "              'escalated_risk': []}\n"
    "print(json.dumps(answer))\n"
    "PY\n"
    "  )\"\n"
    "fi\n"
    "if [[ -n \"${MUSE_SLEEP:-}\" ]]; then exec sleep \"$MUSE_SLEEP\"; fi\n"
    "if [[ -n \"$dynamic_answer\" ]]; then\n"
    "  answer=\"$dynamic_answer\"\n"
    "else\n"
    "  varname=\"MUSE_ANSWER_$n\"\n"
    "  answer=\"${!varname:-$MUSE_ANSWER}\"\n"
    "fi\n"
    "json_mode=0\n"
    "for argument in \"$@\"; do [[ \"$argument\" == '--json' ]] && json_mode=1; done\n"
    "if (( json_mode )); then\n"
    "  session_id=\"muse-call-$n\"\n"
    "  previous=\"\"\n"
    "  for argument in \"$@\"; do\n"
    "    if [[ \"$previous\" == '--session-id' ]]; then session_id=\"$argument\"; break; fi\n"
    "    previous=\"$argument\"\n"
    "  done\n"
    "  uncaptured=0\n"
    "  if [[ \"${MUSE_UNCAPTURED_CALL:-}\" == \"$n\" ]]; then uncaptured=1; fi\n"
    "  late=\"${MUSE_CONFIG_EVENT_LATE:-0}\"\n"
    "  python3 - \"$session_id\" \"$answer\" \"$uncaptured\" \"$late\" <<'PY'\n"
    "import json, sys\n"
    "session_id, answer, uncaptured, late = sys.argv[1:]\n"
    "stream = {'kind': 'session'}\n"
    "if uncaptured != '1': stream['id'] = session_id\n"
    "configured = {'record_type': 'event', 'payload_type': 'run.model.configured',\n"
    "              'sequence': 1, 'stream': dict(stream), 'payload': {}}\n"
    "terminal = {'record_type': 'event', 'payload_type': 'run.terminal.completed',\n"
    "            'sequence': 2, 'stream': dict(stream),\n"
    "            'payload': {'terminal': 'completed', 'text': answer}}\n"
    "events = [terminal, configured] if late == '1' else [configured, terminal]\n"
    "for event in events: print(json.dumps(event, separators=(',', ':')))\n"
    "PY\n"
    "else\n"
    "  printf '%s' \"$answer\"\n"
    "fi\n"
    "if [[ -n \"${MUSE_STDERR:-}\" ]]; then printf '%s' \"$MUSE_STDERR\" >&2; fi\n"
    "exit \"${MUSE_STATUS:-0}\"\n"
)

MUSE_CALL_FAULT_STUB = (
    "import os, pathlib, sys\n"
    "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
    "if sys.argv[1] == os.environ.get('MUSE_CALL_FAIL_COMMAND'):\n"
    "    raise SystemExit(1)\n"
    "import muse_call\n"
    "raise SystemExit(muse_call.main(sys.argv[1:]))\n"
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


def _stubbed_runner(tmp_path, begin, packet, *, args=(), answers=(),
                    routine_body=None, bound_seconds=20, extra_env=None,
                    timeout=40, muse_model_body=None, muse_call_body=None):
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
    engine = repo / "engine"
    engine.mkdir(exist_ok=True)
    (engine / "__init__.py").write_text("")
    (engine / "review.py").write_text((ROOT / "engine" / "review.py").read_text())
    (engine / "shape.py").write_text((ROOT / "engine" / "shape.py").read_text())
    (engine / "shape_split.py").write_text(
        (ROOT / "engine" / "shape_split.py").read_text())
    (repo / "heartbeat.py").write_text(HEARTBEAT_STUB)
    (repo / "review-packet").write_text(PACKET_STUB)
    (repo / "review-apply").write_text(APPLY_STUB)
    # The real module, not a stub: the model tests below assert that the
    # runner's argv comes from the real allowlist.
    (repo / "muse_model.py").write_text(
        muse_model_body if muse_model_body is not None
        else (ROOT / "muse_model.py").read_text())
    (repo / "muse_call.py").write_text((ROOT / "muse_call.py").read_text())
    if muse_call_body is not None:
        (repo / "muse_call_fault.py").write_text(muse_call_body)
    (repo / "breakdown-packet").write_text(_issue_packet_stub("breakdown"))
    (repo / "breakdown-apply").write_text(BREAKDOWN_APPLY_STUB)
    (repo / "shape-packet").write_text(_issue_packet_stub("shape"))
    (repo / "shape-apply").write_text(SHAPE_APPLY_STUB)
    muse = tmp_path / "muse"
    _executable(muse, MUSE_STUB)
    # The same answering stub stands in for zai-exec: both take the prompt by
    # --prompt-file and print the raw answer, so one counter orders every
    # model call. A test tells the two apart by argv — Muse's starts `exec`.
    zai = tmp_path / "zai-exec"
    _executable(zai, MUSE_STUB)
    gh = tmp_path / "gh"
    _executable(gh, GH_STUB)
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        MUSE_REVIEW_ENGINE_REPO=str(repo),
        MUSE_BIN=str(muse),
        ZAI_EXEC_BIN=str(zai),
        # Muse's behaviour is what every test above the z.ai section pins, so
        # the harness puts the z.ai cutoff in the past unless a test moves it.
        MUSE_REVIEW_ENGINE_ZAI_UNTIL="0",
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


def _heartbeat_without_muse_call_record(repo):
    output = _heartbeat(repo)
    lines = [
        line.split(" --muse-call-record ", 1)[0]
        if " --muse-call-record " in line else line
        for line in output.splitlines()
    ]
    stripped = "\n".join(lines)
    return stripped + ("\n" if output.endswith("\n") else "")


def _muse_call_record(repo):
    lines = [line for line in _heartbeat(repo).splitlines()
             if " --muse-call-record " in line]
    if not lines:
        return None
    raw = lines[-1].split(" --muse-call-record ", 1)[1]
    return json.loads(raw)


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


def test_the_review_prompt_requires_a_per_requirement_pass():
    """#1187: the model walks each requirement one at a time against the
    diff and records the pass, so plan conformance is enumerated rather
    than holistically read — and twice-met is unmet."""
    prompt = ROUTINE.read_text().split("\n---\n", 1)[1]
    normalized = " ".join(prompt.split()).lower()
    assert "one at a time" in normalized
    assert "twice" in normalized
    assert '"requirements"' in prompt
    assert '"met" | "unmet" | "unsure"' in prompt
    assert "any `unmet` or `unsure` is recorded as rejected" in normalized


def test_the_runner_reads_the_routine_at_run_time():
    """The prompt is the routine file, not a copy: the drift surface #52
    exists for must not come back in the engine."""
    runner = SCRIPT.read_text()
    assert "routines/muse-review.md" in runner
    assert "PACKET_JSON" in runner


def test_the_runner_always_names_a_model_and_never_hardcodes_one():
    """Superseded the flat pin on 2026-09-22 (#1301).

    The runner used to pass `--model muse-spark-1.3` literally, and this
    test asserted the word "contributor" appeared nowhere in it. The flag
    has been resolved per repository since then; #1315 emptied the
    allowlist the same day, but the resolver stays, and the invariant that
    matters is unchanged: `--model` is always in argv, and its value is
    never written into this file.
    """
    runner = SCRIPT.read_text()
    body = "\n".join(
        line for line in runner.splitlines() if not line.strip().startswith("#"))
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


def test_the_runner_disables_every_model_tool():
    """The plan's "impossible" for review: with no shell, no writes, and no
    web tools the model cannot run funnel.py or gh itself."""
    runner = SCRIPT.read_text()
    body = "\n".join(
        line for line in runner.splitlines() if not line.strip().startswith("#"))
    assert "--disable-shell" in body
    assert "--disable-write" in body
    assert "--disable-web-tools" in body
    # No network sandbox flag: no tools remain that need it. JSON mode is
    # needed to capture each returned session id; the runner extracts the
    # terminal answer before handing it to the existing parser. No approval
    # mode: the on-request default stays for any future enabled tool.
    assert "--sandbox-network" not in body
    assert "--json" in body
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
     ("reserve", "skipped-api-reserve"),
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
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run stop-run --outcome {}\n".format(outcome)
    )


def test_a_stop_with_a_why_records_the_note_and_names_it_on_stderr(tmp_path):
    why = "could not establish ticket branch facts: transient GraphQL response"
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "stop-run", "gate": "error",
         "do": "stop", "why": why},
        _packet(),
    )

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert not (repo / "packet.calls").exists()
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run stop-run --outcome nothing-to-do "
        "--note {}\n".format(why)
    )
    assert "muse-review-engine: begin stopped: {}".format(why) in proc.stderr


def test_a_failed_begin_finishes_its_started_run_with_the_slow_command_note(
        tmp_path):
    why = (
        "funnel: reply-timeout: FUNNEL_SESSION session busy past the 180s "
        "reply budget (slow command: begin)"
    )
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(run="begin-timeout-run", gate="unknown", do="stop", why=why),
        _packet(),
        extra_env={"FUNNEL_STATUS": "2"},
    )

    assert proc.returncode == 2
    assert _muse_calls(repo) == 0
    assert _heartbeat(repo).splitlines() == [
        "start --agent muse --run begin-timeout-run",
        "finish --agent muse --run begin-timeout-run --outcome errored "
        "--note funnel begin failed (exit 2): {}".format(why),
    ]
    assert "funnel begin failed (exit 2)" in proc.stderr


def test_invalid_begin_logs_only_the_first_300_bytes(tmp_path):
    begin = {"payload": "x" * 500}
    raw = json.dumps(begin)
    proc, _repo = _stubbed_runner(tmp_path, begin, _packet())

    assert proc.returncode == 1
    assert (
        "muse-review-engine: begin stdout (first 300 bytes):\n"
        + raw[:300]
        + "\n"
    ) in proc.stderr
    assert raw[300:] not in proc.stderr
    assert "muse-review-engine: funnel begin returned invalid JSON" in proc.stderr


def test_an_unexpected_begin_job_finishes_the_started_run(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path,
        {"agent": "muse", "run": "unexpected-run", "gate": "ok",
         "do": "ticket", "work": {"ref": "owner/repo#6"}},
        _packet(),
    )

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run unexpected-run --outcome errored "
        "--note funnel begin returned unknown job 'ticket'\n"
    )


def test_a_packet_failure_finishes_errored_without_a_model_call(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), extra_env={"PACKET_STATUS": "1"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert _heartbeat_without_muse_call_record(repo) == (
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


def _could_not_run_packet():
    return _packet(
        ci={
            "state": "could-not-run",
            "annotation": "Recent account payments have failed",
            "checks": [{
                "name": "tests",
                "conclusion": "FAILURE",
                "status": "COMPLETED",
            }],
        },
        precheck={"pass": True, "reasons": []},
    )


def _covered_verdict_packet():
    return _packet(
        verdict={"verdict": "approved", "ci": "green", "head_sha": HEAD,
                 "blocking": []},
        verdict_head_sha=HEAD,
        precheck={"pass": False, "reasons": [
            "verdict: a verdict already covers head {}".format(HEAD[:12]),
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
                       "unsure": [],
                       "requirements": []}
    assert (repo / "applied.marker").exists()
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: rejected without a "
        "model call (2 precheck reason(s)) --review-result rejected\n".format(HEAD)
    )


@pytest.mark.parametrize("backend", ["muse", "zcode"])
def test_a_covered_verdict_with_another_failing_reason_still_rejects(
        tmp_path, backend):
    packet = _covered_verdict_packet()
    packet["precheck"]["reasons"].append("stop: stop_auto_merging set")
    if backend == "zcode":
        proc, repo = _zai_standard(tmp_path, _begin(), packet)
    else:
        proc, repo = _stubbed_runner(tmp_path, _begin(), packet)

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert len(_apply_calls(repo)) == 1
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied["verdict"] == "rejected"
    assert applied["blocking"] == [
        "verdict: a verdict already covers head {}".format(HEAD[:12]),
        "stop: stop_auto_merging set",
    ]
    assert (repo / "applied.marker").exists()
    assert _heartbeat_without_muse_call_record(repo).endswith(
        "--review-result rejected\n")


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


def test_a_could_not_run_ci_stands_down_without_a_verdict_or_rejection(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), _could_not_run_packet())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "CI could not run: Recent account payments have failed" in heartbeat
    assert "no verdict recorded" in heartbeat
    assert "--review-result" not in heartbeat


# -- passing precheck with a CI re-run outstanding (#1019) ----------------------
#
# The branch is clean but no green run yet covers the merged overlap: the
# runner re-runs CI once and waits for the result. No model call, no
# verdict, and a failing precheck rejects first and never reaches here.

def _rerun_packet():
    return _packet(ci_rerun={"action": "rerun", "run_id": 123,
                             "overlaps": [5]})


def _wait_packet():
    return _packet(ci_rerun={"action": "wait", "run_id": 124,
                             "overlaps": [5]})


def test_a_rerun_packet_reruns_ci_and_waits_without_a_model_call(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), _rerun_packet())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    assert (repo / "gh.log").read_text().strip() == \
        "run rerun 123 --repo owner/repo"
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note review of PR #7 in owner/repo at {} re-ran CI (run 123) "
        "to cover merged overlap (#5); waiting for the result\n".format(HEAD)
    )


def test_a_failed_rerun_finishes_errored_and_records_nothing(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _rerun_packet(), extra_env={"GH_STATUS": "1"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "gh run rerun 123 failed" in heartbeat
    assert "recorded nothing" in heartbeat
    assert "--review-result" not in heartbeat


def test_a_wait_packet_finishes_without_calling_anything(tmp_path):
    proc, repo = _stubbed_runner(tmp_path, _begin(), _wait_packet())

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    assert not (repo / "gh.log").exists()
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note review of PR #7 in owner/repo at {} waits for CI run 124 "
        "covering merged overlap (#5)\n".format(HEAD)
    )


def test_a_failing_precheck_rejects_first_and_never_reruns(tmp_path):
    failing = _failing_packet()
    failing["ci_rerun"] = {"action": "rerun", "run_id": 123, "overlaps": [5]}
    proc, repo = _stubbed_runner(tmp_path, _begin(), failing)

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert len(_apply_calls(repo)) == 1
    assert (repo / "applied.marker").exists()
    assert not (repo / "gh.log").exists()
    assert _heartbeat_without_muse_call_record(repo).endswith("--review-result rejected\n")


def test_an_unknown_rerun_action_finishes_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(ci_rerun={"action": "rebase"}))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "unknown ci_rerun action" in heartbeat


# -- live review: one lister and bounded judges --------------------------------

def test_an_approval_is_applied_and_finished_done(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    # One lister call and one judge call for the default single requirement.
    assert _muse_calls(repo) == 2
    prompt = (repo / "muse.prompt.2").read_text()
    assert "Does this diff do what the ticket and the plan say" in prompt
    assert "This is one judge call in a larger review" in prompt
    assert "the thing the ticket asks for" in prompt
    assert '"verdict"' not in prompt.split("The assigned requirements are:", 1)[0]
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
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: approved "
        "--review-result approved\n".format(HEAD)
    )


def test_a_rejection_records_the_code_derived_blocking_list(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer(
            status="unmet", evidence="the diff omits the plan requirement")))

    assert proc.returncode == 0, proc.stderr
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied["blocking"] == [
        "requirement unmet: thing.py prints the thing the ticket asks for "
        "-- the diff omits the plan requirement"
    ]
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: rejected "
        "--review-result rejected\n".format(HEAD)
    )


def _assigned_requirements(prompt):
    marker = "The assigned requirements are:\n```json\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n```", start)
    return json.loads(prompt[start:end])


def _cached_packet_from_judge_prompt(prompt):
    start = prompt.rindex("```json\n") + len("```json\n")
    end = prompt.index("\n```", start)
    return prompt[start:end]


def test_seven_requirements_reach_three_parallel_max_judges_once_each(tmp_path):
    """A late judge and late events still produce the full call-id list."""
    requirements = ["requirement {}".format(i) for i in range(1, 8)]
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=(_requirements_answer(*requirements),),
        extra_env={"MUSE_DYNAMIC_JUDGES": "1",
                   "MUSE_JUDGE_BARRIER_COUNT": "3",
                   "MUSE_JUDGE_DELAY_IF": "requirement 1",
                   "MUSE_JUDGE_DELAY_SECONDS": "1",
                   "MUSE_CONFIG_EVENT_LATE": "1"})

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 4
    prompts = [(repo / "muse.prompt.{}".format(call)).read_text()
               for call in (2, 3, 4)]
    chunks = [_assigned_requirements(prompt) for prompt in prompts]
    assert [len(chunk) for chunk in chunks] == [3, 3, 1]
    assert [item for chunk in chunks for item in chunk] == requirements
    assert len(set(_cached_packet_from_judge_prompt(prompt)
                   for prompt in prompts)) == 1
    session_id = (repo / "begin.session_id").read_text()
    call_record = _muse_call_record(repo)
    assert call_record["calls_made"] == 4
    assert call_record["session_ids"][0] == session_id
    assert set(call_record["session_ids"][1:]) == {
        "muse-call-2", "muse-call-3", "muse-call-4",
    }
    lister = (repo / "muse.args.1").read_text().splitlines()
    assert lister[lister.index("--session-id") + 1] == session_id
    for call in (2, 3, 4):
        args = (repo / "muse.args.{}".format(call)).read_text().splitlines()
        assert args[args.index("--reasoning-effort") + 1] == "max"
        # Three judges at once on one id is what Muse refuses as "already
        # in use"; each became `unsure` and a false rejection (#1413).
        assert "--session-id" not in args
    answer = json.loads((repo / "apply.answer").read_text())
    assert answer["verdict"] == "approved"
    assert [entry["requirement"] for entry in answer["requirements"]] == \
        requirements
    assert [entry["status"] for entry in answer["requirements"]] == \
        ["met"] * 7


def test_uncaptured_call_is_kept_in_the_finish_record(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        extra_env={"MUSE_UNCAPTURED_CALL": "2",
                   "MUSE_CONFIG_EVENT_LATE": "1"})

    assert proc.returncode == 0, proc.stderr
    call_record = _muse_call_record(repo)
    assert call_record["calls_made"] == 2
    assert call_record["session_ids"][0] == \
        (repo / "begin.session_id").read_text()
    assert call_record["session_ids"][1] is None


def test_capture_failure_keeps_answer_and_records_uncaptured_call(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        muse_call_body=MUSE_CALL_FAULT_STUB,
        extra_env={
            "MUSE_CALL_BIN": str(tmp_path / "repo" / "muse_call_fault.py"),
            "MUSE_CALL_FAIL_COMMAND": "capture",
        })

    assert proc.returncode == 0, proc.stderr
    assert (repo / "applied.marker").exists()
    assert json.loads((repo / "apply.answer").read_text())["verdict"] == \
        "approved"
    call_record = _muse_call_record(repo)
    assert call_record["calls_made"] == 2
    assert call_record["session_ids"][0] == \
        (repo / "begin.session_id").read_text()
    assert call_record["session_ids"][1] is None


def test_missing_capture_helper_does_not_block_a_successful_review(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        extra_env={
            "MUSE_CALL_BIN": str(tmp_path / "repo" / "missing-muse-call.py"),
        })

    assert proc.returncode == 0, proc.stderr
    assert (repo / "applied.marker").exists()
    assert json.loads((repo / "apply.answer").read_text())["verdict"] == \
        "approved"
    assert "finish --agent muse --run engine-run --outcome done" in \
        _heartbeat(repo)
    assert "--muse-call-record" not in _heartbeat(repo)
    assert "continuing without Muse session capture" in proc.stderr


def test_summary_failure_does_not_prevent_successful_finish(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        muse_call_body=MUSE_CALL_FAULT_STUB,
        extra_env={
            "MUSE_CALL_BIN": str(tmp_path / "repo" / "muse_call_fault.py"),
            "MUSE_CALL_FAIL_COMMAND": "summarize",
        })

    assert proc.returncode == 0, proc.stderr
    assert (repo / "applied.marker").exists()
    assert "finish --agent muse --run engine-run --outcome done" in \
        _heartbeat(repo)
    assert "--muse-call-record" not in _heartbeat(repo)
    assert "finishing without them" in proc.stderr


def test_summary_failure_does_not_prevent_errored_finish(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        muse_call_body=MUSE_CALL_FAULT_STUB,
        extra_env={
            "MUSE_CALL_BIN": str(tmp_path / "repo" / "muse_call_fault.py"),
            "MUSE_CALL_FAIL_COMMAND": "summarize",
            "MUSE_STATUS": "1",
            "MUSE_STDERR": "provider outage",
        })

    assert proc.returncode == 1
    assert _muse_calls(repo) == 1
    assert not (repo / "applied.marker").exists()
    assert "finish --agent muse --run engine-run --outcome errored" in \
        _heartbeat(repo)
    assert "--muse-call-record" not in _heartbeat(repo)
    assert "finishing without them" in proc.stderr


def test_later_capture_slot_allocation_failure_keeps_run_and_capture_count(
        tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp
    mktemp = bin_dir / "mktemp"
    _executable(
        mktemp,
        "#!/bin/bash\n"
        "case \"$*\" in\n"
        "  *muse-call-later*) exit 1 ;;\n"
        "  *) exec {} \"$@\" ;;\n"
        "esac\n".format(shlex.quote(real_mktemp)),
    )
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        extra_env={"PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]})

    assert proc.returncode == 0, proc.stderr
    assert (repo / "applied.marker").exists()
    call_record = _muse_call_record(repo)
    assert call_record["calls_made"] == 2
    assert call_record["session_ids"] == [
        (repo / "begin.session_id").read_text(), "muse-call-2",
    ]
    assert "using a run-local fallback" in proc.stderr


@pytest.mark.parametrize("failure_kind", ["failed", "timed out"])
def test_a_failed_or_timed_out_judge_rejects_its_chunk_without_dropping_it(
        tmp_path, failure_kind):
    requirements = ["requirement {}".format(i) for i in range(1, 5)]
    extra_env = {"MUSE_DYNAMIC_JUDGES": "1"}
    bound_seconds = 20
    if failure_kind == "failed":
        extra_env.update({"MUSE_JUDGE_FAIL_IF": "requirement 4",
                          "MUSE_JUDGE_FAILURE": "provider failure"})
    else:
        extra_env.update({"MUSE_JUDGE_SLEEP_IF": "requirement 4",
                          "MUSE_JUDGE_SLEEP_SECONDS": "30"})
        bound_seconds = 1
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=(_requirements_answer(*requirements),),
        bound_seconds=bound_seconds, extra_env=extra_env, timeout=40)

    assert proc.returncode == 0, proc.stderr
    answer = json.loads((repo / "apply.answer").read_text())
    assert answer["verdict"] == "rejected"
    assert [entry["requirement"] for entry in answer["requirements"]] == \
        requirements
    assert [entry["status"] for entry in answer["requirements"]] == \
        ["met", "met", "met", "unsure"]
    assert "requirement unsure: requirement 4 -- judge call" in \
        answer["blocking"][0]
    if failure_kind == "timed out":
        assert "timed out after 1 seconds" in \
            answer["requirements"][3]["evidence"]
    else:
        assert "provider failure" in answer["requirements"][3]["evidence"]


def test_the_model_call_carries_the_exact_no_tool_shape(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("standard", "high"),
        answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    # Both calls. The lister is a model call like any other, and a flag that
    # reached only the judge would leave the lister reading a private diff
    # with tools in hand.
    assert _muse_calls(repo) == 2
    session_id = (repo / "begin.session_id").read_text()
    assert str(uuid.UUID(session_id)) == session_id
    # The id binds the run through its first call only: Muse refuses a
    # session already in use, so parallel judges sharing it failed (#1413).
    first = (repo / "muse.args.1").read_text().splitlines()
    assert first[first.index("--session-id") + 1] == session_id
    assert "--session-id" not in \
        (repo / "muse.args.2").read_text().splitlines()
    for call in (1, 2):
        invoked = (repo / "muse.args.{}".format(call)).read_text().splitlines()
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
        assert "--json" in invoked
        assert "--approval-mode" not in invoked
    calls = (repo / "funnel.calls").read_text()
    # Standard asks begin for breakdown work too (#811); escalated reviews
    # never wait behind it.
    assert "begin --agent muse --tier standard --breakdown --role review" \
        in calls


@pytest.mark.parametrize("mode", ("failure", "empty"))
def test_session_id_setup_failure_does_not_gate_review(tmp_path, mode):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        extra_env=_python_without_session_id(tmp_path, mode),
    )

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 2
    assert (repo / "begin.session_id").read_text() == ""
    for call in (1, 2):
        invoked = (repo / "muse.args.{}".format(call)).read_text().splitlines()
        assert "--session-id" not in invoked
    assert len(_apply_calls(repo)) == 1
    assert "continuing without session usage telemetry" in proc.stderr


def test_a_malformed_first_answer_retries_once_with_the_parse_error(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers("{not json", _judge_answer()))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 3
    # A reused id resumes the session, so a retry on it would carry the
    # failed attempt in its context (#1413): only the lister is bound.
    assert "--session-id" in (repo / "muse.args.1").read_text().splitlines()
    for call in (2, 3):
        assert "--session-id" not in \
            (repo / "muse.args.{}".format(call)).read_text().splitlines()
    retry_prompt = (repo / "muse.prompt.3").read_text()
    assert "Your previous answer could not be parsed" in retry_prompt
    assert "invalid JSON" in retry_prompt
    assert "Reply again with exactly one JSON object" in retry_prompt
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--attempt 1" in calls[0]
    assert (repo / "applied.marker").exists()
    assert _heartbeat_without_muse_call_record(repo).endswith("--review-result approved\n")


def test_an_already_merged_approved_head_finishes_with_the_merge_record(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()),
        extra_env={"APPLY_ALREADY_MERGED": "1"})

    assert proc.returncode == 0, proc.stderr
    heartbeat = _heartbeat(repo)
    assert "--outcome done" in heartbeat
    assert "--review-result approved" in heartbeat
    assert "--merged 7" in heartbeat
    assert "observed already-merged PR" in heartbeat
    assert "abc123def456" in heartbeat
    assert "nate" in heartbeat
    assert "2026-09-25T02:43:19Z" in heartbeat


def test_a_malformed_final_judge_answer_fails_closed(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers("{not json", "still not"))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 3
    assert len(_apply_calls(repo)) == 1
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied["verdict"] == "rejected"
    assert applied["requirements"][0]["status"] == "unsure"
    assert "could not be parsed after two attempts" in \
        applied["requirements"][0]["evidence"]
    assert _heartbeat_without_muse_call_record(repo).endswith("--review-result rejected\n")


def test_a_muse_failure_finishes_errored_without_applying(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()),
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
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()),
        extra_env={"APPLY_REFUSE": "1"})

    assert proc.returncode == 1
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "review-apply failed on PR #7" in heartbeat
    assert "not the current head" in heartbeat


def test_an_approved_merge_by_another_run_finishes_skipped_locked(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()),
        extra_env={"APPLY_LOCKED": "1"})

    assert proc.returncode == 0, proc.stderr
    assert len(_apply_calls(repo)) == 1
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome skipped-locked" in heartbeat
    assert "superseding-run (agent muse)" in heartbeat
    assert "head {}".format(HEAD) in heartbeat
    assert "this run recorded no verdict" in heartbeat
    assert "--review-result" not in heartbeat


def test_a_run_past_the_bound_is_killed_and_finished_errored(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()),
        bound_seconds=1, extra_env={"MUSE_SLEEP": "30"}, timeout=60)

    assert proc.returncode == 124
    assert "killing run after 1s" in proc.stderr
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "killed after 0 minutes" in heartbeat
    assert "#392" in heartbeat


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
    assert ('"premises": [{"claim": ..., "evidence": ..., "label": '
            '"measured" | "documented" | "inferred"}]') in prompt
    assert "omit them from `plan_markdown`" in normalized
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


# -- live issue jobs: breakdown asks one question, shape on Muse several ------

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
    assert _heartbeat_without_muse_call_record(repo).endswith(
        "broke down {}: created 2 tickets --ticket-count 2 "
        "--needs-decision \n".format(BREAKDOWN_REF))


def test_escalated_tier_reviews_without_breakdown(tmp_path):
    """Breakdown rides the standard tier only, so rare risky reviews never
    wait behind it."""
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()))

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
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note broke down {}: created 2 tickets "
        "--ticket-count 2 --needs-decision \n".format(BREAKDOWN_REF)
    )


def test_a_shape_is_applied_and_finished_done(tmp_path):
    """Shape on Muse (#1599): the framer, then one sibling check, one
    decider and the auditor, merged in code and applied once."""
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 4
    call_record = _muse_call_record(repo)
    assert call_record["calls_made"] == 4
    assert call_record["session_ids"][0] == \
        (repo / "begin.session_id").read_text()
    prompt = (repo / "muse.prompt.1").read_text()
    assert prompt.startswith("This call is the shape framer")
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
    applied = json.loads((repo / "apply.answer").read_text())
    assert applied["decided_by_agent"] == [
        {"decision": "settle which day the key rotates",
         "alternative": "leave it open",
         "why": "a fixed answer is auditable"}]
    assert applied["plan_markdown"].endswith(
        "## Siblings checked\n\n- owner/repo#9 (independent): checked "
        "owner/repo#9")
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note shaped {}: Ready (self-approved: agent idea, finite "
        "class, no open questions) --shape-status Ready\n".format(SHAPE_REF)
    )


def test_a_breakdown_question_is_asked_not_created(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("breakdown"), _issue_packet("breakdown"),
        answers=(_breakdown_answer(
            tickets=None,
            needs_decision="Which repo owns the schedule?"),))

    assert proc.returncode == 0, proc.stderr
    assert (repo / "applied.marker").exists()
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note broke down {}: asked a needs-decision "
        "question --ticket-count 0 --needs-decision Which repo owns the "
        "schedule?\n".format(BREAKDOWN_REF)
    )


def test_a_shape_with_open_questions_holds_at_shaped(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),),
        extra_env={"MUSE_SHAPE_DECISIONS": json.dumps({
            "which day the key rotates": {
                "kind": "nate", "category": "exposure",
                "question": "May this touch credentials?"}})})

    assert proc.returncode == 0, proc.stderr
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note shaped {}: Shaped (open questions for Nate: "
        "exposure) --shape-status Shaped\n".format(SHAPE_REF)
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
    assert "--json" in invoked
    assert "--approval-mode" not in invoked


@pytest.mark.parametrize("job", ("breakdown",))
def test_a_malformed_issue_answer_retries_once_with_the_parse_error(
        tmp_path, job):
    """Breakdown's one call. Shape on Muse is split (#1599): its retries
    are pinned per part below, and its one call is z.ai's."""
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin(job), _issue_packet(job),
        answers=("{not json", _issue_answer(job)))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 2
    # The retry must not resume the malformed attempt's session (#1413).
    assert "--session-id" in (repo / "muse.args.1").read_text().splitlines()
    assert "--session-id" not in \
        (repo / "muse.args.2").read_text().splitlines()
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


@pytest.mark.parametrize("job", ("breakdown",))
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
    assert _heartbeat_without_muse_call_record(repo) == (
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
    assert _heartbeat_without_muse_call_record(repo) == (
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


# --- which model carries which repository (#1301) ------------------------


# -- #1599: shape on Muse asks small questions ---------------------------------
# One framer in the foreground, bound to the run's session; then sibling
# checks of at most three siblings, deciders of at most three points and the
# auditor, together and unbound. The runner merges their answers and applies
# once. z.ai keeps shape's one call.

SHAPE_PART_MARKERS = (
    ("This call is the shape framer", "framer"),
    ("This call is one shape sibling check", "sibling"),
    ("This call is one shape decider", "decider"),
    ("This call is the shape auditor", "auditor"),
)


def _shape_packet_with(siblings):
    """The shape packet with ``siblings`` sibling plans, each body marked."""
    packet = _issue_packet("shape")
    packet["sibling_plans"] = [
        {"ref": "owner/repo#{}".format(20 + index),
         "title": "Sibling {}".format(index), "status": "Shaped",
         "klass": "Improve", "body": "SIBLING-BODY-{}".format(index)}
        for index in range(siblings)]
    return packet


def _shape_part_prompts(repo):
    """Every model call's prompt, grouped by the part its header names."""
    parts = {}
    for call in range(1, _muse_calls(repo) + 1):
        prompt = (repo / "muse.prompt.{}".format(call)).read_text()
        kind = next(kind for marker, kind in SHAPE_PART_MARKERS
                    if prompt.startswith(marker))
        parts.setdefault(kind, []).append((call, prompt))
    return parts


def _part_packet(prompt):
    """The part packet the runner put where PACKET_JSON stood."""
    start = prompt.rindex("```json\n") + len("```json\n")
    return json.loads(prompt[start:prompt.index("\n```", start)])


def test_a_muse_shape_asks_the_framer_then_every_part_at_once(tmp_path):
    refs = ["owner/repo#{}".format(20 + index) for index in range(7)]
    points = ["point {}".format(index) for index in range(1, 5)]
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _shape_packet_with(7),
        answers=(_framer_answer(decision_points=points,
                                depends_on=["owner/repo#3"]),),
        extra_env={
            # All six later parts must be in flight at once.
            "MUSE_JUDGE_BARRIER_COUNT": "6",
            "MUSE_SHAPE_RELATIONS": json.dumps({
                "owner/repo#21": "depends_on",
                "owner/repo#25": "overlaps"}),
        })

    assert proc.returncode == 0, proc.stderr
    # One framer, ceil(7/3) sibling checks, ceil(4/3) deciders, one auditor.
    assert _muse_calls(repo) == 1 + 3 + 2 + 1
    parts = _shape_part_prompts(repo)
    assert [call for call, _ in parts["framer"]] == [1]
    assert len(parts["sibling"]) == 3
    assert len(parts["decider"]) == 2
    assert len(parts["auditor"]) == 1

    # Only the framer, first and in the foreground, carries the session id:
    # parallel calls on one id are what Muse refuses (#1413).
    session_id = (repo / "begin.session_id").read_text()
    for call, argv in enumerate(_model_argvs(repo), 1):
        assert argv[argv.index("--reasoning-effort") + 1] == "max"
        if call == 1:
            assert argv[argv.index("--session-id") + 1] == session_id
        else:
            assert "--session-id" not in argv

    # The framer reads the sibling index, never a sibling body.
    framer_prompt = parts["framer"][0][1]
    assert "SIBLING-BODY-" not in framer_prompt
    assert "PACKET_JSON" not in framer_prompt
    assert [row["ref"] for row in
            _part_packet(framer_prompt)["sibling_index"]] == refs

    # Each sibling check holds its own siblings' bodies and no other's.
    checked = []
    for _, prompt in parts["sibling"]:
        own = [row["ref"] for row in _part_packet(prompt)["sibling_plans"]]
        assert 1 <= len(own) <= 3
        for index, ref in enumerate(refs):
            assert ("SIBLING-BODY-{}".format(index) in prompt) == \
                (ref in own), (ref, own)
        checked.extend(own)
    assert sorted(checked) == sorted(refs)

    # Every decision point reaches exactly one decider, three at most each.
    decided = []
    for _, prompt in parts["decider"]:
        own = _part_packet(prompt)["decision_points"]
        assert 1 <= len(own) <= 3
        assert "SIBLING-BODY-" not in prompt
        decided.extend(own)
    assert sorted(decided) == points
    auditor_prompt = parts["auditor"][0][1]
    assert "SIBLING-BODY-" not in auditor_prompt
    assert _part_packet(auditor_prompt)["draft"]["decision_points"] == points

    # One apply, of the answer merged in code.
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--attempt 1" in calls[0]
    applied = json.loads((repo / "apply.answer").read_text())
    assert [entry["decision"] for entry in applied["decided_by_agent"]] == \
        ["settle " + point for point in points]
    assert applied["depends_on"] == ["owner/repo#3", "owner/repo#21"]
    assert len(applied["needs_nate"]["scope"]) == 1
    assert applied["needs_nate"]["scope"][0].startswith(
        "owner/repo#25 overlaps this plan")
    assert "## Siblings checked" in applied["plan_markdown"]
    assert applied["premises"] == [{"claim": "keys rotate monthly",
                                    "evidence": "plan.md:12",
                                    "label": "documented"}]
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent muse --run engine-run --outcome done "
        "--note shaped {}: Shaped (open questions for Nate: scope) "
        "--shape-status Shaped\n".format(SHAPE_REF))
    call_record = _muse_call_record(repo)
    assert call_record["calls_made"] == 7
    assert call_record["session_ids"][0] == session_id


@pytest.mark.parametrize("failure_kind", ("failed", "timed out"))
def test_a_failed_or_timed_out_shape_part_applies_nothing(
        tmp_path, failure_kind):
    """A missing part has no safe reading: the run errors, nothing is
    applied, and the idea keeps needs-shaping for the next run."""
    if failure_kind == "failed":
        extra_env = {"MUSE_SHAPE_FAIL_PART": "decider",
                     "MUSE_SHAPE_FAILURE": "provider outage"}
        bound_seconds = 20
    else:
        extra_env = {"MUSE_SHAPE_SLEEP_PART": "sibling"}
        bound_seconds = 1
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),), bound_seconds=bound_seconds,
        extra_env=extra_env, timeout=60)

    assert proc.returncode == 1
    # A failed call is not retried: only a parse error is.
    assert _muse_calls(repo) == 4
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "shape parts failed for {}; nothing applied".format(SHAPE_REF) \
        in heartbeat
    if failure_kind == "failed":
        assert "shape decider.0: muse exec failed (exit 1): provider outage" \
            in heartbeat
    else:
        assert "shape sibling.0 was killed after 0 minutes" in heartbeat
    assert not (tmp_path / ".claude" / "command-center-muse-quota-hold").exists()


def test_a_malformed_shape_part_retries_once_with_the_parse_error(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),),
        extra_env={"MUSE_SHAPE_MALFORMED_ONCE_PART": "decider"})

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 5
    deciders = _shape_part_prompts(repo)["decider"]
    assert len(deciders) == 2
    retry_call, retry_prompt = deciders[1]
    assert "Your previous answer could not be parsed" in retry_prompt
    assert "the decider answer is not valid JSON" in retry_prompt
    retry_args = (repo / "muse.args.{}".format(retry_call)).read_text()
    assert "--session-id" not in retry_args.splitlines()
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert (repo / "applied.marker").exists()


def test_a_shape_part_unparseable_twice_applies_nothing(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),),
        extra_env={"MUSE_SHAPE_MALFORMED_PART": "auditor"})

    assert proc.returncode == 1
    assert len(_shape_part_prompts(repo)["auditor"]) == 2
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "shape auditor answer could not be parsed after two attempts: " \
        "the auditor answer is not valid JSON" in heartbeat


def test_a_malformed_framer_retries_once_unbound(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=("{not json", _framer_answer()))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 5
    assert [call for call, _ in _shape_part_prompts(repo)["framer"]] == [1, 2]
    assert "--session-id" in (repo / "muse.args.1").read_text().splitlines()
    # The retry must not resume the malformed attempt's session (#1413).
    for call in range(2, 6):
        assert "--session-id" not in \
            (repo / "muse.args.{}".format(call)).read_text().splitlines()
    retry_prompt = (repo / "muse.prompt.2").read_text()
    assert "the framer answer is not valid JSON" in retry_prompt
    assert len(_apply_calls(repo)) == 1
    assert "--outcome done" in _heartbeat(repo)


def test_a_framer_unparseable_twice_asks_no_part(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=("{not json", "still not"))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 2
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "could not frame the shape of {}; nothing applied: shape framer " \
        "answer could not be parsed after two attempts".format(SHAPE_REF) \
        in heartbeat


def test_a_framer_model_failure_finishes_like_the_one_call(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),),
        extra_env={"MUSE_STATUS": "1", "MUSE_STDERR": "provider outage"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 1
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "muse exec failed (exit 1) on shape of {}: provider outage".format(
        SHAPE_REF) in heartbeat


def test_a_sibling_without_a_ref_is_refused_before_any_part(tmp_path):
    packet = _shape_packet_with(2)
    del packet["sibling_plans"][1]["ref"]
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), packet,
        answers=(_framer_answer(),))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 1
    assert _apply_calls(repo) == []
    assert "could not split the shape parts for {}: a sibling plan in the " \
        "packet has no ref".format(SHAPE_REF) in _heartbeat(repo)


def test_a_quota_refusal_in_a_shape_part_parks_every_lane(tmp_path):
    refusal = ("API error 429: Subscription quota exhausted. Your usage "
               "window resets at 2099-01-01T00:00:00Z.")
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),),
        extra_env={"MUSE_SHAPE_FAIL_PART": "auditor",
                   "MUSE_SHAPE_FAILURE": refusal})

    assert proc.returncode == 0, proc.stderr
    hold = tmp_path / ".claude" / "command-center-muse-quota-hold"
    assert hold.read_text().strip() == "2099-01-01T00:00:00Z"
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome skipped-provider-quota" in heartbeat
    assert "--outcome errored" not in heartbeat


def test_a_retryable_shape_apply_refusal_is_final(tmp_path):
    """The answer is built in code, so shape-apply's exit 3 asks no model
    again and applies nothing."""
    proc, repo = _stubbed_runner(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_framer_answer(),), extra_env={"APPLY_RETRYABLE": "1"})

    assert proc.returncode == 1
    assert _muse_calls(repo) == 4
    calls = _apply_calls(repo)
    assert len(calls) == 1
    assert "--attempt 1" in calls[0]
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "shape-apply failed on {}".format(SHAPE_REF) in heartbeat


def test_a_zai_shape_is_still_one_call(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=(_shape_answer(),))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    prompt = (repo / "muse.prompt.1").read_text()
    assert not any(prompt.startswith(marker)
                   for marker, _ in SHAPE_PART_MARKERS)
    assert "What is the plan, what is settled" in prompt
    assert json.loads((repo / "apply.answer").read_text()) == \
        json.loads(_shape_answer())


def test_a_malformed_zai_shape_answer_retries_its_one_call(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _issue_begin("shape"), _issue_packet("shape"),
        answers=("{not json", _shape_answer()))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 2
    retry_prompt = (repo / "muse.prompt.2").read_text()
    assert "Your previous answer could not be parsed" in retry_prompt
    calls = _apply_calls(repo)
    assert len(calls) == 2
    assert "--attempt 1" in calls[0]
    assert "--attempt 2" in calls[1]
    assert (repo / "applied.marker").exists()


def _engine_model(repo):
    args = (repo / "muse.args.1").read_text().splitlines()
    assert "--model" in args, args
    return args[args.index("--model") + 1]


@pytest.mark.parametrize("subject,model", [
    ("nateprich-projects/command-center", "muse-spark-1.3-contributor"),
    ("nateprich-projects/The-League", "muse-spark-1.3-contributor"),
    ("nateprich-projects/career-toolset", "muse-spark-1.3"),
    ("nateprich-projects/jeffy-finance-agent", "muse-spark-1.3"),
])
def test_a_review_carries_the_production_allowlist(tmp_path, subject, model):
    """#1570 cleared tiers 1 and 3 on 2026-09-26; tier 2 is judged on the
    private model."""
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == model


def test_a_review_of_an_allowlisted_repo_carries_the_contributor_model(
        tmp_path, resolver_clearing):
    """The mechanism outlives the empty list: clear a repository in the
    resolver and the engine must carry that answer, not its fallback."""
    subject = "nateprich-projects/The-League"
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers(_judge_answer()),
        muse_model_body=resolver_clearing("The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3-contributor"


@pytest.mark.parametrize("subject", [
    "nateprich-projects/jeffy-finance-agent",
    "nateprich-projects/workbench",
    "nateprich-projects/career-toolset",
])
def test_a_review_of_an_excluded_repo_uses_the_private_model(
        tmp_path, subject, resolver_clearing):
    """A review packet carries the PR's diff and ticket text, so this is
    the lane where a wrong model discloses the most. Run against #1299's
    allowlist, not the empty one, so the exclusion is tested."""
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers(_judge_answer()),
        muse_model_body=resolver_clearing("command-center", "FF-Weekly-Start-Sit", "The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3"


def test_a_review_of_an_unknown_repo_uses_the_private_model(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3"


def test_a_broken_resolver_still_names_the_private_model(tmp_path):
    """An argv with no `--model` is the exact failure this prevents."""
    subject = "nateprich-projects/The-League"
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers(_judge_answer()),
        muse_model_body="raise SystemExit('resolver is broken')\n")

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3"


@pytest.mark.parametrize("job", ["breakdown", "shape"])
def test_an_issue_job_resolves_the_model_from_its_subject_repo(
        tmp_path, job, resolver_clearing):
    """Breakdown and shape carry no PR, so the repository comes from the
    subject ref rather than from the review's work block. Both branches
    must reach `muse exec` with a model named."""
    begin = _issue_begin(job)
    ref = "nateprich-projects/The-League#" + begin["work"]["ref"].split("#")[1]
    begin["work"]["ref"] = ref
    packet = _issue_packet(job)
    key = "project" if job == "breakdown" else "idea"
    if key in packet:
        packet[key]["ref"] = ref

    proc, repo = _stubbed_runner(
        tmp_path, begin, packet, answers=_muse_issue_answers(job),
        muse_model_body=resolver_clearing("The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3-contributor"


@pytest.mark.parametrize("job", ["breakdown", "shape"])
def test_an_issue_job_on_an_excluded_repo_uses_the_private_model(
        tmp_path, job, resolver_clearing):
    begin = _issue_begin(job)
    ref = "nateprich-projects/workbench#" + begin["work"]["ref"].split("#")[1]
    begin["work"]["ref"] = ref
    packet = _issue_packet(job)
    key = "project" if job == "breakdown" else "idea"
    if key in packet:
        packet[key]["ref"] = ref

    proc, repo = _stubbed_runner(
        tmp_path, begin, packet, answers=_muse_issue_answers(job),
        muse_model_body=resolver_clearing("command-center", "FF-Weekly-Start-Sit", "The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3"


def test_a_resolver_printing_junk_still_names_the_private_model(tmp_path):
    """The engine's counterpart to the implement runner's case. A module
    that answers both calls with the same wrong thing passes membership
    against itself; requiring the private model in the published list is
    what turns that back into a fallback."""
    subject = "nateprich-projects/The-League"
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers(_judge_answer()),
        muse_model_body="print('not-a-model-at-all')\n")

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3"


def test_a_resolver_printing_nothing_still_names_the_private_model(tmp_path):
    subject = "nateprich-projects/The-League"
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers(_judge_answer()),
        muse_model_body="pass\n")

    assert proc.returncode == 0, proc.stderr
    assert _engine_model(repo) == "muse-spark-1.3"


def test_the_retry_reuses_the_model_it_resolved(tmp_path, resolver_clearing):
    """The repository does not change between attempts, and re-resolving
    would be one more place for the answer to differ."""
    subject = "nateprich-projects/The-League"
    proc, repo = _stubbed_runner(
        tmp_path,
        _begin(work={"pr": PR, "repo": subject,
                     "ref": subject + "#6", "tier": "escalated"}),
        _packet(repo=subject),
        answers=_review_answers("not json at all", _judge_answer()),
        muse_model_body=resolver_clearing("The-League"))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 3
    for attempt in (1, 2, 3):
        args = (repo / "muse.args.{}".format(attempt)).read_text().splitlines()
        assert args[args.index("--model") + 1] == "muse-spark-1.3-contributor"


# -- #1240: one packet per run, in one owner-only directory --------------------
# The precondition for #1233, which turns the single review call into a lister
# plus several judges. Two properties matter to that split and are pinned here:
# the packet is assembled once and every call reads the same bytes, and the
# bytes live somewhere no other account on the machine can read.


def _probe(tmp_path, repo):
    """What the muse stub saw of the run directory while the run was live."""
    text = (repo / "rundir.probe").read_text()
    # `partition`, not `split`: a probe line the stub could not fill must show
    # up as an empty value in the assertion below, not as a ValueError three
    # frames away from the thing that actually went wrong.
    lines = [line.partition(" ") for line in text.splitlines() if line]
    return {
        "dirs": [tail for key, _, tail in lines if key == "dir"],
        "owner_only": [tail for key, _, tail in lines if key == "owner_only"],
        "entries": sorted({tail for key, _, tail in lines if key == "entry"}),
        "requirements": [tail for key, _, tail in lines
                         if key == "requirements"],
        "raw": text,
    }


def _with_probe(tmp_path, **kwargs):
    repo_probe = tmp_path / "rundir.probe"
    kwargs.setdefault("extra_env", {})
    kwargs["extra_env"] = dict(kwargs["extra_env"],
                               MUSE_RUNDIR_PROBE=str(repo_probe))
    proc, repo = _stubbed_runner(tmp_path, **kwargs)
    if repo_probe.exists():
        (repo / "rundir.probe").write_text(repo_probe.read_text())
    return proc, repo


def test_every_scratch_file_lives_in_one_run_directory(tmp_path):
    proc, repo = _with_probe(
        tmp_path, begin=_begin(), packet=_packet(), answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    probe = _probe(tmp_path, repo)
    # One directory seen by every model call of the run, not one per call:
    # the lister and the judge share the assembled packet (#1240, #1241).
    assert len(set(probe["dirs"])) == 1, probe["raw"]
    run_dir = pathlib.Path(probe["dirs"][0])
    assert run_dir.parent == tmp_path, run_dir
    assert run_dir.name.startswith("muse-review-engine."), run_dir.name
    # The packet among them: a judge in #1233 reads this path, not GitHub.
    assert "packet.json" in probe["entries"]
    assert "prompt.txt" in probe["entries"]
    # And the lister's list, written before the judging call and read by
    # every judge #1242 adds (#1241).
    assert "requirements.json" in probe["entries"]


def test_the_run_directory_is_readable_only_by_its_owner(tmp_path):
    # A world-readable temp root, which is what `/tmp` actually is on this
    # Mac. Without it the assertion passes on any tree, because pytest hands
    # out 700 directories and the old runner inherited that by accident.
    tmp_path.chmod(0o755)
    proc, repo = _with_probe(
        tmp_path, begin=_begin(), packet=_packet(), answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    # Not a tidiness check. The packet holds the diff, ticket and plan of a
    # private member repository, and this repository is public; mktemp's
    # per-file 600 left the name, size and timing of every review legible to
    # any other account on the machine.
    probe = _probe(tmp_path, repo)
    # Every model call of the run, not just the first: the lister writes the
    # requirement list into the same directory the packet is in.
    assert probe["owner_only"] and set(probe["owner_only"]) == {"yes"}, \
        probe["raw"]
    assert pathlib.Path(probe["dirs"][0]) != tmp_path


def test_the_run_directory_and_the_packet_are_gone_when_the_run_ends(tmp_path):
    proc, repo = _with_probe(
        tmp_path, begin=_begin(), packet=_packet(), answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    run_dir = pathlib.Path(_probe(tmp_path, repo)["dirs"][0])
    assert not run_dir.exists(), sorted(p.name for p in run_dir.iterdir())
    # Nothing of the run survives anywhere under the temp root either: the
    # cache lives for one run, because GitHub is the state.
    assert not list(tmp_path.glob("muse-review-engine.*"))


def test_a_killed_model_call_leaves_nothing_behind_either(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), bound_seconds=1,
        extra_env={"MUSE_SLEEP": "30"}, timeout=60)

    assert proc.returncode == 124, proc.stderr
    assert not list(tmp_path.glob("muse-review-engine.*"))


@pytest.mark.parametrize("begin,args", [
    (_begin(), ()),
    (_begin(do="breakdown", work={"ref": BREAKDOWN_REF, "tier": "standard"}),
     ("standard", "high")),
    (_begin(do="shape",
            work={"ref": SHAPE_REF, "number": SHAPE_NUM, "repo": REPO,
                  "tier": "standard"}),
     ("standard", "high")),
])
def test_the_packet_is_fetched_exactly_once_whatever_the_job(
        tmp_path, begin, args):
    answers = (_review_answers(_judge_answer()) if begin["do"] == "review"
               else _muse_issue_answers(begin["do"]))
    proc, repo = _stubbed_runner(
        tmp_path, begin, _packet(), args=args, answers=answers)

    assert proc.returncode == 0, proc.stderr
    calls = (repo / "packet.calls").read_text().splitlines()
    assert len(calls) == 1, calls


def _guard_harness(tmp_path):
    """Run the shipped `assemble_packet` on its own, twice.

    Extracted from the script text rather than re-typed: a guard that a test
    keeps its own copy of stops being the guard that ships. No caller today
    assembles twice — the guard exists for #1233's judges — so this is the
    only way to make the refusal a fact rather than an intention.
    """
    text = SCRIPT.read_text()
    start = text.index("assemble_packet() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    harness = tmp_path / "guard.sh"
    harness.write_text(
        "set -uo pipefail\n"
        'PACKET_FILE="{}/packet.json"\n'.format(tmp_path)
        + "PACKET_ASSEMBLED=0\n"
        + text[start:end]
        + 'fetch() { printf "fetched\\n" >> "$PACKET_FILE"; return "${FETCH_STATUS:-0}"; }\n'
        "assemble_packet fetch; echo \"first=$?\"\n"
        "assemble_packet fetch; echo \"second=$?\"\n"
    )
    return subprocess.run(
        ["/bin/bash", str(harness)], capture_output=True, text=True,
        timeout=20)


def test_a_second_assembly_is_refused_rather_than_silently_refetched(tmp_path):
    proc = _guard_harness(tmp_path)

    assert "first=0" in proc.stdout, proc.stdout
    assert "second=2" in proc.stdout, proc.stdout
    assert "already assembled" in proc.stderr, proc.stderr
    # The refusal is what keeps the bytes identical: a judge that refetched
    # would be judging a head the lister never enumerated.
    assert (tmp_path / "packet.json").read_text() == "fetched\n"


def test_a_failed_fetch_does_not_burn_the_one_assembly(tmp_path):
    text = SCRIPT.read_text()
    start = text.index("assemble_packet() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    harness = tmp_path / "retry.sh"
    harness.write_text(
        "set -uo pipefail\n"
        "PACKET_ASSEMBLED=0\n"
        'PACKET_FILE="{}/packet.json"\n'.format(tmp_path)
        + text[start:end]
        + "attempts=0\n"
        "fetch() { attempts=$((attempts + 1));"
        ' [[ "$attempts" -gt 1 ]]; }\n'
        "assemble_packet fetch; echo \"first=$?\"\n"
        "assemble_packet fetch; echo \"second=$?\"\n"
    )
    proc = subprocess.run(
        ["/bin/bash", str(harness)], capture_output=True, text=True,
        timeout=20)

    # A packet command that failed assembled nothing, so the run may still
    # try. Only a fetch that succeeded closes the door.
    assert "first=1" in proc.stdout, proc.stdout
    assert "second=0" in proc.stdout, proc.stdout
    assert "already assembled" not in proc.stderr


# -- #1241: one lister call emits the review's requirements --------------------
# The first half of #1233's split. One small question enumerates what the diff
# must do and must avoid; #1242's judges each answer a few of them against the
# same cached packet, and none of them derives its own list.


def test_the_lister_asks_for_requirements_before_the_judge_is_asked(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    lister = (repo / "muse.prompt.1").read_text()
    # The framing is generated in the runner and prepended to the routine's
    # own prompt: the routine file is frozen ground while #794 lands.
    assert "This call is not the review" in lister
    assert "judge nothing" in lister.lower()
    assert '{"requirements": [' in lister
    # It still carries the packet, because that is what it enumerates from.
    assert "print('the thing')" in lister
    assert '"head_sha": "{}"'.format(HEAD) in lister
    assert "PACKET_JSON" not in lister

    judge = (repo / "muse.prompt.2").read_text()
    # The lister framing does not survive; this call judges only its assigned
    # requirements, and the runner derives the verdict after all chunks.
    assert "This call is not the review" not in judge
    assert "Does this diff do what the ticket and the plan say" in judge
    assert "posted PR" in judge
    assert "packet's CI section first" in judge
    assert (
        "successful named check in `ci.checks` for the packet's `head_sha` "
        "satisfies" in judge
    )
    assert "only a run-outcome requirement that check directly proves" in judge
    assert (
        "that proved requirement unsure because no Run evidence comment "
        "exists" in judge
    )
    assert "Run evidence:" in judge
    assert "command, exit status, output summary" in judge
    assert "never automatic satisfaction" in judge
    assert "malformed or incomplete blocks stay prose" in judge
    assert "throwaway `launchctl submit` probe" in judge
    assert "Install nothing; leave the keeper unchanged" in judge


def test_the_requirement_list_is_kept_where_the_judges_will_read_it(tmp_path):
    proc, repo = _with_probe(
        tmp_path, begin=_begin(), packet=_packet(),
        answers=(_requirements_answer("  the runner writes the list  ",
                                      "no prompt file is edited"),
                 _judge_answer("the runner writes the list",
                               "no prompt file is edited")))

    assert proc.returncode == 0, proc.stderr
    probe = _probe(tmp_path, repo)
    # One line, from the judging call: the file does not exist yet when the
    # lister itself is called.
    assert len(probe["requirements"]) == 1, probe["raw"]
    # The lister's own strings, whitespace-trimmed, in its own order — not
    # the judging schema's objects, and not re-derived from the packet.
    assert json.loads(probe["requirements"][0]) == [
        "the runner writes the list",
        "no prompt file is edited",
    ]


def test_a_malformed_requirement_list_retries_once_and_then_lists(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(),
        answers=("{not json", _requirements_answer("one requirement"),
                 _judge_answer("one requirement")))

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 3
    retry = (repo / "muse.prompt.2").read_text()
    assert "Your previous answer could not be parsed" in retry
    assert "invalid JSON" in retry
    assert "Reply again with exactly one JSON object" in retry
    # The retry is still the lister's question, not the judge's.
    assert "This call is not the review" in retry
    # And the review still happened, with one apply on the judge's answer.
    assert len(_apply_calls(repo)) == 1
    assert (repo / "applied.marker").exists()
    assert _heartbeat_without_muse_call_record(repo).endswith("--review-result approved\n")


@pytest.mark.parametrize("answer,reason", [
    ("{not json", "invalid JSON"),
    ("", "empty answer"),
    ('["a requirement"]', "must be a JSON object"),
    ('{"verdict": "approved"}', "missing required key 'requirements'"),
    ('{"requirements": "one requirement"}', "must be a list"),
    ('{"requirements": [3]}', "must be a string"),
    ('{"requirements": ["   "]}', "must not be blank"),
    # The judging schema's own shape: entries there are objects carrying a
    # status. A lister that accepted them would be taking a judgement.
    ('{"requirements": [{"requirement": "x", "status": "met"}]}',
     "must be a string"),
    # An empty list is a failure, not an approval: under #1242 every judge
    # would have nothing to answer and the derived verdict would be an
    # approval nobody checked.
    ('{"requirements": []}', "at least one requirement"),
])
def test_a_malformed_requirement_list_twice_fails_the_run(
        tmp_path, answer, reason):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), answers=(answer, answer, _answer()))

    assert proc.returncode == 1
    assert _muse_calls(repo) == 2
    # Nothing is judged and nothing is recorded: no verdict, no merge, and the
    # PR stays in the review queue for the next run rather than being rejected
    # for the model's formatting.
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "could not list the requirements for PR #7 in owner/repo" in \
        heartbeat
    assert reason in heartbeat


def test_a_lister_call_past_the_bound_is_killed_like_any_other(tmp_path):
    """The bound covers the new call too: #1233 exists because `max` goes
    silent, and a lister that hung would hold the escalated queue exactly
    the way the single call did."""
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), bound_seconds=1,
        extra_env={"MUSE_SLEEP": "30"}, timeout=60)

    assert proc.returncode == 124
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert "--outcome errored" in heartbeat
    assert "killed after 0 minutes" in heartbeat
    assert "#392" in heartbeat


# -- the z.ai standard tier (Nate, 2026-09-23) ---------------------------------
#
# Until 2026-10-07 00:00 Beijing time (2026-10-06 09:00 PDT) the standard
# tier is answered by GLM-5.3 through scripts/zai-exec and recorded as agent
# `zcode`, its judges one at a time (#1411); the escalated tier stays on
# Muse, and at the cutoff the standard tier returns to Muse by itself. The
# cutoff is moved by MUSE_REVIEW_ENGINE_ZAI_UNTIL here only.

FUTURE = "9999999999"


def _model_argvs(repo):
    """Every model call's argv, in call order."""
    return [
        (repo / "muse.args.{}".format(call)).read_text().splitlines()
        for call in range(1, _muse_calls(repo) + 1)
    ]


def _zai_standard(tmp_path, begin, packet, **kwargs):
    extra = dict(kwargs.pop("extra_env", None) or {})
    extra.setdefault("MUSE_REVIEW_ENGINE_ZAI_UNTIL", FUTURE)
    return _stubbed_runner(tmp_path, begin, packet, args=("standard", "max"),
                           extra_env=extra, **kwargs)


@pytest.mark.parametrize("backend", ["muse", "zcode"])
def test_a_sole_covered_verdict_stands_down_without_overwriting_approval(
        tmp_path, backend):
    packet = _covered_verdict_packet()
    if backend == "zcode":
        proc, repo = _zai_standard(tmp_path, _begin(), packet)
    else:
        proc, repo = _stubbed_runner(tmp_path, _begin(), packet)

    assert packet["verdict"]["verdict"] == "approved"  # recorded #1509 shape
    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 0
    assert _apply_calls(repo) == []
    assert not (repo / "applied.marker").exists()
    assert not (repo / "gh.log").exists()
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent {} --run engine-run --outcome done "
        "--note review skipped — a verdict already covers head {}; "
        "no verdict recorded\n".format(backend, HEAD)
    )


def test_the_engine_cutoff_is_the_one_heartbeat_retires_zcode_at():
    """One instant, two readers: the runner routes on it and heartbeat stops
    reading zcode's silence as a dying lane on it. A drifted copy would leave
    a lane running unwatched, or a stopped lane alarming."""
    import heartbeat

    runner = SCRIPT.read_text()
    assert 'MUSE_REVIEW_ENGINE_ZAI_UNTIL:-{}}}'.format(
        heartbeat.ZAI_STANDARD_UNTIL) in runner
    assert heartbeat.ZAI_STANDARD_UNTIL == 1791302400


def test_a_standard_review_before_the_cutoff_runs_on_zai_as_zcode(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    calls = (repo / "funnel.calls").read_text()
    assert "begin --agent zcode --tier standard --breakdown --role review" \
        in calls
    assert "--agent muse" not in calls
    # The lister and the judge both went to zai-exec, never to Muse.
    argvs = _model_argvs(repo)
    assert len(argvs) == 2
    for argv in argvs:
        assert argv[0] == "--prompt-file"
        assert "exec" not in argv
        assert "--model" not in argv
        assert "--reasoning-effort" not in argv
        assert "--session-id" not in argv
        timeout = float(argv[argv.index("--timeout") + 1])
        assert 0 < timeout < 20, "zai-exec's own deadline sits inside the bound"
    applies = _apply_calls(repo)
    assert len(applies) == 1 and "--agent zcode" in applies[0]
    assert _heartbeat_without_muse_call_record(repo) == (
        "finish --agent zcode --run engine-run --outcome done "
        "--note reviewed PR #7 in owner/repo at {}: approved "
        "--review-result approved\n".format(HEAD)
    )


def test_the_zai_run_binds_zcodes_session_and_never_muses(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()),
        extra_env={"MUSE_SESSION_ID": "inherited-muse-session"})

    assert proc.returncode == 0, proc.stderr
    assert (repo / "begin.session_id").read_text() == ""
    session = (repo / "begin.zcode_session_id").read_text()
    assert str(uuid.UUID(session)) == session


@pytest.mark.parametrize("job", ("breakdown", "shape"))
def test_a_standard_issue_job_before_the_cutoff_runs_on_zai(tmp_path, job):
    proc, repo = _zai_standard(
        tmp_path, _issue_begin(job), _issue_packet(job),
        answers=(_issue_answer(job),))

    assert proc.returncode == 0, proc.stderr
    assert [argv[0] for argv in _model_argvs(repo)] == ["--prompt-file"]
    assert "--agent zcode" in _apply_calls(repo)[0]
    assert _heartbeat(repo).startswith(
        "finish --agent zcode --run engine-run --outcome done")


def test_the_escalated_tier_stays_on_muse_before_the_cutoff(tmp_path):
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("escalated", "max"),
        answers=_review_answers(_judge_answer()),
        extra_env={"MUSE_REVIEW_ENGINE_ZAI_UNTIL": FUTURE})

    assert proc.returncode == 0, proc.stderr
    assert "begin --agent muse --tier escalated --role review" in \
        (repo / "funnel.calls").read_text()
    assert all(argv[0] == "exec" for argv in _model_argvs(repo))
    assert _heartbeat(repo).startswith("finish --agent muse ")


@pytest.mark.parametrize("cutoff", ("1", "tomorrow", "-5", "1791302400.5"))
def test_the_standard_tier_is_muses_from_the_cutoff_or_on_a_bad_one(
        tmp_path, cutoff):
    """Past the cutoff the standard tier goes back to Muse with nothing to
    undo; a cutoff that is not a plain epoch keeps the unchanged behaviour
    rather than guessing."""
    proc, repo = _stubbed_runner(
        tmp_path, _begin(), _packet(), args=("standard", "max"),
        answers=_review_answers(_judge_answer()),
        extra_env={"MUSE_REVIEW_ENGINE_ZAI_UNTIL": cutoff})

    assert proc.returncode == 0, proc.stderr
    assert "begin --agent muse --tier standard --breakdown --role review" \
        in (repo / "funnel.calls").read_text()
    argvs = _model_argvs(repo)
    assert argvs and all(argv[0] == "exec" for argv in argvs)
    assert all("--model" in argv for argv in argvs)


def test_a_spent_zai_window_skips_the_run_and_parks_nothing(tmp_path):
    """No fallback to Muse and no hold file: z.ai publishes its windows, so
    the next begin stops on the reading; Muse's lanes are not z.ai's to park."""
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        extra_env={
            "MUSE_STATUS": "75",
            "MUSE_STDERR": "zai-exec: quota-exhausted: HTTP 429 code 1308: "
                           "Usage limit reached for 5 hour",
        })

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 1
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert heartbeat.startswith(
        "finish --agent zcode --run engine-run --outcome skipped-provider-quota")
    assert "z.ai quota exhausted: HTTP 429 code 1308" in heartbeat
    assert "errored" not in heartbeat
    assert not (tmp_path / ".claude" / "command-center-muse-quota-hold").exists()


def test_muses_hold_does_not_park_the_zai_lane(tmp_path):
    hold_dir = tmp_path / ".claude"
    hold_dir.mkdir(parents=True, exist_ok=True)
    hold = hold_dir / "command-center-muse-quota-hold"
    hold.write_text("2099-01-01T00:00:00Z\n")

    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        answers=_review_answers(_judge_answer()))

    assert proc.returncode == 0, proc.stderr
    assert "parked until" not in proc.stderr
    assert _muse_calls(repo) == 2
    assert hold.read_text() == "2099-01-01T00:00:00Z\n", \
        "Muse's hold is left as found"

    # The same hold still parks Muse's own escalated lane.
    proc, repo = _stubbed_runner(
        tmp_path / "escalated", _begin(), _packet(),
        extra_env={"MUSE_REVIEW_ENGINE_ZAI_UNTIL": FUTURE,
                   "HOME": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert "parked until" in proc.stderr
    assert _muse_calls(repo) == 0


def test_a_muse_shaped_refusal_on_the_zai_lane_writes_no_muse_hold(tmp_path):
    """Muse's refusal text arriving on the z.ai path is only a failure there."""
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        extra_env={
            "MUSE_STATUS": "1",
            "MUSE_STDERR": "API error 429: Subscription quota exhausted. Your "
                           "usage window resets at 2099-01-01T00:00:00Z.",
        })

    assert proc.returncode == 1
    assert not (tmp_path / ".claude" / "command-center-muse-quota-hold").exists()
    assert "zai-exec failed (exit 1)" in _heartbeat(repo)


def test_a_zai_failure_finishes_errored_as_zcode(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        extra_env={"MUSE_STATUS": "1",
                   "MUSE_STDERR": "zai-exec: model mismatch: asked for "
                                  "glm-5.3 and glm-5.3-flash answered"})

    assert proc.returncode == 1
    assert _apply_calls(repo) == []
    heartbeat = _heartbeat(repo)
    assert heartbeat.startswith("finish --agent zcode --run engine-run "
                                "--outcome errored")
    assert "zai-exec failed (exit 1)" in heartbeat
    assert "model mismatch" in heartbeat


def test_a_zai_call_past_the_bound_is_killed_like_a_muse_one(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(), bound_seconds=1,
        extra_env={"MUSE_SLEEP": "30"}, timeout=60)

    assert proc.returncode == 124
    heartbeat = _heartbeat(repo)
    assert heartbeat.startswith("finish --agent zcode ")
    assert "#392" in heartbeat


def test_a_missing_zai_exec_refuses_before_begin(tmp_path):
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        extra_env={"ZAI_EXEC_BIN": str(tmp_path / "no-such-zai-exec")})

    assert proc.returncode == 1
    assert "refusing to run the z.ai standard tier" in proc.stderr
    assert not (repo / "funnel.calls").exists()
    assert _muse_calls(repo) == 0


# -- #1411: the z.ai judges and a spent window mid-review ----------------------

SEVEN = ["requirement {}".format(i) for i in range(1, 8)]


def test_zai_judges_run_one_at_a_time_and_muse_judges_together(tmp_path):
    """The Lite plan refuses concurrent requests (1302); parallel judges past
    the limit would read `unsure` and reject a good PR."""
    proc, repo = _zai_standard(
        tmp_path / "zai", _begin(), _packet(),
        answers=(_requirements_answer(*SEVEN),),
        extra_env={"MUSE_DYNAMIC_JUDGES": "1", "MUSE_JUDGE_EXCLUSIVE": "1"})

    assert proc.returncode == 0, proc.stderr
    assert _muse_calls(repo) == 4
    assert not (repo / "muse.count.overlap").exists(), \
        "a z.ai judge started while another was in flight"
    answer = json.loads((repo / "apply.answer").read_text())
    assert answer["verdict"] == "approved"
    assert [entry["requirement"] for entry in answer["requirements"]] == SEVEN
    assert _heartbeat(repo).startswith("finish --agent zcode ")

    # The probe can see overlap: the same review on Muse overlaps.
    proc, repo = _stubbed_runner(
        tmp_path / "on-muse", _begin(), _packet(),
        answers=(_requirements_answer(*SEVEN),),
        extra_env={"MUSE_DYNAMIC_JUDGES": "1", "MUSE_JUDGE_EXCLUSIVE": "1"})
    assert proc.returncode == 0, proc.stderr
    assert (repo / "muse.count.overlap").exists()


@pytest.mark.parametrize("spent_on", ("requirement 1", "requirement 4",
                                      "requirement 7"))
def test_a_judge_meeting_a_spent_window_skips_the_whole_review(tmp_path,
                                                               spent_on):
    """No verdict from a review nobody finished judging: a judge that exits
    75 ends the run as a quota skip, before any apply, and no later judge
    spends credits on it."""
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        answers=(_requirements_answer(*SEVEN),),
        extra_env={
            "MUSE_DYNAMIC_JUDGES": "1",
            "MUSE_JUDGE_FAIL_IF": spent_on,
            "MUSE_JUDGE_FAIL_STATUS": "75",
            "MUSE_JUDGE_FAILURE": "zai-exec: quota-exhausted: HTTP 429 code "
                                  "1308: Usage limit reached for 5 hour",
        })

    assert proc.returncode == 0, proc.stderr
    assert _apply_calls(repo) == [], "no verdict may be applied"
    assert not (repo / "applied.marker").exists()
    heartbeat = _heartbeat(repo)
    assert heartbeat == (
        "finish --agent zcode --run engine-run --outcome "
        "skipped-provider-quota --note z.ai quota exhausted: HTTP 429 code "
        "1308: Usage limit reached for 5 hour\n")
    # Lister, then judges up to and including the one that met the wall.
    chunk = SEVEN.index(spent_on) // 3
    assert _muse_calls(repo) == 1 + chunk + 1
    assert not (tmp_path / ".claude" / "command-center-muse-quota-hold").exists()


def test_a_judge_whose_retries_ran_out_still_fails_closed(tmp_path):
    """zai-exec retries a concurrency refusal until its deadline. A judge
    that still could not get an answer is `unsure`, as on Muse: the verdict
    is rejected rather than approved on requirements nobody judged."""
    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        answers=(_requirements_answer(*SEVEN[:4]),),
        extra_env={
            "MUSE_DYNAMIC_JUDGES": "1",
            "MUSE_JUDGE_FAIL_IF": "requirement 4",
            "MUSE_JUDGE_FAIL_STATUS": "1",
            "MUSE_JUDGE_FAILURE": "zai-exec: HTTP 429 code 1302: High "
                                  "concurrency; no time left to retry",
        })

    assert proc.returncode == 0, proc.stderr
    answer = json.loads((repo / "apply.answer").read_text())
    assert answer["verdict"] == "rejected"
    assert [entry["status"] for entry in answer["requirements"]] == \
        ["met", "met", "met", "unsure"]
    assert "code 1302" in answer["requirements"][3]["evidence"]
    assert "skipped-provider-quota" not in _heartbeat(repo)


def test_a_muse_shaped_refusal_in_a_zai_judge_never_parks_muse(tmp_path):
    """The judge loop's hold guard: on Muse a judge's refusal text records
    the shared hold; on z.ai the same text is only that judge's failure."""
    refusal = ("API error 429: Subscription quota exhausted. Your usage "
               "window resets at 2099-01-01T00:00:00Z.")
    env = {"MUSE_DYNAMIC_JUDGES": "1",
           "MUSE_JUDGE_FAIL_IF": "requirement 4",
           "MUSE_JUDGE_FAILURE": refusal}
    hold = tmp_path / ".claude" / "command-center-muse-quota-hold"

    proc, repo = _zai_standard(
        tmp_path, _begin(), _packet(),
        answers=(_requirements_answer(*SEVEN[:4]),), extra_env=env)
    assert proc.returncode == 0, proc.stderr
    assert not hold.exists(), "a z.ai judge must not park Muse's lanes"
    assert json.loads((repo / "apply.answer").read_text())["verdict"] == \
        "rejected"

    # The guard, not the text, is what spared Muse: the same judge on Muse
    # records the hold.
    proc, repo = _stubbed_runner(
        tmp_path / "on-muse", _begin(), _packet(),
        answers=(_requirements_answer(*SEVEN[:4]),),
        extra_env=dict(env, HOME=str(tmp_path)))
    assert proc.returncode == 0, proc.stderr
    assert hold.read_text().strip() == "2099-01-01T00:00:00Z"
