"""Ticket #2379: source-linked weekly-panel reading inventory checks."""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import muse_reading_inventory as inventory  # noqa: E402


REQUIRED_SOURCES = (
    "#1157",
    "#1182",
    "#1341",
    "#1396",
    "#1409",
    "#1673",
    "#1842",
    "#1994",
    "#1995",
    "#2018",
    "#2123",
    "AGENTS.md",
    "LEARNINGS.md",
    "usage.py",
)


def test_loader_loads_every_reading_with_required_fields():
    loaded = inventory.load_reading_inventory()
    assert loaded["schema_version"] == 1
    assert loaded["readings"], "inventory must hold at least one reading"
    for record in loaded["readings"]:
        for field in inventory.REQUIRED_FIELDS:
            assert field in record, f"{record.get('id')}: missing {field}"
        # Re-validate each loaded record through the public validator.
        assert inventory.validate_reading_record(record)["id"] == record["id"]


def test_loader_covers_every_listed_source():
    loaded = inventory.load_reading_inventory()
    refs = {entry["ref"] for entry in loaded["sources_searched"]}
    for required in REQUIRED_SOURCES:
        assert required in refs, f"source {required} was not searched"


def test_loader_rejects_a_missing_inventory_file(tmp_path):
    with pytest.raises(ValueError):
        inventory.load_reading_inventory(tmp_path / "absent.json")


def _a_valid_record():
    loaded = inventory.load_reading_inventory()
    return copy.deepcopy(loaded["readings"][0])


def test_validator_rejects_pooled_weekly_five_hour_windows():
    for pooled in ("weekly+five_hour", "both", "weekly/five_hour", "", None):
        record = _a_valid_record()
        record["window_identity"] = pooled
        with pytest.raises(ValueError, match="window_identity"):
            inventory.validate_reading_record(record)


def test_validator_rejects_missing_uncertainty_or_source_url():
    record = _a_valid_record()
    del record["observation_uncertainty_seconds"]
    with pytest.raises(ValueError, match="uncertainty"):
        inventory.validate_reading_record(record)

    record = _a_valid_record()
    for bad_url in ("", None, "not-a-url", "http://insecure.example/x"):
        record["source_url"] = bad_url
        with pytest.raises(ValueError, match="source_url"):
            inventory.validate_reading_record(record)


def test_validator_rejects_incomplete_coverage_in_primary_fit():
    record = _a_valid_record()
    record["coverage"] = "incomplete"
    record["include_in_primary_fit"] = True
    with pytest.raises(ValueError, match="primary fit"):
        inventory.validate_reading_record(record)


def test_validator_rejects_duplicate_ids_on_load(tmp_path):
    loaded = inventory.load_reading_inventory()
    payload = {
        "schema_version": 1,
        "sources_searched": loaded["sources_searched"],
        "readings": [loaded["readings"][0], copy.deepcopy(loaded["readings"][0])],
    }
    import json

    target = tmp_path / "dup.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        inventory.load_reading_inventory(target)


def test_display_scope_is_preserved():
    """#2018 stays display-narrowed, #2123 excludes learned weights, and the
    8% figure is a display anchor only -- never a weight observation."""
    loaded = inventory.load_reading_inventory()
    scope = loaded.get("scope_confirmation", {})
    assert "display" in scope.get("issue_2018", {}).get("scope", "").lower()
    assert "learned weights" in scope.get("issue_2123", {}).get("scope", "")

    anchors = [
        record
        for record in loaded["readings"]
        if record.get("display_anchor_only")
    ]
    assert anchors, "the 8% display anchor must be flagged"
    eight = [record for record in anchors if record["panel_percent"] == 8]
    assert len(eight) == 1
    assert eight[0]["include_in_primary_fit"] is False

    weekly_refusals = [
        record
        for record in loaded["readings"]
        if record["kind"] == "refusal_wall"
    ]
    identities = {record["window_identity"] for record in weekly_refusals}
    assert identities <= {"weekly", "five_hour"}
    five_hour = [
        record for record in weekly_refusals if record["window_identity"] == "five_hour"
    ]
    assert five_hour, "the five-hour wall must be kept separate from weekly evidence"
    for record in five_hour:
        assert record["include_in_primary_fit"] is False
