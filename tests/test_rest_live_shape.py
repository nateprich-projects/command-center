"""The REST Project path against payloads recorded from the live API (#2614).

The earlier REST read and write tests replayed a shape nobody had fetched:
GraphQL field ids in ``?fields=``, a ``pageInfo`` cursor in the body,
uppercase state and ``nameWithOwner``, string item ids and a ``field_id``
write body. The live route rejects or never returns each of those. These
tests replay ``tests/fixtures/rest_project_items_live.json``, recorded on
2026-10-17, and take every expected value from that recording or from the
GraphQL read of the same issues, never from the converter.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

LIVE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures"
     / "rest_project_items_live.json").read_text()
)
REPO = "nateprich-projects/command-center"


def reset():
    funnel.reset_api_usage()
    funnel._REST_FIELD_CACHE.clear()


def live_rows():
    return copy.deepcopy(LIVE["items"])


def by_number(items):
    return {item.number: item for item in items}


def page_request(pages, blockers):
    """A ``_request`` replaying recorded pages and blocker lists."""
    seen = []

    def request(endpoint):
        seen.append(endpoint)
        if "/dependencies/blocked_by" in endpoint:
            number = endpoint.split("/issues/")[1].split("/")[0]
            return {"items": copy.deepcopy(blockers[number]),
                    "next_cursor": None}
        return pages.pop(0)

    return request, seen


def test_board_field_ids_are_the_numeric_ids_the_route_accepts():
    """``?fields=`` takes database ids; GraphQL node ids answer HTTP 400."""
    # From GET /users/nateprich/projectsV2/2/fields, 2026-10-17.
    assert set(funnel.REST_BOARD_FIELD_IDS) == {
        "408475351", "408475545", "408497956", "410157169", "411997140",
        "416360306", "416360356", "424937513", "408475358",
    }


def test_live_rows_read_as_the_graphql_view_of_the_same_issues():
    """Recorded rows convert to what GraphQL reads for #1466 and #2027."""
    reset()
    request, seen = page_request(
        [{"items": live_rows(), "next_cursor": None}], LIVE["blocked_by"])

    items = by_number(funnel.load_rest_project_items(
        list(funnel.REST_BOARD_FIELD_IDS), _request=request))

    open_ticket, closed_ticket = items[2027], items[1466]
    assert open_ticket.repo == REPO
    assert open_ticket.state == "OPEN" and closed_ticket.state == "CLOSED"
    assert closed_ticket.state_reason == "COMPLETED"
    assert open_ticket.url == (
        "https://github.com/nateprich-projects/command-center/issues/2027")
    assert open_ticket.item_id == "PVTI_lAHOD7A-N84BihDgzg9mCc4"
    # GraphQL: parent { number } is 1840 and 1439.
    assert open_ticket.parent == REPO + "#1840"
    assert closed_ticket.parent == REPO + "#1439"
    # Unset fields arrive as "value": null and read None, as GraphQL reads
    # them; a ticket with no Class inherits its parent's.
    assert (open_ticket.status, open_ticket.klass, open_ticket.pinned,
            open_ticket.domain, open_ticket.in_motion_since) == (
        None, None, False, None, None)
    assert (open_ticket.needs, open_ticket.origin, open_ticket.risk) == (
        "none", "agent", "standard")
    # GraphQL: three closed-completed blockers, 1837-1839; none open or dead.
    assert sorted(open_ticket.blocked_by_refs) == [
        REPO + "#1837", REPO + "#1838", REPO + "#1839"]
    assert open_ticket.open_blockers == [] == open_ticket.dead_blockers
    # Blockers are read for open tickets only; the closed one costs nothing.
    assert [endpoint for endpoint in seen if "blocked_by" in endpoint] == [
        "/repos/{}/issues/2027/dependencies/blocked_by?per_page=100".format(
            REPO)]


def test_an_open_blocker_keeps_the_ticket_blocked():
    reset()
    blockers = copy.deepcopy(LIVE["blocked_by"])
    blockers["2027"][0]["state"] = "open"
    request, _ = page_request(
        [{"items": live_rows(), "next_cursor": None}], blockers)

    items = by_number(funnel.load_rest_project_items(
        list(funnel.REST_BOARD_FIELD_IDS), _request=request))

    assert items[2027].open_blockers == [REPO + "#1838"]


def test_a_row_counting_blockers_it_does_not_carry_fails_the_load():
    """Counts without the blocking issues never read as unblocked."""
    reset()
    row = live_rows()[1]
    with pytest.raises(funnel.GitHubError, match="does not carry"):
        funnel.rest_item_to_item(row)


def test_a_field_missing_from_the_row_still_reads_unknown():
    reset()
    row = live_rows()[0]
    row["fields"] = [entry for entry in row["fields"]
                     if entry["name"] != "Needs"]
    item = funnel.rest_item_to_item(row)
    assert item.needs == funnel.REST_UNKNOWN_VALUE


def test_production_pages_follow_the_link_header(monkeypatch):
    """Pages chain through ``after=`` from ``Link``; each costs one call."""
    reset()
    first, second = live_rows()[:1], live_rows()[1:]
    calls = []

    def run(args, **kwargs):
        args = list(args)
        calls.append(args)
        assert args[:3] == ["gh", "api", "-i"]
        endpoint = args[3]
        if "/dependencies/blocked_by" in endpoint:
            return SimpleNamespace(
                stdout="HTTP/2.0 200 OK\n\n" + json.dumps(
                    LIVE["blocked_by"]["2027"]), stderr="", returncode=0)
        if "after=" not in endpoint:
            return SimpleNamespace(
                stdout="HTTP/2.0 200 OK\r\n{}\r\n\r\n{}".format(
                    LIVE["link_header"], json.dumps(first)),
                stderr="", returncode=0)
        assert endpoint.endswith("&after=" + LIVE["link_cursor"])
        return SimpleNamespace(
            stdout="HTTP/2.0 200 OK\n\n" + json.dumps(second),
            stderr="", returncode=0)

    monkeypatch.setattr(funnel.subprocess, "run", run)
    items = funnel.load_rest_project_items(list(funnel.REST_BOARD_FIELD_IDS))

    assert sorted(item.number for item in items) == [1466, 2027]
    assert calls[0][3].startswith(
        funnel.REST_PROJECT_ITEMS_PATH + "?per_page=100&fields=408475351,")
    assert funnel.rest_spend() == {"calls": 3}
    assert funnel.graphql_spend()["calls"] == 0


def test_node_ids_decode_to_the_recorded_database_ids():
    for row in LIVE["items"]:
        assert funnel._rest_item_database_id(row["node_id"]) == row["id"]
    for bad in ("PVTI_1", "project-item-42", "PVTSSF_lAHOD7A-N84BihDgzhhY1tc"):
        with pytest.raises(funnel.GitHubError):
            funnel._rest_item_database_id(bad)


def write_request(response=None):
    """Replay the recorded field schema, item read and PATCH answer."""
    calls = []
    patch = LIVE["patch_response"]

    def request(method, endpoint, body):
        calls.append((method, endpoint, body))
        if method == "GET" and endpoint.endswith("/fields/408475351"):
            return copy.deepcopy(LIVE["status_field"])
        if method == "GET":
            return {"id": patch["id"], "node_id": patch["node_id"]}
        return copy.deepcopy(response if response is not None else patch)

    return request, calls


def test_status_write_sends_the_wire_shape_the_route_accepted():
    """Numeric item and field ids, the option id, a ``fields`` list."""
    reset()
    request, calls = write_request()

    funnel.rest_update_project_item_field(
        LIVE["patch_response"]["node_id"], funnel.STATUS_FIELD_ID, "Done",
        _request=request)

    # The same body applied live on 2026-10-17 (a same-value write).
    assert calls[-1] == (
        "PATCH", funnel.REST_PROJECT_ITEMS_PATH + "/241369188",
        {"fields": [{"id": 408475351, "value": "2a3af7fa"}]})
    # The item was checked before the write.
    assert ("GET", funnel.REST_PROJECT_ITEMS_PATH + "/241369188",
            None) in calls[:-1]


def test_write_without_matching_confirmation_reports_failure():
    reset()
    other = dict(LIVE["patch_response"], node_id="PVTI_other")
    request, _ = write_request(response=other)
    with pytest.raises(funnel.GitHubError, match="did not confirm"):
        funnel.rest_update_project_item_field(
            LIVE["patch_response"]["node_id"], funnel.STATUS_FIELD_ID,
            "Done", _request=request)
    request, _ = write_request(response={"id": 241369188})
    with pytest.raises(funnel.GitHubError, match="did not confirm"):
        funnel.rest_update_project_item_field(
            "241369188", funnel.STATUS_FIELD_ID, "Done", _request=request)


def test_an_unknown_option_fails_before_any_write():
    reset()
    request, calls = write_request()
    with pytest.raises(funnel.GitHubError, match="no option"):
        funnel.rest_update_project_item_field(
            LIVE["patch_response"]["node_id"], funnel.STATUS_FIELD_ID,
            "Frobnicate", _request=request)
    assert all(method == "GET" for method, _, _ in calls)
