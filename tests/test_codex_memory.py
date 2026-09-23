"""Each Codex run starts from a reset automation memory file (#1317)."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codex_run  # noqa: E402
import funnel  # noqa: E402
import usage  # noqa: E402


#: Captured at import, before the shared fixture stubs the seam for every
#: test; the tests of begin's reset put the real one back on purpose.
REAL_MEMORY_RESET = funnel._codex_memory_reset

OWN = os.path.join(codex_run.AUTOMATIONS, "command-center-tickets-hourly")
OTHER = os.path.join(codex_run.AUTOMATIONS,
                     "command-center-tickets-weekday-mornings")


def _settings(roots):
    return {"sandbox_policy": {"type": "workspace-write",
                               "writable_roots": roots,
                               "network_access": True}}


# --- which directory is the run's own -----------------------------------------


def test_the_run_s_own_automation_directory_comes_from_its_roots():
    roots = [codex_run.HEARTBEAT_SPOOL, codex_run.SESSION_WORKSPACES, OWN]

    assert codex_run.own_automation(_settings(roots)) == OWN


@pytest.mark.parametrize("roots", [
    [codex_run.HEARTBEAT_SPOOL, codex_run.SESSION_WORKSPACES],
    [OWN, OTHER],
    [os.path.join(codex_run.AUTOMATIONS, "someone-elses")],
    [os.path.join(codex_run.AUTOMATIONS, "command-center-x", "..",
                  "command-center-tickets-hourly")],
    "not-a-list",
])
def test_no_single_automation_directory_means_no_memory_is_touched(roots):
    """A hand session has none; two, or a spelling that needs resolving,
    is not a directory this run can be said to own."""
    assert codex_run.own_automation(_settings(roots)) is None


@pytest.mark.parametrize("settings", [None, {}, {"sandbox_policy": None}])
def test_missing_settings_name_no_directory(settings):
    assert codex_run.own_automation(settings) is None


# --- the reset ------------------------------------------------------------


def test_a_large_memory_file_is_replaced_by_the_stub(tmp_path):
    memory = tmp_path / codex_run.MEMORY_FILE
    memory.write_text("Last run: 2026-09-18T18:41:47Z\n" * 3000)

    assert codex_run.reset_memory(str(tmp_path)) == "reset"

    assert memory.read_text() == codex_run.MEMORY_STUB
    assert list(tmp_path.glob("*.reset")) == []


def test_two_resets_in_a_row_both_succeed(tmp_path):
    """A unique temporary name per reset: two at once cannot collide."""
    assert codex_run.reset_memory(str(tmp_path)) == "reset"
    assert codex_run.reset_memory(str(tmp_path)) == "reset"
    assert list(tmp_path.glob("*.reset")) == []


def test_a_missing_memory_file_is_created(tmp_path):
    assert codex_run.reset_memory(str(tmp_path)) == "reset"

    assert (tmp_path / codex_run.MEMORY_FILE).read_text() == \
        codex_run.MEMORY_STUB


def test_a_missing_directory_is_reported_not_raised(tmp_path):
    result = codex_run.reset_memory(str(tmp_path / "gone"))

    assert result.startswith("failed: ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_an_unwritable_directory_is_reported_and_leaves_nothing(tmp_path):
    memory = tmp_path / codex_run.MEMORY_FILE
    memory.write_text("old notes\n")
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = codex_run.reset_memory(str(tmp_path))
    finally:
        tmp_path.chmod(stat.S_IRWXU)

    assert result.startswith("failed: ")
    assert memory.read_text() == "old notes\n"
    assert list(tmp_path.glob("*.reset")) == []


def test_no_directory_is_skipped():
    assert codex_run.reset_memory(None) == "skipped: no automation directory"
    assert codex_run.reset_memory("") == "skipped: no automation directory"


def test_the_stub_is_short_and_names_no_file_to_read():
    """History lives on GitHub and in the heartbeat. A path here would
    invite an extra read on every run, and nothing in it is advice."""
    stub = codex_run.MEMORY_STUB
    assert "#1317" in stub
    assert "routines/" not in stub
    assert ".md`" not in stub
    assert len(stub.splitlines()) < 8


def test_the_suite_never_resets_a_real_memory_file():
    """The shared fixture stubs the seam: a passing check against fixture
    roots can name this machine's real automation directory."""
    assert funnel._codex_memory_reset(OWN) == "skipped: stubbed in tests"


# --- the check names the directory ------------------------------------------


def test_a_passing_check_names_the_run_s_automation(tmp_path, monkeypatch):
    workspace = os.path.join(codex_run.SESSION_WORKSPACES, "2026-09-22",
                             "memory-run")
    thread = "01a0b588-c5dd-7a90-ab48-168662b0014f"
    turn = {"model": "gpt-6-luna", "effort": "max", "cwd": workspace,
            "approval_policy": "never",
            "sandbox_policy": {"type": "workspace-write",
                               "writable_roots": [codex_run.HEARTBEAT_SPOOL,
                                                  codex_run.SESSION_WORKSPACES,
                                                  OWN],
                               "network_access": True}}
    import datetime

    now = datetime.datetime(2026, 9, 22, 15, 30)
    day = tmp_path / "2026" / "09" / "22"
    day.mkdir(parents=True)
    (day / "rollout-2026-09-22T15-30-00-{}.jsonl".format(thread)).write_text(
        json.dumps({"type": "session_meta",
                    "payload": {"id": thread, "cwd": workspace}}) + "\n"
        + json.dumps({"type": "turn_context", "payload": turn}) + "\n")

    result = codex_run.check(workspace, now=now, root=str(tmp_path),
                             environ={"CODEX_THREAD_ID": thread})
    assert result["ok"] is True, result
    assert result["automation"] == OWN

    turn["model"] = "gpt-5.6-luna"
    (day / "rollout-2026-09-22T15-30-00-{}.jsonl".format(thread)).write_text(
        json.dumps({"type": "session_meta",
                    "payload": {"id": thread, "cwd": workspace}}) + "\n"
        + json.dumps({"type": "turn_context", "payload": turn}) + "\n")
    refused = codex_run.check(workspace, now=now, root=str(tmp_path),
                              environ={"CODEX_THREAD_ID": thread})
    assert refused["ok"] is False
    assert refused["automation"] is None


# --- begin ----------------------------------------------------------------


def _begin(monkeypatch, capsys, settings):
    monkeypatch.setattr(funnel, "_codex_settings_check", lambda: settings)
    monkeypatch.setattr(funnel, "_codex_memory_reset", REAL_MEMORY_RESET)
    monkeypatch.setattr(funnel, "_start_begin_heartbeat",
                        lambda agent: "run-id")
    monkeypatch.setattr(usage, "read_agent", lambda *args: {"windows": {}})
    monkeypatch.setattr(usage, "pace", lambda *args, **kwargs: {
        "over_pace": True})
    assert funnel.main(["begin", "--agent", "codex", "--tier",
                        "standard"]) == 0
    return json.loads(capsys.readouterr().out)


def test_begin_resets_the_launching_automation_s_memory(
        monkeypatch, capsys, tmp_path):
    memory = tmp_path / codex_run.MEMORY_FILE
    memory.write_text("Do not retry this finish.\n")

    result = _begin(monkeypatch, capsys, {
        "ok": True, "effective": {"model": "gpt-6-luna", "effort": "max"},
        "automation": str(tmp_path)})

    assert result["memory_reset"] == "reset"
    assert memory.read_text() == codex_run.MEMORY_STUB


def test_a_failed_reset_is_reported_and_the_run_goes_on(
        monkeypatch, capsys, tmp_path):
    """The settings check passed; memory is not a safety boundary, so the
    run proceeds to its usage gate."""
    result = _begin(monkeypatch, capsys, {
        "ok": True, "effective": {"model": "gpt-6-luna", "effort": "max"},
        "automation": str(tmp_path / "gone")})

    assert result["memory_reset"].startswith("failed: ")
    assert result["gate"] == "over"


def test_a_run_with_no_automation_touches_no_memory(monkeypatch, capsys):
    result = _begin(monkeypatch, capsys, {
        "ok": True, "effective": {"model": "gpt-6-luna", "effort": "max"}})

    assert result["memory_reset"] == "skipped: no automation directory"


def test_a_refused_run_touches_no_memory(monkeypatch, capsys, tmp_path):
    memory = tmp_path / codex_run.MEMORY_FILE
    memory.write_text("notes\n")
    monkeypatch.setattr(funnel, "_record_begin_config_drift",
                        lambda agent, run, why: None)

    result = _begin(monkeypatch, capsys, {
        "ok": False, "why": "model: expected gpt-6-luna, found x",
        "automation": str(tmp_path)})

    assert result["gate"] == "config"
    assert "memory_reset" not in result
    assert memory.read_text() == "notes\n"
