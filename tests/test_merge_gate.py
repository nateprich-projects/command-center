"""The model decides approval; Python decides merge.

A model adds value judging whether a diff matches the plan. It adds none by
being the component that types `gh pr merge` — and being that component is what
makes an unattended merge impossible to audit afterwards, which is why v0 is
still unaccepted. So the reviewer writes a verdict in one checkable shape, and
every merge condition is enforced outside the model.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
REPO = "owner/repo"
SHA = "abc123def456"


def verdict(**kw):
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA, "blocking": []}
    body.update(kw)
    return funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(body) + "\n```"


def items(klass="Improve", children_total=1, children_done=0,
          other_ticket=False, origin="agent"):
    body = (
        funnel.origin_block(origin, at=NOW, run="merge-run", agent="codex")
        if origin in funnel.ORIGIN_VOICES else None
    )
    project = Item(repo=REPO, number=1, title="p", url="", state="OPEN",
                   body=body, status="Building", klass=klass,
                   origin=("agent" if origin == "agent" else "Nate"),
                   risk="standard", needs="none",
                   item_id="project-id",
                   children_total=children_total, children_done=children_done)
    ticket = Item(repo=REPO, number=9, title="t", url="", state="OPEN",
                  parent=REPO + "#1", origin="agent", risk="standard",
                  needs="none")
    rows = [project, ticket]
    if other_ticket:
        rows.append(Item(repo=REPO, number=10, title="other", url="",
                         state="OPEN", parent=REPO + "#1", origin="agent",
                         risk="standard", needs="none"))
    return rows


def wire(monkeypatch, pr_json, comments):
    def fake(*args):
        if "comments" in args:
            return {"comments": [{"body": b} for b in comments]}
        return pr_json

    node = dict(pr_json)
    node.update({"number": 5, "title": "PR 5", "url": "https://example.invalid/5"})
    node["comments"] = {"nodes": [{"body": b} for b in comments]}
    node["commits"] = {
        "nodes": [{
            "commit": {
                "statusCheckRollup": {
                    "contexts": {"nodes": list(pr_json.get("statusCheckRollup") or [])}
                }
            }
        }]
    }

    def graphql(query, **variables):
        return {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
            "repo0": {
                "pullRequests": {
                    "nodes": [node],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "refs": {
                    "nodes": [{"name": "ticket/9"}],
                    "pageInfo": {"hasNextPage": False},
                },
            },
        }

    monkeypatch.setattr(funnel, "_gh_json", fake)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)


def pr(**kw):
    data = {"state": "OPEN", "headRefName": "ticket/9", "headRefOid": SHA,
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "tests", "conclusion": "SUCCESS"}]}
    data.update(kw)
    return data


# -- the verdict format -------------------------------------------------------

def test_a_verdict_is_read_out_of_the_comment():
    found = funnel.parse_verdict(verdict(note="looks right"))
    assert found["verdict"] == "approved" and found["head_sha"] == SHA


def test_ordinary_prose_is_not_a_verdict():
    assert funnel.parse_verdict("Looks good to me, merging.") is None


def test_a_malformed_verdict_is_not_half_read():
    assert funnel.parse_verdict(funnel.REVIEW_MARKER + "\n{not json") is None


def test_the_newest_verdict_wins(monkeypatch):
    """A re-review after a fix is a fresh read; the older one must not linger."""
    wire(monkeypatch, pr(), [verdict(verdict="rejected"), verdict()])
    assert funnel.latest_verdict(REPO, 5)["verdict"] == "approved"


def _wire_review_confirmation(monkeypatch):
    wire(monkeypatch, pr(), [])
    monkeypatch.setattr(
        funnel,
        "_run_gh",
        lambda *args, **kwargs: type(
            "Run", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )(),
    )


def test_review_cli_confirmation_remains_on_stdout(monkeypatch, capsys):
    _wire_review_confirmation(monkeypatch)

    assert funnel.cmd_review(
        REPO, 5, "approved", "green", [], None, run="review-run",
        agent="codex",
    ) == 0

    captured = capsys.readouterr()
    assert "recorded approved on PR #5 against {} in {}".format(
        SHA[:12], REPO
    ) in captured.out
    assert captured.err == ""


def test_ordinary_review_rejection_remains_on_stdout(monkeypatch, capsys):
    _wire_review_confirmation(monkeypatch)

    assert funnel.cmd_review(
        REPO, 5, "rejected", "green", ["needs a correction"], "review note",
        run="review-run", agent="codex",
    ) == 0

    captured = capsys.readouterr()
    assert "recorded rejected on PR #5 against {} in {}".format(
        SHA[:12], REPO
    ) in captured.out
    assert captured.err == ""


# -- the gate -----------------------------------------------------------------

def test_everything_in_order_merges(monkeypatch):
    wire(monkeypatch, pr(), [verdict()])
    assert funnel.merge_blockers(REPO, 5, items(), NOW) == []


def test_a_conflicting_branch_blocks_with_a_rebase_reason(monkeypatch):
    wire(monkeypatch, pr(mergeable="CONFLICTING"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any(
        "ticket/9" in reason
        and "conflicting" in reason
        and "base" in reason
        and "rebase" in reason
        for reason in why
    )


def test_unknown_mergeability_blocks_with_retry_reason(monkeypatch):
    wire(monkeypatch, pr(mergeable="UNKNOWN"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any("not been computed" in reason and "retry" in reason for reason in why)
    assert not any("rebase" in reason for reason in why)


def test_missing_mergeability_blocks_with_retry_reason(monkeypatch):
    data = pr()
    del data["mergeable"]
    wire(monkeypatch, data, [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any("not been computed" in reason and "retry" in reason for reason in why)


def test_a_mixed_review_and_provenance_comment_still_allows_merge(monkeypatch):
    mixed = verdict() + "\n\n" + funnel.provenance_block(
        "agent", at=NOW, run="run-review", agent="zcode"
    )
    wire(monkeypatch, pr(), [mixed])
    assert funnel.merge_blockers(REPO, 5, items(), NOW) == []


def test_a_commit_pushed_after_approval_blocks_the_merge(monkeypatch):
    """The decisive check. Without it an approval authorises a diff it never saw:
    approve, push anything, merge."""
    wire(monkeypatch, pr(headRefOid="9999newcommit"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, items(), NOW)
    assert any("not the head" in w for w in why)


def test_no_verdict_blocks(monkeypatch):
    wire(monkeypatch, pr(), [])
    assert "no review verdict recorded" in funnel.merge_blockers(REPO, 5, items(), NOW)


def test_a_rejected_verdict_blocks(monkeypatch):
    wire(monkeypatch, pr(), [verdict(verdict="rejected")])
    assert any("rejected" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_red_ci_blocks(monkeypatch):
    wire(monkeypatch, pr(statusCheckRollup=[
        {"name": "tests", "conclusion": "FAILURE"}]), [verdict()])
    assert any("CI not green" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_absent_ci_blocks_rather_than_passing_vacuously(monkeypatch):
    """No checks is not the same as checks passing."""
    wire(monkeypatch, pr(statusCheckRollup=[]), [verdict()])
    assert any("no CI checks" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_a_branch_that_is_not_a_ticket_blocks(monkeypatch):
    wire(monkeypatch, pr(headRefName="feature/whatever"), [verdict()])
    assert any("not a ticket/" in w for w in funnel.merge_blockers(REPO, 5, items(), NOW))


def test_a_ticket_whose_project_is_not_building_blocks(monkeypatch):
    rows = items()
    rows[0].status = "Ready"
    wire(monkeypatch, pr(), [verdict()])
    assert any("not Building" in w for w in funnel.merge_blockers(REPO, 5, rows, NOW))


def test_a_ticket_with_a_non_building_parent_keeps_the_old_message(monkeypatch):
    """The ticket case of the split keeps the exact old wording (#1042)."""
    rows = items()
    rows[0].status = "Ready"
    wire(monkeypatch, pr(), [verdict()])
    why = funnel.merge_blockers(REPO, 5, rows, NOW)
    assert (REPO + "#9's project is not Building") in why
    assert not any("no parent" in w for w in why)


def test_a_parent_less_building_project_branch_merges(monkeypatch):
    """#1042: a parent-less branch item is its own parent for the gate."""
    wire(monkeypatch, pr(headRefName="ticket/1"), [verdict()])
    assert funnel.merge_blockers(REPO, 5, items(), NOW) == []


def test_a_parent_less_non_building_project_branch_refuses(monkeypatch):
    rows = items()
    rows[0].status = "Ready"
    wire(monkeypatch, pr(headRefName="ticket/1"), [verdict()])
    why = funnel.merge_blockers(REPO, 5, rows, NOW)
    assert any("no parent" in w and "Building" in w for w in why)
    assert any("ticket/<n>" in w for w in why)
    assert (REPO + "#1's project is not Building") not in why


def test_every_failure_is_reported_not_just_the_first(monkeypatch):
    """A gate that says only 'no' makes the reviewer guess which one to fix."""
    wire(monkeypatch, pr(headRefName="nope", statusCheckRollup=[]), [])
    assert len(funnel.merge_blockers(REPO, 5, items(), NOW)) >= 3


# -- a conflicting branch hands the ticket back -------------------------------

def _gate_rejection_wired(monkeypatch, pr_json, comments):
    posted = []

    def graphql(query, **variables):
        node = dict(pr_json)
        node.update({"number": 5, "title": "PR 5", "url": "https://example.invalid/5"})
        node["comments"] = {
            "nodes": [{"body": body} for body in comments + posted]
        }
        node["commits"] = {
            "nodes": [{
                "commit": {
                    "statusCheckRollup": {
                        "contexts": {
                            "nodes": list(pr_json.get("statusCheckRollup") or [])
                        }
                    }
                }
            }]
        }
        return {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
            "repo0": {
                "pullRequests": {
                    "nodes": [node],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "refs": {
                    "nodes": [{"name": "ticket/9"}],
                    "pageInfo": {"hasNextPage": False},
                },
            },
        }

    def fake_gh_json(*args):
        if "list" in args:
            # The gate rejection is written at SHA, which is still the head:
            # the hand-back to the engineer lasts exactly while that holds (#487).
            return [{"headRefName": "ticket/9", "headRefOid": SHA, "number": 5}]
        if "comments" in args:
            return {"comments": [
                {"body": body} for body in comments + posted
            ]}
        return pr_json

    def fake_run(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "comment"]:
            posted.append(argv[-1])
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    monkeypatch.setattr(funnel.subprocess, "run", fake_run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    return posted


def test_conflicting_approved_head_releases_claim_for_rebase(monkeypatch):
    rows = items()
    rows[1].item_id = "ticket-project-item"
    rows[1].in_motion_since = NOW
    pr_json = pr(mergeable="CONFLICTING")
    posted = _gate_rejection_wired(
        monkeypatch, pr_json, [verdict()]
    )

    assert funnel.cmd_merge(rows, NOW, REPO, 5, False) == 1
    assert pr_json["state"] == "OPEN"
    assert rows[1].in_motion_since is None
    assert funnel.cmd_claim(rows, NOW, REPO + "#9") == 0
    assert len(posted) == 1
    rejection = funnel.parse_verdict(posted[0])
    assert rejection == {
        "blocking": [
            "branch 'ticket/9'" + funnel.CONFLICTING_BRANCH_SUFFIX
        ],
        "ci": "unknown",
        "head_sha": SHA,
        "reviewed_at": rejection["reviewed_at"],
        "verdict": "rejected",
    }
    provenance = funnel.parse_provenance(posted[0])
    assert provenance["voice"] == "agent"
    assert provenance["agent"] == funnel.MERGE_GATE_AGENT

    blocked = funnel.awaiting_review(rows)
    assert [item.ref for item in funnel.startable(
        rows, awaiting_review=blocked
    )] == [REPO + "#9"]

    assert funnel.cmd_merge(rows, NOW, REPO, 5, False) == 1
    assert len(posted) == 1


def test_existing_conflict_rejection_preserves_a_new_rebase_claim(monkeypatch):
    blocker = "branch 'ticket/9'" + funnel.CONFLICTING_BRANCH_SUFFIX
    prior = verdict(
        verdict="rejected",
        ci="unknown",
        blocking=[blocker],
        reviewed_at=(NOW - timedelta(seconds=1)).isoformat(),
    )
    rows = items()
    rows[1].item_id = "ticket-project-item"
    rows[1].in_motion_since = NOW
    posted = _gate_rejection_wired(
        monkeypatch, pr(mergeable="CONFLICTING"), [prior]
    )

    assert funnel.cmd_merge(rows, NOW, REPO, 5, False) == 1
    assert posted == []
    assert rows[1].in_motion_since == NOW


def test_ci_unknown_conflict_rejection_is_repaired(monkeypatch):
    legacy = verdict(
        verdict="rejected", ci="unknown",
        blocking=["ci: CI not green (state unknown)"],
    )
    posted = _gate_rejection_wired(
        monkeypatch,
        pr(mergeable="CONFLICTING", statusCheckRollup=[]),
        [legacy],
    )
    assert funnel.cmd_merge(items(), NOW, REPO, 5, False) == 1

    rejection = funnel.parse_verdict(posted[0])
    assert rejection["blocking"] == [
        "branch 'ticket/9'" + funnel.CONFLICTING_BRANCH_SUFFIX
    ]
    assert funnel.parse_provenance(posted[0])["agent"] == funnel.MERGE_GATE_AGENT


def test_unknown_ci_review_on_conflict_writes_canonical_blocker(monkeypatch):
    rows = items()
    rows[1].item_id = "ticket-project-item"
    rows[1].in_motion_since = NOW
    posted = _gate_rejection_wired(
        monkeypatch,
        pr(mergeable="CONFLICTING", statusCheckRollup=[]),
        [],
    )
    assert funnel.cmd_review(
        REPO, 5, "rejected", "unknown",
        ["ci: CI not green (state unknown)"], None,
        items=rows,
    ) == 0

    assert rows[1].in_motion_since is None
    assert len(posted) == 1
    rejection = funnel.parse_verdict(posted[0])
    assert rejection["blocking"] == [
        "branch 'ticket/9'" + funnel.CONFLICTING_BRANCH_SUFFIX
    ]
    assert funnel.parse_provenance(posted[0])["agent"] == funnel.MERGE_GATE_AGENT


def test_no_verdict_refusal_does_not_write_a_gate_rejection(monkeypatch):
    posted = _gate_rejection_wired(
        monkeypatch, pr(mergeable="CONFLICTING"), []
    )
    assert funnel.cmd_merge(items(), NOW, REPO, 5, False) == 1
    assert posted == []


def test_moved_head_refusal_does_not_write_a_gate_rejection(monkeypatch):
    posted = _gate_rejection_wired(
        monkeypatch,
        pr(mergeable="CONFLICTING", headRefOid="9999newcommit"),
        [verdict()],
    )
    assert funnel.cmd_merge(items(), NOW, REPO, 5, False) == 1
    assert posted == []


def _next_with_pr(monkeypatch, fact, comment):
    """Run the engineer selector against one open ticket PR snapshot."""
    monkeypatch.setattr(funnel, "finished_by_comments", lambda rows: set())

    def fake_gh_json(*args):
        if "comments" in args:
            return {"comments": [{"body": comment}]}
        return [{
            "headRefName": "ticket/9",
            "headRefOid": fact["headRefOid"],
            "number": 5,
        }]

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    return funnel.cmd_next(
        items(), NOW, pr_facts={REPO + "#9": fact}
    )


def test_approved_conflicting_current_head_is_offered_to_engineer(
        monkeypatch, capsys):
    """Replay #185: approval plus a conflicting head needs engineering work."""
    fact = pr(
        mergeable="CONFLICTING",
        verdict={"verdict": "approved", "head_sha": SHA},
    )

    assert _next_with_pr(monkeypatch, fact, verdict()) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ref"] == REPO + "#9"


def test_an_ordinary_open_pr_is_not_unconditionally_reoffered(
        monkeypatch, capsys):
    """Only the narrow approved-conflict state bypasses awaiting-review."""
    fact = pr(
        mergeable="MERGEABLE",
        verdict={"verdict": "approved", "head_sha": SHA},
    )

    assert _next_with_pr(monkeypatch, fact, verdict()) == 1
    assert capsys.readouterr().out == ""


def test_a_conflicting_approval_for_an_old_head_stays_withheld(
        monkeypatch, capsys):
    """A moved head needs review, not an engineer re-hand."""
    fact = pr(
        mergeable="CONFLICTING",
        headRefOid="new-head",
        verdict={"verdict": "approved", "head_sha": "old-head"},
    )

    assert _next_with_pr(
        monkeypatch, fact, verdict(head_sha="old-head")
    ) == 1
    assert capsys.readouterr().out == ""


# -- closing the ticket the PR finished ---------------------------------------
#
# `gh pr merge` closes an issue only when the PR body carries a `Closes #N`
# link, written by hand. On 2026-09-08 thirteen merged ticket PRs omitted it,
# and each left its ticket open, startable, and re-served to the engineer on
# every run — 38 of 176 Codex runs that day did nothing but re-verify a PR that
# had already merged. These tests hold the close to the merge itself.

def _merge_wired(monkeypatch, issue_state="OPEN", close_rc=0, close_err="",
                 rows=None, drift=None, pr_fields=None):
    """Run cmd_merge past a clean gate, recording the subprocesses it runs."""
    calls = []
    graphql_calls = []

    rows = rows or items()

    def fake_gh_json(*args):
        if "issue" in args and "view" in args:
            return {"state": issue_state}
        if "comments" in args:
            return {"comments": [{"body": verdict()}]}
        return pr(**(pr_fields or {}))

    def fake_run(argv, **kwargs):
        calls.append(argv)
        rc = close_rc if argv[:2] == ["gh", "issue"] else 0
        err = close_err if argv[:2] == ["gh", "issue"] else ""
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": err})()

    def fake_graphql(query, **variables):
        graphql_calls.append((query, variables))
        if query == funnel.CLOSED_ITSELF_TICKETS:
            # The parent's tickets, as its sub-issues read would list them.
            parent = "{owner}/{name}#{number}".format(**variables)
            nodes = [
                {"number": row.number, "title": row.title, "state": row.state,
                 "url": row.url, "repository": {"nameWithOwner": row.repo}}
                for row in rows if row.parent == parent
            ]
            return {"repository": {"issue": {"subIssues": {
                "totalCount": len(nodes),
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        data = pr(**(pr_fields or {}))
        data.setdefault("number", 7)
        data.setdefault("url", "https://example.invalid/7")
        data["comments"] = {"nodes": [{"body": verdict()}]}
        data["commits"] = {
            "nodes": [{
                "commit": {
                    "statusCheckRollup": {
                        "contexts": {
                            "nodes": list(data.get("statusCheckRollup") or [])
                        }
                    }
                }
            }]
        }
        return {
            "rateLimit": {"cost": 1, "remaining": 99, "resetAt": "later"},
            "repo0": {
                "pullRequests": {
                    "nodes": [data],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            },
        }

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    monkeypatch.setattr(funnel.subprocess, "run", fake_run)
    monkeypatch.setattr(funnel, "gh_graphql", fake_graphql)
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "done-option")
    monkeypatch.setattr(
        funnel, "drift_since_approval",
        lambda item: list(drift or []),
    )
    rc = funnel.cmd_merge(rows, NOW, REPO, 7, True)
    return rc, calls, graphql_calls


def test_merge_closes_the_ticket_the_branch_names(monkeypatch):
    rc, calls, _ = _merge_wired(monkeypatch)
    assert rc == 0
    assert ["gh", "issue", "close", "9", "--repo", REPO,
            "--reason", "completed"] in calls


def _merge_argv(calls):
    return [c for c in calls if c[:3] == ["gh", "pr", "merge"]][0]


def test_merge_names_the_squash_after_the_pr_title(monkeypatch):
    """#951: a one-commit PR whose commit read "WIP #82: tests failing" landed
    on main under that subject, though the PR was green and correctly titled."""
    rc, calls, _ = _merge_wired(
        monkeypatch, pr_fields={"title": "Send FF alerts over iMessage (#82)"})
    assert rc == 0
    argv = _merge_argv(calls)
    assert argv[argv.index("--subject") + 1] == (
        "Send FF alerts over iMessage (#82) (#7)")


def test_merge_without_a_readable_title_keeps_github_default(monkeypatch):
    rc, calls, _ = _merge_wired(monkeypatch)
    assert rc == 0
    assert "--subject" not in _merge_argv(calls)


def test_merge_does_not_reclose_a_ticket_github_already_closed(monkeypatch):
    rc, calls, _ = _merge_wired(monkeypatch, issue_state="CLOSED")
    assert rc == 0
    assert ["gh", "issue", "close", "9", "--repo", REPO,
            "--reason", "completed"] not in calls


def test_a_failed_close_says_the_merge_landed_and_still_exits_non_zero(
        monkeypatch, capsys):
    # #236's plan: report loudly *and* exit non-zero. A ticket left open is the
    # failure this close exists to prevent, so a silent 0 would hide it. A retry
    # is harmless — the gate refuses a PR that is no longer open.
    rc, _, _ = _merge_wired(monkeypatch, close_rc=1, close_err="gh: nope")
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not be closed" in err and "owner/repo#9" in err
    assert "the merge succeeded" in err


def test_last_upkeep_ticket_auto_closes_parent_and_records_drift(monkeypatch):
    drift = [funnel.DRIFT_PLAN_EDIT, funnel.DRIFT_REGRESSION]
    rc, calls, graphql_calls = _merge_wired(monkeypatch, drift=drift)

    assert rc == 0
    assert (funnel.SET_FIELD, {
        "project": funnel.PROJECT_ID,
        "item": "project-id",
        "field": funnel.STATUS_FIELD_ID,
        "option": "done-option",
    }) in graphql_calls
    assert ["gh", "issue", "close", "1", "--repo", REPO,
            "--reason", "completed"] in calls

    comments = [call[-1] for call in calls
                if call[:3] == ["gh", "issue", "comment"]]
    assert len(comments) == 1
    assert comments[0].startswith(funnel.CLOSED_ITSELF_PREFIX)
    assert "owner/repo#9" in comments[0]
    payload = json.loads(
        comments[0].split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    )
    assert payload == {
        "drift": drift,
        "tickets": [{"ref": REPO + "#9", "title": "t"}],
    }


@pytest.mark.parametrize(
    ("klass", "origin"),
    [
        ("Investigate", None),
        ("Broken", "nate-relayed"),
        ("Maintenance", None),
        ("Improve", "agent"),
    ],
)
def test_each_auto_close_class_closes_on_its_last_merge(
        monkeypatch, klass, origin):
    rc, calls, _ = _merge_wired(
        monkeypatch, rows=items(klass=klass, origin=origin)
    )

    assert rc == 0
    assert ["gh", "issue", "close", "1", "--repo", REPO,
            "--reason", "completed"] in calls


@pytest.mark.parametrize(
    ("klass", "origin"),
    [
        (None, "agent"),
        ("New", "agent"),
        ("Replace", "agent"),
        ("Improve", "nate-relayed"),
        ("Improve", None),
    ],
)
def test_acceptance_gate_classes_and_nate_owned_improve_never_auto_close(
        monkeypatch, klass, origin):
    rc, calls, graphql_calls = _merge_wired(
        monkeypatch,
        rows=items(klass=klass, origin=origin),
        drift=[funnel.DRIFT_PLAN_EDIT],
    )

    assert rc == 0
    assert ["gh", "issue", "close", "1", "--repo", REPO,
            "--reason", "completed"] not in calls
    assert not [call for call in graphql_calls if call[1].get("item") == "project-id"]
    assert not [call for call in calls
                if call[:3] == ["gh", "issue", "comment"]]


def test_a_merge_leaving_another_ticket_open_does_not_close_parent(monkeypatch):
    rc, calls, graphql_calls = _merge_wired(
        monkeypatch,
        rows=items(children_total=2, children_done=0, other_ticket=True),
    )

    assert rc == 0
    assert ["gh", "issue", "close", "9", "--repo", REPO,
            "--reason", "completed"] in calls
    assert ["gh", "issue", "close", "1", "--repo", REPO,
            "--reason", "completed"] not in calls
    assert not [call for call in graphql_calls if call[1].get("item") == "project-id"]
    assert not [call for call in calls
                if call[:3] == ["gh", "issue", "comment"]]


def test_ticket_ref_from_branch_is_the_one_parser():
    assert funnel.ticket_ref_from_branch(REPO, "ticket/9") == REPO + "#9"
    assert funnel.ticket_ref_from_branch(REPO, "main") is None
    assert funnel.ticket_ref_from_branch(REPO, "ticket/not-a-number") is None
