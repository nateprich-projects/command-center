#!/usr/bin/env python3
"""Validate the model's review answer and perform its effects (#800).

Phase 1 of #794: the review runner shows the model a packet (see
``engine/review.py``) and nothing else. The model answers with one JSON
object, ``{"verdict", "blocking", "unsure", "requirements"}``, and this
step validates that answer against the schema and performs every side
effect. The model never runs ``funnel.py`` or ``gh`` itself.

Effects, through the existing paths, never re-derived here:

* approved -> record the verdict at the packet head with ``funnel
  review``, merge with ``funnel merge`` (which also closes the ticket).
* an approved review refused because the PR is no longer open -> re-read the
  PR; if another run's approval covers the merged head, record no verdict and
  report the existing ``skipped-locked`` outcome.
* rejected -> record the verdict with its blocking list. No merge.
* ``unsure`` non-empty -> rejected, whatever the verdict said.
* any ``requirements`` entry not ``met`` -> rejected, whatever the
  verdict said, with the requirement quoted in the blocking list.
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
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402
from engine import review  # noqa: E402

#: Malformed answer on a retryable attempt: the runner feeds the parse
#: error back to the model and calls again with ``--attempt 2``.
RETRY_EXIT = 3

#: Attempts past the first are final: a malformed answer then is
#: recorded as rejected, never retried again.
FINAL_ATTEMPT = 2

#: Raw model output kept in the second-failure note. The note is a GitHub
#: comment, so an unbounded paste of model output does not belong there.
MAX_RAW_NOTE = 4000

# The review runner reads this line from review-apply's captured output to
# finish the run with the existing heartbeat merge record.
OBSERVED_MERGE_PREFIX = "review-apply: observed already-merged PR: "

#: Marker the runner matches to finish the run errored after a final
#: malformed answer. Printed on stdout, where runner diagnostics live.
ERRORED_OUTCOME = "run outcome: errored"

# The review runner records this existing non-error outcome when apply finds
# that another run already merged the PR under an approval for that head.
SKIPPED_LOCKED_OUTCOME = "run outcome: skipped-locked"

#: The review contract uses the participles, but these two exact lowercase
#: verbs are unambiguous synonyms the model has already emitted in practice.
#: Keep the map deliberately narrow: case and whitespace changes still need
#: the retry path so malformed answers remain visible.
NORMALISED_VERDICTS = {
    "approve": "approved",
    "reject": "rejected",
}

#: The per-requirement statuses the conformance pass (#1187) records.
#: ``met`` means the diff does exactly what the requirement asks — a
#: requirement satisfied twice over is ``unmet``, with both sites cited
#: in the evidence, because the diff does more than asked.
REQUIREMENT_STATUSES = ("met", "unmet", "unsure")


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


def _requirements_list(answer: dict) -> List[Dict[str, str]]:
    """The validated per-requirement conformance pass, or raise.

    Each entry quotes one requirement the tickets or the plan state,
    says whether the diff meets it, and cites the diff lines as
    evidence. Unknown keys on an entry are ignored, like unknown keys
    on the answer itself.
    """
    if "requirements" not in answer:
        raise AnswerError("missing required key 'requirements'")
    values = answer["requirements"]
    if not isinstance(values, list):
        raise AnswerError(
            "'requirements' must be a list, got {}".format(
                type(values).__name__))
    shaped: List[Dict[str, str]] = []
    for index, entry in enumerate(values):
        if not isinstance(entry, dict):
            raise AnswerError(
                "'requirements'[{}] must be an object, got {}".format(
                    index, type(entry).__name__))
        for key in ("requirement", "status", "evidence"):
            if key not in entry:
                raise AnswerError(
                    "'requirements'[{}] is missing required key {!r}".format(
                        index, key))
        requirement = entry["requirement"]
        status = entry["status"]
        evidence = entry["evidence"]
        if not isinstance(requirement, str) or not requirement.strip():
            raise AnswerError(
                "'requirements'[{}].requirement must be a non-empty "
                "string".format(index))
        if status not in REQUIREMENT_STATUSES:
            raise AnswerError(
                "'requirements'[{}].status must be one of {}; received "
                "value {!r}".format(
                    index, list(REQUIREMENT_STATUSES), status))
        if not isinstance(evidence, str) or not evidence.strip():
            raise AnswerError(
                "'requirements'[{}].evidence must be a non-empty "
                "string".format(index))
        shaped.append({"requirement": requirement, "status": status,
                       "evidence": evidence})
    return shaped


def parse_answer(raw: str) -> Dict[str, object]:
    """Parse and schema-validate one model answer.

    Returns the answer with its verdict, blocking, and unsure lists
    and its per-requirement conformance pass. Unknown keys are
    ignored: they cannot smuggle an approval through, because only the
    validated ``verdict`` decides. Raises ``AnswerError`` describing
    the first violation found.
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
    original_verdict = answer.get("verdict")
    verdict = original_verdict
    normalised_from = None
    if isinstance(original_verdict, str):
        normalised_verdict = NORMALISED_VERDICTS.get(original_verdict)
        if normalised_verdict is not None:
            verdict = normalised_verdict
            normalised_from = original_verdict
    if verdict not in funnel.VERDICTS:
        raise AnswerError(
            "field 'verdict' must be one of {}; received value {!r}".format(
                list(funnel.VERDICTS), original_verdict))
    blocking = _string_list(answer, "blocking")
    unsure = _string_list(answer, "unsure")
    requirements = _requirements_list(answer)
    if verdict == "approved" and blocking:
        # An approval that names blockers contradicts itself, and the
        # merge gate only reads the verdict word: recording this as
        # approved would merge a diff the model itself flagged. Treat
        # the confusion as malformed so the runner retries once.
        raise AnswerError(
            "approved verdict must not carry blocking items")
    if verdict == "approved" and not requirements:
        # An approval with no conformance pass is baseless: the model
        # must have walked at least one requirement to approve. A
        # rejection may carry an empty pass — the runner's own
        # precheck rejections do — because the safe direction needs
        # no evidence to stay safe.
        raise AnswerError(
            "approved verdict must record at least one requirement")
    parsed = {"verdict": verdict, "blocking": blocking, "unsure": unsure,
              "requirements": requirements}
    if normalised_from is not None:
        parsed["normalised_from"] = normalised_from
    return parsed


def decide(answer: Dict[str, object]) -> Tuple[str, List[str], Optional[str]]:
    """The verdict to record: word, blocking list, and note.

    ``unsure`` non-empty means rejected even when the verdict said
    approved; the unsure entries join the blocking list with their
    provenance marked, so the engineer sees every reason in one place.
    Any ``requirements`` entry not ``met`` does the same: the detailed
    findings override a summary approval, so a diff the pass caught —
    a requirement met twice over, a line missing — can never merge on
    the strength of the verdict word alone. Pure: no IO.
    """
    verdict = str(answer["verdict"])
    blocking = list(answer["blocking"])  # type: ignore[arg-type]
    unsure = list(answer["unsure"])  # type: ignore[arg-type]
    requirements = answer.get("requirements") or []
    notes = []
    normalised_from = answer.get("normalised_from")
    if isinstance(normalised_from, str):
        notes.append(
            "normalised verdict {!r} to {!r}".format(
                normalised_from, verdict))
    if unsure:
        notes.append(
            "recorded as rejected because unsure was non-empty; "
            "the model said {!r}".format(verdict))
        verdict = "rejected"
        blocking = blocking + ["unsure: " + item for item in unsure]
    unmet = [entry for entry in requirements  # type: ignore[union-attr]
             if isinstance(entry, dict) and entry.get("status") == "unmet"]
    req_unsure = [entry for entry in requirements  # type: ignore[union-attr]
                  if isinstance(entry, dict)
                  and entry.get("status") == "unsure"]
    if unmet or req_unsure:
        parts = []
        if unmet:
            parts.append("{} requirement(s) were unmet".format(len(unmet)))
        if req_unsure:
            parts.append("{} requirement(s) were unsure".format(
                len(req_unsure)))
        notes.append(
            "recorded as rejected because {}; "
            "the model said {!r}".format(" and ".join(parts), verdict))
        verdict = "rejected"
        blocking = blocking + [
            "requirement unmet: {} -- {}".format(
                entry.get("requirement"), entry.get("evidence"))
            for entry in unmet]
        blocking = blocking + [
            "requirement unsure: {} -- {}".format(
                entry.get("requirement"), entry.get("evidence"))
            for entry in req_unsure]
    return verdict, blocking, "; ".join(notes) or None


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


def _is_not_open_refusal(error: funnel.GitHubError, pr: int) -> bool:
    """Whether ``cmd_review`` refused only because this PR was not OPEN."""
    message = str(error)
    return (message.startswith("PR #{} is ".format(pr))
            and message.endswith(", not open"))


def _latest_review_comment(comments: object):
    """The newest structured verdict and its provenance from one PR snapshot."""
    if not isinstance(comments, list):
        return None
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        verdict = funnel.parse_verdict(body)
        if verdict is not None:
            return verdict, funnel.parse_provenance(body)
    return None


def _classify_not_open_refusal(repo: str, pr: int,
                               run: Optional[str]) -> int:
    """Classify a review race from the existing batched PR state reader.

    Only an attributable approval from a different run, on the head GitHub
    reports for the merged PR, is a clean ``skipped-locked``. Missing or
    contradictory evidence remains a failure, and this function never writes
    a verdict of its own.
    """
    try:
        fact = funnel._pr_fact_for_number(repo, pr, include_comments=True)
    except (funnel.GitHubError, OSError, subprocess.SubprocessError) as exc:
        print("review-apply: could not re-read PR #{} after the not-open "
              "refusal: {}".format(pr, exc), file=sys.stderr)
        return 1

    if not isinstance(fact, dict):
        print("review-apply: could not re-read PR #{} after the not-open "
              "refusal: no matching PR in the state snapshot".format(pr),
              file=sys.stderr)
        return 1

    state = fact.get("state")
    found = state.upper() if isinstance(state, str) and state else "UNKNOWN"
    if found != "MERGED":
        detail = "CLOSED unmerged" if found == "CLOSED" else found
        print("review-apply: PR #{} was refused as not open; re-read found "
              "state {} instead of MERGED".format(pr, detail),
              file=sys.stderr)
        return 1

    merged_head = fact.get("headRefOid")
    if not isinstance(merged_head, str) or not merged_head.strip():
        print("review-apply: PR #{} is MERGED but its head is unreadable; "
              "no covering approval can be confirmed".format(pr),
              file=sys.stderr)
        return 1

    latest = _latest_review_comment(fact.get("comments"))
    if latest is None:
        print("review-apply: PR #{} is MERGED at head {} but has no "
              "readable covering approved verdict".format(pr, merged_head),
              file=sys.stderr)
        return 1
    verdict, provenance = latest
    if (verdict.get("verdict") != "approved"
            or verdict.get("head_sha") != merged_head):
        found_verdict = verdict.get("verdict") or "unknown verdict"
        found_head = verdict.get("head_sha") or "unknown head"
        print("review-apply: PR #{} is MERGED at head {} but its latest "
              "verdict is {} at head {}; no covering approved verdict "
              "was found".format(pr, merged_head, found_verdict, found_head),
              file=sys.stderr)
        return 1

    superseding_run = (
        provenance.get("run") if isinstance(provenance, dict) else None)
    superseding_agent = (
        provenance.get("agent") if isinstance(provenance, dict) else None)
    if (not isinstance(provenance, dict)
            or provenance.get("voice") != "agent"
            or not isinstance(superseding_run, str)
            or not superseding_run.strip()
            or not isinstance(superseding_agent, str)
            or not superseding_agent.strip()
            or not isinstance(run, str)
            or not run.strip()
            or superseding_run == run):
        print("review-apply: PR #{} is MERGED at head {} with an approved "
              "verdict, but its provenance does not identify another run "
              "and agent".format(pr, merged_head), file=sys.stderr)
        return 1

    note = ("PR #{} in {} was already merged at head {} under the approved "
            "verdict from run {} (agent {}); this run recorded no verdict"
            .format(pr, repo, merged_head, superseding_run, superseding_agent))
    print("review-apply: {}".format(note))
    print(SKIPPED_LOCKED_OUTCOME)
    return 0


def load_merge_items(repo: str, pr_fact: Optional[dict]) -> list:
    """The board without history, plus history for the merge subject (#1621).

    The full board history cost about a minute and a hundred GraphQL points
    per merge. The merge path reads history only for regression items (read
    by ``merge_blockers``) and for the branch ticket and its project, whose
    ``status_since`` is the drift fallback when the project auto-closes. A
    failed read raises, and ``apply_approved`` treats that as a refused merge.
    """
    items = funnel.load_items(include_details=False)
    branch = pr_fact.get("headRefName") if isinstance(pr_fact, dict) else None
    ref = funnel.ticket_ref_from_branch(repo, branch or "")
    if ref is None:
        return items
    ticket = next((item for item in items if item.ref == ref), None)
    if ticket is None:
        return items
    subjects = [ticket]
    parent = next(
        (item for item in items if ticket.parent and item.ref == ticket.parent),
        None)
    if parent is not None:
        subjects.append(parent)
    funnel.hydrate_item_details(items, subjects)
    return items


def apply_approved(repo: str, pr: int, blocking: List[str], note: Optional[str],
                   ci: str, run: Optional[str], agent: Optional[str],
                   approved_head: Optional[str] = None) -> int:
    """Record the approval, then merge through the existing gate.

    ``funnel merge`` refuses unless every condition holds — CI green,
    the verdict at the head, the ticket's project Building — and closes
    the ticket when the merge lands. If it refuses, classify an already-
    merged PR only from the live PR state and the recorded approved head.
    """
    try:
        funnel.cmd_review(repo, pr, "approved", ci, blocking, note,
                          run=run, agent=agent)
    except funnel.GitHubError as exc:
        if _is_not_open_refusal(exc, pr):
            return _classify_not_open_refusal(repo, pr, run)
        raise
    merge_error = None
    try:
        # Read after the verdict is written, as cmd_merge's own read did,
        # so the gate sees this run's approval. The branch in this fact
        # picks the ticket whose history is read. A missing fact goes in
        # as empty, so the gate refuses ("could not be read") instead of
        # reading again and gating a branch whose history was not loaded.
        pr_fact = funnel._pr_fact_for_number(repo, pr, include_comments=True)
        merge_status = funnel.cmd_merge(
            load_merge_items(repo, pr_fact), datetime.now(timezone.utc),
            repo, pr, True, pr_fact=pr_fact if pr_fact is not None else {})
    except funnel.GitHubError as exc:
        # The remote merge command can lose a race with a merge after the gate's read.
        # Re-read GitHub before deciding whether that failure was terminal.
        merge_status = 1
        merge_error = str(exc)
    if merge_status == 0:
        return 0

    try:
        pr_view = review.fetch_pr(repo, pr)
    except funnel.GitHubError as exc:
        detail = "merge refused and current PR state could not be read: {}".format(
            exc)
        if merge_error:
            detail = "{}; merge command: {}".format(detail, merge_error)
        raise funnel.GitHubError(detail)

    if not isinstance(pr_view, dict):
        raise funnel.GitHubError(
            "merge refused and current PR state was unreadable")
    state = pr_view.get("state")
    if not isinstance(state, str) or not state.strip():
        raise funnel.GitHubError(
            "merge refused and current PR state was unreadable")
    if state.upper() != "MERGED":
        if merge_error:
            print("review-apply: {}".format(merge_error), file=sys.stderr)
        return merge_status

    verdict = funnel.latest_verdict(repo, pr)
    if not isinstance(verdict, dict) or verdict.get("verdict") != "approved":
        raise funnel.GitHubError(
            "PR #{} is merged but its latest approved verdict could not be "
            "read".format(pr))
    verdict_head = verdict.get("head_sha")
    if not isinstance(verdict_head, str) or not verdict_head.strip():
        raise funnel.GitHubError(
            "PR #{} is merged but the approved verdict has no readable head"
            .format(pr))
    if approved_head and verdict_head != approved_head:
        raise funnel.GitHubError(
            "PR #{} has an approved verdict at {}, not packet head {}".format(
                pr, verdict_head, approved_head))

    merged_head = pr_view.get("headRefOid")
    if not isinstance(merged_head, str) or not merged_head.strip():
        raise funnel.GitHubError(
            "PR #{} is merged but its branch head is unreadable".format(pr))
    if merged_head != verdict_head:
        print(
            "review-apply: merge refused because PR #{} is already merged at "
            "head {}, while its approved head is {}".format(
                pr, merged_head, verdict_head),
            file=sys.stderr)
        return merge_status

    merged_by = pr_view.get("mergedBy")
    actor = merged_by.get("login") if isinstance(merged_by, dict) else None
    merged_at = pr_view.get("mergedAt")
    if not isinstance(actor, str) or not actor.strip():
        raise funnel.GitHubError(
            "PR #{} is merged at the approved head but its merge actor is "
            "unreadable".format(pr))
    if not isinstance(merged_at, str) or not merged_at.strip():
        raise funnel.GitHubError(
            "PR #{} is merged at the approved head but its merge timestamp is "
            "unreadable".format(pr))

    observed = {"pr": pr, "head": merged_head, "actor": actor,
                "merged_at": merged_at}
    print(OBSERVED_MERGE_PREFIX + json.dumps(observed, sort_keys=True))
    return 0


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
                                  args.run, args.agent, args.head)
        return apply_rejected(repo, args.pr, blocking, note, args.ci,
                              args.run, args.agent)
    except funnel.GitHubError as exc:
        print("review-apply: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
