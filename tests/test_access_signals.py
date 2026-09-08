"""Access-shaped vocabulary is a prompt beside the breakdown checklist."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import access_signals  # noqa: E402


PLAN_25 = """
Run the service under Colima behind a Cloudflare Tunnel. Nate creates the
fine-grained token and registers the connector in his account. The Cloudflare
Tunnel and token are also named in the deployment checklist.
"""

ALL_CLEAR_PLAN = """
Implement the parser, add fixtures, and run the full test suite.
"""


def test_issue_25_vocabulary_is_caught():
    assert access_signals(PLAN_25) == ["account", "register", "token", "tunnel"]


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("OAuth", "oauth"),
        ("DNS", "dns"),
        ("enable", "enable"),
        ("settings", "settings"),
        ("billing", "billing"),
        ("API-key", "api key"),
        ("signed-in", "sign in"),
        ("verification", "verify"),
    ],
)
def test_access_vocabulary_is_case_insensitive_and_handles_inflections(word, expected):
    assert access_signals("The plan mentions {}.".format(word)) == [expected]


def test_a_plan_without_access_vocabulary_stays_clear():
    # The all-clear answers live in the coverage comment; only the plan body
    # is passed to this scan, so checklist labels cannot create their own hits.
    assert access_signals(ALL_CLEAR_PLAN) == []


def test_empty_or_missing_plan_has_no_signals():
    assert access_signals("") == []
    assert access_signals(None) == []
