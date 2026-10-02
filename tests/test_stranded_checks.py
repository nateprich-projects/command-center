"""stranded_items stops listing event waits and unread blocks (#2135).

``unclearable_block`` accepted references, native edges and dates as lifting
conditions but not a well-formed ``**Blocked until event:**`` spec, although
``clear_satisfied_blocks`` lifts those (#1451). So a #1403-shape event wait
was listed as "blocked with no condition that can clear it", and so was a
Needs-agent block whose comments could not be read (#1417's dry window): its
condition is unknown, not absent. Both now read their condition through
``block_condition`` (#2134).

Each stranded reason is also its own named check, composed in the order and
wording the brief has always used. Expected strings are written out here,
not read from the code under test.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
REPO = "owner/repo"
UNREAD = "could not read comments"

#: Block comments count only from the owner account (#1788).
OWNER = {"login": "nateprich"}

EVENT = {
    "agent": "codex",
    "job": "command-center-tickets-hourly",
    "outcome": "errored",
    "after": "2026-09-22T00:00:00Z",
}

#: #1403's settled wait: hold until the next genuine daily failure.
EVENT_COMMENT = '''**Blocked until event:**
```json
{
  "agent": "codex",
  "job": "command-center-tickets-hourly",
  "outcome": "errored",
  "after": "2026-09-22T00:00:00Z"
}
```
Wait for the next genuine daily failure.
'''


def issue(number, **kwargs):
    values = {
        "repo": REPO,
        "number": number,
        "title": "issue {}".format(number),
        "url": "https://example.invalid/{}".format(number),
        "state": "OPEN",
        "origin": "agent",
        "risk": "standard",
        "needs": "none",
    }
    values.update(kwargs)
    return Item(**values)


def building(number, **kwargs):
    values = {"status": "Building", "klass": "Broken", "children_total": 2}
    values.update(kwargs)
    return issue(number, **values)


def ticket(number, project, **kwargs):
    return issue(number, parent=project.ref, **kwargs)


def facts(rows, pr_facts=None):
    return funnel._strand_facts(list(rows), NOW, pr_facts)


def approved_conflicting(head="head-sha", reviewed="head-sha"):
    return {
        "state": "OPEN",
        "mergeable": "CONFLICTING",
        "headRefOid": head,
        "verdict": {"verdict": "approved", "head_sha": reviewed},
    }


# Reproduction: the two blocks this ticket stops calling unclearable.


def test_event_wait_and_unread_agent_block_are_not_stranded():
    """Both were listed as unclearable on main before #2135."""
    project = building(100)
    event_wait = ticket(
        101, project, labels=["blocked"], needs="external-event",
        block_reason="Wait for the next genuine daily failure.",
        block_event=dict(EVENT),
    )
    unread = ticket(
        102, project, labels=["blocked"], needs="agent",
        block_comments_error=UNREAD,
    )

    assert funnel.stranded_items([project, event_wait, unread], NOW) == []
    assert not funnel.unclearable_block(event_wait)
    assert not funnel.unclearable_block(unread)


def test_doctor_lists_event_and_unread_blocks_as_still_waiting():
    """``check_block_conditions`` follows ``unclearable_block``."""
    project = building(110)
    event_wait = ticket(
        111, project, labels=["blocked"], needs="external-event",
        block_reason="Wait for the next genuine daily failure.",
        block_event=dict(EVENT),
    )
    unread = ticket(
        112, project, labels=["blocked"], needs="agent",
        block_comments_error=UNREAD,
    )

    result = funnel.check_block_conditions(
        [project, event_wait, unread], now=NOW)

    assert result.ok
    assert result.found.splitlines() == [
        "owner/repo#111: still-waiting (until event agent=codex "
        "job=command-center-tickets-hourly outcome=errored "
        "after=2026-09-22T00:00:00Z)",
        "owner/repo#112: still-waiting (block comments unread)",
    ]


def test_event_wait_on_an_agent_the_clear_never_reads_stays_stranded():
    """The clear reads heartbeats only for its own providers.

    An event naming any other agent matches no records, so nothing can ever
    lift it and ``gate_question`` asks nothing: it is still stranded.
    """
    project = building(120)
    unread_agent = dict(EVENT, agent="example-nightly-job")
    wait = ticket(
        121, project, labels=["blocked"], needs="external-event",
        block_reason="Wait for the example job to fail.",
        block_event=unread_agent,
    )
    reason = (
        "blocked with no condition that can clear it, and no one is asked "
        "(Needs: external-event)"
    )

    assert funnel.block_condition(wait) == "event"
    assert funnel.gate_question(wait) is None
    assert funnel.unclearable_block(wait)
    assert funnel.stranded_items([project, wait], NOW) == [{
        "ref": wait.ref,
        "title": "issue 121",
        "url": "https://example.invalid/121",
        "reason": reason,
    }]
    result = funnel.check_block_conditions([project, wait], now=NOW)
    assert not result.ok
    assert result.found == "owner/repo#121: stranded: " + reason


def test_agent_block_with_only_a_parsed_reason_stays_stranded(monkeypatch):
    """A read comment with a reason and no date, reference or event (#1432)."""
    project = building(130)
    blocked = ticket(131, project, labels=["blocked"], needs="agent")
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"comments": [
            {"author": OWNER, "body": "**Blocked:** Waiting on a person."},
        ]},
    )

    funnel._load_block_comment(blocked)

    assert blocked.block_comments_error is None
    assert blocked.block_reason == "Waiting on a person."
    assert blocked.block_references == []
    assert blocked.blocked_until is None
    assert blocked.block_event is None
    assert funnel.stranded_items([project, blocked], NOW) == [{
        "ref": blocked.ref,
        "title": "issue 131",
        "url": "https://example.invalid/131",
        "reason": "blocked with no condition that can clear it, and no one "
                  "is asked (Needs: agent)",
    }]


# Replays of the two live shapes, through the real comment loader.


def test_replay_1403_settled_event_wait_is_not_stranded(monkeypatch):
    """#1403: a settled wait for the next daily failure, parsed from GitHub."""
    project = building(200, status="Ready")
    wait = ticket(230, project, labels=["blocked"], needs="external-event")
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"comments": [
            {"author": OWNER, "body": EVENT_COMMENT},
        ]},
    )

    funnel._load_block_comment(wait)

    assert wait.block_event == EVENT
    assert funnel.block_condition(wait) == "event"
    assert funnel.gate_question(wait) is None
    assert funnel.stranded_items([project, wait], NOW) == []


def test_replay_1417_unread_agent_block_is_not_stranded(monkeypatch):
    """#1417: the GraphQL window ran dry and the block comment went unread."""
    project = building(300)
    blocked = ticket(301, project, labels=["blocked"], needs="agent")
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: None)

    funnel._load_block_comment(blocked)

    assert blocked.block_comments_error == UNREAD
    assert funnel.block_condition(blocked) == "unread"
    assert funnel.stranded_items([project, blocked], NOW) == []


def test_brief_stranded_section_omits_event_and_unread_blocks(
    monkeypatch, capsys
):
    """The brief's ``stranded`` list is where the false rows were seen."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", lambda items, now: None
    )
    monkeypatch.setattr(
        funnel, "decline_routing_metric",
        lambda items, now: {"status": "available", "declines": 0},
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": []})
    project = building(400)
    event_wait = ticket(
        401, project, labels=["blocked"], needs="external-event",
        block_reason="Wait for the next genuine daily failure.",
        block_event=dict(EVENT),
    )
    unread = ticket(
        402, project, labels=["blocked"], needs="agent",
        block_comments_error=UNREAD,
    )
    silent = ticket(403, project, labels=["blocked"], needs="agent")

    try:
        assert funnel.cmd_brief([project, event_wait, unread, silent], NOW) == 0
    finally:
        funnel.reset_api_usage()
    brief = json.loads(capsys.readouterr().out)

    assert brief["stranded"] == [{
        "ref": silent.ref,
        "title": "issue 403",
        "url": "https://example.invalid/403",
        "reason": "blocked with no condition that can clear it, and no one "
                  "is asked (Needs: agent)",
    }]


# One test per named check.


def test_open_pr_on_closed_ticket():
    project = building(500)
    closed = ticket(501, project, state="CLOSED")
    closed_project = issue(502, state="CLOSED", status="Done")
    open_ticket = ticket(503, project)
    pr = {"state": "OPEN", "number": 9}
    known = facts(
        [project, closed, closed_project, open_ticket],
        {closed.ref: pr, closed_project.ref: pr, open_ticket.ref: pr},
    )

    check = funnel._strand_open_pr_on_closed_ticket
    assert check(closed, known) == "open PR on closed ticket"
    # A project's PR is not a ticket's, and an open ticket is the next check.
    assert check(closed_project, known) is None
    assert check(open_ticket, known) is None
    merged = facts([closed], {closed.ref: {"state": "MERGED"}})
    assert check(closed, merged) is None
    assert check(closed, facts([closed])) is None


def test_open_pr_under_a_project_that_is_not_building():
    ready = issue(510, status="Ready", children_total=1)
    unset = issue(511, children_total=1)
    live = building(512)
    under_ready = ticket(513, ready)
    under_unset = ticket(514, unset)
    under_live = ticket(515, live)
    orphan = issue(516, parent="{}#999".format(REPO))
    pr = {"state": "OPEN", "number": 9}
    rows = [ready, unset, live, under_ready, under_unset, under_live, orphan]
    known = facts(rows, {
        row.ref: pr for row in (under_ready, under_unset, under_live, orphan)
    })

    check = funnel._strand_pr_under_non_building_project
    assert check(under_ready, known) == (
        "open PR on open ticket whose project Status is Ready; "
        "merge gate will refuse it"
    )
    assert check(under_unset, known) == (
        "open PR on open ticket whose project Status is unset; "
        "merge gate will refuse it"
    )
    assert check(under_live, known) is None
    # A project that is not loaded is not evidence of anything.
    assert check(orphan, known) is None
    assert check(under_ready, facts(rows)) is None


def test_approved_verdict_on_a_conflicting_head():
    project = building(520)
    work = ticket(521, project)
    check = funnel._strand_approved_conflicting_head

    assert check(work, facts([project, work], {
        work.ref: approved_conflicting(),
    })) == "approved verdict against an unmergeable branch"
    # A moved head waits for a fresh review rather than being stranded.
    assert check(work, facts([project, work], {
        work.ref: approved_conflicting(head="new", reviewed="old"),
    })) is None
    mergeable = dict(approved_conflicting(), mergeable="MERGEABLE")
    assert check(work, facts([project, work], {work.ref: mergeable})) is None
    rejected = dict(approved_conflicting(), verdict={"verdict": "rejected"})
    assert check(work, facts([project, work], {work.ref: rejected})) is None


def test_stale_claim_with_no_pr():
    project = building(530)
    claimed = ticket(
        531, project,
        in_motion_since=NOW - funnel.LOCK_TTL - timedelta(minutes=1),
    )
    fresh = ticket(532, project, in_motion_since=NOW - timedelta(minutes=5))
    rows = [project, claimed, fresh]
    check = funnel._strand_stale_claim

    # No PR lookups requested: a stale claim is treated as having no PR.
    assert check(claimed, facts(rows)) == "claim past its TTL with no PR"
    assert check(claimed, facts(rows, {claimed.ref: None})) == (
        "claim past its TTL with no PR"
    )
    # A fact that was not fetched is not a known absence.
    assert check(claimed, facts(rows, {})) is None
    assert check(claimed, facts(rows, {
        claimed.ref: {"state": "OPEN", "number": 9},
    })) is None
    assert check(fresh, facts(rows, {fresh.ref: None})) is None


def test_childless_building_project():
    childless = issue(540, status="Building")
    with_tickets = building(541)
    building_ticket = ticket(542, with_tickets, status="Building")
    rows = [childless, with_tickets, building_ticket]
    check = funnel._strand_childless_building_project

    assert check(childless, facts(rows)) == "Building project has no tickets"
    assert check(with_tickets, facts(rows)) is None
    assert check(building_ticket, facts(rows)) is None


def test_closed_parent_that_is_not_parked():
    """#1169: no gate watches it; a Parked parent is a decision (#1212)."""
    done = issue(550, state="CLOSED", status="Done", children_total=1)
    parked = issue(551, state="CLOSED", status="Parked", children_total=1)
    unset = issue(552, state="CLOSED", children_total=1)
    live = building(553)
    under_done = ticket(554, done)
    under_parked = ticket(555, parked)
    under_unset = ticket(556, unset)
    under_live = ticket(557, live)
    rows = [done, parked, unset, live,
            under_done, under_parked, under_unset, under_live]
    check = funnel._strand_closed_parent

    assert check(under_done, facts(rows)) == (
        "parent owner/repo#550 is closed with Status Done"
    )
    assert check(under_parked, facts(rows)) is None
    assert check(under_unset, facts(rows)) == (
        "parent owner/repo#552 is closed with Status unset"
    )
    assert check(under_live, facts(rows)) is None


def test_finished_upkeep_project_not_closed():
    marker = funnel.origin_block("agent", at=NOW, run="run", agent="codex")
    finished = issue(560, status="Building", klass="Improve", body=marker,
                     children_total=3, children_done=3)
    new_work = issue(561, status="Building", klass="New",
                     children_total=3, children_done=3)
    unfinished = issue(562, status="Building", klass="Improve", body=marker,
                       children_total=3, children_done=2)
    rows = [finished, new_work, unfinished]
    check = funnel._strand_finished_upkeep_project

    assert check(finished, facts(rows)) == "finished upkeep project not closed"
    assert check(new_work, facts(rows)) is None
    assert check(unfinished, facts(rows)) is None


def test_dead_blocker():
    parked = issue(570, state="CLOSED", status="Parked", children_total=2)
    abandoned = ticket(571, parked)
    live = building(572)
    native = ticket(573, live, dead_blockers=["other/repo#99"])
    named = ticket(574, live, labels=["blocked"], block_reason="wait",
                   block_references=["#571"])
    sibling = ticket(575, parked, open_blockers=["{}#571".format(REPO)])
    rows = [parked, abandoned, live, native, named, sibling]
    check = funnel._strand_dead_blocker

    assert check(native, facts(rows)) == (
        "blocked on blocker that will never close: other/repo#99"
    )
    assert check(named, facts(rows)) == (
        "blocked on blocker that will never close: owner/repo#571"
    )
    # Parking is a decision: its own tickets are meant to sit.
    assert check(sibling, facts(rows)) is None


def test_unclearable_block_agent_owned_with_no_condition_or_decline():
    """#1393/#1432: an agent-owned block nothing lifts and nobody is asked."""
    project = building(580)
    silent = ticket(581, project, labels=["blocked"], needs="agent")
    declined = ticket(582, project, labels=["blocked"], needs="agent",
                      decline_reason="prerequisite unlanded")
    reason_only = ticket(583, project, labels=["blocked"],
                         needs="external-event", block_reason="an event")
    event_wait = ticket(584, project, labels=["blocked"],
                        needs="external-event", block_reason="wait",
                        block_event=dict(EVENT))
    unread = ticket(585, project, labels=["blocked"], needs="agent",
                    block_comments_error=UNREAD)
    dated = ticket(586, project, labels=["blocked"], needs="agent",
                   block_reason="wait", blocked_until=NOW.date())
    named = ticket(587, project, labels=["blocked"], needs="agent",
                   block_reason="wait", block_references=["#580"])
    edge = ticket(588, project, labels=["blocked"], needs="agent",
                  open_blockers=["{}#580".format(REPO)])
    environment = ticket(589, project, labels=["blocked"],
                         needs="claude-code-environment")
    rows = [project, silent, declined, reason_only, event_wait, unread,
            dated, named, edge, environment]
    check = funnel._strand_unclearable_block

    assert check(silent, facts(rows)) == (
        "blocked with no condition that can clear it, and no one is asked "
        "(Needs: agent)"
    )
    # A decline asks the watch "Unblock?"; a reason-only event wait asks it
    # too, because Needs external-event cannot stand in for a parsed spec.
    assert funnel.gate_question(declined) == "Unblock?"
    assert check(declined, facts(rows)) is None
    assert funnel.gate_question(reason_only) == "Unblock?"
    assert check(reason_only, facts(rows)) is None
    for lifted in (event_wait, unread, dated, named, edge, environment):
        assert check(lifted, facts(rows)) is None, lifted.ref


def test_block_cycle():
    first = issue(591, open_blockers=["{}#592".format(REPO)])
    second = issue(592, labels=["blocked"], block_reason="wait",
                   block_references=["#591"])
    chain = issue(593, open_blockers=["{}#591".format(REPO)])
    rows = [second, chain, first]
    check = funnel._strand_block_cycle

    # Reported once, on the lowest-numbered member.
    assert check(first, facts(rows)) == "block cycle: #591 → #592 → #591"
    assert check(second, facts(rows)) is None
    assert check(chain, facts(rows)) is None


# Composition: today's order and wording.


def test_reasons_join_in_todays_order():
    done = issue(600, state="CLOSED", status="Done", children_total=1)
    work = ticket(601, done, labels=["blocked"], needs="agent",
                  dead_blockers=["other/repo#99"])
    other = building(602)
    looped = ticket(603, other, dead_blockers=["other/repo#98"],
                    open_blockers=["{}#604".format(REPO)])
    partner = ticket(604, other, open_blockers=["{}#603".format(REPO)])

    rows = funnel.stranded_items(
        [done, work, other, looped, partner], NOW,
        pr_facts={work.ref: approved_conflicting()},
    )

    assert rows == [
        {
            "ref": work.ref,
            "title": "issue 601",
            "url": "https://example.invalid/601",
            "reason": "; ".join([
                "open PR on open ticket whose project Status is Done; "
                "merge gate will refuse it",
                "approved verdict against an unmergeable branch",
                "parent owner/repo#600 is closed with Status Done",
                "blocked on blocker that will never close: other/repo#99",
                "blocked with no condition that can clear it, and no one is "
                "asked (Needs: agent)",
            ]),
        },
        {
            "ref": looped.ref,
            "title": "issue 603",
            "url": "https://example.invalid/603",
            "reason": "blocked on blocker that will never close: "
                      "other/repo#98; block cycle: #603 → #604 → #603",
        },
    ]


def test_a_closed_ticket_reports_only_its_open_pr():
    """Every other check is about work that can still move."""
    done = issue(610, state="CLOSED", status="Done", children_total=1)
    closed = ticket(
        611, done, state="CLOSED", labels=["blocked"], needs="agent",
        dead_blockers=["other/repo#99"],
        in_motion_since=NOW - funnel.LOCK_TTL - timedelta(minutes=1),
    )

    assert funnel.stranded_items(
        [done, closed], NOW, pr_facts={closed.ref: {"state": "OPEN"}},
    ) == [{
        "ref": closed.ref,
        "title": "issue 611",
        "url": "https://example.invalid/611",
        "reason": "open PR on closed ticket",
    }]
    assert funnel.stranded_items([done, closed], NOW) == []


@pytest.mark.parametrize("name", [
    "_strand_open_pr_on_closed_ticket",
    "_strand_pr_under_non_building_project",
    "_strand_approved_conflicting_head",
    "_strand_stale_claim",
    "_strand_childless_building_project",
    "_strand_closed_parent",
    "_strand_finished_upkeep_project",
    "_strand_dead_blocker",
    "_strand_unclearable_block",
    "_strand_block_cycle",
])
def test_every_named_check_is_composed(name):
    assert getattr(funnel, name) in funnel.STRANDED_CHECKS
