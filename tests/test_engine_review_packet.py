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

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import review  # noqa: E402

REPO = "owner/repo"
SHA = "abc123def456"
OTHER_SHA = "7890fedcba98"


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
    }
    data.update(kw)
    return data


def verdict(**kw):
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA,
            "blocking": []}
    body.update(kw)
    return body


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
        "verdict": verdict(),
        "stop_counter": dict(STOP_COUNTER),
        "collected_at": "2026-09-13T00:00:00+00:00",
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
    monkeypatch.setattr(review, "fetch_diff", lambda repo, pr: "diff text")
    monkeypatch.setattr(
        review, "fetch_ticket", lambda repo, number: ticket())
    monkeypatch.setattr(
        review, "fetch_plan_md", lambda repo: ("# design record", False))
    monkeypatch.setattr(review, "fetch_open_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_merged_prs", lambda repo: [])
    monkeypatch.setattr(review, "fetch_verdict", lambda repo, pr: None)
    seen = {}

    def fake_runs(repo, branch):
        seen["repo"] = repo
        seen["branch"] = branch
        return [gh_run()]

    monkeypatch.setattr(review, "fetch_ci_runs", fake_runs)
    found = review.collect(REPO, 7, items_loader=lambda: [])
    assert seen == {"repo": REPO, "branch": "ticket/9"}
    assert found["ci"]["green_run_at"] == RUN_AT


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
        "body": None, "parent": None, "comments": []}


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
                "parent": {"number": 1, "comments": rows},
                "comments": []}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(REPO, 9)
    assert len(calls) == 1
    assert found["parent"]["comments"] == rows


def test_fetch_ticket_reads_parent_comments_with_one_parent_view(monkeypatch):
    rows = [comment("on the plan")]
    calls = []

    def fake_gh_json(*args):
        calls.append(args)
        if len(calls) == 1:
            return {"number": 9, "title": "t", "url": "u", "body": "b",
                    "parent": {"number": 1}, "comments": []}
        if len(calls) == 2:
            return {"number": 1,
                    "repository": {"full_name": REPO}}
        return {"comments": rows}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(REPO, 9)
    assert len(calls) == 3
    assert calls[1] == (
        "gh", "api", "repos/{}/issues/9/parent".format(REPO))
    assert calls[2] == (
        "gh", "issue", "view", "1", "--repo", REPO, "--json", "comments")
    assert found["parent"]["comments"] == rows
    assert found["parent"]["ref"] == "{}#1".format(REPO)


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
        return {"comments": rows}

    monkeypatch.setattr(funnel, "_gh_json", fake_gh_json)
    found = review.fetch_ticket(member, 221)
    assert calls[1] == (
        "gh", "api", "repos/{}/issues/221/parent".format(member))
    assert calls[2] == (
        "gh", "issue", "view", "1054", "--repo", home,
        "--json", "comments")
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
                               "repository": {"full_name": REPO}},
                    "comments": []}
        return {"comments": rows}

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
