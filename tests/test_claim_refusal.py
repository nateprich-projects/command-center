"""A direct `funnel claim` on a live ticket refuses and writes nothing (#1075).

Observed 2026-09-18: `funnel claim ...#1066` succeeded while an escalated
`muse-implement` run held a live claim, because `claim_ticket` compared the
target against itself (`taken is target`, so the timestamp comparison could
never differ) instead of testing ref membership in the live set.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 18, 8, 30, 0, tzinfo=timezone.utc)


def _project(number=1, status="Building", klass="New") -> Item:
    return Item(
        repo="nateprich/beta",
        number=number,
        title="issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state="OPEN",
        status=status,
        klass=klass,
        status_since=NOW - timedelta(days=1),
        item_id="PVTI_{}".format(number),
        children_total=1,
    )


def _ticket(number, parent, held_since=None) -> Item:
    return Item(
        repo="nateprich/beta",
        number=number,
        title="issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state="OPEN",
        parent="nateprich/beta#{}".format(parent),
        item_id="PVTI_{}".format(number),
        status_since=NOW - timedelta(days=1),
        in_motion_since=held_since,
    )


def test_a_second_claim_on_a_live_ticket_refuses_and_writes_nothing(
    monkeypatch, capsys
):
    """The #1066 shape: live claim plus a second claim while held."""
    held_since = NOW - timedelta(minutes=25)
    ticket = _ticket(2, 1, held_since=held_since)
    items = [_project(), ticket]
    # The holder pushed its ticket branch three minutes before the
    # conflicting claim, so the claim is unambiguously live.
    facts = {ticket.ref: {"branch_exists": True}}
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )

    assert funnel.cmd_claim(items, NOW, ticket.ref, pr_facts=facts) == 1

    err = capsys.readouterr().err
    assert "refused \u2014 {} is already claimed".format(ticket.ref) in err
    assert writes == []
    assert ticket.in_motion_since == held_since


def test_a_claim_on_a_stale_lock_still_takes_over(monkeypatch, capsys):
    """The live-ticket refusal must not change the stale-takeover path."""
    target = _ticket(2, 1, held_since=NOW - timedelta(hours=3))
    other = _ticket(4, 3, held_since=NOW - timedelta(hours=3))
    items = [_project(1), target, _project(3), other]
    writes = []
    monkeypatch.setattr(
        funnel, "write_lock", lambda item, value: writes.append((item.ref, value))
    )

    assert funnel.cmd_claim(items, NOW, target.ref) == 0

    err = capsys.readouterr().err
    assert "took over stale claim on {}".format(other.ref) in err
    assert "took over stale claim on {}".format(target.ref) not in err
    assert writes == [
        (other.ref, ""),
        (target.ref, "2026-09-18T08:30:00Z"),
    ]
