"""A ticket finished by comments leaves the engineering queue (#498, ticket #510).

Eleven consecutive runs re-claimed #277 on 2026-09-09 and re-verified the same
nine proposal comments, because a done run releases the claim and nothing
recorded that the deliverable was already delivered.

Only the ticket the comments are on is withheld (#1701, ticket #1732): Codex
run e1b3abbcf90a bound #165 but commented on #780, and #165 was withheld from
every begin for two weeks while ``funnel queue`` and the dashboard listed it
as startable.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from funnel import Item  # noqa: E402

REPO = "nateprich/example"
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def _project(number, status="Building", klass="Investigate", repo=REPO,
             children_total=1):
    return Item(repo=repo, number=number, title="Project %d" % number,
                url="https://github.com/%s/issues/%d" % (repo, number),
                state="OPEN", status=status, klass=klass,
                children_total=children_total,
                origin="agent", risk="standard", needs="none")


def _ticket(number, parent, state="OPEN", repo=REPO, open_blockers=()):
    return Item(repo=repo, number=number, title="Ticket %d" % number,
                url="https://github.com/%s/issues/%d" % (repo, number),
                state=state, parent=parent.ref,
                open_blockers=list(open_blockers),
                origin="agent", risk="standard", needs="none",
                item_id="item-%d" % number)


def _wire(monkeypatch, spools):
    monkeypatch.setattr(heartbeat, "PROVIDERS",
                        {"claude": "anthropic", "codex": "openai",
                         "muse": "meta", "zcode": "zai"})
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset({"zcode"}))
    read = []

    def fake_read(agent):
        read.append(agent)
        return list(spools.get(agent, []))

    monkeypatch.setattr(heartbeat, "read", fake_read)
    return read


def _run(run, ticket_ref, outcome, note=None, ts=100, agent="codex"):
    return [
        {"run": run, "agent": agent, "phase": "start", "ts": ts},
        {"run": run, "agent": agent, "phase": "bind", "ts": ts + 1,
         "do": "ticket", "work": ticket_ref},
        {"run": run, "agent": agent, "phase": "finish", "ts": ts + 50,
         "outcome": outcome, "note": note},
    ]


# The URL is on the ticket itself, in its own repo: since #1701 a comments
# finish withholds only the ticket or parent its comments are on.
COMMENTS = ("finished by comments: "
            "https://github.com/nateprich/example/issues/277#issuecomment-1 "
            "... #issuecomment-9; waiting on Nate to close #277")


def test_the_277_shape_is_withheld_on_the_next_fire(monkeypatch):
    """Investigate ticket, comments-only deliverable recorded, no open PR."""
    project = _project(130)
    ticket = _ticket(277, project)
    _wire(monkeypatch, {"codex": _run("r1", ticket.ref, "skipped-human-step", COMMENTS)})

    withheld = funnel.finished_by_comments([project, ticket])

    assert withheld == {ticket.ref}
    assert funnel.startable([project, ticket], awaiting_review=withheld) == []


def test_an_ordinary_finished_ticket_is_still_offerable(monkeypatch):
    project = _project(130)
    ticket = _ticket(278, project)
    _wire(monkeypatch, {"codex": _run("r1", ticket.ref, "done", "PR #12")})

    assert funnel.finished_by_comments([project, ticket]) == set()
    assert [i.ref for i in funnel.startable([project, ticket])] == [ticket.ref]


def test_a_later_finish_another_way_returns_the_ticket(monkeypatch):
    project = _project(130)
    ticket = _ticket(277, project)
    spool = (_run("r1", ticket.ref, "skipped-human-step", COMMENTS, ts=100)
             + _run("r2", ticket.ref, "done", "PR #40", ts=500))
    _wire(monkeypatch, {"codex": spool})

    assert funnel.finished_by_comments([project, ticket]) == set()


def test_a_closed_ticket_is_never_withheld(monkeypatch):
    project = _project(130)
    ticket = _ticket(277, project, state="CLOSED")
    _wire(monkeypatch, {"codex": _run("r1", ticket.ref, "skipped-human-step", COMMENTS)})

    assert funnel.finished_by_comments([project, ticket]) == set()


def test_the_human_step_stop_path_is_not_this_marker(monkeypatch):
    """`skipped-human-step` for a ticket blocked on a human step keeps its
    existing meaning; that ticket is withheld by its `blocked` label, not here."""
    project = _project(130)
    ticket = _ticket(279, project)
    _wire(monkeypatch, {"codex": _run(
        "r1", ticket.ref, "skipped-human-step",
        "stopped: human step filed as #280; ticket blocked; no PR opened")})

    assert funnel.finished_by_comments([project, ticket]) == set()


def test_a_retired_agent_is_never_read_and_the_reader_is_pure_on_failure(monkeypatch):
    project = _project(130)
    ticket = _ticket(277, project)
    read = _wire(monkeypatch, {"zcode": _run("r1", ticket.ref, "skipped-human-step", COMMENTS)})

    assert funnel.finished_by_comments([project, ticket]) == set()
    assert "zcode" not in read

    monkeypatch.setattr(heartbeat, "read", lambda agent: (_ for _ in ()).throw(OSError("no spool")))
    assert funnel.finished_by_comments([project, ticket]) == set()


# #1701: the bind alone is not trusted. A comments finish withholds its bound
# ticket only when one of the note's GitHub URLs is that ticket or its parent.

CC = "nateprich-projects/command-center"
LEAGUE = "nateprich-projects/The-League"


def _comments_on(url):
    return "finished by comments: {}#issuecomment-5; waiting on Nate".format(url)


def _withheld(monkeypatch, items, ticket, note, run="r1"):
    _wire(monkeypatch, {"codex": _run(run, ticket.ref, "skipped-human-step", note)})
    return funnel.finished_by_comments(items)


def test_a_url_on_the_bound_ticket_withholds_it(monkeypatch):
    project = _project(130)
    ticket = _ticket(277, project)
    note = _comments_on("https://github.com/nateprich/example/issues/277")

    assert _withheld(monkeypatch, [project, ticket], ticket, note) == {ticket.ref}


def test_a_url_on_its_parent_in_the_same_repo_withholds_it(monkeypatch):
    """An investigation's findings posted on the plan, as #762 for #766."""
    project = _project(130)
    ticket = _ticket(277, project)
    note = _comments_on("https://github.com/nateprich/example/issues/130")

    assert _withheld(monkeypatch, [project, ticket], ticket, note) == {ticket.ref}


def test_a_url_on_a_cross_repo_parent_withholds_it(monkeypatch):
    """The-League#220's parent is command-center#1054."""
    project = _project(1054, repo=CC)
    ticket = _ticket(220, project, repo=LEAGUE)
    note = _comments_on(
        "https://github.com/nateprich-projects/command-center/issues/1054")

    assert _withheld(monkeypatch, [project, ticket], ticket, note) == {ticket.ref}


def test_a_mixed_case_repo_withholds_it(monkeypatch):
    """The note is lowercased before it is read; the ref keeps its case."""
    project = _project(1054, repo=CC)
    ticket = _ticket(220, project, repo=LEAGUE)
    note = _comments_on(
        "https://github.com/nateprich-projects/The-League/issues/220")

    assert ticket.ref == "nateprich-projects/The-League#220"
    assert _withheld(monkeypatch, [project, ticket], ticket, note) == {ticket.ref}


def test_the_165_record_on_an_unrelated_issue_stays_in_the_queue(
    monkeypatch, capsys,
):
    """Codex run e1b3abbcf90a bound #165 and commented on #780 (2026-09-13)."""
    project = _project(162, repo=CC)
    ticket = _ticket(165, project, repo=CC)
    note = ("finished by comments: https://github.com/nateprich-projects/"
            "command-center/issues/780#issuecomment-5655462110; "
            "waiting on Nate to close #781")

    assert _withheld(monkeypatch, [project, ticket], ticket, note,
                     run="e1b3abbcf90a") == set()
    assert [i.ref for i in funnel.startable([project, ticket])] == [ticket.ref]

    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda _items: {})
    monkeypatch.setattr(funnel, "awaiting_review", lambda _items, pr_facts=None: set())
    calls = []
    original_listing = funnel.startable_listing

    def counted_listing(*args, **kwargs):
        result = original_listing(*args, **kwargs)
        calls.append([item.ref for item in result])
        return result

    monkeypatch.setattr(funnel, "startable_listing", counted_listing)
    assert funnel.cmd_queue([project, ticket], NOW, pr_facts={}) == 0
    output = capsys.readouterr().out
    assert "Startable by Codex (1)" in output
    assert ticket.ref in output
    assert calls == [[ticket.ref]]


def test_the_same_number_in_another_repo_does_not_withhold(monkeypatch):
    project = _project(130)
    ticket = _ticket(277, project)
    for url in ("https://github.com/nateprich/other/issues/277",
                "https://github.com/someone/example/issues/277",
                "https://github.com/nateprich/example-two/issues/277",
                "https://github.com/nateprich/example/issues/2770",
                "https://github.com/nateprich/other/issues/130"):
        note = _comments_on(url)
        assert _withheld(monkeypatch, [project, ticket], ticket, note) == set(), url


def test_a_later_finish_on_an_unrelated_issue_releases_an_earlier_hold(monkeypatch):
    """A finish that fails the check is a non-marker finish, and the latest
    finish decides, so it returns the ticket like any other finish would."""
    project = _project(130)
    ticket = _ticket(277, project)
    spool = (
        _run("r1", ticket.ref, "skipped-human-step",
             _comments_on("https://github.com/nateprich/example/issues/277"),
             ts=100)
        + _run("r2", ticket.ref, "skipped-human-step",
               _comments_on("https://github.com/nateprich/example/issues/780"),
               ts=500)
    )
    _wire(monkeypatch, {"codex": spool})

    assert funnel.finished_by_comments([project, ticket]) == set()


def test_the_run_that_finished_it_is_named(monkeypatch):
    project = _project(130)
    ticket = _ticket(277, project)
    _wire(monkeypatch, {"codex": _run("e1b3abbcf90a", ticket.ref,
                                      "skipped-human-step", COMMENTS)})

    assert funnel.finished_by_comments_runs([project, ticket]) == {
        ticket.ref: "e1b3abbcf90a"}


def _held_board(monkeypatch):
    project = _project(130, children_total=3)
    held = _ticket(277, project)
    free = _ticket(278, project)
    after = _ticket(279, project, open_blockers=[held.ref])
    _wire(monkeypatch, {"codex": _run("e1b3abbcf90a", held.ref,
                                      "skipped-human-step", COMMENTS)})
    return [project, held, free, after], held, free, after


def test_the_queue_lists_a_withheld_ticket_as_withheld_not_startable(
    monkeypatch, capsys
):
    items, held, free, _after = _held_board(monkeypatch)
    monkeypatch.setattr(
        funnel, "awaiting_review", lambda _items, pr_facts=None: set())

    assert funnel.cmd_queue(items, NOW, pr_facts={}) == 0
    out = capsys.readouterr().out
    startable, rest = out.split("\nWithheld — finished by comments", 1)

    assert "Startable by Codex (1)" in startable
    assert free.ref in startable
    assert held.ref not in startable
    assert rest.startswith(", waiting on Nate to close (1):")
    withheld_line = next(line for line in rest.splitlines() if held.ref in line)
    assert "run e1b3abbcf90a" in withheld_line


def test_the_projected_pull_order_gives_it_no_turn(monkeypatch):
    items, held, free, after = _held_board(monkeypatch)

    assert funnel.projected_pull_order(items, NOW) == [held.ref, free.ref, after.ref]
    assert funnel.projected_pull_order(
        items, NOW, finished=funnel.finished_by_comments(items)) == [free.ref]


def test_the_dashboard_gives_it_no_projected_turn(monkeypatch):
    items, held, free, after = _held_board(monkeypatch)

    board = funnel.dashboard_board(items, NOW)
    rows = {
        ticket["ref"]: ticket
        for column in board["columns"]
        for project in column["items"]
        for ticket in project["tickets"]
    }

    assert rows[held.ref]["finished_by_comments"]
    assert rows[held.ref]["projected_turn"] is None
    assert rows[after.ref]["projected_turn"] is None
    assert rows[free.ref]["projected_turn"] == 0
