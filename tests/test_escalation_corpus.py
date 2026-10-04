"""Recorded escalation cases pin the shared ticket and plan scan results."""

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


@pytest.mark.parametrize("case", CORPUS, ids=lambda case: case["source"])
def test_recorded_escalation_outputs_match_base_corpus(case):
    body = case["body"]
    assert escalation_reasons("", body) == case["escalation_reasons"]
    assert plan_is_escalated(body) == case["plan_is_escalated"]
