"""REST Project write path: field updates, item adds, failure surfacing (#2422)."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def reset():
    funnel.reset_api_usage()
    funnel._REST_FIELD_CACHE.clear()


#: A real Project item node id and the database id it carries (recorded
#: 2026-10-17, ``tests/fixtures/rest_project_items_live.json``).
NODE_ID = "PVTI_lAHOD7A-N84BihDgzg5jAGQ"
DATABASE_ID = 241369188
OPTIONS = {
    408475351: {"Ready": "62970921", "Done": "2a3af7fa"},
    408475545: {"Implement": "c1"},
    411997140: {"none": "n1"},
}


def schema(method, endpoint, body):
    """Answer the field-schema and item reads a write makes first."""
    if "/fields/" in endpoint:
        field = int(endpoint.rsplit("/", 1)[-1])
        return {"id": field, "data_type": "single_select", "options": [
            {"id": option, "name": {"raw": name}}
            for name, option in OPTIONS[field].items()]}
    return {"id": DATABASE_ID, "node_id": NODE_ID}


def confirmed(body):
    return {"id": DATABASE_ID, "node_id": NODE_ID,
            "fields": [{"id": body["fields"][0]["id"]}]}


def test_field_update_half_apply_reports_failure_without_success():
    """A mocked mid-apply error raises; nothing claims the write succeeded."""
    reset()
    calls = []

    def request(method, endpoint, body):
        if method == "GET":
            return schema(method, endpoint, body)
        calls.append((method, endpoint, body))
        if body["fields"][0]["id"] == 408475545:
            raise funnel.GitHubError("REST request failed: HTTP 502")
        return confirmed(body)

    with pytest.raises(funnel.GitHubError, match="HTTP 502"):
        funnel.rest_update_project_item_fields(
            NODE_ID,
            {
                funnel.STATUS_FIELD_ID: "Ready",
                funnel.CLASS_FIELD_ID: "Implement",
                funnel.NEEDS_FIELD_ID: "none",
            },
            _request=request,
        )

    # The first field applied, the second raised, the third never ran:
    # a half-apply that reports failure, not success.
    assert [body["fields"][0]["id"] for _, _, body in calls] == [
        408475351, 408475545,
    ]
    assert all(
        method == "PATCH"
        and endpoint == funnel.REST_PROJECT_ITEMS_PATH + "/241369188"
        for method, endpoint, _ in calls
    )


def test_field_update_without_confirmation_reports_failure():
    """A 200 with no matching item id never counts as an applied write."""
    reset()
    for answer in ({"id": 1, "node_id": NODE_ID, "fields": [{"id": 408475351}]},
                   {"id": DATABASE_ID, "node_id": NODE_ID, "fields": []},
                   [NODE_ID]):
        with pytest.raises(funnel.GitHubError, match="did not confirm"):
            funnel.rest_update_project_item_field(
                NODE_ID,
                funnel.STATUS_FIELD_ID,
                "Ready",
                _request=lambda method, endpoint, body, answer=answer: (
                    schema(method, endpoint, body) if method == "GET"
                    else answer),
            )


def test_item_add_returns_new_item_id():
    """A recorded item-add success payload yields the new item id."""
    reset()
    seen = []

    def request(method, endpoint, body):
        seen.append((method, endpoint, body))
        return {"id": "PVTI_new"}

    item_id = funnel.rest_add_project_item("I_issue_node", _request=request)

    assert item_id == "PVTI_new"
    assert seen == [
        ("POST", funnel.REST_PROJECT_ITEMS_PATH, {"content_id": "I_issue_node"})
    ]


def test_item_add_without_item_id_reports_failure():
    """An add response with no new id raises instead of claiming success."""
    reset()
    with pytest.raises(funnel.GitHubError, match="did not return"):
        funnel.rest_add_project_item(
            "I_issue_node",
            _request=lambda method, endpoint, body: {},
        )


def test_write_transport_failure_surfaces_and_counts_once(monkeypatch):
    """Mocked transport errors raise GitHubError and cost one REST call each."""
    reset()

    class Proc:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    def run(args, **kwargs):
        assert args[:3] == ["gh", "api", "-X"]
        if args[3] == "POST":
            return Proc(stdout=json.dumps({"id": "PVTI_new"}))
        if args[3] == "GET":
            return Proc(stdout=json.dumps(schema("GET", args[4], None)))
        return Proc(stderr="HTTP 502 Bad Gateway", returncode=1)

    monkeypatch.setattr(funnel, "_run_gh", run)

    assert funnel.rest_add_project_item("I_issue_node") == "PVTI_new"
    with pytest.raises(funnel.GitHubError, match="HTTP 502"):
        funnel.rest_update_project_item_field(
            NODE_ID, funnel.STATUS_FIELD_ID, "Ready"
        )
    # The add, the field schema read, the item check and the failed PATCH.
    assert funnel.rest_spend() == {"calls": 4}


def test_write_rejects_empty_ids_and_updates():
    """Empty item, field, content, or update inputs fail before any request."""
    reset()
    with pytest.raises(ValueError):
        funnel.rest_update_project_item_field("", funnel.STATUS_FIELD_ID, "Ready")
    with pytest.raises(ValueError):
        funnel.rest_update_project_item_field("PVTI_1", "", "Ready")
    with pytest.raises(ValueError):
        funnel.rest_update_project_item_fields("PVTI_1", {})
    with pytest.raises(ValueError):
        funnel.rest_add_project_item("")
    assert funnel.rest_spend() == {"calls": 0}
