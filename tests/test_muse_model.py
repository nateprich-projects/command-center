"""Tests for the Muse model allowlist, resolver and rate cards (#1300)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import muse_model  # noqa: E402


ALLOWED = ("command-center", "FF-Weekly-Start-Sit", "The-League")
EXCLUDED = ("jeffy-finance-agent", "workbench", "career-toolset")


@pytest.mark.parametrize("repo", ALLOWED)
def test_allowlisted_repos_resolve_to_contributor(repo):
    assert muse_model.model_for(repo) == muse_model.CONTRIBUTOR_MODEL


@pytest.mark.parametrize("repo", ALLOWED)
def test_owner_qualified_form_resolves_the_same(repo):
    """The runners get `owner/name` from funnel.py begin."""
    assert muse_model.model_for("nateprich-projects/" + repo) == \
        muse_model.CONTRIBUTOR_MODEL


@pytest.mark.parametrize("repo", EXCLUDED)
def test_excluded_member_repos_resolve_to_standard(repo):
    assert muse_model.model_for(repo) == muse_model.STANDARD_MODEL
    assert muse_model.model_for("nateprich-projects/" + repo) == \
        muse_model.STANDARD_MODEL


@pytest.mark.parametrize("repo", [
    "some-repo-nobody-has-named",
    "nateprich-projects/some-repo-nobody-has-named",
    "",
    "   ",
    None,
    42,
    ["command-center"],
])
def test_unknown_input_resolves_to_standard(repo):
    """Safe by default: only an exact allowlist hit reaches contributor."""
    assert muse_model.model_for(repo) == muse_model.STANDARD_MODEL


@pytest.mark.parametrize("repo", [
    "Command-Center",
    "command_center",
    "commandcenter",
    "the-league",
    "command-center/",
    "FF-Weekly-Start-Sit/extra",
])
def test_near_miss_spellings_do_not_reach_contributor(repo):
    """A disclosure to a training tier cannot be withdrawn, so a
    near-miss must not be generous about what it thinks you meant."""
    assert muse_model.model_for(repo) == muse_model.STANDARD_MODEL


@pytest.mark.parametrize("repo", [
    "  The-League  ",
    "FF-Weekly-Start-Sit ",
    "\tcommand-center\n",
    " nateprich-projects/The-League\n",
])
def test_surrounding_whitespace_is_transport_not_a_different_repo(repo):
    """The runners pass a shell variable that can carry a newline;
    stripping it is deliberate, and is not the same as being generous
    about a misspelling."""
    assert muse_model.model_for(repo) == muse_model.CONTRIBUTOR_MODEL


def test_allowlist_holds_exactly_nates_decision():
    """The allowlist is an exposure decision, not an implementation
    detail; a silent addition is the failure this test exists for."""
    assert set(muse_model.CONTRIBUTOR_REPOS) == set(ALLOWED)


def test_rate_cards_cover_both_models():
    assert set(muse_model.RATE_CARDS) == {
        muse_model.CONTRIBUTOR_MODEL, muse_model.STANDARD_MODEL}
    for card in muse_model.RATE_CARDS.values():
        assert set(card) == {"input", "cached_input", "output"}
        assert all(isinstance(value, float) and value > 0
                   for value in card.values())


def test_the_two_cards_are_not_a_flat_multiple():
    """The correction on 2026-09-20 turned on exactly this: contributor
    discounts a cache read to 2% of a fresh token, standard to 12%."""
    contributor = muse_model.RATE_CARDS[muse_model.CONTRIBUTOR_MODEL]
    standard = muse_model.RATE_CARDS[muse_model.STANDARD_MODEL]
    assert contributor["cached_input"] / contributor["input"] == \
        pytest.approx(0.02)
    assert standard["cached_input"] / standard["input"] == pytest.approx(0.12)


@pytest.mark.parametrize("model,expected_key", [
    (muse_model.CONTRIBUTOR_MODEL, muse_model.CONTRIBUTOR_MODEL),
    (muse_model.STANDARD_MODEL, muse_model.STANDARD_MODEL),
    ("muse-spark-1.2-contributor", muse_model.STANDARD_MODEL),
    ("", muse_model.STANDARD_MODEL),
    (None, muse_model.STANDARD_MODEL),
])
def test_rate_card_falls_back_to_the_dearer_card(model, expected_key):
    """Over-reading stops the lanes early; under-reading walks them into
    the provider's refusal. The gate wants the expensive direction."""
    assert muse_model.rate_card(model) is \
        muse_model.RATE_CARDS[expected_key]


def test_cli_prints_the_model_for_an_allowlisted_repo(capsys):
    assert muse_model.main(["model", "--repo",
                            "nateprich-projects/The-League"]) == 0
    assert capsys.readouterr().out.strip() == muse_model.CONTRIBUTOR_MODEL


def test_cli_prints_the_standard_model_for_an_unknown_repo(capsys):
    """The runners fail closed on a bad resolution, so the CLI must not
    exit non-zero for a repo it simply does not know."""
    assert muse_model.main(["model", "--repo", "who-knows"]) == 0
    assert capsys.readouterr().out.strip() == muse_model.STANDARD_MODEL


def test_cli_prints_the_standard_model_for_an_excluded_repo(capsys):
    assert muse_model.main(["model", "--repo", "workbench"]) == 0
    assert capsys.readouterr().out.strip() == muse_model.STANDARD_MODEL


def test_cli_requires_a_repo():
    with pytest.raises(SystemExit):
        muse_model.main(["model"])
