"""Pure overlap candidates for plans being shaped together."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funnel import Item, plan_overlap_candidates, shaping_plan_overlap_candidates  # noqa: E402


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


def test_overlap_check_record_is_not_a_signal():
    recorded = """

## Overlap check

Checked: #27 and #89 (the other open plans considered)

Candidates:
- #27 and #89 both name `startable()`
- #27 and #89 both touch `funnel.py`
- #27 and #89 both reference #91
"""

    assert candidates(
        ("#27", "The plan's only signals are recorded below." + recorded),
        [("#89", "Use `startable()` in `funnel.py` and see #91.")],
    ) == []


def test_printed_overlap_candidates_are_idempotent_when_recorded():
    current = ("#27", "Change `funnel.py` and see #91.")
    other = ("#89", "Update `funnel.py` and see #91.")
    first = candidates(current, [other])
    record = "\n\n## Overlap check\n\nCandidates:\n{}\n".format(
        "\n".join("- {}".format(line) for line in first)
    )

    assert first == [
        "#27 and #89 both touch `funnel.py`",
        "#27 and #89 both reference #91",
    ]
    assert candidates(
        (current[0], current[1] + record),
        [(other[0], other[1] + record)],
    ) == first


def test_own_ref_pairs_are_excluded_for_bare_and_owner_prefixed_forms():
    assert candidates(
        ("owner/repo#27", "Use `startable()` and `funnel.py`."),
        [("#27", "Use `startable()` and `funnel.py`.")],
    ) == []


def test_each_plan_own_issue_ref_is_excluded_from_shared_references():
    assert candidates(
        ("owner/repo#27", "See #27 and #89."),
        [("owner/repo#89", "See owner/repo#27 and #89.")],
    ) == []


COLLISION_FIXTURES = [
    pytest.param(
        ("#27", "The capability categories live in `skills/shape/SKILL.md`."),
        [("#89", "The human-step checklist changes `skills/shape/SKILL.md`.")],
        "#27 and #89 both touch `skills/shape/SKILL.md`",
        id="same-axis-binary-versus-three-categories",
    ),
    pytest.param(
        ("#97", "The doctor reports the live routine path `routines/codex-work.md`."),
        [("#171", "The clone migration repoints `routines/codex-work.md`.")],
        "#97 and #171 both touch `routines/codex-work.md`",
        id="hazard-removed-by-clone-migration",
    ),
    pytest.param(
        ("#65", "Guard the `start()` transition when a project has no children."),
        [("#34", "Delete the `start()` transition from the revised gate model.")],
        "#65 and #34 both name `start()`",
        id="start-guarded-versus-deleted",
    ),
    pytest.param(
        ("#178", "Investigate the `WIP_LIMIT` question in `funnel.py`."),
        [("#250", "Keep the `WIP_LIMIT` decision record in `funnel.py`.")],
        "#178 and #250 both touch `funnel.py`",
        id="wip-limit-already-answered",
    ),
]


@pytest.mark.parametrize("current, others, expected", COLLISION_FIXTURES)
def test_replayed_2026_collisions_surface_advisory_candidates(
    current, others, expected
):
    """The four shaping collisions are visible without a network lookup."""
    assert expected in candidates(current, others)


def _item(number, *, status, body, state="OPEN", parent=None):
    return Item(
        repo="owner/repo",
        number=number,
        title="Item {}".format(number),
        url="https://github.com/owner/repo/issues/{}".format(number),
        state=state,
        status=status,
        body=body,
        parent=parent,
    )


def test_shaping_scan_uses_other_open_project_plans_in_flight():
    current = _item(27, status="Ideas", body="Use `startable()` and #91.")
    others = [
        _item(89, status="Shaped", body="Change `startable()` and see #91."),
        _item(90, status="Ready", body="Change `startable()`."),
        _item(91, status="Building", body="See #91."),
    ]

    assert shaping_plan_overlap_candidates([current] + others, current,
                                           current.body) == [
        "owner/repo#27 and owner/repo#89 both name `startable()`",
        "owner/repo#27 and owner/repo#89 both reference #91",
        "owner/repo#27 and owner/repo#90 both name `startable()`",
    ]


def test_shaping_scan_ignores_closed_ideas_and_child_tickets():
    current = _item(27, status="Ideas", body="Use `startable()`. ")
    ignored = [
        _item(89, status="Done", body="Use `startable()`."),
        _item(90, status="Shaped", body="Use `startable()`. ", state="CLOSED"),
        _item(91, status="Building", body="Use `startable()`. ", parent=current.ref),
    ]

    assert shaping_plan_overlap_candidates([current] + ignored, current,
                                           current.body) == []


def test_shaping_scan_ignores_recorded_overlap_check_on_both_plans():
    current_body = "Implement `startable()` in `funnel.py`."
    other_body = "Implement `brief()` in `tests/test_brief.py`."
    recorded_overlap = """

## Overlap check

Checked: #27 and #89 (the other open plans considered)

Candidates:
- #27 and #89 both touch `skills/shape/SKILL.md`

Conclusion:
- Keep one mechanism and narrow the plan accordingly.
"""
    plain_current = _item(27, status="Ideas", body=current_body)
    plain_other = _item(89, status="Shaped", body=other_body)
    recorded_current = _item(
        27, status="Ideas", body=current_body + recorded_overlap
    )
    recorded_other = _item(
        89, status="Shaped", body=other_body + recorded_overlap
    )

    without_record = shaping_plan_overlap_candidates(
        [plain_current, plain_other], plain_current, current_body
    )
    with_record = shaping_plan_overlap_candidates(
        [recorded_current, recorded_other], recorded_current,
        recorded_current.body,
    )

    assert without_record == []
    assert with_record == without_record
