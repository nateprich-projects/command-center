"""The merge-safety read replaces the brief in the merge path.

Until #801 the review routines ran a full `funnel brief` before every merge
just to read the rejected-merge counter, and a slow brief blocked the merge
by timing out (#830). Now the gate reads the counter itself, and
engine/merge_safety is the narrow read that computes every merge
prerequisite — fresh, fail-closed — and nothing else.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import merge_safety  # noqa: E402
from funnel import Item  # noqa: E402

NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
REPO = "owner/repo"
SHA = "abc123def456"


def verdict(**kw):
    body = {"verdict": "approved", "ci": "green", "head_sha": SHA,
            "blocking": []}
    body.update(kw)
    return funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps(body) + "\n```"


def items():
    project = Item(repo=REPO, number=1, title="p", url="", state="OPEN",
                   status="Building", klass="Improve", item_id="project-id",
                   children_total=1, children_done=0)
    ticket = Item(repo=REPO, number=9, title="t", url="", state="OPEN",
                  parent=REPO + "#1")
    return [project, ticket]


def regression(number, days_ago):
    return Item(repo=REPO, number=number,
                title="{}{}: something".format(
                    funnel.REGRESSION_PREFIX, number),
                url="", state="OPEN",
                status_since=NOW - timedelta(days=days_ago))


def pr(**kw):
    data = {"state": "OPEN", "headRefName": "ticket/9", "headRefOid": SHA,
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"name": "tests",
                                   "conclusion": "SUCCESS"}]}
    data.update(kw)
    return data


def wire(monkeypatch, pr_json, comments):
    fact = dict(pr_json)
    fact["number"] = 5
    fact["comments"] = [{"body": body} for body in comments]
    fact["verdict"] = funnel._latest_verdict_from_comments(fact["comments"])
    monkeypatch.setattr(
        funnel, "_pr_fact_for_number", lambda repo, number, **kwargs: dict(fact)
    )


def sabotage_reporting(monkeypatch):
    """Every reporting-section loader fails loudly. The read must not need one."""
    def explode(*args, **kwargs):
        raise AssertionError("merge path loaded a reporting section")

    monkeypatch.setattr(funnel, "cmd_brief", explode)
    monkeypatch.setattr(funnel, "_brief_timed", explode)
    monkeypatch.setattr(funnel, "ticket_pr_facts", explode)
    monkeypatch.setattr(funnel.BriefCache, "get_pr_facts", explode)


# -- the read reports every prerequisite --------------------------------------

def test_a_clean_pr_is_safe_and_names_what_was_checked(monkeypatch):
    wire(monkeypatch, pr(), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is True
    assert found["blockers"] == []
    assert found["checked"] == ["counter", "open", "conflict", "binding",
                                "ci", "verdict"]
    assert found["stop_auto_merging"] is False
    assert found["rejected_merge_count"] == 0


def test_a_tripped_stop_counter_refuses(monkeypatch):
    wire(monkeypatch, pr(), [verdict()])
    rows = items() + [regression(21, 1), regression(22, 3), regression(23, 6)]
    found = merge_safety.read(REPO, 5, rows, NOW)
    assert found["safe"] is False
    assert found["stop_auto_merging"] is True
    assert found["rejected_merge_count"] == 3
    assert any("auto-merging is stopped" in b for b in found["blockers"])


def test_two_rejections_do_not_stop_merging(monkeypatch):
    wire(monkeypatch, pr(), [verdict()])
    rows = items() + [regression(21, 1), regression(22, 3)]
    assert merge_safety.read(REPO, 5, rows, NOW)["safe"] is True


def test_a_closed_pr_refuses(monkeypatch):
    wire(monkeypatch, pr(state="MERGED"), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("not open" in b for b in found["blockers"])


def test_a_conflicting_branch_refuses(monkeypatch):
    wire(monkeypatch, pr(mergeable="CONFLICTING"), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("conflicting" in b for b in found["blockers"])


def test_an_unbound_branch_refuses(monkeypatch):
    wire(monkeypatch, pr(headRefName="feature/whatever"), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("not a ticket/" in b for b in found["blockers"])


def test_a_ticket_missing_from_the_funnel_refuses(monkeypatch):
    wire(monkeypatch, pr(headRefName="ticket/99"), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("no ticket" in b for b in found["blockers"])


def test_a_project_that_is_not_building_refuses(monkeypatch):
    rows = items()
    rows[0].status = "Ready"
    wire(monkeypatch, pr(), [verdict()])
    found = merge_safety.read(REPO, 5, rows, NOW)
    assert found["safe"] is False
    assert any("not Building" in b for b in found["blockers"])


def test_red_ci_refuses(monkeypatch):
    wire(monkeypatch, pr(statusCheckRollup=[
        {"name": "tests", "conclusion": "FAILURE"}]), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("CI not green" in b for b in found["blockers"])


def test_absent_ci_refuses(monkeypatch):
    wire(monkeypatch, pr(statusCheckRollup=[]), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("no CI checks" in b for b in found["blockers"])


def test_a_missing_verdict_refuses(monkeypatch):
    wire(monkeypatch, pr(), [])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert "no review verdict recorded" in found["blockers"]


def test_a_verdict_on_an_older_head_refuses(monkeypatch):
    wire(monkeypatch, pr(headRefOid="9999newcommit"), [verdict()])
    found = merge_safety.read(REPO, 5, items(), NOW)
    assert found["safe"] is False
    assert any("not the head" in b for b in found["blockers"])


# -- funnel merge no longer invokes brief -------------------------------------

def test_the_gate_refuses_a_tripped_counter_without_the_brief(monkeypatch):
    sabotage_reporting(monkeypatch)
    wire(monkeypatch, pr(), [verdict()])
    rows = items() + [regression(21, 1), regression(22, 3), regression(23, 6)]
    assert any("auto-merging is stopped" in reason
               for reason in funnel.merge_blockers(REPO, 5, rows, NOW))


def test_funnel_merge_passes_a_clean_pr_without_the_brief(monkeypatch, capsys):
    sabotage_reporting(monkeypatch)
    wire(monkeypatch, pr(), [verdict()])
    assert funnel.cmd_merge(items(), NOW, REPO, 5, False) == 0
    assert "passes every merge condition" in capsys.readouterr().out


def test_funnel_merge_refuses_a_tripped_counter_without_the_brief(
        monkeypatch, capsys):
    sabotage_reporting(monkeypatch)
    wire(monkeypatch, pr(), [verdict()])
    rows = items() + [regression(21, 1), regression(22, 3), regression(23, 6)]
    assert funnel.cmd_merge(rows, NOW, REPO, 5, False) == 1
    assert "auto-merging is stopped" in capsys.readouterr().err


def test_the_merge_path_sources_never_name_brief():
    for func in (funnel.cmd_merge, funnel.merge_blockers):
        assert re.search(r"\bbrief\b",
                         inspect.getsource(func), re.IGNORECASE) is None


@pytest.mark.parametrize("routine", ("muse", "zcode", "claude"))
def test_no_review_routine_runs_brief_before_merging(routine):
    body = " ".join(
        (ROOT / "routines" / (routine + ".md")).read_text(
            encoding="utf-8").split())
    assert ("run `funnel brief` and require it to succeed, then check "
            "`rejected_merges.stop_auto_merging`") not in body
    assert "the gate reads the rejected-merge counter itself" in body


# -- the read loads no reporting section --------------------------------------

def test_the_read_completes_without_loading_any_reporting_section(monkeypatch):
    sabotage_reporting(monkeypatch)
    wire(monkeypatch, pr(), [verdict()])
    started = time.perf_counter()
    found = merge_safety.read(REPO, 5, items(), NOW)
    elapsed = time.perf_counter() - started
    assert found["safe"] is True
    # Two GitHub reads and in-memory checks, all fixture-local: anything
    # near the brief's multi-minute budget means a reporting section crept in.
    assert elapsed < 5.0


# -- structural guarantees ----------------------------------------------------

def test_engine_imports_from_funnel_and_never_the_reverse():
    engine_source = (ROOT / "engine" / "merge_safety.py").read_text()
    assert "import funnel" in engine_source
    funnel_source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in funnel_source
    assert "from engine" not in funnel_source


def test_the_read_makes_no_writes():
    mutating = ("pr merge", "pr comment", "pr close", "pr review",
                "pr edit", "pr create", "issue close", "issue create",
                "issue comment", "issue edit", "item-add", "item-edit",
                "item-delete", "--yes", "delete-branch")
    source = (ROOT / "engine" / "merge_safety.py").read_text()
    offenders = [verb for verb in mutating if verb in source]
    assert offenders == []
