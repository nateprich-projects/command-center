"""The queue listing excludes work already sitting in an open PR (#1181)."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
REPO = "nateprich/beta"


def _queue_items():
    parent = funnel.Item(
        repo=REPO, number=10, title="Parent", url="", state="OPEN",
        status="Ready", klass="Broken", children_total=2, status_since=NOW,
    )
    in_pr = funnel.Item(
        repo=REPO, number=11, title="Already in a PR", url="", state="OPEN",
        parent="{}#10".format(REPO),
    )
    free = funnel.Item(
        repo=REPO, number=12, title="Still to do", url="", state="OPEN",
        parent="{}#10".format(REPO),
    )
    return [parent, in_pr, free]


def test_a_ticket_with_an_open_pr_is_left_out_of_the_queue(monkeypatch, capsys):
    items = _queue_items()
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda _items: {})
    monkeypatch.setattr(
        funnel, "awaiting_review",
        lambda _items, pr_facts=None: {"{}#11".format(REPO)},
    )

    assert funnel.cmd_queue(items, NOW) == 0
    out = capsys.readouterr().out

    assert "Startable by Codex (1)" in out
    assert "{}#12".format(REPO) in out
    assert "{}#11".format(REPO) not in out
    assert "degraded" not in out


def test_an_unreadable_pr_scan_says_so_instead_of_listing_blind(
    monkeypatch, capsys
):
    """An unfiltered list is indistinguishable from a correct one, and the
    whole defect is a ticket that looks startable and is not."""
    items = _queue_items()

    def unreadable(_items):
        raise funnel.GitHubError("PR scan offline")

    monkeypatch.setattr(funnel, "ticket_pr_facts", unreadable)

    assert funnel.cmd_queue(items, NOW) == 0
    out = capsys.readouterr().out

    assert "degraded — open-PR facts: PR scan offline" in out
    assert "may name work that is already done" in out
    # The list is still printed; it is the notice that makes it honest.
    assert "Startable by Codex (2)" in out


def test_a_supplied_scan_is_not_refetched(monkeypatch, capsys):
    calls = []

    def counted(_items):
        calls.append(1)
        return {}

    monkeypatch.setattr(funnel, "ticket_pr_facts", counted)
    monkeypatch.setattr(
        funnel, "awaiting_review", lambda _items, pr_facts=None: set()
    )

    assert funnel.cmd_queue(_queue_items(), NOW, pr_facts={}) == 0
    assert calls == []
    capsys.readouterr()


def test_a_timed_out_scan_degrades_like_a_failed_one(monkeypatch, capsys):
    def slow(_items):
        raise funnel.BriefSectionTimeout("ticket_pr_facts", "read timed out")

    monkeypatch.setattr(funnel, "ticket_pr_facts", slow)

    assert funnel.cmd_queue(_queue_items(), NOW) == 0
    assert "degraded — open-PR facts" in capsys.readouterr().out
