"""Heartbeat start and finish read GitHub strictly (#2175, plan #1750).

An unread GitHub heartbeat used to read as "no records". With GitHub answering
502 and another lane's start still in the spool, `heartbeat finish --run <id>`
saw one start that was not its own, refused ("no start recorded"), exited 2
and wrote nothing, so a completed run read as never finished. A quota-parked
Muse run recorded "no paired Muse usage total matches" when the total had
simply not been read.

A failed read is now lost, never empty: start and finish carry on from the
spool alone, finish trusts the run id it was given, and the fields it could
not establish are written as unknown. Only a missing file (HTTP 404) is an
empty history.
"""

from __future__ import annotations

import base64
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import session_usage  # noqa: E402

NOW = 1_788_600_000.0
UNKNOWN_TOKENS = {kind: None for kind in session_usage.TOKEN_KINDS}


def start(run, at=NOW, agent="codex", **extra):
    record = {"run": run, "agent": agent, "phase": "start", "ts": int(at)}
    record.update(extra)
    return record


def bind(run, work, at=NOW + 1, agent="codex"):
    return {"run": run, "agent": agent, "phase": "bind", "ts": int(at),
            "do": "ticket", "work": work}


def finish(run, at=NOW + 60, agent="codex", outcome="done"):
    return {"run": run, "agent": agent, "phase": "finish", "ts": int(at),
            "outcome": outcome}


def api_event(run, points, calls, at=NOW + 5, agent="codex"):
    return {"run": run, "agent": agent, "phase": "api_cost", "ts": int(at),
            "api_cost": {"graphql_points": points, "gh_calls": calls}}


def job(run, name, at=NOW + 2, agent="codex"):
    return {"run": run, "agent": agent, "phase": "job", "ts": int(at),
            "job": name}


def bad_gateway(*args, **kwargs):
    raise heartbeat.HeartbeatError("gh: Bad Gateway (HTTP 502)")


def not_found(*args, **kwargs):
    raise heartbeat.HeartbeatError("gh: Not Found (HTTP 404)")


def github_serving(records):
    """A `gh` whose Contents read returns ``records`` and whose writes fail.

    The writes fail at `_ensure_branch`, before the drain's retry loop, so an
    append is kept in the test's spool and reports "spooled".
    """
    content = "".join(json.dumps(r, sort_keys=True) + "\n" for r in records)
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

    def gh(*args, **kwargs):
        if len(args) == 2 and args[0] == "api" and "/contents/" in args[1]:
            return json.dumps({
                "encoding": "base64", "size": len(content),
                "content": encoded, "sha": "blob-sha",
            })
        raise heartbeat.HeartbeatError("gh: writes are offline in this test")

    return gh


@pytest.fixture
def quiet_machine(monkeypatch):
    """Keep the record fields that read this machine's state out of the test."""
    monkeypatch.setattr(heartbeat, "usage_snapshot", lambda agent: None)
    monkeypatch.setattr(heartbeat, "repo_state", lambda: None)
    monkeypatch.setattr(heartbeat, "runtime_state", lambda: None)
    monkeypatch.setattr(heartbeat, "detect_model", lambda agent: {})
    monkeypatch.setattr(heartbeat, "input_usage", lambda agent: None)
    monkeypatch.setattr(heartbeat, "session_id", lambda agent: None)


def spooled(agent, phase):
    return [r for r in heartbeat._spooled(agent) if r.get("phase") == phase]


def unknown_lines(err):
    return [line for line in err.splitlines()
            if "could not be read" in line and "unknown" in line]


# -- Accept: the reproduction -------------------------------------------------

def test_finish_with_github_unread_writes_the_finish_and_exits_zero(
        monkeypatch, capsys, quiet_machine):
    """main: exit 2, "no start recorded", nothing written."""
    monkeypatch.setattr(heartbeat, "gh", bad_gateway)
    heartbeat._spool("codex", start("other-lane"))

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
    ]) == 0

    [record] = spooled("codex", "finish")
    assert record["run"] == "mine"
    assert record["outcome"] == "done"
    assert "unresolved" not in record
    # Unknown, not zero and not "no funnel command": GitHub was not read.
    assert record["api_cost"] == {"graphql_points": None, "gh_calls": None}
    assert record["graphql_by_caller"] is None
    assert record["token_usage"] == UNKNOWN_TOKENS
    assert "job" not in record

    err = capsys.readouterr().err
    assert "no start recorded" not in err
    [line] = unknown_lines(err)
    assert "HTTP 502" in line
    assert "API cost, job and token fields are unknown" in line

    # The other lane's start is untouched and still open.
    assert [r["run"] for r in heartbeat.open_starts(
        heartbeat._spooled("codex"))] == ["other-lane"]


def test_an_unread_github_never_prices_the_run_from_the_spool_alone(
        monkeypatch, capsys, quiet_machine):
    """The spool holds only what has not drained; the rest is on GitHub.

    Summing the spooled part would write a measured-looking undercount.
    """
    monkeypatch.setattr(heartbeat, "gh", bad_gateway)
    monkeypatch.setattr(
        session_usage, "usage_for_session",
        lambda *args, **kwargs: pytest.fail(
            "an unread run must not read its session's tokens"),
    )
    for record in (
            start("mine", session_id="session-1"), bind("mine", "o/r#9"),
            job("mine", "command-center-tickets-hourly"),
            api_event("mine", 7, 2)):
        heartbeat._spool("codex", record)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
        "--work", "o/r#9",
    ]) == 0

    [record] = spooled("codex", "finish")
    assert record["run"] == "mine"
    assert record["api_cost"] == {"graphql_points": None, "gh_calls": None}
    assert record["graphql_by_caller"] is None
    assert record["token_usage"] == UNKNOWN_TOKENS
    assert "job" not in record
    assert len(unknown_lines(capsys.readouterr().err)) == 1


def test_an_unread_github_still_refuses_a_finish_the_spool_shows(
        monkeypatch, capsys, quiet_machine):
    """A finish that was read is a fact; only absence is unproven."""
    monkeypatch.setattr(heartbeat, "gh", bad_gateway)
    heartbeat._spool("codex", start("mine"))
    heartbeat._spool("codex", finish("mine"))

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
    ]) == 2

    assert "already finished" in capsys.readouterr().err
    assert len(spooled("codex", "finish")) == 1


def test_a_missing_github_file_is_an_empty_history_not_an_unread_one(
        monkeypatch, capsys, quiet_machine):
    """HTTP 404 is a valid empty history: the spool is the whole record."""
    monkeypatch.setattr(heartbeat, "gh", not_found)
    heartbeat._spool("codex", start("mine"))
    heartbeat._spool("codex", job("mine", "command-center-tickets-hourly"))
    heartbeat._spool("codex", api_event("mine", 7, 2))

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
    ]) == 0

    [record] = spooled("codex", "finish")
    assert record["api_cost"] == {"graphql_points": 7, "gh_calls": 2}
    assert record["job"] == "command-center-tickets-hourly"
    assert unknown_lines(capsys.readouterr().err) == []


def test_a_read_github_finish_takes_its_fields_from_github_and_spool(
        monkeypatch, capsys, quiet_machine):
    """The strict read is the whole read when GitHub answers."""
    monkeypatch.setattr(heartbeat, "gh", github_serving([
        start("mine"), job("mine", "command-center-tickets-hourly"),
        api_event("mine", 7, 2),
    ]))
    heartbeat._spool("codex", api_event("mine", 5, 3, at=NOW + 30))

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
    ]) == 0

    [record] = spooled("codex", "finish")
    assert record["api_cost"] == {"graphql_points": 12, "gh_calls": 5}
    assert record["job"] == "command-center-tickets-hourly"
    assert unknown_lines(capsys.readouterr().err) == []


def test_a_read_github_still_refuses_a_run_that_never_began(
        monkeypatch, capsys, quiet_machine):
    monkeypatch.setattr(heartbeat, "gh", github_serving([start("other")]))

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
    ]) == 2

    assert "never began" in capsys.readouterr().err
    assert spooled("codex", "finish") == []


def test_an_unreadable_line_does_not_prove_the_start_absent(
        monkeypatch, capsys, quiet_machine):
    """The run's own start may be the line that could not be read (#2173)."""
    monkeypatch.setattr(heartbeat, "gh", not_found)
    heartbeat._spool("codex", start("other-lane"))
    with open(heartbeat._spool_path("codex"), "a") as fh:
        fh.write('{"run": "mine", "agent": "codex", "phase": "sta\n')

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
    ]) == 0

    assert [r["run"] for r in spooled("codex", "finish")] == ["mine"]


# -- resolve_run --------------------------------------------------------------

def test_resolve_run_trusts_the_given_id_when_the_read_is_incomplete():
    records = [start("other-lane")]

    with pytest.raises(heartbeat.HeartbeatError, match="never began"):
        heartbeat.resolve_run(records, "mine")
    assert heartbeat.resolve_run(records, "mine", complete=False) == (
        "mine", None)

    with pytest.raises(heartbeat.HeartbeatError, match="already finished"):
        heartbeat.resolve_run(
            records + [start("mine"), finish("mine")], "mine",
            complete=False)


# -- the read itself ----------------------------------------------------------

def test_a_strict_read_raises_on_an_unread_github_and_reads_404_as_empty(
        monkeypatch):
    heartbeat._spool("codex", start("spooled"))

    monkeypatch.setattr(heartbeat, "gh", bad_gateway)
    assert heartbeat.read("codex") == [start("spooled")]
    with pytest.raises(heartbeat.HeartbeatError, match="HTTP 502"):
        heartbeat.read("codex", strict=True)

    monkeypatch.setattr(heartbeat, "gh", not_found)
    assert heartbeat.read("codex", strict=True) == [start("spooled")]


def test_a_strict_read_counts_a_bad_durable_line_rather_than_refusing(
        monkeypatch):
    """Strict about the fetch, tolerant per line: `parse_records` (#2173)."""
    good = start("durable")
    content = json.dumps(good) + "\n{not json}\n"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    monkeypatch.setattr(
        heartbeat, "gh",
        lambda *args, **kwargs: json.dumps({
            "encoding": "base64", "size": len(content), "content": encoded,
        }),
    )

    records = heartbeat.read("codex", strict=True)
    assert records == [good]
    assert records.unreadable == 1


# -- start --------------------------------------------------------------------

def test_start_with_github_unread_starts_from_the_spool_alone(
        monkeypatch, capsys, quiet_machine):
    """Spool first, push after: an unread GitHub never stops a start.

    The same-session re-begin close still runs over what the spool holds.
    """
    monkeypatch.setattr(heartbeat, "gh", bad_gateway)
    monkeypatch.setattr(heartbeat, "session_id", lambda agent: "session-a")
    monkeypatch.setattr(heartbeat.uuid, "uuid4",
                        lambda: SimpleNamespace(hex="fresh-run-id-0000"))
    heartbeat._spool("codex", start("old", session_id="session-a"))
    heartbeat._spool("codex", bind("old", "o/r#9"))

    assert heartbeat.main(["start", "--agent", "codex"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "fresh-run-id"
    [close] = spooled("codex", "finish")
    assert close["run"] == "old" and close["re_begun_by"] == "fresh-run-id"
    assert [r["run"] for r in spooled("codex", "start")] == [
        "old", "fresh-run-id"]
    assert [line for line in captured.err.splitlines()
            if "could not be read" in line and "HTTP 502" in line]


def test_start_closes_a_same_session_bound_start_read_from_github(
        monkeypatch, capsys, quiet_machine):
    """The re-begin close is intact under the strict read."""
    monkeypatch.setattr(heartbeat, "gh", github_serving([
        start("old", session_id="session-a"), bind("old", "o/r#9"),
        start("other-session", session_id="session-b"),
        bind("other-session", "o/r#10"),
    ]))
    monkeypatch.setattr(heartbeat, "session_id", lambda agent: "session-a")
    monkeypatch.setattr(heartbeat.uuid, "uuid4",
                        lambda: SimpleNamespace(hex="fresh-run-id-0000"))

    assert heartbeat.main(["start", "--agent", "codex"]) == 0

    [close] = spooled("codex", "finish")
    assert close["run"] == "old"
    assert close["outcome"] == "skipped-blocked"
    assert close["re_begun_by"] == "fresh-run-id"
    assert "could not be read" not in capsys.readouterr().err


# -- the finish reads its run through the per-run view ------------------------

def test_finish_takes_start_binding_job_and_api_cost_from_the_run_view(
        monkeypatch, capsys, quiet_machine):
    """Through `run_views` (#2174), the one per-run reading, not beside it."""
    records = [
        start("mine", session_id="from-records"), bind("mine", "o/r#9"),
        job("mine", "from-records"), api_event("mine", 1, 1),
    ]
    monkeypatch.setattr(heartbeat, "read",
                        lambda agent, **kwargs: list(records))
    real_views = heartbeat.run_views

    def views(rows):
        found = real_views(rows)
        view = found.get("mine")
        if view is not None:
            view["start"] = dict(view["start"], session_id="from-view")
            view["binding"] = dict(view["binding"], work="o/r#12")
            view["job"] = "from-view"
            view["api_cost"] = {
                "graphql_points": {"state": "measured", "value": 99},
                "gh_calls": {"state": "measured", "value": 98},
            }
        return found

    monkeypatch.setattr(heartbeat, "run_views", views)
    sessions = []

    def usage_for_session(agent, session, **kwargs):
        sessions.append(session)
        return {kind: 1 for kind in session_usage.TOKEN_KINDS}

    monkeypatch.setattr(session_usage, "usage_for_session", usage_for_session)

    assert heartbeat.main([
        "finish", "--agent", "codex", "--run", "mine", "--outcome", "done",
        "--work", "o/r#12",
    ]) == 0, capsys.readouterr().err

    [record] = spooled("codex", "finish")
    assert record["job"] == "from-view"
    assert record["api_cost"] == {"graphql_points": 99, "gh_calls": 98}
    assert sessions == ["from-view"]


# -- Accept: record_muse_quota_hit -------------------------------------------

def _next_weekly_reset(after):
    moment = datetime.fromtimestamp(after, tz=timezone.utc)
    reset = (moment + timedelta(days=(7 - moment.weekday()) % 7)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    if reset <= moment:
        reset += timedelta(days=7)
    return reset


def _paired_window(reset_epoch):
    window = {"resets_at": reset_epoch, "spent_dollars": 4.0}
    return [
        start("paired-run", at=NOW - 600, agent="muse",
              usage={"seven_day": window}),
        dict(finish("paired-run", at=NOW - 300, agent="muse"),
             usage={"seven_day": dict(window, spent_dollars=5.25)}),
    ]


def _quota_hit(reset):
    assert heartbeat.record_muse_quota_hit(
        "quota-run", reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Subscription quota exhausted.", observed_at=NOW,
    ) == "spooled"
    [record] = spooled("muse", "quota_hit")
    assert record["classification"] == "weekly"
    return record


@pytest.mark.parametrize("spool_holds_the_window", [False, True])
def test_quota_hit_with_github_unread_records_the_total_could_not_be_read(
        monkeypatch, spool_holds_the_window):
    """main: "no paired Muse usage total matches this weekly reset", or a
    total priced from the spool alone."""
    reset = _next_weekly_reset(NOW)
    monkeypatch.setattr(heartbeat, "gh", bad_gateway)
    if spool_holds_the_window:
        for record in _paired_window(reset.timestamp()):
            heartbeat._spool("muse", record)

    record = _quota_hit(reset)

    assert record["degraded_note"] == (
        "paired Muse usage total could not be read")
    assert record["anchored_window_total_dollars"] is None
    assert record["anchored_window_runs"] is None


def test_quota_hit_reads_the_paired_total_from_github(monkeypatch):
    reset = _next_weekly_reset(NOW)
    monkeypatch.setattr(heartbeat, "gh",
                        github_serving(_paired_window(reset.timestamp())))

    record = _quota_hit(reset)

    assert record["degraded_note"] is None
    assert record["anchored_window_total_dollars"] == 1.25
    assert record["anchored_window_runs"] == 1


def test_quota_hit_with_no_github_file_reads_the_spool_as_the_history(
        monkeypatch):
    reset = _next_weekly_reset(NOW)
    monkeypatch.setattr(heartbeat, "gh", not_found)
    for record in _paired_window(reset.timestamp()):
        heartbeat._spool("muse", record)

    record = _quota_hit(reset)

    assert record["degraded_note"] is None
    assert record["anchored_window_total_dollars"] == 1.25
