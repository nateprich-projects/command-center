"""The implement packet carries the target repo's own rules (#1256)."""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import implement, shape  # noqa: E402


REPO = "nateprich-projects/The-League"


def test_a_present_agents_md_is_carried_verbatim(monkeypatch):
    monkeypatch.setattr(
        shape, "fetch_repo_text", lambda repo, path: ("# Rules\n\nBe kind.\n", False)
    )

    text, missing, truncated = implement.fetch_agents_md(REPO)

    assert text == "# Rules\n\nBe kind.\n"
    assert missing is False
    assert truncated is False


def test_a_missing_agents_md_is_an_absent_fact_not_an_error(monkeypatch):
    """A repo without one is a repo without one; there is nothing to retry."""
    monkeypatch.setattr(shape, "fetch_repo_text", lambda repo, path: ("", True))

    text, missing, truncated = implement.fetch_agents_md(REPO)

    assert (text, missing, truncated) == ("", True, False)


def test_an_oversized_agents_md_is_cut_with_a_visible_marker(monkeypatch):
    """A reader must be able to tell the rules from the first part of them."""
    long_text = "x" * (implement.MAX_AGENTS_MD_CHARS + 500)
    monkeypatch.setattr(
        shape, "fetch_repo_text", lambda repo, path: (long_text, False)
    )

    text, missing, truncated = implement.fetch_agents_md(REPO)

    assert truncated is True
    assert missing is False
    assert text.startswith("x" * 100)
    assert "truncated after {} characters".format(
        implement.MAX_AGENTS_MD_CHARS
    ) in text
    assert len(text) < len(long_text)


def test_a_read_error_still_raises(monkeypatch):
    """Missing is a fact; unreachable is not, and must not read as missing."""
    def explode(repo, path):
        raise implement.funnel.GitHubError("HTTP 502")

    monkeypatch.setattr(shape, "fetch_repo_text", explode)

    with pytest.raises(implement.funnel.GitHubError):
        implement.fetch_agents_md(REPO)


def test_the_packet_carries_the_text_and_its_two_flags():
    packet = implement.build_packet(
        repo=REPO, ticket={"ref": "{}#1".format(REPO)}, plan=None,
        verdict={}, prior_run=None,
        agents_md="# Rules\n", agents_md_missing=False,
        agents_md_truncated=False,
    )

    assert packet["agents_md"] == "# Rules\n"
    assert packet["agents_md_missing"] is False
    assert packet["agents_md_truncated"] is False


def test_the_packet_defaults_to_an_absent_agents_md():
    """Every existing caller keeps working, and reads as "no text carried"."""
    packet = implement.build_packet(
        repo=REPO, ticket={}, plan=None, verdict={}, prior_run=None,
    )

    assert packet["agents_md"] == ""
    assert packet["agents_md_missing"] is False
    assert packet["agents_md_truncated"] is False


def test_collect_reads_the_tickets_own_repo(monkeypatch):
    """A member-repo ticket's rules live in that repo, not in command-center."""
    seen = []

    monkeypatch.setattr(implement.funnel, "resolve_repo", lambda repo: repo)
    monkeypatch.setattr(
        implement, "fetch_ticket",
        lambda repo, number: {"ref": "{}#{}".format(repo, number),
                              "parent": {"ref": "{}#1".format(REPO)}},
    )
    monkeypatch.setattr(implement, "fetch_plan", lambda repo, ticket: None)
    monkeypatch.setattr(
        implement, "fetch_verdict_blocking", lambda repo, number: {}
    )
    monkeypatch.setattr(
        implement, "fetch_prior_run", lambda number, agent: None
    )
    monkeypatch.setattr(implement, "parent_repo", lambda repo, ticket: REPO)

    def record(repo):
        seen.append(repo)
        return "# Rules\n", False, False

    monkeypatch.setattr(implement, "fetch_agents_md", record)

    packet = implement.collect(REPO, 2)

    assert seen == [REPO]
    assert packet["agents_md"] == "# Rules\n"
