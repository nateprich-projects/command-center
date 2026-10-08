"""Ticket #2380: deduplicated main-plus-subagent exposure table checks.

Every fixture here is synthetic: ids, paths and token counts are made up
and only mirror the field shapes of real Muse journals.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import pathlib
import stat
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import muse_exposure_table as exposure  # noqa: E402


DAY = "2026/10/06"
# Tue 2026-10-06 12:00 UTC: inside the window opening Sun 2026-10-04 17:00 PDT.
T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc).timestamp()
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).timestamp()
STANDARD = "synthetic-standard"
CONTRIBUTOR = "synthetic-contributor"


def _model_line(run_id, model, at):
    return {
        "id": "rec-model-" + run_id, "stream": {"kind": "run", "id": run_id},
        "sequence": 1, "recorded_at": int(at * 1_000_000),
        "payload_type": "run.model.configured",
        "payload": {"kind": "run.model.configured", "record": {
            "run_stream": {"kind": "run", "id": run_id},
            "model_id": model, "provider_id": "synthetic"}},
    }


def _usage_line(usage_id, run_id, session_id, at, inp=1000, cached=600,
                out=50, family="provider", **overrides):
    quantity = {"unit": "tokens", "reported": True, "input_tokens": inp,
                "cached_tokens": cached, "output_tokens": out}
    quantity.update(overrides)
    return {
        "id": "rec-" + usage_id, "stream": {"kind": "session", "id": session_id},
        "sequence": 2, "recorded_at": int(at * 1_000_000),
        "payload_type": "runtime.session",
        "payload": {"kind": "run", "run_id": run_id, "event": {
            "kind": "goal_usage_attribution", "record": {
                "usage_id": usage_id, "usage_family": family,
                "quantity": quantity,
                "owner": {"requester_kind": "main", "session_id": session_id,
                          "run_id": run_id, "owner_type": "main_root"}}}},
    }


def _link_line(child_id, at, with_path=True):
    event = {"kind": "memory_reminder_child_session_linked",
             "child_session_id": child_id, "parent_session_id": "p",
             "reminder_agent_id": "synthetic-reminder"}
    if with_path:
        event["child_session_log_path"] = (
            "subagent/" + child_id + "/session.jsonl")
    return {"id": "rec-link-" + child_id, "recorded_at": int(at * 1_000_000),
            "payload_type": "runtime.session",
            "payload": {"kind": "run", "event": event}}


def _write(path: pathlib.Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        (record if isinstance(record, str) else json.dumps(record)) + "\n"
        for record in records))


def _main(store, session, records, day=DAY):
    _write(store / day / session / "session.jsonl", records)


def _child(store, session, child, records, day=DAY):
    _write(store / day / session / "subagent" / child / "session.jsonl",
           records)


def _build(store, tmp_path, **kwargs):
    kwargs.setdefault("now", NOW)
    return exposure.build_exposure_table(
        store_root=str(store), archive_base=str(tmp_path / "archive"),
        **kwargs)


def _window(result, start_iso):
    matches = [w for w in result["coverage"]["windows"]
               if w["window_id"] == start_iso]
    assert len(matches) == 1, [w["window_id"] for w in
                               result["coverage"]["windows"]]
    return matches[0]


WINDOW = "2026-10-04T17:00:00-07:00"


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "sessions"
    # An earlier window, so the window under test is not the first one the
    # store holds (that one is always incomplete: retention may start in it).
    _main(root, "sess-old", [
        _model_line("run-old", CONTRIBUTOR, T0 - 6 * 86400),
        _usage_line("u-old", "run-old", "sess-old", T0 - 6 * 86400),
    ], day="2026/09/30")
    _main(root, "sess-a", [
        _model_line("run-a", CONTRIBUTOR, T0 - 10),
        _usage_line("u-main-1", "run-a", "sess-a", T0, inp=1000, cached=600,
                    out=50),
        _link_line("child-1", T0 + 1),
    ])
    _child(root, "sess-a", "child-1", [
        _model_line("run-c", STANDARD, T0),
        _usage_line("u-sub-1", "run-c", "child-1", T0 + 5, inp=2000,
                    cached=500, out=300),
    ])
    return root


# -- build_exposure_table: missing nested journals bias exposure ------------

def test_nested_journal_exposure_is_counted_and_window_complete(store, tmp_path):
    result = _build(store, tmp_path)
    window = _window(result, WINDOW)
    assert window["coverage"] == exposure.COMPLETE
    assert window["events_by_journal_kind"] == {"main": 1, "subagent": 1}
    assert window["by_model"][STANDARD]["fresh_input_tokens"] == 1500
    assert window["by_model"][STANDARD]["cached_input_tokens"] == 500
    assert window["by_model"][STANDARD]["output_tokens"] == 300
    assert window["by_model"][CONTRIBUTOR]["fresh_input_tokens"] == 400


def test_main_only_read_is_never_labelled_complete(store, tmp_path):
    result = _build(store, tmp_path, include_nested=False)
    window = _window(result, WINDOW)
    assert window["coverage"] == exposure.INCOMPLETE
    assert "main_only_nested_journals_not_read" in window["incomplete_reasons"]
    assert "child_journal_present_not_read" in window["incomplete_reasons"]
    # The nested model's exposure is absent, which is exactly the bias.
    assert STANDARD not in window["by_model"]


def test_missing_linked_nested_journal_is_reported_incomplete(store, tmp_path):
    os.remove(store / DAY / "sess-a" / "subagent" / "child-1" / "session.jsonl")
    result = _build(store, tmp_path)
    window = _window(result, WINDOW)
    assert window["child_links"] == {"missing": 1}
    assert window["coverage"] == exposure.INCOMPLETE
    assert "linked_child_journal_missing" in window["incomplete_reasons"]


def test_child_session_without_journal_path_is_unquantified(store, tmp_path):
    _main(store, "sess-b", [
        _model_line("run-b", CONTRIBUTOR, T0),
        _usage_line("u-main-2", "run-b", "sess-b", T0 + 2),
        _link_line("child-x", T0 + 3, with_path=False),
    ])
    window = _window(_build(store, tmp_path), WINDOW)
    assert window["child_links"]["no_journal_path"] == 1
    assert window["pathless_child_agents"] == {"synthetic-reminder": 1}
    assert window["coverage"] == exposure.UNQUANTIFIED
    assert window["unquantified_reasons"] == [
        "child_sessions_without_journal_path"]


def test_archived_nested_journal_is_read(store, tmp_path):
    import gzip
    child = store / DAY / "sess-a" / "subagent" / "child-1" / "session.jsonl"
    target = (tmp_path / "archive" / "muse" / DAY / "sess-a" / "subagent"
              / "child-1" / "session.jsonl.gz")
    target.parent.mkdir(parents=True)
    with gzip.open(target, "wt") as handle:
        handle.write(child.read_text())
    os.remove(child)
    result = _build(store, tmp_path)
    window = _window(result, WINDOW)
    assert window["coverage"] == exposure.COMPLETE
    assert window["events_by_journal_kind"]["subagent"] == 1
    assert result["coverage"]["journal_sources"] == {"live": 2, "archive": 1}


def test_malformed_provider_record_is_reported_and_incomplete(store, tmp_path):
    _main(store, "sess-m", [
        _model_line("run-m", CONTRIBUTOR, T0),
        _usage_line("u-bad", "run-m", "sess-m", T0 + 1, inp=10, cached=20),
        _usage_line("u-unrep", "run-m", "sess-m", T0 + 2, reported=False),
        _usage_line("u-tool", "run-m", "sess-m", T0 + 3, family="tool"),
    ])
    window = _window(_build(store, tmp_path), WINDOW)
    assert window["malformed"] == {"cached_exceeds_input": 1,
                                   "quantity_unreported": 1}
    assert window["coverage"] == exposure.INCOMPLETE
    assert window["events"] == 2  # the tool attribution is not a provider call


def test_first_window_in_the_store_is_incomplete(store, tmp_path):
    window = _window(_build(store, tmp_path), "2026-09-27T17:00:00-07:00")
    assert window["coverage"] == exposure.INCOMPLETE
    assert window["incomplete_reasons"] == [
        "journal_retention_starts_after_window_open"]


def test_other_device_use_is_never_excluded(store, tmp_path):
    window = _window(_build(store, tmp_path), WINDOW)
    assert window["possible_other_device_use"]["excluded"] is False


# -- deduplicate_usage_ids -------------------------------------------------

def test_cross_journal_duplicate_usage_id_counts_once(store, tmp_path):
    _child(store, "sess-a", "child-1", [
        _model_line("run-c", STANDARD, T0),
        _usage_line("u-sub-1", "run-c", "child-1", T0 + 5, inp=2000,
                    cached=500, out=300),
        # The same provider call echoed into the nested journal.
        _usage_line("u-main-1", "run-a", "sess-a", T0, inp=1000, cached=600,
                    out=50),
    ])
    result = _build(store, tmp_path)
    window = _window(result, WINDOW)
    assert window["events"] == 2
    assert window["duplicates_dropped"] == 1
    assert window["cross_journal_duplicates"] == 1
    assert window["conflicting_duplicates"] == 0
    assert window["by_model"][CONTRIBUTOR]["fresh_input_tokens"] == 400
    assert window["by_model"][CONTRIBUTOR]["output_tokens"] == 50
    assert [r["usage_id"] for r in result["rows"]].count("u-main-1") == 1
    kept = [r for r in result["rows"] if r["usage_id"] == "u-main-1"][0]
    assert kept["provenance"]["journal_kind"] == "main"


def _event(usage_id, kind, inp=10, cached=0, out=1):
    return {"usage_id": usage_id, "input_tokens": inp,
            "cached_input_tokens": cached, "output_tokens": out,
            "provenance": {"journal": kind + "/" + usage_id,
                           "journal_kind": kind}}


def test_deduplicate_usage_ids_drops_copies_without_summing():
    events = [_event("a", "main"), _event("b", "main"),
              _event("a", "subagent"), _event("a", "subagent")]
    result = exposure.deduplicate_usage_ids(events)
    assert [e["usage_id"] for e in result["events"]] == ["a", "b"]
    assert sum(e["input_tokens"] for e in result["events"]) == 20
    assert len(result["duplicates"]) == 2
    assert result["cross_journal_duplicates"] == 2
    assert result["conflicts"] == []


def test_deduplicate_usage_ids_flags_conflicting_copies(store, tmp_path):
    events = [_event("a", "main", inp=10), _event("a", "subagent", inp=99)]
    result = exposure.deduplicate_usage_ids(events)
    assert len(result["events"]) == 1
    assert result["events"][0]["input_tokens"] == 10
    assert len(result["conflicts"]) == 1

    _child(store, "sess-a", "child-1", [
        _model_line("run-c", STANDARD, T0),
        _usage_line("u-main-1", "run-a", "sess-a", T0, inp=9999),
    ])
    window = _window(_build(store, tmp_path), WINDOW)
    assert window["conflicting_duplicates"] == 1
    assert window["coverage"] == exposure.INCOMPLETE


def test_deduplicate_usage_ids_refuses_events_without_an_id():
    with pytest.raises(ValueError):
        exposure.deduplicate_usage_ids([{"usage_id": "", "provenance": {}}])


# -- join_model_identity ---------------------------------------------------

def test_join_model_identity_attaches_configured_model():
    rows = exposure.join_model_identity(
        [{"usage_id": "u", "run_id": "r1", "event_time": 5.0}],
        [{"run_id": "r1", "model": STANDARD, "recorded_at": 1.0},
         {"run_id": "r2", "model": CONTRIBUTOR, "recorded_at": 1.0}])
    assert rows[0]["model"] == STANDARD
    assert rows[0]["model_status"] == "configured"


def test_join_model_identity_flags_unknown_instead_of_guessing():
    configured = [{"run_id": "r1", "model": STANDARD, "recorded_at": 1.0}]
    rows = exposure.join_model_identity(
        [{"usage_id": "u1", "run_id": "r-other", "event_time": 5.0},
         {"usage_id": "u2", "run_id": None, "event_time": 5.0}],
        configured)
    # One model is configured anywhere, but neither event's run is it.
    assert [r["model"] for r in rows] == [exposure.MODEL_UNKNOWN] * 2
    assert [r["model_join"] for r in rows] == ["run_not_configured",
                                               "no_run_id"]


def test_join_model_identity_follows_a_reconfigured_run():
    configured = [{"run_id": "r", "model": CONTRIBUTOR, "recorded_at": 10.0},
                  {"run_id": "r", "model": STANDARD, "recorded_at": 20.0}]
    rows = exposure.join_model_identity(
        [{"usage_id": "a", "run_id": "r", "event_time": 15.0},
         {"usage_id": "b", "run_id": "r", "event_time": 25.0},
         {"usage_id": "c", "run_id": "r", "event_time": 5.0}], configured)
    assert [r["model"] for r in rows] == [CONTRIBUTOR, STANDARD,
                                          exposure.MODEL_UNKNOWN]


def test_unknown_model_row_is_unquantified_not_misfiled(store, tmp_path):
    _main(store, "sess-u", [
        _model_line("run-other", STANDARD, T0),
        _usage_line("u-orphan", "run-missing", "sess-u", T0 + 1),
    ])
    window = _window(_build(store, tmp_path), WINDOW)
    assert window["unknown_model_events"] == 1
    assert window["by_model"][exposure.MODEL_UNKNOWN]["events"] == 1
    assert window["by_model"][STANDARD]["events"] == 1  # only the nested call
    assert window["coverage"] == exposure.UNQUANTIFIED
    assert "unknown_model_events" in window["unquantified_reasons"]


# -- Sunday 17:00 America/Los_Angeles windows ------------------------------

def _utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("instant, opened", [
    # PDT: Sunday 17:00 local is Monday 00:00 UTC.
    (_utc(2026, 10, 5, 0, 0), _utc(2026, 10, 5, 0, 0)),
    (_utc(2026, 10, 4, 23, 59, 59), _utc(2026, 9, 28, 0, 0)),
    # PST: Sunday 17:00 local is Monday 01:00 UTC, not 00:00.
    (_utc(2026, 11, 9, 0, 30), _utc(2026, 11, 2, 1, 0)),
    (_utc(2026, 11, 9, 1, 0), _utc(2026, 11, 9, 1, 0)),
    # The window spanning the PDT->PST change (Sun 2026-11-01 02:00).
    (_utc(2026, 11, 2, 0, 30), _utc(2026, 10, 26, 0, 0)),
    # The window spanning the PST->PDT change (Sun 2026-03-08 02:00).
    (_utc(2026, 3, 9, 0, 0), _utc(2026, 3, 9, 0, 0)),
    (_utc(2026, 3, 8, 23, 59), _utc(2026, 3, 2, 1, 0)),
])
def test_window_start_is_sunday_1700_los_angeles(instant, opened):
    assert exposure.window_start(instant) == opened


def test_dst_windows_are_169_and_167_hours():
    fall = _utc(2026, 10, 26, 0, 0)
    spring = _utc(2026, 3, 2, 1, 0)
    assert exposure.window_end(fall) - fall == 169 * 3600
    assert exposure.window_end(spring) - spring == 167 * 3600


def test_build_files_events_across_the_pst_boundary(tmp_path):
    root = tmp_path / "sessions"
    before = _utc(2026, 11, 2, 0, 30)  # Sun 16:30 PST: old window
    after = _utc(2026, 11, 2, 1, 30)   # Sun 17:30 PST: new window
    _main(root, "sess-d", [
        _model_line("run-d", STANDARD, before - 60),
        _usage_line("u-before", "run-d", "sess-d", before),
        _usage_line("u-after", "run-d", "sess-d", after),
    ], day="2026/11/01")
    result = _build(root, tmp_path, now=_utc(2026, 11, 3, 0, 0))
    by_id = {r["usage_id"]: r["window_id"] for r in result["rows"]}
    assert by_id == {"u-before": "2026-10-25T17:00:00-07:00",
                     "u-after": "2026-11-01T17:00:00-08:00"}


# -- Output under the runtime root -----------------------------------------

def test_write_outputs_is_owner_only_under_runtime_root(store, tmp_path):
    result = _build(store, tmp_path)
    written = exposure.write_outputs(result, tmp_path / "runtime")
    directory = tmp_path / "runtime" / "muse-weekly-weights"
    assert written["directory"] == str(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (exposure.TABLE_FILENAME, exposure.COVERAGE_FILENAME):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    coverage = json.loads((directory / exposure.COVERAGE_FILENAME).read_text())
    states = {w["window_id"]: w["coverage"]
              for w in coverage["coverage"]["windows"]}
    assert states[WINDOW] == exposure.COMPLETE


def test_summary_carries_no_ids_or_paths(store, tmp_path):
    text = json.dumps(exposure.summary(_build(store, tmp_path)))
    for secret in ("u-main-1", "u-sub-1", "sess-a", "child-1", "run-a", "sess-old",
                   str(tmp_path)):
        assert secret not in text
