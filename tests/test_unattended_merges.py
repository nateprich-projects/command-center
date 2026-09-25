"""``unattended_merges`` reads every live reviewer's heartbeat, not Claude's alone (#489).

Measured 2026-09-10: the Muse spool held 29 finish records with ``merged`` set
inside the window while the brief reported one, because the reader was
hard-wired to the retired Claude routine's spool.
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
import heartbeat  # noqa: E402

NOW = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
ALREADY_MERGED_FINISH = (
    pathlib.Path(__file__).parent / "fixtures" /
    "already_merged_done_finish.json"
)


def _finish(agent, merged=None, days_ago=1, note=None):
    ts = (NOW - timedelta(days=days_ago)).timestamp()
    row = {"agent": agent, "phase": "finish", "run": "r-%s-%s" % (agent, merged),
           "ts": ts, "outcome": "done", "note": note}
    if merged is not None:
        row["merged"] = merged
    return row


def _authored(agent, pr, days_ago=1, run=None):
    return {
        "agent": agent,
        "phase": "finish",
        "run": run or "author-%s-%s" % (agent, pr),
        "ts": (NOW - timedelta(days=days_ago)).timestamp(),
        "outcome": "done",
        "note": "PR #%s" % pr,
    }


def _wire(monkeypatch, spools, retired=frozenset({"zcode"})):
    monkeypatch.setattr(heartbeat, "PROVIDERS",
                        {"claude": "anthropic", "codex": "openai",
                         "muse": "meta", "zcode": "zai"})
    monkeypatch.setattr(heartbeat, "RETIRED_AGENTS", frozenset(retired))
    read = []

    def fake_read(agent):
        read.append(agent)
        return spools.get(agent, [])

    monkeypatch.setattr(heartbeat, "read", fake_read)
    return read


def test_every_live_agent_is_read_and_each_record_names_its_agent(monkeypatch):
    spools = {
        "claude": [_finish("claude", merged=101, days_ago=1, note="merged PR #101")],
        "muse": [_finish("muse", merged=202, days_ago=2, note="merged PR #202"),
                 _finish("muse", merged=None)],
        "codex": [_finish("codex", merged=None)],
    }
    read = _wire(monkeypatch, spools)

    found = funnel.unattended_merges(NOW)

    assert [(m["pr"], m["agent"]) for m in found] == [(202, "muse"), (101, "claude")]
    assert all(set(m) == {"pr", "at", "note", "agent"} for m in found)
    assert set(read) == {"claude", "codex", "muse"}


def test_already_merged_finish_note_is_rendered_from_the_done_record(monkeypatch):
    finish = json.loads(ALREADY_MERGED_FINISH.read_text())
    _wire(monkeypatch, {finish["agent"]: [finish]}, retired=frozenset())
    now = datetime.fromtimestamp(finish["ts"] + 60, timezone.utc)

    found = funnel.unattended_merges(now)

    assert len(found) == 1
    assert found[0]["pr"] == finish["merged"]
    assert found[0]["agent"] == finish["agent"]
    assert found[0]["note"] == finish["note"]
    assert "observed already-merged PR" in found[0]["note"]


def test_a_retired_agent_is_never_read(monkeypatch):
    spools = {"zcode": [_finish("zcode", merged=303)],
              "muse": [_finish("muse", merged=202)]}
    read = _wire(monkeypatch, spools)

    found = funnel.unattended_merges(NOW)

    assert [m["pr"] for m in found] == [202]
    assert "zcode" not in read


def test_rows_outside_the_window_fall_out(monkeypatch):
    days = funnel.MAINTENANCE_WINDOW.days
    spools = {"muse": [_finish("muse", merged=1, days_ago=days + 1),
                       _finish("muse", merged=2, days_ago=days - 1)]}
    _wire(monkeypatch, spools)

    assert [m["pr"] for m in funnel.unattended_merges(NOW)] == [2]


def test_one_unreadable_spool_does_not_hide_the_others(monkeypatch):
    spools = {"muse": [_finish("muse", merged=202)]}
    _wire(monkeypatch, spools)
    real = heartbeat.read

    def flaky(agent):
        if agent == "codex":
            raise OSError("spool unreadable")
        return real(agent)

    monkeypatch.setattr(heartbeat, "read", flaky)

    assert [m["pr"] for m in funnel.unattended_merges(NOW)] == [202]


def test_marks_only_a_same_agent_merge_as_self_reviewed(monkeypatch):
    spools = {
        "muse": [
            _authored("muse", 202),
            _finish("muse", merged=202, note="merged PR #202"),
        ],
        "codex": [_authored("codex", 303)],
        "claude": [_finish("claude", merged=303, note="merged PR #303")],
    }
    _wire(monkeypatch, spools)

    found = {
        merge["pr"]: merge for merge in funnel.unattended_merges(NOW)
    }

    assert found[202]["self_reviewed"] is True
    assert "self_reviewed" not in found[303]


def test_the_skill_names_the_agent_field():
    text = (ROOT / "skills" / "funnel" / "SKILL.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `unattended_merges`"))
    assert "`agent`" in row and "retired" in row
    assert "`self_reviewed: true`" in row
