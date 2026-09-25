"""The brief derives decline routing from bounded GitHub issue history."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


CUTOFF = datetime(2026, 9, 24, 21, 25, 26, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)


def _comment(body, when):
    return {
        "body": body,
        "createdAt": when.isoformat().replace("+00:00", "Z"),
    }


def _decline(reason, when, run):
    return funnel.append_provenance(
        "{} {}".format(funnel.DECLINED_PREFIX, reason),
        "agent",
        at=when,
        run=run,
        agent="codex",
    )


def _review_route(reason, when, run):
    record = {
        "type": "accept-body-conflict",
        "decline_excerpt": reason,
        "conflict_pointer": "plan.md#routine-freeze-while-794-lands",
    }
    body = "\n".join((
        "**Review routing: Accept/body conflict**",
        "",
        funnel.DECLINE_ROUTING_REVIEW_MARKER,
        "```json",
        json.dumps(record, ensure_ascii=False, indent=2),
        "```",
    ))
    return funnel.append_provenance(
        body, "agent", at=when, run=run, agent="codex"
    )


def _issue(number, comments, *, state="OPEN", state_reason=None,
           closed_at=None, labels=(), body="", blocked_refs=()):
    blocked_by = []
    for ref in blocked_refs:
        repo, number_text = ref.rsplit("#", 1)
        blocked_by.append({
            "number": int(number_text),
            "repository": {"nameWithOwner": repo},
        })
    return {
        "__typename": "Issue",
        "number": number,
        "repository": {"nameWithOwner": funnel.REPO},
        "body": body,
        "state": state,
        "stateReason": state_reason,
        "closedAt": closed_at,
        "labels": {
            "totalCount": len(labels),
            "nodes": [{"name": label} for label in labels],
        },
        "blockedBy": {
            "totalCount": len(blocked_by),
            "nodes": blocked_by,
        },
        "comments": {
            "pageInfo": {"hasPreviousPage": False},
            "nodes": comments,
        },
    }


def test_decline_routing_counts_each_outcome_and_excludes_pre_cutoff_history(
    monkeypatch,
):
    edge_at = CUTOFF + timedelta(minutes=1)
    defer_at = CUTOFF + timedelta(minutes=2)
    route_at = CUTOFF + timedelta(minutes=3)
    blocked_at = CUTOFF + timedelta(minutes=4)
    before_cutoff = CUTOFF - timedelta(seconds=1)
    route_run = "codex-route-run"
    route_reason = (
        "The ticket Accept contradicts current repo rules; conflicting text "
        "is at plan.md#routine-freeze-while-794-lands."
    )
    defer_reason = (
        "Defer narrative dedup as a separate idea in this close note. The "
        "named condition for including it—coverage by the same shaping edit—"
        "is absent from merged #1369. No code changed."
    )
    issues = [
        _issue(
            1,
            [_comment(_decline("Unlanded prerequisite #81 is still open.", edge_at,
                               "edge-run"), edge_at)],
            blocked_refs=(f"{funnel.REPO}#81",),
        ),
        _issue(
            2,
            [_comment(_decline(defer_reason, defer_at, "defer-run"), defer_at)],
            state="CLOSED",
            state_reason="COMPLETED",
            closed_at=(defer_at + timedelta(seconds=1)).isoformat(),
            body="Accept: an explicit defer note is accepted as proof of completion.",
        ),
        _issue(
            3,
            [
                _comment(_decline(route_reason, route_at, route_run), route_at),
                _comment(
                    _review_route(
                        route_reason, route_at + timedelta(seconds=1), route_run
                    ),
                    route_at + timedelta(seconds=1),
                ),
            ],
        ),
        _issue(
            4,
            [_comment(_decline("Nate must resolve this hold.", blocked_at,
                               "blocked-run"), blocked_at)],
            labels=("blocked",),
        ),
        _issue(
            5,
            [_comment(_decline("This was hand-fixed before the cutoff.",
                               before_cutoff, "old-run"), before_cutoff)],
            labels=("blocked",),
        ),
    ]
    monkeypatch.setattr(
        funnel,
        "_gh_json",
        lambda *args: {"mergedAt": CUTOFF.isoformat()},
    )
    queries = []

    def gh_graphql(query, **variables):
        queries.append((query, variables))
        return {
            "search": {
                "issueCount": len(issues),
                "nodes": issues,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }

    monkeypatch.setattr(funnel, "gh_graphql", gh_graphql)
    parsed_comments = []
    parse_provenance = funnel.parse_provenance

    def parse_seen(body):
        parsed_comments.append(body)
        return parse_provenance(body)

    monkeypatch.setattr(funnel, "parse_provenance", parse_seen)

    report = funnel.decline_routing_metric([], NOW)

    assert report["status"] == "available"
    assert report["window_days"] == 30
    assert report["cutoff_pr"] == 1447
    assert report["cutoff_at"] == "2026-09-24T21:25:26Z"
    assert report["from"] == report["cutoff_at"]
    assert report["declines"] == 4
    assert report["became_edge"] == 1
    assert report["closed_as_proven_defer"] == 1
    assert report["routed_to_review"] == 1
    assert report["stayed_blocked"] == 1
    assert report["unclassified"] == 0
    assert len(queries) == 1
    search = queries[0][1]["search"]
    assert 'in:comments "Declined:"' in search
    assert "updated:>=2026-09-24" in search
    assert all("This was hand-fixed" not in body for body in parsed_comments)


def test_decline_routing_ignores_unrelated_edges_for_unknown_declines():
    decline_at = CUTOFF + timedelta(minutes=1)
    comments = [_comment(
        _decline("A dependency is already linked.", decline_at, "unknown-run"),
        decline_at,
    )]
    issue = _issue(
        10, comments, blocked_refs=(f"{funnel.REPO}#81",)
    )
    rows = funnel._decline_routing_comment_rows(issue, CUTOFF)
    events = funnel._codex_decline_events(rows, CUTOFF, NOW)

    assert funnel._decline_routing_outcome(
        issue, rows, events[0][0], events[0][1], events[0][2], CUTOFF, NOW
    ) is None


def test_decline_search_refuses_to_truncate_a_window_inside_the_comment_tail():
    issue = _issue(
        11,
        [_comment("Recent unrelated comment.", NOW - timedelta(minutes=1))],
    )
    issue["comments"]["pageInfo"]["hasPreviousPage"] = True

    try:
        funnel._decline_routing_comment_rows(issue, CUTOFF)
    except funnel.GitHubError as exc:
        assert "bounded comment page" in str(exc)
    else:
        raise AssertionError("an incomplete in-window comment tail was accepted")


def test_decline_metric_refuses_to_guess_from_a_truncated_blocker_list():
    decline_at = CUTOFF + timedelta(minutes=1)
    reason = "Unlanded prerequisite #81 is still open."
    issue = _issue(
        12,
        [_comment(_decline(reason, decline_at, "edge-run"), decline_at)],
        labels=("blocked",),
        blocked_refs=tuple(
            "{}#{}".format(funnel.REPO, number)
            for number in range(100, 200)
        ),
    )
    issue["blockedBy"]["totalCount"] = 101
    rows = funnel._decline_routing_comment_rows(issue, CUTOFF)
    event = funnel._codex_decline_events(rows, CUTOFF, NOW)[0]

    try:
        funnel._decline_routing_outcome(
            issue, rows, event[0], event[1], event[2], CUTOFF, NOW
        )
    except funnel.GitHubError as exc:
        assert "exceed the bounded response" in str(exc)
    else:
        raise AssertionError("a truncated blocker list was treated as blocked")
