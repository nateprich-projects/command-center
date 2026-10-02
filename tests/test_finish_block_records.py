"""Implement finishes post their record before labelling a ticket (#2168).

``_load_block_comment`` reads a blocked ticket's newest record as its current
block. A finish that labels the ticket before posting its own record leaves an
earlier episode's record current for as long as the comment is missing, and
for good when the comment fails; ``clear_satisfied_blocks`` can then lift the
new block on the old record's condition. These tests hold the writers to
comment first, then Needs, label and edge, and read every body they post back
through funnel's real reader with only ``gh`` stubbed (#1748).

The reader is the one on the branch: whichever record it makes current, the
writer's own record is the newest one here, so these hold under a reader that
lets the newest record win (#2166) as well as under today's.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import funnel  # noqa: E402
from engine import implement  # noqa: E402
from test_engine_implement import (  # noqa: E402  (shared fixture harness)
    ACCEPT_BODY_CONFLICT_REASON,
    LIVE_1453_UNSATISFIABLE_ACCEPTANCE,
    LIVE_1497_PENDING_GATE_ANSWER,
    REPO,
    blocked,
    make_clone,
    ticket,
)

#: A decline with no condition the funnel can clear: the label path.
UNKNOWN_REASON = "The account setting still needs a human decision."
#: The human step these fixtures file is #43 under parent #7.
STEP_NUMBER = 43
OLDER_AT = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


def _stamp(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _owner_record(body, run="run-earlier"):
    """An earlier episode's record as the runner posted it."""
    return funnel.append_provenance(
        body, "agent", at=OLDER_AT, run=run, agent="codex")


#: Earlier records of another kind than the one each writer posts.
OLDER_THAN_A_DECLINE = {
    "human step": "**Blocked on #40:** Complete the human step before "
                  "resuming this ticket.",
    "event": "**Blocked until event:**\n```json\n" + json.dumps({
        "agent": "codex", "job": "muse-review", "outcome": "errored",
        "after": "2026-10-01T00:00:00Z"}, indent=2) + "\n```",
    "date": "**Blocked until 2026-10-09:** Wait for the provider reset.",
    "decision": "**Needs a decision:** Which repository owns this?",
}
OLDER_THAN_A_HUMAN_STEP = {
    "decline": "**Declined:** an earlier run's reason",
    "decision": "**Needs a decision:** Which repository owns this?",
    "date": "**Blocked until 2026-10-09:** Wait for the provider reset.",
}


class FakeTicket:
    """One ticket's comments and labels behind ``funnel._run_gh``.

    The real writers (``post_agent_comment``, ``mark_ticket_blocked``,
    ``close_declined_defer_note_proof``) and the real reader
    (``_load_block_comment``) all reach GitHub through ``_run_gh``; each
    posted comment gets the next minute as its ``createdAt``. Every time the
    ``blocked`` label goes on, the reader is run against the thread as it
    stands at that moment.
    """

    def __init__(self, older_body):
        self.comments = [{
            "author": {"login": funnel.PROJECT_OWNER},
            "createdAt": _stamp(OLDER_AT),
            "body": _owner_record(older_body),
        }]
        self.labels = []
        self.edges = []
        self.posted = []
        self.read_when_labelled = []

    def run_gh(self, args, **kwargs):
        argv = [str(arg) for arg in args]
        if argv[:3] == ["gh", "issue", "view"] and argv[-1] == "comments":
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"comments": self.comments}),
                stderr="")
        if argv[:3] == ["gh", "issue", "comment"]:
            self._post(argv[argv.index("--body") + 1])
        elif argv[:3] == ["gh", "issue", "close"]:
            self._post(argv[argv.index("--comment") + 1])
        elif argv[:3] == ["gh", "issue", "edit"]:
            if "--add-blocked-by" in argv:
                self.edges.append(argv[argv.index("--add-blocked-by") + 1])
            if "--add-label" in argv:
                self.labels.append(argv[argv.index("--add-label") + 1])
                self.read_when_labelled.append(self.read())
        else:
            raise AssertionError("unexpected gh call: {}".format(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def _post(self, body):
        self.posted.append(body)
        self.comments.append({
            "author": {"login": funnel.PROJECT_OWNER},
            "createdAt": _stamp(OLDER_AT + timedelta(
                minutes=len(self.comments))),
            "body": body,
        })

    def read(self):
        """What ``_load_block_comment`` makes of the thread right now."""
        item = funnel.Item(
            repo=REPO, number=42, title=ticket()["title"],
            url=ticket()["url"], state="OPEN", labels=["blocked"])
        funnel._load_block_comment(item)
        assert item.block_comments_error is None
        return {
            "block_references": list(item.block_references),
            "blocked_until": item.blocked_until,
            "block_event": item.block_event,
            "decline_reason": item.decline_reason,
            "decline_route": item.decline_route,
        }


@pytest.fixture
def github(monkeypatch):
    def install(older_body):
        fake = FakeTicket(older_body)
        monkeypatch.setattr(funnel, "_run_gh", fake.run_gh)
        return fake
    return install


def _reads_as_the_decline(state, reason):
    assert state["decline_reason"] == reason
    assert state["block_references"] == []
    assert state["blocked_until"] is None
    assert state["block_event"] is None


def _reads_as_the_human_step(state):
    assert state["block_references"] == ["#{}".format(STEP_NUMBER)]
    assert state["blocked_until"] is None
    assert state["block_event"] is None


def _decline(clone, reason, **effects):
    defaults = dict(
        run="run-42", repo=REPO, cwd=clone,
        release=lambda ref: None,
        heartbeat_finish=lambda *args: None,
        needs_effect=lambda url, ref: None,
        declined_needs_effect=lambda url, ref: None,
        external_event_needs_effect=lambda url, ref: None,
        clear_block_effect=lambda *args, **kwargs: None,
    )
    defaults.update(effects)
    return implement.finish_declined(reason, **defaults)


def _human_step(clone, **effects):
    defaults = dict(
        run="run-42", repo=REPO, cwd=clone,
        release=lambda ref: None,
        heartbeat_finish=lambda *args: None,
        create_effect=lambda repo, parent, title, body, **kwargs: {
            "number": STEP_NUMBER, "ref": "{}#{}".format(repo, STEP_NUMBER),
            "url": "https://github.com/{}/issues/{}".format(
                repo, STEP_NUMBER)},
        needs_effect=lambda url, ref: None,
        session_needs_effect=lambda url, ref: None,
        sub_issues_effect=lambda repo, number: [],
    )
    defaults.update(effects)
    return implement.finish_blocked_on_human(
        blocked()["blocked_on_human"], **defaults)


# -- Reproduction: a record that fails to post leaves no label ---------------

def _fail_comment(*args, **kwargs):
    raise funnel.GitHubError("comment failed")


def test_a_decline_whose_record_fails_never_labels_the_ticket(
        tmp_path, monkeypatch):
    """On main the label and Needs were written before the comment failed."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    effects = {"blocked": [], "needs": [], "released": [], "finished": []}

    with pytest.raises(funnel.GitHubError, match="comment failed"):
        _decline(
            clone, UNKNOWN_REASON,
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            block_effect=lambda *args, **kwargs: effects["blocked"].append(
                (args, kwargs)),
            comment_effect=_fail_comment,
            declined_needs_effect=lambda url, ref: effects["needs"].append(
                ref),
        )

    assert effects == {"blocked": [], "needs": [], "released": [],
                       "finished": []}


def test_a_human_step_whose_record_fails_never_labels_the_ticket(
        tmp_path, monkeypatch):
    """On main the label and edge were written before the comment failed."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    effects = {"blocked": [], "released": [], "finished": []}

    with pytest.raises(funnel.GitHubError, match="already created"):
        _human_step(
            clone,
            release=effects["released"].append,
            heartbeat_finish=lambda *args: effects["finished"].append(args),
            block_effect=lambda *args, **kwargs: effects["blocked"].append(
                (args, kwargs)),
            comment_effect=_fail_comment,
        )

    assert effects == {"blocked": [], "released": [], "finished": []}


# -- Every decline class posts its record before any other write -------------

@pytest.fixture
def landed_prerequisite_facts():
    return {"number": 225, "state": "CLOSED",
            "children_total": 5, "children_completed": 5}


@pytest.mark.parametrize(("reason", "facts", "edge_fails", "expected"), [
    (UNKNOWN_REASON, None, False,
     ["declined-needs", "label"]),
    ("Unlanded prerequisite #165 is still open.",
     {"number": 165, "state": "OPEN"}, False,
     ["edge"]),
    ("Unlanded prerequisite #165 is still open.",
     {"number": 165, "state": "OPEN"}, True,
     ["edge", "declined-needs", "label"]),
    ("Unlanded prerequisite #165 is closed.",
     {"number": 165, "state": "CLOSED"}, False,
     ["declined-needs", "label"]),
    (ACCEPT_BODY_CONFLICT_REASON, None, False,
     ["needs", "comment"]),
    ("Unlanded prerequisite #225 never merged.", "landed", False,
     ["clear", "needs"]),
    (LIVE_1453_UNSATISFIABLE_ACCEPTANCE, None, False,
     ["clear", "needs", "comment"]),
    (LIVE_1497_PENDING_GATE_ANSWER, None, False,
     ["clear", "external-needs", "comment"]),
], ids=["unknown", "open-prerequisite", "edge-fails", "closed-prerequisite",
        "accept-conflict", "landed-prerequisite", "unsatisfiable",
        "pending-gate"])
def test_every_decline_posts_its_record_before_needs_label_or_edge(
        tmp_path, monkeypatch, landed_prerequisite_facts,
        reason, facts, edge_fails, expected):
    """The decline comment is the first write; each class keeps its outcome
    (#1393, #1538): the writes after it are the ones it made before."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    if facts == "landed":
        facts = landed_prerequisite_facts
    order = []

    def comment(repo, number, body, **kwargs):
        order.append("declined" if body.startswith(funnel.DECLINED_PREFIX)
                     else "comment")

    def edge(*args, **kwargs):
        order.append("edge")
        if edge_fails:
            raise funnel.GitHubError("edge write failed")

    _decline(
        clone, reason,
        block_effect=lambda *args, **kwargs: order.append("label"),
        comment_effect=comment,
        needs_effect=lambda url, ref: order.append("needs"),
        declined_needs_effect=lambda url, ref: order.append(
            "declined-needs"),
        external_event_needs_effect=lambda url, ref: order.append(
            "external-needs"),
        clear_block_effect=lambda *args, **kwargs: order.append("clear"),
        prerequisite_facts_effect=lambda ref: facts,
        prerequisite_edge_effect=edge,
    )

    assert order == ["declined"] + expected


def test_a_human_step_posts_its_record_before_the_label_and_edge(
        tmp_path, monkeypatch):
    """The step is filed and routed first: the record names its number."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    order = []

    _human_step(
        clone,
        create_effect=lambda repo, parent, title, body, **kwargs: (
            order.append("step") or {
                "number": STEP_NUMBER, "ref": "{}#43".format(repo),
                "url": "https://github.com/{}/issues/43".format(repo)}),
        needs_effect=lambda url, ref: order.append("step-needs"),
        comment_effect=lambda *args, **kwargs: order.append("comment"),
        block_effect=lambda *args, **kwargs: order.append(
            ("label", kwargs.get("blocked_by"))),
    )

    assert order == ["step", "step-needs", "comment", ("label", STEP_NUMBER)]


# -- Round trip: each writer's bodies read back through _load_block_comment --

@pytest.mark.parametrize("older", sorted(OLDER_THAN_A_DECLINE))
def test_a_labelled_decline_reads_as_its_reason_and_no_condition(
        tmp_path, monkeypatch, github, older):
    """The real comment and label writers, from the moment the label goes on."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    fake = github(OLDER_THAN_A_DECLINE[older])

    _decline(clone, UNKNOWN_REASON)

    assert fake.labels == ["blocked"]
    assert len(fake.posted) == 1
    assert fake.posted[0].startswith(
        "**Declined:** {}\n\n".format(UNKNOWN_REASON))
    assert funnel.parse_provenance(fake.posted[0])["run"] == "run-42"
    when_labelled, = fake.read_when_labelled
    _reads_as_the_decline(when_labelled, UNKNOWN_REASON)
    _reads_as_the_decline(fake.read(), UNKNOWN_REASON)


@pytest.mark.parametrize(("reason", "facts", "route"), [
    # funnel reads no route type for this one; review reads its marker.
    (ACCEPT_BODY_CONFLICT_REASON, None, None),
    ("Named prerequisite nateprich-projects/Fantasy-GM#225 is unlanded.",
     {"number": 225, "state": "CLOSED",
      "children_total": 5, "children_completed": 5}, None),
    (LIVE_1453_UNSATISFIABLE_ACCEPTANCE, None, "unsatisfiable-acceptance"),
    (LIVE_1497_PENDING_GATE_ANSWER, None, "pending-gate-answer"),
], ids=["accept-conflict", "landed-prerequisite", "unsatisfiable",
        "pending-gate"])
def test_a_routed_decline_reads_as_its_reason_and_no_condition(
        tmp_path, monkeypatch, github, reason, facts, route):
    """Declines routed without the label read back the same, route and all."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    fake = github(OLDER_THAN_A_DECLINE["human step"])

    _decline(clone, reason, prerequisite_facts_effect=lambda ref: facts)

    assert fake.labels == []
    assert fake.posted[0].startswith("**Declined:** {}".format(reason))
    state = fake.read()
    _reads_as_the_decline(state, reason)
    assert (state["decline_route"] or {}).get("type") == route
    if facts is not None:
        # The disproof follows the reason in the one decline comment.
        assert "**False unlanded-prerequisite check:**" in fake.posted[0]
        assert len(fake.posted) == 1
    else:
        assert len(fake.posted) == 2
        assert implement.DECLINE_REVIEW_ROUTING_MARKER in fake.posted[1]


def test_a_defer_note_close_reads_as_its_reason(monkeypatch, github):
    """The closing comment of an accepted defer-note proof is a decline."""
    fake = github(OLDER_THAN_A_DECLINE["human step"])
    reason = "Defer the narrative dedup as a separate idea. No code changed."

    implement.close_declined_defer_note_proof(
        REPO, 42, reason, run="run-42", agent="codex", cwd=pathlib.Path("."))

    body, = fake.posted
    assert body.startswith("**Declined:** {}\n\n".format(reason))
    _reads_as_the_decline(fake.read(), reason)


@pytest.mark.parametrize("older", sorted(OLDER_THAN_A_HUMAN_STEP))
def test_a_labelled_human_step_reads_as_blocked_on_its_step(
        tmp_path, monkeypatch, github, older):
    """The real comment and label writers, from the moment the label goes on."""
    _, clone = make_clone(tmp_path)
    monkeypatch.setattr(implement, "fetch_ticket",
                        lambda repo, number: ticket(number))
    fake = github(OLDER_THAN_A_HUMAN_STEP[older])

    _human_step(clone)

    assert fake.labels == ["blocked"]
    assert fake.edges == [str(STEP_NUMBER)]
    body, = fake.posted
    assert body.startswith(
        "**Blocked on #43:** Complete the human step before resuming this "
        "ticket.\n\n")
    when_labelled, = fake.read_when_labelled
    _reads_as_the_human_step(when_labelled)
    _reads_as_the_human_step(fake.read())


def test_mark_ticket_blocked_states_that_its_caller_posts_the_record_first():
    """It writes no comment, so the order is its callers' to keep."""
    doc = implement.mark_ticket_blocked.__doc__ or ""
    assert "#2168" in doc
    assert "before" in doc
