"""The eight deterministic pre-check rows (#799, Phase 1 of #794).

Each row gets a passing and a failing fixture here. The rows run in ticket
order before any model is called; any reason fails the packet. Builders
mirror tests/test_engine_review_packet.py, except the default verdict is
None so the default packet passes every row.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import review  # noqa: E402

REPO = "owner/repo"
WORKBENCH = "nateprich-projects/workbench"
SHA = "abc123def456"
OTHER_SHA = "7890fedcba98"
HEAD_DATE = "2026-09-13T12:00:00Z"
NEWER = "2026-09-13T13:00:00Z"
OLDER = "2026-09-13T11:00:00Z"
MERGED_AT = "2026-09-13T14:00:00Z"
CLOSED_AT = "2026-09-13T15:00:00Z"


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
        "commits": [{"oid": SHA, "committedDate": HEAD_DATE}],
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
        "parent": {"number": 1, "title": "another plan", "state": "OPEN",
                   "url": "https://github.com/{}/issues/1".format(REPO)},
    }
    data.update(kw)
    return data


def verdict(**kw):
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA,
            "blocking": []}
    body.update(kw)
    return body


def merged(number, at, *paths):
    return {"number": number, "mergedAt": at,
            "files": [{"path": path} for path in paths]}


STOP_COUNTER = {"window_days": 7, "count": 0, "refs": [],
                "stop_auto_merging": False}


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
        "merged_prs": [],
        "verdict": None,
        "stop_counter": dict(STOP_COUNTER),
        "collected_at": "2026-09-13T00:00:00+00:00",
    }
    args.update(kw)
    return review.build_packet(**args)


# -- row 1: PR state ---------------------------------------------------------

def test_pr_open_row_rejects_a_merged_pr_with_its_merge_time():
    view = pr_view(state="CLOSED", mergedAt=MERGED_AT, closedAt=MERGED_AT)
    found = packet(pr_view=view, verdict=None)
    assert found["merged_at"] == MERGED_AT
    assert found["precheck"]["reasons"] == [
        "pr_not_open state=CLOSED merged_at={}".format(MERGED_AT)]


def test_pr_open_row_rejects_a_closed_unmerged_pr_with_its_close_time():
    view = pr_view(state="CLOSED", closedAt=CLOSED_AT)
    found = packet(pr_view=view, verdict=None)
    assert found["precheck"]["reasons"] == [
        "pr_not_open state=CLOSED closed_at={}".format(CLOSED_AT)]


def test_pr_open_row_leaves_an_open_pr_unchanged():
    view = pr_view(state="OPEN", mergedAt=MERGED_AT, closedAt=CLOSED_AT)
    assert packet(pr_view=view, verdict=None)["precheck"] == {
        "pass": True, "reasons": []}


# -- row 2: the freeze ------------------------------------------------------

def test_freeze_rejects_a_routines_diff_under_another_parent():
    view = pr_view(files=[{"path": "routines/muse.md"}])
    found = packet(pr_view=view)
    assert found["precheck"]["pass"] is False
    # The protected row fires on the same file behind it; the freeze row
    # leads because it runs first.
    reason = found["precheck"]["reasons"][0]
    assert reason.startswith("freeze:")
    assert "routines/muse.md" in reason
    assert "#1" in reason and "#794" in reason


def test_freeze_passes_the_same_diff_under_794():
    view = pr_view(files=[{"path": "routines/muse.md"}])
    mine = ticket(body="Parent: #794.\n\nWhat: trim routines/muse.md.\n\n"
                       "Risk: escalated",
                  parent={"number": 794, "title": "the engine plan"})
    assert packet(pr_view=view, ticket=mine)["precheck"] == {
        "pass": True, "reasons": []}


def test_freeze_rejects_a_skills_diff():
    view = pr_view(files=[{"path": "skills/shape/SKILL.md"}])
    reasons = packet(pr_view=view)["precheck"]["reasons"]
    assert reasons[0].startswith("freeze:")
    assert "skills/shape/SKILL.md" in reasons[0]


def test_freeze_rejects_a_diff_changing_a_doomed_parser():
    diff = ("diff --git a/funnel.py b/funnel.py\n"
            "@@ -1 +1 @@\n"
            "-found = OLD_SEARCH(body)\n"
            "+found = PROSE_DEPENDENCY_RE.search(body)\n")
    view = pr_view(files=[{"path": "funnel.py"}])
    reasons = packet(pr_view=view, diff=diff)["precheck"]["reasons"]
    assert len(reasons) == 1 and reasons[0].startswith("freeze:")
    assert "PROSE_DEPENDENCY_RE" in reasons[0]


def test_freeze_ignores_a_parser_named_only_in_context():
    diff = ("diff --git a/funnel.py b/funnel.py\n"
            "@@ -1,2 +1,2 @@\n"
            " PROSE_DEPENDENCY_RE = re.compile(\n"
            "-old = 1\n"
            "+new = 1\n")
    view = pr_view(files=[{"path": "funnel.py"}])
    assert packet(pr_view=view, diff=diff)["precheck"] == {
        "pass": True, "reasons": []}


def test_freeze_rejects_frozen_ground_with_no_ticket():
    view = pr_view(files=[{"path": "routines/muse.md"},
                          {"path": "funnel.py"}],
                   headRefName="docs/drive-by")
    reasons = packet(pr_view=view, ticket=None)["precheck"]["reasons"]
    assert any(r.startswith("freeze:") and "no ticket" in r
               for r in reasons)


# -- row 3: CI ---------------------------------------------------------------

def test_ci_row_fails_a_red_rollup_and_names_the_check():
    view = pr_view(statusCheckRollup=[
        {"name": "tests", "conclusion": "SUCCESS", "status": "COMPLETED"},
        {"name": "lint", "conclusion": "FAILURE", "status": "COMPLETED"},
    ])
    reasons = packet(pr_view=view)["precheck"]["reasons"]
    assert reasons == ["ci: CI not green (state red): lint"]


def test_ci_row_fails_while_a_check_is_still_running():
    view = pr_view(statusCheckRollup=[
        {"name": "slow", "conclusion": None, "status": "IN_PROGRESS"},
    ])
    reasons = packet(pr_view=view)["precheck"]["reasons"]
    assert reasons == ["ci: CI not green (state unknown)"]


def test_ci_row_passes_a_green_rollup():
    assert packet()["precheck"] == {"pass": True, "reasons": []}


# -- row 4: verdict coverage --------------------------------------------------

def test_verdict_row_fails_when_a_verdict_covers_this_head():
    found = packet(verdict=verdict())
    assert found["precheck"]["reasons"] == [
        "verdict: a verdict already covers head {}".format(SHA[:12])]


def test_verdict_row_passes_when_the_verdict_is_stale():
    found = packet(verdict=verdict(head_sha=OTHER_SHA))
    assert found["precheck"] == {"pass": True, "reasons": []}


def test_verdict_row_passes_with_no_verdict():
    assert packet(verdict=None)["precheck"] == {"pass": True, "reasons": []}


# -- row 5: merged since the head ----------------------------------------------

def test_merged_row_fails_on_a_newer_merge_sharing_a_file():
    rows = [merged(5, NEWER, "funnel.py"),
            merged(4, OLDER, "funnel.py")]
    found = packet(merged_prs=rows)
    assert found["merged_overlap"] == [
        {"pr": 5, "merged_at": NEWER, "files": ["funnel.py"]}]
    assert found["precheck"]["reasons"] == [
        "merged-overlap: PR #5 merged at {} touches funnel.py".format(NEWER)]


def test_merged_row_passes_when_only_older_merges_share():
    rows = [merged(4, OLDER, "funnel.py")]
    found = packet(merged_prs=rows)
    assert found["merged_overlap"] == []
    assert found["precheck"] == {"pass": True, "reasons": []}


def test_merged_row_passes_when_a_newer_merge_shares_nothing():
    rows = [merged(5, NEWER, "other.py")]
    assert packet(merged_prs=rows)["precheck"] == {
        "pass": True, "reasons": []}


def test_merged_row_ignores_the_candidate_itself():
    rows = [merged(7, NEWER, "funnel.py")]
    assert packet(merged_prs=rows)["precheck"] == {
        "pass": True, "reasons": []}


def test_merged_row_fails_closed_without_a_head_date():
    view = pr_view(commits=[])
    rows = [merged(4, OLDER, "funnel.py")]
    found = packet(pr_view=view, merged_prs=rows)
    assert found["head_date"] is None
    assert len(found["precheck"]["reasons"]) == 1
    assert found["precheck"]["reasons"][0].startswith("merged-overlap:")


def test_head_date_prefers_the_head_sha_then_the_newest():
    view = pr_view(headRefOid="new",
                   commits=[{"oid": "old", "committedDate": OLDER},
                            {"oid": "new", "committedDate": HEAD_DATE}])
    assert review.head_date(view) == HEAD_DATE
    view = pr_view(headRefOid="missing",
                   commits=[{"oid": "old", "committedDate": OLDER},
                            {"oid": "new", "committedDate": HEAD_DATE}])
    assert review.head_date(view) == HEAD_DATE
    assert review.head_date(pr_view(commits=[])) is None


# -- row 6: protected paths ----------------------------------------------------

def test_protected_row_fails_an_unasked_touch():
    view = pr_view(files=[{"path": "AGENTS.md"}])
    reasons = packet(pr_view=view)["precheck"]["reasons"]
    assert reasons == ["protected: AGENTS.md touched but the ticket "
                       "does not ask for it"]


def test_protected_row_passes_when_the_ticket_names_the_file():
    view = pr_view(files=[{"path": "AGENTS.md"}])
    asking = ticket(body="Parent: #1.\n\nWhat: amend AGENTS.md.\n\n"
                         "Risk: standard")
    assert packet(pr_view=view, ticket=asking)["precheck"] == {
        "pass": True, "reasons": []}


def test_protected_row_passes_when_the_ticket_names_the_rule():
    view = pr_view(files=[{"path": "skills/shape/SKILL.md"}])
    asking = ticket(body="Parent: #1.\n\nWhat: rewrite skills/ judgement "
                         "text.\n\nRisk: standard",
                    parent={"number": 794, "title": "the engine plan"})
    assert packet(pr_view=view, ticket=asking)["precheck"] == {
        "pass": True, "reasons": []}


def test_protected_row_fails_without_a_ticket():
    view = pr_view(files=[{"path": "plan.md"}, {"path": "funnel.py"}],
                   headRefName="docs/drive-by")
    reasons = packet(pr_view=view, ticket=None)["precheck"]["reasons"]
    assert reasons == ["protected: plan.md touched but the ticket does "
                       "not ask for it"]


# -- row 7: the stop bar -------------------------------------------------------

def test_stop_row_fails_while_the_bar_is_set():
    counter = {"window_days": 7, "count": 3, "refs": ["a#1", "a#2", "a#3"],
               "stop_auto_merging": True}
    reasons = packet(stop_counter=counter)["precheck"]["reasons"]
    assert reasons == ["stop: stop_auto_merging set "
                       "(3 rejected in 7d: a#1, a#2, a#3)"]


def test_stop_row_passes_while_the_bar_is_clear():
    assert packet()["precheck"] == {"pass": True, "reasons": []}


# -- row 8: repo rules ----------------------------------------------------------

CONNECTOR = "chatgpt-messages-connector/sender.py"


def test_repo_row_fails_a_standard_messages_ticket():
    view = pr_view(files=[{"path": CONNECTOR}])
    standard = ticket(body="Parent: #700.\n\nWhat: change the sender.\n\n"
                           "Risk: standard")
    found = packet(repo=WORKBENCH, pr_view=view, ticket=standard)
    assert found["precheck"]["reasons"] == [
        "repo-rules: chatgpt-messages-connector/ in {} is escalated-only "
        "but the ticket is standard (#724)".format(WORKBENCH)]


def test_repo_row_passes_an_escalated_messages_ticket():
    view = pr_view(files=[{"path": CONNECTOR}])
    escalated = ticket(body="Parent: #700.\n\nWhat: change the sender.\n\n"
                            "Risk: escalated")
    found = packet(repo=WORKBENCH, pr_view=view, ticket=escalated)
    assert found["precheck"] == {"pass": True, "reasons": []}


def test_repo_row_fails_an_unmarked_messages_ticket():
    view = pr_view(files=[{"path": CONNECTOR}])
    unmarked = ticket(title="Wire up the Messages connector",
                      body="Parent: #700.\n\nWhat: change code under "
                           "chatgpt-messages-connector/.")
    reasons = packet(repo=WORKBENCH, pr_view=view,
                     ticket=unmarked)["precheck"]["reasons"]
    assert len(reasons) == 1 and reasons[0].startswith("repo-rules:")


def test_repo_row_passes_a_messages_free_workbench_diff():
    view = pr_view(files=[{"path": "notes/today.md"}])
    assert packet(repo=WORKBENCH, pr_view=view)["precheck"] == {
        "pass": True, "reasons": []}


def test_repo_row_is_scoped_to_its_repo():
    view = pr_view(files=[{"path": CONNECTOR}])
    assert packet(repo=REPO, pr_view=view)["precheck"] == {
        "pass": True, "reasons": []}


def test_repo_row_fails_without_a_ticket():
    view = pr_view(files=[{"path": CONNECTOR}], headRefName="wip/sender")
    reasons = packet(repo=WORKBENCH, pr_view=view,
                     ticket=None)["precheck"]["reasons"]
    assert len(reasons) == 1 and reasons[0].startswith("repo-rules:")


# -- the combined precheck ------------------------------------------------------

def test_reasons_come_out_in_row_order():
    view = pr_view(files=[{"path": "routines/muse.md"},
                          {"path": CONNECTOR}],
                   statusCheckRollup=[
                       {"name": "tests", "conclusion": "FAILURE",
                        "status": "COMPLETED"}])
    reasons = packet(repo=WORKBENCH,
                     pr_view=view)["precheck"]["reasons"]
    assert [reason.split(":")[0] for reason in reasons] == [
        "freeze", "ci", "protected", "repo-rules"]


def test_a_clean_packet_passes_with_no_reasons():
    found = packet()
    assert found["precheck"] == {"pass": True, "reasons": []}
    json.dumps(found)  # the packet stays JSON by contract


def test_packet_carries_the_new_precheck_fields():
    view = pr_view(files=[{"path": "funnel.py"}])
    rows = [merged(5, NEWER, "funnel.py")]
    found = packet(pr_view=view, merged_prs=rows)
    assert found["head_date"] == HEAD_DATE
    assert found["merged_overlap"] == [
        {"pr": 5, "merged_at": NEWER, "files": ["funnel.py"]}]
    assert found["ticket"]["parent"]["number"] == 1
    assert found["precheck"]["pass"] is False


def test_cli_packet_carries_a_failing_precheck(monkeypatch, capsys):
    monkeypatch.setattr(
        review, "fetch_pr",
        lambda repo, pr: pr_view(files=[{"path": "routines/muse.md"}]))
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(
        review, "fetch_ticket", lambda repo, number: ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    monkeypatch.setattr(
        review, "fetch_stop_counter",
        lambda items_loader=None, now=None: dict(STOP_COUNTER))
    assert review.main(["7", "--repo", REPO]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["precheck"]["pass"] is False
    assert found["precheck"]["reasons"][0].startswith("freeze:")
