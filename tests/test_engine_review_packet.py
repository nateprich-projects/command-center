"""One read-only review packet per PR (#798, Phase 1 of #794).

The review runner shows the model this packet and nothing else. Each field
gets a fixture test here so a later phase can build pre-checks on the
packet without re-deriving what every field means.
"""

from __future__ import annotations

import json
import pathlib
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import review  # noqa: E402

REPO = "owner/repo"
SHA = "abc123def456"
OTHER_SHA = "7890fedcba98"


@pytest.fixture(autouse=True)
def project_risk(monkeypatch):
    monkeypatch.setattr(
        funnel, "load_items",
        lambda: [type("Row", (), {"ref": REPO + "#9", "risk": "standard"})()],
    )


def pr_view(**kw):
    data = {
        "number": 7,
        "title": "do the thing",
        "headRefName": "ticket/9",
        "headRefOid": SHA,
        "baseRefName": "main",
        "state": "OPEN",
        "mergedAt": None,
        "closedAt": None,
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"name": "tests", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
        "files": [{"path": "funnel.py"}],
    }
    data.update(kw)
    return data


def ticket(**kw):
    data = {
        "ref": REPO + "#9",
        "number": 9,
        "title": "the ticket",
        "url": "https://github.com/{}/issues/9".format(REPO),
        "body": "Parent: #1.\n\nWhat: do the thing.\n\nRisk: standard",
        "risk": "standard",
    }
    data.update(kw)
    return data


def verdict(**kw):
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA,
            "blocking": []}
    body.update(kw)
    return body


def _compare_unavailable(repo, base_ref, head_sha):
    """A failing compare: collect() falls back to the PR reads (#1043)."""
    raise funnel.GitHubError("compare unavailable")


STOP_COUNTER = {"window_days": 7, "count": 0, "refs": [],
                "stop_auto_merging": False}


def empty_pr_comments():
    return {"status": "empty", "message": "No PR comments.", "comments": []}


def packet(**kw):
    args = {
        "repo": REPO,
        "pr_number": 7,
        "pr_view": pr_view(),
        "diff": "diff --git a/funnel.py b/funnel.py\n",
        "ticket": ticket(),
        "plan_md": "# design record",
        "plan_md_missing": False,
        "open_prs": [],
        "verdict": verdict(),
        "stop_counter": dict(STOP_COUNTER),
        "collected_at": "2026-09-13T00:00:00+00:00",
        "pr_comments": empty_pr_comments(),
    }
    args.update(kw)
    return review.build_packet(**args)


# -- CI state ---------------------------------------------------------------

def test_ci_green_when_every_check_succeeded():
    assert review.ci_state(pr_view()["statusCheckRollup"]) == "green"


def test_ci_red_when_any_check_failed():
    rollup = [
        {"name": "tests", "conclusion": "SUCCESS", "status": "COMPLETED"},
        {"name": "lint", "conclusion": "FAILURE", "status": "COMPLETED"},
    ]
    assert review.ci_state(rollup) == "red"


def test_ci_red_on_the_rest_state_shape():
    assert review.ci_state([{"context": "ci", "state": "FAILURE"}]) == "red"
    assert review.ci_state([{"context": "ci", "state": "SUCCESS"}]) == "green"


def test_ci_unknown_when_no_checks_reported():
    assert review.ci_state([]) == "unknown"


def test_ci_unknown_while_a_check_is_still_running():
    rollup = [
        {"name": "tests", "conclusion": "SUCCESS", "status": "COMPLETED"},
        {"name": "slow", "conclusion": None, "status": "IN_PROGRESS"},
    ]
    assert review.ci_state(rollup) == "unknown"


def test_ci_red_beats_pending():
    rollup = [
        {"name": "slow", "conclusion": None, "status": "IN_PROGRESS"},
        {"name": "lint", "conclusion": "FAILURE", "status": "COMPLETED"},
    ]
    assert review.ci_state(rollup) == "red"


def test_neutral_and_skipped_do_not_fail_ci():
    rollup = [
        {"name": "optional", "conclusion": "NEUTRAL", "status": "COMPLETED"},
        {"name": "skipped", "conclusion": "SKIPPED", "status": "COMPLETED"},
    ]
    assert review.ci_state(rollup) == "green"


# -- protected paths --------------------------------------------------------

def test_each_protected_rule_fires():
    files = [".claude/settings.json", "routines/muse.md",
             "skills/shape/SKILL.md", "AGENTS.md", "plan.md"]
    found = review.protected_touches(files, "diff")
    assert found["touched"] == sorted(files)
    assert found["rules"] == sorted(review.PROTECTED_PATHS)


def test_ordinary_files_touch_no_protected_rule():
    found = review.protected_touches(["funnel.py", "tests/test_x.py"], "diff")
    assert found["touched"] == [] and found["rules"] == []


def test_a_prefix_match_is_not_enough_for_a_file_rule():
    # "plan.md" the rule must not fire on a longer filename that merely
    # starts the same way.
    found = review.protected_touches(["plan.md.backup"], "diff")
    assert found["touched"] == [] and found["rules"] == []


def test_a_resolved_path_spelling_in_the_diff_is_flagged():
    diff = "+RUN = /Volumes/External SSD/repo\n"
    found = review.protected_touches(["funnel.py"], diff)
    assert found["resolved_path_spelling"] is True


def test_a_canonical_diff_has_no_respelling():
    diff = "+RUN = /Users/nateprich/.claude/command-center-run\n"
    found = review.protected_touches(["funnel.py"], diff)
    assert found["resolved_path_spelling"] is False


# -- overlap ----------------------------------------------------------------

def test_overlap_lists_only_prs_sharing_a_file():
    open_prs = [
        {"number": 7, "headRefName": "ticket/9",
         "files": [{"path": "funnel.py"}]},
        {"number": 8, "headRefName": "ticket/10",
         "files": [{"path": "funnel.py"}, {"path": "other.py"}]},
        {"number": 11, "headRefName": "ticket/12",
         "files": [{"path": "unrelated.py"}]},
    ]
    found = review.file_overlap(["funnel.py"], open_prs, 7)
    assert found == [{"pr": 8, "branch": "ticket/10",
                      "files": ["funnel.py"]}]


def test_overlap_is_empty_when_nothing_is_shared():
    open_prs = [{"number": 8, "headRefName": "ticket/10",
                 "files": [{"path": "other.py"}]}]
    assert review.file_overlap(["funnel.py"], open_prs, 7) == []


def test_overlap_reports_every_shared_file_sorted():
    open_prs = [{"number": 8, "headRefName": "ticket/10",
                 "files": [{"path": "b.py"}, {"path": "a.py"}]}]
    found = review.file_overlap(["a.py", "b.py", "c.py"], open_prs, 7)
    assert found == [{"pr": 8, "branch": "ticket/10",
                      "files": ["a.py", "b.py"]}]


# -- CI runs: the merged-overlap row's coverage evidence (#1019) ---------------

RUN_BRANCH = "ticket/9"
RUN_AT = "2026-09-13T13:30:00Z"
OLD_RUN_AT = "2026-09-13T11:00:00Z"


def gh_run(run_id=11, started_at=RUN_AT, conclusion="success",
           status="completed", event="pull_request", head=SHA):
    """One ``gh run list`` row: the wire shape the fetcher returns."""
    return {"databaseId": run_id, "event": event, "headSha": head,
            "headBranch": RUN_BRANCH, "conclusion": conclusion,
            "status": status, "createdAt": started_at,
            "startedAt": started_at, "updatedAt": started_at}


def test_fetch_ci_runs_lists_pull_request_runs_on_the_branch(monkeypatch):
    seen = []

    def fake_gh_json(*args):
        seen.append(args)
        return [gh_run()]

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    assert review.fetch_ci_runs(REPO, RUN_BRANCH) == [gh_run()]
    command = seen[0]
    assert command[:5] == ("gh", "run", "list", "--repo", REPO)
    assert command[command.index("--branch") + 1] == RUN_BRANCH
    assert command[command.index("--event") + 1] == "pull_request"
    fields = command[command.index("--json") + 1].split(",")
    for field in ("databaseId", "event", "headSha", "conclusion", "status",
                  "startedAt"):
        assert field in fields


def test_fetch_ci_runs_reads_no_runs_without_actions(monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)
    assert review.fetch_ci_runs(REPO, RUN_BRANCH) == []


def test_fetch_ci_runs_skips_an_empty_branch_without_calling(monkeypatch):
    def fail(*args):
        raise AssertionError("no branch, no runs call")

    monkeypatch.setattr(funnel, "_gh_json", fail)
    assert review.fetch_ci_runs(REPO, "") == []


def test_fetch_ci_runs_drops_rubbish_rows(monkeypatch):
    monkeypatch.setattr(funnel, "_gh_json",
                        lambda *args: [None, "nonsense", gh_run()])
    assert review.fetch_ci_runs(REPO, RUN_BRANCH) == [gh_run()]


def test_packet_carries_the_ci_runs_newest_first():
    runs = [gh_run(11, OLD_RUN_AT),
            gh_run(12, RUN_AT, status="in_progress", conclusion=None)]
    found = packet(ci_runs=runs)
    assert found["ci"]["runs"] == [
        {"id": 12, "conclusion": None, "status": "in_progress",
         "started_at": RUN_AT},
        {"id": 11, "conclusion": "success", "status": "completed",
         "started_at": OLD_RUN_AT},
    ]
    assert found["ci"]["green_run_at"] == OLD_RUN_AT
    assert found["ci"]["latest_run_id"] == 11


def test_packet_without_ci_runs_carries_no_coverage():
    found = packet()
    assert found["ci"]["runs"] == []
    assert found["ci"]["green_run_at"] is None
    assert found["ci"]["latest_run_id"] is None
    assert found["ci_rerun"] is None


def test_collect_fetches_runs_for_the_pr_branch(monkeypatch):
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: pr_view())
    monkeypatch.setattr(review, "fetch_scope", _compare_unavailable)
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(
        review, "fetch_ticket", lambda repo, number: ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(
        review, "fetch_pr_comments", lambda repo, pr: empty_pr_comments())
    seen = {}

    def fake_runs(repo, branch):
        seen["repo"] = repo
        seen["branch"] = branch
        return [gh_run()]

    monkeypatch.setattr(review, "fetch_ci_runs", fake_runs)
    found = review.collect(REPO, 7, items_loader=lambda: [])
    assert seen == {"repo": REPO, "branch": "ticket/9"}
    assert found["ci"]["green_run_at"] == RUN_AT


def test_packet_exposes_the_watch_run_comment_for_a_run_outcome_accept():
    run_comment = {
        "kind": "issue",
        "author": "nateprich",
        "created_at": "2026-09-24T17:59:00Z",
        "body": "Fresh-clone run: `python3 -m pytest`; exit 0; 597 tests OK.",
    }
    requirement = (
        "Run-outcome requirement: the reviewer judges it against the posted "
        "run evidence in the PR comments."
    )
    found = packet(
        ticket=ticket(body=requirement),
        pr_comments={"status": "available", "message": None,
                     "comments": [run_comment]},
    )
    assert found["pr_comments"]["comments"] == [run_comment]
    assert "597 tests OK" in found["pr_comments"]["comments"][0]["body"]
    assert requirement in found["ticket"]["body"]


def test_packet_has_an_explicit_empty_pr_comments_section():
    assert packet()["pr_comments"] == empty_pr_comments()


def test_fetch_pr_comments_uses_shared_graphql_and_sorts_both_comment_kinds(
        monkeypatch):
    seen = {}

    def fake_graphql(query, **variables):
        seen["query"] = query
        seen["variables"] = variables
        return {"repository": {"pullRequest": {
            "issueComments": {
                "nodes": [{"author": {"login": "author-a"},
                           "body": "watch run: 597 tests OK",
                           "createdAt": "2026-09-24T17:59:00Z"}],
                "pageInfo": {"hasNextPage": False, "endCursor": "issue-end"},
            },
            "reviewThreads": {
                "nodes": [{"id": "thread-1", "comments": {
                    "nodes": [{"author": {"login": "reviewer"},
                               "body": "run outcome is judgeable",
                               "createdAt": "2026-09-24T17:58:00Z"}],
                    "pageInfo": {"hasNextPage": False,
                                 "endCursor": "review-end"},
                }}],
                "pageInfo": {"hasNextPage": False, "endCursor": "thread-end"},
            },
        }}}

    monkeypatch.setattr(funnel, "gh_graphql", fake_graphql)
    found = review.fetch_pr_comments(REPO, 7)
    assert seen["variables"] == {"owner": "owner", "name": "repo", "number": 7}
    assert "issueComments: comments" in seen["query"]
    assert "reviewThreads" in seen["query"]
    assert "rateLimit { cost remaining resetAt }" in seen["query"]
    assert found == {
        "status": "available",
        "message": None,
        "comments": [
            {"kind": "review", "author": "reviewer",
             "created_at": "2026-09-24T17:58:00Z",
             "body": "run outcome is judgeable"},
            {"kind": "issue", "author": "author-a",
             "created_at": "2026-09-24T17:59:00Z",
             "body": "watch run: 597 tests OK"},
        ],
    }


def test_fetch_pr_comments_paginates_each_connection(monkeypatch):
    requests = []

    def connection(nodes, has_next, end_cursor):
        return {"nodes": nodes,
                "pageInfo": {"hasNextPage": has_next,
                             "endCursor": end_cursor}}

    def fake_graphql(query, **variables):
        requests.append((query, variables))
        if query == review.PR_REVIEW_THREAD_COMMENTS_QUERY:
            assert variables == {"threadId": "thread-1",
                                 "cursor": "review-cursor-1"}
            return {"node": {"comments": connection([
                {"author": {"login": "reviewer"}, "body": "review page two",
                 "createdAt": "2026-09-24T17:58:00Z"}], False,
                "review-cursor-2")}}

        issue_cursor = variables.get("issueCursor")
        if issue_cursor is None:
            return {"repository": {"pullRequest": {
                "issueComments": connection([
                    {"author": {"login": "author"}, "body": "issue page one",
                     "createdAt": "2026-09-24T17:56:00Z"}], True,
                    "issue-cursor-1"),
                "reviewThreads": connection([{
                    "id": "thread-1",
                    "comments": connection([
                        {"author": {"login": "reviewer"},
                         "body": "review page one",
                         "createdAt": "2026-09-24T17:57:00Z"}], True,
                        "review-cursor-1"),
                }], False, "thread-end"),
            }}}

        assert issue_cursor == "issue-cursor-1"
        assert variables.get("threadCursor") == "thread-end"
        return {"repository": {"pullRequest": {
            "issueComments": connection([
                {"author": {"login": "author"}, "body": "issue page two",
                 "createdAt": "2026-09-24T17:59:00Z"}], False,
                "issue-cursor-2"),
            "reviewThreads": connection([], False, None),
        }}}

    monkeypatch.setattr(funnel, "gh_graphql", fake_graphql)
    found = review.fetch_pr_comments(REPO, 7)
    assert [entry["body"] for entry in found["comments"]] == [
        "issue page one", "review page one", "review page two", "issue page two"]
    assert len(requests) == 3


def test_pr_comments_are_capped_with_an_explicit_truncation_marker(monkeypatch):
    body = "x" * (review.PR_COMMENT_BODY_LIMIT + 10)
    monkeypatch.setattr(funnel, "gh_graphql", lambda query, **variables: {
        "repository": {"pullRequest": {
            "issueComments": {"nodes": [{
                "author": {"login": "author"}, "body": body,
                "createdAt": "2026-09-24T17:59:00Z"}],
                "pageInfo": {"hasNextPage": False, "endCursor": "issue-end"}},
            "reviewThreads": {"nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None}},
        }}})
    found = review.fetch_pr_comments(REPO, 7)
    comment_body = found["comments"][0]["body"]
    assert comment_body.startswith("x" * review.PR_COMMENT_BODY_LIMIT)
    assert comment_body.endswith("…[truncated 10 chars]")


def fetch_one_pr_comment(monkeypatch, body):
    """Shape one issue comment through the packet's GraphQL read path."""
    monkeypatch.setattr(funnel, "gh_graphql", lambda query, **variables: {
        "repository": {"pullRequest": {
            "issueComments": {
                "nodes": [{"author": {"login": "engineer"},
                           "body": body,
                           "createdAt": "2026-09-24T17:59:00Z"}],
                "pageInfo": {"hasNextPage": False,
                             "endCursor": "issue-end"},
            },
            "reviewThreads": {
                "nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }}})
    return review.fetch_pr_comments(REPO, 7)["comments"][0]


def test_run_evidence_comment_parses_canonical_fields_and_keeps_body(
        monkeypatch):
    fields = {
        "command": "python3 -m pytest tests/test_engine_review_packet.py",
        "exit_status": 2,
        "output_summary": "2 failures from a fixture run",
        "environment_note": "clean checkout on macOS",
    }
    body = "**Run evidence:**\n\n```json\n{}\n```".format(
        json.dumps(fields, indent=2))

    found = fetch_one_pr_comment(monkeypatch, body)

    assert found["body"] == body
    assert found["run_evidence"] == {
        "format": "canonical",
        "fields": fields,
    }


def test_malformed_run_evidence_stays_prose_and_keeps_body(monkeypatch):
    body = (
        "**Run evidence:**\n\n```json\n"
        '{"command": "python3 -m pytest", "exit_status": }\n'
        "```"
    )

    found = fetch_one_pr_comment(monkeypatch, body)

    assert found["body"] == body
    assert found["run_evidence"] == {"format": "prose"}


def test_unreadable_pr_comment_list_is_not_rendered_as_empty(monkeypatch):
    monkeypatch.setattr(funnel, "gh_graphql", lambda query, **variables: {
        "repository": {"pullRequest": {
            "issueComments": {},
            "reviewThreads": {"nodes": [],
                "pageInfo": {"hasNextPage": False, "endCursor": None}},
        }}})
    found = review.fetch_pr_comments(REPO, 7)
    assert found == {
        "status": "could_not_read",
        "message": "Could not read PR comments: issue comment list was unreadable",
        "comments": [],
    }


def test_graphql_failure_is_an_explicit_could_not_read_section(monkeypatch):
    def fail(query, **variables):
        raise funnel.GitHubError("fixture unavailable")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    found = review.fetch_pr_comments(REPO, 7)
    assert found["status"] == "could_not_read"
    assert found["message"] == "Could not read PR comments: fixture unavailable"


# -- the assembled packet ---------------------------------------------------

def test_packet_carries_every_field():
    found = packet()
    assert found["repo"] == REPO
    assert found["pr"] == 7
    assert found["pr_title"] == "do the thing"
    assert found["branch"] == "ticket/9"
    assert found["state"] == "OPEN"
    assert found["merged_at"] is None
    assert found["closed_at"] is None
    assert found["head_sha"] == SHA
    assert found["ticket"]["body"].startswith("Parent: #1.")
    assert found["ticket"]["ref"] == REPO + "#9"
    assert found["plan_md"] == "# design record"
    assert found["plan_md_missing"] is False
    assert found["diff"].startswith("diff --git")
    assert found["changed_files"] == ["funnel.py"]
    assert found["ci"]["state"] == "green"
    assert found["ci"]["checks"] == [
        {"name": "tests", "conclusion": "SUCCESS", "state": None,
         "status": "COMPLETED"}]
    assert found["verdict"]["verdict"] == "approved"
    assert found["verdict_head_sha"] == SHA
    assert found["overlap"] == []
    assert found["ticket_prior_prs"] == []
    assert found["protected"] == {
        "touched": [], "rules": [], "resolved_path_spelling": False}
    assert found["stop_auto_merging"] == STOP_COUNTER
    assert found["collected_at"] == "2026-09-13T00:00:00+00:00"
    json.dumps(found)  # the packet is JSON by contract


def test_packet_ci_section_renders_per_check_conclusions_at_its_head():
    found = review.build_packet(
        repo=REPO,
        pr_number=7,
        pr_view=pr_view(statusCheckRollup=[
            {"name": "offline", "conclusion": "SUCCESS",
             "status": "COMPLETED"},
        ]),
        diff="diff --git a/funnel.py b/funnel.py\n",
        ticket=ticket(),
        plan_md="# design record",
        plan_md_missing=False,
        open_prs=[],
        verdict=verdict(),
        stop_counter=dict(STOP_COUNTER),
        collected_at="2026-09-13T00:00:00+00:00",
        pr_comments=empty_pr_comments(),
    )

    assert found["head_sha"] == SHA
    assert found["ci"]["state"] == "green"
    assert found["ci"]["checks"] == [
        {"name": "offline", "conclusion": "SUCCESS", "state": None,
         "status": "COMPLETED"},
    ]


# -- prior instalments of the same ticket (#908) ----------------------------

def merged_row(number, branch="ticket/9", **kw):
    row = {"number": number,
           "title": "slice {}".format(number),
           "mergedAt": "2026-09-1{}T00:00:00Z".format(number),
           "headRefName": branch,
           "files": [{"path": "dashboard/public/app.js"}]}
    row.update(kw)
    return row


def test_prior_prs_are_the_merges_on_this_same_branch():
    prior = review.ticket_prior_prs(
        "ticket/9", [merged_row(4), merged_row(5)], 7)
    assert [entry["pr"] for entry in prior] == [4, 5]
    assert prior[0]["title"] == "slice 4"
    assert prior[0]["files"] == ["dashboard/public/app.js"]


def test_prior_prs_ignore_other_branches():
    prior = review.ticket_prior_prs(
        "ticket/9", [merged_row(4, branch="ticket/8")], 7)
    assert prior == []


def test_prior_prs_exclude_the_candidate_itself():
    """Reviewing an already-merged PR must not list it as its own predecessor."""
    prior = review.ticket_prior_prs("ticket/9", [merged_row(7)], 7)
    assert prior == []


def test_prior_prs_are_oldest_first():
    rows = [merged_row(5, mergedAt="2026-09-15T00:00:00Z"),
            merged_row(4, mergedAt="2026-09-14T00:00:00Z")]
    assert [e["pr"] for e in review.ticket_prior_prs("ticket/9", rows, 7)] == [4, 5]


def test_prior_prs_are_empty_without_a_branch():
    assert review.ticket_prior_prs(None, [merged_row(4)], 7) == []
    assert review.ticket_prior_prs("", [merged_row(4)], 7) == []


def test_prior_prs_survive_rubbish_rows():
    rows = [None, "nonsense", {}, merged_row(4)]
    assert [e["pr"] for e in review.ticket_prior_prs("ticket/9", rows, 7)] == [4]


def test_an_older_prior_slice_is_not_a_merged_overlap():
    """The regression behind #908.

    #907 was rejected for not containing #904's work. ``merged_overlap``
    could never have surfaced #904: it reports only merges strictly newer
    than the candidate's head, and a prior instalment is older. So the
    packet said nothing about it, and the reviewer judged one slice against
    the whole ticket.
    """
    older = merged_row(4, mergedAt="2026-09-01T00:00:00Z")
    view = pr_view(commits=[{"committedDate": "2026-09-10T00:00:00Z"}])
    found = packet(pr_view=view, merged_prs=[older])
    assert found["merged_overlap"] == []
    assert [entry["pr"] for entry in found["ticket_prior_prs"]] == [4]


def test_packet_carries_prior_slices_from_the_pr_view_branch():
    found = packet(merged_prs=[merged_row(4), merged_row(5, branch="other")])
    assert [entry["pr"] for entry in found["ticket_prior_prs"]] == [4]


# -- the review question names them (#908) ----------------------------------

def test_the_review_question_tells_the_model_to_judge_the_increment():
    text = (ROOT / "routines" / "muse-review.md").read_text()
    assert "ticket_prior_prs" in text
    assert "adds" in text


def test_packet_without_a_verdict_says_so_plainly():
    found = packet(verdict=None)
    assert found["verdict"] is None
    assert found["verdict_head_sha"] is None


def test_packet_exposes_a_stale_verdict_head():
    found = packet(verdict=verdict(head_sha=OTHER_SHA))
    assert found["verdict_head_sha"] == OTHER_SHA
    assert found["head_sha"] == SHA


def test_packet_without_a_ticket_branch_has_no_ticket_body():
    view = pr_view(headRefName="docs/meta-terms-read")
    found = packet(pr_view=view, ticket=None)
    assert found["ticket"] == {
        "ref": None, "number": None, "title": None, "url": None,
        "body": None, "risk": None, "parent": None, "comments": []}


def test_packet_marks_a_missing_plan():
    found = packet(plan_md="", plan_md_missing=True)
    assert found["plan_md"] == "" and found["plan_md_missing"] is True


def test_packet_computes_overlap_and_protected_from_the_pr_view():
    view = pr_view(files=[{"path": "AGENTS.md"}, {"path": "funnel.py"}])
    open_prs = [{"number": 8, "headRefName": "ticket/10",
                 "files": [{"path": "funnel.py"}]}]
    found = packet(pr_view=view, open_prs=open_prs)
    assert found["changed_files"] == ["AGENTS.md", "funnel.py"]
    assert found["overlap"] == [{"pr": 8, "branch": "ticket/10",
                                 "files": ["funnel.py"]}]
    assert found["protected"]["rules"] == ["AGENTS.md"]
    assert found["protected"]["touched"] == ["AGENTS.md"]


def test_packet_counts_the_stop_counter_through():
    counter = {"window_days": 7, "count": 3, "refs": ["a#1", "a#2", "a#3"],
               "stop_auto_merging": True}
    assert packet(stop_counter=counter)["stop_auto_merging"] == counter


# -- structural guarantees --------------------------------------------------

def test_engine_imports_from_funnel_and_never_the_reverse():
    engine_source = (ROOT / "engine" / "review.py").read_text()
    assert "import funnel" in engine_source
    funnel_source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in funnel_source
    assert "from engine" not in funnel_source


def test_the_packet_makes_no_writes():
    # Every GitHub call in the packet path is a view, list, diff, or
    # content read. If a mutating verb appears here, the read-only
    # contract this ticket promises is broken.
    mutating = ("pr merge", "pr comment", "pr close", "pr review",
                "pr edit", "pr create", "issue close", "issue create",
                "issue comment", "issue edit", "item-add", "item-edit",
                "item-delete", "--yes", "delete-branch")
    for name in ("engine/review.py", "review-packet"):
        source = (ROOT / name).read_text()
        offenders = [verb for verb in mutating if verb in source]
        assert offenders == [], "{} makes writes: {}".format(name, offenders)


def test_the_entry_point_is_executable():
    entry = ROOT / "review-packet"
    assert entry.exists()
    assert entry.stat().st_mode & stat.S_IXUSR


# -- the CLI ----------------------------------------------------------------

def test_cli_prints_valid_json_with_every_field(monkeypatch, capsys):
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: pr_view())
    monkeypatch.setattr(review, "fetch_scope", _compare_unavailable)
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(
        review, "fetch_ticket", lambda repo, number: ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(
        review, "fetch_verdict", lambda repo, pr: verdict())
    monkeypatch.setattr(
        review, "fetch_stop_counter",
        lambda items_loader=None, now=None: dict(STOP_COUNTER))
    assert review.main(["7", "--repo", REPO]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["repo"] == REPO and found["pr"] == 7
    assert found["ticket"]["body"].startswith("Parent: #1.")
    assert found["plan_md"] == "# design record"
    assert found["diff"] == "diff text"
    assert found["ci"]["state"] == "green"
    assert found["verdict"]["verdict"] == "approved"
    assert found["verdict_head_sha"] == SHA
    assert found["overlap"] == []
    assert found["protected"]["rules"] == []
    assert found["stop_auto_merging"] == STOP_COUNTER


def test_cli_leaves_a_non_ticket_branch_without_a_ticket(monkeypatch, capsys):
    monkeypatch.setattr(
        review, "fetch_pr",
        lambda repo, pr: pr_view(headRefName="docs/meta-terms-read"))
    monkeypatch.setattr(review, "fetch_scope", _compare_unavailable)
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "")
    monkeypatch.setattr(review, "fetch_plan_md", lambda repo: ("", True))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(
        review, "fetch_stop_counter",
        lambda items_loader=None, now=None: dict(STOP_COUNTER))
    seen = []

    def fail_if_called(repo, number):
        seen.append(number)
        raise AssertionError("no ticket branch, no ticket fetch")

    monkeypatch.setattr(review, "fetch_ticket", fail_if_called)
    assert review.main(["335", "--repo", REPO]) == 0
    found = json.loads(capsys.readouterr().out)
    assert seen == []
    assert found["ticket"]["body"] is None
    assert found["plan_md_missing"] is True


def test_a_pending_status_shape_is_unknown_not_red():
    """#900: "PENDING" is not in the success set, so the red rule used to
    swallow it and a legacy Status context that had not reported came back red
    while the CheckRun shape came back unknown."""
    assert review.ci_state([{"context": "ci", "state": "PENDING"}]) == "unknown"
    assert review.ci_state([{"context": "ci", "state": "QUEUED"}]) == "unknown"
    assert review.ci_state(
        [{"context": "ci", "state": "PENDING"},
         {"context": "lint", "state": "FAILURE"}]) == "red"


# -- ticket comments (#1005) -------------------------------------------------

def comment(body, voice=None, author="nateprich",
            created_at="2026-09-13T00:00:00Z"):
    if voice is not None:
        body = funnel.append_provenance(
            body, voice, run="fixture-run", agent="muse")
    return {"author": {"login": author}, "body": body,
            "createdAt": created_at}


def test_each_provenance_voice_is_read_from_its_comment():
    rows = [comment("direct words", "nate-direct",
                    created_at="2026-09-13T00:00:00Z"),
            comment("relayed words", "nate-relayed",
                    created_at="2026-09-14T00:00:00Z"),
            comment("agent words", "agent",
                    created_at="2026-09-15T00:00:00Z")]
    assert [entry["voice"] for entry in review.ticket_comments(rows)] == [
        "nate-direct", "nate-relayed", "agent"]


def test_a_comment_without_a_marker_has_unknown_voice():
    (found,) = review.ticket_comments([comment("just words")])
    assert found == {"author": "nateprich",
                     "created_at": "2026-09-13T00:00:00Z",
                     "voice": "unknown", "body": "just words"}


def test_the_marker_block_is_stripped_from_the_body():
    (found,) = review.ticket_comments(
        [comment("Retire the drift checks.", "nate-direct")])
    assert found["voice"] == "nate-direct"
    assert found["body"] == "Retire the drift checks."
    assert "command-center" not in found["body"]


def test_comments_arrive_in_time_order_oldest_first():
    rows = [comment("second", created_at="2026-09-14T00:00:00Z"),
            comment("first", created_at="2026-09-13T00:00:00Z")]
    assert [entry["body"] for entry in review.ticket_comments(rows)] == [
        "first", "second"]


def test_only_the_newest_thirty_comments_are_kept():
    rows = [comment("note {}".format(day),
                    created_at="2026-09-{:02d}T00:00:00Z".format(day))
            for day in range(1, 36)]
    found = review.ticket_comments(rows)
    assert len(found) == 30
    assert found[0]["body"] == "note 6"
    assert found[-1]["body"] == "note 35"


def test_a_long_body_is_capped_with_its_cut_marked():
    (found,) = review.ticket_comments([comment("x" * 4100)])
    assert found["body"] == "x" * 4000 + "\n…[truncated 100 chars]"


def test_a_body_at_the_cap_is_left_alone():
    (found,) = review.ticket_comments([comment("y" * 4000)])
    assert found["body"] == "y" * 4000


def test_rubbish_rows_and_missing_fields_do_not_break_shaping():
    rows = [None, "nonsense", {},
            {"author": "bare-login", "body": "plain"},
            {"author": {"login": "who"},
             "created_at": "2026-09-13T00:00:00Z"}]
    found = review.ticket_comments(rows)
    assert [(entry["author"], entry["created_at"], entry["voice"],
             entry["body"]) for entry in found] == [
        (None, None, "unknown", ""),
        ("bare-login", None, "unknown", "plain"),
        ("who", "2026-09-13T00:00:00Z", "unknown", ""),
    ]


def test_packet_carries_the_ticket_comments_with_voices():
    rows = [comment("Retire the drift checks.", "nate-direct",
                    created_at="2026-09-14T00:00:00Z"),
            comment("noted", created_at="2026-09-15T00:00:00Z")]
    found = packet(ticket=ticket(comments=rows))
    assert found["ticket"]["comments"] == [
        {"author": "nateprich", "created_at": "2026-09-14T00:00:00Z",
         "voice": "nate-direct", "body": "Retire the drift checks."},
        {"author": "nateprich", "created_at": "2026-09-15T00:00:00Z",
         "voice": "unknown", "body": "noted"},
    ]
    json.dumps(found)  # the packet is JSON by contract


def test_packet_carries_parent_comments_with_the_same_shape_and_caps():
    rows = [comment("parent decision", "nate-direct",
                    created_at="2026-09-14T00:00:00Z"),
            comment("z" * 4100, created_at="2026-09-15T00:00:00Z")]
    parent = {"number": 1, "title": "the plan", "comments": rows}
    found = packet(ticket=ticket(parent=parent))
    assert found["ticket"]["parent"]["comments"] == [
        {"author": "nateprich", "created_at": "2026-09-14T00:00:00Z",
         "voice": "nate-direct", "body": "parent decision"},
        {"author": "nateprich", "created_at": "2026-09-15T00:00:00Z",
         "voice": "unknown",
         "body": "z" * 4000 + "\n…[truncated 100 chars]"},
    ]
    json.dumps(found)


def test_packet_renders_the_four_1315_premises_as_testable_entries():
    entries = [
        {"claim": "CODEX_THREAD_ID reaches the automation process",
         "evidence": "#1315 CODEX_THREAD_ID probe", "label": "inferred"},
        {"claim": "The Codex app injects the memory-file read into each run",
         "evidence": "#1315 memory-read injector", "label": "inferred"},
        {"claim": "nothing-to-do means the queue had no available work",
         "evidence": "#1315 nothing-to-do outcomes", "label": "inferred"},
        {"claim": "writable_roots bound the run to the listed write scope",
         "evidence": "#1315 writable_roots probe", "label": "inferred"},
    ]
    body = "# Parent plan\n\n## Premises\n\n{}\n\nProposed class: Improve\n".format(
        "\n".join(
            "- {} (label: {}; evidence: {})".format(
                row["claim"], row["label"], row["evidence"])
            for row in entries))
    parent = {"number": 1, "ref": "owner/repo#1", "body": body,
              "comments": []}

    found = packet(ticket=ticket(parent=parent))

    assert found["plan_premises"] == [{
        "parent_ref": "owner/repo#1",
        "ticket_refs": ["owner/repo#9"],
        "available": True,
        "premises": entries,
    }]
    assert "body" not in found["ticket"]["parent"]
    json.dumps(found)


@pytest.mark.parametrize("body", [
    "# Legacy plan\n\nNo premises section was recorded.\n",
    "# New plan\n\n## Premises\n\nNone recorded.\n\n"
    "Proposed class: Improve\n",
])
def test_packet_keeps_a_plan_without_premises_valid(body):
    parent = {"number": 1, "ref": "owner/repo#1", "body": body,
              "comments": []}
    found = packet(ticket=ticket(parent=parent))

    assert found["plan_premises"] == [{
        "parent_ref": "owner/repo#1",
        "ticket_refs": ["owner/repo#9"],
        "available": True,
        "premises": [],
    }]
    json.dumps(found)


def test_packet_marks_an_unreadable_parent_plan_instead_of_empty_premises():
    parent = {"number": 1, "ref": "owner/repo#1", "comments": []}

    found = packet(ticket=ticket(parent=parent))

    assert found["plan_premises"] == [{
        "parent_ref": "owner/repo#1",
        "ticket_refs": ["owner/repo#9"],
        "available": False,
        "premises": [],
        "error": "parent plan body is unavailable",
    }]


def test_review_checklist_probes_inferred_premises_against_live_evidence():
    text = (ROOT / "routines" / "muse-review.md").read_text()
    assert "plan_premises" in text
    assert "labelled `inferred`" in text
    assert "live" in text and "evidence" in text
    assert "Do not\nre-derive" in text


def test_a_ticket_without_a_comments_list_gets_an_empty_one():
    assert packet()["ticket"]["comments"] == []


def test_a_parentless_ticket_still_builds_a_valid_packet():
    found = packet(ticket=ticket(parent=None))
    assert found["ticket"]["parent"] is None
    json.dumps(found)


def test_a_ticketless_branch_carries_no_comments():
    view = pr_view(headRefName="docs/meta-terms-read")
    assert packet(pr_view=view, ticket=None)["ticket"]["comments"] == []


def test_fetch_ticket_reads_comments_with_the_ticket(monkeypatch):
    seen = {}

    def fake_gh_json(*args):
        seen["args"] = args
        return {"number": 9, "title": "t", "url": "u", "body": "b",
                "parent": None, "comments": []}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    assert review.fetch_ticket(REPO, 9)["comments"] == []
    assert "comments" in seen["args"][-1].split(",")


def test_fetch_ticket_uses_parent_comments_already_in_the_parent_row(monkeypatch):
    rows = [comment("already here")]
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        return {"number": 9, "title": "t", "url": "u", "body": "b",
                "parent": {"number": 1, "body": "", "comments": rows},
                "comments": []}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(REPO, 9)
    assert len(calls) == 1
    assert found["parent"]["comments"] == rows


def test_fetch_ticket_reads_parent_comments_with_one_parent_view(monkeypatch):
    rows = [comment("on the plan")]
    parent_body = ("# Plan\n\n## Premises\n\n"
                   "- Claim holds (label: inferred; evidence: probe output)\n\n"
                   "Proposed class: Improve\n")
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 9, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1}, "comments": []}
        if len(calls) == 2:
            return {"number": 1,
                    "repository": {"full_name": REPO}}
        return {"body": parent_body, "comments": rows}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(REPO, 9)
    assert len(calls) == 3
    assert calls[1] == (
        "gh", "api", "repos/{}/issues/9/parent".format(REPO))
    assert calls[2] == (
        "gh", "issue", "view", "1", "--repo", REPO,
        "--json", "body,comments")
    assert found["parent"]["comments"] == rows
    assert found["parent"]["ref"] == "{}#1".format(REPO)
    assert found["parent"]["body"] == parent_body
    assert packet(ticket=found)["plan_premises"][0]["premises"] == [{
        "claim": "Claim holds", "evidence": "probe output",
        "label": "inferred"}]


def test_fetch_ticket_resolves_a_cross_repo_parent_to_its_own_repo(monkeypatch):
    """#1066: a member-repo ticket's parent lives in command-center."""
    member = "nateprich-projects/The-League"
    home = "nateprich-projects/command-center"
    rows = [comment("ship the runners.", "nate-direct")]
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 221, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1054}, "comments": []}
        if len(calls) == 2:
            return {"number": 1054,
                    "repository": {"full_name": home}}
        return {"body": "", "comments": rows}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(member, 221)
    assert calls[1] == (
        "gh", "api", "repos/{}/issues/221/parent".format(member))
    assert calls[2] == (
        "gh", "issue", "view", "1054", "--repo", home,
        "--json", "body,comments")
    assert found["parent"]["ref"] == "{}#1054".format(home)
    built = packet(repo=member, ticket=found)
    assert built["ticket"]["parent"]["ref"] == "{}#1054".format(home)
    assert built["ticket"]["parent"]["comments"] == [
        {"author": "nateprich", "created_at": "2026-09-13T00:00:00Z",
         "voice": "nate-direct", "body": "ship the runners."}]
    json.dumps(built)


def test_fetch_ticket_skips_the_relationship_read_when_the_row_names_the_repo(
        monkeypatch):
    calls = []
    rows = [comment("on the plan")]

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 9, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1,
                               "repository": {"full_name": REPO},
                               "body": ""},
                    "comments": []}
        return {"body": "", "comments": rows}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(REPO, 9)
    assert len(calls) == 2
    assert found["parent"]["comments"] == rows
    assert found["parent"]["ref"] == "{}#1".format(REPO)


def test_a_404_parent_view_degrades_to_a_packet_without_parent_comments(
        monkeypatch):
    """#1066: an unreadable parent must not block a review."""
    member = "nateprich-projects/The-League"
    home = "nateprich-projects/command-center"
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 221, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1054}, "comments": []}
        if len(calls) == 2:
            return {"number": 1054,
                    "repository": {"full_name": home}}
        return None  # `gh` 404s surface as None from `_gh_json`

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(member, 221)  # must not raise
    assert found["parent"]["comments"] == []
    assert found["parent"]["comments_unavailable"] is True
    assert found["parent"]["ref"] == "{}#1054".format(home)
    built = packet(repo=member, ticket=found)
    assert built["ticket"]["parent"]["comments"] == []
    assert built["ticket"]["parent"]["comments_unavailable"] is True
    json.dumps(built)


def test_a_404_parent_relationship_degrades_without_naming_a_repo(
        monkeypatch):
    """#1066: an unresolvable parent degrades rather than guessing a repo."""
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 9, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1}, "comments": []}
        return None

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(REPO, 9)  # must not raise
    assert len(calls) == 2
    assert found["parent"]["comments"] == []
    assert found["parent"]["comments_unavailable"] is True
    assert "ref" not in found["parent"]
    built = packet(ticket=found)
    assert built["ticket"]["parent"]["comments_unavailable"] is True
    json.dumps(built)


def test_fetch_ticket_reads_a_cross_repo_parent_from_its_own_repo(monkeypatch):
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 220, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1054, "url": (
                        "https://github.com/nateprich-projects/"
                        "command-center/issues/1054")},
                    "comments": []}
        return {"body": "", "comments": []}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    review.fetch_ticket("nateprich-projects/The-League", 220)
    assert len(calls) == 2  # the URL resolves the repo: no relationship read
    assert calls[1] == (
        "gh", "issue", "view", "1054", "--repo",
        "nateprich-projects/command-center", "--json", "body,comments")


def test_a_failed_cross_repo_parent_read_degrades_and_names_the_parent_repo(
        monkeypatch):
    """#1066: a 404 parent view degrades; the URL-resolved repo is named."""
    def fake_gh_json(*args):
        if "--json" in args and args[-1] == "body,comments":
            return None
        return {"number": 220, "title": "t", "url": "u", "body": "b",
                "parent": {"number": 1054, "url": (
                    "https://github.com/nateprich-projects/"
                    "command-center/issues/1054")},
                "comments": []}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(
        "nateprich-projects/The-League", 220)  # must not raise
    assert found["parent"]["comments"] == []
    assert found["parent"]["comments_unavailable"] is True
    assert found["parent"]["ref"] == \
        "nateprich-projects/command-center#1054"


def test_the_review_question_treats_nate_comments_as_amending_decisions():
    text = (ROOT / "routines" / "muse-review.md").read_text()
    assert "ticket.comments" in text
    assert "nate-direct" in text and "nate-relayed" in text
    assert "amend" in text
    assert "unknown" in text


def test_cli_shows_the_ticket_comments_with_voices(monkeypatch, capsys):
    rows = [comment("Retire the drift checks.", "nate-direct")]
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: pr_view())
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(
        review, "fetch_ticket",
        lambda repo, number: ticket(comments=rows))
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(
        review, "fetch_verdict", lambda repo, pr: verdict())
    monkeypatch.setattr(
        review, "fetch_stop_counter",
        lambda items_loader=None, now=None: dict(STOP_COUNTER))
    assert review.main(["7", "--repo", REPO]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["ticket"]["comments"] == [
        {"author": "nateprich", "created_at": "2026-09-13T00:00:00Z",
         "voice": "nate-direct", "body": "Retire the drift checks."}]


# -- every ticket the PR closes (#1088) ---------------------------------------
#
# jeffy PR #132 closed #131 and #129 but the packet showed only the branch
# ticket #131, so the engine rejected the hobby-linux change #129 asked
# for. The packet now carries the union under `tickets` while `ticket`
# stays the branch ticket for the pre-check rows.

def test_closing_refs_parse_numbers_in_the_pr_repo():
    view = pr_view(closingIssuesReferences=[
        {"number": 131}, {"number": 129}])
    assert review.closing_ticket_refs(view, REPO) == [
        (REPO, 131), (REPO, 129)]


def test_closing_refs_keep_each_entry_own_repo():
    other = "owner/other"
    view = pr_view(closingIssuesReferences=[
        {"number": 1, "repository": {"nameWithOwner": other}},
        {"number": 2, "url": "https://github.com/{}/issues/2".format(other)},
        {"number": 3, "ref": "{}#3".format(other)},
        {"number": 9},
    ])
    assert review.closing_ticket_refs(view, REPO) == [
        (other, 1), (other, 2), (other, 3), (REPO, 9)]


def test_closing_refs_deduplicate_and_drop_rubbish():
    view = pr_view(closingIssuesReferences=[
        {"number": 9}, {"number": 9},
        None, "nonsense", {}, {"number": True}, {"number": 0},
    ])
    assert review.closing_ticket_refs(view, REPO) == [(REPO, 9)]


def test_closing_refs_read_a_graphql_nodes_connection():
    view = pr_view(closingIssuesReferences={
        "nodes": [{"number": 129,
                   "repository": {"nameWithOwner": REPO}}]})
    assert review.closing_ticket_refs(view, REPO) == [(REPO, 129)]


def test_closing_refs_without_a_list_read_as_empty():
    assert review.closing_ticket_refs(pr_view(), REPO) == []
    assert review.closing_ticket_refs(
        pr_view(closingIssuesReferences=None), REPO) == []


def test_packet_carries_each_closing_ticket_shaped_like_the_branch_one():
    first = ticket(number=131, ref=REPO + "#131",
                   body="The workflow and the runner are unchanged.")
    second = ticket(number=129, ref=REPO + "#129",
                    body="Run the tests workflow on hobby-linux.",
                    comments=[comment("ship it", "nate-direct")])
    found = packet(ticket=first, tickets=[first, second])
    assert found["ticket"]["number"] == 131
    assert [entry["number"] for entry in found["tickets"]] == [131, 129]
    assert found["tickets"][1]["body"].startswith("Run the tests workflow")
    assert found["tickets"][1]["comments"][0]["voice"] == "nate-direct"
    json.dumps(found)


def test_a_two_ticket_pr_is_not_rejected_for_the_second_ticket_change():
    """The #1088 accept fixture: the first ticket calls the workflow
    unchanged, the second asks for exactly the hobby-linux change."""
    first = ticket(number=131, ref=REPO + "#131",
                   body="The Python 3.9 pin, the workflow and the runner "
                        "are unchanged. Carry LD_LIBRARY_PATH.")
    second = ticket(number=129, ref=REPO + "#129",
                    body="Run the tests workflow on the self-hosted "
                         "hobby-linux runner.")
    view = pr_view(headRefName="ticket/131",
                   files=[{"path": ".github/workflows/tests.yml"}])
    found = packet(pr_view=view, ticket=first, tickets=[first, second],
                   verdict=None)
    assert [entry["number"] for entry in found["tickets"]] == [131, 129]
    assert found["ticket"]["ref"] == REPO + "#131"
    assert found["precheck"]["pass"] is True, found["precheck"]


def test_a_single_ticket_packet_defaults_to_the_branch_ticket_alone():
    found = packet()
    assert [entry["number"] for entry in found["tickets"]] == [9]
    assert found["tickets"][0]["ref"] == found["ticket"]["ref"]
    assert found["tickets"][0]["body"] == found["ticket"]["body"]


def test_a_ticketless_branch_carries_an_empty_tickets_list():
    view = pr_view(headRefName="docs/meta-terms-read")
    found = packet(pr_view=view, ticket=None)
    assert found["tickets"] == []
    assert found["ticket"]["body"] is None


def test_collect_fetches_each_closing_ticket_once(monkeypatch):
    view = pr_view(headRefName="ticket/131",
                   closingIssuesReferences=[{"number": 131},
                                            {"number": 129}])
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: view)
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    seen = []

    def fake_ticket(repo, number):
        seen.append((repo, number))
        return ticket(number=number, ref=repo + "#" + str(number))

    monkeypatch.setattr(review, "fetch_ticket", fake_ticket)
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(
        review, "fetch_pr_comments", lambda repo, pr: empty_pr_comments())
    found = review.collect(REPO, 132, items_loader=lambda: [])
    assert seen == [(REPO, 131), (REPO, 129)]
    assert [entry["number"] for entry in found["tickets"]] == [131, 129]
    assert found["ticket"]["number"] == 131


def test_collect_without_closing_refs_fetches_only_the_branch_ticket(
        monkeypatch):
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: pr_view())
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    seen = []
    monkeypatch.setattr(
        review, "fetch_ticket",
        lambda repo, number: seen.append(number) or ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(
        review, "fetch_pr_comments", lambda repo, pr: empty_pr_comments())
    found = review.collect(REPO, 7, items_loader=lambda: [])
    assert seen == [9]
    assert [entry["number"] for entry in found["tickets"]] == [9]


def test_the_review_question_names_the_tickets_union_as_the_spec():
    text = (ROOT / "routines" / "muse-review.md").read_text()
    assert "tickets" in text and "union" in text
    assert "authorised" in text
    assert "branch ticket" in text


# -- diffs over GitHub's line cap (#1114) -----------------------------------

TOO_LARGE = ("could not find pull request diff: HTTP 406: Sorry, the diff "
             "exceeded the maximum number of lines (20000)\n"
             "PullRequest.diff too_large")


def _proc(args, returncode=0, stdout="", stderr=""):
    import subprocess
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def _files_page(start, count, missing=()):
    rows = []
    for index in range(start, start + count):
        row = {"filename": "f{}.py".format(index)}
        if index not in missing:
            row["patch"] = "@@ -0,0 +1 @@\n+line {}".format(index)
        rows.append(row)
    return rows


def _stub_gh(monkeypatch, diff_proc, pages):
    calls = []

    def fake(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["gh", "pr", "diff"]:
            return diff_proc(args)
        page = int(args[2].rsplit("page=", 1)[1])
        return _proc(args, stdout=json.dumps(pages[page - 1]))

    monkeypatch.setattr(funnel, "_run_gh", fake)
    return calls


def _collect_with_diff(monkeypatch):
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: pr_view())
    monkeypatch.setattr(review, "fetch_scope", _compare_unavailable)
    monkeypatch.setattr(
        review, "fetch_ticket", lambda repo, number: ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(
        review, "fetch_pr_comments", lambda repo, pr: empty_pr_comments())
    return review.collect(REPO, 7, items_loader=lambda: [])


def test_too_large_diff_falls_back_to_the_files_api(monkeypatch):
    pages = [_files_page(0, 100, missing={3}), _files_page(100, 2,
                                                          missing={101})]
    calls = _stub_gh(
        monkeypatch, lambda args: _proc(args, 1, stderr=TOO_LARGE), pages)
    found = _collect_with_diff(monkeypatch)
    assert found["diff_truncated"] is True
    assert found["diff_omitted_files"] == 2
    assert "diff --git a/f0.py b/f0.py\n" in found["diff"]
    assert "diff --git a/f100.py b/f100.py\n" in found["diff"]
    assert "f3.py" not in found["diff"]
    assert "f101.py" not in found["diff"]
    assert found["diff"].index("f99.py") < found["diff"].index("f100.py")
    file_calls = [c for c in calls if c[:2] == ["gh", "api"]]
    assert [c[2] for c in file_calls] == [
        "repos/owner/repo/pulls/7/files?per_page=100&page=1",
        "repos/owner/repo/pulls/7/files?per_page=100&page=2",
    ]
    json.dumps(found)


def test_normal_diff_takes_gh_pr_diff_with_no_flag(monkeypatch):
    calls = _stub_gh(
        monkeypatch,
        lambda args: _proc(args, stdout="diff --git a/x b/x\n"), [])
    found = _collect_with_diff(monkeypatch)
    assert found["diff"] == "diff --git a/x b/x\n"
    assert "diff_truncated" not in found
    assert "diff_omitted_files" not in found
    assert not [c for c in calls if c[:2] == ["gh", "api"]]


def test_other_diff_failures_still_raise(monkeypatch):
    import pytest
    for stderr in ("HTTP 401: Bad credentials",
                   "API rate limit exceeded for user ID 1",
                   "no pull requests found for branch"):
        _stub_gh(monkeypatch,
                 lambda args, e=stderr: _proc(args, 1, stderr=e), [])
        with pytest.raises(funnel.GitHubError):
            review.fetch_diff(REPO, 7)


def test_files_api_failure_raises(monkeypatch):
    import pytest

    def fake(args, **kwargs):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(args, 1, stderr=TOO_LARGE)
        return _proc(args, 1, stderr="HTTP 404: Not Found")

    monkeypatch.setattr(funnel, "_run_gh", fake)
    with pytest.raises(funnel.GitHubError):
        review.fetch_diff(REPO, 7)


# -- review scope from the live compare (#1043) -------------------------------

PR_BASE_SHA = "prbase0000000000000000000000000000000001"
MERGE_BASE_SHA = "merge0000000000000000000000000000000002"
HEAD_SHA = "head000000000000000000000000000000000003"

BRANCH_FILES = ["branch-{}.py".format(index) for index in range(6)]


def compare_payload(filenames, merge_base=MERGE_BASE_SHA):
    return {
        "merge_base_commit": {"sha": merge_base},
        "files": [
            {"filename": name,
             "patch": "@@ -0,0 +1 @@\n+line {}".format(name)}
            for name in filenames
        ],
    }


def pr_view_with_49_files():
    """#194's shape: the PR files API lists 49 paths, compare lists 6."""
    files = ([{"path": "main-{}.py".format(index)} for index in range(43)]
             + [{"path": name} for name in BRANCH_FILES])
    return pr_view(files=files, baseRefOid=PR_BASE_SHA, headRefOid=HEAD_SHA)


def test_fetch_scope_reads_the_compare_endpoint(monkeypatch):
    seen = {}

    def fake(*args):
        seen["args"] = list(args)
        return compare_payload(BRANCH_FILES)

    monkeypatch.setattr(funnel, "_gh_json", fake)
    changed, diff, merge_base = review.fetch_scope(REPO, "main", HEAD_SHA)
    assert seen["args"] == [
        "gh", "api", "repos/{}/compare/main...{}".format(REPO, HEAD_SHA)]
    assert changed == sorted(BRANCH_FILES)
    assert "diff --git a/branch-0.py b/branch-0.py\n" in diff
    assert "@@ -0,0 +1 @@\n+line branch-0.py" in diff
    assert merge_base == MERGE_BASE_SHA


def test_fetch_scope_raises_when_the_compare_call_fails(monkeypatch):
    import pytest
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)
    with pytest.raises(funnel.GitHubError):
        review.fetch_scope(REPO, "main", HEAD_SHA)


def test_fetch_scope_raises_on_an_unreadable_compare_answer(monkeypatch):
    import pytest
    for payload in ([], {"files": {}}, {"files": None}, "nonsense"):
        monkeypatch.setattr(
            funnel, "_gh_json", lambda *args, p=payload: p)
        with pytest.raises(funnel.GitHubError):
            review.fetch_scope(REPO, "main", HEAD_SHA)


def test_fetch_scope_counts_patchless_entries_like_the_files_rebuild(
        monkeypatch):
    payload = compare_payload(["a.py"])
    payload["files"].append({"filename": "big.bin"})  # binary: no patch
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: payload)
    changed, diff, _ = review.fetch_scope(REPO, "main", HEAD_SHA)
    assert changed == ["a.py", "big.bin"]
    assert isinstance(diff, review.AssembledDiff)
    assert diff.omitted_patches == 1
    assert "big.bin" not in diff


def test_fetch_scope_keeps_a_complete_diff_plain(monkeypatch):
    monkeypatch.setattr(
        funnel, "_gh_json", lambda *args: compare_payload(["a.py"]))
    _, diff, _ = review.fetch_scope(REPO, "main", HEAD_SHA)
    assert type(diff) is str


def test_fetch_pr_reads_the_recorded_base_sha(monkeypatch):
    seen = {}

    def fake(*args):
        seen["args"] = list(args)
        return {"number": 7}

    monkeypatch.setattr(funnel, "_gh_json", fake)
    review.fetch_pr(REPO, 7)
    fields = seen["args"][seen["args"].index("--json") + 1].split(",")
    assert "baseRefOid" in fields
    assert "mergedBy" in fields


def _stub_collect_prereqs(monkeypatch, view):
    monkeypatch.setattr(review, "fetch_pr", lambda repo, pr: view)
    monkeypatch.setattr(
        review, "fetch_ticket", lambda repo, number: ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(review, "fetch_ci_runs", lambda repo, branch: [])
    monkeypatch.setattr(
        review, "fetch_pr_comments", lambda repo, pr: empty_pr_comments())


def test_compare_scope_holds_only_the_branch_files(monkeypatch):
    _stub_collect_prereqs(monkeypatch, pr_view_with_49_files())
    monkeypatch.setattr(
        review, "fetch_scope",
        lambda repo, base, head: (
            list(BRANCH_FILES), "compare diff text", MERGE_BASE_SHA))

    def fail_if_called(repo, pr):
        raise AssertionError("compare scope must not read the PR diff")

    monkeypatch.setattr(review, "fetch_diff", fail_if_called)
    found = review.collect(REPO, 7, items_loader=lambda: [])
    assert found["changed_files"] == sorted(BRANCH_FILES)
    assert len(found["changed_files"]) == 6
    assert found["diff"] == "compare diff text"
    assert found["merge_base"] == MERGE_BASE_SHA
    assert found["pr_base_sha"] == PR_BASE_SHA
    assert found["merge_base"] != found["pr_base_sha"]
    assert found["scope_source"] == "compare"
    json.dumps(found)


def test_failing_compare_falls_back_to_the_pr_reads(monkeypatch):
    _stub_collect_prereqs(
        monkeypatch,
        pr_view(files=[{"path": "funnel.py"}], baseRefOid=PR_BASE_SHA))
    monkeypatch.setattr(review, "fetch_scope", _compare_unavailable)
    monkeypatch.setattr(
        review, "fetch_diff", lambda repo, pr: "pr diff text")
    found = review.collect(REPO, 7, items_loader=lambda: [])
    assert found["scope_source"] == "pr"
    assert found["changed_files"] == ["funnel.py"]
    assert found["diff"] == "pr diff text"
    assert found["merge_base"] is None
    assert found["pr_base_sha"] == PR_BASE_SHA


def test_missing_base_skips_the_compare_call(monkeypatch):
    _stub_collect_prereqs(
        monkeypatch,
        pr_view(files=[{"path": "funnel.py"}], baseRefName=""))

    def fail_if_called(repo, base, head):
        raise AssertionError("no base, no compare call")

    monkeypatch.setattr(review, "fetch_scope", fail_if_called)
    monkeypatch.setattr(
        review, "fetch_diff", lambda repo, pr: "pr diff text")
    found = review.collect(REPO, 7, items_loader=lambda: [])
    assert found["scope_source"] == "pr"
    assert found["changed_files"] == ["funnel.py"]


def test_packet_defaults_to_the_pr_scope():
    found = packet()
    assert found["scope_source"] == "pr"
    assert found["merge_base"] is None
    assert found["pr_base_sha"] is None


def test_packet_carries_the_compare_scope_and_both_bases():
    view = pr_view(baseRefOid=PR_BASE_SHA)
    found = packet(pr_view=view, changed_files=list(BRANCH_FILES),
                   merge_base=MERGE_BASE_SHA, scope_source="compare")
    assert found["changed_files"] == sorted(BRANCH_FILES)
    assert found["merge_base"] == MERGE_BASE_SHA
    assert found["pr_base_sha"] == PR_BASE_SHA
    assert found["scope_source"] == "compare"


def test_open_overlap_reads_the_compare_scope():
    open_prs = [{"number": 8, "headRefName": "ticket/10",
                 "files": [{"path": "branch-0.py"},
                           {"path": "main-0.py"}]}]
    found = packet(pr_view=pr_view_with_49_files(), open_prs=open_prs,
                   changed_files=list(BRANCH_FILES),
                   merge_base=MERGE_BASE_SHA, scope_source="compare")
    assert found["overlap"] == [{"pr": 8, "branch": "ticket/10",
                                 "files": ["branch-0.py"]}]


def test_protected_row_ignores_main_only_files_outside_the_compare_scope():
    view = pr_view(files=[{"path": "AGENTS.md"}, {"path": "funnel.py"}])
    found = packet(pr_view=view, changed_files=["funnel.py"],
                   merge_base=MERGE_BASE_SHA, scope_source="compare")
    assert found["protected"]["touched"] == []
    assert found["protected"]["rules"] == []
