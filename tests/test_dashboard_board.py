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
