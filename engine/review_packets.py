"""Load the frozen review packet sets shared by review evaluations."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from datetime import datetime
from typing import Dict


_PACKET_ROOT = pathlib.Path(__file__).resolve().parents[1] / "data" / "review_packets"
_VERSION_RE = re.compile(r"v[1-9][0-9]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_VERSION = "v1"
_HEAD_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHORT_SHA_RE = re.compile(r"[0-9a-f]{12}\Z")
_V2_SIDES = {"bad", "good"}
_V2_REFERENCES = {
    "must_reject": ("must_reject", "bad"),
    "must_approve": ("must_approve", "good"),
}


class PacketSetError(ValueError):
    """A frozen review packet set is missing or failed integrity checks."""


def _validate_v2_selection(manifest: dict, records: dict) -> None:
    selection = manifest.get("selection")
    recurrence = (selection.get("fix_recurrence_snapshot")
                  if isinstance(selection, dict) else None)
    if not isinstance(recurrence, dict):
        raise PacketSetError("v2 recurrence snapshot evidence is missing")

    examples = recurrence.get("examples")
    if (recurrence.get("window_days") != 7 or
            recurrence.get("example_count") != 44 or
            not isinstance(examples, list) or len(examples) != 44):
        raise PacketSetError("v2 recurrence snapshot examples are incomplete")
    history_bounds = recurrence.get("history_bounds")
    history_window = selection.get("history_window")
    if (recurrence.get("generated_at") !=
            selection.get("snapshot_generated_at") or
            not isinstance(history_bounds, dict) or
            not isinstance(history_window, dict) or
            history_bounds.get("start") != history_window.get("start") or
            history_bounds.get("end") != history_window.get("end")):
        raise PacketSetError("v2 recurrence snapshot window is inconsistent")
    example_by_sha = {}
    for example in examples:
        if not isinstance(example, dict):
            raise PacketSetError("v2 recurrence example is invalid")
        short_sha = example.get("sha")
        commit_sha = example.get("commit_sha")
        ticket = example.get("ticket")
        if (not isinstance(short_sha, str) or
                not _SHORT_SHA_RE.fullmatch(short_sha) or
                not isinstance(commit_sha, str) or
                not _HEAD_SHA_RE.fullmatch(commit_sha) or
                not commit_sha.startswith(short_sha) or
                not isinstance(ticket, int) or isinstance(ticket, bool) or
                ticket <= 0 or short_sha in example_by_sha):
            raise PacketSetError("v2 recurrence example identity is invalid")
        example_by_sha[short_sha] = example

    candidates = (selection.get("known_bad_candidates")
                  if isinstance(selection, dict) else None)
    if (selection.get("known_bad_candidate_count") != 53 or
            not isinstance(candidates, list) or len(candidates) != 53):
        raise PacketSetError("v2 known-bad candidate ranking is incomplete")

    candidate_by_rank = {}
    mapped_examples = set()
    seen_prs = set()
    previous_merged_at = None
    for expected_rank, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict) or candidate.get("rank") != expected_rank:
            raise PacketSetError("v2 known-bad candidate ranks are invalid")
        pr = candidate.get("pr")
        source_ticket = candidate.get("source_ticket")
        source_sha = candidate.get("source_commit_sha")
        merged_at = candidate.get("merged_at")
        if (not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0 or
                pr in seen_prs or
                not isinstance(source_ticket, int) or
                isinstance(source_ticket, bool) or source_ticket <= 0 or
                not isinstance(source_sha, str) or
                not _HEAD_SHA_RE.fullmatch(source_sha) or
                not isinstance(merged_at, str)):
            raise PacketSetError("v2 known-bad candidate identity is invalid")
        try:
            merged_time = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PacketSetError("v2 known-bad candidate time is invalid") from exc
        if (merged_time.tzinfo is None or
                (previous_merged_at is not None and
                 merged_time > previous_merged_at)):
            raise PacketSetError("v2 known-bad candidates are not newest first")
        previous_merged_at = merged_time
        seen_prs.add(pr)

        later_fixes = candidate.get("later_fixes")
        if not isinstance(later_fixes, list) or not later_fixes:
            raise PacketSetError("v2 candidate later-fix mapping is missing")
        fix_by_example = {}
        for fix in later_fixes:
            if not isinstance(fix, dict):
                raise PacketSetError("v2 candidate later-fix mapping is invalid")
            example_sha = fix.get("example_sha")
            fix_ticket = fix.get("fix_ticket")
            fix_commit_sha = fix.get("fix_commit_sha")
            example = example_by_sha.get(example_sha)
            if (example is None or
                    not isinstance(fix_ticket, int) or
                    isinstance(fix_ticket, bool) or
                    not isinstance(fix_commit_sha, str) or
                    not _HEAD_SHA_RE.fullmatch(fix_commit_sha) or
                    example.get("ticket") != fix_ticket or
                    example.get("commit_sha") != fix_commit_sha or
                    example_sha in fix_by_example):
                raise PacketSetError("v2 candidate later-fix example is unmatched")
            fix_by_example[example_sha] = fix
            mapped_examples.add(example_sha)

        if expected_rank <= 9:
            proofs = candidate.get("blame_proof")
            if not isinstance(proofs, list) or len(proofs) != len(later_fixes):
                raise PacketSetError("v2 selected candidate blame proof is missing")
            proof_examples = set()
            for proof in proofs:
                if not isinstance(proof, dict):
                    raise PacketSetError("v2 selected candidate blame proof is invalid")
                example_sha = proof.get("example_sha")
                lines = proof.get("blamed_lines")
                if (example_sha not in fix_by_example or
                        example_sha in proof_examples or
                        proof.get("fix_ticket") !=
                        fix_by_example[example_sha].get("fix_ticket") or
                        proof.get("fix_commit_sha") !=
                        fix_by_example[example_sha].get("fix_commit_sha") or
                        proof.get("source_commit_sha") != source_sha or
                        not isinstance(proof.get("fix_parent_sha"), str) or
                        not _HEAD_SHA_RE.fullmatch(proof["fix_parent_sha"]) or
                        not isinstance(lines, list) or not lines):
                    raise PacketSetError("v2 selected candidate blame proof is unmatched")
                for line in lines:
                    if (not isinstance(line, dict) or
                            not isinstance(line.get("path"), str) or
                            not line["path"].strip() or
                            not isinstance(line.get("parent_line"), int) or
                            isinstance(line.get("parent_line"), bool) or
                            line["parent_line"] <= 0):
                        raise PacketSetError("v2 selected candidate blame line is invalid")
                    if line.get("commit_sha") != source_sha:
                        raise PacketSetError(
                            "v2 selected candidate blame line does not match source commit"
                        )
                proof_examples.add(example_sha)
            if proof_examples != set(fix_by_example):
                raise PacketSetError("v2 selected candidate blame proof is incomplete")

        candidate_by_rank[expected_rank] = candidate

    if mapped_examples != set(example_by_sha):
        raise PacketSetError("v2 recurrence examples do not map to source PRs")

    for index in range(1, 10):
        name = f"bad_{index:02d}"
        record = records.get(name)
        candidate = candidate_by_rank[index]
        source = record.get("source") if isinstance(record, dict) else None
        expected_fix_examples = [
            {"sha": fix["example_sha"], "ticket": fix["fix_ticket"],
             "commit_sha": fix["fix_commit_sha"]}
            for fix in candidate["later_fixes"]
        ]
        if (not isinstance(source, dict) or
                record.get("pr") != candidate["pr"] or
                source.get("rank") != index or
                source.get("ticket") != candidate["source_ticket"] or
                source.get("commit_sha") != candidate["source_commit_sha"] or
                source.get("fix_examples") != expected_fix_examples):
            raise PacketSetError("v2 selected packet source does not match its candidate")


def _read_packet_set(version: str) -> tuple[dict, Dict[str, bytes], Dict[str, str]]:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise PacketSetError("unknown packet set version")

    version_dir = _PACKET_ROOT / version
    try:
        manifest = json.loads((version_dir / "manifest.json").read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PacketSetError("packet set manifest is unavailable") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != version:
        raise PacketSetError("packet set manifest version does not match")

    records = manifest.get("packets")
    if not isinstance(records, dict) or not records:
        raise PacketSetError("packet set manifest has no packets")

    resolved_dir = version_dir.resolve()
    contents: Dict[str, bytes] = {}
    digests: Dict[str, str] = {}
    sides = {"bad": 0, "good": 0}
    seen_pr_heads = set()
    seen_prs = set()
    for name, record in records.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise PacketSetError("packet set manifest entry is invalid")

        if version == "v2":
            side = record.get("side")
            reason = record.get("reason")
            if (not isinstance(side, str) or side not in _V2_SIDES or
                    not isinstance(reason, str) or not reason.strip()):
                raise PacketSetError("v2 packet side or reason is missing")
            expected_side = (
                _V2_REFERENCES[name][1] if name in _V2_REFERENCES else
                "bad" if name.startswith("bad_") else
                "good" if name.startswith("good_") else None
            )
            if side != expected_side:
                raise PacketSetError("v2 packet side does not match its name")
            sides[side] += 1

            reference = record.get("reference")
            if reference is not None:
                expected_reference = _V2_REFERENCES.get(name)
                if (expected_reference is None or not isinstance(reference, dict) or
                        reference.get("version") != "v1" or
                        reference.get("name") != expected_reference[0] or
                        side != expected_reference[1] or
                        set(record) != {"reference", "sha256", "side", "reason"}):
                    raise PacketSetError("v2 packet reference is invalid")
                _ref_manifest, ref_contents, ref_digests = _read_packet_set("v1")
                ref_name = reference["name"]
                expected = record.get("sha256")
                if (ref_name not in ref_contents or
                        expected != ref_digests.get(ref_name)):
                    raise PacketSetError("v2 packet reference checksum mismatch")
                contents[name] = ref_contents[ref_name]
                digests[name] = ref_digests[ref_name]
                continue

            pr_number = record.get("pr")
            head_sha = record.get("head_sha")
            if (not isinstance(pr_number, int) or isinstance(pr_number, bool) or
                    pr_number <= 0 or not isinstance(head_sha, str) or
                    not _HEAD_SHA_RE.fullmatch(head_sha)):
                raise PacketSetError("v2 packet PR or head SHA is missing")
            identity = (pr_number, head_sha)
            if identity in seen_pr_heads or pr_number in seen_prs:
                raise PacketSetError("v2 packet PR and head SHA are duplicated")
            seen_pr_heads.add(identity)
            seen_prs.add(pr_number)

        filename = record.get("file")
        expected = record.get("sha256")
        if (not isinstance(filename, str) or not filename or
                "/" in filename or "\\" in filename or
                not isinstance(expected, str) or
                not _SHA256_RE.fullmatch(expected)):
            raise PacketSetError("packet set manifest entry is invalid")
        try:
            path = (version_dir / filename).resolve(strict=True)
            path.relative_to(resolved_dir)
            raw = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise PacketSetError("packet file is unavailable") from exc
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise PacketSetError("packet checksum mismatch")
        if version == "v2":
            try:
                packet = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise PacketSetError("packet file is not valid JSON") from exc
            if (not isinstance(packet, dict) or
                    packet.get("repo") != "nateprich-projects/command-center" or
                    packet.get("pr") != record["pr"] or
                    packet.get("head_sha") != record["head_sha"]):
                raise PacketSetError("v2 packet identity does not match its manifest")
        contents[name] = raw
        digests[name] = actual
    if version == "v2":
        expected_names = (
            set(_V2_REFERENCES) |
            {f"bad_{index:02d}" for index in range(1, 10)} |
            {f"good_{index:02d}" for index in range(1, 10)}
        )
        if (set(records) != expected_names or sides != {"bad": 10, "good": 10}):
            raise PacketSetError("v2 packet set must contain 10 bad and 10 good packets")
        _validate_v2_selection(manifest, records)
    return manifest, contents, digests


def verify_checksums(version: str = DEFAULT_VERSION) -> Dict[str, str]:
    """Verify every packet in a frozen set and return its SHA-256 digests."""
    _manifest, _contents, digests = _read_packet_set(version)
    return digests


def load_packet_set(version: str = DEFAULT_VERSION) -> Dict[str, dict]:
    """Load all packets in a set after verifying their recorded checksums."""
    _manifest, contents, _digests = _read_packet_set(version)
    packets = {}
    for name, raw in contents.items():
        try:
            packet = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PacketSetError("packet file is not valid JSON") from exc
        if not isinstance(packet, dict):
            raise PacketSetError("packet file must contain a JSON object")
        packets[name] = packet
    return packets


def load_packet(name: str, version: str = DEFAULT_VERSION) -> dict:
    """Load one named packet from a frozen set after verifying the full set."""
    if not isinstance(name, str):
        raise PacketSetError("unknown packet name")
    packets = load_packet_set(version)
    try:
        return packets[name]
    except KeyError as exc:
        raise PacketSetError("unknown packet name") from exc
