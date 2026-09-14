"""The recorded Needs field ids match the Project schema decided in #794."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def test_needs_field_id_is_a_single_select_field():
    assert funnel.NEEDS_FIELD_ID.startswith("PVTSSF_")


def test_needs_option_ids_are_distinct():
    ids = {
        funnel.NEEDS_OPTION_NONE,
        funnel.NEEDS_OPTION_HUMAN,
        funnel.NEEDS_OPTION_CLAUDE_CODE_ENVIRONMENT,
    }
    assert len(ids) == 3
    assert all(isinstance(option_id, str) and option_id for option_id in ids)


def test_needs_options_match_decided_enum():
    assert funnel.NEEDS_OPTIONS == ("none", "human", "claude-code-environment")
