"""Backoff on a repeatedly failing job (#1202, #1230).

The shape pinned here is measured, not invented: over the heartbeat's
10,000-record window on 2026-09-21, 27 of 493 bound works recorded two or
more errored finishes. The worst were FF-Weekly-Start-Sit#208 — eleven errored
runs in 23.1 hours with one success at the end — The-League#186 (nine in 21.8
hours), the shape job on command-center#1178 (seven in 4.7 hours), and
The-League#225 (five in 5.9 hours with no success at all).
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)
WORK = "nateprich-projects/FF-Weekly-Start-Sit#208"


def _run(run, work, outcome, hours_ago, agent="muse"):
    """One run's bind and finish, as the heartbeat records them."""
    ts = (NOW - timedelta(hours=hours_ago)).timestamp()
    return [
        {"phase": "bind", "run": run, "ts": ts - 1, "agent": agent,
         "do": "ticket", "work": work},
        {"phase": "finish", "run": run, "ts": ts, "agent": agent,
         "outcome": outcome},
    ]


def _history(outcomes, work=WORK, start=23.1, step=2.0):
    rows = []
    for index, outcome in enumerate(outcomes):
        rows += _run("r{}".format(index), work, outcome, start - index * step)
    return rows


def test_the_measured_eleven_failure_run_is_counted():
    """FF#208's shape: eleven errored runs, oldest first."""
    rows = _history(["errored"] * 11)

    counts = funnel.consecutive_failures(rows)

    assert counts[WORK][0] == 11


def test_a_success_clears_the_run_of_failures():
    """The same job after its one success reads as zero, not eleven."""
    rows = _history(["errored"] * 11 + ["done"])

    assert funnel.consecutive_failures(rows).get(WORK) is None


def test_a_skip_neither_counts_nor_clears():
    """A lane that stood down for the budget says nothing about the work."""
    rows = _history(["errored", "errored", "skipped-over-pace", "errored"])

    assert funnel.consecutive_failures(rows)[WORK][0] == 3


def test_three_consecutive_failures_withhold_the_work():
    rows = _history(["errored"] * 3, start=1.0, step=0.5)

    withheld = funnel.backoff_withheld(rows, NOW)

    assert WORK in withheld
    assert withheld[WORK]["failures"] == 3
    assert "3 consecutive failed runs" in withheld[WORK]["reason"]
    assert "startable again after" in withheld[WORK]["reason"]


def test_two_failures_are_still_offered():
    """One bad GitHub response must not park a ticket."""
    rows = _history(["errored", "errored"], start=1.0, step=0.5)

    assert funnel.backoff_withheld(rows, NOW) == {}


def test_the_cooldown_expires_and_the_work_returns():
    rows = _history(["errored"] * 3, start=9.0, step=0.5)

    assert funnel.backoff_withheld(rows, NOW) == {}
    # Still withheld an hour after the last failure, released after six.
    assert WORK in funnel.backoff_withheld(
        rows, NOW - timedelta(hours=8)
    )


def test_a_further_failure_restarts_the_cooldown():
    old = _history(["errored"] * 3, start=9.0, step=0.5)
    fresh = old + _run("r-fresh", WORK, "errored", 0.5)

    assert funnel.backoff_withheld(old, NOW) == {}
    assert WORK in funnel.backoff_withheld(fresh, NOW)


def test_startable_withholds_a_backed_off_ticket():
    parent = funnel.Item(
        repo="nateprich/beta", number=10, title="Parent", url="",
        state="OPEN", status="Ready", klass="Broken", children_total=2,
    )
    first = funnel.Item(
        repo="nateprich/beta", number=11, title="One", url="",
        state="OPEN", parent="nateprich/beta#10",
    )
    second = funnel.Item(
        repo="nateprich/beta", number=12, title="Two", url="",
        state="OPEN", parent="nateprich/beta#10",
    )

    assert len(funnel.startable([parent, first, second])) == 2
    queue = funnel.startable(
        [parent, first, second],
        backed_off={"nateprich/beta#11": {"reason": "backoff"}},
    )
    assert [item.ref for item in queue] == ["nateprich/beta#12"]


def test_the_withheld_row_names_the_count_and_the_condition():
    ticket = funnel.Item(
        repo="nateprich-projects/FF-Weekly-Start-Sit", number=208,
        title="A ticket", url="", state="OPEN",
        parent="nateprich-projects/FF-Weekly-Start-Sit#200",
    )
    rows = _history(["errored"] * 3, start=1.0, step=0.5)

    reported = funnel.backoff_withheld_rows([ticket], rows, NOW)

    assert len(reported) == 1
    assert reported[0]["failures"] == 3
    assert "startable again after" in reported[0]["reason"]


def test_a_closed_ticket_is_history_rather_than_a_withheld_row():
    ticket = funnel.Item(
        repo="nateprich-projects/FF-Weekly-Start-Sit", number=208,
        title="A ticket", url="", state="CLOSED",
        parent="nateprich-projects/FF-Weekly-Start-Sit#200",
    )
    rows = _history(["errored"] * 3, start=1.0, step=0.5)

    assert funnel.backoff_withheld_rows([ticket], rows, NOW) == []


def test_no_heartbeat_rows_withhold_nothing():
    """An unreadable history offers every ticket exactly as before."""
    assert funnel.backoff_withheld([], NOW) == {}
