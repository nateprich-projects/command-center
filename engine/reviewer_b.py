"""Trial bookkeeping for the nonblocking Muse reviewer-B shadow run."""

from __future__ import annotations

import html
import json
import math
import argparse
import pathlib
import re
from typing import Dict, Iterable, List, Optional


TRIAL_ID = "reviewer-b-2250-v1"
LIVE_LIMIT = 30
CALIBRATION_LIMIT = 20
CALIBRATION_VERSION = "v2"
CALIBRATION_NAMES = (
    "must_reject",
    *("bad_{:02d}".format(index) for index in range(1, 10)),
    "must_approve",
    *("good_{:02d}".format(index) for index in range(1, 10)),
)
CALIBRATION_SIDES = {
    **{"must_reject": "bad", "must_approve": "good"},
    **{"bad_{:02d}".format(index): "bad" for index in range(1, 10)},
    **{"good_{:02d}".format(index): "good" for index in range(1, 10)},
}
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_VERDICTS = {"approved", "rejected"}
_RESERVATION_TTL_SECONDS = 2 * 60 * 60


class ReviewerBError(ValueError):
    """A reviewer-B trial record or answer is malformed."""


def validate_answer(raw: str) -> Dict[str, object]:
    """Validate B's independent, notes-only verdict object."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ReviewerBError("answer is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReviewerBError("answer must be a JSON object")
    verdict = value.get("verdict")
    findings = value.get("findings")
    if verdict not in _VERDICTS:
        raise ReviewerBError("verdict must be approved or rejected")
    if (not isinstance(findings, list) or len(findings) > 12
            or any(not isinstance(item, str) or not item.strip()
                   for item in findings)):
        raise ReviewerBError("findings must be a list of nonblank strings")
    return {
        "verdict": verdict,
        "findings": [item.strip()[:1000] for item in findings],
    }


def _records(records: Iterable[Dict]) -> List[Dict]:
    """Drop duplicate heartbeat rows without collapsing conflicting rows."""
    found = []
    seen = set()
    for row in records:
        if not isinstance(row, dict):
            continue
        try:
            key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(row)
    return found


def _timestamp(record: Dict) -> Optional[float]:
    value = record.get("ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _usage_reading(record: Dict) -> Optional[tuple]:
    usage = record.get("usage")
    window = usage.get("seven_day") if isinstance(usage, dict) else None
    if not isinstance(window, dict):
        return None
    reset = window.get("resets_at")
    spent = window.get("spent_dollars")
    if (isinstance(reset, bool) or not isinstance(reset, (int, float))
            or isinstance(spent, bool) or not isinstance(spent, (int, float))):
        return None
    reset, spent = float(reset), float(spent)
    if not math.isfinite(reset) or not math.isfinite(spent) or reset < 0 or spent < 0:
        return None
    return reset, spent


def pair_rows(records: Iterable[Dict]) -> List[Dict[str, object]]:
    """Return valid stable pairs measured from their heartbeat start/end rows."""
    grouped: Dict[str, Dict[str, List[Dict]]] = {}
    for row in _records(records):
        if (row.get("agent") != "muse"
                or row.get("trial_id") != TRIAL_ID
                or row.get("phase") not in ("shadow_pair_start", "shadow_pair_finish")):
            continue
        pair_id = row.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id.strip():
            continue
        phase = "start" if row["phase"] == "shadow_pair_start" else "finish"
        grouped.setdefault(pair_id, {"start": [], "finish": []})[phase].append(row)

    pairs = []
    for pair_id, sides in grouped.items():
        if len(sides["start"]) != 1 or len(sides["finish"]) != 1:
            continue
        start, finish = sides["start"][0], sides["finish"][0]
        identity_fields = ("run", "kind", "repo", "pr", "head_sha", "sample_name")
        if any(start.get(field) != finish.get(field) for field in identity_fields):
            continue
        if finish.get("stable") is not True:
            continue
        kind = start.get("kind")
        repo = start.get("repo")
        pr = start.get("pr")
        sha = start.get("head_sha")
        if (kind not in ("live", "bad", "good")
                or not isinstance(repo, str) or not repo.strip()
                or isinstance(pr, bool) or not isinstance(pr, int) or pr < 1
                or not isinstance(sha, str) or not _SHA_RE.fullmatch(sha)):
            continue
        if kind == "live":
            if start.get("sample_name") not in (None, ""):
                continue
        elif start.get("sample_name") not in CALIBRATION_NAMES or CALIBRATION_SIDES.get(start.get("sample_name")) != kind:
            continue
        if (start.get("a_verdict") not in _VERDICTS
                or finish.get("a_verdict") != start.get("a_verdict")
                or finish.get("b_verdict") not in _VERDICTS):
            continue
        start_at, finish_at = _timestamp(start), _timestamp(finish)
        start_reading, finish_reading = _usage_reading(start), _usage_reading(finish)
        if (start_at is None or finish_at is None or finish_at < start_at
                or start_reading is None or finish_reading is None
                or start_reading[0] != finish_reading[0]
                or finish_reading[1] < start_reading[1]):
            continue
        pairs.append({
            "pair_id": pair_id,
            "run": start.get("run"),
            "kind": kind,
            "repo": repo,
            "pr": pr,
            "head_sha": sha,
            "sample_name": start.get("sample_name") or None,
            "a_verdict": start["a_verdict"],
            "b_verdict": finish["b_verdict"],
            "started_at": start_at,
            "finished_at": finish_at,
            "latency_seconds": round(finish_at - start_at, 3),
            "cost_dollars": round(finish_reading[1] - start_reading[1], 6),
        })
    return sorted(pairs, key=lambda pair: (pair["finished_at"], pair["pair_id"]))


def trial_state(records: Iterable[Dict], *, now: Optional[float] = None
                ) -> Dict[str, object]:
    """Count completed pairs and live reservations from heartbeat records.

    The two-hour reservation recovery only releases a slot after a dead outer
    run; it is not a time-based trial stop or quality gate.
    """
    import time

    pairs = pair_rows(records)
    live = {}
    calibration = {}
    for pair in pairs:
        if pair["kind"] == "live":
            key = (pair["repo"], pair["pr"], pair["head_sha"])
            live.setdefault(key, pair)
        else:
            calibration.setdefault(pair["sample_name"], pair)

    rows = _records(records)
    finished_runs = {
        row.get("run") for row in rows
        if row.get("agent") == "muse" and row.get("phase") == "finish"
        and isinstance(row.get("run"), str)
    }
    ended_pairs = {
        row.get("pair_id") for row in rows
        if row.get("agent") == "muse"
        and row.get("phase") == "shadow_pair_finish"
        and row.get("trial_id") == TRIAL_ID
        and isinstance(row.get("pair_id"), str)
    }
    now = time.time() if now is None else now
    live_reserved = {}
    calibration_reserved = {}
    for row in rows:
        if (row.get("agent") != "muse" or row.get("trial_id") != TRIAL_ID
                or row.get("phase") != "shadow_reservation"):
            continue
        pair_id = row.get("pair_id")
        run = row.get("run")
        stamp = _timestamp(row)
        if (not isinstance(pair_id, str) or pair_id in ended_pairs
                or run in finished_runs or stamp is None
                or now - stamp > _RESERVATION_TTL_SECONDS):
            continue
        identity = (row.get("kind"), row.get("repo"), row.get("pr"),
                    row.get("head_sha"), row.get("sample_name"))
        if row.get("kind") == "live":
            if (isinstance(row.get("repo"), str)
                    and isinstance(row.get("pr"), int)
                    and isinstance(row.get("head_sha"), str)):
                live_reserved.setdefault(identity, row)
        elif (row.get("kind") in ("bad", "good")
              and row.get("sample_name") in CALIBRATION_NAMES
              and CALIBRATION_SIDES.get(row.get("sample_name")) == row.get("kind")):
            calibration_reserved.setdefault(row.get("sample_name"), row)

    # A completed pair supersedes any duplicate reservation for that identity.
    for identity in live:
        live_reserved.pop(identity, None)
    for name in calibration:
        calibration_reserved.pop(name, None)

    live_rows = list(live.values())
    calibration_rows = [calibration[name] for name in CALIBRATION_NAMES
                        if name in calibration]
    live_used = len(live_rows) + len(live_reserved)
    calibration_used = len(calibration_rows) + len(calibration_reserved)
    return {
        "trial_id": TRIAL_ID,
        "live_limit": LIVE_LIMIT,
        "live_count": min(len(live_rows), LIVE_LIMIT),
        "live_reserved": min(len(live_reserved), LIVE_LIMIT),
        "live_used_count": min(live_used, LIVE_LIMIT),
        "calibration_limit": CALIBRATION_LIMIT,
        "calibration_count": min(len(calibration_rows), CALIBRATION_LIMIT),
        "calibration_reserved": min(len(calibration_reserved), CALIBRATION_LIMIT),
        "calibration_used_count": min(calibration_used, CALIBRATION_LIMIT),
        "calibration_names": [row["sample_name"] for row in calibration_rows],
        "calibration_reserved_names": list(
            name for name in CALIBRATION_NAMES if name in calibration_reserved),
        "live_identities": [
            {"repo": row["repo"], "pr": row["pr"], "head_sha": row["head_sha"]}
            for row in live_rows
        ],
        "next_calibration": next((name for name in CALIBRATION_NAMES
                                  if name not in calibration
                                  and name not in calibration_reserved), None),
        "sampling_open": (live_used < LIVE_LIMIT
                          or calibration_used < CALIBRATION_LIMIT),
        "pairs": pairs,
    }


def reserve_decision(records: Iterable[Dict], *, run: str, pair_id: str,
                     kind: str, repo: str, pr: int, head_sha: str,
                     sample_name: Optional[str] = None,
                     now: Optional[float] = None) -> str:
    """Return whether an atomic GitHub reservation can claim this sample."""
    if (not isinstance(run, str) or not run.strip()
            or not isinstance(pair_id, str) or not pair_id.strip()
            or kind not in ("live", "bad", "good")
            or not isinstance(repo, str) or not repo.strip()
            or isinstance(pr, bool) or not isinstance(pr, int) or pr < 1
            or not isinstance(head_sha, str) or not _SHA_RE.fullmatch(head_sha)):
        raise ReviewerBError("reviewer-B reservation identity is invalid")
    if kind == "live":
        if sample_name not in (None, ""):
            raise ReviewerBError("live reservation cannot name a calibration")
        sample_name = None
    elif (sample_name not in CALIBRATION_NAMES
          or CALIBRATION_SIDES.get(sample_name) != kind):
        raise ReviewerBError("calibration reservation identity is invalid")

    state = trial_state(records, now=now)
    identity = {"repo": repo, "pr": pr, "head_sha": head_sha}
    if kind == "live":
        if any(isinstance(row, dict) and row.get("pair_id") == pair_id
               and row.get("phase") == "shadow_reservation"
               and row.get("trial_id") == TRIAL_ID for row in records):
            return "reserved"
        if identity in state["live_identities"]:
            return "duplicate"
        for row in records:
            if (isinstance(row, dict) and row.get("phase") == "shadow_reservation"
                    and row.get("trial_id") == TRIAL_ID
                    and row.get("kind") == "live"
                    and row.get("repo") == repo and row.get("pr") == pr
                    and row.get("head_sha") == head_sha
                    and row.get("run") != run):
                row_state = trial_state([row], now=now)
                if row_state["live_reserved"]:
                    return "busy"
        if state["live_used_count"] >= LIVE_LIMIT:
            return "closed"
    else:
        if any(isinstance(row, dict) and row.get("pair_id") == pair_id
               and row.get("phase") == "shadow_reservation"
               and row.get("trial_id") == TRIAL_ID for row in records):
            return "reserved"
        if sample_name in state["calibration_names"]:
            return "duplicate"
        if sample_name != state["next_calibration"]:
            return ("busy" if sample_name in state["calibration_reserved_names"]
                    else "closed")
        if state["calibration_used_count"] >= CALIBRATION_LIMIT:
            return "closed"
    if state["live_used_count"] + state["calibration_used_count"] >= (
            LIVE_LIMIT + CALIBRATION_LIMIT):
        return "closed"
    return "reserve"


def render_note(pair: Dict[str, object]) -> str:
    """Render the required nonblocking PR note, escaping model-authored text."""
    required = ("pair_id", "kind", "repo", "pr", "head_sha", "a_verdict",
                "b_verdict", "cost_dollars", "latency_seconds")
    if any(name not in pair for name in required):
        raise ReviewerBError("paired note is missing required fields")
    if pair["a_verdict"] not in _VERDICTS or pair["b_verdict"] not in _VERDICTS:
        raise ReviewerBError("paired note has an invalid verdict")
    if (not isinstance(pair["cost_dollars"], (int, float))
            or not math.isfinite(float(pair["cost_dollars"]))
            or not isinstance(pair["latency_seconds"], (int, float))
            or not math.isfinite(float(pair["latency_seconds"]))):
        raise ReviewerBError("paired note has unreadable cost or latency")
    label = "live" if pair["kind"] == "live" else "calibration {}".format(
        pair.get("sample_name") or "")
    findings = pair.get("findings") or []
    if not isinstance(findings, list):
        raise ReviewerBError("findings must be a list")
    lines = [
        "### Reviewer B shadow note ({})".format(label),
        "",
        "Nonblocking observation; this note does not approve, reject, or block merging.",
        "",
        "- Reviewer A: `{}`".format(pair["a_verdict"]),
        "- Reviewer B: `{}`".format(pair["b_verdict"]),
        "- Head SHA: `{}`".format(pair["head_sha"]),
        "- Reviewer B cost: ${:.6f} own-card usage delta from heartbeat records.".format(
            float(pair["cost_dollars"])),
        "- Reviewer B latency: {:.3f} seconds from paired heartbeat timestamps.".format(
            float(pair["latency_seconds"])),
    ]
    if findings:
        lines.extend(["", "Reviewer B findings:"])
        lines.extend("- {}".format(html.escape(str(item)[:1000])) for item in findings[:12])
    lines.extend(["", "<!-- {} pair={} -->".format(TRIAL_ID, pair["pair_id"])])
    return "\n".join(lines)


def write_calibration_packet(name: str, output: str) -> None:
    """Write one checksum-verified fixed v2 packet to an ephemeral run file."""
    from engine import review_packets

    if name not in CALIBRATION_NAMES:
        raise ReviewerBError("unknown calibration packet")
    try:
        packet = review_packets.load_packet(name, CALIBRATION_VERSION)
    except review_packets.PacketSetError as exc:
        raise ReviewerBError("calibration packet set is unavailable") from exc
    path = pathlib.Path(output)
    path.write_text(json.dumps(packet, separators=(",", ":")) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    answer = sub.add_parser("answer")
    answer.add_argument("source")
    answer.add_argument("output")
    calibration = sub.add_parser("calibration")
    calibration.add_argument("name")
    calibration.add_argument("output")
    note = sub.add_parser("note")
    note.add_argument("state")
    note.add_argument("pair_id")
    note.add_argument("answer")
    note.add_argument("output")
    args = parser.parse_args(argv)
    try:
        if args.command == "answer":
            normalized = validate_answer(pathlib.Path(args.source).read_text())
            pathlib.Path(args.output).write_text(
                json.dumps(normalized, separators=(",", ":")) + "\n")
            return 0
        if args.command == "calibration":
            write_calibration_packet(args.name, args.output)
            return 0
        state = json.loads(pathlib.Path(args.state).read_text())
        answer_data = validate_answer(pathlib.Path(args.answer).read_text())
        pairs = state.get("pairs") if isinstance(state, dict) else None
        if not isinstance(pairs, list):
            raise ReviewerBError("paired state is malformed")
        pair = next((row for row in pairs
                     if isinstance(row, dict)
                     and row.get("pair_id") == args.pair_id), None)
        if pair is None:
            raise ReviewerBError("measured pair is unavailable")
        note_data = dict(pair)
        note_data["findings"] = answer_data["findings"]
        pathlib.Path(args.output).write_text(render_note(note_data) + "\n")
        return 0
    except (OSError, ValueError, ReviewerBError) as exc:
        print("reviewer-B: {}".format(exc), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
