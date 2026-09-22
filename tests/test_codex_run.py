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
from agent_health import assess  # noqa: E402


NOW = datetime.datetime(2026, 9, 22, 15, 30)
HOME = codex_run.HOME
THREAD = "01a0b588-c5dd-7a90-ab48-168662b0014f"
OTHER_THREAD = "01a0b13e-fdf2-70c0-90d5-dc46ec9c4602"
ENV = {"CODEX_THREAD_ID": THREAD, "CODEX_SESSION_ID": THREAD}
WORKSPACE = os.path.join(codex_run.SESSION_WORKSPACES, "2026-09-22",
                         "codex-routine-one-ticket-per-run-63")
OWN_AUTOMATION = os.path.join(codex_run.AUTOMATIONS,
                              "command-center-tickets-hourly")
VISUALS = os.path.join(codex_run.VISUALIZATIONS, "2026", "09", "22", THREAD)

#: The writable roots a 2026-09-18 automation run actually had, rebased onto
#: this machine's home: the heartbeat spool, the app's session workspaces,
#: the run's own automation directory, and one visualizations directory.
GOOD_ROOTS = [codex_run.HEARTBEAT_SPOOL, codex_run.SESSION_WORKSPACES,
              OWN_AUTOMATION, VISUALS]


def _write(path):
    return {"path": {"type": "path", "path": path}, "access": "write"}


def _special(kind, access="write"):
    return {"path": {"type": "special", "value": {"kind": kind}},
            "access": access}


def _entries(cwd=WORKSPACE, extra=()):
    """The shape of a real `file_system_sandbox_policy`: root readable, the
    working directory, the listed roots and the temporary directories
    writable, and a few read-only carve-outs."""
    return {"kind": "restricted", "entries": [
        _special("root", "read"),
        _write(cwd),
        _write(codex_run.HEARTBEAT_SPOOL),
        _special("slash_tmp"),
        _special("tmpdir"),
        _write(codex_run.SESSION_WORKSPACES),
        _write(OWN_AUTOMATION),
        _write(VISUALS),
        {"path": {"type": "path", "path": os.path.join(cwd, ".git")},
         "access": "read"},
        *extra,
    ]}


def _turn(model="gpt-6-luna", effort="max", approval="never", network=True,
          roots=None, cwd=WORKSPACE, sandbox_type="workspace-write",
          entries="default", profile="default"):
    payload = {
        "model": model, "effort": effort, "cwd": cwd,
        "sandbox_policy": {"type": sandbox_type,
                           "writable_roots": GOOD_ROOTS if roots is None
                           else roots,
                           "network_access": network},
        "approval_policy": approval,
    }
    if entries == "default":
        payload["file_system_sandbox_policy"] = _entries(cwd)
    elif entries is not None:
        payload["file_system_sandbox_policy"] = entries
    if profile == "default":
        policy = payload.get("file_system_sandbox_policy") or _entries(cwd)
        payload["permission_profile"] = {
            "type": "managed",
            "file_system": {"type": "restricted",
                            "entries": list(policy.get("entries", []))
                            if isinstance(policy, dict) else []},
            "network": {"type": "managed", "network": "enabled"},
        }
    elif profile is not None:
        payload["permission_profile"] = profile
    return {"type": "turn_context", "payload": payload}


def _rollout(root, *, thread=THREAD, meta_id=None, cwd=WORKSPACE, day=None,
             turns=None, meta=True, torn=False):
    day = day or NOW.date()
    directory = pathlib.Path(root) / day.strftime("%Y") / day.strftime(
        "%m") / day.strftime("%d")
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    if meta:
        records.append({"type": "session_meta", "payload": {
            "id": meta_id or thread, "session_id": thread, "cwd": cwd,
            "originator": "Codex Desktop"}})
    records.extend([_turn()] if turns is None else turns)
    text = "".join(json.dumps(record) + "\n" for record in records)
    if torn:
        text += '{"type": "turn_con'
    path = directory / "rollout-{}T15-30-00-{}.jsonl".format(
        day.isoformat(), thread)
    path.write_text(text)
    return path


def _check(root, cwd=WORKSPACE, now=NOW, environ=None):
    return codex_run.check(cwd, now=now, root=str(root),
                           environ=ENV if environ is None else environ)


# --- the manifest and the thread id ------------------------------------------


def test_the_manifest_is_gpt6_luna_at_max():
    """Spelled out rather than imported, so a transposed constant fails
    here instead of passing every comparison that reads it (#1315)."""
    assert codex_run.MODEL == "gpt-6-luna"
    assert codex_run.EFFORT == "max"
    assert codex_run.APPROVAL_POLICY == "never"
    assert codex_run.SANDBOX_TYPE == "workspace-write"
    assert codex_run.NETWORK_ACCESS is True


def test_the_sessions_root_cannot_be_pointed_elsewhere(monkeypatch, tmp_path):
    """A run that could redirect the check could hand it a rollout of its
    own making from a directory it can write."""
    monkeypatch.setenv("COMMAND_CENTER_CODEX_SESSIONS", str(tmp_path))
    assert codex_run.sessions_root() == codex_run.DEFAULT_SESSIONS


def test_the_thread_id_comes_from_the_environment():
    assert codex_run.thread_id({"CODEX_THREAD_ID": THREAD}) == THREAD
    assert codex_run.thread_id({"CODEX_THREAD_ID": THREAD,
                                "CODEX_SESSION_ID": OTHER_THREAD}) == THREAD


def test_the_session_id_is_not_a_fallback():
    """A subagent's session id is its parent's: falling back to it would
    check the parent's settings on the subagent's behalf."""
    assert codex_run.thread_id({"CODEX_SESSION_ID": THREAD}) is None


@pytest.mark.parametrize("value", [
    "", "   ", "*", "01a0b588-*", THREAD + "/../x", "not-a-thread",
    THREAD.upper(),
])
def test_a_malformed_thread_id_is_not_searched_for(value):
    """The id goes into a file-name pattern; a glob character must not."""
    assert codex_run.thread_id({"CODEX_THREAD_ID": value}) is None


# --- finding this run's rollout ---------------------------------------------


def test_a_run_matching_the_manifest_is_ok(tmp_path):
    rollout = _rollout(tmp_path)

    result = _check(tmp_path)

    assert result["ok"] is True, result
    assert result["drift"] == []
    assert result["why"] == ""
    assert result["rollout"] == str(rollout)
    assert result["effective"] == {"model": "gpt-6-luna", "effort": "max"}


def test_a_run_with_no_thread_id_is_refused(tmp_path):
    _rollout(tmp_path)

    result = _check(tmp_path, environ={})

    assert result["ok"] is False
    assert "no Codex thread id" in result["why"]


def test_a_thread_with_no_rollout_is_refused(tmp_path):
    _rollout(tmp_path, thread=OTHER_THREAD)

    result = _check(tmp_path)

    assert result["ok"] is False
    assert result["rollout"] is None
    assert "no rollout for thread" in result["why"]


def test_another_runs_newer_rollout_is_never_read(tmp_path):
    """The directory match this replaced took the newest rollout from any
    session containing the workspace; a newer one with matching settings
    passed a drifted run."""
    _rollout(tmp_path, turns=[_turn(model="gpt-5.6-sol")])
    _rollout(tmp_path, thread=OTHER_THREAD)

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "model: expected gpt-6-luna, found gpt-5.6-sol" in result["drift"]


def test_a_rollout_recording_another_id_is_refused(tmp_path):
    _rollout(tmp_path, meta_id=OTHER_THREAD)

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "records id {}".format(OTHER_THREAD) in result["why"]


def test_begin_in_a_directory_inside_the_workspace_is_ok(tmp_path):
    _rollout(tmp_path)

    assert _check(tmp_path, cwd=os.path.join(WORKSPACE, "checkout"))["ok"]


@pytest.mark.parametrize("cwd", [
    WORKSPACE + "0",
    codex_run.SESSION_WORKSPACES,
    os.path.join(HOME, ".claude", "command-center-run"),
])
def test_begin_outside_its_sessions_directory_is_refused(tmp_path, cwd):
    _rollout(tmp_path)

    result = _check(tmp_path, cwd=cwd)

    assert result["ok"] is False
    assert "outside its session's directory" in result["why"]


def test_a_rollout_from_before_midnight_is_found(tmp_path):
    _rollout(tmp_path, day=NOW.date() - datetime.timedelta(days=1))

    after_midnight = datetime.datetime.combine(NOW.date(),
                                               datetime.time(0, 5))
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


def test_a_rollout_without_session_meta_is_refused(tmp_path):
    _rollout(tmp_path, meta=False)

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "no session_meta" in result["why"]


# --- the working directory is writable, so it must be a workspace ------------


@pytest.mark.parametrize("where", [
    os.path.join(HOME, ".claude", "command-center-run"),
    os.path.join(HOME, ".claude", "command-center"),
    HOME,
    os.path.join(HOME, ".codex"),
    codex_run.SESSION_WORKSPACES,
])
def test_a_session_started_outside_the_workspaces_is_refused(tmp_path, where):
    """The sandbox grants write to the working directory though
    `writable_roots` never lists it: a session started in the deployed
    checkout could write the deployed checkout."""
    _rollout(tmp_path, cwd=where, turns=[_turn(cwd=where)])

    result = _check(tmp_path, cwd=where)

    assert result["ok"] is False
    assert any("expected a workspace under" in line
               for line in result["drift"]), result["drift"]


def test_a_turn_moved_outside_the_workspaces_is_refused(tmp_path):
    elsewhere = os.path.join(HOME, ".claude", "command-center-run")
    _rollout(tmp_path, turns=[_turn(cwd=elsewhere)])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert ("working directory: expected a workspace under {}, found {}"
            .format(codex_run.SESSION_WORKSPACES, elsewhere)
            in result["drift"])


# --- drift in the settings --------------------------------------------------


@pytest.mark.parametrize("turn,fragment", [
    (_turn(model="gpt-5.6-luna"),
     "model: expected gpt-6-luna, found gpt-5.6-luna"),
    (_turn(model="gpt-6-sol"), "model: expected gpt-6-luna, found gpt-6-sol"),
    (_turn(effort="high"), "effort: expected max, found high"),
    (_turn(approval="on-request"),
     "approval policy: expected never, found on-request"),
    (_turn(network=False), "network access: expected True, found False"),
    (_turn(sandbox_type="danger-full-access"),
     "sandbox type: expected workspace-write, found danger-full-access"),
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


@pytest.mark.parametrize("extra", [
    os.path.join(codex_run.SESSION_WORKSPACES, "..", "..", ".claude"),
    os.path.join(codex_run.AUTOMATIONS, "command-center-x", "..",
                 "command-center-tickets-weekday-mornings"),
    "Documents/Codex",
])
def test_a_root_that_is_not_a_plain_absolute_path_is_drift(tmp_path, extra):
    _rollout(tmp_path, turns=[_turn(roots=GOOD_ROOTS + [extra])])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "writable root is not a plain absolute path: {}".format(extra) in \
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


@pytest.mark.parametrize("entry,fragment", [
    (_write(os.path.join(HOME, ".claude", "command-center-run")),
     "write access outside the manifest"),
    (_write(HOME), "write access outside the manifest"),
    (_special("root"), "write access to special path root"),
    (_write("relative/path"), "write entry is not a plain absolute path"),
    (dict(_write(HOME), access="read_write"),
     "write access outside the manifest"),
    ({"path": {"type": "path", "path": HOME}},
     "write access outside the manifest"),
])
def test_a_write_entry_outside_the_manifest_is_drift(tmp_path, entry,
                                                     fragment):
    """The entry lists are the complete write set; the root list omits the
    working directory and the temporary directories. Any access other than
    `read`, a missing one included, counts as a write. Both lists are
    checked."""
    _rollout(tmp_path, turns=[_turn(entries=_entries(extra=[entry]))])

    result = _check(tmp_path)

    assert result["ok"] is False
    for label in ("file system policy", "permission profile"):
        assert any(line.startswith(label) and fragment in line
                   for line in result["drift"]), (label, result["drift"])


def test_the_temporary_directories_are_expected_writes(tmp_path):
    _rollout(tmp_path)

    assert _check(tmp_path)["ok"] is True


def test_a_turn_without_an_entry_list_relies_on_roots_and_workspace(tmp_path):
    _rollout(tmp_path, turns=[_turn(entries=None, profile=None)])

    assert _check(tmp_path)["ok"] is True


@pytest.mark.parametrize("entries", [
    "restricted", {"kind": "restricted"},
    {"kind": "unrestricted", "entries": []},
    {"entries": []},
])
def test_a_malformed_or_unrestricted_entry_list_is_drift(tmp_path, entries):
    _rollout(tmp_path, turns=[_turn(entries=entries, profile=None)])

    assert _check(tmp_path)["ok"] is False


def test_a_second_automation_directory_only_in_the_entries_is_drift(
        tmp_path):
    other = os.path.join(codex_run.AUTOMATIONS,
                         "command-center-tickets-weekday-mornings")
    _rollout(tmp_path, turns=[_turn(entries=_entries(extra=[_write(other)]))])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert any("2 automation directories" in line for line in result["drift"])


@pytest.mark.parametrize("profile,fragment", [
    ({"type": "disabled"}, "expected a managed profile"),
    ("managed", "expected a managed profile"),
    ({"type": "managed", "file_system": {"type": "unrestricted",
                                         "entries": []}},
     "expected a restricted entry list"),
    ({"type": "managed", "file_system": {
        "type": "restricted",
        "entries": [_write(os.path.join(HOME, ".claude",
                                        "command-center-run"))]}},
     "write access outside the manifest"),
    ({"type": "managed",
      "file_system": {"type": "restricted", "entries": []},
      "network": {"type": "managed", "network": "restricted"}},
     "network expected enabled"),
])
def test_the_permission_profile_is_checked_like_the_entry_list(
        tmp_path, profile, fragment):
    """It carries the same grants again. It matched the entry list in
    every real record, and it is checked rather than trusted to."""
    _rollout(tmp_path, turns=[_turn(profile=profile)])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert any(line.startswith("permission profile") and fragment in line
               for line in result["drift"]), result["drift"]


def test_a_workspace_that_links_elsewhere_is_refused(tmp_path, monkeypatch):
    """The working directory is writable whatever the root list says; a
    symlink from a workspace into the deployed checkout must not pass."""
    workspaces = tmp_path / "Codex"
    elsewhere = tmp_path / "command-center-run"
    (workspaces / "2026-09-22").mkdir(parents=True)
    elsewhere.mkdir()
    link = workspaces / "2026-09-22" / "run-1"
    link.symlink_to(elsewhere)
    monkeypatch.setattr(codex_run, "SESSION_WORKSPACES", str(workspaces))
    monkeypatch.setattr(codex_run, "WRITABLE_PREFIXES", (
        codex_run.HEARTBEAT_SPOOL, str(workspaces), codex_run.VISUALIZATIONS))
    sessions = tmp_path / "sessions"
    _rollout(sessions, cwd=str(link), turns=[_turn(
        cwd=str(link), roots=[codex_run.HEARTBEAT_SPOOL, str(workspaces)],
        entries=None, profile=None)])

    result = _check(sessions, cwd=str(link))

    assert result["ok"] is False
    assert any("expected a workspace under" in line
               for line in result["drift"])

    real = workspaces / "2026-09-22" / "run-2"
    real.mkdir()
    _rollout(sessions, cwd=str(real), turns=[_turn(
        cwd=str(real), roots=[codex_run.HEARTBEAT_SPOOL, str(workspaces)],
        entries=None, profile=None)])

    assert _check(sessions, cwd=str(real))["ok"] is True


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


@pytest.mark.parametrize("roots", [None, "~/Documents/Codex", [1, 2], [""]])
def test_unreadable_writable_roots_are_drift(tmp_path, roots):
    turn = _turn()
    turn["payload"]["sandbox_policy"]["writable_roots"] = roots
    _rollout(tmp_path, turns=[turn])

    result = _check(tmp_path)

    assert result["ok"] is False
    assert any(line.startswith("writable roots") for line in result["drift"])


def test_a_run_with_no_automation_directory_is_not_drift(tmp_path):
    """A narrower grant is not wider access."""
    roots = [root for root in GOOD_ROOTS if root != OWN_AUTOMATION]
    _rollout(tmp_path, turns=[_turn(roots=roots)])

    assert _check(tmp_path)["ok"] is True


# --- begin ----------------------------------------------------------------


def _begin_codex(monkeypatch, capsys):
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
    code = funnel.main(["begin", "--agent", "codex", "--tier", "standard"])
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
    assert events == [("codex", "run-id", "config-drift",
                       {"note": result["why"]})]
    assert "funnel: config-drift:" in err


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
    workspace = os.path.join(codex_run.SESSION_WORKSPACES, "2026-09-22",
                             "fixture-run")
    monkeypatch.setattr(codex_run, "DEFAULT_SESSIONS", str(sessions))
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD)
    monkeypatch.setattr(os, "getcwd", lambda: workspace)
    _rollout(sessions, cwd=workspace, day=datetime.date.today(),
             turns=[_turn(effort="high", cwd=workspace)])
    monkeypatch.setattr(usage, "read_agent", lambda *args: pytest.fail(
        "a refused run must not read usage"))

    code, result, events, _ = _begin_codex(monkeypatch, capsys)

    assert code == 0
    assert result["gate"] == "config"
    assert "effort: expected max, found high" in result["why"]
    assert workspace in result["why"]
    assert [event[2] for event in events] == ["config-drift"]


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
    assert "`config` is `config-drift`" in routine
    assert "config-drift" in heartbeat.OUTCOMES


def test_the_outcome_is_not_one_the_watchdog_treats_as_healthy():
    """Every `skipped-*` outcome is healthy by the watchdog's design."""
    assert not "config-drift".startswith("skipped-")


# --- the alarm ----------------------------------------------------------------


ALARM_NOW = 1_790_100_000.0


def _drift_row(run, hours_ago, phase="event"):
    return {"run": run, "agent": "codex", "phase": phase,
            "ts": ALARM_NOW - hours_ago * 3600, "outcome": "config-drift",
            "note": "model: expected gpt-6-luna, found gpt-5.6-luna"}


def test_one_refusal_in_the_last_day_is_reported():
    problems = assess("codex", [_drift_row("r1", 2)], ALARM_NOW)

    drift = [p for p in problems if "codex_run.py" in p]
    assert len(drift) == 1
    assert "refused 1 run(s)" in drift[0]
    assert "gpt-5.6-luna" in drift[0]


def test_an_event_and_a_finish_for_one_run_count_once():
    rows = [_drift_row("r1", 2), _drift_row("r1", 2, phase="finish"),
            _drift_row("r2", 1)]

    drift = [p for p in assess("codex", rows, ALARM_NOW)
             if "codex_run.py" in p]
    assert "refused 2 run(s)" in drift[0]


def test_a_refusal_older_than_a_day_has_cleared():
    assert [p for p in assess("codex", [_drift_row("r1", 25)], ALARM_NOW)
            if "codex_run.py" in p] == []
