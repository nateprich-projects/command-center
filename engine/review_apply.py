#!/usr/bin/env python3
"""Validate the model's review answer and perform its effects (#800).

Phase 1 of #794: the review runner shows the model a packet (see
``engine/review.py``) and nothing else. The model answers with one JSON
object, ``{"verdict", "blocking", "unsure"}``, and this step validates
that answer against the schema and performs every side effect. The model
never runs ``funnel.py`` or ``gh`` itself.

Effects, through the existing paths, never re-derived here:

* approved -> record the verdict at the packet head with ``funnel
  review``, merge with ``funnel merge`` (which also closes the ticket).
* rejected -> record the verdict with its blocking list. No merge.
* ``unsure`` non-empty -> rejected, whatever the verdict said.
* malformed, first attempt -> exit 3 with the parse error, recording
  nothing, so the runner can retry once with the error fed back.
* malformed, final attempt -> record rejected with the raw output in
  the note and exit 1 with ``run outcome: errored`` on stdout, which
  the runner maps to its errored finish.

``--validate-only`` parses and decides without recording anything, for
the review runner's shadow path: valid input prints the decided
``{"verdict", "blocking", "note"}`` and exits 0; malformed input exits
3 on a retryable attempt and 1 on a final one, recording nothing
either way.

No approval can result from malformed input: every validation failure
either retries or records rejected. Exit codes: 0 applied (or valid,
with ``--validate-only``), 1 failed after recording or refused without
recording, 3 malformed and retryable.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402

#: Malformed answer on a retryable attempt: the runner feeds the parse
#: error back to the model and calls again with ``--attempt 2``.
RETRY_EXIT = 3

#: Attempts past the first are final: a malformed answer then is
#: recorded as rejected, never retried again.
FINAL_ATTEMPT = 2

#: Raw model output kept in the second-failure note. The note is a GitHub
#: comment, so an unbounded paste of model output does not belong there.
MAX_RAW_NOTE = 4000

#: Marker the runner matches to finish the run errored after a final
#: malformed answer. Printed on stdout, where runner diagnostics live.
ERRORED_OUTCOME = "run outcome: errored"


class AnswerError(ValueError):
    """The model's answer failed schema validation. Fail-closed."""


def _string_list(answer: dict, key: str) -> List[str]:
    """The validated string list at ``key``, or raise ``AnswerError``."""
    if key not in answer:
        raise AnswerError("missing required key {!r}".format(key))
    values = answer[key]
    if not isinstance(values, list):
        raise AnswerError(
            "{!r} must be a list, got {}".format(key, type(values).__name__))
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise AnswerError(
                "{!r}[{}] must be a string, got {}".format(
                    key, index, type(value).__name__))
    return list(values)


def parse_answer(raw: str) -> Dict[str, object]:
    """Parse and schema-validate one model answer.

    Returns the answer with its verdict, blocking, and unsure lists.
    Unknown keys are ignored: they cannot smuggle an approval through,
    because only the validated ``verdict`` decides. Raises
    ``AnswerError`` describing the first violation found.
    """
    if not (raw or "").strip():
        raise AnswerError("empty answer: expected a JSON object")
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnswerError("invalid JSON: {}".format(exc))
    if not isinstance(answer, dict):
        raise AnswerError(
            "answer must be a JSON object, got {}".format(
                type(answer).__name__))
    verdict = answer.get("verdict")
    if verdict not in funnel.VERDICTS:
        raise AnswerError(
            "verdict must be one of {}, got {!r}".format(
                list(funnel.VERDICTS), verdict))
    blocking = _string_list(answer, "blocking")
    unsure = _string_list(answer, "unsure")
    if verdict == "approved" and blocking:
        # An approval that names blockers contradicts itself, and the
        # merge gate only reads the verdict word: recording this as
        # approved would merge a diff the model itself flagged. Treat
        # the confusion as malformed so the runner retries once.
        raise AnswerError(
            "approved verdict must not carry blocking items")
    return {"verdict": verdict, "blocking": blocking, "unsure": unsure}


def decide(answer: Dict[str, object]) -> Tuple[str, List[str], Optional[str]]:
    """The verdict to record: word, blocking list, and note.

    ``unsure`` non-empty means rejected even when the verdict said
    approved; the unsure entries join the blocking list with their
    provenance marked, so the engineer sees every reason in one place.
    Pure: no IO.
    """
    verdict = str(answer["verdict"])
    blocking = list(answer["blocking"])  # type: ignore[arg-type]
    unsure = list(answer["unsure"])  # type: ignore[arg-type]
    if unsure:
        return (
            "rejected",
            blocking + ["unsure: " + item for item in unsure],
            "recorded as rejected because unsure was non-empty; "
            "the model said {!r}".format(verdict),
        )
    return verdict, blocking, None


def truncate_raw(raw: str) -> str:
    """The raw output as a note: capped, with the cut marked."""
    if len(raw) <= MAX_RAW_NOTE:
        return raw
    return raw[:MAX_RAW_NOTE] + "\n…[truncated {} chars]".format(
        len(raw) - MAX_RAW_NOTE)


def read_answer(source: str) -> str:
    """The answer text from ``-`` (stdin) or a file path."""
    if source == "-":
        return sys.stdin.read()
    with io.open(source, encoding="utf-8") as handle:
        return handle.read()


def current_head(repo: str, pr: int) -> str:
    """The branch head right now, read the way the review path reads it."""
    data = funnel._gh_json(
        "gh", "pr", "view", str(pr), "--repo", repo, "--json", "headRefOid")
    head = (data or {}).get("headRefOid")
    if not head:
        raise funnel.GitHubError(
            "could not read the head commit of PR #{}".format(pr))
    return head


def apply_approved(repo: str, pr: int, blocking: List[str], note: Optional[str],
                   ci: str, run: Optional[str], agent: Optional[str]) -> int:
    """Record the approval, then merge through the existing gate.

    ``funnel merge`` refuses unless every condition holds — CI green,
    the verdict at the head, the ticket's project Building — and closes
    the ticket when the merge lands. Its exit code is the answer.
    """
    funnel.cmd_review(repo, pr, "approved", ci, blocking, note,
                      run=run, agent=agent)
    return funnel.cmd_merge(
        funnel.load_items(), datetime.now(timezone.utc), repo, pr, True)


def apply_rejected(repo: str, pr: int, blocking: List[str],
                   note: Optional[str], ci: str, run: Optional[str],
                   agent: Optional[str]) -> int:
    """Record the rejection with its blocking list. No merge follows."""
    funnel.cmd_review(repo, pr, "rejected", ci, blocking, note,
                      run=run, agent=agent)
    return 0


def apply_malformed_final(repo: str, pr: int, raw: str, error: AnswerError,
                          ci: str, run: Optional[str], agent: Optional[str]) -> int:
    """Second malformed answer: record rejected, signal the errored run.

    The parse error becomes the blocking item and the raw output the
    note, so the next reader sees what the model actually said. Exits 1
    with the errored-outcome marker the runner finishes on.
    """
    funnel.cmd_review(
        repo, pr, "rejected", ci,
        ["the model's answer could not be parsed: {}".format(error)],
        truncate_raw(raw), run=run, agent=agent)
    print("review-apply: recorded rejected on PR #{} in {} after "
          "a malformed final answer; {}".format(pr, repo, ERRORED_OUTCOME))
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate one model answer and perform its effects."""
    parser = argparse.ArgumentParser(
        description="validate a review answer and perform its effects")
    parser.add_argument("pr", type=int, help="PR number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    parser.add_argument("--answer", required=True,
                        help="model answer JSON file, or - for stdin")
    parser.add_argument("--ci", default="unknown", choices=funnel.CI_STATES,
                        help="CI state from the packet (default: unknown)")
    parser.add_argument("--run", default=None,
                        help="heartbeat run id recorded in provenance")
    parser.add_argument("--agent", default=None,
                        help="agent name recorded in provenance")
    parser.add_argument("--head", default=None,
                        help="packet head sha: refuse without recording "
                             "when the branch has moved since")
    parser.add_argument("--attempt", type=int, default=1,
                        help="1 (default) retries a malformed answer with "
                             "exit 3; 2 records it as rejected")
    parser.add_argument("--validate-only", action="store_true",
                        help="parse and decide without recording anything; "
                             "print the decision as JSON")
    args = parser.parse_args(argv)
    if args.attempt < 1:
        parser.error("--attempt must be at least 1")
    try:
        raw = read_answer(args.answer)
    except (IOError, OSError) as exc:
        print("review-apply: cannot read the answer: {}".format(exc),
              file=sys.stderr)
        return 1
    try:
        answer = parse_answer(raw)
    except AnswerError as exc:
        print("review-apply: malformed answer "
              "(attempt {}): {}".format(args.attempt, exc),
              file=sys.stderr)
        if args.validate_only:
            # Shadow validation: the attempt split without the effects.
            return RETRY_EXIT if args.attempt < FINAL_ATTEMPT else 1
        if args.attempt < FINAL_ATTEMPT:
            return RETRY_EXIT
        try:
            return apply_malformed_final(
                funnel.resolve_repo(args.repo), args.pr, raw, exc, args.ci,
                args.run, args.agent)
        except funnel.GitHubError as exc:
            print("review-apply: {}".format(exc), file=sys.stderr)
            return 1
    verdict, blocking, note = decide(answer)
    if args.validate_only:
        print(json.dumps({"verdict": verdict, "blocking": blocking,
                          "note": note}, sort_keys=True))
        return 0
    try:
        repo = funnel.resolve_repo(args.repo)
        if args.head is not None:
            head = current_head(repo, args.pr)
            if head != args.head:
                # The model reviewed the packet's diff, not this one.
                # Recording nothing keeps the PR awaiting review, so the
                # next run re-collects the packet and asks again.
                print("review-apply: packet head {} is not the current "
                      "head {}; re-collect the packet".format(
                          args.head, head), file=sys.stderr)
                return 1
        if verdict == "approved":
            return apply_approved(repo, args.pr, blocking, note, args.ci,
                                  args.run, args.agent)
        return apply_rejected(repo, args.pr, blocking, note, args.ci,
                              args.run, args.agent)
    except funnel.GitHubError as exc:
        print("review-apply: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
