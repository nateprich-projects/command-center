"""Fixture-driven tests for GitHub-derived ticket outcome records."""

from __future__ import annotations

import base64
import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import outcomes  # noqa: E402


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
REPO = "owner/repo"
SHA = "abc123"


def ticket(number=42, **extra):
    row = {
        "repo": REPO,
        "number": number,
        "title": "Ticket {}".format(number),
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "closedAt": "2026-09-10T10:00:00Z",
        "stateReason": "COMPLETED",
    }
    row.update(extra)
    return row


def pr(number, *, created="2026-09-10T08:00:00Z", state="CLOSED",
       merged=None, head=SHA, checks=None, branch="ticket/42"):
    return {
        "number": number,
        "state": "MERGED" if merged else state,
        "url": "https://github.com/{}/pull/{}".format(REPO, number),
        "headRefName": branch,
        "headRefOid": head,
        "createdAt": created,
        "closedAt": merged or "2026-09-10T09:00:00Z",
        "mergedAt": merged,
        "statusCheckRollup": checks,
        # The funnel's own PR: same-repository head, owner author (#1794).
        "isCrossRepository": False,
        "author": {"login": "nateprich"},
    }


def verdict_comment(verdict, *, at="2026-09-10T08:30:00Z", head=SHA,
                    author="nateprich"):
    body = funnel.REVIEW_MARKER + "\n\n```json\n" + json.dumps({
        "verdict": verdict,
        "ci": "green",
        "head_sha": head,
        "blocking": [],
        "reviewed_at": at,
    }) + "\n```\n\n" + agent_provenance(at)
    return {"author": {"login": author}, "body": body, "createdAt": at}


def agent_provenance(at="2026-09-10T08:30:00Z"):
    return funnel.provenance_block(
        "agent", at=outcomes._timestamp(at), run="run-1", agent="muse"
    )


def test_attempts_and_turns_cover_every_pr_on_one_ticket_branch():
    first = pr(10, created="2026-09-10T08:00:00Z", checks=[
        {"name": "pytest", "conclusion": "SUCCESS"},
    ])
    second = pr(11, created="2026-09-10T09:00:00Z", merged="2026-09-10T09:30:00Z",
                checks=[{"name": "pytest", "conclusion": "SUCCESS"}])
    record = outcomes.derive_outcome(
        ticket(),
        [first, second],
        {
            10: {"comments": [verdict_comment("rejected")]},
            11: {"comments": [verdict_comment("approved", at="2026-09-10T09:15:00Z")]},
        },
        now=NOW,
    )

    assert record["attempts"] == 2
    assert record["turns"] == 2
    assert record["ci_green"] is True
    assert record["ci_result"] == "green"
    assert record["review_result"] == "approved"
    assert record["merged"] is True
    assert record["merged_prs"] == [11]
    assert [row["number"] for row in record["prs"]] == [10, 11]
    assert record["prs"][0]["head_ref_name"] == "ticket/42"
    assert record["prs"][0]["first_review_result"] == "rejected"
    assert record["prs"][0]["first_reviewed_at"] == "2026-09-10T08:30:00Z"
    assert record["prs"][1]["first_review_result"] == "approved"


def test_latest_verdict_is_chronological_not_pr_list_order():
    older = pr(10, created="2026-09-10T08:00:00Z")
    newer = pr(11, created="2026-09-10T09:00:00Z")
    record = outcomes.derive_outcome(
        ticket(),
        [newer, older],
        {
            10: {"comments": [verdict_comment("approved", at="2026-09-10T08:30:00Z")]},
            11: {"comments": [verdict_comment("rejected", at="2026-09-10T09:30:00Z")]},
        },
        now=NOW,
    )

    assert record["review_result"] == "rejected"
    assert record["turns"] == 2


def test_only_the_owners_verdicts_are_outcomes():
    """A forged verdict is neither a turn nor the review result (#1787)."""
    owner = verdict_comment("rejected", at="2026-09-10T08:30:00Z")
    forged = verdict_comment("approved", at="2026-09-10T08:45:00Z",
                             author="mallory")
    unauthored = dict(verdict_comment("approved", at="2026-09-10T08:50:00Z"))
    unauthored.pop("author")
    rest_owner = verdict_comment("rejected", at="2026-09-10T08:40:00Z")
    rest_owner["user"] = rest_owner.pop("author")
    record = outcomes.derive_outcome(
        ticket(),
        [pr(10, created="2026-09-10T08:00:00Z")],
        {10: {"comments": [owner, rest_owner, forged, unauthored]}},
        now=NOW,
    )

    assert record["review_result"] == "rejected"
    assert record["turns"] == 2
    assert record["prs"][0]["first_review_result"] == "rejected"


def test_no_verdicts_are_unknown_turns_not_zero():
    record = outcomes.derive_outcome(
        ticket(),
        [pr(10, checks=[{"name": "pytest", "conclusion": "SUCCESS"}])],
        {10: {"comments": []}},
        now=NOW,
    )

    assert record["turns"] is None
    assert record["review_result"] is None
    assert record["merged"] is False


def test_derive_prices_each_run_and_keeps_missing_tokens_unknown():
    rates = []
    for token_kind, amount in zip(
        ("fresh_input_tokens", "cache_read_input_tokens",
         "cache_write_input_tokens", "output_tokens"),
        (1, 2, 3, 4),
    ):
        rates.append({
            "provider": "openai",
            "model": "gpt-test",
            "token_kind": token_kind,
            "usd_per_million_tokens": amount,
            "effective_from": "2026-09-01T00:00:00Z",
            "source_url": "https://example.test/pricing",
            "recorded_at": "2026-09-10T12:00:00Z",
        })
    observation = {
        "run": "run-priced",
        "agent": "codex",
        "provider": "openai",
        "model": "gpt-test",
        "started_at": "2026-09-09T12:00:00Z",
        "token_usage": {
            "fresh_input_tokens": 100,
            "cache_read_input_tokens": 200,
            "cache_write_input_tokens": 300,
            "output_tokens": 400,
        },
    }

    record = outcomes.derive_outcome(
        ticket(), now=NOW, run_observations=[observation], rate_rows=rates
    )

    assert record["runs"][0]["notional_api_cost"]["value"] == pytest.approx(.003)
    assert record["notional_api_cost"]["value"] == pytest.approx(.003)

    observation["token_usage"]["cache_read_input_tokens"] = None
    incomplete = outcomes.derive_outcome(
        ticket(43), now=NOW, run_observations=[observation], rate_rows=rates
    )
    assert incomplete["runs"][0]["notional_api_cost"]["value"] is None
    assert incomplete["runs"][0]["notional_api_cost"]["missing_token_kinds"] == [
        "cache_read_input_tokens"
    ]
    assert incomplete["notional_api_cost"]["value"] is None


@pytest.mark.parametrize(
    "finish_provider, finish_model",
    [
        ("openai", "gpt-5.6-sol"),
        ("anthropic", "gpt-5.6-luna"),
    ],
)
def test_conflicting_start_and_finish_pricing_observations_are_not_priced(
        finish_provider, finish_model):
    start_provider = "openai"
    start_model = "gpt-5.6-luna"
    token_kinds = (
        "fresh_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
    )
    token_counts = (1000, 30000, 0, 200)
    rates = []
    for token_kind, amount in zip(token_kinds, (1, 2, 3, 4)):
        rates.append({
            "provider": finish_provider,
            "model": finish_model,
            "token_kind": token_kind,
            "usd_per_million_tokens": amount,
            "effective_from": "1970-01-01T00:00:00Z",
            "source_url": "https://example.test/pricing",
            "recorded_at": "2026-09-10T12:00:00Z",
        })
    rows = {
        "codex": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "provider": start_provider, "model": start_model},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "provider": finish_provider,
             "model": finish_model, "token_usage": dict(zip(
                 token_kinds, token_counts
             ))},
        ],
    }

    runs = outcomes._ticket_runs("owner/repo#42", rows)
    record = outcomes.derive_outcome(
        ticket(), run_observations=runs, rate_rows=rates, now=NOW
    )

    assert runs[0]["start_provider"] == start_provider
    assert runs[0]["finish_provider"] == finish_provider
    assert runs[0]["start_model"] == start_model
    assert runs[0]["finish_model"] == finish_model
    cost = record["runs"][0]["notional_api_cost"]
    assert cost["value"] is None
    assert cost["reason"] == "conflicting_model_observations"


def test_cost_signal_sums_same_lane_runs_before_counting_merged_pr():
    row = signal_record(1, cost=None)
    row["runs"] = [
        {
            "agent": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high",
            "notional_api_cost": {
                "value": 2.0, "unit": "USD",
                "basis": "notional_api_list_price", "status": "priced",
            },
        },
        {
            "agent": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high",
            "notional_api_cost": {
                "value": 3.0, "unit": "USD",
                "basis": "notional_api_list_price", "status": "priced",
            },
        },
    ]

    cost = outcomes.signal_summary([row], now=NOW)["signals"][
        "cost_per_merged_pr"
    ]

    assert cost["status"] == "available"
    assert cost["by_lane"] == [{
        "lane": "codex/gpt-5.6-sol/high",
        "unit": "USD",
        "merged_prs": 1,
        "total_cost": 5.0,
        "cost_per_merged_pr": 5.0,
    }]


@pytest.mark.parametrize(
    "checks, expected",
    [
        ([{"conclusion": "FAILURE"}], False),
        ([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}], True),
        ([{"conclusion": "IN_PROGRESS"}], None),
        ([], None),
        (None, None),
    ],
)
def test_ci_is_aggregated_fail_closed(checks, expected):
    assert outcomes.ci_green(checks) is expected


def test_reopen_after_a_merge_is_derived_from_issue_events():
    merged = pr(10, merged="2026-09-10T09:00:00Z")
    record = outcomes.derive_outcome(
        ticket(), [merged], {10: {"comments": [verdict_comment("approved")] }},
        issue_events=[
            {"event": "closed", "created_at": "2026-09-10T09:00:01Z"},
            {"event": "reopened", "created_at": "2026-09-10T09:30:00Z"},
        ],
        now=NOW,
    )

    assert record["reopened_after_merge"] is True
    assert record["reopened_at"] == "2026-09-10T09:30:00Z"


def test_direct_nate_comment_counts_but_agent_comment_under_his_login_does_not():
    direct = {"author": {"login": "nateprich"}, "body": "Please change the scope."}
    agent = {
        "author": {"login": "nateprich"},
        "body": "reviewed by the automation\n\n" + agent_provenance(),
    }
    no_intervention = outcomes.derive_outcome(
        ticket(),
        [pr(10, merged="2026-09-10T09:00:00Z")],
        {10: {"comments": [verdict_comment("approved")] }},
        issue_comments=[agent],
        now=NOW,
    )
    intervention = outcomes.derive_outcome(
        ticket(),
        [pr(10, merged="2026-09-10T09:00:00Z")],
        {10: {"comments": [verdict_comment("approved")] }},
        issue_comments=[direct],
        now=NOW,
    )

    assert no_intervention["human_intervention_required"] is False
    assert intervention["human_intervention_required"] is True


def test_merge_without_an_approving_verdict_is_intervention():
    record = outcomes.derive_outcome(
        ticket(),
        [pr(10, merged="2026-09-10T09:00:00Z")],
        {10: {"comments": [verdict_comment("rejected")] }},
        now=NOW,
    )

    assert record["human_intervention_required"] is True


def test_ticket_runs_join_bindings_to_session_usage_and_metadata(monkeypatch):
    rows = {
        "codex": [
            {
                "run": "run-1",
                "agent": "codex",
                "phase": "start",
                "ts": 100,
                "session_id": "session-1",
                "provider": "openai",
                "harness": "codex",
                "model": "gpt-5.6-luna",
                "reasoning_effort": "high",
                "model_source": "detected",
            },
            {"run": "run-1", "agent": "codex", "phase": "bind",
             "ts": 101, "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "agent": "codex", "phase": "finish",
             "ts": 110, "outcome": "done"},
        ],
    }
    monkeypatch.setattr(
        outcomes.session_usage,
        "usage_for_session",
        lambda agent, session_id, started_at=None, finished_at=None: {
            "fresh_input_tokens": 40,
            "cache_read_input_tokens": 60,
            "cache_write_input_tokens": 2,
            "output_tokens": 8,
        },
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)
    record = outcomes.derive_outcome(
        ticket(), run_observations=runs, now=NOW
    )

    assert runs[0]["run"] == "run-1"
    assert runs[0]["started_at"] == "1970-01-01T00:01:40Z"
    assert record["token_usage"] == {
        "fresh_input_tokens": 40,
        "cache_read_input_tokens": 60,
        "cache_write_input_tokens": 2,
        "output_tokens": 8,
    }
    assert record["model"] == "gpt-5.6-luna"
    assert record["reasoning_effort"] == "high"


def test_ticket_runs_prefer_the_durable_finish_token_snapshot(monkeypatch):
    rows = {
        "codex": [
            {"run": "run-1", "agent": "codex", "phase": "start", "ts": 100,
             "session_id": "session-1"},
            {"run": "run-1", "agent": "codex", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "agent": "codex", "phase": "finish", "ts": 110,
             "outcome": "done", "token_usage": {
                 "fresh_input_tokens": 40,
                 "cache_read_input_tokens": 60,
                 "cache_write_input_tokens": 2,
                 "output_tokens": 8,
             }},
        ],
    }

    def should_not_read_local_transcript(*args, **kwargs):
        raise AssertionError("new outcome derivation must use heartbeat data")

    monkeypatch.setattr(
        outcomes.session_usage,
        "usage_for_session",
        should_not_read_local_transcript,
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)

    assert runs[0]["token_usage"] == {
        "fresh_input_tokens": 40,
        "cache_read_input_tokens": 60,
        "cache_write_input_tokens": 2,
        "output_tokens": 8,
    }


def test_ticket_run_recovers_an_all_null_durable_snapshot_from_its_session(
        monkeypatch):
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-session"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "token_usage": {
                 "fresh_input_tokens": None,
                 "cache_read_input_tokens": None,
                 "cache_write_input_tokens": None,
                 "output_tokens": None,
             }},
        ],
    }
    expected = {
        "fresh_input_tokens": 100,
        "cache_read_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 10,
    }
    seen = []
    monkeypatch.setattr(
        outcomes.session_usage,
        "usage_for_session",
        lambda agent, session_id, **kwargs:
            seen.append((agent, session_id)) or expected,
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)
    record = outcomes.derive_outcome(ticket(), run_observations=runs, now=NOW)

    assert seen == [("muse", "muse-session")]
    assert runs[0]["token_usage"] == expected
    assert record["token_usage"] == expected


def test_muse_ticket_usage_sums_all_recorded_sessions_at_read_time(monkeypatch):
    first = {
        "fresh_input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_write_input_tokens": 1,
        "output_tokens": 3,
    }
    second = {
        "fresh_input_tokens": 20,
        "cache_read_input_tokens": 4,
        "cache_write_input_tokens": 2,
        "output_tokens": 5,
    }
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-one"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "muse_session_ids": [
                 "muse-one", "muse-two"], "muse_calls_made": 2,
             "token_usage": first},
        ],
    }
    seen = []

    def usage_for_session(agent, session_id, **kwargs):
        seen.append((agent, session_id))
        return {"muse-one": first, "muse-two": second}.get(session_id)

    monkeypatch.setattr(
        outcomes.session_usage, "usage_for_session", usage_for_session
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)
    record = outcomes.derive_outcome(ticket(), run_observations=runs, now=NOW)

    expected = {
        "fresh_input_tokens": 30,
        "cache_read_input_tokens": 6,
        "cache_write_input_tokens": 3,
        "output_tokens": 8,
    }
    assert seen == [("muse", "muse-one"), ("muse", "muse-two")]
    assert runs[0]["token_usage"] == expected
    assert runs[0]["token_usage_coverage"] == {
        "status": "complete",
        "captured_calls": 2,
        "made_calls": 2,
        "readable_journals": 2,
        "unreadable_journals": 0,
        "uncaptured_calls": 0,
    }
    assert record["token_usage"] == expected


def test_muse_missing_journal_makes_outcome_partial_not_zero(monkeypatch):
    expected = {
        "fresh_input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_write_input_tokens": 0,
        "output_tokens": 3,
    }
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-one"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "muse_session_ids": [
                 "muse-one", "muse-missing"], "muse_calls_made": 2},
        ],
    }
    monkeypatch.setattr(
        outcomes.session_usage,
        "usage_for_session",
        lambda agent, session_id, **kwargs:
            expected if session_id == "muse-one" else None,
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)
    record = outcomes.derive_outcome(ticket(), run_observations=runs, now=NOW)

    assert runs[0]["token_usage"] is None
    assert runs[0]["token_usage_coverage"] == {
        "status": "partial",
        "captured_calls": 2,
        "made_calls": 2,
        "readable_journals": 1,
        "unreadable_journals": 1,
        "uncaptured_calls": 0,
        "reasons": ["unreadable_session_journals_or_usage"],
    }
    assert record["token_usage"] is None


def test_muse_uncaptured_call_reports_captured_versus_made(monkeypatch):
    expected = {
        "fresh_input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_write_input_tokens": 0,
        "output_tokens": 3,
    }
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-one"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "muse_session_ids": [
                 "muse-one", None, "muse-three"], "muse_calls_made": 3},
        ],
    }
    monkeypatch.setattr(
        outcomes.session_usage,
        "usage_for_session",
        lambda agent, session_id, **kwargs: expected,
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)

    assert runs[0]["token_usage"] is None
    assert runs[0]["token_usage_coverage"] == {
        "status": "partial",
        "captured_calls": 2,
        "made_calls": 3,
        "readable_journals": 2,
        "unreadable_journals": 0,
        "uncaptured_calls": 1,
        "reasons": ["uncaptured_session_ids"],
    }


def test_muse_absent_session_list_uses_the_bound_single_session(monkeypatch):
    expected = {
        "fresh_input_tokens": 10,
        "cache_read_input_tokens": 2,
        "cache_write_input_tokens": 0,
        "output_tokens": 3,
    }
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-one"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "muse_calls_made": 1},
        ],
    }
    seen = []
    monkeypatch.setattr(
        outcomes.session_usage,
        "usage_for_session",
        lambda agent, session_id, **kwargs:
            seen.append((agent, session_id)) or expected,
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)

    assert seen == [("muse", "muse-one")]
    assert runs[0]["token_usage"] == expected
    assert runs[0]["token_usage_coverage"]["status"] == "complete"
    assert runs[0]["token_usage_coverage"]["captured_calls"] == 1
    assert runs[0]["token_usage_coverage"]["made_calls"] == 1


def test_malformed_muse_session_list_is_exposed_as_fault(monkeypatch):
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-one"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "muse_session_ids": ["muse-one"],
             "muse_calls_made": 2},
        ],
    }

    def should_not_read_session(*args, **kwargs):
        raise AssertionError("malformed list must fail closed before reading")

    monkeypatch.setattr(
        outcomes.session_usage, "usage_for_session", should_not_read_session
    )

    runs = outcomes._ticket_runs("owner/repo#42", rows)

    assert runs[0]["token_usage"] is None
    assert runs[0]["token_usage_coverage"]["status"] == "fault"
    assert runs[0]["token_usage_coverage"]["reasons"] == [
        "call_count_mismatch"
    ]


def test_legacy_muse_snapshot_without_call_count_is_not_run_total():
    rows = {
        "muse": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "muse-one"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "token_usage": {
                 "fresh_input_tokens": 10,
                 "cache_read_input_tokens": 2,
                 "cache_write_input_tokens": 0,
                 "output_tokens": 3,
             }},
        ],
    }

    runs = outcomes._ticket_runs("owner/repo#42", rows)

    assert runs[0]["token_usage"] is None
    assert runs[0]["token_usage_coverage"] == {
        "status": "partial",
        "captured_calls": 1,
        "made_calls": None,
        "readable_journals": None,
        "unreadable_journals": None,
        "uncaptured_calls": None,
        "reasons": ["legacy_call_count_not_recorded"],
    }


def test_ticket_run_with_unreadable_session_and_all_null_snapshot_stays_unknown(
        monkeypatch):
    rows = {
        "codex": [
            {"run": "run-1", "phase": "start", "ts": 100,
             "session_id": "missing"},
            {"run": "run-1", "phase": "bind", "ts": 101,
             "do": "ticket", "work": "owner/repo#42"},
            {"run": "run-1", "phase": "finish", "ts": 110,
             "outcome": "done", "token_usage": {
                 "fresh_input_tokens": None,
                 "cache_read_input_tokens": None,
                 "cache_write_input_tokens": None,
                 "output_tokens": None,
             }},
        ],
    }
    monkeypatch.setattr(outcomes.session_usage, "usage_for_session", lambda *a, **k: None)

    runs = outcomes._ticket_runs("owner/repo#42", rows)
    record = outcomes.derive_outcome(ticket(), run_observations=runs, now=NOW)

    assert runs[0]["token_usage"] is None
    assert outcomes._aggregate_token_usage(runs) is None
    assert record["runs"] == runs
    assert record["token_usage"] is None


def signal_record(number, *, merged=True, attempts=1, intervention=False,
                  cost=None, agent="codex", model="gpt-5.6-luna",
                  effort="high"):
    row = {
        "ticket": "owner/repo#{}".format(number),
        "merged": merged,
        "merged_prs": [number] if merged else [],
        "attempts": attempts,
        "human_intervention_required": intervention,
        "runs": [{
            "agent": agent,
            "model": model,
            "reasoning_effort": effort,
        }],
    }
    if cost is not None:
        row["cost_usd"] = cost
    return row


def test_signal_summary_computes_named_signals_independently_by_lane():
    summary = outcomes.signal_summary([
        signal_record(1, cost=2.0),
        signal_record(
            2, attempts=3, intervention=True, cost=6.0,
            model="gpt-5.6-sol", effort="max",
        ),
        signal_record(3, merged=False, cost=None, intervention=False),
    ], now=NOW)

    signals = summary["signals"]
    assert summary["outcome_records"] == 3

    cost = signals["cost_per_merged_pr"]
    assert cost["status"] == "available"
    assert cost["value"] is None  # no cross-lane north-star total
    assert cost["by_lane"] == [
        {
            "lane": "codex/gpt-5.6-luna/high",
            "unit": "USD",
            "merged_prs": 1,
            "total_cost": 2.0,
            "cost_per_merged_pr": 2.0,
        },
        {
            "lane": "codex/gpt-5.6-sol/max",
            "unit": "USD",
            "merged_prs": 1,
            "total_cost": 6.0,
            "cost_per_merged_pr": 6.0,
        },
    ]

    rework = signals["rework_rate"]
    assert rework["status"] == "available"
    assert rework["value"] == 1.0
    assert rework["attempts"] == 4
    assert rework["rework_attempts"] == 2
    assert rework["merged_prs"] == 2

    intervention = signals["intervention_rate"]
    assert intervention["status"] == "available"
    assert intervention["value"] == pytest.approx(1 / 3)
    assert intervention["interventions"] == 1
    assert intervention["sample_size"] == 3


def test_signal_summary_keeps_missing_cost_and_partial_fields_honest():
    summary = outcomes.signal_summary([
        signal_record(1, cost=None),
        signal_record(2, attempts=None, intervention=None, cost=4.0),
    ], now=NOW)
    signals = summary["signals"]

    cost = signals["cost_per_merged_pr"]
    assert cost["status"] == "partial"
    assert cost["available"] is True
    assert cost["missing_records"] == 1
    assert cost["by_lane"][0]["cost_per_merged_pr"] == 4.0

    rework = signals["rework_rate"]
    assert rework["status"] == "partial"
    assert rework["value"] == 0.0
    assert rework["missing_records"] == 1

    intervention = signals["intervention_rate"]
    assert intervention["status"] == "partial"
    assert intervention["value"] == 0.0
    assert intervention["missing_records"] == 1


def test_signal_summary_does_not_treat_raw_tokens_as_priced_cost():
    row = signal_record(1, cost=None)
    row["token_usage"] = {
        "fresh_input_tokens": 10,
        "cache_read_input_tokens": 20,
        "cache_write_input_tokens": 0,
        "output_tokens": 5,
    }

    assert outcomes._cost_observation(row) is None
    cost = outcomes.signal_summary([row], now=NOW)["signals"][
        "cost_per_merged_pr"
    ]
    assert cost["status"] == "insufficient_data"
    assert cost["available"] is False
    assert cost["value"] is None
    assert cost["by_lane"] == []


def test_signal_summary_reports_null_values_when_a_signal_has_no_data():
    signals = outcomes.signal_summary([], now=NOW)["signals"]
    assert signals["cost_per_merged_pr"]["value"] is None
    assert signals["rework_rate"]["value"] is None
    assert signals["intervention_rate"]["value"] is None
    assert all(
        signal["status"] == "insufficient_data"
        and signal["available"] is False
        for signal in signals.values()
    )


def test_signals_command_prints_the_durable_summary(monkeypatch, capsys):
    rows = [signal_record(1, cost=3.0)]
    monkeypatch.setattr(
        outcomes, "read_records", lambda repo, branch: rows
    )

    assert outcomes.main(["signals", "--repo", REPO, "--branch", "heartbeat"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "outcomes"
    assert payload["signals"]["cost_per_merged_pr"]["by_lane"][0][
        "cost_per_merged_pr"
    ] == 3.0


def test_append_encoder_preserves_one_record_per_ticket():
    first = outcomes.derive_outcome(ticket(1), now=NOW)
    duplicate = dict(first)
    duplicate["derived_at"] = "later"
    second = outcomes.derive_outcome(ticket(2), now=NOW)

    fresh = outcomes._new_records([first], [duplicate, second])

    assert [row["ticket"] for row in fresh] == ["owner/repo#2"]
    assert outcomes._decode_records(outcomes._encode_records([first, second])) == [
        first, second
    ]


def test_repository_walk_uses_index_rows_once_and_writes_no_partial_scan(
    monkeypatch,
):
    calls = []
    ticket_row = ticket(42)
    first = pr(10, created="2026-09-10T08:00:00Z")
    second = pr(11, created="2026-09-10T09:00:00Z", merged="2026-09-10T09:30:00Z")

    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [ticket_row])

    class Index(dict):
        all_rows = (first, second)

    def index(repo, limit):
        calls.append((repo, limit))
        return Index(), False

    monkeypatch.setattr(funnel, "ticket_pr_index", index)
    monkeypatch.setattr(outcomes, "read_heartbeat_records", lambda: {})

    def gh_json(*args):
        if args[0] == "pr":
            return {"number": args[2], "comments": []}
        if args[0] == "issue":
            return {"comments": []}
        return [[]]

    monkeypatch.setattr(outcomes, "gh_json", gh_json)

    records = outcomes.derive_repository(REPO, limit=77, now=NOW)

    assert calls == [(REPO, 77)]
    assert records[0]["attempts"] == 2
    assert records[0]["merged"] is True


@pytest.mark.parametrize("trust", [
    {"isCrossRepository": True,
     "headRepository": {"nameWithOwner": "mallory/repo"},
     "author": {"login": "mallory"}},
    {"author": {"login": "mallory"}},
    {"author": None},
    {"isCrossRepository": None},
])
def test_a_foreign_pr_is_never_a_tickets_attempt(monkeypatch, trust):
    """A fork's PR can carry the branch name or a closing reference (#1794)."""
    ticket_row = ticket(42)
    owner = pr(10, created="2026-09-10T08:00:00Z")
    by_branch = dict(pr(11, created="2026-09-10T09:00:00Z",
                        merged="2026-09-10T09:30:00Z"), **trust)
    by_link = dict(pr(12, branch="feature/x"), **trust)
    by_link["closingIssuesReferences"] = [
        {"number": 42, "repository": {"nameWithOwner": REPO}}]

    class Index(dict):
        all_rows = (owner, by_branch, by_link)

    monkeypatch.setattr(
        outcomes, "list_closed_tickets", lambda repo, limit: [ticket_row])
    monkeypatch.setattr(
        funnel, "ticket_pr_index", lambda repo, limit: (Index(), False))
    monkeypatch.setattr(outcomes, "read_heartbeat_records", lambda: {})

    def gh_json(*args):
        if args[0] == "pr":
            return {"number": args[2], "comments": []}
        if args[0] == "issue":
            return {"comments": []}
        return [[]]

    monkeypatch.setattr(outcomes, "gh_json", gh_json)

    records = outcomes.derive_repository(REPO, limit=77, now=NOW)

    assert records[0]["attempts"] == 1
    assert records[0]["merged"] is False


def test_repository_walk_rejects_a_truncated_pr_scan(monkeypatch):
    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [])
    monkeypatch.setattr(funnel, "ticket_pr_index", lambda repo, limit: ({}, True))
    # The offline `gh` used to read as empty heartbeat ledgers; since #1717
    # an unreadable ledger raises, so stub it as the sibling walks do.
    monkeypatch.setattr(outcomes, "read_heartbeat_records", lambda: {})

    with pytest.raises(outcomes.OutcomeError, match="refusing partial"):
        outcomes.derive_repository(REPO, limit=77, now=NOW)


def test_repository_walk_uses_batched_issue_events_and_deleted_ref_links(
    monkeypatch,
):
    ticket_row = ticket(42, comments=[])
    deleted_ref_pr = pr(10, branch=None)
    deleted_ref_pr["headRefName"] = None
    deleted_ref_pr["comments"] = []
    deleted_ref_pr["closingIssuesReferences"] = [
        {"number": 42, "repository": {"nameWithOwner": REPO}}
    ]

    class Index(dict):
        all_rows = (deleted_ref_pr,)

    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [ticket_row])
    monkeypatch.setattr(
        funnel, "ticket_pr_index", lambda repo, limit, include_comments=False: (Index(), False)
    )
    monkeypatch.setattr(
        outcomes,
        "gh_json",
        lambda *args: [[{
            "issue": {"number": 42},
            "event": "reopened",
            "created_at": "2026-09-10T11:00:00Z",
        }]],
    )
    monkeypatch.setattr(outcomes, "read_heartbeat_records", lambda: {})

    records = outcomes.derive_repository(REPO, limit=77, now=NOW)

    assert records[0]["attempts"] == 1
    assert records[0]["reopened_after_merge"] is False


def test_outcome_summary_reports_null_fields_by_name():
    first = outcomes.derive_outcome(ticket(1), now=NOW)
    second = outcomes.derive_outcome(
        ticket(2), [pr(20, checks=[{"conclusion": "SUCCESS"}])],
        {20: {"comments": []}}, now=NOW,
    )

    summary = outcomes.outcome_summary([first, second], appended=2)

    assert summary["derived"] == 2
    assert summary["appended"] == 2
    assert summary["null_fields"] == sum(summary["null_fields_by_name"].values())
    assert summary["null_fields_by_name"]["turns"] == 2
    assert summary["storage"] == "heartbeat:outcomes.jsonl"


def test_closed_ticket_list_requests_comments_for_the_full_walk(monkeypatch):
    calls = []

    def gh_json(*args):
        calls.append(args)
        return []

    monkeypatch.setattr(outcomes, "gh_json", gh_json)

    assert outcomes.list_closed_tickets(REPO, limit=77) == []
    assert "comments" in calls[0][-1]


def _closed_rows(count):
    return [
        {"number": number, "title": "t", "url": "u",
         "closedAt": "2026-09-01T00:00:00Z", "stateReason": "COMPLETED",
         "comments": []}
        for number in range(1, count + 1)
    ]


def test_closed_ticket_scan_of_todays_size_derives_at_the_default_ceiling(
    monkeypatch,
):
    # command-center had 1,017 closed issues on 2026-09-27; the old 1,000-row
    # ceiling refused that scan outright (#1751).
    requested = []

    def gh_json(*args):
        requested.append(args[args.index("--limit") + 1])
        return _closed_rows(1017)

    monkeypatch.setattr(outcomes, "gh_json", gh_json)

    assert len(outcomes.list_closed_tickets(REPO)) == 1017
    assert requested == [str(outcomes.PR_SCAN_LIMIT + 1)]
    assert outcomes.PR_SCAN_LIMIT >= 10000


def test_closed_ticket_scan_past_the_ceiling_still_refuses(monkeypatch):
    monkeypatch.setattr(
        outcomes, "gh_json",
        lambda *args: _closed_rows(outcomes.PR_SCAN_LIMIT + 1),
    )

    with pytest.raises(outcomes.OutcomeError, match="refusing partial"):
        outcomes.list_closed_tickets(REPO)


def test_repository_walk_scans_prs_to_the_default_ceiling(monkeypatch):
    seen = []

    def index(repo, limit, include_comments=False):
        seen.append(limit)
        return {}, False

    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [])
    monkeypatch.setattr(funnel, "ticket_pr_index", index)
    monkeypatch.setattr(outcomes, "read_heartbeat_records", lambda: {})
    monkeypatch.setattr(outcomes, "_repository_issue_events", lambda repo: {})

    assert outcomes.derive_repository(REPO, now=NOW) == []
    assert seen == [outcomes.PR_SCAN_LIMIT]


def test_repository_walk_refuses_a_pr_scan_truncated_at_the_default_ceiling(
    monkeypatch,
):
    monkeypatch.setattr(outcomes, "list_closed_tickets", lambda repo, limit: [])
    monkeypatch.setattr(
        funnel, "ticket_pr_index",
        lambda repo, limit, include_comments=False: ({}, True),
    )
    monkeypatch.setattr(outcomes, "read_heartbeat_records", lambda: {})

    with pytest.raises(outcomes.OutcomeError, match="PR scan .* exceeded 10000"):
        outcomes.derive_repository(REPO, now=NOW)


def test_remote_append_uses_sha_and_retries_a_contents_conflict(monkeypatch):
    existing = outcomes.derive_outcome(ticket(1), now=NOW)
    addition = outcomes.derive_outcome(ticket(2), now=NOW)
    encoded = base64.b64encode(
        outcomes._encode_records([existing]).encode()
    ).decode()
    responses = [
        SimpleNamespace(returncode=0, stdout=json.dumps({"content": encoded, "sha": "old"}), stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="HTTP 409 conflict"),
        SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content": encoded, "sha": "new"}),
            stderr="",
        ),
        SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    ]
    commands = []
    bodies = []

    def run(args, stdin=None):
        commands.append(list(args))
        bodies.append(stdin)
        return responses.pop(0)

    monkeypatch.setattr(outcomes, "_run_gh", run)
    monkeypatch.setattr(outcomes.time, "sleep", lambda seconds: None)

    assert outcomes.append_records([existing, addition]) == 1
    # The sha moved from argv into the request body with #1294; what matters
    # is still that the retry carries the *refreshed* sha, not the stale one.
    assert json.loads(bodies[-1])["sha"] == "new"
    assert len(commands) == 4


def test_the_append_body_goes_on_stdin_and_never_onto_the_command_line(
        monkeypatch):
    """#1294. The ledger used to be passed as `-f content=<base64>`, so every
    append put the whole file in argv. On 2026-09-22, deriving across all seven
    member repos for the first time, that died with `OSError: [Errno 7]
    Argument list too long` before `gh` ever ran — and because nothing was
    written, the next run re-derived the same backlog and failed identically.

    Asserting on the shape rather than on a size: a length threshold would pass
    against the old code until someone picked a number larger than the
    fixture."""
    existing = outcomes.derive_outcome(ticket(1), now=NOW)
    addition = outcomes.derive_outcome(ticket(2), now=NOW)
    encoded = base64.b64encode(
        outcomes._encode_records([existing]).encode()
    ).decode()
    responses = [
        SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"content": encoded, "sha": "old"}),
            stderr="",
        ),
        SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    ]
    seen = {}

    def run(args, stdin=None):
        seen["args"] = list(args)
        seen["stdin"] = stdin
        return responses.pop(0)

    monkeypatch.setattr(outcomes, "_run_gh", run)

    assert outcomes.append_records([existing, addition]) == 1

    args, stdin = seen["args"], seen["stdin"]
    assert "--input" in args and args[args.index("--input") + 1] == "-"
    # No argument carries the payload, under any flag.
    assert not any("content=" in a for a in args), args
    assert not any(a.startswith("-f") and "=" in a for a in args), args

    payload = json.loads(stdin)
    assert payload["content"] == base64.b64encode(
        outcomes._encode_records([existing, addition]).encode()
    ).decode()
    assert payload["sha"] == "old"
    assert payload["message"].endswith("+1 record(s)")


def test_all_members_resolves_the_topic_and_never_a_list_in_the_schedule(
        monkeypatch):
    """#1288: the daily schedule passes no repository, so a repository that
    opts into the funnel topic is scanned without anyone editing a plist.
    AGENTS.md: GitHub is the state; `funnel.member_repos` is "never an
    allowlist"."""
    monkeypatch.setattr(
        funnel, "member_repos",
        lambda: ["nateprich-projects/The-League",
                 "nateprich-projects/command-center"])

    assert outcomes.resolve_derive_repos(None, True) == [
        "nateprich-projects/The-League",
        "nateprich-projects/command-center",
    ]


def test_all_members_refuses_to_derive_nothing_and_report_success(monkeypatch):
    """Fail closed. A scheduled run that resolved no repositories would append
    no records and exit 0, and the gap would be indistinguishable from a quiet
    day — the same reason `usage.py` refuses to turn an unknown count into a
    zero."""
    monkeypatch.setattr(funnel, "member_repos", lambda: [])

    with pytest.raises(outcomes.OutcomeError) as caught:
        outcomes.resolve_derive_repos(None, True)

    assert "refusing to derive nothing" in str(caught.value)


def test_all_members_and_an_explicit_repo_is_refused_rather_than_merged(
        monkeypatch):
    """Silently taking the union would make the schedule's behaviour depend on
    a flag nobody passed; silently preferring one would drop the other."""
    monkeypatch.setattr(funnel, "member_repos", lambda: ["owner/repo"])

    with pytest.raises(outcomes.OutcomeError) as caught:
        outcomes.resolve_derive_repos(["owner/other"], True)

    assert "do not also pass" in str(caught.value)


def test_without_all_members_the_explicit_repos_and_the_default_are_unchanged():
    """The existing callers keep their behaviour: #1288 adds a flag, it does
    not change what `derive` does when the flag is absent."""
    assert outcomes.resolve_derive_repos(["a/b", "c/d"], False) == ["a/b", "c/d"]
    assert outcomes.resolve_derive_repos(None, False) == [outcomes.REPO]


def _contents_response(records, *, inline, sha="abc123"):
    """A Contents API payload. Above 1 MB GitHub sends `size` and `sha` but an
    empty `content`, which is the case that matters here."""
    text = outcomes._encode_records(records)
    encoded = base64.b64encode(text.encode()).decode()
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({
            "content": encoded if inline else "",
            "size": len(text.encode()),
            "sha": sha,
        }),
        stderr="",
    )


def test_a_ledger_too_big_to_inline_is_read_through_the_blob_api(monkeypatch):
    """#1294. Over one megabyte the Contents API returns an empty `content`
    alongside a perfectly valid `sha`, so the old read reported an empty
    ledger while handing `append_records` a usable sha — and the next write
    would have put `[] + fresh` over all of history. `outcomes.jsonl` crossed
    that line at 1,277,231 bytes on 2026-09-22."""
    records = [outcomes.derive_outcome(ticket(n), now=NOW) for n in (1, 2, 3)]
    blob = SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"content": base64.b64encode(
            outcomes._encode_records(records).encode()).decode()}),
        stderr="",
    )
    responses = [_contents_response(records, inline=False), blob]
    calls = []

    def run(args, stdin=None):
        calls.append(list(args))
        return responses.pop(0)

    monkeypatch.setattr(outcomes, "_run_gh", run)

    got, sha = outcomes._read_remote(outcomes.REPO, outcomes.HEARTBEAT_BRANCH)

    assert len(got) == 3
    assert sha == "abc123"
    assert calls[1] == ["api", "repos/{}/git/blobs/abc123".format(outcomes.REPO)]


def test_an_unreadable_blob_raises_rather_than_reporting_an_empty_ledger(
        monkeypatch):
    """Fail closed. Every caller treats the returned list as the whole ledger,
    so 'I could not read it' must never arrive looking like 'there is nothing
    there' — that is the shape that overwrites history."""
    records = [outcomes.derive_outcome(ticket(1), now=NOW)]
    responses = [
        _contents_response(records, inline=False),
        SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502"),
    ]

    monkeypatch.setattr(
        outcomes, "_run_gh", lambda args, stdin=None: responses.pop(0))

    with pytest.raises(outcomes.OutcomeError) as caught:
        outcomes._read_remote(outcomes.REPO, outcomes.HEARTBEAT_BRANCH)

    assert "502" in str(caught.value)


def test_a_blob_that_reads_back_empty_also_raises(monkeypatch):
    """A 200 with no content is the same lie as a failed call."""
    records = [outcomes.derive_outcome(ticket(1), now=NOW)]
    responses = [
        _contents_response(records, inline=False),
        SimpleNamespace(returncode=0, stdout=json.dumps({"content": ""}),
                        stderr=""),
    ]

    monkeypatch.setattr(
        outcomes, "_run_gh", lambda args, stdin=None: responses.pop(0))

    with pytest.raises(outcomes.OutcomeError) as caught:
        outcomes._read_remote(outcomes.REPO, outcomes.HEARTBEAT_BRANCH)

    assert "read back empty" in str(caught.value)


def test_a_ledger_small_enough_to_inline_makes_no_second_call(monkeypatch):
    """The common path is unchanged: one call, no blob read."""
    records = [outcomes.derive_outcome(ticket(1), now=NOW)]
    calls = []

    def run(args, stdin=None):
        calls.append(list(args))
        return _contents_response(records, inline=True)

    monkeypatch.setattr(outcomes, "_run_gh", run)

    got, _sha = outcomes._read_remote(outcomes.REPO, outcomes.HEARTBEAT_BRANCH)

    assert len(got) == 1
    assert len(calls) == 1


def test_an_empty_ledger_is_still_read_as_empty(monkeypatch):
    """Size zero is a genuine empty file, not a declined inline: no blob read
    and no exception, or a first-ever append could never happen."""
    responses = [SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"content": "", "size": 0, "sha": "abc123"}),
        stderr="",
    )]
    calls = []

    def run(args, stdin=None):
        calls.append(list(args))
        return responses.pop(0)

    monkeypatch.setattr(outcomes, "_run_gh", run)

    got, sha = outcomes._read_remote(outcomes.REPO, outcomes.HEARTBEAT_BRANCH)

    assert got == []
    assert sha == "abc123"
    assert len(calls) == 1


# --- #1717: heartbeat ledgers over 1 MB, and healing records they emptied ---

def _heartbeat_rows(ticket_ref="owner/repo#42", run="run-1", agent="codex"):
    return [
        {"run": run, "agent": agent, "phase": "start", "ts": 100,
         "session_id": "session-1", "model": "gpt-6-luna"},
        {"run": run, "agent": agent, "phase": "bind", "ts": 101,
         "do": "ticket", "work": ticket_ref},
        {"run": run, "agent": agent, "phase": "finish", "ts": 110,
         "outcome": "done", "token_usage": {
             "fresh_input_tokens": 40, "cache_read_input_tokens": 60,
             "cache_write_input_tokens": 2, "output_tokens": 8}},
    ]


def _jsonl(rows):
    return "".join(json.dumps(row) + "\n" for row in rows)


def _heartbeat_gh(ledgers, *, large=(), blob_failures=()):
    """A stubbed `gh` over the heartbeat branch. An agent absent from
    `ledgers` is a missing file (404). A ledger in `large` answers as the
    Contents API does above 1 MB, measured on codex.jsonl on 2026-09-27:
    `encoding: none`, an empty `content`, and the real size and sha."""
    calls = []

    def run(args, stdin=None):
        calls.append(list(args))
        path = args[1]
        for agent, rows in ledgers.items():
            text = _jsonl(rows)
            sha = "sha-{}".format(agent)
            if path == "repos/{}/contents/{}.jsonl?ref=heartbeat".format(
                    outcomes.REPO, agent):
                payload = {"size": 3542750 if agent in large else len(text),
                           "sha": sha}
                if agent in large:
                    payload.update(encoding="none", content="")
                else:
                    payload.update(encoding="base64", content=base64.b64encode(
                        text.encode()).decode())
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload), stderr="")
            if path == "repos/{}/git/blobs/{}".format(outcomes.REPO, sha):
                if agent in blob_failures:
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="HTTP 502: Bad Gateway")
                return SimpleNamespace(returncode=0, stdout=json.dumps({
                    "encoding": "base64",
                    "content": base64.b64encode(text.encode()).decode(),
                }), stderr="")
        return SimpleNamespace(
            returncode=1, stdout="", stderr="gh: Not Found (HTTP 404)")

    return run, calls


def test_a_heartbeat_ledger_over_one_megabyte_returns_its_rows(monkeypatch):
    """#1717. Above 1 MB the Contents API sends `encoding: none` and an empty
    `content`; decoding that gave zero rows, so every outcome derived from
    2026-09-24 carried `runs=[]` although the runs were in the ledger."""
    rows = _heartbeat_rows()
    run, calls = _heartbeat_gh({"codex": rows}, large={"codex"})
    monkeypatch.setattr(outcomes, "_run_gh", run)

    found = outcomes.read_heartbeat_records()

    assert found["codex"] == rows
    assert ["api", "repos/{}/git/blobs/sha-codex".format(outcomes.REPO)] in calls
    # And the rows reach the record, which is what the loss was.
    assert outcomes._ticket_runs("owner/repo#42", found)[0]["token_usage"] == (
        rows[-1]["token_usage"])


def test_a_small_heartbeat_ledger_is_still_read_inline(monkeypatch):
    rows = _heartbeat_rows(agent="claude")
    run, calls = _heartbeat_gh({"claude": rows})
    monkeypatch.setattr(outcomes, "_run_gh", run)

    assert outcomes.read_heartbeat_records()["claude"] == rows
    assert not any("git/blobs" in call[1] for call in calls)


def test_a_failed_large_heartbeat_read_raises_rather_than_reading_no_rows(
        monkeypatch):
    """Fail closed. The store is append-only, so a ledger that could not be
    read must stop the derive, not be stored as tickets with no runs."""
    run, _calls = _heartbeat_gh(
        {"muse": _heartbeat_rows(agent="muse")},
        large={"muse"}, blob_failures={"muse"})
    monkeypatch.setattr(outcomes, "_run_gh", run)

    with pytest.raises(outcomes.OutcomeError) as caught:
        outcomes.read_heartbeat_records()

    assert "502" in str(caught.value)


def test_a_failed_heartbeat_read_that_is_not_a_missing_file_raises(
        monkeypatch):
    """The same silent-empty shape one call earlier: a 5xx on the Contents
    read itself used to read as an agent with no runs."""
    monkeypatch.setattr(outcomes, "_run_gh", lambda args, stdin=None: (
        SimpleNamespace(returncode=1, stdout="", stderr="HTTP 502: Bad Gateway")))

    with pytest.raises(outcomes.OutcomeError):
        outcomes.read_heartbeat_records()


def test_a_missing_heartbeat_ledger_reads_as_no_rows(monkeypatch):
    """An agent that has never written a heartbeat is not a failed read."""
    rows = _heartbeat_rows()
    run, _calls = _heartbeat_gh({"codex": rows})
    monkeypatch.setattr(outcomes, "_run_gh", run)

    found = outcomes.read_heartbeat_records()

    assert found == {"claude": [], "codex": rows, "muse": [], "zcode": []}


def _outcome(number, runs):
    return outcomes.derive_outcome(ticket(number), now=NOW, run_observations=runs)


def _run(agent="codex", run="run-1", usage=True):
    return {"run": run, "agent": agent, "token_usage": {
        "fresh_input_tokens": 1, "cache_read_input_tokens": 2,
        "cache_write_input_tokens": 3, "output_tokens": 4,
    } if usage else None}


def test_reproduction_heals_current_key_from_old_repo_heartbeat_binding():
    old_repo = "nateprich-projects/FF-Weekly-Start-Sit"
    current_repo = "nateprich-projects/Fantasy-GM"
    old_ticket = outcomes.derive_outcome(
        ticket(42, repo=old_repo), now=NOW
    )
    current_ticket = outcomes.derive_outcome(
        ticket(42, repo=current_repo), now=NOW
    )
    heartbeat = {
        "codex": _heartbeat_rows(
            ticket_ref=old_repo + "#42", run="rename-run-42"
        )
    }

    runs = outcomes._ticket_runs(current_repo + "#42", heartbeat)
    fresh = outcomes.derive_outcome(
        ticket(42, repo=current_repo), now=NOW, run_observations=runs
    )
    combined, healed = outcomes._heal_empty_runs(
        [old_ticket, current_ticket], [fresh]
    )

    assert runs[0]["run"] == "rename-run-42"
    assert combined == [old_ticket, fresh]
    assert healed == [fresh]
    assert old_ticket["runs"] == []
    assert fresh["runs"][0]["run"] == "rename-run-42"


def test_read_records_normalizes_alias_and_prefers_the_current_key(monkeypatch):
    old_repo = "nateprich-projects/FF-Weekly-Start-Sit"
    current_repo = "nateprich-projects/Fantasy-GM"
    old = {"ticket": old_repo + "#42", "title": "old", "runs": []}
    current = {
        "ticket": current_repo + "#42", "title": "current", "runs": []
    }
    monkeypatch.setattr(
        outcomes, "_read_remote", lambda repo, branch: ([old, current], "sha")
    )

    assert outcomes.read_records() == [current]

    monkeypatch.setattr(
        outcomes, "_read_remote", lambda repo, branch: ([old], "sha")
    )
    normalized_old = dict(old)
    normalized_old["ticket"] = current_repo + "#42"
    assert outcomes.read_records() == [normalized_old]


def test_write_records_does_not_append_an_existing_current_key(monkeypatch):
    old_repo = "nateprich-projects/FF-Weekly-Start-Sit"
    current_repo = "nateprich-projects/Fantasy-GM"
    stored = [
        {"ticket": old_repo + "#42", "runs": []},
        {"ticket": current_repo + "#42", "runs": []},
    ]
    fresh = {"ticket": current_repo + "#42", "runs": [{"run": "run-42"}]}
    monkeypatch.setattr(
        outcomes, "_read_remote", lambda repo, branch: (stored, "sha")
    )
    writes = []

    def run(args, stdin=None):
        writes.append(list(args))
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(outcomes, "_run_gh", run)

    assert outcomes.write_records([fresh]) == (0, [])
    assert writes == []


def test_heal_replaces_only_empty_runs_records_whose_fresh_derivation_has_runs():
    empty_healable = _outcome(1, [])
    with_runs = _outcome(2, [_run(run="old")])
    empty_still_empty = _outcome(3, [])
    not_rederived = _outcome(4, [])
    stored = [empty_healable, with_runs, empty_still_empty, not_rederived]

    later = datetime(2026, 9, 28, tzinfo=timezone.utc)
    fresh = [
        outcomes.derive_outcome(ticket(1), now=later, run_observations=[_run()]),
        outcomes.derive_outcome(
            ticket(2), now=later, run_observations=[_run(run="new")]),
        outcomes.derive_outcome(ticket(3), now=later, run_observations=[]),
    ]

    combined, healed = outcomes._heal_empty_runs(stored, fresh)

    assert combined == [fresh[0], with_runs, empty_still_empty, not_rederived]
    assert healed == [fresh[0]]


def test_derive_heal_empty_runs_writes_one_ledger_with_only_the_heal(
        monkeypatch, capsys):
    """End to end through `derive --heal-empty-runs` and a stubbed `gh`: the
    empty record is replaced in place, the record with runs and the empty
    record whose fresh derivation is also empty come back byte-for-byte, and
    a new ticket is still appended."""
    stored = [_outcome(1, []), _outcome(2, [_run(run="old")]), _outcome(3, [])]
    stored_text = outcomes._encode_records(stored)
    later = datetime(2026, 9, 28, tzinfo=timezone.utc)
    fresh = [
        outcomes.derive_outcome(ticket(1), now=later, run_observations=[
            _run(), _run(agent="muse", run="run-2", usage=False)]),
        outcomes.derive_outcome(
            ticket(2), now=later, run_observations=[_run(run="new")]),
        outcomes.derive_outcome(ticket(3), now=later, run_observations=[]),
        outcomes.derive_outcome(ticket(5), now=later, run_observations=[]),
    ]
    monkeypatch.setattr(
        outcomes, "derive_repository",
        lambda repo, limit, ticket_numbers: fresh)
    writes = []

    def run(args, stdin=None):
        if "PUT" in args:
            writes.append(json.loads(stdin))
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "content": base64.b64encode(stored_text.encode()).decode(),
            "size": len(stored_text), "sha": "old",
        }), stderr="")

    monkeypatch.setattr(outcomes, "_run_gh", run)

    assert outcomes.main(["derive", "--repo", REPO, "--heal-empty-runs"]) == 0

    assert len(writes) == 1
    written = base64.b64decode(writes[0]["content"]).decode().splitlines()
    kept = stored_text.splitlines()
    assert json.loads(written[0]) == fresh[0]
    assert written[1:3] == kept[1:3]
    assert json.loads(written[3]) == fresh[3]
    assert len(written) == 4
    summary = json.loads(capsys.readouterr().out)
    assert summary["appended"] == 1
    assert summary["healed"] == 1
    assert summary["healed_runs_by_agent"] == {"codex": 1, "muse": 1}
    assert summary["healed_runs_with_token_usage"] == 1


def test_derive_heal_empty_runs_writes_when_nothing_new_needs_appending(
        monkeypatch, capsys):
    """The one-time heal re-derives tickets that are all already stored, so
    there is nothing to append. A write gated on new records alone would
    return before the PUT, and `--heal-empty-runs` would report success
    having healed nothing."""
    stored = [_outcome(1, []), _outcome(2, [_run(run="old")])]
    stored_text = outcomes._encode_records(stored)
    later = datetime(2026, 9, 28, tzinfo=timezone.utc)
    fresh = [
        outcomes.derive_outcome(ticket(1), now=later, run_observations=[_run()]),
        outcomes.derive_outcome(
            ticket(2), now=later, run_observations=[_run(run="new")]),
    ]
    monkeypatch.setattr(
        outcomes, "derive_repository",
        lambda repo, limit, ticket_numbers: fresh)
    writes = []

    def run(args, stdin=None):
        if "PUT" in args:
            writes.append(json.loads(stdin))
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "content": base64.b64encode(stored_text.encode()).decode(),
            "size": len(stored_text), "sha": "old",
        }), stderr="")

    monkeypatch.setattr(outcomes, "_run_gh", run)

    assert outcomes.main(["derive", "--repo", REPO, "--heal-empty-runs"]) == 0

    assert len(writes) == 1
    assert writes[0]["sha"] == "old"
    written = base64.b64decode(writes[0]["content"]).decode().splitlines()
    assert len(written) == 2
    assert json.loads(written[0]) == fresh[0]
    assert written[1] == stored_text.splitlines()[1]
    summary = json.loads(capsys.readouterr().out)
    assert summary["appended"] == 0
    assert summary["healed"] == 1


def test_derive_without_the_heal_flag_never_replaces_a_stored_record(
        monkeypatch):
    stored_text = outcomes._encode_records([_outcome(1, [])])
    fresh = [outcomes.derive_outcome(
        ticket(1), now=NOW, run_observations=[_run()])]
    monkeypatch.setattr(
        outcomes, "derive_repository",
        lambda repo, limit, ticket_numbers: fresh)
    calls = []

    def run(args, stdin=None):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "content": base64.b64encode(stored_text.encode()).decode(),
            "size": len(stored_text), "sha": "old",
        }), stderr="")

    monkeypatch.setattr(outcomes, "_run_gh", run)

    assert outcomes.main(["derive", "--repo", REPO]) == 0
    assert not any("PUT" in call for call in calls)


def test_reproduction_old_store_ref_is_normalized_before_exact_empty_run_heal(
    monkeypatch,
):
    old_repo = "nateprich-projects/FF-Weekly-Start-Sit"
    current_repo = "nateprich-projects/Fantasy-GM"
    old_ticket = outcomes.derive_outcome(
        ticket(43, repo=old_repo), now=NOW
    )
    fresh = outcomes.derive_outcome(
        ticket(43, repo=current_repo),
        now=NOW,
        run_observations=[_run(run="finished-rename-run-43")],
    )
    monkeypatch.setattr(
        outcomes, "_read_remote", lambda repo, branch: ([old_ticket], "sha")
    )

    stored = outcomes.read_records()
    combined, healed = outcomes._heal_empty_runs(stored, [fresh])

    assert [record["ticket"] for record in stored] == [
        current_repo + "#43"
    ]
    assert healed == [fresh]
    assert combined == [fresh]
