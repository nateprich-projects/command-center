"""The reads `main` makes before `cmd_brief` keep #1595's contract (#2131).

`ticket_pr_facts`, `outcome_signals`, `portfolio_metrics`, `decline_routing`,
`main_ci` and `member_issues_without_project_items` are read in `main()`'s
brief branch, so `cmd_brief` stays pure over its arguments. A skipped,
timed-out or raising read must still publish null with exactly one `missing`
entry, the same as a section `cmd_brief` runs itself, and the brief must still
print JSON. These replay #1168 (PR facts), #1178 (`main_ci`), #1169's orphan
scan and #1595 (a skipped section is null plus a `missing` entry).
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402
import outcomes  # noqa: E402


# The real readers, taken before any fixture replaces them, for the tests that
# drive a timeout from inside a reader rather than from its outside.
REAL_READ_OUTCOME_SIGNALS = funnel._read_outcome_signals
REAL_READ_PORTFOLIO_METRICS = funnel._read_portfolio_metrics

OUTCOMES = {"schema_version": 1, "source": "outcomes", "signals": {}}
PORTFOLIO = {
    "recorded_cause_regressions": {"status": "available", "value": 0},
    "command_center_ticket_pr_share": {"status": "available", "share": 1.0},
}
DECLINES = {"status": "available", "declines": 0}
ORPHANS = {"status": "read", "issues": []}

#: section -> (the reader `main` calls, the brief fields it publishes, and
#: the value those fields carry when the read succeeds).
SECTIONS = {
    "outcome_signals": (
        "_read_outcome_signals", {"outcome_signals": OUTCOMES},
    ),
    "portfolio_metrics": (
        "_read_portfolio_metrics", {
            "recorded_cause_regressions":
                PORTFOLIO["recorded_cause_regressions"],
            "command_center_ticket_pr_share":
                PORTFOLIO["command_center_ticket_pr_share"],
        },
    ),
    "decline_routing": (
        "decline_routing_metric", {"decline_routing": DECLINES},
    ),
    "main_ci": ("main_ci_json", {"main_ci": []}),
    "member_issues_without_project_items": (
        "member_issues_without_project_items",
        {"member_issues_without_project_items": ORPHANS},
    ),
}


@pytest.fixture(autouse=True)
def readable_brief(monkeypatch):
    """Every read succeeds offline unless a test breaks one."""
    funnel.reset_api_usage()
    monkeypatch.setattr(funnel, "load_items", lambda: [])
    monkeypatch.setattr(funnel, "ticket_pr_facts", lambda items: {})
    monkeypatch.setattr(
        funnel, "_read_outcome_signals", lambda now: dict(OUTCOMES)
    )
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", lambda items, now: dict(PORTFOLIO)
    )
    monkeypatch.setattr(
        funnel, "decline_routing_metric", lambda items, now: dict(DECLINES)
    )
    monkeypatch.setattr(funnel, "main_ci_json", lambda: [])
    monkeypatch.setattr(
        funnel, "member_issues_without_project_items",
        lambda items: dict(ORPHANS),
    )
    # cmd_brief's own live readers, as test_brief's _make_brief_readers_safe.
    for name, value in {
        "parked_json": [],
        "closed_itself_json": [],
        "cleared_blocks_json": [],
        "unattended_merges": [],
        "unattended_approvals": [],
        "connector_gate_answers": [],
        "agent_run_summary": [],
        "agent_health": [],
        "working_tree_touched": [],
        "rejected_merges": {},
        "recent_resend_ratio": {},
    }.items():
        monkeypatch.setattr(
            funnel, name, lambda *args, _value=value, **kwargs: _value
        )
    # The display snapshot's own local reads, which never gate the brief.
    monkeypatch.setattr(funnel, "_dashboard_muse_usage", lambda now: {})
    monkeypatch.setattr(funnel, "_dashboard_claude_usage", lambda now: None)
    yield
    funnel.reset_api_usage()


def _brief(capsys):
    assert funnel.main(["brief"]) == 0
    return json.loads(capsys.readouterr().out)


def _break(monkeypatch, section, attribute, mode):
    """Make one read fail the way ``mode`` names."""
    if mode == "skip":
        monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS, section, 0.0)
        return

    def failing(*args, **kwargs):
        if mode == "timeout":
            raise funnel.BriefSectionTimeout(section, "GitHub read timed out")
        raise funnel.GitHubError("{} offline".format(section))

    monkeypatch.setattr(funnel, attribute, failing)


def test_every_read_succeeding_leaves_missing_empty(capsys):
    """The fixture is clean, so one entry below is the broken read's own."""
    brief = _brief(capsys)

    for _attribute, fields in SECTIONS.values():
        for field, value in fields.items():
            assert brief[field] == value
    assert brief["missing"] == []
    assert brief["degraded"] == []


def test_reproduction_zero_budget_main_ci_is_named_in_missing(
    monkeypatch, capsys
):
    """#2131's reproduction: the skipped read published null and no entry."""
    monkeypatch.setitem(funnel.BRIEF_SECTION_BUDGETS, "main_ci", 0)

    brief = _brief(capsys)

    assert brief["main_ci"] is None
    assert [
        row for row in brief["missing"] if row["section"] == "main_ci"
    ] == [{
        "section": "main_ci",
        "error": "could not read main_ci within its budget; this is an "
                 "unread section, not an empty one",
    }]


@pytest.mark.parametrize("mode", ["skip", "timeout", "raise"])
@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_an_unread_pre_brief_read_is_null_with_one_missing_entry(
    monkeypatch, capsys, section, mode
):
    attribute, fields = SECTIONS[section]
    _break(monkeypatch, section, attribute, mode)

    brief = _brief(capsys)

    for field in fields:
        assert brief[field] is None, field
    assert [row["section"] for row in brief["missing"]] == [section]
    degraded = [row for row in brief["degraded"] if row["section"] == section]
    if mode == "raise":
        # The same shape as a raising cmd_brief section: the reader's error,
        # and no budget record, because no budget was missed (#1595).
        assert brief["missing"] == [
            {"section": section, "error": "{} offline".format(section)}
        ]
        assert degraded == []
    else:
        assert "not an empty one" in brief["missing"][0]["error"]
        assert len(degraded) == 1
        assert degraded[0]["budget_seconds"] == (
            0.0 if mode == "skip" else funnel.BRIEF_SECTION_BUDGETS[section]
        )
    # The rest of the brief is intact.
    assert brief["counts_by_gate"] is not None
    assert brief["items"] == []


@pytest.mark.parametrize("mode", ["skip", "timeout", "raise"])
def test_unread_ticket_pr_facts_marks_each_dependent_section_once(
    monkeypatch, capsys, mode
):
    """ticket_pr_facts is not published itself: its failure names the
    sections built on it, once each, and never itself (#1168)."""
    _break(monkeypatch, "ticket_pr_facts", "ticket_pr_facts", mode)

    brief = _brief(capsys)

    for section in funnel.BRIEF_PR_FACT_SECTIONS:
        assert brief[section] is None, section
    reason = (
        "ticket_pr_facts offline" if mode == "raise"
        else "brief section read timed out"
    )
    assert brief["missing"] == [
        {
            "section": section,
            "error": "could not read ticket branch facts: " + reason,
        }
        for section in funnel.BRIEF_PR_FACT_SECTIONS
    ]
    degraded = [
        row for row in brief["degraded"]
        if row["section"] == "ticket_pr_facts"
    ]
    assert len(degraded) == (0 if mode == "raise" else 1)


def test_a_timed_out_outcome_record_read_is_unread_not_unavailable(
    monkeypatch, capsys
):
    """`outcomes.read_records` reads GitHub through `funnel._run_gh`, which
    raises BriefSectionTimeout when the section's budget runs out. The reader
    must let the runner see it rather than publish its own fallback."""
    monkeypatch.setattr(
        funnel, "_read_outcome_signals", REAL_READ_OUTCOME_SIGNALS
    )

    def timed_out(*args, **kwargs):
        raise funnel.BriefSectionTimeout(
            "outcome_signals", "GitHub read timed out"
        )

    monkeypatch.setattr(outcomes, "read_records", timed_out)

    brief = _brief(capsys)

    assert brief["outcome_signals"] is None
    assert [row["section"] for row in brief["missing"]] == ["outcome_signals"]
    assert [
        row["reason"] for row in brief["degraded"]
        if row["section"] == "outcome_signals"
    ] == ["GitHub read timed out"]


def test_a_failed_outcome_record_read_keeps_its_explicit_fallback(
    monkeypatch, capsys
):
    """An ordinary read failure is still the reader's own explicit
    `unavailable` record, which no reader of the brief mistakes for zeros."""
    monkeypatch.setattr(
        funnel, "_read_outcome_signals", REAL_READ_OUTCOME_SIGNALS
    )

    def unreadable(*args, **kwargs):
        raise funnel.GitHubError("heartbeat branch offline")

    monkeypatch.setattr(outcomes, "read_records", unreadable)

    brief = _brief(capsys)

    assert brief["outcome_signals"]["status"] == "unavailable"
    assert "heartbeat branch offline" in brief["outcome_signals"]["reason"]
    assert brief["missing"] == []


def test_a_timed_out_ticket_pr_share_read_is_unread_not_unavailable(
    monkeypatch, capsys
):
    """The portfolio read's one GitHub read is the merged-PR scan; a timeout
    there is the section's timeout, not an `unavailable` share."""
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", REAL_READ_PORTFOLIO_METRICS
    )

    def timed_out(*args, **kwargs):
        raise funnel.BriefSectionTimeout(
            "portfolio_metrics", "GitHub read timed out"
        )

    monkeypatch.setattr(funnel, "_recent_merged_pr_rows", timed_out)

    brief = _brief(capsys)

    assert brief["recorded_cause_regressions"] is None
    assert brief["command_center_ticket_pr_share"] is None
    assert [row["section"] for row in brief["missing"]] == [
        "portfolio_metrics"
    ]
    assert [
        row["reason"] for row in brief["degraded"]
        if row["section"] == "portfolio_metrics"
    ] == ["GitHub read timed out"]


def test_a_failed_ticket_pr_share_read_keeps_its_explicit_fallback(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        funnel, "_read_portfolio_metrics", REAL_READ_PORTFOLIO_METRICS
    )

    def unreadable(*args, **kwargs):
        raise funnel.GitHubError("PR list offline")

    monkeypatch.setattr(funnel, "_recent_merged_pr_rows", unreadable)

    brief = _brief(capsys)

    assert brief["command_center_ticket_pr_share"]["status"] == "unavailable"
    assert brief["recorded_cause_regressions"] is not None
    assert brief["missing"] == []
