"""The block-comment convention and its fail-closed parser."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def test_named_block_comment_returns_references_and_reason():
    assert funnel.parse_block_comment([
        "**Blocked on #84 and #90:** Wait for both decisions.",
    ]) == (["#84", "#90"], "Wait for both decisions.")


def test_block_comment_without_a_condition_returns_empty_references():
    assert funnel.parse_block_comment([
        "**Blocked:** Nate needs to decide whether this still matters.",
    ]) == ([], "Nate needs to decide whether this still matters.")


def test_latest_matching_block_comment_wins():
    assert funnel.parse_block_comment([
        "**Blocked on #84:** Older condition.",
        "An unrelated comment.",
        "**Blocked:** Newer decision needed.",
    ]) == ([], "Newer decision needed.")


def test_unparseable_block_comment_returns_no_condition():
    assert funnel.parse_block_comment([
        "**Blocked on #84, 2026-09-07.** Legacy format.",
    ]) is None


def test_embedded_block_prefix_does_not_match():
    assert funnel.parse_block_comment([
        "A sentence before **Blocked on #84:** is not a header.",
    ]) is None
