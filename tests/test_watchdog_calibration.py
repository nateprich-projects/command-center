"""Replay the real heartbeat history used to calibrate watchdog silence.

The heartbeat branch is append-only telemetry, not a test dependency.  The
fixture is a lossless snapshot of the record timestamps at the calibration
cutoff, normalized to the only fields the silence inference reads.  A gap that
would have crossed a candidate threshold before the next recorded heartbeat is
counted as a false alarm: the history has no outage label that would justify
the watchdog filing it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import pathlib
import sys
from typing import Dict, Iterator, List, Tuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "heartbeat_history.json"
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "watchdog_calibration_target", ROOT / ".github" / "scripts" / "watchdog.py"
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)

HOUR = 3600
ISSUE_33_LAST_RECORD = 1_788_630_670.0
ISSUE_33_QUIET_HOURS = 12.1

PARAMETERS = Tuple[int, int, int, int]
CHOSEN: PARAMETERS = (90, 5, HOUR, 8)
PROVISIONAL: PARAMETERS = (90, 4, HOUR, 3)
MULTIPLE_ONLY: PARAMETERS = (90, 5, HOUR, 5)


def _snapshot() -> Dict[str, List[Dict[str, float]]]:
    payload = json.loads(FIXTURE.read_text())
    agents = {}
    for agent, data in payload["agents"].items():
        timestamps = [data["first_ts"]]
        for gap in data["gaps"]:
            timestamps.append(timestamps[-1] + gap)
        agents[agent] = [
            {"agent": agent, "phase": "finish", "ts": timestamp}
            for timestamp in timestamps
        ]
    return agents


@contextlib.contextmanager
def _parameters(candidate: PARAMETERS) -> Iterator[None]:
    names = (
        "NORMAL_PERCENTILE",
        "NORMAL_MULTIPLE",
        "SILENCE_FLOOR_SECONDS",
        "MINIMUM_HISTORY",
    )
    previous = {name: getattr(watchdog, name) for name in names}
    for name, value in zip(names, candidate):
        setattr(watchdog, name, value)
    try:
        yield
    finally:
        for name, value in previous.items():
            setattr(watchdog, name, value)


def _silence_problems(agent: str, rows: List[Dict[str, float]], now: float,
                      candidate: PARAMETERS) -> List[str]:
    with _parameters(candidate):
        return [problem for problem in watchdog.assess(agent, rows, now)
                if "normal gap" in problem]


def _false_alarm_count(agent: str, rows: List[Dict[str, float]],
                       candidate: PARAMETERS) -> int:
    timestamps = [row["ts"] for row in rows]
    return sum(
        bool(_silence_problems(agent, rows, later - 1, candidate))
        for earlier, later in zip(timestamps, timestamps[1:])
        if later > earlier
    )


def _drop_trailing(rows: List[Dict[str, float]], hours: float):
    end = max(row["ts"] for row in rows)
    cutoff = end - hours * HOUR
    return [row for row in rows if row["ts"] <= cutoff], end


def test_fixture_is_a_lossless_snapshot_of_the_heartbeat_timestamps():
    agents = _snapshot()
    assert {agent: len(rows) for agent, rows in agents.items()} == {
        "claude": 34,
        "codex": 302,
        "muse": 6,
        "zcode": 166,
    }
    assert all(
        row["ts"] <= later["ts"]
        for rows in agents.values()
        for row, later in zip(rows, rows[1:])
    )


def test_replay_prints_candidate_false_alarm_counts():
    agents = _snapshot()
    candidates = (PROVISIONAL, MULTIPLE_ONLY, CHOSEN)
    results = {}
    for candidate in candidates:
        results[candidate] = {
            agent: _false_alarm_count(agent, rows, candidate)
            for agent, rows in agents.items()
        }
        print("candidate {}: {}".format(candidate, results[candidate]))

    assert results[PROVISIONAL] == {
        "claude": 2,
        "codex": 0,
        "muse": 0,
        "zcode": 0,
    }
    assert results[MULTIPLE_ONLY]["claude"] == 1
    assert results[CHOSEN] == {
        "claude": 0,
        "codex": 0,
        "muse": 0,
        "zcode": 0,
    }


def test_chosen_tuple_does_not_replay_the_issue_33_false_alarm():
    rows = _snapshot()["claude"]
    now = ISSUE_33_LAST_RECORD + ISSUE_33_QUIET_HOURS * HOUR
    assert _silence_problems("claude", rows, now, CHOSEN) == []


def test_chosen_tuple_still_reports_a_simulated_trailing_outage():
    agents = _snapshot()
    outage_hours = {"claude": 24, "codex": 6, "muse": 1, "zcode": 6}
    expected = {"claude": True, "codex": True, "muse": False, "zcode": True}

    for agent, rows in agents.items():
        retained, now = _drop_trailing(rows, outage_hours[agent])
        problems = _silence_problems(agent, retained, now, CHOSEN)
        print("{} outage {}h: {}".format(agent, outage_hours[agent], problems))
        assert bool(problems) is expected[agent]

    # Muse has only five observed gaps; it is deliberately still in the
    # thin-history/no-alarm state under the chosen eight-gap warm-up.
    retained, now = _drop_trailing(agents["muse"], outage_hours["muse"])
    assert watchdog._normal_gap(retained, now) is None
