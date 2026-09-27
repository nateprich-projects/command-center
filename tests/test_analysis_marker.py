from pathlib import Path
from types import SimpleNamespace

import pytest

import funnel


def test_issue_1126_quoted_example_does_not_carry_analysis_marker():
    body = (Path(__file__).parent / "fixtures" /
            "analysis_marker_issue_1126.md").read_text()

    assert funnel.ANALYSIS_MARKER in body
    assert not any(
        line.strip() == funnel.ANALYSIS_MARKER
        for line in body.splitlines()
    )
    assert funnel.parse_analysis_marker(body) is None
    assert funnel._acceptance_waiting_reason(
        SimpleNamespace(body=body), "Accept it?"
    ) == "Ordinary accept"


@pytest.mark.parametrize(
    "body",
    [
        f'Example prose quotes "{funnel.ANALYSIS_MARKER}".',
        f"> {funnel.ANALYSIS_MARKER}\n",
        (f"~~~html\n{funnel.ANALYSIS_MARKER}\n~~~\n"),
    ],
)
def test_quoted_or_fenced_marker_examples_do_not_carry(body):
    assert funnel.parse_analysis_marker(body) is None


def test_standalone_analysis_marker_declaration_is_carried():
    fence = chr(96) * 3
    body = (funnel.ANALYSIS_MARKER +
            f'\n\n{fence}json\n{{"analysis": true}}\n{fence}\n')

    assert funnel.parse_analysis_marker(body) is True


def test_malformed_standalone_analysis_marker_still_fails_closed():
    fence = chr(96) * 3
    body = funnel.ANALYSIS_MARKER + f"\n\n{fence}json\n{{not json}}\n{fence}\n"

    assert funnel.parse_analysis_marker(body) is False
