"""Branch planting at implementation bind preserves unknown remote work."""

from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


REPO = "nateprich/command-center"
BASE_SHA = "a" * 40
BRANCH_SHA = "b" * 40


def _ok(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def _missing():
    return SimpleNamespace(
        returncode=1, stdout="", stderr="gh: HTTP 404: Not Found"
    )


def test_absent_ticket_branch_is_created_at_the_current_main_sha(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        if args == ["gh", "api", "repos/{}/git/ref/heads/ticket/42".format(REPO)]:
            return _missing()
        if args == ["gh", "api", "repos/{}/git/ref/heads/main".format(REPO)]:
            return _ok({
                "ref": "refs/heads/main",
                "object": {"sha": BASE_SHA},
            })
        if args[:4] == ["gh", "api", "-X", "POST"]:
            assert args[4] == "repos/{}/git/refs".format(REPO)
            assert args[6:] == [
                "ref=refs/heads/ticket/42", "-f", "sha=" + BASE_SHA
            ]
            return _ok({
                "ref": "refs/heads/ticket/42",
                "object": {"sha": BASE_SHA},
            })
        raise AssertionError("unexpected GitHub call: {!r}".format(args))

    monkeypatch.setattr(funnel, "_run_gh", run)

    assert funnel.ensure_ticket_branch(REPO, 42) == BASE_SHA
    assert [call[0][2] for call in calls[:2]] == [
        "repos/{}/git/ref/heads/ticket/42".format(REPO),
        "repos/{}/git/ref/heads/main".format(REPO),
    ]
    assert calls[2][0][3] == "POST"


def test_existing_ticket_branch_is_left_untouched(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(list(args))
        return _ok({
            "ref": "refs/heads/ticket/42",
            "object": {"sha": BRANCH_SHA},
        })

    monkeypatch.setattr(funnel, "_run_gh", run)

    assert funnel.ensure_ticket_branch(REPO, 42) == BRANCH_SHA
    assert calls == [[
        "gh", "api", "repos/{}/git/ref/heads/ticket/42".format(REPO)
    ]]


def test_failed_ticket_branch_push_is_an_error(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(list(args))
        if args == ["gh", "api", "repos/{}/git/ref/heads/ticket/42".format(REPO)]:
            return _missing()
        if args == ["gh", "api", "repos/{}/git/ref/heads/main".format(REPO)]:
            return _ok({
                "ref": "refs/heads/main",
                "object": {"sha": BASE_SHA},
            })
        if args[:4] == ["gh", "api", "-X", "POST"]:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="remote rejected the ref"
            )
        raise AssertionError("unexpected GitHub call: {!r}".format(args))

    monkeypatch.setattr(funnel, "_run_gh", run)

    with pytest.raises(funnel.GitHubError, match="could not push"):
        funnel.ensure_ticket_branch(REPO, 42)

    assert len(calls) == 4
    assert calls[-1] == [
        "gh", "api", "repos/{}/git/ref/heads/ticket/42".format(REPO)
    ]
