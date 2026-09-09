"""`Ready` parents are startable, and a claim writes `Building` (#343).

#287 removed the `start` gate, which was the only writer of `Building`, while
`startable()` and `merge_blockers` both still required it. Every `Ready` project
became unstartable and its accept gate unreachable — silently, and worsening as
the pre-#287 backlog drained.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def item(number, status=None, klass=None, parent=None, **kw) -> Item:
    kw.setdefault("repo", "nateprich/beta")
    kw.setdefault("title", "issue {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("item_id", "PVTI_{}".format(number))
    return Item(number=number, status=status, klass=klass, parent=parent,
                status_since=NOW - timedelta(days=1), **kw)


def project(number, status, klass="Improve", children=1):
    return item(number, status, klass, children_total=children,
                first_child_created_at=NOW - timedelta(days=1))


def ticket(number, parent):
    return item(number, parent="nateprich/beta#{}".format(parent))


def test_a_ready_projects_tickets_are_startable():
    """plan.md: Codex draws tickets from any Ready or Building parent."""
    rows = [project(1, "Ready"), ticket(2, 1)]

    assert [i.number for i in funnel.startable(rows)] == [2]


def test_a_building_projects_tickets_are_still_startable():
    rows = [project(1, "Building"), ticket(2, 1)]

    assert [i.number for i in funnel.startable(rows)] == [2]


def test_a_shaped_project_is_not_startable():
    """Ready means "broken into issues". Shaped has not passed that gate."""
    for stage in ("Shaped", "Ideas", "Done", "Parked"):
        rows = [project(1, stage), ticket(2, 1)]
        assert funnel.startable(rows) == [], stage


def test_claiming_moves_a_ready_parent_to_building(monkeypatch):
    writes = []
    monkeypatch.setattr(funnel, "write_lock", lambda item, value: None)
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt-" + name)
    monkeypatch.setattr(funnel, "gh_graphql",
                        lambda q, **kw: writes.append(kw) or {})

    rows = [project(1, "Ready"), ticket(2, 1)]
    assert funnel.cmd_claim(rows, NOW, "nateprich/beta#2") == 0

    assert [w["option"] for w in writes] == ["opt-Building"]
    assert writes[0]["item"] == "PVTI_1"


def test_a_second_claim_does_not_rewrite_building(monkeypatch):
    """The stage is already correct; rewriting it is a pointless mutation."""
    writes = []
    monkeypatch.setattr(funnel, "write_lock", lambda item, value: None)
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt-" + name)
    monkeypatch.setattr(funnel, "gh_graphql",
                        lambda q, **kw: writes.append(kw) or {})

    rows = [project(1, "Building"), ticket(2, 1)]
    assert funnel.cmd_claim(rows, NOW, "nateprich/beta#2") == 0

    assert writes == []


def test_a_failed_promotion_does_not_lose_the_claim(monkeypatch):
    """The lock is the correctness-bearing write and already succeeded."""
    locked = []
    monkeypatch.setattr(funnel, "write_lock",
                        lambda item, value: locked.append(item.ref))
    monkeypatch.setattr(funnel, "_option_id", lambda field, name: "opt")

    def boom(*a, **kw):
        raise funnel.GitHubError("nope")

    monkeypatch.setattr(funnel, "gh_graphql", boom)

    rows = [project(1, "Ready"), ticket(2, 1)]
    assert funnel.cmd_claim(rows, NOW, "nateprich/beta#2") == 0
    assert locked == ["nateprich/beta#2"]
