"""An unsatisfiable-acceptance decline lifts when its prerequisite lands (#2497).

The route withholds a Needs-agent ticket while its body digest is unchanged.
When the route's excerpt names one prerequisite issue/PR beside blocked-on /
needs / requires / prerequisite wording, that reference is a canonical
condition: it re-resolves from GitHub state on each startability check, and a
satisfied prerequisite lifts the withhold with no body edit. Every GitHub
read here goes through fakes; nothing reaches GitHub.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


REPO = "owner/repo"
BODY = (
    "What: do the thing.\n\n"
    "Accept: the acceptance the decline called unsatisfiable."
)
DIGEST = hashlib.sha256(BODY.encode("utf-8")).hexdigest()


def _parent():
    return funnel.Item(
        repo=REPO, number=1, title="parent", url="", state="OPEN",
        status="Ready", klass="Improve", needs="none", children_total=1,
    )


def _ticket(body, excerpt, *, digest=DIGEST):
    route = {"type": "unsatisfiable-acceptance", "acceptance_digest": digest}
    if excerpt is not None:
        route["decline_excerpt"] = excerpt
    parent = _parent()
    item = funnel.Item(
        repo=REPO, number=2, title="ticket", url="", state="OPEN",
        body=body, parent=parent.ref, needs="agent", risk="standard",
        decline_route=route,
    )
    return parent, item


def _issue(number, state, repo=REPO):
    return funnel.Item(
        repo=repo, number=number, title="prerequisite", url="",
        state=state,
    )


def test_open_prerequisite_still_withheld():
    parent, item = _ticket(BODY, "Acceptance is blocked on #9.")
    blocker = _issue(9, "OPEN")
    by_ref = {row.ref: row for row in (parent, item, blocker)}

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_satisfied_issue_prerequisite_lifts_without_body_edit(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance is blocked on #9.")
    blocker = _issue(9, "CLOSED")
    by_ref = {row.ref: row for row in (parent, item, blocker)}

    def no_network(*args, **kwargs):
        raise AssertionError("a loaded issue must decide with no request")

    monkeypatch.setattr(funnel, "_gh_api_json", no_network)

    assert funnel._decline_route_withholds_startability(item, by_ref) is False
    assert [row.ref for row in funnel.startable([parent, item, blocker])] == [
        item.ref]


def test_merged_pr_prerequisite_lifts_without_body_edit(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance needed #9.")
    by_ref = {row.ref: row for row in (parent, item)}
    calls = []

    def fake_api(endpoint, **kwargs):
        calls.append(endpoint)
        assert endpoint == "repos/{}/pulls/9".format(REPO)
        return {"state": "closed", "merged": True,
                "merged_at": "2026-10-03T03:30:00Z"}

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is False
    assert calls == ["repos/{}/pulls/9".format(REPO)]


def test_closed_but_unmerged_pr_still_withheld(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance needed #9.")
    by_ref = {row.ref: row for row in (parent, item)}

    def fake_api(endpoint, **kwargs):
        assert endpoint == "repos/{}/pulls/9".format(REPO)
        return {"state": "closed", "merged": False, "merged_at": None}

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_open_pr_still_withheld(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance requires owner/repo#9.")
    by_ref = {row.ref: row for row in (parent, item)}

    def fake_api(endpoint, **kwargs):
        if endpoint == "repos/{}/pulls/9".format(REPO):
            return {"state": "open", "merged": False, "merged_at": None}
        return {"state": "open"}

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_closed_issue_prerequisite_lifts_through_the_api(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance requires owner/repo#9.")
    by_ref = {row.ref: row for row in (parent, item)}
    calls = []

    def fake_api(endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint == "repos/{}/pulls/9".format(REPO):
            return None
        assert endpoint == "repos/{}/issues/9".format(REPO)
        return {"state": "closed"}

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is False
    assert calls == [
        "repos/{}/pulls/9".format(REPO),
        "repos/{}/issues/9".format(REPO),
    ]


def test_no_prerequisite_still_withheld():
    parent, item = _ticket(BODY, None)
    by_ref = {row.ref: row for row in (parent, item)}

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_free_text_prerequisite_stays_withheld():
    parent, item = _ticket(
        BODY, "Acceptance waits on the auth rework landing first.")
    by_ref = {row.ref: row for row in (parent, item)}

    assert funnel._decline_prerequisite_ref(item.decline_route, REPO) is None
    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_two_prerequisites_stay_withheld():
    parent, item = _ticket(
        BODY, "Acceptance is blocked on #9 and requires #10.")
    by_ref = {row.ref: row for row in (parent, item)}

    assert funnel._decline_prerequisite_ref(item.decline_route, REPO) is None
    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_digest_changed_behavior_is_unchanged():
    parent, item = _ticket(
        BODY + "\n\nAccept revised after shaping.",
        "Acceptance is blocked on #9.",
        digest=DIGEST,
    )
    blocker = _issue(9, "OPEN")
    by_ref = {row.ref: row for row in (parent, item, blocker)}

    assert funnel._decline_route_withholds_startability(item, by_ref) is False


def test_unresolvable_prerequisite_stays_withheld(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance is blocked on #9.")
    by_ref = {row.ref: row for row in (parent, item)}

    def fake_api(endpoint, **kwargs):
        return None

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


def test_prerequisite_lookup_error_stays_withheld(monkeypatch):
    parent, item = _ticket(BODY, "Acceptance is blocked on #9.")
    by_ref = {row.ref: row for row in (parent, item)}

    def fake_api(endpoint, **kwargs):
        raise funnel.GitHubError("offline")

    monkeypatch.setattr(funnel, "_gh_api_json", fake_api)

    assert funnel._decline_route_withholds_startability(item, by_ref) is True


@pytest.mark.parametrize("excerpt,expected", [
    ("Acceptance is blocked on #9.", REPO + "#9"),
    ("Acceptance needed #9.", REPO + "#9"),
    ("Acceptance requires owner/repo#9.", "owner/repo#9"),
    ("Blocked on the merge of #9.", REPO + "#9"),
    ("Needs PR #9 before this can land.", REPO + "#9"),
    ("#9 as a prerequisite for acceptance.", REPO + "#9"),
    ("Acceptance waits on the auth rework.", None),
    ("Unrelated #9 mention with no trigger words.", None),
])
def test_prerequisite_ref_parsing(excerpt, expected):
    assert funnel._decline_prerequisite_ref(
        {"decline_excerpt": excerpt}, REPO) == expected
