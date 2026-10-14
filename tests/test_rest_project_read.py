"""REST Project read path: loader, fail-closed parsing, counter (#2421)."""

from __future__ import annotations

import io
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def reset():
    funnel.reset_api_usage()


def rest_row(number, status="Ready", klass="Implement", body="hello",
             repo="nateprich/command-center", item_id="PVTI_1",
             extra_fields=()):
    fields = [
        {"field_id": funnel.STATUS_FIELD_ID,
         "value": {"name": {"raw": status}}} if status is not None else None,
        {"field_id": funnel.CLASS_FIELD_ID,
         "value": {"name": {"raw": klass}}} if klass is not None else None,
        {"field_id": funnel.NEEDS_FIELD_ID,
         "value": {"name": {"raw": "none"}}},
        *extra_fields,
    ]
    return {
        "id": item_id,
        "fields": [entry for entry in fields if entry is not None],
        "content": {
            "number": number,
            "title": "row {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(repo, number),
            "state": "OPEN",
            "body": body,
            "repository": {"nameWithOwner": repo},
        },
    }


def test_loader_returns_items_with_field_values_and_bodies():
    """Recorded paginated items payloads read as items with values+bodies."""
    reset()
    first = {
        "items": [rest_row(1, body="first body"),
                  {"id": "PVTI_draft", "fields": [], "content": {}}],
        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
    }
    second = {"items": [rest_row(2, status="Building", body="second body")]}
    seen = []

    def request(endpoint):
        seen.append(endpoint)
        assert "fields=" in endpoint
        assert endpoint.startswith(funnel.REST_PROJECT_ITEMS_PATH)
        if "cursor=" in endpoint:
            assert endpoint.endswith("cursor=cursor-1")
            return second
        return first

    items = funnel.load_rest_project_items(
        [funnel.STATUS_FIELD_ID, funnel.CLASS_FIELD_ID, funnel.NEEDS_FIELD_ID],
        _request=request,
    )

    # The draft row (no issue number) is skipped, like `_from_node`.
    assert [(item.number, item.status, item.klass, item.body)
            for item in items] == [
        (1, "Ready", "Implement", "first body"),
        (2, "Building", "Implement", "second body"),
    ]
    assert len(seen) == 2


def test_absent_routing_value_fails_closed_with_explicit_unknown():
    reset()
    assert funnel.parse_rest_single_select(
        [], funnel.STATUS_FIELD_ID, known_values=funnel.STAGES,
    ) == funnel.REST_UNKNOWN_VALUE

    item = funnel.rest_item_to_item(rest_row(3, status=None))
    assert item is not None
    # No default Status is substituted: the row names its own unreadability.
    assert item.status == funnel.REST_UNKNOWN_VALUE
    assert item.status != "Ideas"


def test_unknown_routing_value_fails_closed_with_explicit_unknown():
    reset()
    assert funnel.parse_rest_single_select(
        [{"field_id": funnel.STATUS_FIELD_ID,
          "value": {"name": {"raw": "Frobnicate"}}}],
        funnel.STATUS_FIELD_ID,
        known_values=funnel.STAGES,
    ) == funnel.REST_UNKNOWN_VALUE

    item = funnel.rest_item_to_item(
        rest_row(4, klass="Frobnicate"))
    assert item is not None
    assert item.klass == funnel.REST_UNKNOWN_VALUE
    assert item.klass != "Broken"


def test_known_value_reads_through_and_disagreeing_entries_fail_closed():
    reset()
    fields = [{"field_id": funnel.STATUS_FIELD_ID,
               "value": {"name": {"raw": "Ready"}}}]
    assert funnel.parse_rest_single_select(
        fields, funnel.STATUS_FIELD_ID,
        known_values=funnel.STAGES) == "Ready"
    disagreeing = fields + [{
        "name": "Status", "value": {"name": {"raw": "Done"}}}]
    assert funnel.parse_rest_single_select(
        disagreeing, funnel.STATUS_FIELD_ID, "Status",
        known_values=funnel.STAGES) == funnel.REST_UNKNOWN_VALUE


def test_rest_counter_increments_per_request_alongside_graphql_spend(
        monkeypatch):
    """Mocked REST calls count per request; GraphQL spend is untouched."""
    reset()

    class Proc:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    pages = [
        {"items": [rest_row(1)], "pageInfo": {
            "hasNextPage": True, "endCursor": "c1"}},
        {"items": [rest_row(2)]},
    ]

    def run(args, **kwargs):
        if args[:3] == ["gh", "api", "graphql"]:
            return Proc(json.dumps({"data": {"viewer": {"login": "n"}}}))
        assert args[:2] == ["gh", "api"]
        return Proc(json.dumps(pages.pop(0)))

    monkeypatch.setattr(funnel.subprocess, "run", run)

    funnel.gh_graphql("{viewer{login}}")
    items = funnel.load_rest_project_items([funnel.STATUS_FIELD_ID])

    assert [item.number for item in items] == [1, 2]
    assert funnel.rest_spend() == {"calls": 2}
    assert funnel.graphql_spend()["calls"] == 1


def test_rest_report_names_core_pool_spend_and_stays_silent_when_idle():
    reset()
    out = io.StringIO()
    funnel.report_rest_spend(out)
    assert out.getvalue() == ""

    funnel._REST_SPEND.update({"calls": 3})
    out = io.StringIO()
    funnel.report_rest_spend(out)
    assert "3 call(s)" in out.getvalue()
    reset()


def test_rest_loader_rejects_empty_fields_and_stalled_cursors():
    reset()
    with pytest.raises(ValueError):
        funnel.load_rest_project_items([])
    stalled = {"items": [rest_row(1)],
               "pageInfo": {"hasNextPage": True, "endCursor": "same"}}
    with pytest.raises(funnel.GitHubError, match="did not advance"):
        funnel.load_rest_project_items(
            [funnel.STATUS_FIELD_ID],
            _request=lambda endpoint: stalled,
        )
