"""The #794 frozen-ground rule withholds tickets from the queue (#1176).

A ticket whose What or Accept names frozen ground — a path under routines/
or skills/, or a doomed parser constant — under a non-exempt parent is not
startable while the freeze governs. Offering it would only fail at review,
on a diff that does not exist until a run has already built it. Withheld
tickets surface with the marker and the parent named, never silently.
"""

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
from engine import review as engine_review  # noqa: E402
import usage  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
REPO = "nateprich-projects/command-center"


def project(number, status="Building", klass="Improve", **kw) -> funnel.Item:
    kw.setdefault("repo", REPO)
    kw.setdefault("title", "project {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("status_since", NOW - timedelta(days=1))
    kw.setdefault("children_total", 1)
    return funnel.Item(number=number, status=status, klass=klass, **kw)


def ticket(number, parent, body, **kw) -> funnel.Item:
    kw.setdefault("repo", REPO)
    kw.setdefault("title", "ticket {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    return funnel.Item(
        number=number, body=body, parent="{}#{}".format(REPO, parent), **kw
    )


def body(parent, what, accept="It works.", risk="standard",
         sequencing=None) -> str:
    text = "Parent: #{!s}.\n\nWhat: {!s}.\n\nAccept: {!s}".format(
        parent, what, accept)
    if sequencing is not None:
        text += "\n\nSequencing: {}".format(sequencing)
    return text + "\n\nRisk: {}".format(risk)


# The two hand-held tickets from the #1164 report, as fixtures. Both were
# startable with correct bodies and no machine-readable freeze signal; both
# read as withheld under this rule without their manual blocked labels.
def ticket_1142_shape(**kw) -> funnel.Item:
    return ticket(
        1142, 1135,
        body(1135, "rewrite section 7 of routines/muse.md",
             "section 7 of routines/muse.md reads clean"),
        **kw,
    )


def ticket_1139_shape(**kw) -> funnel.Item:
    return ticket(
        1139, 1126,
        body(1126, "retire the judgement copy",
             "skills/shape/SKILL.md no longer carries the retired section"),
        **kw,
    )


# -- startable() withholds frozen tickets ------------------------------------


def test_1142_shape_is_withheld_while_the_freeze_governs():
    items = [project(1135), ticket_1142_shape()]
    assert funnel.startable(items) == []
    rows = funnel.freeze_withheld(items)
    assert [found["ref"] for found in rows] == ["{}#1142".format(REPO)]
    assert rows[0]["markers"] == ["routines/muse.md"]
    assert rows[0]["parent"] == 1135
    assert "routines/muse.md" in rows[0]["reason"]
    assert "#1135" in rows[0]["reason"]


def test_1139_shape_is_withheld_on_an_accept_marker():
    items = [project(1126), ticket_1139_shape()]
    assert funnel.startable(items) == []
    rows = funnel.freeze_withheld(items)
    assert rows[0]["markers"] == ["skills/shape/SKILL.md"]
    assert rows[0]["parent"] == 1126


def test_routines_claude_md_in_what_withholds():
    mine = ticket(1143, 1135, body(1135, "reword routines/claude.md"))
    items = [project(1135), mine]
    assert funnel.startable(items) == []
    assert funnel.freeze_markers(mine.body) == ["routines/claude.md"]


@pytest.mark.parametrize("parent", [794, 1044])
def test_exempt_parents_stay_startable_on_the_same_body(parent):
    mine = ticket(1142, parent,
                  body(parent, "rewrite section 7 of routines/muse.md",
                       "section 7 of routines/muse.md reads clean"))
    items = [project(parent), mine]
    assert funnel.startable(items) == [mine]
    assert funnel.freeze_withheld(items) == []


@pytest.mark.parametrize("name", list(engine_review.FROZEN_PARSERS))
def test_each_engine_parser_name_withholds(name):
    mine = ticket(1143, 1135, body(1135, "delete the {} parser".format(name)))
    items = [project(1135), mine]
    assert funnel.startable(items) == []
    assert funnel.freeze_markers(mine.body) == [name]


def test_a_ticket_with_no_marker_stays_startable():
    mine = ticket(1143, 1135, body(1135, "fix the retry backoff"))
    items = [project(1135), mine]
    assert funnel.startable(items) == [mine]
    assert funnel.freeze_withheld(items) == []


def test_a_marker_only_in_sequencing_is_context_not_work():
    mine = ticket(
        1143, 1135,
        body(1135, "fix the review queue",
             sequencing="routines/muse.md stays as the rollback "
                        "until the replay rejects"),
    )
    items = [project(1135), mine]
    assert funnel.startable(items) == [mine]


def test_a_marker_only_in_the_title_is_not_ticket_text():
    mine = ticket(1143, 1135, body(1135, "fix the review queue"),
                  title="touch routines/muse.md")
    items = [project(1135), mine]
    assert funnel.startable(items) == [mine]


# -- the mirror is pinned to the shared source, not a third list -------------


def test_freeze_mirror_matches_the_engine_lists():
    """The queue predicate mirrors engine/review.py; this test is the update
    mechanism. The package dependency runs one way only, so funnel.py cannot
    read the canonical lists at run time — but if they move, this fails until
    the mirror moves with them. The two can never drift silently, and #794
    closeout must delete both together."""
    assert funnel.FROZEN_GROUND_PATHS == tuple(engine_review.FROZEN_PATHS)
    assert funnel.FROZEN_GROUND_PARSERS == tuple(engine_review.FROZEN_PARSERS)
    assert (funnel.FROZEN_GROUND_EXEMPT_PARENTS
            == tuple(engine_review.FREEZE_PARENT_NUMBERS))


# -- the predicate goes inert with the freeze --------------------------------


def test_a_closed_794_releases_frozen_tickets():
    owner = project(794, children_total=0, state="CLOSED")
    items = [owner, project(1135), ticket_1142_shape()]
    by_ref = {item.ref: item for item in items}
    assert funnel.freeze_active(by_ref) is False
    assert funnel.startable(items) == [items[2]]
    assert funnel.freeze_withheld(items) == []


def test_a_closed_1044_does_not_end_the_freeze():
    witness = project(1044, children_total=0, state="CLOSED")
    items = [witness, project(1135), ticket_1142_shape()]
    assert funnel.startable(items) == []
    assert [row["ref"] for row in funnel.freeze_withheld(items)] == [
        "{}#1142".format(REPO)]


def test_an_open_794_keeps_the_freeze_governing():
    owner = project(794, children_total=0)
    items = [owner, project(1135), ticket_1142_shape()]
    assert funnel.startable(items) == []


def test_a_landed_freeze_releases_parser_tickets_too():
    owner = project(794, children_total=0, state="CLOSED")
    mine = ticket(1143, 1135,
                  body(1135, "delete the PROSE_DEPENDENCY_RE parser"))
    items = [owner, project(1135), mine]
    assert funnel.startable(items) == [mine]


# -- no tier may be offered a frozen ticket -----------------------------------


@pytest.mark.parametrize("tier", [None, "standard", "escalated"])
def test_no_tier_is_offered_a_frozen_ticket(tier):
    items = [project(1135), ticket_1142_shape()]
    assert funnel.next_ticket_for_tier(items, NOW, tier=tier) is None


def test_a_clean_ticket_is_offered_past_a_frozen_one():
    frozen = ticket_1142_shape()
    clean = ticket(1143, 1135, body(1135, "fix the retry backoff"))
    items = [project(1135), frozen, clean]
    assert funnel.next_ticket_for_tier(items, NOW, tier="standard") == clean
    assert funnel.next_ticket_for_tier(items, NOW) == clean


# -- the queue names what it withholds ----------------------------------------


def test_queue_lists_frozen_tickets_as_withheld_with_reason(capsys):
    frozen = ticket_1142_shape()
    clean = ticket(1143, 1135, body(1135, "fix the retry backoff"))
    items = [project(1135), frozen, clean]
    assert funnel.cmd_queue(items, NOW) == 0
    output = capsys.readouterr().out
    startable, withheld = output.split("Withheld by frozen ground (1):", 1)
    assert "{}#1143".format(REPO) in startable
    assert "{}#1142".format(REPO) not in startable
    assert "{}#1142".format(REPO) in withheld
    assert "routines/muse.md" in withheld
    assert "#1135" in withheld


def test_queue_omits_the_frozen_section_when_nothing_is_frozen(capsys):
    clean = ticket(1143, 1135, body(1135, "fix the retry backoff"))
    assert funnel.cmd_queue([project(1135), clean], NOW) == 0
    assert "Withheld by frozen ground" not in capsys.readouterr().out


def test_next_names_the_freeze_when_it_is_all_that_waits(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "finished_by_comments", lambda items: set())
    items = [project(1135), ticket_1142_shape()]
    assert funnel.cmd_next(items, NOW, pr_facts={}) == 1
    err = capsys.readouterr().err
    assert "tickets withheld by frozen ground" in err
    assert "routines/muse.md" in err
    assert "#1135" in err


def _allow_begin(monkeypatch):
    funnel.reset_api_usage()

    def run(argv, **kwargs):
        if argv[:3] == ["gh", "api", "graphql"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {
                        "rateLimit": {
                            "cost": 1,
                            "remaining": 5_000,
                            "resetAt": "later",
                        },
                    },
                }),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="run-id\n", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)
    monkeypatch.setattr(
        usage, "read_agent", lambda agent, timestamp: {"windows": {}})
    monkeypatch.setattr(
        usage, "pace",
        lambda reading, timestamp, provider: {"over_pace": False})
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda rows: {})


def test_begin_reports_frozen_tickets_instead_of_offering_one(
    monkeypatch, capsys
):
    import heartbeat

    _allow_begin(monkeypatch)
    monkeypatch.setattr(
        heartbeat, "record_binding", lambda *args, **kwargs: "pushed")
    monkeypatch.setattr(funnel, "finished_by_comments", lambda items: set())
    monkeypatch.setattr(
        funnel, "reconcile_orphaned_starts", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        funnel, "reconcile_abandoned_claims", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        funnel, "reconcile_approved_merges", lambda *args: [])
    monkeypatch.setattr(funnel, "awaiting_review", lambda rows: set())
    monkeypatch.setattr(
        funnel, "implementation_packet",
        lambda repo, number, agent: {
            "repo": repo,
            "ticket": {"number": number},
            "plan": None,
            "verdict": {"blocking": []},
            "prior_run": None,
        },
    )
    monkeypatch.setattr(
        funnel, "read_lock", lambda item: item.in_motion_since)
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock",
        lambda item, value: writes.append((item.ref, value)))

    items = [project(1135), ticket_1142_shape()]
    assert funnel.cmd_begin(items, NOW, "codex", "standard", False) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["do"] == "stop"
    assert "tickets withheld by frozen ground" in result["why"]
    assert "routines/muse.md" in result["why"]
    assert "#1135" in result["why"]
    assert result["freeze_withheld"] == [{
        "ref": "{}#1142".format(REPO),
        "repo": REPO,
        "parent": 1135,
        "markers": ["routines/muse.md"],
        "reason": result["freeze_withheld"][0]["reason"],
    }]
    assert writes == []
