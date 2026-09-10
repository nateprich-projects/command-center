"""The opening command reports only queues it actually consulted."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import usage  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _bindings_never_touch_the_real_spool(monkeypatch):
    """`begin` now writes a binding record through the heartbeat (#497). The
    subprocess stubs in these tests cover the push but not the local spool, so
    stub the writer itself; tests that care patch it again explicitly."""
    import heartbeat

    monkeypatch.setattr(heartbeat, "record_binding", lambda *args, **kwargs: "pushed")


def _allow_begin(monkeypatch):
    monkeypatch.setattr(
        funnel.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage,
        "read_agent",
        lambda agent, timestamp: {"windows": {}},
    )
    monkeypatch.setattr(
        usage,
        "pace",
        lambda reading, timestamp, provider: {"over_pace": False},
    )
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: {})


def _begin(monkeypatch, capsys, *, breakdown):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    assert funnel.cmd_begin([], NOW, "zcode", "standard", False, breakdown) == 0
    return json.loads(capsys.readouterr().out)


def _ticket(number, parent, *, body="Risk: standard", klass="Improve",
            in_motion_since=None):
    project = funnel.Item(
        repo="nateprich/example",
        number=parent,
        title="Project {}".format(parent),
        url="https://github.com/nateprich/example/issues/{}".format(parent),
        state="OPEN",
        status="Building",
        klass=klass,
        children_total=1,
    )
    ticket = funnel.Item(
        repo=project.repo,
        number=number,
        title="Ticket {}".format(number),
        url="https://github.com/nateprich/example/issues/{}".format(number),
        state="OPEN",
        body=body,
        parent=project.ref,
        item_id="item-{}".format(number),
        in_motion_since=in_motion_since,
    )
    return project, ticket


def _codex_begin(monkeypatch, capsys, items, *, tier="standard",
                 repo_readiness=None, pr_facts=None):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(
        funnel, "ticket_pr_facts", lambda rows: pr_facts or {}
    )
    bodies = {item.number: item.body for item in items}
    monkeypatch.setattr(
        funnel, "_ticket_body", lambda repo, number: bodies.get(number) or ""
    )
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )
    assert funnel.cmd_begin(
        items, NOW, "codex", tier, False,
        repo_readiness=repo_readiness,
    ) == 0
    return json.loads(capsys.readouterr().out), writes


def _reconcile_begin(monkeypatch, capsys, items, rows, verdicts, merge_result=0):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: rows)
    monkeypatch.setattr(
        funnel, "latest_verdict", lambda repo, pr: verdicts.get(pr)
    )
    calls = []

    def merge(rows, now, repo, pr, confirmed):
        calls.append((repo, pr, confirmed))
        return merge_result

    monkeypatch.setattr(funnel, "cmd_merge", merge)
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)
    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    return json.loads(capsys.readouterr().out), calls


def test_begin_retries_an_approval_at_the_current_head(monkeypatch, capsys):
    project, ticket = _ticket(7, 6)
    result, calls = _reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        [{"number": 70, "headRefName": "ticket/7", "headRefOid": "new-head"}],
        {70: {"verdict": "approved", "head_sha": "new-head"}},
    )

    assert calls == [(ticket.repo, 70, True)]
    assert result["reconciled"] == [{
        "repo": ticket.repo,
        "pr": 70,
        "ref": ticket.ref,
        "result": "merged",
    }]
    assert ticket.state == "CLOSED"
    assert ticket.state_reason == "COMPLETED"


def test_begin_does_not_retry_an_old_or_rejected_verdict(monkeypatch, capsys):
    project, ticket = _ticket(8, 6)
    rows = [
        {"number": 80, "headRefName": "ticket/8", "headRefOid": "new-head"},
        {"number": 81, "headRefName": "ticket/8", "headRefOid": "new-head"},
    ]
    result, calls = _reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        rows,
        {
            80: {"verdict": "approved", "head_sha": "old-head"},
            81: {"verdict": "rejected", "head_sha": "new-head"},
        },
    )

    assert calls == []
    assert "reconciled" not in result
    assert ticket.state == "OPEN"


def test_begin_reports_a_merge_gate_refusal_without_closing_the_ticket(
    monkeypatch, capsys
):
    project, ticket = _ticket(9, 6)
    result, calls = _reconcile_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        [{"number": 90, "headRefName": "ticket/9", "headRefOid": "head"}],
        {90: {"verdict": "approved", "head_sha": "head"}},
        merge_result=1,
    )

    assert calls == [(ticket.repo, 90, True)]
    assert result["reconciled"] == [{
        "repo": ticket.repo,
        "pr": 90,
        "ref": ticket.ref,
        "result": "refused",
    }]
    assert ticket.state == "OPEN"


def test_begin_reconcile_runs_before_codex_queue_lookup(monkeypatch, capsys):
    project, ticket = _ticket(10, 6)
    events = []
    _allow_begin(monkeypatch)
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda *args: events.append("reconcile") or [],
    )
    monkeypatch.setattr(
        funnel,
        "clear_satisfied_blocks",
        lambda *args, **kwargs: events.append("clear") or [],
    )
    monkeypatch.setattr(
        funnel,
        "awaiting_review",
        lambda rows: events.append("awaiting") or set(),
    )
    monkeypatch.setattr(
        funnel,
        "next_ticket_for_tier",
        lambda *args, **kwargs: events.append("next") or None,
    )

    assert funnel.cmd_begin([project, ticket], NOW, "codex", "standard", False) == 0
    capsys.readouterr()
    assert events == ["reconcile", "clear", "awaiting", "next"]


def test_begin_reconcile_is_idempotent_when_the_pr_is_no_longer_open(
    monkeypatch, capsys
):
    project, ticket = _ticket(11, 6)
    rows = [
        {"number": 110, "headRefName": "ticket/11", "headRefOid": "head"}
    ]
    calls = []
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: rows)
    monkeypatch.setattr(
        funnel, "latest_verdict", lambda repo, pr: {
            "verdict": "approved", "head_sha": "head"
        }
    )

    def merge(items, now, repo, pr, confirmed):
        calls.append(pr)
        rows.clear()
        return 0

    monkeypatch.setattr(funnel, "cmd_merge", merge)
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)

    assert funnel.cmd_begin([project, ticket], NOW, "codex", "standard", False) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["reconciled"][0]["result"] == "merged"

    assert funnel.cmd_begin([project, ticket], NOW, "codex", "standard", False) == 0
    second = json.loads(capsys.readouterr().out)
    assert calls == [110]
    assert "reconciled" not in second


def _completed_project(number, *, klass="Improve", children_done=2,
                      carried_human_step=False):
    repo = "nateprich/example"
    project = funnel.Item(
        repo=repo,
        number=number,
        title="Project {}".format(number),
        url="https://github.com/{}/issues/{}".format(repo, number),
        state="OPEN",
        status="Building",
        klass=klass,
        item_id="project-{}".format(number),
        children_total=2,
        children_done=children_done,
        carried_human_step=carried_human_step,
    )
    children = [
        funnel.Item(
            repo=repo,
            number=number + offset,
            title="Ticket {}".format(number + offset),
            url="https://github.com/{}/issues/{}".format(repo, number + offset),
            state="CLOSED",
            parent=project.ref,
        )
        for offset in (1, 2)
    ]
    return [project] + children


def _closed_project_item(number, *, status, state_reason, labels=None):
    repo = "nateprich/example"
    return funnel.Item(
        repo=repo,
        number=number,
        title="Closed project {}".format(number),
        url="https://github.com/{}/issues/{}".format(repo, number),
        state="CLOSED",
        state_reason=state_reason,
        status=status,
        labels=list(labels or []),
        item_id="project-{}".format(number),
    )


def _begin_with_reconcile_wired(monkeypatch, capsys, items):
    calls = []
    graphql_calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "--agent" in argv:
            return SimpleNamespace(stdout="run-id\n", returncode=0, stderr="")
        return SimpleNamespace(stdout="", returncode=0, stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        usage, "read_agent", lambda agent, timestamp: {"windows": {}}
    )
    monkeypatch.setattr(
        usage, "pace", lambda reading, timestamp, provider: {"over_pace": False}
    )
    monkeypatch.setattr(funnel, "drift_since_approval", lambda item: [])
    monkeypatch.setattr(funnel, "gh_graphql", lambda query, **variables: (
        graphql_calls.append((query, variables)) or {}
    ))
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "done-option")
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "next_ticket_for_tier", lambda *args, **kwargs: None)

    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    return json.loads(capsys.readouterr().out), calls, graphql_calls


def test_begin_reconciles_a_completed_upkeep_project_and_records_marker(
    monkeypatch, capsys
):
    items = _completed_project(200)

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert result["auto_closed"] == [items[0].ref]
    assert items[0].state == "CLOSED"
    assert items[0].status == "Done"
    assert items[0].state_reason == "COMPLETED"
    assert (funnel.SET_FIELD, {
        "project": funnel.PROJECT_ID,
        "item": "project-200",
        "field": funnel.STATUS_FIELD_ID,
        "option": "done-option",
    }) in graphql_calls
    assert [
        "gh", "issue", "close", "200", "--repo", "nateprich/example",
        "--reason", "completed",
    ] in calls
    comments = [
        call[-1] for call in calls
        if call[:3] == ["gh", "issue", "comment"]
    ]
    assert len(comments) == 1
    assert comments[0].startswith(funnel.CLOSED_ITSELF_PREFIX)
    payload = json.loads(
        comments[0].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    assert payload == {
        "drift": [],
        "tickets": [
            {"ref": items[1].ref, "title": items[1].title},
            {"ref": items[2].ref, "title": items[2].title},
        ],
    }


@pytest.mark.parametrize(
    "klass,carried_human_step,children_done",
    [
        ("New", False, 2),
        ("Broken", True, 2),
        ("Improve", False, 1),
    ],
)
def test_begin_leaves_non_reconcilable_projects_untouched(
    monkeypatch, capsys, klass, carried_human_step, children_done
):
    items = _completed_project(
        210,
        klass=klass,
        carried_human_step=carried_human_step,
        children_done=children_done,
    )

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "auto_closed" not in result
    assert items[0].state == "OPEN"
    assert items[0].status == "Building"
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]
    assert not [
        call for call in graphql_calls
        if call[1].get("item") == "project-210"
    ]


def test_begin_reconcile_is_idempotent(monkeypatch, capsys):
    items = _completed_project(220)

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )
    assert result["auto_closed"] == [items[0].ref]

    calls.clear()
    graphql_calls.clear()
    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "auto_closed" not in result
    assert not graphql_calls
    assert not [
        call for call in calls
        if call[:3] in (["gh", "issue", "close"], ["gh", "issue", "comment"])
    ]


def test_begin_repairs_closed_terminal_statuses_and_stale_shaping_labels(
    monkeypatch, capsys
):
    completed = _closed_project_item(
        230, status="Building", state_reason="COMPLETED"
    )
    parked = _closed_project_item(
        231, status="Ideas", state_reason="NOT_PLANNED",
        labels=["needs-shaping"],
    )
    already_done = _closed_project_item(
        232, status="Done", state_reason="COMPLETED"
    )
    already_parked = _closed_project_item(
        233, status="Parked", state_reason="NOT_PLANNED"
    )
    open_item = funnel.Item(
        repo="nateprich/example",
        number=234,
        title="Open idea",
        url="https://github.com/nateprich/example/issues/234",
        state="OPEN",
        state_reason="NOT_PLANNED",
        status="Ideas",
        labels=["needs-shaping"],
        item_id="project-234",
    )
    items = [completed, parked, already_done, already_parked, open_item]

    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert result["reconciled"] == [completed.ref, parked.ref]
    assert completed.status == "Done"
    assert parked.status == "Parked"
    assert parked.labels == []
    assert already_done.status == "Done"
    assert already_parked.status == "Parked"
    assert open_item.status == "Ideas"
    assert open_item.labels == ["needs-shaping"]
    assert [variables for query, variables in graphql_calls] == [
        {
            "project": funnel.PROJECT_ID,
            "item": "project-230",
            "field": funnel.STATUS_FIELD_ID,
            "option": "done-option",
        },
        {
            "project": funnel.PROJECT_ID,
            "item": "project-231",
            "field": funnel.STATUS_FIELD_ID,
            "option": "done-option",
        },
    ]
    assert [
        call for call in calls
        if call[:3] == ["gh", "issue", "edit"]
    ] == [[
        "gh", "issue", "edit", "231", "--repo", "nateprich/example",
        "--remove-label", "needs-shaping",
    ]]

    calls.clear()
    graphql_calls.clear()
    result, calls, graphql_calls = _begin_with_reconcile_wired(
        monkeypatch, capsys, items
    )

    assert "reconciled" not in result
    assert not graphql_calls
    assert not [
        call for call in calls
        if call[:3] == ["gh", "issue", "edit"]
    ]


def test_codex_begin_records_heartbeat_before_selecting_and_claiming(
    monkeypatch, capsys
):
    project, ticket = _ticket(7, 6)
    items = [project, ticket]
    events = []

    def heartbeat(*args, **kwargs):
        events.append("heartbeat")
        return SimpleNamespace(stdout="run-id\n")

    monkeypatch.setattr(funnel.subprocess, "run", heartbeat)
    monkeypatch.setattr(
        funnel,
        "reconcile_approved_merges",
        lambda *args: events.append("reconcile") or [],
    )
    monkeypatch.setattr(usage, "read_agent", lambda agent, timestamp: {"windows": {}})
    monkeypatch.setattr(
        usage, "pace", lambda reading, timestamp, provider: {"over_pace": False}
    )
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: {})
    monkeypatch.setattr(
        funnel,
        "clear_satisfied_blocks",
        lambda *args, **kwargs: events.append("clear") or [],
    )
    monkeypatch.setattr(
        funnel,
        "next_ticket_for_tier",
        lambda *args, **kwargs: events.append("next") or ticket,
    )
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: events.append("claim")
    )

    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert events == ["heartbeat", "reconcile", "clear", "next", "claim"]


def test_codex_begin_skips_the_other_tier_before_claiming(monkeypatch, capsys):
    standard_project, standard = _ticket(8, 9, body="Risk: standard")
    escalated_project, escalated = _ticket(
        10, 11, body="Risk: escalated — concurrency"
    )
    result, writes = _codex_begin(
        monkeypatch, capsys,
        [escalated_project, escalated, standard_project, standard],
        tier="standard",
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == standard.ref
    assert [ref for ref, value in writes if value] == [standard.ref]


def test_codex_begin_respects_the_wip_limit(monkeypatch, capsys):
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            20 + index * 2, 21 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(40, 41)
    items.extend((project, ticket))

    result, writes = _codex_begin(monkeypatch, capsys, items)

    assert result["do"] == "stop"
    assert "lock held" in result["why"]
    assert writes == []


def test_codex_begin_allows_broken_preemption_at_the_wip_limit(
    monkeypatch, capsys
):
    items = []
    for index in range(funnel.WIP_LIMIT):
        project, ticket = _ticket(
            50 + index * 2, 51 + index * 2,
            in_motion_since=NOW,
        )
        items.extend((project, ticket))
    project, ticket = _ticket(70, 71, klass="Broken")
    items.extend((project, ticket))

    result, writes = _codex_begin(monkeypatch, capsys, items)

    assert result["do"] == "ticket"
    assert result["work"]["ref"] == ticket.ref
    assert [ref for ref, value in writes if value] == [ticket.ref]


def test_codex_begin_takes_over_the_five_branchless_claims(monkeypatch, capsys):
    items = []
    claims = []
    for index, number in enumerate((221, 214, 223, 224, 301)):
        project, claimed_ticket = _ticket(
            number,
            500 + index,
            in_motion_since=NOW - timedelta(minutes=31 + index),
        )
        items.extend((project, claimed_ticket))
        claims.append(claimed_ticket)
    facts = {item.ref: None for item in claims}

    result, writes = _codex_begin(
        monkeypatch, capsys, items, pr_facts=facts
    )

    assert result["do"] == "ticket"
    assert result["work"]["ref"] in facts
    assert {ref for ref, value in writes if not value} == (
        set(facts) - {result["work"]["ref"]}
    )
    assert [ref for ref, value in writes if value] == [result["work"]["ref"]]


def test_codex_begin_reports_repo_readiness_when_work_is_withheld(
    monkeypatch, capsys
):
    project, ticket = _ticket(72, 73)
    readiness = {
        ticket.repo: funnel.MemberRepoReadiness(
            ticket.repo, topic=True, ci_workflow=False,
            stock_labels=(), dependabot=False,
        ),
    }

    result, writes = _codex_begin(
        monkeypatch, capsys, [project, ticket],
        repo_readiness=readiness,
    )

    assert result["do"] == "stop"
    assert "no CI workflow" in result["why"]
    assert result["withheld"] == [{
        "ref": ticket.ref,
        "repo": ticket.repo,
        "reasons": ["no CI workflow"],
    }]
    assert writes == []


def test_main_supplies_repo_readiness_to_the_codex_begin_path(monkeypatch):
    project, ticket = _ticket(74, 75)
    rows = [project, ticket]
    readiness = {
        ticket.repo: funnel.MemberRepoReadiness(
            ticket.repo, topic=True, ci_workflow=True,
            stock_labels=(), dependabot=True,
        ),
    }
    received = []
    monkeypatch.setattr(funnel, "load_items", lambda: rows)
    monkeypatch.setattr(
        funnel,
        "repo_readiness_for_items",
        lambda items: received.append(items) or readiness,
    )
    monkeypatch.setattr(
        funnel,
        "cmd_begin",
        lambda items, now, agent, tier, idle, breakdown=False,
        routine_sha_literal=None, repo_readiness=None: (
            received.append(repo_readiness) or 0
        ),
    )

    assert funnel.main([
        "begin", "--agent", "codex", "--tier", "standard",
    ]) == 0
    assert received == [rows, readiness]


def test_begin_stop_reason_omits_breakdown_when_it_was_not_requested(
    monkeypatch, capsys
):
    calls = []

    def unexpected_breakdown_lookup(items):
        calls.append(items)
        raise AssertionError("breakdown queue was consulted without --breakdown")

    monkeypatch.setattr(funnel, "awaiting_breakdown", unexpected_breakdown_lookup)

    result = _begin(monkeypatch, capsys, breakdown=False)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review"
    assert "break down" not in result["why"]
    assert calls == []


def test_begin_stop_reason_names_both_empty_queues_when_both_were_consulted(
    monkeypatch, capsys
):
    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"


def test_begin_keeps_review_first_against_same_class_later_jobs(monkeypatch, capsys):
    work = {"pr": 7, "repo": "nateprich/beta", "ref": "nateprich/beta#19"}
    pending = SimpleNamespace(
        ref="nateprich-projects/command-center#20",
        url="https://github.com/nateprich-projects/command-center/issues/20",
        title="Break down project",
        repo="nateprich-projects/command-center",
        number=20,
        klass="Improve",
    )
    idea = SimpleNamespace(klass="Improve")
    breakdown_calls = []
    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [work])
    monkeypatch.setattr(
        funnel,
        "awaiting_breakdown",
        lambda items: breakdown_calls.append(items) or [pending],
    )
    monkeypatch.setattr(
        funnel, "shapeable_idea", lambda items, tier, reading: idea
    )

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "review"
    assert result["work"] == work
    assert breakdown_calls == [[]]


def test_begin_offers_shape_when_needs_decision_blocks_breakdown(monkeypatch, capsys):
    blocked = funnel.Item(
        repo="nateprich/example",
        number=38,
        title="Blocked breakdown",
        url="https://github.com/nateprich/example/issues/38",
        state="OPEN",
        status="Ready",
        labels=["blocked"],
        needs_decision="Where should this connector live?",
    )
    idea = funnel.Item(
        repo="nateprich/example",
        number=39,
        title="A standard idea",
        url="https://github.com/nateprich/example/issues/39",
        state="OPEN",
        status="Ideas",
        klass="New",
        body="Risk: standard",
    )

    monkeypatch.setattr(funnel, "review_queue", lambda items, tier: [])
    monkeypatch.setattr(funnel, "ideas", lambda items: [idea])
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    assert funnel.cmd_begin(
        [blocked, idea], NOW, "zcode", "standard", False, True
    ) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "shape"
    assert result["work"]["ref"] == idea.ref


def test_breakdown_work_carries_plan_access_signals(monkeypatch, capsys):
    item = SimpleNamespace(
        ref="nateprich-projects/command-center#25",
        repo="nateprich-projects/command-center",
        number=25,
        url="https://github.com/nateprich-projects/command-center/issues/25",
        title="Reach the funnel from general chat",
    )
    monkeypatch.setattr(funnel, "awaiting_breakdown", lambda items: [item])
    monkeypatch.setattr(
        funnel,
        "_ticket_body",
        lambda repo, number: "Cloudflare Tunnel and a fine-grained token",
    )

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "breakdown"
    assert result["work"]["access_signals"] == ["token", "tunnel"]


def _idea(number, title, body, klass=None):
    return SimpleNamespace(
        ref="nateprich-projects/command-center#{}".format(number),
        repo="nateprich-projects/command-center",
        number=number,
        url="https://github.com/nateprich-projects/command-center/issues/{}".format(number),
        title=title,
        body=body,
        klass=klass,
    )


def _review_job(ticket, pr=7):
    return {"pr": pr, "repo": ticket.repo, "ref": ticket.ref}


def _reviewer_begin(
    monkeypatch,
    capsys,
    items,
    *,
    review=None,
    breakdown_item=None,
    idea=None,
    breakdown=False,
):
    _allow_begin(monkeypatch)
    monkeypatch.setattr(funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(
        funnel,
        "review_queue",
        lambda rows, tier: [review] if review is not None else [],
    )
    monkeypatch.setattr(
        funnel,
        "awaiting_breakdown",
        lambda rows: ([breakdown_item] if breakdown_item is not None else []),
    )
    monkeypatch.setattr(
        funnel,
        "shapeable_idea",
        lambda rows, tier, reading: idea,
    )
    monkeypatch.setattr(funnel, "_ticket_body", lambda repo, number: "")

    assert funnel.cmd_begin(
        items, NOW, "zcode", "standard", False, breakdown
    ) == 0
    return json.loads(capsys.readouterr().out)


def test_broken_idea_preempts_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(101, 100, klass="Improve")
    idea = _idea(102, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "shape"
    assert result["work"]["ref"] == idea.ref


def test_improve_idea_does_not_preempt_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(103, 100, klass="Improve")
    idea = _idea(104, "Improve idea", "Risk: standard", klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_broken_review_is_before_a_broken_idea(monkeypatch, capsys):
    project, ticket = _ticket(105, 100, klass="Broken")
    idea = _idea(106, "Broken idea", "Risk: standard", klass="Broken")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_unclassed_idea_does_not_preempt_an_improve_review(monkeypatch, capsys):
    project, ticket = _ticket(107, 100, klass="Improve")
    idea = _idea(108, "Unclassed idea", "Risk: standard")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        idea=idea,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_same_class_candidates_keep_bottom_up_order(monkeypatch, capsys):
    project, ticket = _ticket(109, 100, klass="Improve")
    pending = funnel.Item(
        repo="nateprich/example",
        number=110,
        title="Project 110",
        url="https://github.com/nateprich/example/issues/110",
        state="OPEN",
        status="Ready",
        klass="Improve",
        children_total=0,
    )
    idea = _idea(111, "Improve idea", "Risk: standard", klass="Improve")

    result = _reviewer_begin(
        monkeypatch,
        capsys,
        [project, ticket],
        review=_review_job(ticket),
        breakdown_item=pending,
        idea=idea,
        breakdown=True,
    )

    assert result["do"] == "review"
    assert result["work"] == _review_job(ticket)


def test_shape_is_not_offered_when_the_first_idea_is_the_other_tier(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: [_idea(31, "Risky idea", "Risk: escalated")],
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"


def test_shape_is_not_offered_without_headroom(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: calls.append(items) or [_idea(32, "An idea", "Risk: standard")],
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: False)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "stop"
    assert result["why"] == "nothing to review and nothing to break down"
    assert calls == []


def test_shape_offers_only_the_first_idea_matching_the_run_tier(
    monkeypatch, capsys
):
    candidates = [
        _idea(33, "Escalated first", "Risk: escalated"),
        _idea(34, "Standard first", "Risk: standard"),
        _idea(35, "Standard second", "Risk: standard"),
    ]
    calls = []
    monkeypatch.setattr(
        funnel,
        "ideas",
        lambda items: calls.append(items) or candidates,
    )
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    result = _begin(monkeypatch, capsys, breakdown=True)

    assert result["do"] == "shape"
    assert result["work"] == {
        "ref": candidates[1].ref,
        "url": candidates[1].url,
        "title": candidates[1].title,
    }
    assert len(calls) == 1


def test_muse_standard_schedule_is_offered_a_standard_idea_on_an_unmetered_reading(
    monkeypatch, capsys
):
    """#86, revised by Nate 2026-09-09: Muse's standard schedule shapes too.
    The real headroom gate must admit an unmetered reading, not a patched one."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"unmetered": True, "windows": {}},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(36, "A standard idea", "Risk: standard")],
    )

    assert funnel.cmd_begin([], NOW, "muse", "standard", False, True) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["unmetered"] is True
    assert result["do"] == "shape"
    assert result["work"]["ref"] == "nateprich-projects/command-center#36"


def test_muse_escalated_schedule_is_never_offered_shaping(monkeypatch, capsys):
    """The hourly escalated schedule is review-only: no --breakdown, and an
    escalated idea is not offered either, so nothing routes shaping there."""
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="run-id\n"),
    )
    monkeypatch.setattr(
        usage, "read_agent",
        lambda agent, timestamp: {"unmetered": True, "windows": {}},
    )
    monkeypatch.setattr(
        funnel, "ideas",
        lambda items: [_idea(37, "A standard idea", "Risk: standard")],
    )

    assert funnel.cmd_begin([], NOW, "muse", "escalated", False, False) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["do"] == "stop"


def test_begin_binds_the_ticket_it_issues_to_the_run(monkeypatch, capsys):
    """The run id alone let a stale finish land on the wrong run (#497)."""
    import heartbeat

    bound = []
    monkeypatch.setattr(
        heartbeat, "record_binding",
        lambda agent, run, do, work: bound.append((agent, run, do, work)) or "pushed",
    )
    project, ticket = _ticket(7, 6)
    result, writes = _codex_begin(monkeypatch, capsys, [project, ticket])

    assert result["do"] == "ticket"
    assert result["bound"] == {"do": "ticket", "work": ticket.ref}
    assert bound == [("codex", "run-id", "ticket", ticket.ref)]


def test_a_stop_run_binds_nothing(monkeypatch, capsys):
    import heartbeat

    bound = []
    monkeypatch.setattr(
        heartbeat, "record_binding",
        lambda *args: bound.append(args) or "pushed",
    )
    result, writes = _codex_begin(monkeypatch, capsys, [])

    assert result["do"] == "stop"
    assert "bound" not in result and bound == []
