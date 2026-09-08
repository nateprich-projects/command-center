"""The human-readable queue and ideas renderings."""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def item(number, status=None, klass=None, days=1.0, **kw):
    kw.setdefault("repo", "nateprich/beta")
    kw.setdefault("title", "issue {}".format(number))
    kw.setdefault("url", "https://example.invalid/{}".format(number))
    kw.setdefault("state", "OPEN")
    kw.setdefault("status_since", NOW - timedelta(days=days))
    return funnel.Item(
        number=number, status=status, klass=klass, **kw
    )


def test_queue_renders_classes_in_each_section(capsys):
    waiting = item(1, "Building", "Broken", children_total=1, children_done=1)
    unclassed = item(2, "Building", None, children_total=1, children_done=1)
    awaiting_breakdown = item(3, "Ready", "Maintenance", children_total=0)
    in_flight = item(4, "Building", "New", children_total=1, children_done=0)
    inherited_ticket = item(5, parent=in_flight.ref)

    assert funnel.cmd_queue(
        [waiting, unclassed, awaiting_breakdown, in_flight, inherited_ticket], NOW
    ) == 0
    output = capsys.readouterr().out

    assert "Broken" in output
    assert "Maintenance" in output
    assert "New (inherited)" in output
    assert "no class" in output


def test_ideas_renders_class_without_inventing_one(capsys):
    broken = item(1, "Ideas", "Broken")
    unclassed = item(2, "Ideas", None)

    assert funnel.cmd_ideas([broken, unclassed], NOW) == 0
    output = capsys.readouterr().out

    assert "Broken" in output
    assert "no class" in output
