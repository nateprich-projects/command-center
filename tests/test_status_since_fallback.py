"""Gate age falls back to the Status field's write time (#1949).

GitHub stopped writing ProjectV2ItemStatusChangedEvent on 2026-09-28 (#1906),
so every item moved since then read its gate age as unknown. The Status
field's own ``updatedAt`` stands in when the read timeline has no event into
the current Status; the event stays canonical when it exists.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

REPO = "owner/repo"
EVENT_AT = "2026-09-27T10:00:00Z"
UPDATED_AT = "2026-09-29T05:19:47Z"


def _node(status="Ready", updated_at=UPDATED_AT, events=(), timeline=True):
    status_value = {"name": status}
    if updated_at is not None:
        status_value["updatedAt"] = updated_at
    content = {
        "number": 7,
        "title": "issue 7",
        "url": "https://github.com/{}/issues/7".format(REPO),
        "state": "OPEN",
        "stateReason": None,
        "closedAt": None,
        "repository": {"nameWithOwner": REPO},
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "parent": None,
        "subIssuesSummary": {"total": 0, "completed": 0},
        "blockedBy": {"nodes": []},
    }
    if timeline:
        content["timelineItems"] = {"nodes": list(events)}
    return {"id": "item-7", "status": status_value, "content": content}


def _event(status, at, project=None):
    return {
        "__typename": "ProjectV2ItemStatusChangedEvent",
        "createdAt": at,
        "previousStatus": "Shaped",
        "status": status,
        "project": {"number": funnel.PROJECT_NUMBER if project is None else project},
    }


def _at(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_missing_status_event_falls_back_to_status_field_updated_at():
    item = funnel._from_node(_node(events=[_event("Ideas", EVENT_AT)]))

    assert item.status_since == _at(UPDATED_AT)


def test_matching_status_event_stays_canonical_over_updated_at():
    item = funnel._from_node(_node(events=[_event("Ready", EVENT_AT)]))

    assert item.status_since == _at(EVENT_AT)


def test_event_from_another_project_does_not_count_and_falls_back():
    item = funnel._from_node(
        _node(events=[_event("Ready", EVENT_AT, project=funnel.PROJECT_NUMBER + 1)])
    )

    assert item.status_since == _at(UPDATED_AT)


def test_unknown_only_when_neither_source_exists():
    item = funnel._from_node(_node(updated_at=None, events=[]))

    assert item.status_since is None


def test_compact_load_without_timeline_stays_unknown_for_hydration():
    """The paged list reads no timeline; hydration keys on a missing age."""
    item = funnel._from_node(_node(timeline=False))

    assert item.status_updated_at == _at(UPDATED_AT)
    assert item.status_since is None


def test_hydrated_detail_read_heals_an_unknown_row():
    item = funnel._from_node(_node(timeline=False))

    funnel._apply_item_detail_fields(
        item,
        {"timelineItems": {"nodes": []}},
        include_children=False,
    )

    assert item.status_since == _at(UPDATED_AT)


def test_project_item_query_reads_the_status_write_time():
    status_selection = funnel.ITEM_NODE_FIELDS.split('name: "Status")', 1)[1]
    status_selection = status_selection.split("}", 2)[0]

    assert "updatedAt" in status_selection
