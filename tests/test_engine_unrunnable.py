"""Evidence tickets defer review only when live issue state proves it."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import review  # noqa: E402

REPO = "nateprich-projects/command-center"
REVIEWED = REPO + "#1598"
FIXTURE = ROOT / "tests" / "fixtures" / "review_unrunnable_1581.json"


def live_fixture():
    return json.loads(FIXTURE.read_text())


def _parts(ref):
    repo, number = ref.rsplit("#", 1)
    return repo, int(number)


def install_github_fixture(monkeypatch, data):
    def graphql(query, **variables):
        repo = "{}/{}".format(variables["owner"], variables["name"])
        ref = "{}#{}".format(repo, variables["number"])
        if "subIssues(" in query:
            return {"repository": {"issue": {
                "subIssues": {
                    "nodes": [
                        {"number": _parts(child)[1],
                         "repository": {"nameWithOwner": _parts(child)[0]}}
                        for child in data["plans"].get(ref, [])
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
            }}}
        issue = data["issues"].get(ref)
        if issue is None:
            return {"repository": {"issue": None}}
        blockers = issue["blocked_by"]
        return {"repository": {"issue": {
            "state": issue["state"],
            "blockedBy": {
                "totalCount": len(blockers),
                "nodes": [
                    {"number": _parts(blocker)[1],
                     "repository": {"nameWithOwner": _parts(blocker)[0]}}
                    for blocker in blockers
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}}

    def gh_json(*args):
        endpoint = args[2]
        prefix = "repos/"
        suffix = "/parent"
        assert endpoint.startswith(prefix) and endpoint.endswith(suffix)
        issue_ref = endpoint[len(prefix):-len(suffix)]
        repo, number_text = issue_ref.rsplit("/issues/", 1)
        issue = data["issues"].get("{}#{}".format(repo, number_text))
        parent_ref = issue.get("parent") if issue else None
        if parent_ref is None:
            return None
        parent_repo, parent_number = _parts(parent_ref)
        return {"number": parent_number,
                "repository": {"full_name": parent_repo}}

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_gh_json", gh_json)


def test_1581_cycle_evidence_ticket_is_unrunnable(monkeypatch):
    install_github_fixture(monkeypatch, live_fixture())

    assert review.evidence_ticket_is_unrunnable(
        "Ticket 4 (#1600)", REVIEWED)


def test_later_same_plan_ticket_is_unrunnable_without_dependency_edges(
        monkeypatch):
    data = live_fixture()
    data["issues"][REPO + "#1600"]["blocked_by"] = []
    install_github_fixture(monkeypatch, data)

    assert review.evidence_ticket_is_unrunnable("#1600", REVIEWED)


def test_transitive_dependency_counts_across_plans(monkeypatch):
    install_github_fixture(monkeypatch, live_fixture())

    assert review.evidence_ticket_is_unrunnable(
        "{}#1701".format(REPO), REVIEWED)


@pytest.mark.parametrize("pointer", ["#1601", "#1999"])
def test_closed_or_nonexistent_evidence_ticket_is_not_unrunnable(
        monkeypatch, pointer):
    install_github_fixture(monkeypatch, live_fixture())

    assert not review.evidence_ticket_is_unrunnable(pointer, REVIEWED)


def test_unrelated_open_ticket_is_not_unrunnable(monkeypatch):
    install_github_fixture(monkeypatch, live_fixture())

    assert not review.evidence_ticket_is_unrunnable("#1700", REVIEWED)
