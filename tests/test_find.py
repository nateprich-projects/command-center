"""Reference resolution refuses ambiguous bare issue numbers."""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def item(repo: str, number: int, url: str | None = None) -> funnel.Item:
    return funnel.Item(
        repo=repo,
        number=number,
        title="issue {}".format(number),
        url=url or "https://github.com/{}/issues/{}".format(repo, number),
        state="OPEN",
    )


def test_ambiguous_bare_number_refuses_and_names_every_match():
    one = item("owner/one", 4)
    two = item("owner/two", 4)

    with pytest.raises(funnel.GitHubError) as exc:
        funnel.find([one, two], "4")

    message = str(exc.value)
    assert "ambiguous" in message.lower()
    assert one.ref in message
    assert two.ref in message


def test_full_ref_wins_over_a_bare_number_twin():
    twin = item("owner/one", 4)
    target = item("owner/two", 4)

    assert funnel.find([twin, target], target.ref) is target


def test_url_ref_still_resolves():
    target = item("owner/two", 4)

    assert funnel.find([target], target.url) is target


def test_unique_bare_number_still_resolves():
    target = item("owner/two", 4)

    assert funnel.find([target], "4") is target


def test_missing_ref_keeps_existing_error():
    with pytest.raises(funnel.GitHubError, match=r"no funnel item matches 99"):
        funnel.find([item("owner/two", 4)], "99")
