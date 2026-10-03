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
    for name, record in records.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise PacketSetError("packet set manifest entry is invalid")
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
        contents[name] = raw
        digests[name] = actual
    return manifest, contents, digests


def verify_checksums(version: str = "v1") -> Dict[str, str]:
    """Verify every packet in a frozen set and return its SHA-256 digests."""
    _manifest, _contents, digests = _read_packet_set(version)
    return digests


def load_packet_set(version: str = "v1") -> Dict[str, dict]:
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


def load_packet(name: str, version: str = "v1") -> dict:
    """Load one named packet from a frozen set after verifying the full set."""
    if not isinstance(name, str):
        raise PacketSetError("unknown packet name")
    packets = load_packet_set(version)
    try:
        return packets[name]
    except KeyError as exc:
        raise PacketSetError("unknown packet name") from exc
