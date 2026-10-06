"""Build and post the reviewer-B paired-trial endpoint report."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import funnel
import heartbeat
from engine import reviewer_b, review_packets


TRIAL_ID = reviewer_b.TRIAL_ID
PARENT_REPO = "nateprich-projects/command-center"
PARENT_ISSUE = 2078
TABLE_MARKER = "<!-- command-center-paired-trial: {} -->".format(TRIAL_ID)
CURRENCY = chr(36)
TICK = chr(96)

_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_PAIR_MARKER_RE = re.compile(
    r"(?m)^[ \t]*<!--\s*" + re.escape(TRIAL_ID)
    + r"\s+pair=([^\s>]+)\s*-->[ \t]*$"
)
_HEADING_RE = re.compile(r"(?m)^### Reviewer B shadow note \(([^)]+)\)$")
_VERDICT_RE = re.compile(
    r"(?m)^- Reviewer ([AB]): " + re.escape(TICK)
    + r"(approved|rejected)" + re.escape(TICK) + r"$"
)
_HEAD_RE = re.compile(
    r"(?m)^- Head SHA: " + re.escape(TICK) + r"([0-9a-f]{40})"
    + re.escape(TICK) + r"$"
)
_COST_RE = re.compile(
    r"(?m)^- Reviewer B cost: " + re.escape(CURRENCY)
    + r"([0-9]+(?:\.[0-9]+)?) own-card usage delta from heartbeat records\.$"
)
_LATENCY_RE = re.compile(
    r"(?m)^- Reviewer B latency: ([0-9]+(?:\.[0-9]+)?) seconds "
    r"from paired heartbeat timestamps\.$"
)

_PARENT_COMMENTS_QUERY = """query ParentPairedTrialComments(
  $owner: String!, $name: String!, $number: Int!, $cursor: String
) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      comments(first: 100, after: $cursor) {
        nodes { id body }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

_PR_COMMENTS_QUERY = """query PairedTrialPrComments(
  $owner: String!, $name: String!, $number: Int!, $cursor: String
) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        nodes { id body }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""

_UPDATE_COMMENT_MUTATION = """mutation UpdatePairedTrialComment(
  $id: ID!, $body: String!
) {
  updateIssueComment(input: {id: $id, body: $body}) {
    issueComment { id body }
  }
}"""

_ADD_COMMENT_MUTATION = """mutation AddPairedTrialComment(
  $subjectId: ID!, $body: String!
) {
  addComment(input: {subjectId: $subjectId, body: $body}) {
    commentEdge { node { id } }
  }
}"""


class PairedTrialError(ValueError):
    """A reviewer-B trial note or GitHub roll-up is malformed."""


def _one(pattern: re.Pattern, body: str) -> Optional[object]:
    matches = pattern.findall(body)
    return matches[0] if len(matches) == 1 else None


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise PairedTrialError("{} is invalid".format(label))
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PairedTrialError("{} is invalid".format(label)) from exc
    if not parsed.is_finite() or parsed < 0:
        raise PairedTrialError("{} is invalid".format(label))
    return parsed


def _pair_id(body: str) -> Optional[str]:
    value = _one(_PAIR_MARKER_RE, body)
    return value if isinstance(value, str) else None


def parse_shadow_note(body: object, *, repo: str, pr: int
                      ) -> Optional[Dict[str, object]]:
    """Parse one complete reviewer-B Markdown note, keeping its shared head.

    Reviewer A and B verdicts are carried in one note with one head SHA. That
    representation binds both verdicts to the same commit. Caller-provided
    heartbeat identity is checked separately before the note is counted.
    """
    if (not isinstance(body, str) or not body
            or not _REPOSITORY_RE.fullmatch(repo)
            or isinstance(pr, bool) or not isinstance(pr, int) or pr < 1):
        return None

    pair_id = _pair_id(body)
    headings = _HEADING_RE.findall(body)
    if not pair_id or len(headings) != 1:
        return None
    heading = headings[0]
    if heading == "live":
        kind, sample_name = "live", None
    elif heading.startswith("calibration "):
        sample_name = heading[len("calibration "):].strip()
        kind = reviewer_b.CALIBRATION_SIDES.get(sample_name)
        if kind not in ("bad", "good"):
            return None
    else:
        return None

    # Model-authored findings follow this section and are not note metadata.
    header = body.split("\nReviewer B findings:", 1)[0]
    verdicts = _VERDICT_RE.findall(header)
    if (len(verdicts) != 2 or {side for side, _ in verdicts} != {"A", "B"}):
        return None
    by_side = dict(verdicts)
    head_sha = _one(_HEAD_RE, header)
    cost_text = _one(_COST_RE, header)
    latency_text = _one(_LATENCY_RE, header)
    if (not isinstance(head_sha, str) or not isinstance(cost_text, str)
            or not isinstance(latency_text, str)):
        return None
    try:
        cost = _decimal(cost_text, "reviewer-B cost")
        latency = _decimal(latency_text, "reviewer-B latency")
    except PairedTrialError:
        return None

    return {
        "pair_id": pair_id,
        "repo": repo,
        "pr": pr,
        "kind": kind,
        "sample_name": sample_name,
        "head_sha": head_sha,
        # The per-run format carries one SHA for the paired A/B observation.
        "a_head_sha": head_sha,
        "b_head_sha": head_sha,
        "a_verdict": by_side["A"],
        "b_verdict": by_side["B"],
        "cost_dollars": cost,
        "latency_seconds": latency,
    }


def _validated_pair_reference(
    value: object, *, calibration_packets: Optional[Dict[str, Dict]] = None,
) -> Dict[str, object]:
    if not isinstance(value, dict):
        raise PairedTrialError("heartbeat paired-trial reference is malformed")
    pair_id = value.get("pair_id")
    repo = value.get("repo")
    pr = value.get("pr")
    head_sha = value.get("head_sha")
    kind = value.get("kind")
    sample_name = value.get("sample_name")
    if (not isinstance(pair_id, str) or not pair_id.strip()
            or any(char.isspace() for char in pair_id)
            or not isinstance(repo, str) or not _REPOSITORY_RE.fullmatch(repo)
            or isinstance(pr, bool) or not isinstance(pr, int) or pr < 1
            or not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha)
            or kind not in ("live", "bad", "good")):
        raise PairedTrialError("heartbeat paired-trial reference is malformed")
    if kind == "live":
        if sample_name not in (None, ""):
            raise PairedTrialError("live paired-trial reference has a calibration")
        sample_name = None
    elif (sample_name not in reviewer_b.CALIBRATION_NAMES
          or reviewer_b.CALIBRATION_SIDES.get(sample_name) != kind):
        raise PairedTrialError("calibration paired-trial reference is malformed")
    else:
        packet = (calibration_packets or {}).get(sample_name)
        if (not isinstance(packet, dict)
                or repo != packet.get("repo")
                or pr != packet.get("pr")
                or head_sha != packet.get("head_sha")):
            raise PairedTrialError(
                "calibration paired-trial reference does not match fixed packet")
    return {
        "pair_id": pair_id,
        "repo": repo,
        "pr": pr,
        "head_sha": head_sha,
        "kind": kind,
        "sample_name": sample_name,
    }


def _read_pr_comments(repo: str, number: int) -> List[Dict[str, object]]:
    """Read all issue comments attached to one pull request."""
    if (not isinstance(repo, str) or not _REPOSITORY_RE.fullmatch(repo)
            or isinstance(number, bool) or not isinstance(number, int)
            or number < 1):
        raise PairedTrialError("paired-trial pull request reference is malformed")
    owner, name = repo.split("/", 1)
    cursor = None
    cursors = set()
    comments = []
    while True:
        variables = {"owner": owner, "name": name, "number": number}
        if cursor is not None:
            variables["cursor"] = cursor
        data = funnel.gh_graphql(_PR_COMMENTS_QUERY, **variables)
        repository = data.get("repository") if isinstance(data, dict) else None
        pull_request = (repository.get("pullRequest")
                        if isinstance(repository, dict) else None)
        connection = (pull_request.get("comments")
                      if isinstance(pull_request, dict) else None)
        page_info = (connection.get("pageInfo")
                     if isinstance(connection, dict) else None)
        nodes = (connection.get("nodes")
                 if isinstance(connection, dict) else None)
        if (not isinstance(nodes, list) or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)):
            raise PairedTrialError(
                "could not read comments for {} PR #{}".format(repo, number))
        for row in nodes:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not isinstance(row.get("body"), str)):
                raise PairedTrialError(
                    "pull-request comment row is unreadable for {} PR #{}".
                    format(repo, number))
            comments.append({"id": row["id"], "body": row["body"]})
        if not page_info["hasNextPage"]:
            return comments
        next_cursor = page_info.get("endCursor")
        if (not isinstance(next_cursor, str) or not next_cursor
                or next_cursor in cursors):
            raise PairedTrialError(
                "pull-request comment pagination did not advance for {} PR #{}".
                format(repo, number))
        cursors.add(next_cursor)
        cursor = next_cursor


def collect_pair_notes(
    pair_references: Iterable[Dict[str, object]],
    *,
    comment_reader: Optional[Callable[[str, int], Sequence[Dict[str, object]]]] = None,
) -> List[Dict[str, object]]:
    """Read per-run PR comments for heartbeat-indexed completed pairs."""
    read_comments = comment_reader or _read_pr_comments
    references: Dict[str, Dict[str, object]] = {}
    conflicting_references = set()
    grouped: Dict[Tuple[str, int], List[Dict[str, object]]] = {}
    calibration_packets = None
    for raw in pair_references:
        if (calibration_packets is None and isinstance(raw, dict)
                and raw.get("kind") in ("bad", "good")):
            try:
                calibration_packets = review_packets.load_packet_set(
                    reviewer_b.CALIBRATION_VERSION)
            except review_packets.PacketSetError as exc:
                raise PairedTrialError(
                    "fixed calibration packet set is unavailable") from exc
        reference = _validated_pair_reference(
            raw, calibration_packets=calibration_packets)
        pair_id = reference["pair_id"]
        previous = references.get(pair_id)
        if previous is not None and previous != reference:
            conflicting_references.add(pair_id)
            continue
        references[pair_id] = reference
        grouped.setdefault((reference["repo"], reference["pr"]), []).append(reference)

    candidates: Dict[str, List[Dict[str, object]]] = {}
    invalid_pair_ids = set(conflicting_references)
    for (repo, pr), rows in grouped.items():
        expected_ids = {row["pair_id"] for row in rows}
        comments = read_comments(repo, pr)
        if not isinstance(comments, (list, tuple)):
            raise PairedTrialError(
                "PR comments were unreadable for {}#{}".format(repo, pr))
        for comment in comments:
            body = comment if isinstance(comment, str) else (
                comment.get("body") if isinstance(comment, dict) else None)
            if not isinstance(body, str):
                continue
            marked_ids = _PAIR_MARKER_RE.findall(body)
            if not marked_ids:
                continue
            if len(marked_ids) != 1:
                invalid_pair_ids.update(
                    pair_id for pair_id in marked_ids if pair_id in expected_ids)
                continue
            pair_id = marked_ids[0]
            if pair_id not in expected_ids:
                continue
            note = parse_shadow_note(body, repo=repo, pr=pr)
            if note is None:
                invalid_pair_ids.add(pair_id)
                continue
            reference = references[pair_id]
            if (note["head_sha"] != reference["head_sha"]
                    or note["kind"] != reference["kind"]
                    or note["sample_name"] != reference["sample_name"]):
                # The note does not belong to this exact stable pair. In
                # particular, a moved head cannot be paired with stale A data.
                invalid_pair_ids.add(pair_id)
                continue
            note["expected_head_sha"] = reference["head_sha"]
            candidates.setdefault(pair_id, []).append(note)

    result = []
    for pair_id, rows in candidates.items():
        if pair_id in invalid_pair_ids:
            continue
        distinct = []
        for row in rows:
            if row not in distinct:
                distinct.append(row)
        if len(distinct) == 1:
            result.append(distinct[0])
    return result


def aggregate_pairs(pairs: Iterable[Dict[str, object]]) -> Dict[str, object]:
    """Return raw same-head counts and totals from complete per-run notes."""
    by_id: Dict[str, Dict[str, object]] = {}
    conflicted_ids = set()
    for raw in pairs:
        if not isinstance(raw, dict):
            continue
        pair_id = raw.get("pair_id")
        head_sha = raw.get("head_sha")
        a_head_sha = raw.get("a_head_sha", head_sha)
        b_head_sha = raw.get("b_head_sha", head_sha)
        expected_head_sha = raw.get("expected_head_sha")
        if (not isinstance(pair_id, str) or not pair_id.strip()
                or not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha)
                or not isinstance(a_head_sha, str) or not _SHA_RE.fullmatch(a_head_sha)
                or not isinstance(b_head_sha, str) or not _SHA_RE.fullmatch(b_head_sha)
                or a_head_sha != b_head_sha or head_sha != a_head_sha
                or (expected_head_sha is not None
                    and expected_head_sha != head_sha)):
            continue

        kind = raw.get("kind")
        sample_name = raw.get("sample_name")
        if kind not in ("live", "bad", "good"):
            continue
        if kind == "live":
            if sample_name not in (None, ""):
                continue
            sample_name = None
        elif (sample_name not in reviewer_b.CALIBRATION_NAMES
              or reviewer_b.CALIBRATION_SIDES.get(sample_name) != kind):
            continue

        repo = raw.get("repo")
        pr = raw.get("pr")
        a_verdict = raw.get("a_verdict")
        b_verdict = raw.get("b_verdict")
        if (not isinstance(repo, str) or not _REPOSITORY_RE.fullmatch(repo)
                or isinstance(pr, bool) or not isinstance(pr, int) or pr < 1
                or a_verdict not in ("approved", "rejected")
                or b_verdict not in ("approved", "rejected")):
            continue
        try:
            cost = _decimal(raw.get("cost_dollars"), "reviewer-B cost")
            latency = _decimal(raw.get("latency_seconds"), "reviewer-B latency")
        except PairedTrialError:
            continue

        row = {
            "pair_id": pair_id,
            "repo": repo,
            "pr": pr,
            "kind": kind,
            "sample_name": sample_name,
            "head_sha": head_sha,
            "a_verdict": a_verdict,
            "b_verdict": b_verdict,
            "cost_dollars": cost,
            "latency_seconds": latency,
        }
        previous = by_id.get(pair_id)
        if previous is not None and previous != row:
            conflicted_ids.add(pair_id)
        else:
            by_id[pair_id] = row

    accepted = [
        row for pair_id, row in by_id.items() if pair_id not in conflicted_ids
    ]
    accepted.sort(key=lambda row: row["pair_id"])
    live = [row for row in accepted if row["kind"] == "live"]
    bad = [row for row in accepted if row["kind"] == "bad"]
    good = [row for row in accepted if row["kind"] == "good"]
    costs = sum(
        (row["cost_dollars"] for row in accepted), Decimal("0.000000"))
    latencies = sum(
        (row["latency_seconds"] for row in accepted), Decimal("0.000"))

    return {
        "n": len(accepted),
        "live_n": len(live),
        "live_a_approved": sum(row["a_verdict"] == "approved" for row in live),
        "live_a_rejected": sum(row["a_verdict"] == "rejected" for row in live),
        "live_b_approved": sum(row["b_verdict"] == "approved" for row in live),
        "live_b_rejected": sum(row["b_verdict"] == "rejected" for row in live),
        "bad_n": len(bad),
        "good_n": len(good),
        "bad_a_misses": sum(row["a_verdict"] == "approved" for row in bad),
        "bad_b_catches": sum(
            row["a_verdict"] == "approved" and row["b_verdict"] == "rejected"
            for row in bad),
        "bad_overlap": sum(
            row["a_verdict"] == "rejected" and row["b_verdict"] == "rejected"
            for row in bad),
        "bad_correlated_misses": sum(
            row["a_verdict"] == "approved" and row["b_verdict"] == "approved"
            for row in bad),
        "bad_joint_detection": sum(
            row["a_verdict"] == "rejected" or row["b_verdict"] == "rejected"
            for row in bad),
        "good_a_false_blocks": sum(
            row["a_verdict"] == "rejected" for row in good),
        "good_b_false_blocks": sum(
            row["b_verdict"] == "rejected" for row in good),
        "good_joint_false_blocks": sum(
            row["a_verdict"] == "rejected" or row["b_verdict"] == "rejected"
            for row in good),
        "cost_dollars_total": costs,
        "latency_seconds_total": latencies,
        "pairs": accepted,
    }


def _wilson_interval(successes: int, trials: int
                     ) -> Optional[Tuple[float, float]]:
    """Return a two-sided 95% Wilson score interval, or None for 0 trials."""
    if (isinstance(successes, bool) or not isinstance(successes, int)
            or isinstance(trials, bool) or not isinstance(trials, int)
            or successes < 0 or trials < 0 or successes > trials):
        raise PairedTrialError("paired-trial interval counts are invalid")
    if trials == 0:
        return None

    z = 1.96
    z_squared = z * z
    observed = successes / trials
    denominator = 1 + z_squared / trials
    center = (observed + z_squared / (2 * trials)) / denominator
    spread = z * math.sqrt(
        observed * (1 - observed) / trials
        + z_squared / (4 * trials * trials)
    ) / denominator
    return max(0.0, center - spread), min(1.0, center + spread)


def _rate_summary(successes: int, trials: int) -> str:
    interval = _wilson_interval(successes, trials)
    if interval is None:
        return "not estimable (no A misses)"
    low, high = interval
    return "{:.1%}; 95% Wilson score interval {:.1%} to {:.1%}".format(
        successes / trials, low, high)


def build_trial_report(
    counts: Dict[str, object],
    *,
    bound_closed: bool,
    run: str,
    at: Optional[datetime] = None,
) -> str:
    """Build the final endpoint table from raw same-head aggregate counts."""
    if not isinstance(run, str) or not run.strip():
        raise PairedTrialError("paired-trial report needs its run id")
    if not isinstance(bound_closed, bool) or not bound_closed:
        raise PairedTrialError("paired-trial sampling bound is still open")
    stamp = at or datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise PairedTrialError("paired-trial report timestamp is invalid")

    if not isinstance(counts, dict):
        raise PairedTrialError("paired-trial counts are malformed")
    required_counts = (
        "n", "live_n", "live_a_approved", "live_a_rejected",
        "live_b_approved", "live_b_rejected", "bad_n", "good_n",
        "bad_a_misses",
        "bad_b_catches", "bad_overlap", "bad_correlated_misses",
        "bad_joint_detection", "good_a_false_blocks", "good_b_false_blocks",
        "good_joint_false_blocks",
    )
    values = {}
    for name in required_counts:
        value = counts.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PairedTrialError(
                "paired-trial count {} is invalid".format(name))
        values[name] = value
    cost = _decimal(counts.get("cost_dollars_total"), "cost total")
    latency = _decimal(counts.get("latency_seconds_total"), "latency total")

    live_limit = reviewer_b.LIVE_LIMIT
    bad_limit = sum(
        side == "bad" for side in reviewer_b.CALIBRATION_SIDES.values())
    good_limit = sum(
        side == "good" for side in reviewer_b.CALIBRATION_SIDES.values())
    endpoint_n = live_limit + bad_limit + good_limit
    if (reviewer_b.CALIBRATION_LIMIT != bad_limit + good_limit
            or values["n"] != endpoint_n
            or values["live_n"] != live_limit
            or values["bad_n"] != bad_limit
            or values["good_n"] != good_limit):
        raise PairedTrialError(
            "paired-trial report requires the complete fixed endpoint")
    if values["n"] != values["live_n"] + values["bad_n"] + values["good_n"]:
        raise PairedTrialError("paired-trial endpoint counts do not add up")

    bad_n = values["bad_n"]
    good_n = values["good_n"]
    live_n = values["live_n"]
    if (values["live_a_approved"] + values["live_a_rejected"] != live_n
            or values["live_b_approved"] + values["live_b_rejected"] != live_n):
        raise PairedTrialError("paired-trial live verdict counts are inconsistent")
    a_misses = values["bad_a_misses"]
    b_catches = values["bad_b_catches"]
    overlap = values["bad_overlap"]
    correlated_misses = values["bad_correlated_misses"]
    joint_detection = values["bad_joint_detection"]
    good_a_false_blocks = values["good_a_false_blocks"]
    good_b_false_blocks = values["good_b_false_blocks"]
    good_joint_false_blocks = values["good_joint_false_blocks"]
    if (a_misses > bad_n or b_catches > a_misses
            or correlated_misses > a_misses
            or b_catches + correlated_misses != a_misses
            or overlap > bad_n - a_misses
            or joint_detection != bad_n - correlated_misses
            or good_a_false_blocks > good_n
            or good_b_false_blocks > good_n
            or good_joint_false_blocks > good_n
            or good_joint_false_blocks < max(
                good_a_false_blocks, good_b_false_blocks)
            or good_joint_false_blocks > (
                good_a_false_blocks + good_b_false_blocks)):
        raise PairedTrialError("paired-trial verdict counts are inconsistent")

    bad_b_rejections = b_catches + overlap
    good_a_approvals = good_n - good_a_false_blocks
    good_b_approvals = good_n - good_b_false_blocks
    mean_cost = cost / Decimal(endpoint_n)
    mean_latency = latency / Decimal(endpoint_n)
    lines = [
        TABLE_MARKER,
        "",
        "## Reviewer B paired-trial endpoint report",
        "",
        "The fixed sampling bound is closed at {} live pairs plus {} known-bad "
        "and {} known-good calibration pairs (N = {}). Counts include only "
        "complete same-head pairs; per-run PR notes retain the source verdicts, "
        "head SHA, cost, and latency.".format(
            live_limit, bad_limit, good_limit, endpoint_n),
        "",
        "| Measure | Raw count | Rate or summary |",
        "| --- | ---: | --- |",
        "| Trial endpoint | {} live + {} bad + {} good | closed |".format(
            live_limit, bad_limit, good_limit),
        "| Same-head paired observations (N) | {} | |".format(values["n"]),
        "| Reviewer A verdicts on live pairs | {} approved / {} rejected | |".format(
            values["live_a_approved"], values["live_a_rejected"]),
        "| Reviewer B verdicts on live pairs | {} approved / {} rejected | |".format(
            values["live_b_approved"], values["live_b_rejected"]),
        "| Reviewer A verdicts on known-bad | {} approved / {} rejected | |".format(
            a_misses, bad_n - a_misses),
        "| Reviewer B verdicts on known-bad | {} approved / {} rejected | |".format(
            bad_n - bad_b_rejections, bad_b_rejections),
        "| Reviewer A verdicts on known-good | {} approved / {} rejected | |".format(
            good_a_approvals, good_a_false_blocks),
        "| Reviewer B verdicts on known-good | {} approved / {} rejected | |".format(
            good_b_approvals, good_b_false_blocks),
        "| B catches among A misses on known-bad | {}/{} | {} |".format(
            b_catches, a_misses, _rate_summary(b_catches, a_misses)),
        "| Overlap on known-bad (A and B reject) | {}/{} | {} |".format(
            overlap, bad_n, _rate_summary(overlap, bad_n)),
        "| Correlated misses on known-bad (A and B approve) | {}/{} | {} |".format(
            correlated_misses, bad_n,
            _rate_summary(correlated_misses, bad_n)),
        "| Joint detection on known-bad (A or B reject) | {}/{} | {} |".format(
            joint_detection, bad_n, _rate_summary(joint_detection, bad_n)),
        "| Joint false-blocks on known-good (A or B reject) | {}/{} | {} |".format(
            good_joint_false_blocks, good_n,
            _rate_summary(good_joint_false_blocks, good_n)),
        "| Reviewer B cost | total {}{:.6f}; mean {}{:.6f} per pair | |".format(
            CURRENCY, cost, CURRENCY, mean_cost),
        "| Reviewer B latency | total {:.3f} seconds; mean {:.3f} seconds per pair | |".format(
            latency, mean_latency),
        "",
        "Built by Muse run {} at {}.".format(run.strip(), stamp.isoformat()),
    ]
    return funnel.append_provenance(
        "\n".join(lines) + "\n", "agent",
        at=stamp, run=run.strip(), agent="muse",
    )


def _parent_comments(
    graphql: Callable[..., Dict[str, object]],
) -> Tuple[str, List[Dict[str, str]]]:
    owner, name = PARENT_REPO.split("/", 1)
    cursor = None
    cursors = set()
    issue_id = None
    comments = []
    while True:
        variables = {"owner": owner, "name": name, "number": PARENT_ISSUE}
        if cursor is not None:
            variables["cursor"] = cursor
        data = graphql(_PARENT_COMMENTS_QUERY, **variables)
        repository = data.get("repository") if isinstance(data, dict) else None
        issue = repository.get("issue") if isinstance(repository, dict) else None
        connection = issue.get("comments") if isinstance(issue, dict) else None
        if not isinstance(issue, dict) or not isinstance(connection, dict):
            raise PairedTrialError(
                "could not read comments for {}#{}".format(
                    PARENT_REPO, PARENT_ISSUE))
        current_issue_id = issue.get("id")
        if not isinstance(current_issue_id, str) or not current_issue_id:
            raise PairedTrialError("parent issue id is unreadable")
        if issue_id is not None and issue_id != current_issue_id:
            raise PairedTrialError("parent issue changed during comment pagination")
        issue_id = current_issue_id
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if (not isinstance(nodes, list) or not isinstance(page_info, dict)
                or not isinstance(page_info.get("hasNextPage"), bool)):
            raise PairedTrialError("parent issue comment pagination is unreadable")
        for row in nodes:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not isinstance(row.get("body"), str)):
                raise PairedTrialError("parent issue comment row is unreadable")
            comments.append({"id": row["id"], "body": row["body"]})
        if not page_info["hasNextPage"]:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
            raise PairedTrialError("parent issue comment pagination did not advance")
        cursors.add(next_cursor)
        cursor = next_cursor
    return issue_id, comments


def update_parent_issue_table(
    report_body: str,
    *,
    graphql: Optional[Callable[..., Dict[str, object]]] = None,
) -> Dict[str, str]:
    """Post a built endpoint table, updating its one marked GitHub comment."""
    if (not isinstance(report_body, str)
            or report_body.count(TABLE_MARKER) != 1
            or report_body.count("## Reviewer B paired-trial endpoint report") != 1):
        raise PairedTrialError("built paired-trial report is malformed")
    call = graphql or funnel.gh_graphql
    issue_id, comments = _parent_comments(call)
    matches = [row for row in comments if TABLE_MARKER in row["body"]]
    if len(matches) > 1:
        raise PairedTrialError("parent issue has multiple paired-trial roll-ups")
    if matches:
        comment_id = matches[0]["id"]
        data = call(
            _UPDATE_COMMENT_MUTATION, id=comment_id, body=report_body)
        payload = (data.get("updateIssueComment") or {}).get("issueComment") \
            if isinstance(data, dict) else None
        if (not isinstance(payload, dict) or payload.get("id") != comment_id
                or payload.get("body") != report_body):
            raise PairedTrialError("parent paired-trial roll-up update was not confirmed")
        return {"action": "updated", "comment_id": comment_id}

    data = call(
        _ADD_COMMENT_MUTATION, subjectId=issue_id, body=report_body)
    payload = (data.get("addComment") or {}).get("commentEdge") \
        if isinstance(data, dict) else None
    node = payload.get("node") if isinstance(payload, dict) else None
    comment_id = node.get("id") if isinstance(node, dict) else None
    if not isinstance(comment_id, str) or not comment_id:
        raise PairedTrialError("parent paired-trial roll-up creation was not confirmed")
    return {"action": "created", "comment_id": comment_id}


def refresh_parent_issue_table(*, run: str,
                               at: Optional[datetime] = None
                               ) -> Optional[Tuple[Dict[str, object], Dict[str, str]]]:
    """Post the report only after the fixed paired-trial bound has closed."""
    state = heartbeat.reviewer_b_state()
    if (not isinstance(state, dict) or state.get("trial_id") != TRIAL_ID
            or not isinstance(state.get("sampling_open"), bool)):
        raise PairedTrialError("reviewer-B sampling bound is unreadable")
    if state["sampling_open"]:
        return None
    references = state.get("pairs") if isinstance(state, dict) else None
    if not isinstance(references, list):
        raise PairedTrialError("reviewer-B heartbeat pairs are unreadable")
    notes = collect_pair_notes(references)
    counts = aggregate_pairs(notes)
    report_body = build_trial_report(
        counts, bound_closed=True, run=run, at=at)
    result = update_parent_issue_table(report_body)
    return counts, result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    update = sub.add_parser("update")
    update.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    try:
        refreshed = refresh_parent_issue_table(run=args.run)
    except (PairedTrialError, funnel.GitHubError, heartbeat.HeartbeatError) as exc:
        print("paired-trial-recorder: {}".format(exc), file=sys.stderr)
        return 1
    if refreshed is None:
        print(json.dumps({"action": "waiting", "sampling_open": True}))
        return 0
    counts, result = refreshed
    print(json.dumps({
        "action": result["action"],
        "comment_id": result["comment_id"],
        "n": counts["n"],
        "bad_n": counts["bad_n"],
        "good_n": counts["good_n"],
        "live_n": counts["live_n"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
