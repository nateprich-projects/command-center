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

#: Spelled out rather than imported. Every other assertion here compares
#: against `muse_model.CONTRIBUTOR_MODEL` and `muse_model.STANDARD_MODEL`,
#: which means transposing those two constants would leave the whole file
#: green while routing the excluded repos to the training tier. These two
#: literals and the tests below are what make the rest of the file mean
#: anything.
CONTRIBUTOR_ID = "muse-spark-1.3-contributor"
STANDARD_ID = "muse-spark-1.3"


def test_the_model_ids_are_the_ones_meta_publishes():
    """A typo here passes every other test and fails inside muse exec."""
    assert muse_model.CONTRIBUTOR_MODEL == CONTRIBUTOR_ID
    assert muse_model.STANDARD_MODEL == STANDARD_ID


@pytest.mark.parametrize("repo", ALLOWED)
def test_cleared_repos_route_to_the_literal_contributor_id(repo):
    assert muse_model.model_for(repo) == CONTRIBUTOR_ID


@pytest.mark.parametrize("repo", EXCLUDED)
def test_excluded_repos_route_to_the_literal_private_id(repo):
    """The three repos Nate kept off Discounted Services on 2026-09-22.
    If this ever passes while naming the contributor id, personal data
    is going somewhere he declined to send it."""
    assert muse_model.model_for(repo) == STANDARD_ID
    assert muse_model.model_for(repo) != CONTRIBUTOR_ID


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
    "/Users/nateprich/.claude/command-center",
    "/Users/nateprich/.claude/command-center-run",
    "~/.claude/command-center",
    "./command-center",
    "a/b/c/The-League",
    "workbench/The-League",
    "https://github.com/nateprich-projects/The-League",
])
def test_a_filesystem_path_is_not_a_repository(repo):
    """Both runners hold a `REPO` variable that is a checkout path, next
    to the `BEGIN_REPO` that is an `owner/name`. Passing the wrong one
    must not route every repo, including the excluded ones, to the
    training tier."""
    assert muse_model.model_for(repo) == STANDARD_ID


@pytest.mark.parametrize("repo", [
    "someone-else/The-League",
    "braven112/command-center",
    "forks-r-us/FF-Weekly-Start-Sit",
    "/The-League",
    " /The-League",
])
def test_another_owners_repo_is_not_on_the_allowlist(repo):
    """A fork or a collaborator's copy shares the name and nothing else."""
    assert muse_model.model_for(repo) == STANDARD_ID


@pytest.mark.parametrize("owner", sorted(muse_model.KNOWN_OWNERS))
def test_both_known_owners_resolve(owner):
    assert muse_model.model_for(owner + "/The-League") == CONTRIBUTOR_ID


@pytest.mark.parametrize("value,expected", [
    ("command-center", "command-center"),
    ("nateprich-projects/command-center", "command-center"),
    (" nateprich-projects / The-League ", "The-League"),
    ("/Users/nateprich/command-center", ""),
    ("stranger/command-center", ""),
    ("", ""),
    (None, ""),
])
def test_repo_name_is_tested_directly(value, expected):
    """It is public and documented, so it gets its own assertions rather
    than being reached only through model_for."""
    assert muse_model.repo_name(value) == expected


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
        assert all(value > 0
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
    assert muse_model.rate_card(model) == \
        muse_model.RATE_CARDS[expected_key]


def test_rate_card_hands_back_a_copy():
    """A spending gate must not be able to re-price the module."""
    card = muse_model.rate_card(muse_model.STANDARD_MODEL)
    card["input"] = 0.0
    assert muse_model.rate_card(muse_model.STANDARD_MODEL)["input"] == 1.25
    assert muse_model.RATE_CARDS[muse_model.STANDARD_MODEL]["input"] == 1.25


def test_the_standard_card_is_the_one_checked_against_the_account():
    """The 2026-09-20 correction turned on these three numbers; the
    $200 weekly ceiling in usage.py is calibrated against them."""
    assert muse_model.RATE_CARDS[STANDARD_ID] == {
        "input": 1.25, "cached_input": 0.15, "output": 4.25}


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
