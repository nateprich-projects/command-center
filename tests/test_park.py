"""The explicit, reasoned transition from an active project to Parked."""

from __future__ import annotations

import math
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def _no_github(monkeypatch):
    calls = []

    def load_items():
        calls.append("load items")
        return []

    monkeypatch.setattr(funnel, "load_items", load_items)
    return calls


def _assert_reason_refused(monkeypatch, capsys, argv):
    calls = _no_github(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        funnel.main(argv)

    assert exc.value.code != 0
    assert "reason" in capsys.readouterr().err.lower()
    assert calls == []


def _assert_wake_date_refused(monkeypatch, capsys, wake_date):
    calls = _no_github(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        funnel.main([
            "park", "42", "--reason", "Not now",
            "--wake-date", wake_date,
        ])

    assert exc.value.code != 0
    assert "wake date" in capsys.readouterr().err.lower()
    assert calls == []


def test_park_refuses_an_absent_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42"])


def test_park_refuses_an_empty_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42", "--reason", ""])


def test_park_refuses_a_whitespace_reason_without_calling_github(monkeypatch, capsys):
    _assert_reason_refused(monkeypatch, capsys, ["park", "42", "--reason", "   "])


@pytest.mark.parametrize("wake_date", ["not-a-date", "2026-02-30"])
def test_park_refuses_a_malformed_wake_date_before_calling_github(
    monkeypatch, capsys, wake_date
):
    _assert_wake_date_refused(monkeypatch, capsys, wake_date)


def test_park_refuses_a_past_wake_date_before_calling_github(monkeypatch, capsys):
    yesterday = (funnel._block_condition_date() - timedelta(days=1)).isoformat()
    _assert_wake_date_refused(monkeypatch, capsys, yesterday)


def test_park_sets_status_closes_not_planned_then_posts_the_reason(monkeypatch):
    target = funnel.Item(
        repo="nateprich/beta",
        number=42,
        title="A project to stop",
        url="https://github.com/nateprich/beta/issues/42",
        state="OPEN",
        status="Ready",
        item_id="project-item-42",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [target])
    events = []

    def graphql(query, **variables):
        if query == funnel.SET_FIELD:
            events.append(("set field", variables))
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": target.item_id}}}
        events.append(("read options", variables))
        return {"node": {"options": [{"id": "parked-option", "name": "Parked"}]}}

    def run(args, capture_output, text=True):
        events.append((args[2], tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "park", "42", "--reason", "No longer worth the cost",
        "--run", "run-42", "--agent", "claude",
    ]) == 0

    assert [event[0] for event in events] == [
        "read options", "set field", "close", "comment"
    ]
    assert events[1][1] == {
        "project": funnel.PROJECT_ID,
        "item": "project-item-42",
        "field": funnel.STATUS_FIELD_ID,
        "option": "parked-option",
    }
    assert events[2][1] == (
        "gh", "issue", "close", "42", "--repo", "nateprich/beta",
        "--reason", "not planned",
    )
    assert events[3][1][:6] == (
        "gh", "issue", "comment", "42", "--repo", "nateprich/beta",
    )
    posted = events[3][1][-1]
    assert funnel._visible_comment(posted) == (
        funnel.PARK_COMMENT_PREFIX + "No longer worth the cost"
    )
    provenance = funnel.parse_provenance(posted)
    assert posted == funnel.append_provenance(
        funnel.PARK_COMMENT_PREFIX + "No longer worth the cost",
        "nate-relayed", at=datetime.fromisoformat(provenance["at"]),
        run="run-42", agent="claude",
    )
    assert funnel.render_voice(posted) == "Nate (relayed by claude)"
    assert funnel.parse_provenance(posted)["run"] == "run-42"


def test_park_with_wake_date_records_and_reads_status_and_reason(monkeypatch):
    target = funnel.Item(
        repo="nateprich/beta",
        number=42,
        title="A project to resume",
        url="https://github.com/nateprich/beta/issues/42",
        state="OPEN",
        status="Ready",
        item_id="project-item-42",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [target])
    events = []

    def graphql(query, **variables):
        if query == funnel.SET_FIELD:
            events.append(("set field", variables))
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": target.item_id}}}
        events.append(("read options", variables))
        return {"node": {"options": [{"id": "parked-option", "name": "Parked"}]}}

    def run(args, capture_output, text=True):
        events.append((args[2], tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    wake_date = funnel._block_condition_date() + timedelta(days=1)

    assert funnel.main([
        "park", "42", "--reason", "Resume after the study",
        "--wake-date", wake_date.isoformat(),
        "--run", "run-42", "--agent", "claude",
    ]) == 0

    posted = events[3][1][-1]
    parsed = funnel.parse_park_comment(posted)
    assert parsed == {
        "reason": "Resume after the study",
        "wake_date": wake_date,
        "prior_status": "Ready",
    }
    assert funnel._visible_comment(posted).splitlines() == [
        "{}date={} status=Ready".format(
            funnel.PARK_WAKE_PREFIX, wake_date.isoformat()
        ),
        "{}Resume after the study".format(funnel.PARK_COMMENT_PREFIX),
    ]

    monkeypatch.setattr(
        funnel, "_issue_comments", lambda item: [{"body": posted}]
    )
    brief_item = funnel._parked_item_json(target)
    assert brief_item["reason"] == "Resume after the study"
    assert brief_item["wake_date"] == wake_date.isoformat()
    assert brief_item["wake_status"] == "Ready"


def _wake_candidate():
    return funnel.Item(
        repo="nateprich/beta",
        number=43,
        title="A project to resume",
        url="https://github.com/nateprich/beta/issues/43",
        state="CLOSED",
        state_reason="NOT_PLANNED",
        status="Parked",
        klass="New",
        children_total=1,
        children_done=1,
        item_id="project-item-43",
    )


def _wake_comment(wake_date, status="Building"):
    status_line = " status={}".format(status) if status is not None else ""
    return (
        "{}date={}{}\n{}Resume after the study".format(
            funnel.PARK_WAKE_PREFIX, wake_date, status_line,
            funnel.PARK_COMMENT_PREFIX,
        )
    )


_REPO_ALIAS = re.compile(
    r'(repo\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)'
)
_ISSUE_ALIAS = re.compile(r"(issue\d+): issue\(number: (\d+)\)")


def _answer_comment_tails(query, comments_by_ref, *, drop=()):
    """Answer one batched comment-tail query the way GitHub shapes it.

    The query is parsed rather than rebuilt, so the double answers whichever
    batch the funnel actually sent. Refs in ``drop`` are left out, which is
    the partial response a failed alias produces.
    """
    response = {"rateLimit": {"cost": 1, "remaining": 4999, "resetAt": "later"}}
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
        if ref in drop:
            continue
        tail = comments_by_ref.get(ref, [])
        response[repo_alias][issue_match.group(1)] = {
            "comments": {
                "nodes": tail[-funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE:]
            }
        }
    return response


def _no_full_read(current):
    raise AssertionError(
        "a complete comment read for {}".format(current.ref)
    )


def _wire_wake_writes(monkeypatch, item, comment, events, reads=None):
    comments = {item.ref: [{"body": comment}]}
    monkeypatch.setattr(funnel, "_issue_comments", _no_full_read)

    def run(args, capture_output, text=True):
        events.append(("gh", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def graphql(query, **variables):
        if "comments(last:" in query:
            if reads is not None:
                reads.append(query)
            return _answer_comment_tails(query, comments)
        events.append(("graphql", variables))
        return {
            "updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id}
            }
        }

    monkeypatch.setattr(funnel, "_run_gh", run)
    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "building-option")
    monkeypatch.setattr(funnel, "live_issue_state", lambda current: current.state)


def test_park_wake_stays_parked_before_the_utc_date(monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    item = _wake_candidate()
    comment = _wake_comment("2026-09-24")
    events = []
    _wire_wake_writes(monkeypatch, item, comment, events)

    assert funnel.reconcile_parked_wakes([item], now) == []

    assert item.status == "Parked"
    assert item.state == "CLOSED"
    assert events == []
    assert funnel.gate_question(item) is None
    assert funnel.awaiting_decision([item]) == []


@pytest.mark.parametrize("wake_date", ["2026-09-23", "2026-09-22"])
def test_park_wake_restores_prior_status_on_and_after_the_utc_date(
    monkeypatch, wake_date
):
    now = datetime(2026, 9, 23, 23, 59, tzinfo=timezone.utc)
    item = _wake_candidate()
    comment = _wake_comment(wake_date)
    events = []
    _wire_wake_writes(monkeypatch, item, comment, events)

    assert funnel.reconcile_parked_wakes([item], now) == [item.ref]

    assert [event[0] for event in events] == ["gh", "graphql"]
    assert events[0][1] == (
        "gh", "issue", "reopen", "43", "--repo", "nateprich/beta"
    )
    assert events[1][1] == {
        "project": funnel.PROJECT_ID,
        "item": item.item_id,
        "field": funnel.STATUS_FIELD_ID,
        "option": "building-option",
    }
    assert item.state == "OPEN"
    assert item.state_reason == "REOPENED"
    assert item.status == "Building"
    parsed = funnel.parse_park_comment(comment)
    assert parsed["reason"] == "Resume after the study"
    assert funnel.gate_question(item) == "Accept it?"


def test_park_wake_without_prior_status_fails_closed(monkeypatch):
    now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
    item = _wake_candidate()
    comment = _wake_comment("2026-09-22", status=None)
    events = []
    _wire_wake_writes(monkeypatch, item, comment, events)

    assert funnel.reconcile_parked_wakes([item], now) == []

    assert item.status == "Parked"
    assert item.state == "CLOSED"
    assert events == []


# --- Batched parked-wakes reads (#1592) ------------------------------------

def _parked(repo, number, state="CLOSED"):
    return funnel.Item(
        repo=repo,
        number=number,
        title="Parked project {}".format(number),
        url="https://github.com/{}/issues/{}".format(repo, number),
        state=state,
        state_reason="NOT_PLANNED" if state == "CLOSED" else None,
        status="Parked",
        klass="Improve",
        item_id="project-item-{}-{}".format(repo.rsplit("/", 1)[1], number),
    )


def _plain_park_comment():
    return "{}Not now".format(funnel.PARK_COMMENT_PREFIX)


def _chatter(count):
    return [{"body": "Progress note {}".format(n)} for n in range(count)]


def _wire_batched_wakes(monkeypatch, items, comments_by_ref, *, drop=(),
                        full_reads=None):
    """Serve comment tails through GraphQL and record every write."""
    reads, events = [], []

    def graphql(query, **variables):
        if "comments(last:" in query:
            reads.append(query)
            return _answer_comment_tails(query, comments_by_ref, drop=drop)
        events.append(("graphql", variables["item"]))
        return {
            "updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": variables["item"]}
            }
        }

    def run(args, capture_output, text=True):
        events.append(("gh", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def complete_read(current):
        if full_reads is None:
            return _no_full_read(current)
        full_reads.append(current.ref)
        return comments_by_ref[current.ref]

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel, "_run_gh", run)
    monkeypatch.setattr(funnel, "_issue_comments", complete_read)
    monkeypatch.setattr(funnel, "_option_id", lambda *args: "status-option")
    return reads, events


def _forty_two_parked():
    """The 2026-09-26 Parked set's shape: 42 items across member repos.

    Numbers and repos are synthetic (member repos are private); the shape is
    the recorded one: most parks carry no wake date, a few are dated in the
    future, and a handful are due.
    """
    repos = [
        "nateprich-projects/command-center",
        "nateprich-projects/alpha",
        "nateprich-projects/beta",
    ]
    items, comments, due = [], {}, []
    for index in range(42):
        item = _parked(repos[index % 3], 100 + index,
                       state="OPEN" if index % 7 == 0 else "CLOSED")
        items.append(item)
        if index % 10 == 3:
            comments[item.ref] = [{"body": _wake_comment("2026-09-26", "Ready")}]
            due.append(item.ref)
        elif index % 10 == 6:
            comments[item.ref] = [{"body": _wake_comment("2026-10-05", "Ready")}]
        elif index == 41:
            comments[item.ref] = [{"body": _wake_comment("2027-01-04", "Shaped")}]
        else:
            comments[item.ref] = _chatter(index % 4) + [
                {"body": _plain_park_comment()}
            ]
    return items, comments, sorted(due, key=lambda ref: (
        ref.split("#")[0], int(ref.split("#")[1])
    ))


@pytest.mark.parametrize("batch_size", [None, 10])
def test_parked_wakes_batch_the_recorded_42_item_shape(monkeypatch, batch_size):
    if batch_size is not None:
        monkeypatch.setattr(
            funnel, "CLOSED_ITSELF_COMMENT_BATCH_SIZE", batch_size
        )
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    items, comments, due = _forty_two_parked()
    reads, events = _wire_batched_wakes(monkeypatch, items, comments)

    woke = funnel.reconcile_parked_wakes(items, now)

    assert len(reads) == math.ceil(
        42 / funnel.CLOSED_ITSELF_COMMENT_BATCH_SIZE
    )
    assert woke == due
    assert len(due) == 4
    woken = {item.ref: item for item in items if item.ref in due}
    assert all(item.status == "Ready" for item in woken.values())
    assert all(item.state == "OPEN" for item in woken.values())
    assert all(
        item.status == "Parked" for item in items if item.ref not in due
    )
    written = [target for kind, target in events if kind == "graphql"]
    assert written == [woken[ref].item_id for ref in due]


def test_parked_wakes_partial_batch_fails_closed_with_no_writes(monkeypatch):
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    due = _parked("nateprich/beta", 43)
    missing = _parked("nateprich/beta", 44)
    comments = {
        due.ref: [{"body": _wake_comment("2026-09-25", "Ready")}],
        missing.ref: [{"body": _wake_comment("2026-09-25", "Ready")}],
    }
    reads, events = _wire_batched_wakes(
        monkeypatch, [due, missing], comments, drop={missing.ref}
    )

    with pytest.raises(funnel.GitHubError, match="could not read comments"):
        funnel.reconcile_parked_wakes([due, missing], now)

    assert len(reads) == 1
    assert events == []
    assert due.status == "Parked" and due.state == "CLOSED"


def test_parked_wakes_failed_batch_fails_closed_with_no_writes(monkeypatch):
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    item = _parked("nateprich/beta", 43)
    _, events = _wire_batched_wakes(monkeypatch, [item], {})

    def refused(query, **variables):
        raise funnel.GitHubError("GraphQL: secondary rate limit")

    monkeypatch.setattr(funnel, "gh_graphql", refused)

    with pytest.raises(funnel.GitHubError, match="secondary rate limit"):
        funnel.reconcile_parked_wakes([item], now)

    assert events == []
    assert item.status == "Parked"


def test_parked_wakes_without_a_wake_date_stay_parked(monkeypatch):
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    item = _parked("nateprich/beta", 43)
    comments = {item.ref: [{"body": _plain_park_comment()}]}
    reads, events = _wire_batched_wakes(monkeypatch, [item], comments)

    assert funnel.reconcile_parked_wakes([item], now) == []

    assert len(reads) == 1
    assert events == []
    assert item.status == "Parked"
    assert funnel.parse_park_comment(_plain_park_comment()) == {
        "reason": "Not now", "wake_date": None, "prior_status": None,
    }


@pytest.mark.parametrize(
    "today, wakes", [("2027-01-03", False), ("2027-01-04", True)]
)
def test_parked_wake_date_survives_the_batch_verbatim(
    monkeypatch, today, wakes
):
    now = datetime.fromisoformat(today + "T23:59:00+00:00")
    item = _parked("nateprich/beta", 43)
    comments = {item.ref: [{"body": _wake_comment("2027-01-04", "Shaped")}]}
    _, events = _wire_batched_wakes(monkeypatch, [item], comments)

    woke = funnel.reconcile_parked_wakes([item], now)

    assert woke == ([item.ref] if wakes else [])
    assert item.status == ("Shaped" if wakes else "Parked")
    assert bool(events) is wakes


def test_parked_wakes_with_no_parked_items_issue_no_query(monkeypatch):
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    active = _parked("nateprich/beta", 43)
    active.status = "Building"
    reads, events = _wire_batched_wakes(monkeypatch, [active], {})

    assert funnel.reconcile_parked_wakes([], now) == []
    assert funnel.reconcile_parked_wakes([active], now) == []

    assert reads == []
    assert events == []


def test_parked_wakes_full_tail_without_a_header_reads_the_whole_thread(
    monkeypatch,
):
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    buried = _parked("nateprich/beta", 43)
    short = _parked("nateprich/beta", 44)
    size = funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE
    comments = {
        # The park comment is older than the newest `size` comments.
        buried.ref: [{"body": _wake_comment("2026-09-20", "Ready")}]
        + _chatter(size),
        # A short tail is the whole thread: no header means no wake date.
        short.ref: _chatter(size - 1),
    }
    full_reads = []
    reads, events = _wire_batched_wakes(
        monkeypatch, [buried, short], comments, full_reads=full_reads
    )

    assert funnel.reconcile_parked_wakes([buried, short], now) == [buried.ref]

    assert len(reads) == 1
    assert full_reads == [buried.ref]
    assert buried.status == "Ready"
    assert short.status == "Parked"


def test_parked_wakes_failed_complete_read_makes_no_writes(monkeypatch):
    now = datetime(2026, 9, 26, 15, 0, tzinfo=timezone.utc)
    due = _parked("nateprich/beta", 43)
    buried = _parked("nateprich/beta", 44)
    size = funnel.CLOSED_ITSELF_COMMENT_PAGE_SIZE
    comments = {
        due.ref: [{"body": _wake_comment("2026-09-25", "Ready")}],
        buried.ref: _chatter(size),
    }
    _, events = _wire_batched_wakes(monkeypatch, [due, buried], comments)

    def unreadable(current):
        raise funnel.GitHubError(
            "could not read comments for {}".format(current.ref)
        )

    monkeypatch.setattr(funnel, "_issue_comments", unreadable)

    with pytest.raises(funnel.GitHubError, match=re.escape(buried.ref)):
        funnel.reconcile_parked_wakes([due, buried], now)

    assert events == []
    assert due.status == "Parked"
