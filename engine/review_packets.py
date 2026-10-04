"""Load the frozen review packet sets shared by review evaluations."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Dict


_PACKET_ROOT = pathlib.Path(__file__).resolve().parents[1] / "data" / "review_packets"
_VERSION_RE = re.compile(r"v[1-9][0-9]*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_VERSION = "v1"
_HEAD_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_V2_SIDES = {"bad", "good"}
_V2_REFERENCES = {
    "must_reject": ("must_reject", "bad"),
    "must_approve": ("must_approve", "good"),
}


class PacketSetError(ValueError):
    """A frozen review packet set is missing or failed integrity checks."""


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
