"""A sent-back Ideas parent with only closed children is shapeable (#2524).

#2078 was sent back to Ideas with ten closed sub-issues kept as evidence,
and the picker skipped it forever because ``unshapeable_reason`` counted
those closed children as children. The exemption is narrow: an Ideas row
with no open sub-issue that carries the #2460 send-back record on the
parent itself. These fixtures hold the picker and the apply fresh-read to
the same outcomes on each case (#2139).
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import usage  # noqa: E402
from engine import shape  # noqa: E402
from test_engine_shape import (  # noqa: E402  (shared fixture harness)
    NOW,
    answer,
    gh_calls,
    idea,
    stub_fresh_project_row,
    stub_gh,
)

REASON = "The plan needs a clearer treatment of migration risk"
INSTRUCTION = "Nate asked me to send this plan back to Ideas for that revision."


@pytest.fixture(autouse=True)
def canonical_field_writes(monkeypatch):
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: None,
    )


def send_back_body(voice="nate-relayed"):
    """The exact comment body ``cmd_send_back`` posts on the parent."""
    return funnel.append_provenance(
        funnel.SEND_BACK_COMMENT_PREFIX + REASON, voice, at=NOW,
        run="send-back-run", agent="muse", instruction=INSTRUCTION)


def comment(body, login="nateprich"):
    return {"author": {"login": login}, "body": body}


def test_unshapeable_reason_releases_sent_back_all_closed_parent():
    """Reproduction: this row read ``with-children`` before the exemption."""
    assert funnel.unshapeable_reason(
        "OPEN", "Ideas", 10, 10, True) is None


@pytest.mark.parametrize("total,done,sent_back", [
    (10, 9, True),  # any open child stays refused, marker or not
    (10, 10, False),  # closed children without provenance stay refused
    (2, 0, True),  # all open with a marker stays refused
    (1, 1, False),  # a single kept child without provenance stays refused
])
def test_unshapeable_reason_keeps_refusing_beyond_the_exemption(
        total, done, sent_back):
    assert funnel.unshapeable_reason(
        "OPEN", "Ideas", total, done, sent_back) == "with-children"


def test_unshapeable_reason_fails_closed_on_missing_inputs():
    assert funnel.unshapeable_reason("OPEN", "Ideas", 10) == "with-children"
    assert funnel.unshapeable_reason(None, "Ideas", 10, 10, True) == "not-open"
    assert funnel.unshapeable_reason(
        "OPEN", "Shaped", 10, 10, True) == "status-changed"


def test_has_send_back_marker_reads_only_a_nate_voiced_parent_record():
    assert funnel.has_send_back_marker([comment(send_back_body())]) is True
    # An outsider copying the prefix lends it no authority (#1788).
    assert funnel.has_send_back_marker(
        [comment(send_back_body(), login="mallory")]) is False
    # An agent-voiced comment with the prefix is not a send-back.
    assert funnel.has_send_back_marker(
        [comment(send_back_body(voice="agent"))]) is False
    # The prefix without provenance reads as absent.
    assert funnel.has_send_back_marker(
        [comment(funnel.SEND_BACK_COMMENT_PREFIX + REASON)]) is False
    assert funnel.has_send_back_marker(
        [comment("ordinary discussion")]) is False
    assert funnel.has_send_back_marker(None) is False
    assert funnel.has_send_back_marker("Send-back to Ideas: prose") is False


def test_sent_back_to_ideas_fails_closed_on_an_unreadable_thread(
        monkeypatch):
    item = idea(42, children_total=10, children_done=10)

    def fail(item):
        raise funnel.GitHubError("could not read comments for {}".format(
            item.ref))

    monkeypatch.setattr(funnel, "_issue_comments", fail)
    assert funnel.sent_back_to_ideas(item) is False


def test_sent_back_to_ideas_honours_a_caller_held_thread(monkeypatch):
    item = idea(42, children_total=10, children_done=10)

    def fail(item):
        raise AssertionError("must not read GitHub for a held thread")

    monkeypatch.setattr(funnel, "_issue_comments", fail)
    assert funnel.sent_back_to_ideas(
        item, [comment(send_back_body())]) is True
    assert funnel.sent_back_to_ideas(item, []) is False


#: The ticket's fixture matrix: (children_total, children_done, thread).
MATRIX = {
    # sent-back plus all-closed is shapeable again.
    "sent-back-all-closed": (10, 10, [comment(send_back_body())]),
    # any open child stays refused, marker or not.
    "any-open-with-marker": (10, 9, [comment(send_back_body())]),
    # closed children without provenance stay refused.
    "all-closed-no-marker": (10, 10, [comment("ordinary discussion")]),
}


@pytest.mark.parametrize("case", sorted(MATRIX))
def test_picker_and_fresh_read_agree_on_sent_back_fixtures(
        monkeypatch, capsys, case):
    """#2139: the picker offers exactly the rows the fresh-read proceeds on."""
    total, done, thread = MATRIX[case]
    monkeypatch.setattr(funnel, "_issue_comments", lambda item: thread)
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    offered = funnel.shapeable_idea(
        [idea(42, children_total=total, children_done=done)], None, {}) is not None

    snapshot = idea(42)
    fresh = idea(42, children_total=total, children_done=done)
    calls = stub_gh(monkeypatch, snapshot)
    stub_fresh_project_row(monkeypatch, fresh)
    assert shape.apply_shape(
        [snapshot], NOW, snapshot.ref, answer(),
        run="shape-run", agent="muse") == 0
    proceeded = bool(gh_calls(calls, "gh", "issue", "edit"))

    assert offered == proceeded
    assert offered is (case == "sent-back-all-closed")
    if not offered:
        assert "reason=with-children" in capsys.readouterr().out


def test_picker_and_fresh_read_agree_when_the_thread_is_unreadable(
        monkeypatch, capsys):
    """Missing provenance fails closed to the refusal on both paths."""
    def fail(item):
        raise funnel.GitHubError("could not read comments for {}".format(
            item.ref))

    monkeypatch.setattr(funnel, "_issue_comments", fail)
    monkeypatch.setattr(usage, "shaping_allowed", lambda reading: True)

    offered = funnel.shapeable_idea(
        [idea(42, children_total=10, children_done=10)], None, {}) is not None

    snapshot = idea(42)
    fresh = idea(42, children_total=10, children_done=10)
    calls = stub_gh(monkeypatch, snapshot)
    stub_fresh_project_row(monkeypatch, fresh)
    assert shape.apply_shape(
        [snapshot], NOW, snapshot.ref, answer(),
        run="shape-run", agent="muse") == 0
    proceeded = bool(gh_calls(calls, "gh", "issue", "edit"))

    assert offered is False
    assert proceeded is False
    assert "reason=with-children" in capsys.readouterr().out
