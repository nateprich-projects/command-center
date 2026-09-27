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
REJECTED_1612 = ROOT / "tests" / "fixtures" / "review_rejected_1612.json"


def live_fixture():
    return json.loads(FIXTURE.read_text())


def rejected_1612_fixture():
    return json.loads(REJECTED_1612.read_text())


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


def test_rejected_1612_packet_carries_a_verified_deferred_answer(monkeypatch):
    install_github_fixture(monkeypatch, live_fixture())
    fixture = rejected_1612_fixture()
    packet = fixture["packet"]

    result = review.annotate_unrunnable_premises(packet)

    assert result is packet
    premise = packet["plan_premises"][0]["premises"][0]
    assert premise["deferred_answer"] == fixture["expected_deferred_answer"]
    assert "Probe the parent plan" in fixture["rejected_requirement"]


def test_lister_requirement_is_normalized_to_the_verified_deferral():
    fixture = rejected_1612_fixture()
    packet = fixture["packet"]
    packet["plan_premises"][0]["premises"][0]["deferred_answer"] = (
        fixture["expected_deferred_answer"])
    probe = fixture["rejected_requirement"]

    result = review.normalize_plan_premise_requirements(packet, [probe])

    assert result == [
        "Defer the inferred premise 'The split framer may itself still go "
        "silent' to its evidence pointer 'ticket 4, #1600, checks' until "
        "ticket #1598 is complete."
    ]
    assert all("Probe the parent plan" not in row for row in result)


def test_verified_deferral_does_not_remain_unsure_at_judgement():
    fixture = rejected_1612_fixture()
    packet = fixture["packet"]
    packet["plan_premises"][0]["premises"][0]["deferred_answer"] = (
        fixture["expected_deferred_answer"])
    requirement = review.normalize_plan_premise_requirements(
        packet, [fixture["rejected_requirement"]])[0]

    result = review.mark_verified_premise_requirements(packet, [{
        "requirement": requirement,
        "status": "unsure",
        "evidence": "the model could not resolve this",
    }])

    assert result == [{
        "requirement": requirement,
        "status": "met",
        "evidence": (
            "Verified packet deferral: the inferred premise's evidence "
            "pointer and reviewed ticket match its live deferred_answer."),
    }]


def test_unverified_deferred_fields_keep_the_probe_requirement():
    fixture = rejected_1612_fixture()
    packet = fixture["packet"]
    packet["plan_premises"][0]["premises"][0]["deferred_answer"] = {
        **fixture["expected_deferred_answer"],
        "reviewed_ticket": REPO + "#1597",
    }

    assert review.normalize_plan_premise_requirements(
        packet, [fixture["rejected_requirement"]]) == [
            fixture["rejected_requirement"]]


def test_inferred_premises_attach_records_for_cited_tickets_and_prs(
        monkeypatch):
    fetched = []
    packet = {
        "ticket": {"ref": REPO + "#1652"},
        "plan_premises": [{
            "parent_ref": REPO + "#1626",
            "available": True,
            "premises": [{
                "claim": ("PR #1613 for ticket #1597 has the same "
                          "later-sibling evidence shape as PR #1612"),
                "evidence": (REPO + "#1613 for #1597 shares #1581 "
                            "later-sibling evidence shape"),
                "label": "inferred",
            }],
        }],
    }

    def fake_pr(repo, number):
        fetched.append(("pr", repo, number))
        return {
            "number": number,
            "title": "PR {}".format(number),
            "state": "CLOSED",
            "mergedAt": "2026-09-27T00:00:00Z",
            "headRefName": "ticket/{}".format(number),
            "headRefOid": "sha-{}".format(number),
            "files": [{"path": "engine/review.py"}],
        }

    def fake_ticket(repo, number):
        fetched.append(("issue", repo, number))
        parent = None
        body = "Body {}".format(number)
        if number == 1597:
            parent = {
                "number": 1581,
                "ref": REPO + "#1581",
                "body": ("# Plan\n\n## Premises\n\n"
                         "None recorded.\n\nProposed class: Broken\n"),
                "comments": [],
            }
        return {
            "ref": REPO + "#{}".format(number),
            "number": number,
            "title": "Issue {}".format(number),
            "url": "https://github.com/{}/issues/{}".format(REPO, number),
            "body": body,
            "state": "CLOSED",
            "parent": parent,
            "comments": [],
        }

    monkeypatch.setattr(review, "fetch_pr", fake_pr)
    monkeypatch.setattr(review, "fetch_pr_comments", lambda repo, number: {
        "status": "empty", "message": "No PR comments.", "comments": []})
    monkeypatch.setattr(review, "fetch_ticket", fake_ticket)

    result = review.attach_inferred_premise_evidence(packet, REPO)
    premise = result["plan_premises"][0]["premises"][0]
    records = premise["referenced_evidence"]["records"]

    assert premise["referenced_evidence"]["status"] == "available"
    assert {row["ref"] for row in records} == {
        REPO + "#1581", REPO + "#1597", REPO + "#1612", REPO + "#1613"}
    assert {row["kind"] for row in records} == {"issue", "pull_request"}
    assert {row[1] for row in fetched} == {REPO}
    assert {row[2] for row in fetched} == {1581, 1597, 1612, 1613}
    assert any(row.get("comments", {}).get("status") == "empty"
               for row in records if row["kind"] == "pull_request")


def test_unreadable_referenced_inferred_evidence_is_not_reported_empty(
        monkeypatch):
    packet = {
        "ticket": {"ref": REPO + "#9"},
        "plan_premises": [{
            "available": True,
            "premises": [{
                "claim": "The claim has support",
                "evidence": "PR #1700",
                "label": "inferred",
            }],
        }],
    }
    monkeypatch.setattr(
        review, "fetch_pr",
        lambda repo, number: (_ for _ in ()).throw(
            funnel.GitHubError("transport detail")))

    review.attach_inferred_premise_evidence(packet, REPO)

    evidence = packet["plan_premises"][0]["premises"][0][
        "referenced_evidence"]
    assert evidence["status"] == "partial"
    assert evidence["records"] == [{
        "ref": REPO + "#1700",
        "kind": "pull_request",
        "status": "could_not_read",
        "message": "Could not read this cited reference.",
    }]


@pytest.mark.parametrize("label", ["measured", "documented"])
def test_unrunnable_measured_and_documented_premises_are_label_errors(
        monkeypatch, label):
    install_github_fixture(monkeypatch, live_fixture())
    packet = rejected_1612_fixture()["packet"]
    premise = packet["plan_premises"][0]["premises"][0]
    premise["label"] = label

    review.annotate_unrunnable_premises(packet)

    assert "deferred_answer" not in premise
    assert premise["label_error"] == {
        "status": "verified",
        "label": label,
        "evidence_pointer": "ticket 4, #1600, checks",
        "reviewed_ticket": REVIEWED,
        "reason": (
            "live issue state shows the named evidence ticket is open and "
            "cannot run before the reviewed ticket is complete"),
    }


@pytest.mark.parametrize("label", ["measured", "documented"])
def test_verified_forward_label_error_becomes_a_canonical_rejection(
        monkeypatch, label):
    install_github_fixture(monkeypatch, live_fixture())
    fixture = rejected_1612_fixture()
    packet = fixture["packet"]
    premise = packet["plan_premises"][0]["premises"][0]
    premise["label"] = label
    review.annotate_unrunnable_premises(packet)

    result = review.normalize_plan_premise_requirements(
        packet, [fixture["rejected_requirement"]])

    assert result == [
        "Reject the {} premise 'The split framer may itself still go silent' "
        "as a labeling error because its evidence pointer 'ticket 4, #1600, "
        "checks' names an open ticket that cannot run before ticket #1598 is "
        "complete.".format(label)
    ]
    marked = review.mark_verified_premise_requirements(packet, [{
        "requirement": result[0],
        "status": "unsure",
        "evidence": "the evidence is not available yet",
    }])
    assert marked == [{
        "requirement": result[0],
        "status": "unmet",
        "evidence": (
            "Verified labeling error: the measured/documented premise's "
            "evidence pointer names an open ticket that cannot run before "
            "the reviewed ticket is complete."),
    }]


def test_unverified_label_error_fields_keep_the_model_probe():
    fixture = rejected_1612_fixture()
    packet = fixture["packet"]
    premise = packet["plan_premises"][0]["premises"][0]
    premise.update({
        "label": "measured",
        "label_error": {
            "status": "verified",
            "label": "measured",
            "evidence_pointer": "ticket 4, #1600, checks",
            "reviewed_ticket": REPO + "#1597",
        },
    })

    assert review.normalize_plan_premise_requirements(
        packet, [fixture["rejected_requirement"]]) == [
            fixture["rejected_requirement"]]


def test_a_checkable_inferred_pointer_keeps_the_probe_path(monkeypatch):
    install_github_fixture(monkeypatch, live_fixture())
    packet = rejected_1612_fixture()["packet"]
    premise = packet["plan_premises"][0]["premises"][0]
    premise["evidence"] = "#1700"

    review.annotate_unrunnable_premises(packet)

    assert "deferred_answer" not in premise


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
