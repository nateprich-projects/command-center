"""Gate implement re-offer on a same-head open PR until a new verdict (#2541).

The Muse implement lane re-bound #2492 seven times noting PR #2521 with no
PR commit between finishes while other tickets sat startable. A rejected
head hands back to the engineer once; a finish that pushes nothing records
its head in the PR body it rewrites, and the shared offer path skips that
head until a new review verdict lands. No local state: record, head, and
verdict all come from the PR snapshot.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import implement  # noqa: E402
from engine import review  # noqa: E402
from funnel import Item  # noqa: E402

REPO = "nateprich/example"
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)

H1 = "a" * 40
H2 = "b" * 40
H9 = "c" * 40

T_V = "2026-10-11T21:11:00Z"
T_M = "2026-10-12T01:19:00Z"
T_V2 = "2026-10-12T04:25:00Z"

#: What makes a PR row the funnel's own: a same-repository head opened by
#: a trusted account.
OWNER_ROW = {"isCrossRepository": False, "author": {"login": "nateprich"}}


def _project(number=7, status="Building"):
    return Item(repo=REPO, number=number, title="Project %d" % number,
                url="https://github.com/{}/issues/{}".format(REPO, number),
                state="OPEN", status=status, klass="Implement",
                children_total=2,
                origin="agent", risk="standard", needs="none")


def _ticket(number, parent):
    return Item(repo=REPO, number=number, title="Ticket %d" % number,
                url="https://github.com/{}/issues/{}".format(REPO, number),
                state="OPEN", parent=parent.ref,
                origin="agent", risk="standard", needs="none",
                item_id="item-%d" % number)


def _verdict(head, at):
    return {"verdict": "rejected", "head_sha": head,
            "comment_created_at": at,
            "blocking": ["requirement unmet: the widget stays dark"]}


def _body(*markers):
    return "\n".join([
        "Implements #101.",
        "",
        "Summary:",
        "Looked at the rejected head and changed nothing.",
        "",
        "<!-- command-center-evidence -->",
        "Evidence, written by the runner:",
        "- sha: {}".format(H1),
        *markers,
        "<!-- /command-center-evidence -->",
        "",
    ])


def _pr_row(number, head, body="", verdict=None, state="OPEN", **kw):
    row = {"number": number, "state": state,
           "url": "https://github.com/{}/pull/{}".format(REPO, number),
           "headRefName": "ticket/101", "headRefOid": head,
           "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
           **OWNER_ROW}
    if body:
        row["body"] = body
    # The batched snapshot always stamps the verdict key on open rows, even
    # when there is none, so the fixture carries it too (a missing key would
    # send the gate down the live-read path).
    row["verdict"] = verdict
    row.update(kw)
    return row


def _board():
    project = _project()
    return project, _ticket(101, project), _ticket(102, project)


def test_rejected_handback_without_marker_is_offered():
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    pr_facts = {ticket_a.ref: _pr_row(2521, H1, verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    assert [i.ref for i in funnel.startable(items)] == [
        ticket_a.ref, ticket_b.ref]


def test_no_push_same_head_withholds_and_other_ticket_offered():
    """The #2492/PR #2521 replay: one bind, then skip until a new verdict."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V))}

    withheld = funnel.no_push_reoffer_withheld(items, pr_facts)
    assert withheld == {ticket_a.ref}
    # No second done-with-no-push on the same head: the looping ticket is
    # not startable, while the other startable ticket is offered.
    assert [i.ref for i in funnel.startable(items, awaiting_review=withheld)] == [
        ticket_b.ref]
    assert funnel.next_ticket_for_tier(
        items, NOW, blocked=set(withheld)).ref == ticket_b.ref


def test_new_head_lifts_the_hold():
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H2, body=_body(marker), verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()


def test_merge_of_main_head_change_does_not_reoffer():
    """A merge-of-main head change is not new work: the gate lifts, but the
    uncovered head is still review work, so begin does not re-offer."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H2, body=_body(marker), verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    blocked = (funnel.awaiting_review(items, pr_facts=pr_facts)
               | funnel.no_push_reoffer_withheld(items, pr_facts))
    assert ticket_a.ref in blocked
    assert [i.ref for i in funnel.startable(items, awaiting_review=blocked)] == [
        ticket_b.ref]


def test_new_verdict_after_marker_rearms():
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V2))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    assert ticket_a.ref in [i.ref for i in funnel.startable(items)]


def test_older_verdict_stays_withheld():
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}


def test_marker_without_any_verdict_stays_withheld():
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=None)}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}


def test_unparseable_marker_reads_as_absent():
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1,
        body=_body("<!-- no-push-finish head=%s at=soon -->" % H1),
        verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()


def test_marker_for_another_head_is_ignored():
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    marker = funnel.render_no_push_finish_marker(H9, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()


def test_closed_and_foreign_prs_are_ignored():
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    closed = _pr_row(2521, H1, body=_body(marker), verdict=_verdict(H1, T_V),
                     state="CLOSED")
    assert funnel.no_push_reoffer_withheld(items, {ticket_a.ref: closed}) == set()
    foreign = _pr_row(2521, H1, body=_body(marker),
                      verdict=_verdict(H1, T_V), isCrossRepository=True)
    assert funnel.no_push_reoffer_withheld(items, {ticket_a.ref: foreign}) == set()


def test_marker_round_trip_and_ordering():
    first = funnel.render_no_push_finish_marker(H1, T_V)
    second = funnel.render_no_push_finish_marker(H1, T_M)
    assert funnel.no_push_finish_markers(
        "noise\n%s\nmore noise\n%s\n" % (second, first)) == [
            (H1, T_V), (H1, T_M)]
    assert funnel.no_push_finish_markers(None) == []
    assert funnel.no_push_finish_markers("no record here") == []


def test_evidence_block_carries_marker_without_disturbing_review():
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    block = implement.render_evidence_block(
        sha=H1, merged=None,
        reproduction={"line": "added one red test", "tests": []},
        repo="owner/repo", prior_fixes=[], no_push_marker=marker)
    assert marker in block
    assert not any(line.strip().startswith("- ") and "no-push" in line
                   for line in block.split("\n"))

    body = "Implements #101.\n\nSummary:\nNothing new.\n\n" + block
    rest, inner = review.split_evidence_block(body)
    assert inner is not None and marker in inner
    assert "no-push" not in rest
    assert review.packet_evidence(inner, H1) != review.EVIDENCE_UNAVAILABLE


def _run_git(*args, cwd=None):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True)


def _make_clone(tmp_path):
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    _run_git("init", "--bare", "--quiet", str(remote))
    _run_git("init", "--quiet", "-b", "main", str(seed))
    _run_git("config", "user.name", "Fixture", cwd=seed)
    _run_git("config", "user.email", "fixture@example.test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _run_git("add", "README.md", cwd=seed)
    _run_git("commit", "--quiet", "-m", "seed", cwd=seed)
    _run_git("remote", "add", "origin", str(remote), cwd=seed)
    _run_git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    _run_git("--git-dir", str(remote), "symbolic-ref", "HEAD",
             "refs/heads/main")
    _run_git("clone", "--quiet", str(remote), str(clone))
    _run_git("config", "user.name", "Fixture", cwd=clone)
    _run_git("config", "user.email", "fixture@example.test", cwd=clone)
    _run_git("switch", "--quiet", "-c", "ticket/42", cwd=clone)
    return remote, clone


def _implement_ticket(number=42, repo="owner/repo"):
    return {
        "ref": "{}#{}".format(repo, number),
        "number": number,
        "title": "do the thing",
        "url": "https://github.com/{}/issues/{}".format(repo, number),
        "body": "Parent: #7.\n\nWhat: do it.",
        "parent": {"number": 7, "title": "plan",
                   "url": "https://github.com/{}/issues/7".format(repo)},
    }


def test_finish_without_new_work_records_the_head(tmp_path, monkeypatch):
    """An earlier run pushed the branch; this run changes nothing, so its
    finish records the head and the gate withholds it."""
    _, clone = _make_clone(tmp_path)
    (clone / "work.txt").write_text("done\n")
    _run_git("add", "work.txt", cwd=clone)
    _run_git("commit", "--quiet", "-m", "the work", cwd=clone)
    _run_git("push", "--quiet", "-u", "origin", "ticket/42", cwd=clone)
    head = _run_git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    monkeypatch.setattr(implement, "_claim_state", lambda ref, run, agent: ("empty", []))
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: _implement_ticket(number, repo))
    bodies = []
    effects = {"released": [], "finished": []}

    result = implement.finish_done(
        {"done": True, "summary": "Still done.", "departures": []},
        run="run-42", repo="owner/repo", cwd=clone,
        test_commands=[[sys.executable, "-c", "pass"]],
        release=effects["released"].append,
        heartbeat_finish=lambda *args: effects["finished"].append(args),
        pr_effect=lambda repo, context, found_ticket, body: (
            bodies.append(body) or {"number": 91, "url": "https://example.test/91"}),
    )

    assert result["number"] == 91
    assert len(bodies) == 1
    markers = funnel.no_push_finish_markers(bodies[0])
    assert [marked_head for marked_head, _ in markers] == [head]


def test_finish_with_new_work_records_no_marker(tmp_path, monkeypatch):
    _, clone = _make_clone(tmp_path)
    (clone / "work.txt").write_text("done\n")
    monkeypatch.setattr(implement, "_claim_state", lambda ref, run, agent: ("empty", []))
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: _implement_ticket(number, repo))
    bodies = []

    implement.finish_done(
        {"done": True, "summary": "Did the work.", "departures": []},
        run="run-42", repo="owner/repo", cwd=clone,
        test_commands=[[sys.executable, "-c", "pass"]],
        release=lambda ref: None,
        heartbeat_finish=lambda *args: None,
        pr_effect=lambda repo, context, found_ticket, body: (
            bodies.append(body) or {"number": 91, "url": "https://example.test/91"}),
    )

    assert len(bodies) == 1
    assert funnel.no_push_finish_markers(bodies[0]) == []
