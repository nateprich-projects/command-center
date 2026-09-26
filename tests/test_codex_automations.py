"""funnel doctor checks the Codex automation files against the manifest (#1318)."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codex_run  # noqa: E402
import funnel  # noqa: E402


ALL_DAY = ("RRULE:FREQ=HOURLY;INTERVAL=1;BYMINUTE=0,10,20,30,40,50;"
           "BYDAY=SU,MO,TU,WE,TH,FR,SA")
EVERY_HOUR = ("RRULE:FREQ=WEEKLY;BYDAY=SU,MO,TU,WE,TH,FR,SA;BYHOUR="
              + ",".join(str(hour) for hour in range(24))
              + ";BYMINUTE=4;BYSECOND=0")
WEEKEND = "RRULE:FREQ=WEEKLY;BYDAY=SA,SU;BYHOUR=2,3;BYMINUTE=4;BYSECOND=0"
_NO_ERROR_FIELD = object()


def _automation(root, name, *, model="gpt-6-luna", effort="max",
                status="PAUSED", rrule=ALL_DAY, memory=None):
    """One automation directory, in the flat TOML the app writes."""
    directory = pathlib.Path(root) / name
    directory.mkdir(parents=True, exist_ok=True)
    prompt = "# Codex routine — one ticket per run\n\nRun \"begin\".\n"
    lines = ['version = 1', 'id = "{}"'.format(name), 'kind = "cron"',
             'name = "Command Center tickets"',
             "prompt = {}".format(json.dumps(prompt)),
             'status = "{}"'.format(status), 'rrule = "{}"'.format(rrule)]
    if model is not None:
        lines.append('model = "{}"'.format(model))
    if effort is not None:
        lines.append('reasoning_effort = "{}"'.format(effort))
    lines += ['notification_policy = "failed_runs_only"',
              'execution_environment = "local"',
              'target = { type = "projectless" }', 'cwds = ["~"]']
    (directory / "automation.toml").write_text("\n".join(lines) + "\n")
    if memory is not None:
        (directory / "memory.md").write_text(memory)
    return directory


def _manifest_set(root, **overrides):
    """The set the manifest expects: two live, three retired and paused."""
    specs = {
        "command-center-tickets-hourly": {"rrule": ALL_DAY},
        "command-center-tickets-weekday-mornings": {"rrule": EVERY_HOUR},
        "command-center-tickets-mon-fri-after-midnight": {
            "rrule": WEEKEND, "model": "gpt-5.6-sol", "effort": "high"},
        "command-center-tickets-sun-thu-late-night": {
            "rrule": WEEKEND, "model": "gpt-5.6-sol", "effort": "high"},
        "command-center-tickets-weekend-early-mornings": {
            "rrule": WEEKEND, "model": "gpt-5.6-sol", "effort": "high"},
    }
    for name, spec in specs.items():
        spec = dict(spec, **overrides.get(name, {}))
        if spec.pop("absent", False):
            continue
        _automation(root, name, **spec)


def _write_rollout(root, timestamp, *, source="automation",
                   error=_NO_ERROR_FIELD, malformed=False):
    directory = pathlib.Path(root) / "2026" / "09" / "26"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("rollout-2026-09-26T{}.jsonl".format(timestamp))
    if malformed:
        metadata = {"type": "session_meta",
                    "payload": {"thread_source": source}}
        path.write_text(json.dumps(metadata) + '\n{"type":"event_msg"\n')
        return path

    events = [
        {"type": "session_meta", "payload": {"thread_source": source}},
        {"type": "response_item", "payload": {
            "private_transcript": "PRIVATE_MEMBER_REPO_TRANSCRIPT"}},
    ]
    task_complete = {"type": "task_complete"}
    if error is not _NO_ERROR_FIELD:
        task_complete["error"] = error
    events.append({"type": "event_msg", "payload": task_complete})
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def _write_automation_window(root, *, errors=0):
    timestamps = ["13-41-00", "13-42-00", "13-43-00", "13-44-00",
                  "13-45-00"]
    for index, timestamp in enumerate(timestamps):
        error = {"message": "failure at rollout {}".format(timestamp)}
        _write_rollout(
            root, timestamp,
            error=error if index >= len(timestamps) - errors else _NO_ERROR_FIELD,
        )


# --- the manifest -----------------------------------------------------------


def test_the_manifest_names_two_live_automations_and_three_retired():
    assert codex_run.AUTOMATIONS_EXPECTED == {
        "command-center-tickets-hourly": "standard",
        "command-center-tickets-weekday-mornings": "escalated",
    }
    assert codex_run.AUTOMATIONS_RETIRED == {
        "command-center-tickets-mon-fri-after-midnight",
        "command-center-tickets-sun-thu-late-night",
        "command-center-tickets-weekend-early-mornings",
    }


def test_the_fields_are_read_from_the_flat_file(tmp_path):
    directory = _automation(tmp_path, "command-center-x",
                            model="gpt-6-luna", status="ACTIVE")
    fields = codex_run.automation_fields(
        (directory / "automation.toml").read_text())

    assert fields == {"status": "ACTIVE", "rrule": ALL_DAY,
                      "model": "gpt-6-luna", "reasoning_effort": "max"}


def test_the_prompt_is_never_read_as_a_field():
    """Prompts are the run-keeper's; a second derivation here would be one
    more copy to drift."""
    text = 'prompt = "model = \\"gpt-5.6-sol\\""\nmodel = "gpt-6-luna"\n'

    assert codex_run.automation_fields(text) == {"model": "gpt-6-luna"}


# --- findings ----------------------------------------------------------------


def test_a_set_matching_the_manifest_has_no_drift(tmp_path):
    _manifest_set(tmp_path)

    findings = codex_run.automation_findings(str(tmp_path))

    assert findings["drift"] == []
    assert len(findings["notes"]) == 5


def test_today_s_automations_show_their_gpt5_6_pins(tmp_path):
    """The state found on 2026-09-22: both live automations still named the
    old models, and the escalated one ran at `high`."""
    _manifest_set(tmp_path, **{
        "command-center-tickets-hourly": {"model": "gpt-5.6-luna"},
        "command-center-tickets-weekday-mornings": {
            "model": "gpt-5.6-sol", "effort": "high", "rrule": WEEKEND},
    })

    drift = codex_run.automation_findings(str(tmp_path))["drift"]

    assert drift == [
        "command-center-tickets-hourly: model expected gpt-6-luna, found "
        "gpt-5.6-luna",
        "command-center-tickets-weekday-mornings: model expected gpt-6-luna, "
        "found gpt-5.6-sol",
        "command-center-tickets-weekday-mornings: effort expected max, found "
        "high",
    ]


@pytest.mark.parametrize("overrides,fragment", [
    ({"command-center-tickets-hourly": {"absent": True}},
     "command-center-tickets-hourly: missing"),
    ({"command-center-tickets-hourly": {"effort": "high"}},
     "command-center-tickets-hourly: effort expected max, found high"),
    ({"command-center-tickets-hourly": {"model": None}},
     "command-center-tickets-hourly: model expected gpt-6-luna, found None"),
    ({"command-center-tickets-hourly": {"rrule": EVERY_HOUR}},
     "keeper installs it as escalated"),
    ({"command-center-tickets-weekday-mornings": {"rrule": ALL_DAY}},
     "keeper installs it as standard"),
    ({"command-center-tickets-sun-thu-late-night": {"status": "ACTIVE"}},
     "command-center-tickets-sun-thu-late-night: retired, but status is "
     "ACTIVE"),
])
def test_each_difference_is_drift(tmp_path, overrides, fragment):
    _manifest_set(tmp_path, **overrides)

    drift = codex_run.automation_findings(str(tmp_path))["drift"]

    assert any(fragment in line for line in drift), drift


def test_an_automation_outside_the_manifest_is_drift(tmp_path):
    _manifest_set(tmp_path)
    _automation(tmp_path, "command-center-tickets-new")

    drift = codex_run.automation_findings(str(tmp_path))["drift"]

    assert drift == ["command-center-tickets-new: not in the manifest"]


def test_other_automations_are_not_the_funnel_s(tmp_path):
    _manifest_set(tmp_path)
    _automation(tmp_path, "weekly-review", model="gpt-5.6-sol")

    assert codex_run.automation_findings(str(tmp_path))["drift"] == []


def test_an_unreadable_file_is_drift(tmp_path):
    _manifest_set(tmp_path)
    toml = tmp_path / "command-center-tickets-hourly" / "automation.toml"
    toml.unlink()
    toml.mkdir()  # reading it raises, whoever runs the suite

    drift = codex_run.automation_findings(str(tmp_path))["drift"]

    assert any(line.startswith("command-center-tickets-hourly: unreadable")
               for line in drift), drift


def test_a_field_written_twice_is_drift(tmp_path):
    """A hand edit that appends a corrected line under a stale one must not
    read clean: TOML forbids it, and the keeper reads the first `rrule`."""
    _manifest_set(tmp_path)
    toml = tmp_path / "command-center-tickets-hourly" / "automation.toml"
    toml.write_text(toml.read_text() + 'model = "gpt-6-luna"\n')

    drift = codex_run.automation_findings(str(tmp_path))["drift"]

    assert drift == ["command-center-tickets-hourly: `model` is written more "
                     "than once"]


def test_a_raw_tab_does_not_hide_a_field(tmp_path):
    fields = codex_run.automation_fields('rrule = "RRULE:FREQ=DAILY;\tBYHOUR=1"\n')

    assert fields == {"rrule": "RRULE:FREQ=DAILY;\tBYHOUR=1"}


def test_a_file_that_is_not_utf8_is_unreadable_not_fatal(tmp_path):
    """One bad file names itself; the other automations are still read."""
    _manifest_set(tmp_path, **{
        "command-center-tickets-weekday-mornings": {"effort": "high"}})
    toml = tmp_path / "command-center-tickets-hourly" / "automation.toml"
    toml.write_bytes(toml.read_bytes() + b"\xff\n")

    drift = codex_run.automation_findings(str(tmp_path))["drift"]

    assert any(line.startswith("command-center-tickets-hourly: unreadable")
               for line in drift), drift
    assert any("weekday-mornings: effort expected max" in line
               for line in drift), drift


def test_a_retired_automation_that_is_gone_is_not_drift(tmp_path):
    _manifest_set(tmp_path, **{
        "command-center-tickets-sun-thu-late-night": {"absent": True}})

    assert codex_run.automation_findings(str(tmp_path))["drift"] == []


def test_no_automations_directory_is_drift(tmp_path):
    drift = codex_run.automation_findings(str(tmp_path / "missing"))["drift"]

    assert drift == ["no automations directory at {}".format(
        tmp_path / "missing")]


def test_notes_report_status_and_memory_size(tmp_path):
    _manifest_set(tmp_path)
    (tmp_path / "command-center-tickets-hourly" / "memory.md").write_text(
        "x" * 401_588)

    notes = codex_run.automation_findings(str(tmp_path))["notes"]

    assert "command-center-tickets-hourly: PAUSED, memory 401,588 bytes" in \
        notes
    assert "command-center-tickets-sun-thu-late-night: PAUSED, memory none" \
        in notes


# --- the doctor row -----------------------------------------------------------


def test_a_matching_set_passes_with_its_notes(tmp_path):
    _manifest_set(tmp_path)
    rollouts = tmp_path / "sessions"
    _write_automation_window(rollouts)

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=rollouts)

    assert check.ok is True
    assert check.name == "codex automations"
    assert "command-center-tickets-hourly: PAUSED" in check.found
    expected_notes = codex_run.automation_findings(str(tmp_path))["notes"]
    assert check.found == "\n".join("  " + note for note in expected_notes)
    assert "rollout" not in check.found


def test_drift_fails_with_the_edit_procedure(tmp_path):
    _manifest_set(tmp_path, **{
        "command-center-tickets-hourly": {"model": "gpt-5.6-luna"}})
    rollouts = tmp_path / "sessions"
    _write_automation_window(rollouts)

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=rollouts)

    assert check.ok is False
    assert "model expected gpt-6-luna, found gpt-5.6-luna" in check.found
    assert "memory" not in check.found  # notes are not things to fix
    assert check.fix == funnel.CODEX_AUTOMATIONS_FIX
    assert "quit the ChatGPT app" in check.fix


def test_recorded_nine_run_401_streak_fails_and_redacts_error_tokens(tmp_path):
    _manifest_set(tmp_path)
    rollouts = tmp_path / "sessions"
    timestamps = ["15-11-00", "15-31-00", "15-42-00", "15-51-00",
                  "16-00-00", "16-05-00", "16-11-00", "16-20-00",
                  "16-31-00"]
    error = {
        "message": "401 Incorrect API key: sk-proj-0123456789abcdefgh",
        "codex_error_info": "Authorization: Bearer " + "a" * 40,
    }
    for timestamp in timestamps:
        _write_rollout(rollouts, timestamp, error=error)

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=rollouts)

    assert check.ok is False
    assert "5 of 5 newest Codex automation rollouts errored" in check.found
    assert "401 Incorrect API key" in check.found
    assert "[REDACTED]" in check.found
    assert "sk-proj-0123456789abcdefgh" not in check.found
    assert "a" * 40 not in check.found
    assert "PRIVATE_MEMBER_REPO_TRANSCRIPT" not in check.found


def test_mixed_rollouts_fail_with_count_and_newest_error_only(tmp_path):
    _manifest_set(tmp_path)
    rollouts = tmp_path / "sessions"
    _write_automation_window(rollouts, errors=2)

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=rollouts)

    assert check.ok is False
    assert "2 of 5 newest Codex automation rollouts errored" in check.found
    assert "failure at rollout 13-45-00" in check.found
    assert "failure at rollout 13-44-00" not in check.found
    assert "PRIVATE_MEMBER_REPO_TRANSCRIPT" not in check.found


def test_personal_thread_error_does_not_fail_automation_row(tmp_path):
    _manifest_set(tmp_path)
    rollouts = tmp_path / "sessions"
    _write_automation_window(rollouts)
    _write_rollout(
        rollouts, "13-50-00", source="user",
        error={"message": "personal thread failure"},
    )

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=rollouts)

    assert check.ok is True
    assert "rollout" not in check.found
    assert "personal thread failure" not in check.found


def test_unreadable_rollout_directory_reports_unknown(tmp_path):
    _manifest_set(tmp_path)

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=tmp_path / "missing-sessions")

    assert check.ok is False
    assert "unknown" in check.found.lower()


def test_unparseable_rollout_set_reports_unknown(tmp_path):
    _manifest_set(tmp_path)
    rollouts = tmp_path / "sessions"
    _write_automation_window(rollouts)
    _write_rollout(rollouts, "13-50-00", malformed=True)

    check = funnel.check_codex_automations(
        str(tmp_path), rollout_root=rollouts)

    assert check.ok is False
    assert "unknown" in check.found.lower()
    assert "PRIVATE_MEMBER_REPO_TRANSCRIPT" not in check.found


def test_a_reader_that_raises_is_a_finding(monkeypatch):
    def boom(root=None):
        raise RuntimeError("bad disk")

    monkeypatch.setattr(codex_run, "automation_findings", boom)

    check = funnel.check_codex_automations()

    assert check.ok is False
    assert "bad disk" in check.found
