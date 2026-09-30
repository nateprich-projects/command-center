"""Display-only carry-forward for prior board PR facts."""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from dashboard.prior_facts import (  # noqa: E402
    DISABLE_CARRY_FORWARD_FLAG,
    carry_forward_display_facts,
    dashboard_pr_display_overrides,
    read_prior_pr_facts,
)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _entry(generated_at, tickets):
    return {
        "generated_at": generated_at,
        "board": {
            "columns": [{
                "items": [{"tickets": tickets}],
            }],
        },
    }


def _ticket(ref, pr, number=None):
    return {
        "ref": ref,
        "state": "OPEN",
        "pr": pr,
        "pr_number": number,
    }


def test_reader_uses_latest_capture_and_extracts_only_known_pr_facts(tmp_path):
    older = NOW - timedelta(hours=5)
    newest = NOW - timedelta(hours=1)
    (tmp_path / "brief-999-aaaaaaaaaaaaaaaa.json").write_text(
        json.dumps(_entry(older.isoformat(), [_ticket("repo#1", "submitted", 10)])),
        encoding="utf-8",
    )
    (tmp_path / "brief-001-bbbbbbbbbbbbbbbb.json").write_text(
        json.dumps(_entry(newest.isoformat(), [
            _ticket("repo#1", "approved", 11),
            _ticket("repo#2", None),
            _ticket("repo#3", "unknown"),
        ])),
        encoding="utf-8",
    )
    (tmp_path / "brief-zzz-cccccccccccccccc.json").write_text(
        '{"generated_at":"not-a-time"}', encoding="utf-8",
    )

    captured_at, facts = read_prior_pr_facts(tmp_path)

    assert captured_at == newest
    assert facts == {"repo#1": {"pr": "approved", "pr_number": 11}}


def test_partial_live_gaps_carry_per_ticket_but_known_absence_stays_fresh():
    captured_at = NOW - timedelta(hours=5)
    overrides = carry_forward_display_facts(
        ["repo#1", "repo#2", "repo#3", "repo#4"],
        {
            "repo#1": {"state": "OPEN", "number": 11},
            "repo#2": None,
            "repo#3": {},
        },
        live_facts_known=True,
        captured_at=captured_at,
        prior_facts={
            "repo#1": {"pr": "approved"},
            "repo#2": {"pr": "submitted"},
            "repo#3": {"pr": "changes requested", "pr_number": 13},
        },
        now=NOW,
    )

    assert "repo#1" not in overrides
    assert "repo#2" not in overrides
    assert overrides["repo#3"] == {
        "status": "stale",
        "pr": "changes requested",
        "pr_number": 13,
        "age": "5h",
    }
    assert overrides["repo#4"] == {"status": "unknown"}


def test_total_live_failure_uses_recent_prior_facts():
    overrides = carry_forward_display_facts(
        ["repo#1"],
        {},
        live_facts_known=False,
        captured_at=NOW - timedelta(hours=2),
        prior_facts={"repo#1": {"pr": "submitted", "pr_number": 7}},
        now=NOW,
    )

    assert overrides["repo#1"] == {
        "status": "stale",
        "pr": "submitted",
        "pr_number": 7,
        "age": "2h",
    }


def test_missing_fact_without_prior_brief_remains_unknown():
    overrides = carry_forward_display_facts(
        ["repo#1"], {}, live_facts_known=False,
        captured_at=None, prior_facts={}, now=NOW,
    )

    assert overrides == {"repo#1": {"status": "unknown"}}


def test_prior_facts_expire_at_24_hours():
    overrides = carry_forward_display_facts(
        ["repo#1"], {}, live_facts_known=False,
        captured_at=NOW - timedelta(hours=24),
        prior_facts={"repo#1": {"pr": "approved"}}, now=NOW,
    )

    assert overrides == {"repo#1": {"status": "unknown"}}


def test_runtime_disable_flag_turns_off_carry_forward_and_can_be_removed(tmp_path):
    (tmp_path / "brief-001-aaaaaaaaaaaaaaaa.json").write_text(
        json.dumps(_entry(
            (NOW - timedelta(hours=5)).isoformat(),
            [_ticket("repo#1", "submitted", 7)],
        )),
        encoding="utf-8",
    )
    kwargs = {
        "spool_dir": tmp_path,
        "ticket_refs": ["repo#1"],
        "live_facts": {},
        "live_facts_known": False,
        "now": NOW,
    }

    enabled = dashboard_pr_display_overrides(**kwargs)
    assert enabled["repo#1"]["status"] == "stale"

    disable_flag = tmp_path / DISABLE_CARRY_FORWARD_FLAG
    disable_flag.touch()
    assert dashboard_pr_display_overrides(**kwargs) == {}

    disable_flag.unlink()
    assert dashboard_pr_display_overrides(**kwargs) == enabled


def test_merge_and_review_decisions_do_not_import_prior_display_facts():
    tree = ast.parse(pathlib.Path(funnel.__file__).read_text(encoding="utf-8"))

    def imports_prior_facts(node):
        return any(
            (
                isinstance(child, ast.ImportFrom)
                and child.module == "dashboard.prior_facts"
            )
            or (
                isinstance(child, ast.Import)
                and any(
                    alias.name == "dashboard.prior_facts"
                    for alias in child.names
                )
            )
            for child in ast.walk(node)
        )

    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    prior_imports = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and imports_prior_facts(node)
    ]
    assert len(prior_imports) == 1
    main = functions["main"]
    assert prior_imports[0] in ast.walk(main)
    brief_blocks = [
        node for node in ast.walk(main)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "args.command == 'brief'"
    ]
    assert any(
        prior_imports[0] in ast.walk(block) for block in brief_blocks
    )
    for name in ("cmd_merge", "merge_blockers", "review_queue"):
        assert not imports_prior_facts(functions[name])
