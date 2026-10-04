"""An open Muse auth outage reports as its park in agent_health (#2176).

Plan #1750, the quota-parked shape (#1160) through a second park. Since #1946
the Muse lanes park on an open auth outage: an errored finish carrying
`heartbeat.MUSE_AUTH_OUTAGE_NOTE`, which only a successful `auth_probe` record
clears. A parked lane writes no heartbeat, and `agent_health.assess` knew only
the quota hold file, so the lane read as the generic silence alarm ("Nothing
recorded for ...") while the thing that would end it, a person signing Muse in
again, went unnamed.

The same ticket moves assess onto the per-run heartbeat view (#2174): open
starts, bindings, completed durations and per-run outcomes are read through
`heartbeat.run_views` rather than from the raw rows. The outcome cases below
pin what that move must keep: an outcome filed as an event (a `config-drift`
refusal, a re-attached misfiled finish) and one that names no run (an
unresolved finish) still count.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_health  # noqa: E402
import funnel  # noqa: E402
import heartbeat  # noqa: E402
from agent_health import assess  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "watchdog_for_auth_park", ROOT / ".github" / "scripts" / "watchdog.py"
)
watchdog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watchdog)

NOW = 1_788_600_000.0
MIN = 60
HOUR = 3600


def start(run, at, agent="muse"):
    return {"run": run, "agent": agent, "phase": "start", "ts": int(at)}


def finish(run, at, outcome="done", agent="muse", **extra):
    record = {"run": run, "agent": agent, "phase": "finish", "ts": int(at),
              "outcome": outcome}
    record.update(extra)
    return record


def event(run, at, outcome, agent="codex", **extra):
    record = {"run": run, "agent": agent, "phase": "event", "ts": int(at),
              "outcome": outcome}
    record.update(extra)
    return record


def outage(run, at, agent="muse"):
    """The finish `scripts/muse-review-engine` writes on a missing login."""
    return finish(run, at, "errored", agent=agent,
                  note=heartbeat.MUSE_AUTH_OUTAGE_NOTE,
                  error_class="unclassified")


def probe(at, result="success"):
    """What `heartbeat.py muse-auth-recovered` writes after a good probe."""
    return {"agent": "muse", "phase": "auth_probe", "ts": int(at),
            "result": result}


def cadence(last_at, count=12, gap=10 * MIN, agent="muse"):
    """A history dense enough for the p90 inference, oldest first."""
    rows = []
    for index in range(count):
        at = last_at - (count - 1 - index) * gap
        run = "r{}".format(index)
        rows += [start(run, at - MIN, agent), finish(run, at, agent=agent)]
    return rows


def outage_rows(quiet=3 * HOUR):
    """A ten-minute cadence ending in the auth-outage finish `quiet` ago."""
    opened_at = NOW - quiet
    rows = cadence(opened_at - 10 * MIN)
    rows += [start("auth", opened_at - MIN), outage("auth", opened_at)]
    return rows, opened_at


def stamp(at):
    return "<t:{}:f>".format(int(at))


def auth_parks(conditions):
    return [c for c in conditions if "auth outage" in c]


def silence(conditions):
    return [
        c for c in conditions
        if "Nothing recorded for" in c or "absolute silence floor" in c
    ]


def errored(conditions):
    return [c for c in conditions if "errored" in c and "times" in c]


# -- the reproduction --------------------------------------------------------


def test_an_open_auth_outage_names_its_park_not_the_silence_alarm():
    """The ticket's reproduction: three hours quiet behind an open outage."""
    rows, opened_at = outage_rows(quiet=3 * HOUR)

    conditions = assess("muse", rows, NOW)

    assert silence(conditions) == []
    parked = auth_parks(conditions)
    assert len(parked) == 1
    assert parked[0].startswith("`muse`: parked by an open Muse auth outage")
    # The time of the finish that opened it, and what clears it.
    assert stamp(opened_at) in parked[0]
    assert "successful login probe clears it" in parked[0]
    # One errored run is under every errored-run threshold.
    assert conditions == parked


def test_a_successful_probe_returns_the_normal_silence_reading():
    rows, opened_at = outage_rows(quiet=3 * HOUR)
    probed_at = opened_at + 10 * MIN

    conditions = assess("muse", rows + [probe(probed_at)], NOW)

    assert auth_parks(conditions) == []
    quiet = silence(conditions)
    assert len(quiet) == 1
    # Measured from the newest record, which is the probe itself.
    assert "Nothing recorded for 2h50m" in quiet[0]
    assert stamp(probed_at) in quiet[0]


# -- what the park reports ---------------------------------------------------


def test_the_park_is_raised_as_soon_as_the_outage_opens():
    """Not yet silent, still a raised condition: a person has to sign in."""
    rows, opened_at = outage_rows(quiet=5 * MIN)

    conditions = assess("muse", rows, NOW)

    assert silence(conditions) == []
    assert len(auth_parks(conditions)) == 1
    assert stamp(opened_at) in auth_parks(conditions)[0]


def test_the_park_also_replaces_the_absolute_silence_floor():
    """Sparse history has its own silence alarm; the park explains it too."""
    opened_at = NOW - 30 * HOUR
    rows = [start("auth", opened_at - MIN), outage("auth", opened_at)]

    conditions = assess("muse", rows, NOW)

    assert silence(conditions) == []
    assert len(auth_parks(conditions)) == 1
    assert stamp(opened_at) in auth_parks(conditions)[0]


def test_the_park_names_the_finish_that_opened_it():
    """Both tiers can hit the missing login before either parks."""
    rows, opened_at = outage_rows(quiet=3 * HOUR)
    second = opened_at + 4 * MIN
    rows += [start("auth-2", second - MIN), outage("auth-2", second)]

    parked = auth_parks(assess("muse", rows, NOW))

    assert len(parked) == 1
    assert stamp(opened_at) in parked[0]
    assert stamp(second) not in parked[0]


def test_an_outage_reopened_after_a_probe_names_the_reopening_finish():
    rows, opened_at = outage_rows(quiet=5 * HOUR)
    reopened_at = NOW - 3 * HOUR
    rows += [
        probe(opened_at + 10 * MIN),
        start("again", reopened_at - MIN),
        outage("again", reopened_at),
    ]

    parked = auth_parks(assess("muse", rows, NOW))

    assert len(parked) == 1
    assert stamp(reopened_at) in parked[0]
    assert stamp(opened_at) not in parked[0]


def test_a_quota_hold_keeps_its_own_wording_beside_the_auth_park():
    rows, _opened_at = outage_rows(quiet=3 * HOUR)

    conditions = assess("muse", rows, NOW, hold_until=NOW + HOUR)

    assert silence(conditions) == []
    assert len(auth_parks(conditions)) == 1
    quota = [c for c in conditions if "(provider quota)" in c]
    assert len(quota) == 1
    assert "parked until" in quota[0]


def test_the_park_does_not_hide_runs_that_errored():
    """It explains a gap in records, not three runs that failed (#1160)."""
    rows, _opened_at = outage_rows(quiet=3 * HOUR)
    for index in range(3):
        at = NOW - (index + 1) * MIN
        rows.append(finish("boom-{}".format(index), at, "errored",
                           note="boom {}".format(index)))

    conditions = assess("muse", rows, NOW)

    assert len(auth_parks(conditions)) == 1
    assert errored(conditions)


def test_the_outage_speaks_only_for_muse():
    """Another agent's errored finish with the same note parks nothing."""
    rows = cadence(NOW - 3 * HOUR - 10 * MIN, agent="codex")
    rows += [outage("auth", NOW - 3 * HOUR, agent="codex")]

    conditions = assess("codex", rows, NOW)

    assert auth_parks(conditions) == []
    assert [c for c in conditions if "Nothing recorded for" in c]


def test_a_failed_probe_leaves_the_park_open():
    rows, _opened_at = outage_rows(quiet=3 * HOUR)

    conditions = assess(
        "muse", rows + [probe(NOW - 2 * HOUR, result="failure")], NOW)

    assert len(auth_parks(conditions)) == 1
    assert silence(conditions) == []


def test_an_outage_read_twice_still_closes_on_its_probe(monkeypatch):
    """The dual-written shape (#1225): the outage finish is on GitHub and
    still spooled after a PUT whose reply was lost, and the probe that
    cleared it was pushed after. Read in append order the spooled copy would
    reopen the park; it is the same record, so it does not."""
    rows, opened_at = outage_rows(quiet=3 * HOUR)
    github = rows + [probe(opened_at + 10 * MIN)]
    content = "".join(json.dumps(r, sort_keys=True) + "\n" for r in github)
    monkeypatch.setattr(
        heartbeat, "_fetch", lambda _agent, *args, **kwargs: (content, "sha")
    )
    heartbeat._spool("muse", outage("auth", opened_at))
    read = heartbeat.read("muse")
    assert len(read) == len(github) + 1

    assert heartbeat.muse_auth_outage(read) is None
    assert not heartbeat.muse_auth_outage_open(read)
    assert auth_parks(assess("muse", read, NOW)) == []


def test_the_outage_reader_returns_the_opening_finish():
    first = outage("a", NOW - 3 * HOUR)
    second = outage("b", NOW - 2 * HOUR)
    other = finish("c", NOW - HOUR, "errored", note="connection reset")

    assert heartbeat.muse_auth_outage([]) is None
    assert heartbeat.muse_auth_outage([other]) is None
    assert heartbeat.muse_auth_outage([first, second, other]) == first
    assert heartbeat.muse_auth_outage(
        [first, probe(NOW - 150 * MIN)]) is None
    assert heartbeat.muse_auth_outage(
        [first, probe(NOW - 150 * MIN), second]) == second


# -- both callers, unchanged -------------------------------------------------


def test_the_watchdog_names_the_auth_park_from_the_records_alone():
    """The Actions watchdog has no hold file; the outage is in the records."""
    rows, opened_at = outage_rows(quiet=3 * HOUR)

    problems = watchdog.assess("muse", rows, NOW)

    assert silence(problems) == []
    assert len(auth_parks(problems)) == 1
    assert stamp(opened_at) in auth_parks(problems)[0]


def test_the_brief_names_the_auth_park(monkeypatch):
    rows, opened_at = outage_rows(quiet=3 * HOUR)
    monkeypatch.setattr(heartbeat, "PROVIDERS", {"muse": "meta"})
    monkeypatch.setattr(heartbeat, "read", lambda agent: rows)
    monkeypatch.setattr(agent_health, "quota_hold_until", lambda path=None: None)

    found = funnel.agent_health(datetime.fromtimestamp(NOW, timezone.utc))

    assert len(found) == 1
    assert found[0]["agent"] == "muse"
    assert found[0]["condition"].startswith(
        "`muse`: parked by an open Muse auth outage")
    assert stamp(opened_at) in found[0]["condition"]


# -- assess reads its outcomes through the per-run view ------------------------


def test_the_view_carries_each_runs_events():
    """A gate's refusal is an event on its run (`config-drift`, #1316), and
    the run's own finish says something else."""
    refusal = event("a", NOW - HOUR, "config-drift", note="model differs")
    rows = [start("a", NOW - HOUR - MIN, "codex"), refusal,
            finish("a", NOW - HOUR + MIN, "nothing-to-do", agent="codex")]

    view = heartbeat.run_views(rows)["a"]

    assert view["events"] == [refusal]
    assert view["outcome"] == "nothing-to-do"
    drift = [c for c in assess("codex", rows, NOW) if "codex_run.py" in c]
    assert len(drift) == 1
    assert "refused 1 run(s)" in drift[0]
    assert "model differs" in drift[0]


def test_errored_outcomes_filed_as_events_still_count():
    """A misfiled finish is re-attached to its open run as an event; the
    outcome must not vanish because the run has no finish of its own."""
    rows = []
    for index in range(3):
        run = "open-{}".format(index)
        at = NOW - (index + 1) * HOUR
        rows += [start(run, at - MIN, "codex"),
                 event(run, at, "errored", misfiled_from="other",
                       note="boom {}".format(index))]

    found = errored(assess("codex", rows, NOW))

    assert len(found) == 1
    assert "errored 3 times" in found[0]


def test_unattributed_errored_finishes_count_once_each():
    """An unresolved finish names no run. Each still counts, and one read
    twice (GitHub plus spool, #1225) is still one."""
    rows = []
    for index in range(3):
        run = "r{}".format(index)
        at = NOW - (index + 1) * HOUR
        rows += [start(run, at - MIN, "codex"),
                 finish(None, at, "errored", agent="codex", unresolved=True,
                        candidates=[run], note="boom {}".format(index))]
    rows.append(dict(rows[-1]))

    found = errored(assess("codex", rows, NOW))

    assert len(found) == 1
    assert "errored 3 times" in found[0]


def test_an_errored_finish_before_error_class_is_classified_on_read(
        monkeypatch):
    """The view classifies a finish written before the class was recorded,
    so the brief's regression filter sees it (#2174)."""
    rows = [
        finish("old-{}".format(index), NOW - (index + 1) * HOUR, "errored",
               note="tests failed: test_x {}".format(index),
               runtime={"head": "0123456789ab"})
        for index in range(3)
    ]
    assert all("error_class" not in row for row in rows)

    conditions = assess("muse", rows, NOW, regressions_only=True)

    assert [c for c in conditions if "3 consecutive regression errors" in c]


def test_completed_durations_count_runs_not_rows(monkeypatch):
    """The open-start threshold is ten times the median completed run.

    Two quick runs are read twice — on GitHub and still spooled (#1225) —
    and two twenty-minute runs once. Counted in rows, the quick runs are four
    of six durations and the median is 18 seconds, so a 40-minute start
    passes the 15-minute floor. Counted in runs it is two of four, the median
    is about ten minutes, and the start is inside its threshold.
    """
    def run(name, started, seconds):
        return [start(name, started, "codex"),
                finish(name, started + seconds, agent="codex")]

    once = run("slow-1", NOW - 5 * HOUR, 20 * MIN) + run(
        "slow-2", NOW - 4 * HOUR, 20 * MIN)
    twice = run("quick-1", NOW - 3 * HOUR, 18) + run(
        "quick-2", NOW - 2 * HOUR, 18)
    content = "".join(
        json.dumps(r, sort_keys=True) + "\n" for r in once + twice)
    monkeypatch.setattr(
        heartbeat, "_fetch", lambda _agent, *args, **kwargs: (content, "sha")
    )
    for record in twice:
        heartbeat._spool("codex", record)
    rows = heartbeat.read("codex") + [start("working", NOW - 40 * MIN, "codex")]

    conditions = assess("codex", rows, NOW)

    assert [c for c in conditions if "one open start" in c] == []
