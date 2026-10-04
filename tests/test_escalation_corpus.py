"""Recorded escalation cases pin the shared ticket and plan scan results.

The unchanged expected-output corpus was verified with this test against
base commit c7317ff01e2088c01a1c6bc9625ff9fbcad2af4c.
"""

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import escalation_reasons, plan_is_escalated  # noqa: E402

CORPUS = json.loads(
    (ROOT / "tests" / "fixtures" / "escalation_corpus.json").read_text(
        encoding="utf-8"
    )
)


FIXTURES = ROOT / "tests" / "fixtures"
REQUIRED_MARKDOWN_SOURCES = {
    "escalation_scan_1167_recorded.md",
    "escalation_plan_1503_recorded.md",
    "escalation_scan_ff225_capture.md",
}


def test_corpus_covers_every_escalation_fixture_body():
    expected = {}
    for path in sorted(FIXTURES.glob("escalation*.json")):
        if path.name == "escalation_corpus.json":
            continue
        records = json.loads(path.read_text(encoding="utf-8"))
        for index, record in enumerate(records):
            expected[f"{path.name}[{index}]"] = record["body"]

    markdown_sources = {path.name for path in FIXTURES.glob("escalation*.md")}
    assert REQUIRED_MARKDOWN_SOURCES <= markdown_sources
    for name in markdown_sources:
        expected[name] = (FIXTURES / name).read_text(encoding="utf-8")

    actual = {}
    for case in CORPUS:
        source = case["source"]
        assert source not in actual
        actual[source] = case["body"]
    assert actual == expected


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case["source"])
def test_recorded_escalation_outputs_match_base_corpus(case):
    body = case["body"]
    assert escalation_reasons("", body) == case["escalation_reasons"]
    assert plan_is_escalated(body) == case["plan_is_escalated"]
