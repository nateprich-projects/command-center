"""A marker quoted inside a JSON string does not cut its block short (#1688)."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def test_live_verdict_that_quotes_the_review_marker_parses():
    """PR #1667's verdicts quoted earlier verdicts and were all unreadable."""
    body = (FIXTURES / "review_verdict_quoting_marker_1667.md").read_text()

    verdict = funnel.parse_verdict(body)

    assert verdict is not None
    assert verdict["verdict"] == "rejected"
    assert verdict["head_sha"] == "937ce2c7ba842cc8887cdf8489b5e83651957939"
    assert all(funnel.REVIEW_MARKER in line for line in verdict["blocking"])
    assert funnel.parse_provenance(body)["agent"] == "zcode"


def test_marker_quoted_in_a_json_string_does_not_bound_the_block():
    body = (
        "{}\n\n```json\n"
        '{{\n  "verdict": "approved",\n  "note": "saw {} and {} above"\n}}\n'
        "```\n".format(funnel.REVIEW_MARKER, funnel.REVIEW_MARKER,
                       funnel.PROVENANCE_MARKER)
    )

    assert funnel.parse_verdict(body)["verdict"] == "approved"


def test_a_line_leading_marker_still_ends_the_owning_block():
    body = (
        "{}\n\n```json\n{{\"verdict\": \"approved\"}}\n```\n\n"
        "{}\n\n```json\n{{\"agent\": \"claude\", \"voice\": \"agent\"}}\n```\n"
        .format(funnel.REVIEW_MARKER, funnel.PROVENANCE_MARKER)
    )

    assert funnel.parse_verdict(body) == {"verdict": "approved"}
    assert funnel.parse_provenance(body)["agent"] == "claude"


def test_an_indented_marker_line_is_still_a_boundary():
    body = (
        "{}\n  {}\n```json\n{{\"agent\": \"claude\", \"voice\": \"agent\"}}\n```\n"
        .format(funnel.REVIEW_MARKER, funnel.PROVENANCE_MARKER)
    )

    assert funnel.parse_verdict(body) is None
