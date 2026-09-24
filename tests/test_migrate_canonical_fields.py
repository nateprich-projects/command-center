"""Canonical Project-field migration preserves state before trimming prose."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import migrate_canonical_fields as migrate  # noqa: E402


def item(**values):
    defaults = dict(
        repo="owner/repo", number=1, title="Work", url="u", state="OPEN",
        item_id="item-1", status="Ready", klass="Broken",
        body=funnel.origin_block("agent"), labels=[],
    )
    defaults.update(values)
    return funnel.Item(**defaults)


def test_needs_expansion_preserves_every_existing_option_id(monkeypatch):
    queries = []
    field = {
        "id": "needs-field", "name": "Needs", "options": [
            {"id": "keep-none", "name": "none", "description": "", "color": "GRAY"},
            {"id": "keep-human", "name": "human", "description": "", "color": "RED"},
            {"id": "keep-claude", "name": "claude-code-environment",
             "description": "", "color": "PURPLE"},
        ],
    }
    monkeypatch.setattr(
        funnel, "gh_graphql", lambda query, **variables: queries.append(query) or {})

    migrate._update_options(field, funnel.NEEDS_OPTIONS)

    query = queries[0]
    assert 'id:"keep-none"' in query
    assert 'id:"keep-human"' in query
    assert 'id:"keep-claude"' in query
    assert 'name:"agent"' in query
    assert 'name:"external-event"' in query


def test_blocked_ticket_without_an_owner_refuses_to_guess():
    blocked = item(parent="owner/repo#2", labels=["blocked"], needs="none")
    with pytest.raises(migrate.MigrationError, match="canonical owner"):
        migrate.infer_values(blocked, {})


def test_explicit_external_event_override_settles_blocked_ticket():
    blocked = item(parent="owner/repo#2", labels=["blocked"], needs="none")
    values = migrate.infer_values(
        blocked, {blocked.ref: "external-event"})
    assert values == {
        "Origin": "agent", "Risk": "standard", "Needs": "external-event",
    }


def test_explicit_origin_override_settles_a_pre_marker_project():
    old = item(body="A project from before capture markers existed.")
    values = migrate.infer_values(old, {}, {old.ref: "Nate"})
    assert values["Origin"] == "Nate"


def test_backfill_reports_every_unmappable_row():
    first = item(number=11, body="No origin record.")
    second = item(number=12, item_id="item-2", body="Also no origin record.")

    with pytest.raises(migrate.MigrationError) as caught:
        migrate.plan_backfill([first, second], {})

    assert first.ref in str(caught.value)
    assert second.ref in str(caught.value)


def test_backfill_inventory_contains_only_actual_field_writes():
    unchanged = item(
        origin="agent", risk="standard", needs="none")
    changed = item(
        number=2, item_id="item-2", origin="agent",
        risk="standard", needs="human")
    rows = [
        (unchanged, {
            "Origin": "agent", "Risk": "standard", "Needs": "none"}),
        (changed, {
            "Origin": "agent", "Risk": "escalated", "Needs": "human"}),
    ]

    writes = migrate.plan_backfill_writes(rows)

    assert [(found.ref, field, value) for found, field, value in writes] == [
        (changed.ref, "Risk", "escalated"),
    ]


def test_prose_inventory_contains_only_bodies_that_will_be_edited():
    unchanged = item(body="# Plan\n\nKeep this.\n")
    changed = item(
        number=2, item_id="item-2",
        body="# Plan\n\nKeep this.\n\nRisk: standard\n")

    writes = migrate.plan_prose_writes([unchanged, changed])

    assert [(found.ref, body) for found, body in writes] == [
        (changed.ref, "# Plan\n\nKeep this.\n"),
    ]


def test_incident_replays_1401_1402_1403_use_canonical_fields():
    false_risk = item(
        number=1401, status="Shaped", origin="agent", risk="standard",
        needs="none",
        body="Rejected: migrate data. Explicitly not doing credentials.")
    scheduling = item(
        number=1402, item_id="item-2", status="Shaped", origin="agent",
        risk="standard", needs="none",
        body="Should this land now, or wait for other work?")
    external_wait = item(
        number=1403, item_id="item-3", labels=["blocked"],
        origin="agent", risk="standard", needs="external-event")

    assert funnel.gate_question(false_risk) is None
    assert funnel.gate_question(scheduling) is None
    assert funnel.gate_question(external_wait) is None


def test_trim_removes_routing_copies_but_keeps_the_actual_question():
    body = "\n\n".join([
        "# Plan\n\nDo it.",
        "Risk: standard",
        "## Needs Nate\n\n"
        "- Exposure: nothing outstanding. No new credentials.\n"
        "- Gates: Who may write Ready?\n"
        "- Preference: nothing outstanding. No choice remains.",
        funnel.origin_block("agent"),
    ])

    found = migrate.trim_routing_prose(body)

    assert "Risk: standard" not in found
    assert funnel.ORIGIN_MARKER not in found
    assert "nothing outstanding" not in found
    assert "## Needs Nate\n\n- Gates: Who may write Ready?" in found


def test_trim_keeps_escalated_explanation_without_repeating_the_field():
    found = migrate.trim_routing_prose(
        "Plan.\n\nRisk: escalated — concurrency: two writers may race\n")
    assert "Risk: escalated" not in found
    assert "## Risk rationale\n\nconcurrency: two writers may race" in found


def test_trim_normalizes_the_legacy_needs_you_heading():
    found = migrate.trim_routing_prose(
        "## Needs you\n\n- Gates: Who may approve this?\n")
    assert found == "## Needs Nate\n\n- Gates: Who may approve this?\n"
