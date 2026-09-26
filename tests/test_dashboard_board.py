"""The dashboard board carries ticket rows with PR, tier and owner flags (#902).

Nate, 2026-09-15: the page shows the board and human steps only, so every
other signal he wants must ride on a board row. These rows are built from
items the brief already loaded plus the PR facts it already read; the only
extra lookup is a verdict for an open ticket PR.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
REPO = "owner/repo"


def project(**kw):
    values = dict(
        repo=REPO, number=1, title="Project", url="u", state="OPEN",
        status="Building", klass="Improve", children_total=0, children_done=0,
        status_since=NOW, origin="agent", risk="standard", needs="none",
    )
    values.update(kw)
    return Item(**values)


def ticket(number, **kw):
    values = dict(
        repo=REPO, number=number, title="t{}".format(number), url="u",
        state="OPEN", parent=REPO + "#1", body="Risk: standard",
        origin="agent", risk="standard", needs="none",
    )
    values.update(kw)
    if "risk" not in kw and "Risk: escalated" in values.get("body", ""):
        values["risk"] = "escalated"
    return Item(**values)


def rows(items, pr_facts=None, stage="Building"):
    board = funnel.dashboard_board(items, NOW, pr_facts=pr_facts)
    column = next(c for c in board["columns"] if c["stage"] == stage)
    return column["items"]


def test_tickets_ride_on_the_project_row_in_number_order():
    found = rows([project(children_total=2), ticket(12), ticket(11)])
    assert [t["number"] for t in found[0]["tickets"]] == [11, 12]
    assert found[0]["ref"] == REPO + "#1"


def test_an_open_pr_reads_submitted_and_an_approved_head_reads_approved():
    facts = {
        REPO + "#11": {"state": "OPEN", "number": 7, "headRefOid": "abc"},
        REPO + "#12": {
            "state": "OPEN", "number": 8, "headRefOid": "def",
            "verdict": {"verdict": "approved", "head_sha": "def"},
        },
        REPO + "#13": {
            "state": "OPEN", "number": 9, "headRefOid": "ghi",
            "verdict": {"verdict": "approved", "head_sha": "older"},
        },
    }
    found = rows([project(), ticket(11), ticket(12), ticket(13)], facts)
    pr = {t["number"]: t["pr"] for t in found[0]["tickets"]}
    assert pr == {11: "submitted", 12: "approved", 13: "submitted"}


def test_a_current_rejection_requests_changes_and_hands_work_to_the_implementer():
    facts = {
        REPO + "#11": {
            "state": "OPEN", "number": 7, "headRefOid": "abc",
            "verdict": {"verdict": "rejected", "head_sha": "abc"},
        },
        REPO + "#12": {
            "state": "OPEN", "number": 8, "headRefOid": "def",
            "verdict": {"verdict": "rejected", "head_sha": "def"},
            "body": "Generated with [Claude Code](https://claude.com/claude-code)",
        },
        REPO + "#13": {
            "state": "OPEN", "number": 9, "headRefOid": "ghi",
            "verdict": {"verdict": "rejected", "head_sha": "ghi"},
        },
        REPO + "#14": {
            "state": "OPEN", "number": 10, "headRefOid": "jkl",
            "verdict": {"verdict": "rejected", "head_sha": "older"},
        },
        REPO + "#15": {
            "state": "OPEN", "number": 11, "headRefOid": "mno",
        },
    }
    found = rows([
        project(),
        ticket(11), ticket(12), ticket(13, body="Risk: escalated — concurrency"),
        ticket(14), ticket(15),
    ], facts)
    by_number = {t["number"]: t for t in found[0]["tickets"]}
    assert by_number[11]["pr"] == "changes requested"
    assert by_number[11]["owner"] == "Codex"
    assert by_number[11]["queue_rank"] == 0
    assert by_number[12]["pr"] == "changes requested"
    assert by_number[12]["owner"] == "Claude"
    assert by_number[13]["pr"] == "changes requested"
    # Codex implements escalated work too since #1322; rework goes back to it.
    assert by_number[13]["owner"] == "Codex"
    assert by_number[14]["pr"] == "submitted"
    assert by_number[14]["owner"] == "Muse"
    assert by_number[15]["pr"] == "submitted"
    assert by_number[15]["owner"] == "Muse"


def test_runner_attribution_hands_rejected_work_back_to_the_authoring_agent():
    facts = {
        REPO + "#11": {
            "state": "OPEN", "number": 7, "headRefOid": "abc",
            "verdict": {"verdict": "rejected", "head_sha": "abc"},
        },
    }
    found = funnel.dashboard_board(
        [project(), ticket(11)], NOW, pr_facts=facts,
        authoring_pr_agents={"7": {"claude"}},
    )
    column = next(c for c in found["columns"] if c["stage"] == "Building")
    ticket_row = column["items"][0]["tickets"][0]
    assert ticket_row["pr"] == "changes requested"
    assert ticket_row["owner"] == "Claude"


def test_a_merged_pr_and_no_pr_are_distinct():
    facts = {REPO + "#11": {"state": "MERGED", "number": 7}}
    found = rows([project(), ticket(11, state="CLOSED"), ticket(12)], facts)
    pr = {t["number"]: t["pr"] for t in found[0]["tickets"]}
    assert pr == {11: "merged", 12: None}


def test_the_owner_follows_the_needs_field_then_the_pr_then_the_tier():
    facts = {REPO + "#14": {"state": "OPEN", "number": 7, "headRefOid": "abc"}}
    found = rows([
        project(),
        ticket(11, needs="claude-code-environment",
               body="Risk: standard"),
        ticket(12, needs="human", body="Risk: standard"),
        ticket(13, body="Risk: escalated — credentials"),
        ticket(14),
        ticket(15),
        ticket(16, state="CLOSED"),
    ], facts)
    owners = {t["number"]: t["owner"] for t in found[0]["tickets"]}
    # Fresh escalated work goes to Codex since #1322; an open PR (14) is
    # still the reviewer's move.
    assert owners == {
        11: "Claude", 12: "Nate", 13: "Codex", 14: "Muse", 15: "Codex", 16: None,
    }


def test_the_tier_comes_from_the_risk_field():
    found = rows([project(), ticket(11, risk="escalated"),
                  ticket(12)], None)
    tiers = {t["number"]: t["tier"] for t in found[0]["tickets"]}
    assert tiers == {11: "escalated", 12: "standard"}
    matches = {t["number"]: t["escalation_matches"]
               for t in found[0]["tickets"]}
    assert matches[11] == []
    assert matches[12] == []


def test_a_board_without_pr_facts_still_renders_rows():
    found = rows([project(children_total=1), ticket(11)], None)
    assert found[0]["tickets"][0]["pr"] is None
    assert found[0]["tickets"][0]["owner"] == "Codex"


def test_the_board_performs_no_per_ticket_github_read(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("dashboard_board read GitHub per ticket")

    monkeypatch.setattr(funnel, "latest_verdict", explode)
    monkeypatch.setattr(funnel, "_gh_json", explode)
    found = rows([project(), ticket(11), ticket(12, state="CLOSED")],
                 {REPO + "#11": {"state": "CLOSED", "number": 7}})
    assert len(found[0]["tickets"]) == 2


def test_a_verdict_is_read_once_for_an_open_pr_without_one(monkeypatch):
    calls = []

    def record(repo, number):
        calls.append((repo, number))
        return {"verdict": "approved", "head_sha": "abc"}

    monkeypatch.setattr(funnel, "latest_verdict", record)
    found = rows([project(), ticket(11)],
                 {REPO + "#11": {"state": "OPEN", "number": 7, "headRefOid": "abc"}})
    assert calls == [(REPO, 7)]
    assert found[0]["tickets"][0]["pr"] == "approved"


def test_tickets_read_in_queue_order_with_closed_work_last():
    """The board reads as the priority it is: next to be taken first (#902)."""
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=3, children_done=1),
         ticket(11, state="CLOSED"),
         ticket(12, body="Risk: standard"),
         ticket(13, body="Risk: standard")],
        NOW,
    )["columns"]
    column = next(c for c in rows if c["stage"] == "Building")
    tickets = column["items"][0]["tickets"]
    assert [t["number"] for t in tickets] == [12, 13, 11]
    assert tickets[0]["queue_rank"] == 0
    assert tickets[-1]["queue_rank"] is None


def test_the_project_row_names_only_the_next_step_owner():
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=2),
         ticket(11, body="Risk: standard"),
         ticket(12, needs="human", body="Risk: standard")],
        NOW,
    )["columns"]
    column = next(c for c in rows if c["stage"] == "Building")
    row = column["items"][0]
    assert row["next_owner"] == row["tickets"][0]["owner"]
    assert row["next_owner"] == "Codex"


def test_a_ticket_in_review_is_not_ranked_as_next():
    facts = {REPO + "#11": {"state": "OPEN", "number": 7, "headRefOid": "abc"}}
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=2), ticket(11), ticket(12)],
        NOW, pr_facts=facts,
    )["columns"]
    column = next(c for c in rows if c["stage"] == "Building")
    ranks = {t["number"]: t["queue_rank"] for t in column["items"][0]["tickets"]}
    assert ranks[11] is None
    assert ranks[12] == 0


def test_done_reads_newest_first():
    from datetime import timedelta
    older = project(number=2, status="Done", state="CLOSED",
                    closed_at=NOW - timedelta(days=5))
    newer = project(number=3, status="Done", state="CLOSED",
                    closed_at=NOW - timedelta(days=1))
    rows = funnel.dashboard_board([older, newer], NOW)["columns"]
    column = next(c for c in rows if c["stage"] == "Done")
    assert [i["ref"] for i in column["items"]] == [newer.ref, older.ref]


def test_a_pinned_project_leads_its_stage_even_with_nothing_startable():
    """#902: with no startable ticket the pin used to sink under gate age."""
    from datetime import timedelta
    pinned = project(number=5, title="pinned", pinned=True, klass="New",
                     status_since=NOW - timedelta(hours=1))
    broken = project(number=6, title="broken", klass="Broken",
                     status_since=NOW - timedelta(days=4))
    improve = project(number=7, title="improve", klass="Improve",
                      status_since=NOW - timedelta(days=9))
    rows = funnel.dashboard_board([pinned, broken, improve], NOW)["columns"]
    column = next(c for c in rows if c["stage"] == "Building")
    assert [i["title"] for i in column["items"]] == ["pinned", "broken", "improve"]


def test_the_ladder_outranks_startable_work_for_unpinned_projects():
    """Nate, 2026-09-24: class first, whoever owns the next step. This test
    asserted the opposite until then."""
    from datetime import timedelta
    ready = project(number=8, title="has work", klass="Improve",
                    children_total=1, status_since=NOW - timedelta(days=1))
    review = project(number=9, title="broken in review", klass="Broken",
                     status_since=NOW - timedelta(days=3))
    facts = {REPO + "#31": {"state": "OPEN", "number": 7, "headRefOid": "a"}}
    rows = funnel.dashboard_board(
        [ready, review, ticket(21, parent=ready.ref),
         ticket(31, parent=review.ref)], NOW, pr_facts=facts,
    )["columns"]
    column = next(c for c in rows if c["stage"] == "Building")
    assert [i["title"] for i in column["items"]] == [
        "broken in review", "has work",
    ]
    assert [i["next_owner"] for i in column["items"]] == ["Muse", "Codex"]


def test_the_bar_fills_from_the_left_whatever_the_queue_order():
    """#902: finished work reads as progress, so it leads the bar."""
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=4, children_done=2),
         ticket(11), ticket(12, state="CLOSED"), ticket(13),
         ticket(14, state="CLOSED")],
        NOW,
    )["columns"]
    row = next(c for c in rows if c["stage"] == "Building")["items"][0]
    assert row["pips"] == ["closed", "closed", "open", "open"]
    # The rows under the bar still answer "what next", in queue order.
    assert [t["state"] for t in row["tickets"]] == ["OPEN", "OPEN", "CLOSED", "CLOSED"]


def test_bar_segments_rank_by_how_far_the_work_has_travelled():
    facts = {
        REPO + "#11": {"state": "OPEN", "number": 7, "headRefOid": "abc",
                       "verdict": {"verdict": "approved", "head_sha": "abc"}},
        REPO + "#12": {"state": "OPEN", "number": 8, "headRefOid": "def"},
    }
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=3),
         ticket(11), ticket(12), ticket(13)],
        NOW, pr_facts=facts,
    )["columns"]
    row = next(c for c in rows if c["stage"] == "Building")["items"][0]
    assert row["pips"] == ["approved", "submitted", "open"]


def bar_for(closed=0, approved=0, changes_requested=0, submitted=0,
            blocked=0, open_=0):
    rows = []
    number = 10
    for _ in range(closed):
        number += 1
        rows.append(ticket(number, state="CLOSED"))
    for _ in range(approved + changes_requested + submitted + blocked + open_):
        number += 1
        rows.append(ticket(number))
    facts = {}
    index = closed - 1
    for _ in range(approved):
        index += 1
        facts[rows[index].ref] = {
            "state": "OPEN", "number": index, "headRefOid": "a",
            "verdict": {"verdict": "approved", "head_sha": "a"},
        }
    for _ in range(changes_requested):
        index += 1
        facts[rows[index].ref] = {
            "state": "OPEN", "number": index, "headRefOid": "a",
            "verdict": {"verdict": "rejected", "head_sha": "a"},
        }
    for _ in range(submitted):
        index += 1
        facts[rows[index].ref] = {"state": "OPEN", "number": index, "headRefOid": "b"}
    total = closed + approved + submitted + blocked + open_
    board = funnel.dashboard_board(
        [project(status="Building", children_total=total)] + rows, NOW,
        pr_facts=facts,
    )["columns"]
    return next(c for c in board if c["stage"] == "Building")["items"][0]["pips"]


def _building_row(items):
    board = funnel.dashboard_board(items, NOW)["columns"]
    return next(c for c in board if c["stage"] == "Building")["items"][0]


def test_open_work_sits_left_of_blocked_work_in_the_bar():
    """Nate, 2026-09-24: open pips before blocked pips, and a ticket waiting
    only on a sibling before one blocked from outside the project."""
    row = _building_row([
        project(children_total=4),
        ticket(11, labels=["blocked"], block_reason="Waiting on Nate."),
        ticket(12, open_blockers=[REPO + "#13"]),
        ticket(13),
        ticket(14, state="CLOSED"),
    ])
    assert row["pips"] == ["closed", "open", "queued", "blocked"]


@pytest.mark.parametrize("blocked_ticket", [
    dict(open_blockers=[REPO + "#11"]),
    dict(labels=["blocked"], block_references=["#11"]),
    dict(open_blockers=[REPO + "#11"], labels=["blocked"],
         block_references=[REPO + "#11"]),
])
def test_a_ticket_blocked_only_by_siblings_reads_apart(blocked_ticket):
    row = _building_row([
        project(children_total=2), ticket(11), ticket(12, **blocked_ticket),
    ])
    by_number = {t["number"]: t for t in row["tickets"]}
    assert by_number[12]["blocked"] is True
    assert by_number[12]["blocked_by_siblings"] is True
    assert by_number[11]["blocked_by_siblings"] is False
    assert row["pips"] == ["open", "queued"]


@pytest.mark.parametrize("blocked_ticket", [
    # A blocker in another project, or anywhere outside this one.
    dict(open_blockers=[REPO + "#99"]),
    # One sibling and one outsider is still blocked from outside.
    dict(open_blockers=[REPO + "#11", REPO + "#99"]),
    dict(open_blockers=[REPO + "#11"], labels=["blocked"],
         block_references=["#99"]),
    # A reason with no references, or a date hold, names no sibling.
    dict(open_blockers=[REPO + "#11"], labels=["blocked"],
         block_reason="Waiting on Nate."),
    dict(labels=["blocked"], block_references=["#11"],
         blocked_until=datetime(2026, 10, 1).date()),
])
def test_a_block_from_outside_the_project_keeps_the_blocked_pip(blocked_ticket):
    row = _building_row([
        project(children_total=2), ticket(11), ticket(12, **blocked_ticket),
    ])
    by_number = {t["number"]: t for t in row["tickets"]}
    assert by_number[12]["blocked_by_siblings"] is False
    assert row["pips"] == ["open", "blocked"]


def test_blocked_pips_line_up_behind_their_blockers():
    """Nate, 2026-09-24, on a project whose sibling-blocked tickets wait on
    tickets blocked from outside: the bar should show who blocks whom."""
    outside = [REPO + "#225"]
    row = _building_row([
        project(children_total=8),
        ticket(260, open_blockers=outside),
        ticket(261, open_blockers=outside),
        ticket(262, open_blockers=outside),
        ticket(263, open_blockers=[REPO + "#261", REPO + "#260", REPO + "#262"]),
        ticket(264, open_blockers=outside),
        ticket(265, state="CLOSED"),
        ticket(266, open_blockers=[REPO + "#264"]),
        ticket(267, open_blockers=[REPO + "#266", REPO + "#264"]),
    ])
    by_number = {t["number"]: t for t in row["tickets"]}
    assert by_number[267]["sibling_blockers"] == [REPO + "#266", REPO + "#264"]
    assert by_number[260]["sibling_blockers"] == []
    assert row["pips"] == (
        ["closed"] + ["blocked"] * 4 + ["queued"] * 3
    )


def test_unfinished_rows_follow_the_bar_and_closed_rows_stay_last():
    """Nate, 2026-09-24: the pips and the sub-issue rows should agree on the
    order the tickets will be worked; closed work stays where it was in
    both (first in the bar, last in the rows)."""
    outside = [REPO + "#225"]
    row = _building_row([
        project(children_total=8),
        ticket(260, open_blockers=outside),
        ticket(261, open_blockers=outside),
        ticket(262, open_blockers=outside),
        ticket(263, open_blockers=[REPO + "#261", REPO + "#260", REPO + "#262"]),
        ticket(264, open_blockers=outside),
        ticket(265, state="CLOSED"),
        ticket(266, open_blockers=[REPO + "#264"]),
        ticket(267, open_blockers=[REPO + "#266", REPO + "#264"]),
    ])
    numbers = [t["number"] for t in row["tickets"]]
    assert numbers == [260, 261, 262, 264, 263, 266, 267, 265]
    open_rows = [t for t in row["tickets"] if t["state"] == "OPEN"]
    closed = row["pips"].count("closed")
    assert [funnel._dashboard_pip_state(t) for t in open_rows] == (
        row["pips"][closed:]
    )


def test_a_chain_from_open_work_leads_a_chain_from_outside():
    row = _building_row([
        project(children_total=4),
        ticket(11),
        ticket(12, open_blockers=[REPO + "#11"]),
        ticket(13, labels=["blocked"], block_reason="Waiting on Nate."),
        ticket(14, open_blockers=[REPO + "#13"]),
        ticket(15, open_blockers=[REPO + "#14"]),
    ])
    assert row["pips"] == [
        "open", "queued", "blocked", "queued",
        "queued",
    ]


def test_a_sibling_cycle_still_draws_every_ticket():
    row = _building_row([
        project(children_total=2),
        ticket(11, open_blockers=[REPO + "#12"]),
        ticket(12, open_blockers=[REPO + "#11"]),
    ])
    assert row["pips"] == ["queued", "queued"]


def test_a_scaled_bar_keeps_the_blocked_states_in_first_seen_order():
    outside = [REPO + "#999"]
    items = [project(children_total=20)]
    items += [ticket(n, state="CLOSED") for n in range(11, 21)]
    items += [ticket(n, open_blockers=outside) for n in range(21, 26)]
    items += [ticket(n, open_blockers=[REPO + "#21"]) for n in range(26, 31)]
    bar = _building_row(items)["pips"]
    assert len(bar) == funnel.PIP_SEGMENTS
    assert bar.index("blocked") < bar.index("queued")


def test_a_blocked_projects_tickets_are_blocked_from_outside():
    row = _building_row([
        project(children_total=2, labels=["blocked"], block_references=["#794"]),
        ticket(11), ticket(12, open_blockers=[REPO + "#11"]),
    ])
    assert [t["blocked_by_siblings"] for t in row["tickets"]] == [False, False]
    assert row["pips"] == ["blocked", "blocked"]


def test_a_large_project_gets_a_proportional_bar_not_one_pip_per_ticket():
    """#902: 44 tickets rendered as one solid block in a 130px column."""
    bar = bar_for(closed=37, open_=7)
    assert len(bar) == funnel.PIP_SEGMENTS
    assert bar.count("closed") == 10
    assert bar.count("open") == 2


def test_in_flight_work_keeps_a_segment_and_sits_at_the_end_of_the_colour():
    bar = bar_for(closed=30, approved=2, changes_requested=2, submitted=3, open_=7)
    assert len(bar) == funnel.PIP_SEGMENTS
    assert bar.count("approved") >= 1
    assert bar.count("changes-requested") >= 1 and bar.count("submitted") >= 1
    # The coloured run ends with the work in flight, then the open remainder.
    assert bar.index("approved") > bar.index("closed")
    assert bar.index("submitted") > bar.index("approved")
    assert bar.index("open") > bar.index("submitted")


def test_one_submitted_pr_among_forty_is_still_visible():
    bar = bar_for(closed=39, submitted=1)
    assert bar.count("submitted") == 1
    assert bar[-1] == "submitted"


def test_a_small_project_still_shows_one_pip_per_ticket():
    assert bar_for(closed=5, open_=1) == ["closed"] * 5 + ["open"]


def test_a_failed_pr_scan_reads_unknown_rather_than_no_pr():
    """#934: an empty scan and a failed scan looked identical on the page."""
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=2), ticket(11),
         ticket(12, state="CLOSED")],
        NOW, pr_facts={}, pr_facts_known=False,
    )["columns"]
    row = next(c for c in rows if c["stage"] == "Building")["items"][0]
    states = {t["number"]: t["pr"] for t in row["tickets"]}
    assert states[11] == "unknown"
    # A closed ticket needs no PR state; only open work can be in review.
    assert states[12] is None
    assert "unknown" in row["pips"]


def test_a_successful_scan_with_no_prs_still_reads_as_no_pr():
    rows = funnel.dashboard_board(
        [project(status="Building", children_total=1), ticket(11)],
        NOW, pr_facts={}, pr_facts_known=True,
    )["columns"]
    row = next(c for c in rows if c["stage"] == "Building")["items"][0]
    assert row["tickets"][0]["pr"] is None


def test_an_open_native_blocker_reads_blocked_with_no_owner():
    # #807 on 2026-09-16: blocked by #806 natively, no label, shown as Muse's.
    found = rows([
        project(children_total=2),
        ticket(11, open_blockers=[REPO + "#10"]),
        ticket(12),
    ])
    by_number = {t["number"]: t for t in found[0]["tickets"]}
    assert by_number[11]["blocked"] is True
    assert by_number[11]["owner"] is None
    assert by_number[11]["blockers"] == [REPO + "#10"]
    assert by_number[12]["blocked"] is False
    assert by_number[12]["owner"] == "Codex"
    assert found[0]["next_owner"] == "Codex"


def test_tickets_under_a_blocked_project_read_blocked_with_its_reason():
    # #25 on 2026-09-16: the project waits on #794; its tickets showed Codex's.
    found = rows(
        [
            project(
                status="Ready", labels=["blocked"], block_references=["#794"],
                block_reason="Put behind the engine plan.",
            ),
            ticket(11),
            ticket(12, state="CLOSED"),
        ],
        stage="Ready",
    )
    row = found[0]
    assert row["blocked"] is True
    assert row["blockers"] == ["#794"]
    assert row["block_reason"] == "Put behind the engine plan."
    assert row["next_owner"] is None
    by_number = {t["number"]: t for t in row["tickets"]}
    assert by_number[11]["blocked"] is True
    assert by_number[11]["owner"] is None
    assert by_number[11]["block_reason"] == "project blocked by #794"
    assert by_number[12]["blocked"] is False


def test_a_blocked_project_without_refs_passes_down_its_reason():
    found = rows(
        [
            project(labels=["blocked"], block_reason="Waiting on Nate."),
            ticket(11),
        ],
    )
    ticket_row = found[0]["tickets"][0]
    assert ticket_row["block_reason"] == "project blocked: Waiting on Nate."


def test_an_unblocked_project_row_says_so():
    found = rows([project(), ticket(11)])
    assert found[0]["blocked"] is False
    assert found[0]["blockers"] == []
    assert found[0]["block_reason"] is None
    assert found[0]["tickets"][0]["block_reason"] is None


def _rework_owner(monkeypatch, *, tier_body, agents, roster=None):
    if roster is not None:
        monkeypatch.setitem(funnel.AGENTS_BY_ROLE, "implement", roster)
    facts = {REPO + "#11": {
        "state": "OPEN", "number": 7, "headRefOid": "abc",
        "verdict": {"verdict": "rejected", "head_sha": "abc"},
    }}
    found = funnel.dashboard_board(
        [project(), ticket(11, body=tier_body)], NOW, pr_facts=facts,
        authoring_pr_agents={"7": agents},
    )
    column = next(c for c in found["columns"] if c["stage"] == "Building")
    return column["items"][0]["tickets"][0]["owner"]


@pytest.mark.parametrize("tier_body", ["Risk: standard",
                                       "Risk: escalated — concurrency"])
def test_muse_authored_rework_goes_to_codex(monkeypatch, tier_body):
    """Muse no longer implements (#1322): a rejected PR it wrote is
    reworked by the roster's implementer."""
    assert _rework_owner(monkeypatch, tier_body=tier_body,
                         agents={"muse"}) == "Codex"


def test_a_roster_naming_muse_alone_gives_muse_its_work_back(monkeypatch):
    """The reversal path keeps the board consistent with begin."""
    found = rows([project(), ticket(12, body="Risk: escalated — x")])
    assert found[0]["tickets"][0]["owner"] == "Codex"

    roster = {"muse": frozenset(funnel.TIERS)}
    assert _rework_owner(monkeypatch, tier_body="Risk: escalated — x",
                         agents={"muse"}, roster=roster) == "Muse"
    found = rows([project(), ticket(12, body="Risk: escalated — x")])
    assert found[0]["tickets"][0]["owner"] == "Muse"


# The board is what comes next (Nate, 2026-09-24): "The dashboard should
# simply be an accurate representation of what comes next, not a string of
# contrived rules to make it appear to be an accurate representation."
# ``projected_pull_order`` runs ``startable()`` forward; projects and their
# open rows follow its turns, and what it never reaches sinks below.

def _titles(found):
    return [row["title"] for row in found]


def _turns(items):
    return [
        int(ref.rsplit("#", 1)[1])
        for ref in funnel.projected_pull_order(items, NOW)
    ]


def test_the_projection_frees_work_as_it_goes():
    a = project(number=2, title="A", klass="Broken")
    b = project(number=3, title="B", klass="Improve")
    c = project(number=4, title="C", klass="Improve")
    items = [
        a, b, c,
        ticket(11, parent=a.ref),
        # Startable only once #11 is done.
        ticket(41, parent=b.ref, open_blockers=[REPO + "#11"]),
        ticket(31, parent=c.ref),
    ]
    assert _turns(items) == [11, 31, 41]
    # The board is that order, not "under whatever frees it".
    assert _titles(rows(items)) == ["A", "C", "B"]


def test_a_ticket_that_frees_more_work_goes_first_as_startable_says():
    a = project(number=2, title="A", klass="Improve")
    b = project(number=3, title="B", klass="Improve")
    items = [
        a, b,
        ticket(11, parent=a.ref),
        ticket(21, parent=b.ref),
        ticket(22, parent=b.ref, open_blockers=[REPO + "#21"]),
        ticket(23, parent=b.ref, open_blockers=[REPO + "#21"]),
    ]
    assert _turns(items) == [21, 11, 22, 23]
    assert _titles(rows(items)) == ["B", "A"]


def test_a_labelled_block_lifts_once_what_it_names_is_done():
    a = project(number=2, title="A")
    items = [
        a,
        ticket(11, parent=a.ref),
        ticket(12, parent=a.ref, labels=["blocked"],
               block_references=["#11"], block_reason=""),
    ]
    assert _turns(items) == [11, 12]


def test_a_project_ref_clears_once_all_its_tickets_are_done():
    a = project(number=2, title="A", children_total=2)
    b = project(number=3, title="B")
    items = [
        a, b,
        ticket(11, parent=a.ref),
        ticket(12, parent=a.ref),
        ticket(31, parent=b.ref, open_blockers=[a.ref]),
    ]
    assert _turns(items) == [11, 12, 31]


def test_work_under_way_takes_its_turn_and_a_date_never_does():
    a = project(number=2, title="A", children_total=4)
    facts = {REPO + "#13": {"state": "OPEN", "number": 7, "headRefOid": "a"}}
    items = [
        a,
        ticket(11, parent=a.ref, needs="human"),
        ticket(12, parent=a.ref),
        ticket(13, parent=a.ref),
        ticket(14, parent=a.ref, labels=["blocked"], block_reason="",
               blocked_until=datetime(2026, 12, 1).date()),
    ]
    assert _turns(items) == [11, 12, 13]
    found = rows(items, facts)[0]
    assert [t["number"] for t in found["tickets"]] == [11, 12, 13, 14]
    assert [t["projected_turn"] for t in found["tickets"]] == [0, 1, 2, None]
    # The next step is the first row's: Nate's human step.
    assert found["next_owner"] == "Nate"
    # Rows and pips agree.
    assert found["pips"] == ["open", "open", "submitted", "blocked"]


def test_unreached_projects_follow_in_gate_order_below_the_projection():
    from datetime import timedelta
    moving = project(number=2, title="new, moving", klass="New")
    stuck_broken = project(number=3, title="broken, dated", klass="Broken",
                           status_since=NOW - timedelta(days=1))
    stuck_improve = project(number=4, title="improve, waits elsewhere",
                            klass="Improve",
                            status_since=NOW - timedelta(days=9))
    found = rows([
        stuck_improve, stuck_broken, moving,
        ticket(21, parent=moving.ref),
        ticket(31, parent=stuck_broken.ref, labels=["blocked"],
               blocked_until=datetime(2026, 10, 1).date()),
        ticket(41, parent=stuck_improve.ref, open_blockers=["other/repo#1"]),
    ])
    assert _titles(found) == [
        "new, moving", "broken, dated", "improve, waits elsewhere",
    ]


def test_a_ticket_shows_the_class_it_ranks_as_and_what_it_unblocks():
    """Nate, 2026-09-24, on #1424: an Improve project's ticket led the
    Broken ones because three Broken tickets wait on it."""
    plans = project(number=2, title="plans", klass="Improve",
                    children_total=2)
    review = project(number=3, title="review", klass="Broken",
                     status="Ready", children_total=3)
    found = rows([
        plans, review,
        ticket(24, parent=plans.ref),
        ticket(23, parent=plans.ref),
        ticket(65, parent=review.ref, open_blockers=[REPO + "#24"]),
        ticket(67, parent=review.ref, open_blockers=[REPO + "#24"]),
        ticket(66, parent=review.ref, open_blockers=[REPO + "#24"]),
    ])[0]
    by_number = {t["number"]: t for t in found["tickets"]}
    assert by_number[24]["class"] == "Broken"
    assert by_number[24]["unblocks"] == [
        REPO + "#65", REPO + "#66", REPO + "#67",
    ]
    assert by_number[24]["unblocks_later"] == []
    assert by_number[23]["class"] == "Improve"
    assert by_number[23]["unblocks"] == []
    # The class shown is the class startable() ranks by.
    ranked = funnel.queue_classes([
        plans, review, ticket(24, parent=plans.ref),
        ticket(65, parent=review.ref, open_blockers=[REPO + "#24"]),
    ])
    assert ranked[REPO + "#24"] == "Broken"


def test_the_rest_of_a_chain_is_unblocked_later():
    """The live #1424 shape: #1465 waits on it, #1466 on #1465, #1467 on
    #1466. Finishing #1424 frees #1465 alone; the rest follow."""
    found = rows([
        project(children_total=4),
        ticket(24),
        ticket(65, open_blockers=[REPO + "#24"]),
        ticket(66, open_blockers=[REPO + "#65"]),
        ticket(67, open_blockers=[REPO + "#66"]),
    ])[0]
    first = next(t for t in found["tickets"] if t["number"] == 24)
    assert first["unblocks"] == [REPO + "#65"]
    assert first["unblocks_later"] == [REPO + "#66", REPO + "#67"]


def test_a_closed_ticket_carries_no_class_or_unblocks():
    found = rows([
        project(children_total=2),
        ticket(11, state="CLOSED"),
        ticket(12, open_blockers=[REPO + "#11"]),
    ])[0]
    closed = next(t for t in found["tickets"] if t["number"] == 11)
    assert closed["class"] is None and closed["unblocks"] == []


def test_a_paused_ticket_waits_until_the_available_work_has_had_its_turn():
    """Nate, 2026-09-25: a row must never claim a next step the engineers
    won't take. A backoff hold keeps a ticket out of `begin` until it lifts."""
    a = project(number=2, title="A", klass="Broken")
    b = project(number=3, title="B", klass="Improve")
    items = [a, b, ticket(11, parent=a.ref), ticket(21, parent=b.ref)]
    assert _turns(items) == [11, 21]
    assert [
        int(r.rsplit("#", 1)[1])
        for r in funnel.projected_pull_order(items, NOW, paused=[REPO + "#11"])
    ] == [21, 11]


def test_paused_and_finished_tickets_name_their_hold_not_a_false_owner():
    until = datetime(2026, 9, 26, 8, 9, 44, tzinfo=timezone.utc)
    a = project(number=2, title="paused", klass="Broken", children_total=1)
    b = project(number=3, title="finished", klass="Improve", children_total=1)
    c = project(number=4, title="moving", klass="Improve", children_total=1)
    items = [a, b, c, ticket(11, parent=a.ref), ticket(21, parent=b.ref),
             ticket(31, parent=c.ref)]
    monkey = {REPO + "#11": {"failures": 6, "until": until}}
    original = funnel.finished_by_comments
    funnel.finished_by_comments = lambda rows: [REPO + "#21"]
    try:
        board = funnel.dashboard_board(items, NOW, backed_off=monkey)
    finally:
        funnel.finished_by_comments = original
    found = next(c for c in board["columns"] if c["stage"] == "Building")
    by_title = {row["title"]: row for row in found["items"]}
    paused = by_title["paused"]["tickets"][0]
    assert paused["owner"] is None
    assert paused["paused_until"] == until.isoformat()
    assert paused["paused_failures"] == 6
    assert by_title["paused"]["next_owner"] is None
    assert by_title["paused"]["next_step_paused_until"] == until.isoformat()
    finished = by_title["finished"]["tickets"][0]
    assert finished["owner"] == "Nate" and finished["finished_by_comments"]
    assert by_title["finished"]["next_owner"] == "Nate"
    # The paused Broken project no longer leads: its turn comes after the
    # work available now.
    assert [row["title"] for row in found["items"]][-1] == "paused"


def test_a_projection_failure_still_renders_the_board(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("projection")
    monkeypatch.setattr(funnel, "projected_pull_order", boom)
    stuck = project(number=2, title="stuck", klass="Broken",
                    labels=["blocked"], block_references=[REPO + "#9"])
    moving = project(number=3, title="moving", klass="Improve")
    found = rows([stuck, moving, ticket(31, parent=moving.ref)])
    assert _titles(found) == ["moving", "stuck"]


def test_a_blocked_project_sits_below_every_project_that_can_move():
    from datetime import timedelta
    # The stuck project has waited longer, but its only ticket waits on a
    # date, while the other's PR is Muse's to review right now.
    stuck = project(number=2, title="stuck broken", klass="Broken",
                    status_since=NOW - timedelta(days=9))
    moving = project(number=3, title="in review", klass="Broken",
                     status_since=NOW - timedelta(days=1))
    facts = {REPO + "#31": {"state": "OPEN", "number": 7, "headRefOid": "a"}}
    found = rows([
        stuck, moving,
        ticket(21, parent=stuck.ref, labels=["blocked"],
               blocked_until=datetime(2026, 10, 1).date()),
        ticket(31, parent=moving.ref),
    ], facts)
    assert _titles(found) == ["in review", "stuck broken"]
    assert found[0]["next_owner"] == "Muse"
    assert found[0]["next_step_blocked"] is False
    assert found[1]["next_owner"] is None
    assert found[1]["next_step_blocked"] is True


def test_a_block_nothing_on_the_column_can_lift_sinks_to_the_bottom():
    from datetime import timedelta
    moving = project(number=2, title="moving", klass="Broken",
                     status_since=NOW)
    offboard = project(number=3, title="waits elsewhere", klass="Broken",
                       status_since=NOW - timedelta(days=9))
    reason = project(number=4, title="waits on a reason", klass="Broken",
                     status_since=NOW - timedelta(days=3),
                     labels=["blocked"], block_reason="Nate's call.")
    found = rows([
        offboard, reason, moving,
        ticket(21, parent=moving.ref),
        # Blocked by a project in another stage: not work on this column.
        ticket(31, parent=offboard.ref, open_blockers=["other/repo#9"]),
        ticket(41, parent=reason.ref),
    ])
    assert _titles(found) == ["moving", "waits elsewhere", "waits on a reason"]
    assert [row["next_step_blocked"] for row in found] == [False, True, True]


def test_a_dependency_cycle_sinks_rather_than_recursing():
    a = project(number=2, title="a", klass="Broken", status_since=NOW)
    b = project(number=3, title="b", klass="Broken", status_since=NOW)
    moving = project(number=4, title="moving", klass="Broken",
                     status_since=NOW)
    found = rows([
        a, b, moving,
        ticket(21, parent=a.ref, open_blockers=[b.ref]),
        ticket(31, parent=b.ref, open_blockers=[a.ref]),
        ticket(41, parent=moving.ref),
    ])
    assert _titles(found)[0] == "moving"
    assert set(_titles(found)[1:]) == {"a", "b"}


def test_a_project_with_one_actionable_ticket_is_not_blocked():
    found = rows([
        project(children_total=2),
        ticket(11, open_blockers=[REPO + "#99"]),
        ticket(12, needs="human"),
    ])
    assert found[0]["next_step_blocked"] is False
    assert found[0]["next_owner"] == "Nate"


def test_within_a_project_actionable_tickets_lead_blocked_ones():
    facts = {REPO + "#13": {"state": "OPEN", "number": 7, "headRefOid": "a"}}
    found = rows([
        project(children_total=4),
        ticket(11, open_blockers=[REPO + "#99"]),
        ticket(12, state="CLOSED"),
        ticket(13),
        ticket(14, needs="human"),
    ], facts)
    assert [t["number"] for t in found[0]["tickets"]] == [13, 14, 11, 12]


def test_a_pinned_blocked_project_keeps_its_pin():
    from datetime import timedelta
    pinned = project(number=2, title="pinned", pinned=True,
                     labels=["blocked"], block_reason="Waiting.",
                     status_since=NOW)
    moving = project(number=3, title="moving", status_since=NOW)
    found = rows([moving, pinned, ticket(31, parent=moving.ref)])
    assert _titles(found) == ["pinned", "moving"]
    assert found[0]["next_step_blocked"] is True


def test_a_ticketless_blocked_project_sinks_in_a_gate_stage():
    from datetime import timedelta
    blocked = project(number=2, title="blocked", status="Shaped",
                      klass="Broken", labels=["blocked"],
                      block_reason="Waiting.",
                      status_since=NOW - timedelta(days=9))
    waiting = project(number=3, title="at gate", status="Shaped",
                      klass="Broken", status_since=NOW)
    found = rows([blocked, waiting], stage="Shaped")
    assert _titles(found) == ["at gate", "blocked"]
    assert [row["next_step_blocked"] for row in found] == [False, True]


# Review of PR #1415: every condition on a block has to clear, including the
# blocker's own block, before a blocked project may sit above workable work.

def test_a_blocker_that_cannot_move_itself_sends_its_dependent_to_the_bottom():
    x = project(number=2, title="X free", klass="Broken")
    y = project(number=3, title="Y free", klass="Improve")
    b = project(number=4, title="B waits on x22", klass="Improve")
    found = rows([
        x, y, b,
        ticket(21, parent=x.ref, needs="human"),
        ticket(22, parent=x.ref, labels=["blocked"],
               blocked_until=datetime(2026, 12, 1).date()),
        ticket(41, parent=b.ref, open_blockers=[REPO + "#22"]),
    ])
    assert _titles(found) == ["X free", "Y free", "B waits on x22"]


@pytest.mark.parametrize("blocked_project, blocked_ticket", [
    # A project block naming X's ticket, but also a date.
    (dict(labels=["blocked"], block_references=[REPO + "#61"],
          blocked_until=datetime(2026, 12, 1).date()), {}),
    # A native blocker on X, plus a label whose only condition is a reason.
    ({}, dict(open_blockers=[REPO + "#61"], labels=["blocked"],
              block_reason="Nate's call")),
    # A project block naming X, while its one ticket waits off the column.
    (dict(labels=["blocked"], block_references=[REPO + "#61"]),
     dict(open_blockers=["other/repo#1"])),
])
def test_a_condition_nothing_here_can_lift_sinks_the_project(
    blocked_project, blocked_ticket,
):
    x = project(number=6, title="X free", klass="Broken")
    y = project(number=7, title="Y free", klass="Improve")
    z = project(number=8, title="Z", klass="Improve", **blocked_project)
    found = rows([
        x, y, z,
        ticket(61, parent=x.ref, needs="human"),
        ticket(71, parent=y.ref, needs="human"),
        ticket(81, parent=z.ref, **blocked_ticket),
    ])
    assert _titles(found) == ["X free", "Y free", "Z"]


def test_a_cycle_with_a_way_out_resolves_through_it():
    from datetime import timedelta
    f = project(number=2, title="F", klass="Broken")
    g = project(number=3, title="G", klass="Improve")
    a = project(number=4, title="A", klass="Broken",
                status_since=NOW - timedelta(days=5))
    b = project(number=5, title="B", klass="Broken",
                status_since=NOW - timedelta(days=5))
    found = rows([
        f, g, a, b,
        ticket(21, parent=f.ref, needs="human"),
        ticket(31, parent=g.ref, needs="human"),
        # A waits on B's b51 or on F's f21; b51 waits on A's a42.
        ticket(41, parent=a.ref, open_blockers=[REPO + "#51"]),
        ticket(42, parent=a.ref, open_blockers=[REPO + "#21"]),
        ticket(51, parent=b.ref, open_blockers=[REPO + "#42"]),
    ])
    assert _titles(found) == ["F", "A", "B", "G"]


def test_finished_work_never_reads_blocked():
    done = project(number=9, title="done", status="Done", state="CLOSED",
                   labels=["blocked"], closed_at=NOW)
    found = rows([done], stage="Done")
    assert found[0]["next_step_blocked"] is False
