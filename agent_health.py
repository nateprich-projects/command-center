#!/usr/bin/env python3
"""Shared assessment of heartbeat conditions worth surfacing.

The local funnel and the Actions watchdog read the same heartbeat rows. Keeping
the thresholds and wording here prevents the two readers from drifting apart.
"""

from __future__ import annotations

import datetime
import os
import statistics
from typing import Dict, List, Optional, Tuple

#: Calibrated by the offline replay in tests/test_watchdog_calibration.py against
#: the heartbeat branch snapshot in tests/fixtures/heartbeat_history.json.
NORMAL_PERCENTILE = 90
NORMAL_MULTIPLE = 5
SILENCE_FLOOR_SECONDS = 3600
#: A live agent with too little history for cadence inference is still silent
#: when its last recorded run is this old. This is deliberately independent of
#: the one-hour floor used by the inferred cadence path.
ABSOLUTE_SILENCE_SECONDS = 6 * 3600
MINIMUM_HISTORY = 8

#: The normal rhythm is learned from the trailing fortnight. This is deliberately
#: separate from the four provisional calibration parameters above: the ticket
#: calls for the window shown in the alarm evidence, "p90 over 14 days".
HISTORY_WINDOW_SECONDS = 14 * 86400

#: A run still unfinished after this long is presumed dead. Matches the lock TTL
#: in funnel.py — the same two hours after which its claim becomes takeable.
UNFINISHED_SECONDS = 2 * 3600

#: One dying run is noise. Three in a week means runs are dying.
DYING_THRESHOLD = 3
#: A single open start is normal while it is within this floor and the agent's
#: own typical completed-run duration. The floor avoids alarming on short runs
#: when the history contains only very fast no-op sessions.
OPEN_START_FLOOR_SECONDS = 15 * 60
OPEN_START_MULTIPLE = 10
ERROR_THRESHOLD = 3
WEEK = 7 * 86400

#: A Codex run refused because its settings differ from `codex_run.py`
#: (#1316). One is enough to report: the refusal repeats on every run until
#: someone fixes the automation, and the lane does no work meanwhile. A day
#: rather than a week, so the alarm clears soon after the fix.
CONFIG_DRIFT_OUTCOME = "config-drift"
CONFIG_DRIFT_WINDOW = 86400

#: The provider hold the Muse lanes write when a window is spent. The same file
#: the runners check (`scripts/muse-quota-hold.sh`), read here so the watchdog
#: and the lanes cannot disagree about whether an agent is parked or dead.
#: One ISO-8601 stamp, nothing else.
QUOTA_HOLD_ENV = "MUSE_QUOTA_HOLD_FILE"
QUOTA_HOLD_DEFAULT = "~/.claude/command-center-muse-quota-hold"

#: Which agent the hold speaks for. It is Muse's file and says nothing about
#: any other lane's silence.
QUOTA_HOLD_AGENT = "muse"

#: Which agent an open auth outage parks (#1946). Unlike the hold, it is read
#: from the heartbeat records themselves (`heartbeat.muse_auth_outage`, the
#: reader the lanes' gate also uses), so the Actions watchdog sees it too.
AUTH_OUTAGE_AGENT = "muse"


def quota_hold_path() -> str:
    """Where the hold lives: the environment's answer, else the default."""
    return os.environ.get(QUOTA_HOLD_ENV) or os.path.expanduser(
        QUOTA_HOLD_DEFAULT
    )


def quota_hold_until(path: Optional[str] = None) -> Optional[float]:
    """Epoch seconds of a recorded provider hold, or ``None``.

    Missing, empty, unreadable and unparseable all mean *no hold* — the same
    direction the runners take, and the safe one: a hold that cannot be
    understood must not silence a dead lane. Unlike the shell reader, this one
    never removes the file. It is a watchdog, and a diagnostic that deletes the
    evidence it read is worse than one that reports nothing.

    An expired stamp is returned as-is rather than dropped, because the caller
    needs it: silence after a park is measured from the reset, not from the
    last record before it.
    """
    try:
        with open(path or quota_hold_path()) as handle:
            stamp = handle.read().strip()
    except OSError:
        return None
    if not stamp:
        return None
    if stamp.endswith("Z"):
        stamp = stamp[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def _hold_stamp(hold_until: float) -> str:
    """The reset instant, rendered the way the hold file writes it."""
    return datetime.datetime.fromtimestamp(
        hold_until, datetime.timezone.utc
    ).isoformat()


def _deduplicated(rows: List[Dict]) -> List[Dict]:
    """Rows with byte-identical duplicates removed.

    The cadence inference reads the gaps between records, and a record written
    twice with the same ``ts`` is a gap of zero that never happened — it drags
    the p90 down and so tightens the silence threshold. Same canonical rule as
    the counting readers, applied to the timing one (#1225).
    """
    import heartbeat

    return heartbeat.distinct_records(rows)


def _history(
    rows: List[Dict],
    now: float,
    *,
    history_window_seconds: int = HISTORY_WINDOW_SECONDS,
) -> Tuple[List[float], List[float]]:
    """Return recent record timestamps and their positive consecutive gaps."""
    cutoff = now - history_window_seconds
    timestamps = sorted(
        float(row["ts"])
        for row in rows
        if isinstance(row.get("ts"), (int, float))
        and cutoff <= float(row["ts"]) <= now
    )
    gaps = [later - earlier for earlier, later in zip(timestamps, timestamps[1:])
            if later > earlier]
    return timestamps, gaps


def _percentile(values: List[float], percentile: float) -> float:
    """Return a linearly interpolated percentile without a third-party library."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _normal_gap(
    rows: List[Dict],
    now: float,
    *,
    normal_percentile: float = NORMAL_PERCENTILE,
    minimum_history: int = MINIMUM_HISTORY,
    history_window_seconds: int = HISTORY_WINDOW_SECONDS,
) -> Optional[Tuple[float, float, int]]:
    """Return ``(normal gap, latest record, record count)`` when inferable."""
    timestamps, gaps = _history(
        _deduplicated(rows), now,
        history_window_seconds=history_window_seconds,
    )
    if len(gaps) < minimum_history:
        return None
    return _percentile(gaps, normal_percentile), timestamps[-1], len(timestamps)


def _duration(seconds: float) -> str:
    """Render a duration compactly enough to scan in an issue body."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return "{}s".format(total)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append("{}d".format(days))
    if hours:
        parts.append("{}h".format(hours))
    if minutes:
        parts.append("{}m".format(minutes))
    if not parts:
        parts.append("0m")
    return "".join(parts)


def _window_label(history_window_seconds: int = HISTORY_WINDOW_SECONDS) -> str:
    days = history_window_seconds / 86400
    if days.is_integer():
        return "{} days".format(int(days))
    return _duration(history_window_seconds)


def _completed_run_durations(
    runs: List[Dict],
    now: float,
    *,
    history_window_seconds: int = HISTORY_WINDOW_SECONDS,
) -> List[float]:
    """Return per-run start-to-finish durations completed in the history window."""
    import heartbeat

    cutoff = now - history_window_seconds
    durations = []
    for run in runs:
        start = run.get("start")
        finish = run.get("finish")
        if (
            run.get("pairing") != heartbeat.PAIRING_FINISHED
            or not isinstance(start, dict)
            or not isinstance(finish, dict)
            or heartbeat.is_rebegin_finish(finish)
        ):
            continue
        started_at = start.get("ts")
        finished_at = finish.get("ts")
        if (
            isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or isinstance(finished_at, bool)
            or not isinstance(finished_at, (int, float))
        ):
            continue
        started_at = float(started_at)
        finished_at = float(finished_at)
        if (
            started_at > now
            or finished_at < cutoff
            or finished_at < started_at
            or finished_at > now
        ):
            continue
        durations.append(finished_at - started_at)
    return durations


def _recent_outcomes(
    runs: List[Dict],
    unattributed: List[Dict],
    now: float,
    outcome: str,
    week: int,
) -> List[Dict]:
    """Return one record per run with ``outcome`` in the diagnostic week.

    A count read from these records is a count of *runs*. Reading rows instead
    made the alarm report the heartbeat's own write duplication: 67 muse errors
    against 55 true runs, measured 2026-09-21 (#1225).

    Read from the per-run view (#2176). A run's outcome is on its finish or on
    one of its non-terminal events — a gate's ``config-drift`` refusal is an
    event, usually with no finish for that run (#1316), and so is a misfiled
    finish re-attached to its run — and the newest of those carrying
    ``outcome`` stands for the run, the finish on a tie. A finish carries the
    view's error class, so one written before the class was recorded is
    classified on read.

    ``unattributed`` are the distinct records that name no run: an unresolved
    finish, an event filed against no open run. They cannot be attributed, so
    each counts on its own, as `heartbeat.one_record_per_run` always kept them;
    dropping them would quietly lose history.
    """
    def recent(record) -> bool:
        timestamp = record.get("ts") if isinstance(record, dict) else None
        return (
            isinstance(record, dict)
            and record.get("outcome") == outcome
            and not isinstance(timestamp, bool)
            and isinstance(timestamp, (int, float))
            and now - float(timestamp) < week
        )

    found = []
    for run in runs:
        finish = run.get("finish")
        candidates = [
            dict(finish, error_class=run.get("error_class"))
            if isinstance(finish, dict) else None
        ] + list(run.get("events") or [])
        newest = None
        for record in candidates:
            if recent(record) and (
                newest is None or float(record["ts"]) > float(newest["ts"])
            ):
                newest = record
        if newest is not None:
            found.append(newest)
    found.extend(record for record in unattributed if recent(record))
    return found


def _auth_outage_condition(agent: str, opened: Dict) -> str:
    """The Muse auth-outage park, in place of the silence alarm (#2176).

    ``opened`` is the finish that opened the outage, as
    `heartbeat.muse_auth_outage` returns it.
    """
    opened_at = opened.get("ts")
    if isinstance(opened_at, bool) or not isinstance(opened_at, (int, float)):
        opened_when = "an unrecorded time"
    else:
        opened_when = "<t:{}:f>".format(int(opened_at))
    return (
        "`{}`: parked by an open Muse auth outage, opened by the finish at "
        "{}; no silence alarm while it holds. A person has to sign Muse in "
        "again: a successful login probe clears it.".format(agent, opened_when)
    )


def _runtime_head(row: Dict) -> Optional[str]:
    runtime = row.get("runtime")
    if not isinstance(runtime, dict):
        return None
    head = runtime.get("head")
    return head.strip() if isinstance(head, str) and head.strip() else None


def assess(
    agent: str,
    rows: List[Dict],
    now: float,
    *,
    normal_percentile: Optional[float] = None,
    normal_multiple: Optional[int] = None,
    silence_floor_seconds: Optional[int] = None,
    absolute_silence_seconds: Optional[int] = None,
    minimum_history: Optional[int] = None,
    history_window_seconds: Optional[int] = None,
    unfinished_seconds: Optional[int] = None,
    dying_threshold: Optional[int] = None,
    week: Optional[int] = None,
    error_threshold: Optional[int] = None,
    hold_until: Optional[float] = None,
    regressions_only: bool = False,
) -> List[str]:
    """Return raised conditions in the watchdog's existing wording.

    The optional keyword arguments keep the Actions wrapper's historical test
    hooks intact while leaving one implementation of the assessment rules.

    ``hold_until`` is a recorded provider park, in epoch seconds. While it is
    in the future the agent's silence is explained rather than alarming: it
    reports as parked, naming the reset and the last record before the park,
    and raises no silence condition for the parked span. Once it has passed,
    silence is measured from the reset rather than from that last record —
    a lane that could not run was not quiet, and counting the park against it
    would alarm for hours after the thing was fixed.

    Every other condition is unchanged by a hold. A park explains a gap in
    records; it does not explain a run that died or three that errored.

    ``regressions_only`` is used by the brief: it suppresses floor and
    unclassified finishes and requires the recorded runtime head so every
    surfaced regression can be traced to its code revision. The watchdog keeps
    the default all-error assessment.

    An open Muse auth outage (#1946) is the second park, and it is read from
    the records rather than passed in (#2176). While no successful login
    probe has cleared it, Muse's silence reports as that park — naming the
    outage and the time of the finish that opened it — in place of the
    silence alarm, and it stays a raised condition however recent the
    outage, because a person has to sign in again. Once a probe clears it,
    silence reads as normal again, measured from the newest record (the probe
    is one). A quota hold keeps its own wording beside it, and every other
    condition is unchanged by it.

    The run-level inputs — open starts, bindings, completed durations and
    per-run outcomes — come from the per-run heartbeat view
    (`heartbeat.run_views`, #2174), built once here (#2176).
    """
    normal_percentile = (
        NORMAL_PERCENTILE if normal_percentile is None else normal_percentile
    )
    normal_multiple = NORMAL_MULTIPLE if normal_multiple is None else normal_multiple
    silence_floor_seconds = (
        SILENCE_FLOOR_SECONDS
        if silence_floor_seconds is None else silence_floor_seconds
    )
    absolute_silence_seconds = (
        ABSOLUTE_SILENCE_SECONDS
        if absolute_silence_seconds is None else absolute_silence_seconds
    )
    minimum_history = MINIMUM_HISTORY if minimum_history is None else minimum_history
    history_window_seconds = (
        HISTORY_WINDOW_SECONDS
        if history_window_seconds is None else history_window_seconds
    )
    unfinished_seconds = (
        UNFINISHED_SECONDS if unfinished_seconds is None else unfinished_seconds
    )
    dying_threshold = DYING_THRESHOLD if dying_threshold is None else dying_threshold
    week = WEEK if week is None else week
    error_threshold = ERROR_THRESHOLD if error_threshold is None else error_threshold

    problems: List[str] = []

    if not rows:
        return []

    import heartbeat

    retired = getattr(heartbeat, "RETIRED_AGENTS", frozenset())
    # The run-level inputs come from the per-run view (#2176), built once:
    # each is a count of runs, not of rows (#1225).
    runs = list(heartbeat.run_views(rows).values())
    unattributed = [
        row for row in heartbeat.distinct_records(rows)
        if not isinstance(row.get("run"), str) or not row.get("run")
    ]
    # The second park (#1946): read from the records through the same reader
    # the lanes' gate uses, so the watchdog and the lanes cannot disagree.
    auth_outage_finish = (
        heartbeat.muse_auth_outage(rows)
        if agent == AUTH_OUTAGE_AGENT and agent not in retired else None
    )
    auth_outage_open = auth_outage_finish is not None
    if auth_outage_open:
        problems.append(_auth_outage_condition(agent, auth_outage_finish))
    inferred = _normal_gap(
        rows,
        now,
        normal_percentile=normal_percentile,
        minimum_history=minimum_history,
        history_window_seconds=history_window_seconds,
    )
    parked = hold_until is not None and hold_until > now
    if parked and inferred is not None:
        problems.append(
            "`{}`: parked until {} (provider quota). Last record before the "
            "park at <t:{}:f>; no silence alarm while the park holds.".format(
                agent, _hold_stamp(hold_until), int(inferred[1])
            )
        )
    elif not auth_outage_open and inferred is not None:
        normal, latest, record_count = inferred
        if hold_until is not None:
            # The park is over. Silence since the reset is the honest gap;
            # the records before it are older than the reason for the quiet.
            latest = max(latest, hold_until)
        quiet_for = now - latest
        threshold = max(silence_floor_seconds, normal_multiple * normal)
        if quiet_for > threshold:
            ratio = quiet_for / normal if normal else float("inf")
            ratio_text = "{:.0f}x normal".format(ratio) if ratio != float("inf") else "unbounded"
            problems.append(
                "`{}`: normal gap {} (p{} over {}, {} records). "
                "Nothing recorded for {} — {} (alarm threshold {}x normal; "
                "last at <t:{}:f>).".format(
                    agent,
                    _duration(normal),
                    normal_percentile,
                    _window_label(history_window_seconds),
                    record_count,
                    _duration(quiet_for),
                    ratio_text,
                    normal_multiple,
                    int(latest),
                )
            )
    elif parked and agent not in retired:
        problems.append(
            "`{}`: parked until {} (provider quota); no silence alarm while "
            "the park holds.".format(agent, _hold_stamp(hold_until))
        )
    elif not auth_outage_open and inferred is None and agent not in retired:
        timestamps = [
            float(row["ts"])
            for row in rows
            if not isinstance(row.get("ts"), bool)
            and isinstance(row.get("ts"), (int, float))
            and float(row["ts"]) <= now
        ]
        if timestamps:
            latest = max(timestamps)
            if hold_until is not None:
                latest = max(latest, hold_until)
            quiet_for = now - latest
            if quiet_for > absolute_silence_seconds:
                problems.append(
                    "`{}`: absolute silence floor {} exceeded. "
                    "Nothing recorded for {} — last at <t:{}:f>; "
                    "cadence unavailable with fewer than {} gaps in the "
                    "trailing {} history.".format(
                        agent,
                        _duration(absolute_silence_seconds),
                        _duration(quiet_for),
                        int(latest),
                        minimum_history,
                        _window_label(history_window_seconds),
                    )
                )

    # An unresolved finish counts as a finish for one of its candidates. A run
    # that completed but could not name itself must not be reported as dying —
    # that false alarm is the failure this signal exists to avoid.
    open_starts = [
        run["start"] for run in runs
        if run.get("pairing") == heartbeat.PAIRING_OPEN
        and isinstance(run.get("start"), dict)
    ]
    completed_durations = _completed_run_durations(
        runs, now, history_window_seconds=history_window_seconds
    )
    if len(open_starts) == 1 and completed_durations:
        open_start = open_starts[0]
        started_at = open_start.get("ts")
        if isinstance(started_at, (int, float)) and started_at <= now:
            age = now - started_at
            median = statistics.median(completed_durations)
            threshold = max(
                OPEN_START_FLOOR_SECONDS,
                OPEN_START_MULTIPLE * median,
            )
            if age > threshold:
                problems.append(
                    "`{}` has one open start `{}` aged {} — over the {} "
                    "threshold (10x median completed-run length {}, floor {}). "
                    "Started at <t:{}:f>.".format(
                        agent,
                        open_start.get("run"),
                        _duration(age),
                        _duration(threshold),
                        _duration(median),
                        _duration(OPEN_START_FLOOR_SECONDS),
                        int(started_at),
                    )
                )

    bound_runs = {
        run.get("run") for run in runs if isinstance(run.get("binding"), dict)
    }
    dying = [
        r for r in open_starts
        if now - (r.get("ts") or 0) > unfinished_seconds
        and now - (r.get("ts") or 0) < week
    ]
    bound_dying = [r for r in dying if r.get("run") in bound_runs]
    never_bound = [r for r in dying if r.get("run") not in bound_runs]
    if len(bound_dying) >= dying_threshold:
        problems.append(
            "`{}` has {} runs this week that started and never finished — "
            "tickets {}. That is what a session killed mid-work by a rate limit "
            "looks like. Check whether the reserves in `usage.py` are too low.".format(
                agent,
                len(bound_dying),
                ", ".join(str(r.get("ticket")) for r in bound_dying[-5:]),
            )
        )
    if len(never_bound) >= dying_threshold:
        problems.append(
            "`{}` has {} begins this week that started and never returned a job — "
            "the begin reply most likely passed the 180 s budget (#1519). "
            "This is not a usage-reserve problem.".format(
                agent,
                len(never_bound),
            )
        )

    errored = _recent_outcomes(runs, unattributed, now, "errored", week)
    if regressions_only:
        errored = [
            row for row in errored
            if row.get("error_class") == "regression"
            and _runtime_head(row) is not None
        ]
    else:
        begin_timeouts = [
            row for row in errored
            if row.get("error_class") == "begin-timeout"
        ]
        if len(begin_timeouts) >= error_threshold:
            notes = [r.get("note") for r in begin_timeouts[-3:] if r.get("note")]
            problems.append(
                "`{}` had {} begin-timeout errors this week.{}".format(
                    agent, len(begin_timeouts),
                    (" Most recent: " + "; ".join(notes)) if notes else "",
                )
            )
            # A classified begin timeout has its own diagnosis above. Do not
            # repeat it in the generic errored-runs condition. Below the
            # class threshold, keep it in the aggregate so mixed failures
            # still reach the existing error threshold.
            errored = [
                row for row in errored
                if row.get("error_class") != "begin-timeout"
            ]
    if len(errored) >= error_threshold:
        if regressions_only:
            details = []
            for row in errored[-3:]:
                detail = "head {}".format(_runtime_head(row))
                note = row.get("note")
                if isinstance(note, str) and note.strip():
                    detail += ": " + note.strip()
                details.append(detail)
            problems.append(
                "`{}` had {} regression errors this week. Latest: {}".format(
                    agent, len(errored), "; ".join(details)
                )
            )
        else:
            notes = [r.get("note") for r in errored[-3:] if r.get("note")]
            problems.append(
                "`{}` errored {} times this week.{}".format(
                    agent, len(errored),
                    (" Most recent: " + "; ".join(notes)) if notes else "",
                )
            )

    drifted = _recent_outcomes(
        runs, unattributed, now, CONFIG_DRIFT_OUTCOME, CONFIG_DRIFT_WINDOW)
    if drifted:
        newest = max(drifted, key=lambda r: r.get("ts") or 0)
        problems.append(
            "`{}` refused {} run(s) in the last day because its settings "
            "differ from codex_run.py (#1316).{}".format(
                agent, len(drifted),
                (" Most recent: " + str(newest["note"]))
                if newest.get("note") else "",
            )
        )
    return problems
