"""The dashboard board carries ticket rows with PR, tier and owner flags (#902).

Nate, 2026-09-15: the page shows the board and human steps only, so every
other signal he wants must ride on a board row. These rows are built from
items the brief already loaded plus the PR facts it already read; the only
extra lookup is a verdict for an open ticket PR.
"""

from __future__ import annotations

import pathlib
import sys
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
        status_since=NOW,
    )
    values.update(kw)
    return Item(**values)


def ticket(number, **kw):
    values = dict(
        repo=REPO, number=number, title="t{}".format(number), url="u",
        state="OPEN", parent=REPO + "#1", body="Risk: standard",
    )
    values.update(kw)
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


def test_a_merged_pr_and_no_pr_are_distinct():
    facts = {REPO + "#11": {"state": "MERGED", "number": 7}}
    found = rows([project(), ticket(11, state="CLOSED"), ticket(12)], facts)
    pr = {t["number"]: t["pr"] for t in found[0]["tickets"]}
    assert pr == {11: "merged", 12: None}


def test_the_owner_follows_the_capability_marker_then_the_pr_then_the_tier():
    facts = {REPO + "#14": {"state": "OPEN", "number": 7, "headRefOid": "abc"}}
    found = rows([
        project(),
        ticket(11, body="Human step: a Claude Code environment\nRisk: standard"),
        ticket(12, body="Human step: entering a credential\nRisk: standard"),
        ticket(13, body="Risk: escalated — credentials"),
        ticket(14),
        ticket(15),
        ticket(16, state="CLOSED"),
    ], facts)
    owners = {t["number"]: t["owner"] for t in found[0]["tickets"]}
    assert owners == {
        11: "Claude", 12: "Nate", 13: "Muse", 14: "Muse", 15: "Codex", 16: None,
    }


def test_the_tier_comes_from_the_ticket_body():
    found = rows([project(), ticket(11, body="Risk: escalated — concurrency"),
                  ticket(12)], None)
    tiers = {t["number"]: t["tier"] for t in found[0]["tickets"]}
    assert tiers == {11: "escalated", 12: "standard"}


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
         ticket(12, body="Human step: entering a credential\nRisk: standard")],
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


def test_startable_work_still_outranks_the_ladder_for_unpinned_projects():
    from datetime import timedelta
    ready = project(number=8, title="has work", klass="Improve",
                    children_total=1, status_since=NOW - timedelta(days=1))
    stuck = project(number=9, title="stuck broken", klass="Broken",
                    status_since=NOW - timedelta(days=3))
    rows = funnel.dashboard_board(
        [ready, stuck, ticket(21, parent=ready.ref)], NOW,
    )["columns"]
    column = next(c for c in rows if c["stage"] == "Building")
    assert [i["title"] for i in column["items"]] == ["has work", "stuck broken"]


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


def bar_for(closed=0, approved=0, submitted=0, blocked=0, open_=0):
    rows = []
    number = 10
    for _ in range(closed):
        number += 1
        rows.append(ticket(number, state="CLOSED"))
    for _ in range(approved + submitted + blocked + open_):
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
    for _ in range(submitted):
        index += 1
        facts[rows[index].ref] = {"state": "OPEN", "number": index, "headRefOid": "b"}
    total = closed + approved + submitted + blocked + open_
    board = funnel.dashboard_board(
        [project(status="Building", children_total=total)] + rows, NOW,
        pr_facts=facts,
    )["columns"]
    return next(c for c in board if c["stage"] == "Building")["items"][0]["pips"]


def test_a_large_project_gets_a_proportional_bar_not_one_pip_per_ticket():
    """#902: 44 tickets rendered as one solid block in a 130px column."""
    bar = bar_for(closed=37, open_=7)
    assert len(bar) == funnel.PIP_SEGMENTS
    assert bar.count("closed") == 10
    assert bar.count("open") == 2


def test_in_flight_work_keeps_a_segment_and_sits_at_the_end_of_the_colour():
    bar = bar_for(closed=30, approved=2, submitted=3, open_=9)
    assert len(bar) == funnel.PIP_SEGMENTS
    assert bar.count("approved") >= 1 and bar.count("submitted") >= 1
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
