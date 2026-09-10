"""A ticket finished by comments leaves the engineering queue (#498, ticket #510).

Eleven consecutive runs re-claimed #277 on 2026-09-09 and re-verified the same
nine proposal comments, because a done run releases the claim and nothing
recorded that the deliverable was already delivered.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402
from funnel import Item  # noqa: E402

REPO = "nateprich/example"


def _project(number, status="Building", klass="Investigate"):
    return Item(repo=REPO, number=number, title="Project %d" % number,
                url="https://github.com/%s/issues/%d" % (REPO, number),
                state="OPEN", status=status, klass=klass, children_total=1)


def _ticket(number, parent, state="OPEN"):
    return Item(repo=REPO, number=number, title="Ticket %d" % number,
                url="https://github.com/%s/issues/%d" % (REPO, number),
                state=state, body="Risk: standard", parent=parent.ref,
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


COMMENTS = ("finished by comments: https://github.com/x/issues/277#issuecomment-1 "
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


def test_the_engineer_routine_records_the_no_pr_finish():
    text = " ".join((ROOT / "routines" / "codex-work.md").read_text(encoding="utf-8").split()).lower()
    assert "finished by comments:" in text
    assert "--outcome skipped-human-step --note \"finished by comments:" in text
    assert "waiting on nate to close #<current-number>" in text
    assert "do not close the ticket" in text
    assert funnel.COMMENTS_DELIVERABLE_PREFIX in text
