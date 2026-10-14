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


def test_field_update_half_apply_reports_failure_without_success():
    """A mocked mid-apply error raises; nothing claims the write succeeded."""
    reset()
    calls = []

    def request(method, endpoint, body):
        calls.append((method, endpoint, body))
        if body["field_id"] == funnel.CLASS_FIELD_ID:
            raise funnel.GitHubError("REST request failed: HTTP 502")
        return {"id": "PVTI_1"}

    with pytest.raises(funnel.GitHubError, match="HTTP 502"):
        funnel.rest_update_project_item_fields(
            "PVTI_1",
            {
                funnel.STATUS_FIELD_ID: "Ready",
                funnel.CLASS_FIELD_ID: "Implement",
                funnel.NEEDS_FIELD_ID: "none",
            },
            _request=request,
        )

    # The first field applied, the second raised, the third never ran:
    # a half-apply that reports failure, not success.
    assert [body["field_id"] for _, _, body in calls] == [
        funnel.STATUS_FIELD_ID,
        funnel.CLASS_FIELD_ID,
    ]
    assert all(
        method == "PATCH"
        and endpoint == funnel.REST_PROJECT_ITEMS_PATH + "/PVTI_1"
        for method, endpoint, _ in calls
    )


def test_field_update_without_confirmation_reports_failure():
    """A 200 with no matching item id never counts as an applied write."""
    reset()
    with pytest.raises(funnel.GitHubError, match="did not confirm"):
        funnel.rest_update_project_item_field(
            "PVTI_1",
            funnel.STATUS_FIELD_ID,
            "Ready",
            _request=lambda method, endpoint, body: {"id": "PVTI_other"},
        )
    with pytest.raises(funnel.GitHubError, match="did not confirm"):
        funnel.rest_update_project_item_field(
            "PVTI_1",
            funnel.STATUS_FIELD_ID,
            "Ready",
            _request=lambda method, endpoint, body: ["PVTI_1"],
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
        return Proc(stderr="HTTP 502 Bad Gateway", returncode=1)

    monkeypatch.setattr(funnel, "_run_gh", run)

    assert funnel.rest_add_project_item("I_issue_node") == "PVTI_new"
    with pytest.raises(funnel.GitHubError, match="HTTP 502"):
        funnel.rest_update_project_item_field(
            "PVTI_1", funnel.STATUS_FIELD_ID, "Ready"
        )
    assert funnel.rest_spend() == {"calls": 2}


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
