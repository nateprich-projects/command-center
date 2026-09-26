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
    kw.setdefault("origin", "agent")
    kw.setdefault("risk", "standard")
    kw.setdefault("needs", "none")
    return funnel.Item(
        number=number, status=status, klass=klass, **kw
    )


def test_queue_renders_classes_in_each_section(capsys):
    waiting = item(
        1, "Building", "Improve", children_total=1, children_done=1,
        body=funnel.origin_block("nate-relayed", at=NOW, run="render-run", agent="codex"),
        origin="Nate",
    )
    unclassed = item(2, "Building", None, children_total=1, children_done=1)
    awaiting_breakdown = item(3, "Ready", "Maintenance", children_total=0)
    in_flight = item(4, "Building", "New", children_total=1, children_done=0)
    inherited_ticket = item(5, parent=in_flight.ref)

    assert funnel.cmd_queue(
        [waiting, unclassed, awaiting_breakdown, in_flight, inherited_ticket], NOW
    ) == 0
    output = capsys.readouterr().out

    assert "Improve" in output
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


def test_queue_groups_each_section_in_existing_rank_order(capsys):
    waiting_alpha = item(
        1, "Building", "New", days=1, children_total=1, children_done=1,
        repo="alpha/repo",
    )
    waiting_zeta = item(
        2, "Building", "New", days=10, children_total=1, children_done=1,
        repo="zeta/repo",
    )

    parent_alpha = item(
        10, "Ready", "New", children_total=1, repo="alpha/repo"
    )
    ticket_alpha = item(11, parent=parent_alpha.ref, repo="alpha/repo")
    parent_zeta = item(
        20, "Ready", "Broken", children_total=1, repo="zeta/repo"
    )
    ticket_zeta = item(21, parent=parent_zeta.ref, repo="zeta/repo")

    pending_alpha = item(30, "Ready", "New", days=1, repo="alpha/repo")
    pending_zeta = item(31, "Ready", "New", days=10, repo="zeta/repo")

    assert funnel.cmd_queue(
        [
            waiting_alpha,
            waiting_zeta,
            parent_alpha,
            ticket_alpha,
            parent_zeta,
            ticket_zeta,
            pending_alpha,
            pending_zeta,
        ],
        NOW,
    ) == 0
    output = capsys.readouterr().out

    waiting, rest = output.split("\n\nStartable", 1)
    startable, pending = rest.split("\n\nApproved", 1)
    assert waiting.index("  zeta/repo:\n") < waiting.index("  alpha/repo:\n")
    assert waiting.index("zeta/repo#2") < waiting.index("alpha/repo#1")
    assert startable.index("  zeta/repo:\n") < startable.index("  alpha/repo:\n")
    assert startable.index("zeta/repo#21") < startable.index("alpha/repo#11")
    assert pending.index("  zeta/repo:\n") < pending.index("  alpha/repo:\n")
    assert pending.index("zeta/repo#31") < pending.index("alpha/repo#30")
    assert "    " in waiting
    assert "    " in startable
    assert "    " in pending


def test_queue_keeps_single_repo_output_unchanged(capsys):
    waiting = item(
        1, "Building", "Improve", children_total=1, children_done=1,
        body=funnel.origin_block("nate-relayed", at=NOW, run="render-run", agent="codex"),
        origin="Nate",
    )
    parent = item(2, "Ready", "New", children_total=1)
    ticket = item(3, parent=parent.ref)
    pending = item(4, "Ready", "Maintenance")

    # The open-PR scan is a live read (#1181); this test is about rendering,
    # so hand it an empty scan rather than letting it degrade.
    assert funnel.cmd_queue(
        [waiting, parent, ticket, pending], NOW, pr_facts={}
    ) == 0
    output = capsys.readouterr().out

    assert output == (
        "Waiting on Nate (1), bottom-up:\n"
        "  Building   Improve                  nateprich/beta#1                   1 day              Accept it?\n"
        "\n"
        "Startable by Codex (1), ladder order:\n"
        "  New (inherited)          nateprich/beta#3                   issue 3\n"
        "\n"
        "Approved, awaiting breakdown into tickets (1):\n"
        "  Maintenance              nateprich/beta#4                   1 day              issue 4\n"
    )
    assert "nateprich/beta:" not in output


def test_queue_empty_sections_keep_nothing_without_repo_headings(capsys):
    idea = item(1, "Ideas")

    assert funnel.cmd_queue([idea], NOW) == 0
    output = capsys.readouterr().out

    assert output == (
        "Waiting on Nate (0), bottom-up:\n"
        "  nothing\n"
        "\n"
        "Startable by Codex (0), ladder order:\n"
        "  nothing\n"
    )
    assert "  nateprich/beta:\n" not in output
