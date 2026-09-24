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
