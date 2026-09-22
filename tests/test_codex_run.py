"""A Codex run is checked against the manifest from its own rollout (#1316)."""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codex_run  # noqa: E402
import funnel  # noqa: E402
import heartbeat  # noqa: E402
import usage  # noqa: E402


NOW = datetime.datetime(2026, 9, 22, 15, 30)
HOME = codex_run.HOME
WORKSPACE = os.path.join(codex_run.SESSION_WORKSPACES, "2026-09-22",
                         "codex-routine-one-ticket-per-run-63")
OWN_AUTOMATION = os.path.join(codex_run.AUTOMATIONS,
                              "command-center-tickets-hourly")

#: The writable roots a 2026-09-18 automation run actually had, rebased onto
#: this machine's home: the heartbeat spool, the app's session workspaces,
#: the run's own automation directory, and one visualizations directory.
GOOD_ROOTS = [
    codex_run.HEARTBEAT_SPOOL,
    codex_run.SESSION_WORKSPACES,
    OWN_AUTOMATION,
    os.path.join(codex_run.VISUALIZATIONS, "2026", "09", "22", "01a0b588"),
]


def _turn(model="gpt-6-luna", effort="max", approval="never", network=True,
          roots=None, cwd=WORKSPACE):
    return {"type": "turn_context", "payload": {
        "model": model, "effort": effort, "cwd": cwd,
        "sandbox_policy": {"type": "workspace-write",
                           "writable_roots": GOOD_ROOTS if roots is None
                           else roots,
                           "network_access": network},
        "approval_policy": approval,
    }}


def _rollout(root, *, cwd=WORKSPACE, day=None, name="rollout-a.jsonl",
             turns=None, meta=True, torn=False, mtime=None):
    day = day or NOW.date()
    directory = pathlib.Path(root) / day.strftime("%Y") / day.strftime(
        "%m") / day.strftime("%d")
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    if meta:
        records.append({"type": "session_meta",
                        "payload": {"cwd": cwd,
                                    "originator": "Codex Desktop"}})
    records.extend([_turn()] if turns is None else turns)
    text = "".join(json.dumps(record) + "\n" for record in records)
    if torn:
        text += '{"type": "turn_con'
    path = directory / name
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _check(root, cwd=WORKSPACE, now=NOW):
    return codex_run.check(cwd, now=now, root=str(root))


# --- the manifest ---------------------------------------------------------


def test_the_manifest_is_gpt6_luna_at_max():
    """Spelled out rather than imported, so a transposed constant fails
    here instead of passing every comparison that reads it (#1315)."""
    assert codex_run.MODEL == "gpt-6-luna"
    assert codex_run.EFFORT == "max"
    assert codex_run.APPROVAL_POLICY == "never"
    assert codex_run.NETWORK_ACCESS is True


def test_the_sessions_root_can_be_pointed_elsewhere(monkeypatch, tmp_path):
    monkeypatch.setenv(codex_run.SESSIONS_ENV, str(tmp_path))
    assert codex_run.sessions_root() == str(tmp_path)
    monkeypatch.delenv(codex_run.SESSIONS_ENV)
    assert codex_run.sessions_root() == codex_run.DEFAULT_SESSIONS


# --- matching -------------------------------------------------------------


def test_a_run_matching_the_manifest_is_ok(tmp_path):
    rollout = _rollout(tmp_path)

    result = _check(tmp_path)

    assert result["ok"] is True, result
    assert result["drift"] == []
    assert result["why"] == ""
    assert result["rollout"] == str(rollout)
    assert result["effective"] == {"model": "gpt-6-luna", "effort": "max"}


def test_a_run_in_a_directory_inside_its_workspace_matches(tmp_path):
    """`begin` may run from the ticket checkout the model made inside the
    session workspace."""
    _rollout(tmp_path)

    assert _check(tmp_path, cwd=os.path.join(WORKSPACE, "checkout"))["ok"]


def test_a_sibling_workspace_sharing_a_prefix_does_not_match(tmp_path):
    """`...-63` must not claim `...-630`'s rollout: prefix is not
    containment."""
    _rollout(tmp_path)

    result = _check(tmp_path, cwd=WORKSPACE + "0")

    assert result["ok"] is False
    assert "no rollout" in result["why"]


def test_a_run_with_no_rollout_is_refused(tmp_path):
    _rollout(tmp_path, cwd=os.path.join(codex_run.SESSION_WORKSPACES, "other"))

    result = _check(tmp_path)

    assert result["ok"] is False
    assert result["rollout"] is None
    assert result["effective"] is None
    assert "no rollout" in result["why"]
    assert WORKSPACE in result["why"]


def test_an_empty_sessions_root_is_a_refusal_not_a_crash(tmp_path):
    result = _check(tmp_path / "missing")

    assert result["ok"] is False
    assert "no rollout" in result["why"]


def test_the_newest_matching_rollout_governs(tmp_path):
    """The same workspace path can be reused; the newest run is this one."""
    _rollout(tmp_path, name="rollout-old.jsonl",
             turns=[_turn(model="gpt-5.6-luna")], mtime=1_000_000)
    newest = _rollout(tmp_path, name="rollout-new.jsonl", mtime=2_000_000)

    result = _check(tmp_path)

    assert result["ok"] is True, result
    assert result["rollout"] == str(newest)


def test_a_rollout_from_before_midnight_is_found(tmp_path):
    yesterday = NOW.date() - datetime.timedelta(days=1)
    _rollout(tmp_path, day=yesterday)

    after_midnight = datetime.datetime.combine(
        NOW.date(), datetime.time(0, 5))
    assert _check(tmp_path, now=after_midnight)["ok"]


def test_an_older_day_is_not_searched(tmp_path):
    _rollout(tmp_path, day=NOW.date() - datetime.timedelta(days=2))

    assert _check(tmp_path)["ok"] is False


def test_the_newest_turn_context_governs(tmp_path):
    """`begin` runs a few turns in; a setting changed since the first turn
    is the one the work would run under."""
    _rollout(tmp_path, turns=[_turn(), _turn(model="gpt-5.6-sol")])
    assert _check(tmp_path)["ok"] is False

    _rollout(tmp_path, turns=[_turn(model="gpt-5.6-sol"), _turn()])
    assert _check(tmp_path)["ok"] is True


def test_a_torn_last_line_does_not_hide_the_lines_before_it(tmp_path):
    _rollout(tmp_path, torn=True)

    assert _check(tmp_path)["ok"] is True


def test_a_rollout_without_a_turn_context_is_refused(tmp_path):
    _rollout(tmp_path, turns=[])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "no turn_context" in result["why"]


def test_a_rollout_without_session_meta_is_not_this_run(tmp_path):
    _rollout(tmp_path, meta=False)

    assert _check(tmp_path)["ok"] is False


# --- drift ----------------------------------------------------------------


@pytest.mark.parametrize("turn,fragment", [
    (_turn(model="gpt-5.6-luna"), "model: expected gpt-6-luna, found gpt-5.6-luna"),
    (_turn(model="gpt-6-sol"), "model: expected gpt-6-luna, found gpt-6-sol"),
    (_turn(effort="high"), "effort: expected max, found high"),
    (_turn(approval="on-request"),
     "approval policy: expected never, found on-request"),
    (_turn(network=False), "network access: expected True, found False"),
])
def test_each_setting_that_differs_is_named(tmp_path, turn, fragment):
    _rollout(tmp_path, turns=[turn])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert fragment in result["drift"]
    assert fragment in result["why"]


@pytest.mark.parametrize("extra", [
    os.path.join(HOME, ".claude", "command-center-run"),
    os.path.join(HOME, ".claude", "command-center"),
    os.path.join(HOME, ".claude", "command-center", "funnel.py"),
    os.path.join(HOME, ".claude"),
    HOME,
    "/",
    os.path.join(HOME, "Documents"),
    os.path.join(HOME, ".codex"),
    codex_run.AUTOMATIONS,
    os.path.join(codex_run.AUTOMATIONS, "someone-elses-automation"),
])
def test_a_writable_root_outside_the_manifest_is_drift(tmp_path, extra):
    """The routine grants `command-center-run` read-and-execute only; a
    root at or above it makes the deployed checkout writable."""
    _rollout(tmp_path, turns=[_turn(roots=GOOD_ROOTS + [extra])])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "writable root outside the manifest: {}".format(extra) in \
        result["drift"]


def test_a_second_automation_directory_is_drift(tmp_path):
    """A run that may write another automation's directory can rewrite
    that automation's schedule, model or prompt."""
    other = os.path.join(codex_run.AUTOMATIONS,
                         "command-center-tickets-weekday-mornings")
    _rollout(tmp_path, turns=[_turn(roots=GOOD_ROOTS + [other])])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert any("2 automation directories" in line for line in result["drift"])


def test_every_difference_is_reported_not_just_the_first(tmp_path):
    _rollout(tmp_path, turns=[_turn(model="gpt-5.6-luna", effort="high",
                                    roots=GOOD_ROOTS + [HOME])])

    assert len(_check(tmp_path)["drift"]) == 3


@pytest.mark.parametrize("sandbox", [None, "workspace-write", []])
def test_a_missing_sandbox_record_is_drift(tmp_path, sandbox):
    turn = _turn()
    turn["payload"]["sandbox_policy"] = sandbox
    _rollout(tmp_path, turns=[turn])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert any(line.startswith("sandbox policy") for line in result["drift"])


@pytest.mark.parametrize("roots", [None, "~/Documents/Codex", [1, 2]])
def test_unreadable_writable_roots_are_drift(tmp_path, roots):
    turn = _turn()
    turn["payload"]["sandbox_policy"]["writable_roots"] = roots
    _rollout(tmp_path, turns=[turn])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert any(line.startswith("writable roots") for line in result["drift"])


def test_a_run_with_no_automation_directory_is_not_drift(tmp_path):
    """A session Nate starts by hand has no automation root; the check is
    about what the run may reach, and fewer roots is not wider access."""
    roots = [root for root in GOOD_ROOTS if root != OWN_AUTOMATION]
    _rollout(tmp_path, turns=[_turn(roots=roots)])

    assert _check(tmp_path)["ok"] is True


# --- begin ----------------------------------------------------------------


def _begin_codex(monkeypatch, capsys, *, argv_tail=("--tier", "standard")):
    events = []
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent: "run-id")
    monkeypatch.setattr(
        heartbeat, "record_event",
        lambda agent, run, outcome, **fields: events.append(
            (agent, run, outcome, fields)))
    monkeypatch.setattr(
        funnel, "load_items",
        lambda *args, **kwargs: pytest.fail(
            "a refused begin must not load the Project"))
    code = funnel.main(["begin", "--agent", "codex", *argv_tail])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), events, captured.err


def test_begin_refuses_a_drifted_codex_run_before_the_budget(
        monkeypatch, capsys):
    """Before the usage read: a run on the wrong model must not reach
    anything, including the budget it would spend."""
    monkeypatch.setattr(funnel, "_codex_settings_check", lambda: {
        "ok": False, "why": "model: expected gpt-6-luna, found gpt-5.6-luna"})
    monkeypatch.setattr(usage, "read_agent", lambda *args: pytest.fail(
        "a refused run must not read usage"))

    code, result, events, err = _begin_codex(monkeypatch, capsys)

    assert code == 0
    assert result["do"] == "stop"
    assert result["gate"] == "config"
    assert result["run"] == "run-id"
    assert "gpt-5.6-luna" in result["why"]
    assert "effective" not in result
    assert events == [("codex", "run-id", "skipped-config-drift",
                       {"note": result["why"]})]
    assert "funnel: skipped-config-drift:" in err


def test_begin_carries_the_effective_settings_of_a_matching_run(
        monkeypatch, capsys):
    monkeypatch.setattr(usage, "read_agent", lambda *args: {"windows": {}})
    monkeypatch.setattr(usage, "pace", lambda *args, **kwargs: {
        "over_pace": True})

    code, result, events, _ = _begin_codex(monkeypatch, capsys)

    assert code == 0
    assert result["gate"] == "over"
    assert result["effective"] == {"model": "gpt-6-luna", "effort": "max"}
    assert events == []


def test_begin_reads_the_real_rollout_end_to_end(
        monkeypatch, capsys, tmp_path, codex_settings_match):
    """The shared fixture stands in for this machine's rollouts; this test
    puts the real seam back and points it at fixture rollouts."""
    monkeypatch.setattr(funnel, "_codex_settings_check", codex_settings_match)
    sessions = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv(codex_run.SESSIONS_ENV, str(sessions))
    _rollout(sessions, cwd=str(workspace), day=datetime.date.today(),
             turns=[_turn(effort="high", cwd=str(workspace))])
    monkeypatch.setattr(usage, "read_agent", lambda *args: pytest.fail(
        "a refused run must not read usage"))

    code, result, events, _ = _begin_codex(monkeypatch, capsys)

    assert code == 0
    assert result["gate"] == "config"
    assert "effort: expected max, found high" in result["why"]
    assert str(workspace) in result["why"]
    assert [event[2] for event in events] == ["skipped-config-drift"]


def test_a_check_that_raises_is_a_refusal(monkeypatch, codex_settings_match):
    monkeypatch.setattr(funnel, "_codex_settings_check", codex_settings_match)

    def boom(*args, **kwargs):
        raise RuntimeError("rollout reader exploded")

    monkeypatch.setattr(codex_run, "check", boom)

    result = funnel._codex_settings_check()

    assert result["ok"] is False
    assert "rollout reader exploded" in result["why"]


def test_other_agents_never_read_codex_rollouts(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "_codex_settings_check", lambda: pytest.fail(
        "only a Codex run is checked against the Codex manifest"))
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent: "run-id")
    monkeypatch.setattr(usage, "read_agent", lambda *args: {"windows": {}})
    monkeypatch.setattr(usage, "pace", lambda *args, **kwargs: {
        "over_pace": True})
    monkeypatch.setattr(funnel, "load_items", lambda *args, **kwargs: [])

    assert funnel.main(["begin", "--agent", "muse", "--tier",
                        "escalated"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["gate"] == "over"


def test_the_routine_maps_the_config_gate_to_its_outcome():
    """The routine owns the terminal finish; a gate it cannot name would
    be filed as nothing-to-do."""
    routine = (ROOT / "routines" / "codex-work.md").read_text()
    assert "`config` is `skipped-config-drift`" in routine
    assert "skipped-config-drift" in heartbeat.OUTCOMES
