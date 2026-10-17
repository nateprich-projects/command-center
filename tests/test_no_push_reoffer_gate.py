"""Gate implement re-offer on a same-head open PR until a new verdict (#2541).

The Muse implement lane re-bound #2492 seven times noting PR #2521 with no
PR commit between finishes while other tickets sat startable. A rejected
head hands back to the engineer once; a finish that pushes nothing records
its head in the PR body it rewrites, and the shared offer path skips that
marked head: only a new head, or the #2618 stranded-head re-arm as review
work, releases it — a fresh verdict on the same head does not. No local
state: record, head, and verdict all come from the PR snapshot.
"""

from __future__ import annotations

import json
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


def test_no_push_same_head_rearms_as_review_work():
    """The #2492/PR #2521 replay under the #2618 re-arm: one bind, then
    review work. The gate releases the stranded head, but the ticket
    stays out of implement's reach as review work, so no second
    done-with-no-push happens without a fresh verdict in between; the
    other startable ticket is offered."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    blocked = (funnel.awaiting_review(items, pr_facts=pr_facts)
               | funnel.no_push_reoffer_withheld(items, pr_facts))
    assert ticket_a.ref in blocked
    assert [i.ref for i in funnel.startable(items, awaiting_review=blocked)] == [
        ticket_b.ref]
    assert funnel.next_ticket_for_tier(
        items, NOW, blocked=set(blocked)).ref == ticket_b.ref


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


def test_fresh_verdict_after_marker_stays_withheld():
    """#2618 fixture: a fresh rejected verdict on the same marked head
    stays gate-withheld. The ticket is not re-offered to implement; only
    a new head, or the stranded-head re-arm, releases it."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V2))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}
    blocked = (funnel.awaiting_review(items, pr_facts=pr_facts)
               | funnel.no_push_reoffer_withheld(items, pr_facts))
    assert [i.ref for i in funnel.startable(items, awaiting_review=blocked)] == [
        ticket_b.ref]


def test_older_verdict_rearms_as_review_work():
    """A rejected cover older than the marker re-arms (#2618): the gate
    releases the stranded head into the review set instead of holding it
    on every lane."""
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    marker = funnel.render_no_push_finish_marker(H1, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H1, body=_body(marker), verdict=_verdict(H1, T_V))}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    assert ticket_a.ref in funnel.awaiting_review(
        items, pr_facts=pr_facts)


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


def _rearm_row(number, head, ticket_number, verdict_at=T_V, marker_at=T_M,
               verdict_kind="rejected"):
    """One stranded-head PR row: rejected cover plus a newer no-push record."""
    if verdict_kind == "rejected":
        verdict = _verdict(head, verdict_at)
    else:
        verdict = {"verdict": "approved", "head_sha": head,
                   "comment_created_at": verdict_at, "blocking": []}
    return _pr_row(number, head,
                   body=_body(funnel.render_no_push_finish_marker(
                       head, marker_at)),
                   verdict=verdict,
                   headRefName="ticket/{}".format(ticket_number))


def test_rearm_releases_stranded_head_as_review_work():
    """#2618 reproduction: rejected cover plus a newer marker re-arms."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    pr_facts = {ticket_a.ref: _rearm_row(2521, H1, 101)}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    assert ticket_a.ref in funnel.awaiting_review(
        items, pr_facts=pr_facts)
    standing = funnel.review_standing(
        REPO, pr_facts[ticket_a.ref], _verdict(H1, T_V), [])
    assert standing.state == "reviewable"


def test_fresh_verdict_keeps_ordinary_review_shape():
    """A verdict newer than the marker never arms the re-arm path.

    The gate withholds the marked head (#2618), the rejected hand-back
    still keeps the ticket out of review, and the head still reads
    covered: only a marker newer than the verdict re-arms.
    """
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    pr_facts = {ticket_a.ref: _rearm_row(
        2521, H1, 101, verdict_at=T_V2, marker_at=T_M)}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}
    assert ticket_a.ref not in funnel.awaiting_review(
        items, pr_facts=pr_facts)
    standing = funnel.review_standing(
        REPO, pr_facts[ticket_a.ref], _verdict(H1, T_V2), [])
    assert standing.state == "covered"


def test_fresh_approval_after_marker_stays_out_of_implement():
    """A fresh approval newer than the marker stays out of implement: the
    gate withholds the marked head and the ticket stays review-side work
    through the ordinary approved path."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    pr_facts = {ticket_a.ref: _rearm_row(
        2521, H1, 101, verdict_at=T_V2, marker_at=T_M,
        verdict_kind="approved")}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}
    blocked = (funnel.awaiting_review(items, pr_facts=pr_facts)
               | funnel.no_push_reoffer_withheld(items, pr_facts))
    assert ticket_a.ref in blocked
    assert [i.ref for i in funnel.startable(
        items, awaiting_review=blocked)] == [ticket_b.ref]


def test_rearm_requires_rejection():
    """An approved cover with a newer marker stays withheld: the re-arm
    releases only rejected heads, never an approval waiting to merge."""
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    pr_facts = {ticket_a.ref: _rearm_row(
        2521, H1, 101, verdict_kind="approved")}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}
    standing = funnel.review_standing(
        REPO, pr_facts[ticket_a.ref],
        {"verdict": "approved", "head_sha": H1,
         "comment_created_at": T_V, "blocking": []}, [])
    assert standing.state == "covered"


def test_rearm_requires_same_head_cover():
    """A verdict on an older head plus a marker on the current head stays
    withheld: uncovered heads are review work through the ordinary path."""
    project, ticket_a, ticket_b = _board()
    items = [project, ticket_a, ticket_b]
    marker = funnel.render_no_push_finish_marker(H2, T_M)
    pr_facts = {ticket_a.ref: _pr_row(
        2521, H2, body=_body(marker), verdict=_verdict(H1, T_V),
        headRefName="ticket/101")}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {ticket_a.ref}
    assert ticket_a.ref in funnel.awaiting_review(
        items, pr_facts=pr_facts)


def _lane_review_comment(at):
    payload = {"verdict": "rejected", "head_sha": H1,
               "blocking": ["requirement unmet: the widget stays dark"]}
    return {"author": {"login": "nateprich"},
            "body": (funnel.REVIEW_MARKER + "\n\n```json\n"
                     + json.dumps(payload) + "\n```"),
            "createdAt": at}


def test_rearm_respects_review_cap():
    """A stranded head past three lane reviews stays capped: the re-arm
    frees the no-push gate but the #2550 stop still routes the ticket
    instead of requesting a fourth review."""
    project, ticket_a, _ = _board()
    items = [project, ticket_a]
    comments = [_lane_review_comment("2026-10-10T0%d:00:00Z" % hour)
                for hour in range(3)]
    row = _rearm_row(2521, H1, 101)
    row["comments"] = comments
    pr_facts = {ticket_a.ref: row}

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == set()
    assert ticket_a.ref in funnel.awaiting_review(
        items, pr_facts=pr_facts)
    standing = funnel.review_standing(
        REPO, pr_facts[ticket_a.ref], _verdict(H1, T_V), comments)
    assert standing.state == "capped"


def test_rearm_releases_six_ticket_hold_without_requeue():
    """Read-only replay of the 2026-10-17 04:25 PDT hold: six stranded
    tickets plus nine startable waiting. The six release as review work
    while a fresh-verdict head stays gate-withheld and ordinary rejected
    heads stay out of the review set."""
    project = _project()
    stranded = [_ticket(number, project) for number in range(101, 107)]
    waiting = [_ticket(number, project) for number in range(107, 116)]
    handback = _ticket(116, project)
    fresh = _ticket(117, project)
    items = [project] + stranded + waiting + [handback, fresh]
    pr_facts = {}
    for ticket in stranded:
        head = "{:040x}".format(0xa0 + ticket.number)
        pr_facts[ticket.ref] = _rearm_row(
            2500 + ticket.number, head, ticket.number)
    pr_facts[handback.ref] = _pr_row(
        2516, H2, verdict=_verdict(H2, T_V), headRefName="ticket/116")
    pr_facts[fresh.ref] = _rearm_row(
        2517, H9, 117, verdict_at=T_V2, marker_at=T_M)

    assert funnel.no_push_reoffer_withheld(items, pr_facts) == {fresh.ref}
    in_review = funnel.awaiting_review(items, pr_facts=pr_facts)
    assert in_review == {ticket.ref for ticket in stranded}
    blocked = in_review | funnel.no_push_reoffer_withheld(items, pr_facts)
    offered = [i.ref for i in funnel.startable(
        items, awaiting_review=blocked)]
    for ticket in stranded + [fresh]:
        assert ticket.ref not in offered
    for ticket in waiting + [handback]:
        assert ticket.ref in offered


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
