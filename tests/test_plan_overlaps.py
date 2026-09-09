"""Pure overlap candidates for plans being shaped together."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import plan_overlap_candidates  # noqa: E402


def candidates(current, others):
    return plan_overlap_candidates(current[0], current[1], others)


def test_shared_function_name_names_both_plans_and_function():
    assert candidates(
        ("#27", "The queue uses `startable()`."),
        [("#89", "The gate also changes startable().")],
    ) == ["#27 and #89 both name `startable()`"]


def test_plans_with_nothing_in_common_produce_no_candidates():
    assert candidates(
        ("#27", "The queue uses `startable()`."),
        [("#89", "The report updates `brief()`."),
         ("#90", "The parser reads `tests/test_parser.py`.")],
    ) == []


def test_shared_file_path_is_reported_without_line_number():
    assert candidates(
        ("#27", "Change `tests/test_ordering.py:339`."),
        [("#89", "Add a fixture in `tests/test_ordering.py`.")],
    ) == ["#27 and #89 both touch `tests/test_ordering.py`"]


def test_file_extensions_and_relative_prefixes_are_normalized_safely():
    assert candidates(
        ("#27", "Change `./.github/workflows/checks.tsx`."),
        [("#89", "Update `.github/workflows/checks.tsx`.")],
    ) == ["#27 and #89 both touch `.github/workflows/checks.tsx`"]


def test_shared_issue_reference_is_reported():
    assert candidates(
        ("#27", "This follows the decision in #91."),
        [("#89", "The same precedent is recorded in #91.")],
    ) == ["#27 and #89 both reference #91"]


def test_multiple_signal_types_are_deterministic_and_other_refs_are_sorted():
    assert candidates(
        ("#27", "Use `startable()` in `funnel.py:931`; see #91."),
        [
            ("#90", "See #91 and `startable()`."),
            ("#89", "Touch `funnel.py` and refer to #91."),
        ],
    ) == [
        "#27 and #89 both touch `funnel.py`",
        "#27 and #89 both reference #91",
        "#27 and #90 both name `startable()`",
        "#27 and #90 both reference #91",
    ]


def test_current_plan_is_not_compared_with_itself():
    assert candidates(
        ("#27", "Use `startable()` and `funnel.py`."),
        [("#27", "Use `startable()` and `funnel.py`.")],
    ) == []
