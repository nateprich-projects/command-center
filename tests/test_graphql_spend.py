"""Per-run GraphQL spend, accumulated from responses and reported to stderr (#295)."""

from __future__ import annotations

import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


class Proc:
    returncode = 0

    def __init__(self, payload):
        self.stdout = json.dumps(payload)
        self.stderr = ""


def reset():
    funnel._GRAPHQL_SPEND.update(
        {"calls": 0, "cost": 0, "remaining": None, "reset_at": None})


def test_spend_is_accumulated_from_the_response(monkeypatch):
    reset()
    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"viewer": {"login": "n"},
                  "rateLimit": {"cost": 7, "remaining": 4993,
                                "resetAt": "2026-09-09T04:52:19Z"}}}))

    funnel.gh_graphql("{viewer{login}}")

    spend = funnel.graphql_spend()
    assert spend == {"calls": 1, "cost": 7, "remaining": 4993,
                     "reset_at": "2026-09-09T04:52:19Z"}


def test_cost_is_summed_not_differenced(monkeypatch):
    """A shared token means a `remaining` delta charges us other agents' spend.

    Measured 2026-09-08: 1,826 points on this token belonged to something other
    than the measuring session.
    """
    reset()
    responses = [
        {"data": {"a": 1, "rateLimit": {"cost": 2, "remaining": 100, "resetAt": "z"}}},
        {"data": {"a": 1, "rateLimit": {"cost": 2, "remaining": 50, "resetAt": "z"}}},
    ]
    monkeypatch.setattr(funnel.subprocess, "run",
                        lambda *a, **k: Proc(responses.pop(0)))

    funnel.gh_graphql("{a}")
    funnel.gh_graphql("{a}")

    assert funnel.graphql_spend()["cost"] == 4, "sum reported cost, never a delta"
    assert funnel.graphql_spend()["remaining"] == 50


def test_a_missing_or_malformed_block_does_not_break_the_call(monkeypatch):
    reset()
    monkeypatch.setattr(funnel.subprocess, "run",
                        lambda *a, **k: Proc({"data": {"a": 1}}))
    assert funnel.gh_graphql("{a}") == {"a": 1}

    monkeypatch.setattr(funnel.subprocess, "run", lambda *a, **k: Proc(
        {"data": {"a": 1, "rateLimit": "nonsense"}}))
    assert funnel.gh_graphql("{a}") == {"a": 1, "rateLimit": "nonsense"}
    assert funnel.graphql_spend()["calls"] == 2


def test_the_report_goes_to_a_stream_and_names_the_numbers():
    reset()
    funnel._GRAPHQL_SPEND.update(
        {"calls": 3, "cost": 41, "remaining": 130, "reset_at": "04:52:19Z"})
    out = io.StringIO()

    funnel.report_graphql_spend(out)

    text = out.getvalue()
    assert "3 call(s)" in text and "41 point(s)" in text
    assert "130 remaining" in text and "04:52:19Z" in text


def test_a_run_that_made_no_graphql_call_reports_nothing():
    """A row of zeros on every local command trains the reader to ignore it."""
    reset()
    out = io.StringIO()
    funnel.report_graphql_spend(out)
    assert out.getvalue() == ""


def test_unknown_remaining_is_said_rather_than_guessed():
    reset()
    funnel._GRAPHQL_SPEND.update({"calls": 1, "cost": 0})
    out = io.StringIO()
    funnel.report_graphql_spend(out)
    assert "unknown remaining" in out.getvalue()
