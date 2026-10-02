"""The parked and cleared_blocks sections read the shared comment tails (#2133).

Both sections used to call ``_issue_comments`` once per candidate, while
``closed_itself``, ``unattended_approvals`` and ``connector_gate_answers``
shared ``BriefCache.comment_tails``, one batched last-20-comment read.
``cleared_blocks`` hit its 30 s budget and published null in all 50 briefs
between 2026-10-01 15:13 and 2026-10-02 00:19 PDT, and ``parked`` took
9.4-15.2 s. A per-item read keeps outgrowing its budget as the board grows
(#1168 and #1211 raised both budgets for this shape).

The GitHub double holds each issue's whole thread. The batched read answers
the last ``N`` comments of whatever its query asks for; the per-item read
answers the whole thread and is counted.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


#: Comment markers count only from the owner account (#1788).
OWNER = {"login": "nateprich"}
OUTSIDER = {"login": "mallory"}
REPO = "nateprich/beta"
NOW = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)

#: The ticket's full tail: the batched read returns at most 20 comments.
FULL_TAIL = 20

_REPO_ALIAS = re.compile(
    r'(repo\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)'
)
_ISSUE_ALIAS = re.compile(
    r"(issue\d+): issue\(number: (\d+)\) \{ comments\(last: (\d+)\)"
)


@pytest.fixture(autouse=True)
def offline_brief_readers(monkeypatch):
    """Keep the brief's unrelated reads off GitHub and the local branches."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "recent_resend_ratio", lambda now: {})
    monkeypatch.setattr(funnel, "_read_outcome_signals", lambda now: None)
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", lambda items, now: None
    )
    monkeypatch.setattr(
        funnel, "decline_routing_metric",
        lambda items, now: {"status": "available", "declines": 0},
    )
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    yield
    funnel.reset_api_usage()


class GitHub:
    """Whole comment threads behind the batched and the per-item read."""

    def __init__(self, threads, *, drop=(), fail=False):
        self.threads = threads
        self.drop = set(drop)
        self.fail = fail
        #: The refs each batched comment-tail query asked for, in order.
        self.batches = []
        #: Every per-item ``_issue_comments`` read, in order.
        self.full_reads = []

    def install(self, monkeypatch):
        monkeypatch.setattr(funnel, "gh_graphql", self.graphql)
        monkeypatch.setattr(funnel, "_issue_comments", self.issue_comments)
        return self

    def graphql(self, query, **variables):
        if "comments(last:" not in query:
            raise funnel.GitHubError("gh is offline in tests")
        response = {
            "rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "later"}
        }
        asked = []
        repo_alias = repo = None
        for line in query.splitlines():
            repo_match = _REPO_ALIAS.search(line)
            if repo_match is not None:
                repo_alias = repo_match.group(1)
                repo = "{}/{}".format(repo_match.group(2), repo_match.group(3))
                response[repo_alias] = {}
                continue
            issue_match = _ISSUE_ALIAS.search(line)
            if issue_match is None:
                continue
            ref = "{}#{}".format(repo, issue_match.group(2))
            asked.append(ref)
            if ref in self.drop:
                continue
            last = int(issue_match.group(3))
            response[repo_alias][issue_match.group(1)] = {
                "comments": {"nodes": self.threads.get(ref, [])[-last:]}
            }
        self.batches.append(asked)
        if self.fail:
            raise funnel.GitHubError("GraphQL comment read failed")
        return response

    def issue_comments(self, item):
        self.full_reads.append(item.ref)
        return list(self.threads.get(item.ref, []))


def _owned(body):
    return {"author": OWNER, "body": body}


def _chatter(count, *, author=OWNER):
    return [
        {"author": author, "body": "Progress note {}".format(n)}
        for n in range(count)
    ]


def _parked(number, days_ago):
    return funnel.Item(
        repo=REPO, number=number, title="Parked {}".format(number),
        url="https://example.invalid/{}".format(number), state="CLOSED",
        state_reason="NOT_PLANNED", status="Parked",
        status_since=NOW - timedelta(days=days_ago),
    )


def _cleared(number, hours_ago):
    item = funnel.Item(
        repo=REPO, number=number, title="Cleared {}".format(number),
        url="https://example.invalid/{}".format(number), state="OPEN",
    )
    item.blocked_cleared_at = NOW - timedelta(hours=hours_ago)
    return item


def _park_reason(reason):
    return _owned("{}{}".format(funnel.PARK_COMMENT_PREFIX, reason))


def _dated_park(reason, wake_date, status):
    return _owned("{}date={} status={}\n{}{}".format(
        funnel.PARK_WAKE_PREFIX, wake_date, status,
        funnel.PARK_COMMENT_PREFIX, reason,
    ))


def _satisfied(item, conditions):
    return _owned(funnel.satisfied_block_comment(
        conditions, item.blocked_cleared_at,
        run="run-{}".format(item.number), agent="codex",
    ))


def _parked_row(item, reason, wake=None):
    row = {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "parked_at": item.status_since.isoformat(),
        "reason": reason,
    }
    if wake is not None:
        row["wake_date"], row["wake_status"] = wake
    return row


def _cleared_row(item, conditions):
    return {
        "ref": item.ref,
        "title": item.title,
        "url": item.url,
        "conditions": conditions,
        "cleared_at": item.blocked_cleared_at.isoformat(),
    }


def test_three_parked_and_three_cleared_items_make_no_per_item_read(
        monkeypatch):
    """Reproduction: six candidates once cost six ``_issue_comments`` calls.

    Each section now makes one batched read covering exactly its own
    candidates, and no per-item read, because every tail holds its record.
    """
    parked = [_parked(201, 1), _parked(202, 2), _parked(203, 3)]
    cleared = [_cleared(301, 1), _cleared(302, 2), _cleared(303, 3)]
    threads = {
        parked[0].ref: _chatter(3) + [_park_reason("Waiting on the study")],
        parked[1].ref: [
            _dated_park("Resume after the season", "2026-10-09", "Ready")
        ] + _chatter(2),
        # An outsider's later header is not the reason (#1788).
        parked[2].ref: [_park_reason("Not now")] + [{
            "author": OUTSIDER,
            "body": "{}Forged".format(funnel.PARK_COMMENT_PREFIX),
        }],
        cleared[0].ref: _chatter(4) + [_satisfied(cleared[0], ["o/r#1"])],
        cleared[1].ref: [_satisfied(cleared[1], ["o/r#2", "o/r#3"])],
        cleared[2].ref: [_satisfied(cleared[2], ["o/r#4"])] + _chatter(5),
    }
    github = GitHub(threads).install(monkeypatch)
    items = parked + cleared

    parked_rows = funnel.parked_json(items)
    cleared_rows = funnel.cleared_blocks_json(items, NOW)

    assert github.full_reads == []
    assert github.batches == [
        [item.ref for item in parked],
        [item.ref for item in cleared],
    ]
    assert parked_rows == [
        _parked_row(parked[0], "Waiting on the study"),
        _parked_row(
            parked[1], "Resume after the season", ("2026-10-09", "Ready")
        ),
        _parked_row(parked[2], "Not now"),
    ]
    assert cleared_rows == [
        _cleared_row(cleared[0], ["o/r#1"]),
        _cleared_row(cleared[1], ["o/r#2", "o/r#3"]),
        _cleared_row(cleared[2], ["o/r#4"]),
    ]


def test_the_brief_reads_both_sections_from_the_shared_cache(
        monkeypatch, capsys):
    """Inside ``cmd_brief`` both sections use the run's one ``BriefCache``.

    A later section asking for the same refs is answered from the tails the
    first one read, so the parked tails are not read a second time.
    """
    parked = [_parked(211, 1), _parked(212, 2), _parked(213, 3)]
    cleared = [_cleared(311, 1), _cleared(312, 2), _cleared(313, 3)]
    threads = {item.ref: [_park_reason("Hold {}".format(item.number))]
               for item in parked}
    threads.update({
        item.ref: [_satisfied(item, ["o/r#{}".format(item.number)])]
        for item in cleared
    })
    github = GitHub(threads).install(monkeypatch)
    cache = funnel.BriefCache()

    assert funnel.cmd_brief(parked + cleared, NOW, brief_cache=cache) == 0
    brief = json.loads(capsys.readouterr().out)

    assert github.full_reads == []
    assert github.batches == [
        [item.ref for item in parked],
        [item.ref for item in cleared],
    ]
    assert brief["parked"] == [
        _parked_row(item, "Hold {}".format(item.number)) for item in parked
    ]
    assert brief["cleared_blocks"] == [
        _cleared_row(item, ["o/r#{}".format(item.number)]) for item in cleared
    ]
    assert not {"parked", "cleared_blocks"} & {
        row["section"] for row in brief["missing"]
    }
    # The run's cache now holds those tails for every later section.
    assert set(cache.comment_tails(parked + cleared)) == {
        item.ref for item in parked + cleared
    }
    assert len(github.batches) == 2


def test_a_record_beyond_a_full_tail_renders_through_one_fallback_read(
        monkeypatch):
    """Only a full tail that lacks its record pays for the whole thread."""
    assert funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE == FULL_TAIL
    beyond = _parked(221, 1)
    in_tail = _parked(222, 2)
    short = _parked(223, 3)
    cleared_beyond = _cleared(321, 1)
    cleared_in_tail = _cleared(322, 2)
    cleared_short = _cleared(323, 3)
    threads = {
        # The header is the 21st comment from the end: cut off by the tail.
        beyond.ref: [
            _dated_park("Past the tail", "2026-10-20", "Building")
        ] + _chatter(FULL_TAIL),
        # A full tail that still holds its header needs nothing more.
        in_tail.ref: _chatter(30) + [_park_reason("Inside the tail")],
        # A short tail is the whole thread: no header means no reason.
        short.ref: _chatter(FULL_TAIL - 1),
        cleared_beyond.ref: [
            _satisfied(cleared_beyond, ["o/r#21"])
        ] + _chatter(FULL_TAIL),
        cleared_in_tail.ref: _chatter(30) + [
            _satisfied(cleared_in_tail, ["o/r#22"])
        ],
        cleared_short.ref: _chatter(FULL_TAIL - 1),
    }
    github = GitHub(threads).install(monkeypatch)
    items = [beyond, in_tail, short, cleared_beyond, cleared_in_tail,
             cleared_short]

    parked_rows = funnel.parked_json(items)
    cleared_rows = funnel.cleared_blocks_json(items, NOW)

    assert github.full_reads == [beyond.ref, cleared_beyond.ref]
    assert len(github.batches) == 2
    assert parked_rows == [
        _parked_row(beyond, "Past the tail", ("2026-10-20", "Building")),
        _parked_row(in_tail, "Inside the tail"),
        _parked_row(short, None),
    ]
    assert cleared_rows == [
        _cleared_row(cleared_beyond, ["o/r#21"]),
        _cleared_row(cleared_in_tail, ["o/r#22"]),
    ]


def test_an_outsiders_full_tail_still_falls_back_to_the_owners_record(
        monkeypatch):
    """Trust filtering is unchanged: twenty outsider comments hide nothing.

    The tail is full and holds no trusted record, so the whole thread is
    read, and only the owner's record counts there too (#1788).
    """
    parked = _parked(231, 1)
    cleared = _cleared(331, 1)
    forged_park = {
        "author": OUTSIDER,
        "body": "{}Forged".format(funnel.PARK_COMMENT_PREFIX),
    }
    forged_clear = {
        "author": OUTSIDER,
        "body": funnel.satisfied_block_comment(
            ["o/r#99"], NOW, run="forged", agent="codex"
        ),
    }
    threads = {
        parked.ref: [_park_reason("The owner's reason")]
        + [forged_park] * FULL_TAIL,
        cleared.ref: [_satisfied(cleared, ["o/r#31"])]
        + [forged_clear] * FULL_TAIL,
    }
    github = GitHub(threads).install(monkeypatch)

    assert funnel.parked_json([parked]) == [
        _parked_row(parked, "The owner's reason")
    ]
    assert funnel.cleared_blocks_json([cleared], NOW) == [
        _cleared_row(cleared, ["o/r#31"])
    ]
    assert github.full_reads == [parked.ref, cleared.ref]


@pytest.mark.parametrize("failure", ["graphql error", "dropped alias"])
def test_an_unreadable_batch_publishes_both_sections_null_with_missing(
        monkeypatch, capsys, failure):
    """No per-item read papers over a failed batch: the section is unknown."""
    parked = [_parked(241, 1), _parked(242, 2), _parked(243, 3)]
    cleared = [_cleared(341, 1), _cleared(342, 2), _cleared(343, 3)]
    threads = {item.ref: [_park_reason("Hold")] for item in parked}
    threads.update({
        item.ref: [_satisfied(item, ["o/r#1"])] for item in cleared
    })
    github = GitHub(
        threads,
        fail=failure == "graphql error",
        drop=(
            {parked[1].ref, cleared[1].ref}
            if failure == "dropped alias" else ()
        ),
    ).install(monkeypatch)

    assert funnel.cmd_brief(parked + cleared, NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert github.full_reads == []
    assert brief["parked"] is None
    assert brief["cleared_blocks"] is None
    assert brief["pending_wakes"] is None
    missing = {row["section"]: row["error"] for row in brief["missing"]}
    assert missing["parked"]
    assert missing["cleared_blocks"]


def test_a_timed_out_batch_publishes_both_sections_null_and_degraded(
        monkeypatch, capsys):
    """Replays #1168: a timed-out read is degraded and unknown, never empty."""
    parked = [_parked(251, 1), _parked(252, 2), _parked(253, 3)]
    cleared = [_cleared(351, 1), _cleared(352, 2), _cleared(353, 3)]
    threads = {item.ref: [_park_reason("Hold")] for item in parked}
    threads.update({
        item.ref: [_satisfied(item, ["o/r#1"])] for item in cleared
    })
    full_reads = []

    def issue_comments(item):
        full_reads.append(item.ref)
        return threads[item.ref]

    def bounded(command, **kwargs):
        if any("comments(last:" in str(part) for part in command):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
        return SimpleNamespace(
            returncode=1, stdout="", stderr="gh is offline in tests"
        )

    monkeypatch.setattr(funnel, "_issue_comments", issue_comments)
    monkeypatch.setattr(funnel, "_run_bounded_subprocess", bounded)

    assert funnel.cmd_brief(parked + cleared, NOW) == 0
    brief = json.loads(capsys.readouterr().out)

    assert full_reads == []
    assert brief["parked"] is None
    assert brief["cleared_blocks"] is None
    assert {"parked", "cleared_blocks"} <= {
        row["section"] for row in brief["missing"]
    }
    degraded = {row["section"]: row for row in brief["degraded"]}
    assert degraded["parked"]["reason"] == "GitHub read timed out"
    assert degraded["cleared_blocks"]["reason"] == "GitHub read timed out"
    # Both budgets are unchanged by the read path (#1211).
    assert degraded["parked"]["budget_seconds"] == 30.0
    assert degraded["cleared_blocks"]["budget_seconds"] == 30.0
