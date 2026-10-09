"""Domain single-select: repo defaults and the Fantasy-GM requirement (#2426)."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def item(**values):
    defaults = dict(
        repo="owner/command-center", number=1, title="Work", url="u",
        state="OPEN", status="Ready",
    )
    defaults.update(values)
    return funnel.Item(**defaults)


def test_domain_options_match_decided_enum():
    assert funnel.DOMAIN_OPTIONS == (
        "League", "AFL", "command-center",
        "workbench", "career-toolset", "jeffy-finance-agent",
    )


def test_domain_defaults_map_repos_to_domains():
    assert funnel.domain_default("command-center") == "command-center"
    assert funnel.domain_default("owner/command-center") == "command-center"
    assert funnel.domain_default("github-runners") == "command-center"
    assert funnel.domain_default("owner/github-runners") == "command-center"
    assert funnel.domain_default("workbench") == "workbench"
    assert funnel.domain_default("career-toolset") == "career-toolset"
    assert funnel.domain_default("jeffy-finance-agent") == "jeffy-finance-agent"


def test_fantasy_gm_has_no_default_and_stays_explicit():
    assert funnel.domain_default("Fantasy-GM") is None
    assert funnel.domain_default("owner/Fantasy-GM") is None


def test_unknown_repos_have_no_default_rather_than_a_guess():
    assert funnel.domain_default("owner/some-new-repo") is None


def test_fantasy_gm_project_without_domain_needs_one():
    assert funnel.needs_domain(item(repo="owner/Fantasy-GM")) is True


def test_fantasy_gm_project_with_league_or_afl_needs_none():
    assert funnel.needs_domain(
        item(repo="owner/Fantasy-GM", domain="League")) is False
    assert funnel.needs_domain(
        item(repo="owner/Fantasy-GM", domain="AFL")) is False


def test_defaulted_project_without_an_explicit_domain_needs_none():
    assert funnel.needs_domain(item(repo="owner/command-center")) is False
    assert funnel.needs_domain(item(repo="owner/github-runners")) is False
    assert funnel.needs_domain(item(repo="owner/workbench")) is False


def test_invalid_domain_does_not_satisfy_the_requirement():
    assert funnel.needs_domain(
        item(repo="owner/Fantasy-GM", domain="hobby")) is True


def test_tickets_and_closed_items_are_exempt_because_they_inherit():
    assert funnel.needs_domain(item(
        repo="owner/Fantasy-GM", parent="owner/Fantasy-GM#1")) is False
    assert funnel.needs_domain(item(
        repo="owner/Fantasy-GM", state="CLOSED")) is False


def test_effective_domain_prefers_parent_then_own_then_default():
    parent = item(number=1, repo="owner/Fantasy-GM", domain="League")
    ticket = item(number=2, repo="owner/Fantasy-GM", parent=parent.ref)
    by_ref = {parent.ref: parent, ticket.ref: ticket}
    assert funnel.effective_domain(ticket, by_ref) == "League"
    assert funnel.effective_domain(
        item(repo="owner/command-center"), {}) == "command-center"
    assert funnel.effective_domain(
        item(repo="owner/Fantasy-GM"), {}) is None


def test_from_node_reads_domain_from_both_projections():
    content = {
        "number": 1, "title": "Work", "url": "u", "state": "OPEN",
        "repository": {"nameWithOwner": "owner/Fantasy-GM"},
    }
    full = funnel._from_node({
        "id": "item-1", "status": {"name": "Ready"},
        "domain": {"name": "AFL"}, "content": content,
    })
    assert full is not None and full.domain == "AFL"
    startable = funnel._from_node({
        "id": "item-1", "status": {"name": "Ready"},
        "domain": {"name": "League"}, "startable": content,
    })
    assert startable is not None and startable.domain == "League"
    missing = funnel._from_node({
        "id": "item-1", "status": {"name": "Ready"}, "content": content,
    })
    assert missing is not None and missing.domain is None


def test_list_queries_select_domain():
    assert 'domain: fieldValueByName(name: "Domain")' in funnel.ITEM_NODE_FIELDS
    assert 'domain: fieldValueByName(name: "Domain")' in (
        funnel.STARTABLE_ITEM_NODE_FIELDS)
    assert 'domain: fieldValueByName(name: "Domain")' in (
        funnel.BEGIN_ITEM_NODE_FIELDS)


def project_payload(**overrides):
    fields = {
        "Status": funnel.STAGES,
        "Class": funnel.LADDER,
        "Origin": funnel.ORIGIN_OPTIONS,
        "Risk": funnel.RISK_OPTIONS,
        "Needs": funnel.NEEDS_OPTIONS,
        "Domain": funnel.DOMAIN_OPTIONS,
        funnel.LOCK_FIELD: (),
    }
    fields.update(overrides)
    nodes = [
        {"name": name, "options": [{"name": option} for option in options]}
        for name, options in fields.items()
    ]
    return {"user": {"projectV2": {"fields": {"nodes": nodes}}}}


def test_project_check_passes_with_domain_and_fails_without_it(monkeypatch):
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: project_payload())
    assert funnel.check_project_fields().ok

    payload = project_payload()
    payload["user"]["projectV2"]["fields"]["nodes"] = [
        node for node in payload["user"]["projectV2"]["fields"]["nodes"]
        if node["name"] != "Domain"
    ]
    monkeypatch.setattr(
        funnel, "gh_graphql", lambda query, **variables: payload)
    result = funnel.check_project_fields()
    assert not result.ok
    assert "missing field Domain" in result.found


def test_project_check_names_a_missing_domain_option(monkeypatch):
    options = [option for option in funnel.DOMAIN_OPTIONS if option != "AFL"]
    monkeypatch.setattr(
        funnel, "gh_graphql",
        lambda query, **variables: project_payload(Domain=options))
    result = funnel.check_project_fields()
    assert not result.ok
    assert "Domain is missing option AFL" in result.found
