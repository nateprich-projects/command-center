"""Pure satisfaction checks for parsed blocked-ticket conditions."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"


def issue(number, **kwargs):
    values = {
        "repo": REPO,
        "number": number,
        "title": "issue {}".format(number),
        "url": "https://example.invalid/{}".format(number),
        "state": "OPEN",
    }
    values.update(kwargs)
    return funnel.Item(**values)


@pytest.mark.parametrize(
    "ticket_number, references, blocker_numbers",
    [
        (108, ["#77"], [77]),
        (141, ["#138"], [138]),
        (145, ["#138"], [138]),
        (168, ["#161"], [161]),
    ],
)
def test_fully_parsed_closed_conditions_are_satisfied(
    ticket_number, references, blocker_numbers
):
    ticket = issue(
        ticket_number,
        block_reason="Wait for the condition.",
        block_references=references,
    )
    blockers = [
        issue(number, state="CLOSED", state_reason="COMPLETED")
        for number in blocker_numbers
    ]
    rows = {item.ref: item for item in [ticket] + blockers}

    assert funnel.satisfied_block_refs(ticket, rows) == [
        "{}{}".format(REPO, references[0])
    ]


def test_multiple_closed_conditions_return_sorted_full_references():
    ticket = issue(
        145,
        block_reason="Wait for both conditions.",
        block_references=["#161", "#138"],
    )
    blockers = [
        issue(138, state="CLOSED", state_reason="COMPLETED"),
        issue(161, state="CLOSED", state_reason="COMPLETED"),
    ]
    rows = {item.ref: item for item in [ticket] + blockers}

    assert funnel.satisfied_block_refs(ticket, rows) == [
        "owner/repo#138",
        "owner/repo#161",
    ]


@pytest.mark.parametrize(
    "ticket, blockers",
    [
        (issue(1, block_references=["#77"]), [issue(77, state="CLOSED")]),
        (issue(2, block_reason="Waiting for Nate."), []),
        (issue(3, block_reason="Malformed.", block_references=["77"]), []),
        (issue(4, block_reason="Missing.", block_references=["#77"]), []),
        (issue(5, block_reason="Still waiting.", block_references=["#77"]),
         [issue(77)]),
    ],
)
def test_any_unsatisfied_part_returns_nothing(ticket, blockers):
    rows = {item.ref: item for item in [ticket] + blockers}

    assert funnel.satisfied_block_refs(ticket, rows) is None


def test_a_genuine_dependency_chain_stays_unsatisfied():
    middle = issue(108)
    waiting = issue(
        109,
        block_reason="Wait for the chain.",
        block_references=["#108"],
    )

    assert funnel.satisfied_block_refs(
        waiting, {middle.ref: middle, waiting.ref: waiting}
    ) is None
