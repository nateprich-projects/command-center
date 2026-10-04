import shutil

import pytest

from engine import review_packets


EXPECTED_V1_DIGESTS = {
    "must_approve": "2e6c601ea1625d07fdc50075a26b1095c435a602bcd226dd0820d3feb5187eda",
    "must_reject": "d292e9981a1c7dba768c1c659e0ead270d3917d63e708826f6650e8589c521a4",
}


def test_load_packet_set_loads_both_frozen_v1_packets():
    packets = review_packets.load_packet_set()

    assert set(packets) == {"must_approve", "must_reject"}
    assert (packets["must_reject"]["repo"], packets["must_reject"]["pr"]) == (
        "nateprich-projects/The-League", 237
    )
    assert (packets["must_approve"]["repo"], packets["must_approve"]["pr"]) == (
        "nateprich-projects/command-center", 1959
    )


def test_load_packet_returns_the_named_v1_packet():
    packet = review_packets.load_packet("must_reject")

    assert (packet["repo"], packet["pr"]) == (
        "nateprich-projects/The-League", 237
    )


def test_verify_checksums_matches_the_independently_recorded_v1_digests():
    assert review_packets.verify_checksums() == EXPECTED_V1_DIGESTS


def test_verify_checksums_rejects_a_changed_packet_file(tmp_path, monkeypatch):
    packet_root = tmp_path / "review_packets"
    shutil.copytree(review_packets._PACKET_ROOT / "v1", packet_root / "v1")
    packet_path = packet_root / "v1" / "must_reject.json"
    packet_path.write_bytes(packet_path.read_bytes() + b"\n")
    monkeypatch.setattr(review_packets, "_PACKET_ROOT", packet_root)

    with pytest.raises(review_packets.PacketSetError, match="checksum mismatch"):
        review_packets.verify_checksums()


def test_load_packet_rejects_an_unknown_name():
    with pytest.raises(review_packets.PacketSetError, match="unknown packet name"):
        review_packets.load_packet("unregistered")
