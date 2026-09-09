"""Fixtures for parsed block conditions and their satisfied state."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


REPO = "owner/repo"


def issue(
    number,
    *,
    comment=None,
    state="OPEN",
    open_blockers=(),
    dead_blockers=(),
):
    parsed = funnel.parse_block_comment([comment]) if comment is not None else None
    references, reason = parsed or ([], None)
    return funnel.Item(
        repo=REPO,
        number=number,
        title="issue {}".format(number),
        url="https://example.invalid/{}".format(number),
        state=state,
        block_references=references,
        block_reason=reason,
        open_blockers=list(open_blockers),
        dead_blockers=list(dead_blockers),
    )


@pytest.mark.parametrize(
    "body, expected",
    [
        pytest.param(
            "**Blocked on #77:** waiting for the scan.",
            (["#77"], "waiting for the scan."),
            id="single-reference",
        ),
        pytest.param(
            "**Blocked on #138 and #141:** waiting for both decisions.",
            (["#138", "#141"], "waiting for both decisions."),
            id="multiple-references",
        ),
        pytest.param(
            "**Blocked:** waiting for Nate to provision the token.",
            ([], "waiting for Nate to provision the token."),
            id="no-reference",
        ),
    ],
)
def test_block_comment_fixtures_parse(body, expected):
    assert funnel.parse_block_comment([body]) == expected


@pytest.mark.parametrize(
    "number, blocker_number",
    [
        pytest.param(108, 77, id="108-blocked-on-77"),
        pytest.param(141, 138, id="141-blocked-on-138"),
        pytest.param(145, 138, id="145-blocked-on-138"),
        pytest.param(168, 161, id="168-blocked-on-161"),
    ],
)
def test_closed_block_fixtures_are_satisfied(number, blocker_number):
    waiting = issue(
        number,
        comment="**Blocked on #{}:** waiting for the prerequisite.".format(
            blocker_number
        ),
    )
    blocker = issue(blocker_number, state="CLOSED")

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, blocker.ref: blocker}
    ) == [blocker.ref]


def test_empty_reference_block_is_never_satisfied():
    human_step = issue(
        270,
        comment="**Blocked:** waiting for Nate to provision the token.",
    )

    assert human_step.block_references == []
    assert human_step.block_reason is not None
    assert funnel.satisfied_block_refs(human_step, {}) is None


def test_open_chain_block_is_not_satisfied():
    waiting = issue(
        109,
        comment="**Blocked on #108:** waiting for the earlier ticket to land.",
    )
    not_landed = issue(108)

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, not_landed.ref: not_landed}
    ) is None


def test_native_open_blocker_keeps_a_closed_named_block_unsatisfied():
    waiting = issue(
        108,
        comment="**Blocked on #77:** waiting for the scan.",
        open_blockers=["#77"],
    )
    blocker = issue(77, state="CLOSED")

    assert funnel.satisfied_block_refs(
        waiting, {waiting.ref: waiting, blocker.ref: blocker}
    ) is None
