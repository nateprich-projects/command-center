import json
import shutil
from collections import Counter

import pytest

from engine import review_packets


EXPECTED_V1_DIGESTS = {
    "must_approve": "2e6c601ea1625d07fdc50075a26b1095c435a602bcd226dd0820d3feb5187eda",
    "must_reject": "d292e9981a1c7dba768c1c659e0ead270d3917d63e708826f6650e8589c521a4",
}

EXPECTED_V2_DIGESTS = {
    "must_approve": "2e6c601ea1625d07fdc50075a26b1095c435a602bcd226dd0820d3feb5187eda",
    "must_reject": "d292e9981a1c7dba768c1c659e0ead270d3917d63e708826f6650e8589c521a4",
    "bad_01": "d823c78e74db074b7ce1d241be8bbed05f6d385f3a56ace09c305ce5ecd14472",
    "bad_02": "4dd366211b3c312cdc92a28b937db5160ffbcf720b62f12f32b1cb2ba5449291",
    "bad_03": "f25eb38bedbffbf56f9fc3c70c4957691a7b6e3b1f8e4a924086f47935f73337",
    "bad_04": "a4893a750f11d73b4d1940ed262e17ed2bd42825531048512aff6d9bbbb6ddd7",
    "bad_05": "4714d534f3a8ded80d869eaf5ae7b206116192bb90534f7551f9034508b211a7",
    "bad_06": "6a743eab8113ff32a7515cc8180e5b3050ef01545181ad43d528b2e77cc3e97c",
    "bad_07": "4fbcf34be57305ab1fbb1fcdce824f140791f4e23a078b84ddb998f3f1587cad",
    "bad_08": "650b07207106bff8aae9eaaa87f313163b712af09e7138362ebc78efdf60baa4",
    "bad_09": "cec5fdcf3588e3cdfaf09e1b34c721c6162bf231d83d6e03950a99bc467a8057",
    "good_01": "ec056a4c382dcf3ebc4bdb71a3cb5afcb9ac0c7b9d499e04c176e5ebf3a628aa",
    "good_02": "c12992bf6ddb54672b256d1386a632c016991b0aaba57d87b62d3aa13e7b880d",
    "good_03": "cded7a92e3d13b231f74a200bfa7a5040b6cdba3b86e274fb4d92a4b6aadeb89",
    "good_04": "c5bdfa421cf85bb5583893c6575378591b97fd679667dea8e658f55cccd9e593",
    "good_05": "d28fd83f89e8367afa0909d512611748587ec1c2cb6f61577b3cf530165dcf33",
    "good_06": "01116e0b863286ab5a62a877f71e0ef5033ad2bc089ff336317c59f2569ea815",
    "good_07": "7881fed5e4a9e4d17259816c510d78f2ba3e02b52536c42fee95d41c9616e031",
    "good_08": "84e6aab7187afcbbb68801f84d8a50cf97c87bd4bc9a85d02e468b23b0d14376",
    "good_09": "a409a740661aa8b3edc843e7d80e7b8d02ad63c1396947c8cafc449fe8af53d6",
}

EXPECTED_BAD_PRS = [2191, 2091, 2030, 2025, 1973, 1958, 1791, 1873, 1870]


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


def test_load_packet_set_loads_v2_with_ten_packets_per_side():
    manifest = json.loads(
        (review_packets._PACKET_ROOT / "v2" / "manifest.json").read_bytes()
    )
    packets = review_packets.load_packet_set("v2")

    assert set(packets) == set(manifest["packets"])
    assert Counter(row["side"] for row in manifest["packets"].values()) == {
        "bad": 10,
        "good": 10,
    }


def test_verify_checksums_matches_independently_recorded_v2_digests():
    assert review_packets.verify_checksums("v2") == EXPECTED_V2_DIGESTS


def test_v2_references_the_unchanged_v1_packets():
    manifest = json.loads(
        (review_packets._PACKET_ROOT / "v2" / "manifest.json").read_bytes()
    )
    packets = review_packets.load_packet_set("v2")
    assert review_packets.load_packet_set("v1") == {
        "must_reject": packets["must_reject"],
        "must_approve": packets["must_approve"],
    }
    assert manifest["packets"]["must_reject"]["reference"] == {
        "version": "v1",
        "name": "must_reject",
    }
    assert manifest["packets"]["must_approve"]["reference"] == {
        "version": "v1",
        "name": "must_approve",
    }


def test_v2_new_packet_pr_heads_match_the_manifest():
    manifest = json.loads(
        (review_packets._PACKET_ROOT / "v2" / "manifest.json").read_bytes()
    )
    packets = review_packets.load_packet_set("v2")
    for name, packet in packets.items():
        if name.startswith(("bad_", "good_")):
            assert packet["pr"] == manifest["packets"][name]["pr"]
            assert packet["head_sha"] == manifest["packets"][name]["head_sha"]


def test_v2_manifest_preserves_recurrence_ranking_and_selected_blame_lineage():
    manifest = json.loads(
        (review_packets._PACKET_ROOT / "v2" / "manifest.json").read_bytes()
    )
    selection = manifest["selection"]
    recurrence = selection["fix_recurrence_snapshot"]
    examples = {row["sha"]: row for row in recurrence["examples"]}
    candidates = selection["known_bad_candidates"]

    assert recurrence["example_count"] == len(examples) == 44
    assert selection["known_bad_candidate_count"] == len(candidates) == 53
    assert [candidate["pr"] for candidate in candidates[:9]] == EXPECTED_BAD_PRS
    assert set(examples) == {
        fix["example_sha"]
        for candidate in candidates
        for fix in candidate["later_fixes"]
    }

    for index, expected_pr in enumerate(EXPECTED_BAD_PRS, start=1):
        candidate = candidates[index - 1]
        packet_source = manifest["packets"][f"bad_{index:02d}"]["source"]
        assert candidate["pr"] == expected_pr
        assert packet_source["rank"] == index
        assert packet_source["ticket"] == candidate["source_ticket"]
        assert packet_source["commit_sha"] == candidate["source_commit_sha"]
        assert candidate["blame_proof"]
        proofs_by_example = {
            proof["example_sha"]: proof for proof in candidate["blame_proof"]
        }
        for fix in candidate["later_fixes"]:
            example = examples[fix["example_sha"]]
            assert example["sha"] == fix["example_sha"]
            assert (example["ticket"], example["commit_sha"]) == (
                fix["fix_ticket"], fix["fix_commit_sha"]
            )
            proof = proofs_by_example[example["sha"]]
            assert proof["fix_ticket"] == example["ticket"]
            assert proof["fix_commit_sha"] == example["commit_sha"]
        for proof in candidate["blame_proof"]:
            assert proof["source_commit_sha"] == candidate["source_commit_sha"]
            assert proof["blamed_lines"]
            assert all(line["path"] and line["parent_line"] > 0 and
                       line["commit_sha"] == candidate["source_commit_sha"]
                       for line in proof["blamed_lines"])


@pytest.mark.parametrize(
    "corruption",
    ["missing_example_mapping", "wrong_source_rank", "wrong_blame_commit"],
)
def test_v2_manifest_rejects_broken_recurrence_lineage(
    tmp_path, monkeypatch, corruption
):
    packet_root = tmp_path / "review_packets"
    shutil.copytree(review_packets._PACKET_ROOT / "v1", packet_root / "v1")
    shutil.copytree(review_packets._PACKET_ROOT / "v2", packet_root / "v2")
    manifest_path = packet_root / "v2" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if corruption == "missing_example_mapping":
        manifest["selection"]["known_bad_candidates"][0]["later_fixes"][0][
            "example_sha"
        ] = "000000000000"
        expected_error = "later-fix example is unmatched"
    elif corruption == "wrong_source_rank":
        manifest["packets"]["bad_01"]["source"]["rank"] = 2
        expected_error = "selected packet source does not match"
    else:
        manifest["selection"]["known_bad_candidates"][0]["blame_proof"][0][
            "blamed_lines"
        ][0]["commit_sha"] = "0" * 40
        expected_error = "blame line does not match source commit"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(review_packets, "_PACKET_ROOT", packet_root)

    with pytest.raises(review_packets.PacketSetError, match=expected_error):
        review_packets.verify_checksums("v2")


def test_v2_new_packets_hide_retrospective_outcomes():
    packets = review_packets.load_packet_set("v2")
    for name, packet in packets.items():
        if name.startswith(("bad_", "good_")):
            assert packet["repo"] == "nateprich-projects/command-center"
            assert packet["state"] == "OPEN"
            assert packet["verdict"] is None


def test_verify_checksums_rejects_changed_v2_packet_bytes(tmp_path, monkeypatch):
    packet_root = tmp_path / "review_packets"
    shutil.copytree(review_packets._PACKET_ROOT / "v1", packet_root / "v1")
    shutil.copytree(review_packets._PACKET_ROOT / "v2", packet_root / "v2")
    packet_path = packet_root / "v2" / "bad_01.json"
    packet_path.write_bytes(packet_path.read_bytes() + b"\n")
    monkeypatch.setattr(review_packets, "_PACKET_ROOT", packet_root)

    with pytest.raises(review_packets.PacketSetError, match="checksum mismatch"):
        review_packets.verify_checksums("v2")


@pytest.mark.parametrize("field", ["pr", "head_sha"])
def test_v2_manifest_requires_each_new_packet_identity(
    tmp_path, monkeypatch, field
):
    packet_root = tmp_path / "review_packets"
    shutil.copytree(review_packets._PACKET_ROOT / "v1", packet_root / "v1")
    shutil.copytree(review_packets._PACKET_ROOT / "v2", packet_root / "v2")
    manifest_path = packet_root / "v2" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    del manifest["packets"]["bad_01"][field]
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(review_packets, "_PACKET_ROOT", packet_root)

    with pytest.raises(review_packets.PacketSetError, match="PR or head SHA is missing"):
        review_packets.verify_checksums("v2")


def test_load_packet_rejects_an_unknown_name():
    with pytest.raises(review_packets.PacketSetError, match="unknown packet name"):
        review_packets.load_packet("unregistered")
