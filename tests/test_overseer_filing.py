"""Overseer filing into Project 2 (#2512, parent #2418).

The workflow files issues the cloud overseers create over repo-scoped REST,
which cannot reach user-owned Projects (probe 2 failed, #2438). These tests
prove the fail-closed contract: no routing block means no writes and no
comment, and a malformed, missing or unknown routing value leaves the item
unset with a comment saying why. Routing is never defaulted.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "overseer_filing", ROOT / ".github" / "scripts" / "overseer_filing.py"
)
overseer_filing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overseer_filing)

WORKFLOW = ROOT / ".github" / "workflows" / "overseer-filing.yml"

REPO = "nateprich-projects/command-center"


def _body(block: str) -> str:
    return (
        "Propose nightly snapshot checks.\n\n"
        "<!-- command-center-routing -->\n"
        "```json\n" + block + "\n```\n"
    )


VALID = '{"Risk": "standard", "Needs": "agent", "Domain": "League"}'


def test_valid_block_reads_all_three_values():
    block, error = overseer_filing.extract_routing_block(_body(VALID))
    assert error is None
    values, error = overseer_filing.validate_routing(block)
    assert error is None
    assert values == {"Risk": "standard", "Needs": "agent",
                      "Domain": "League"}


def test_missing_marker_skips_silently():
    block, error = overseer_filing.extract_routing_block(
        "A human-filed issue with no routing block.")
    assert (block, error) == (None, None)


def test_marker_without_fenced_block_is_malformed():
    block, error = overseer_filing.extract_routing_block(
        "<!-- command-center-routing -->\nno json here\n")
    assert block is None
    assert "no fenced" in error


def test_invalid_json_is_malformed():
    block, error = overseer_filing.extract_routing_block(
        _body('{"Risk": "standard",'))
    assert block is None
    assert "not valid JSON" in error


def test_non_object_json_is_malformed():
    block, error = overseer_filing.extract_routing_block(_body('["Risk"]'))
    assert block is None
    assert "must be a JSON object" in error


def test_missing_key_names_the_key():
    block, _ = overseer_filing.extract_routing_block(
        _body('{"Risk": "standard", "Needs": "agent"}'))
    values, error = overseer_filing.validate_routing(block)
    assert values is None
    assert "'Domain'" in error


def test_unknown_value_names_the_value_and_options():
    block, _ = overseer_filing.extract_routing_block(
        _body('{"Risk": "urgent", "Needs": "agent", "Domain": "League"}'))
    values, error = overseer_filing.validate_routing(block)
    assert values is None
    assert "unknown Risk 'urgent'" in error
    assert "standard, escalated" in error


def test_matching_is_exact_not_case_insensitive():
    block, _ = overseer_filing.extract_routing_block(
        _body('{"Risk": "Standard", "Needs": "agent", "Domain": "League"}'))
    values, error = overseer_filing.validate_routing(block)
    assert values is None
    assert "unknown Risk 'Standard'" in error


def test_non_string_value_is_rejected():
    block, _ = overseer_filing.extract_routing_block(
        _body('{"Risk": "standard", "Needs": ["agent"], "Domain": "League"}'))
    values, error = overseer_filing.validate_routing(block)
    assert values is None
    assert "non-string Needs" in error


def test_extra_keys_are_ignored():
    block, _ = overseer_filing.extract_routing_block(_body(
        '{"Risk": "standard", "Needs": "agent", "Domain": "League",'
        ' "Class": "Implement"}'))
    values, error = overseer_filing.validate_routing(block)
    assert error is None
    assert values == {"Risk": "standard", "Needs": "agent",
                      "Domain": "League"}


def test_later_marker_cuts_the_span():
    body = (
        "<!-- command-center-routing -->\n"
        "<!-- command-center-provenance -->\n"
        "```json\n" + VALID + "\n```\n"
    )
    block, error = overseer_filing.extract_routing_block(body)
    assert block is None
    assert "no fenced" in error


def test_quoted_marker_inside_json_does_not_cut():
    block_text = (
        '{"Risk": "standard", "Needs": "agent", "Domain": "League",'
        ' "note": "see <!-- command-center-review --> above"}'
    )
    block, error = overseer_filing.extract_routing_block(_body(block_text))
    assert error is None
    values, error = overseer_filing.validate_routing(block)
    assert error is None
    assert values["Risk"] == "standard"


def test_allowed_values_match_funnel():
    assert (overseer_filing.ALLOWED_VALUES["Risk"] ==
            tuple(funnel.RISK_OPTIONS))
    assert (overseer_filing.ALLOWED_VALUES["Needs"] ==
            tuple(funnel.NEEDS_OPTIONS))
    assert (overseer_filing.ALLOWED_VALUES["Domain"] ==
            tuple(funnel.DOMAIN_OPTIONS))


def test_overseer_repos_are_the_four_attached_repos():
    assert overseer_filing.OVERSEER_REPOS == frozenset({
        "nateprich-projects/command-center",
        "nateprich-projects/Fantasy-GM",
        "nateprich-projects/The-League",
        "nateprich-projects/AFL",
    })


def test_failure_comment_names_reason_and_options_without_defaults():
    comment = overseer_filing.failure_comment("routing block is missing 'Risk'")
    assert "routing block is missing 'Risk'" in comment
    assert "no fields set" in comment
    assert "never defaulted" in comment
    assert "standard, escalated" in comment
    assert "overseer-filing" in comment


# --- workflow file shape ---

def test_workflow_runs_on_hobby_linux():
    text = WORKFLOW.read_text()
    assert "runs-on: [self-hosted, linux, x64, hobby-linux]" in text
    assert "ubuntu-latest" not in text


def test_workflow_triggers_on_opened_and_dispatch():
    text = WORKFLOW.read_text()
    assert "types: [opened]" in text
    assert "repository_dispatch" in text
    assert "overseer-filed" in text
    assert "workflow_dispatch" in text
    assert "issue_repo" in text
    assert "issue_number" in text


def test_workflow_files_with_the_filing_token():
    text = WORKFLOW.read_text()
    assert "GH_TOKEN: ${{ secrets.PROJECT_FILING_TOKEN }}" in text
    assert ".github/scripts/overseer_filing.py" in text


# --- main() paths, with gh faked ---

def _project_payload(**overrides):
    def select(name, options):
        return {"id": "FIELD-" + name, "name": name,
                "options": [{"id": "OPT-{}-{}".format(name, option),
                             "name": option} for option in options]}
    fields = [
        select("Status", ["Ideas", "Shaped"]),
        select("Origin", ["agent", "Nate"]),
        select("Risk", list(funnel.RISK_OPTIONS)),
        select("Needs", list(funnel.NEEDS_OPTIONS)),
        select("Domain", list(funnel.DOMAIN_OPTIONS)),
    ]
    for name, options in overrides.get("fields", {}).items():
        for field in fields:
            if field["id"] == "FIELD-" + name:
                field["options"] = [
                    {"id": "OPT-{}-{}".format(name, option), "name": option}
                    for option in options
                ]
    return {"data": {"user": {"projectV2": {
        "id": "PVT-1", "fields": {"nodes": fields},
    }}}}


class _GhFake:
    """A fake `gh` recording every call, answering GraphQL by query shape."""

    def __init__(self, already_filed=False, project=None):
        self.calls = []
        self.already_filed = already_filed
        self.project = project if project is not None else _project_payload()

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("api", "graphql"):
            query = ""
            for entry in args:
                if entry.startswith("query="):
                    query = entry[len("query="):]
            if "projectItems" in query:
                nodes = ([{"id": "ITEM-OLD", "project": {"id": "PVT-1"}}]
                         if self.already_filed else [])
                return json.dumps({"data": {"repository": {"issue": {
                    "id": "ISSUE-1", "projectItems": {"nodes": nodes},
                }}}})
            if "updateProjectV2ItemFieldValue" in query:
                return json.dumps({"data": {"updateProjectV2ItemFieldValue": {
                    "projectV2Item": {"id": self._last_item()}}}})
            return json.dumps(self.project)
        if args[:2] == ("project", "item-add"):
            return json.dumps({"id": "ITEM-9"})
        if args[:3] == ("api", "-X", "POST"):
            return json.dumps({})
        raise AssertionError("unexpected gh call: {}".format(args))

    def _last_item(self):
        return "ITEM-OLD" if self.already_filed else "ITEM-9"

    def writes(self):
        return [call for call in self.calls
                if any("updateProjectV2ItemFieldValue" in str(entry)
                       for entry in call)]

    def comments(self):
        return [call for call in self.calls
                if call[:3] == ("api", "-X", "POST")]


def _event_file(tmp_path, name, payload):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload))
    return path


def _issues_event(number=7, body="plain human issue", repo=REPO):
    return {"issue": {"number": number, "body": body},
            "repository": {"full_name": repo}}


def _run_main(monkeypatch, tmp_path, fake, event_name, payload):
    monkeypatch.setenv("GH_TOKEN", "filing-token")
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH", str(_event_file(tmp_path, event_name, payload)))
    monkeypatch.setattr(overseer_filing, "gh", fake)
    return overseer_filing.main()


def test_main_without_token_fails_before_any_call(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "issues")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_event_file(tmp_path, "issues", _issues_event())))
    fake = _GhFake()
    monkeypatch.setattr(overseer_filing, "gh", fake)
    assert overseer_filing.main() == 2
    assert fake.calls == []


def test_main_refuses_non_overseer_repo(monkeypatch, tmp_path):
    fake = _GhFake()
    status = _run_main(
        monkeypatch, tmp_path, fake, "issues",
        _issues_event(repo="someone-else/other"))
    assert status == 2
    assert fake.calls == []


def test_main_skips_unmarked_issues_without_writes(monkeypatch, tmp_path,
                                                   capsys):
    fake = _GhFake()
    status = _run_main(
        monkeypatch, tmp_path, fake, "issues", _issues_event())
    assert status == 0
    assert fake.calls == []
    assert "no routing block" in capsys.readouterr().out


def test_main_files_valid_block_with_all_five_fields(monkeypatch, tmp_path):
    fake = _GhFake()
    status = _run_main(monkeypatch, tmp_path, fake, "issues",
                       _issues_event(body=_body(VALID)))
    assert status == 0
    assert len(fake.writes()) == 5
    assert fake.comments() == []
    assert any(call[:2] == ("project", "item-add") for call in fake.calls)


def test_main_reuses_an_already_filed_item(monkeypatch, tmp_path):
    fake = _GhFake(already_filed=True)
    status = _run_main(monkeypatch, tmp_path, fake, "issues",
                       _issues_event(body=_body(VALID)))
    assert status == 0
    assert len(fake.writes()) == 5
    assert not any(call[:2] == ("project", "item-add")
                   for call in fake.calls)


def test_main_malformed_block_gets_no_fields_and_a_comment(monkeypatch,
                                                           tmp_path):
    fake = _GhFake()
    status = _run_main(
        monkeypatch, tmp_path, fake, "issues",
        _issues_event(body=_body('{"Risk": "standard",')))
    assert status == 0
    assert any(call[:2] == ("project", "item-add") for call in fake.calls)
    assert fake.writes() == []
    assert len(fake.comments()) == 1
    posted = fake.comments()[0]
    assert "not valid JSON" in posted[-1]


def test_main_unknown_value_gets_no_fields_and_a_comment(monkeypatch,
                                                         tmp_path):
    fake = _GhFake()
    status = _run_main(
        monkeypatch, tmp_path, fake, "issues",
        _issues_event(body=_body(
            '{"Risk": "standard", "Needs": "agent",'
            ' "Domain": "Fantasy-GM"}')))
    assert status == 0
    assert fake.writes() == []
    assert len(fake.comments()) == 1
    assert "unknown Domain 'Fantasy-GM'" in fake.comments()[0][-1]


def test_main_validates_every_write_before_any_write(monkeypatch, tmp_path):
    project = _project_payload(fields={"Status": ["Shaped"]})
    fake = _GhFake(project=project)
    status = _run_main(monkeypatch, tmp_path, fake, "issues",
                       _issues_event(body=_body(VALID)))
    assert status == 1
    assert fake.writes() == []
    assert len(fake.comments()) == 1


def test_main_supports_workflow_dispatch(monkeypatch, tmp_path):
    def dispatch_gh(*args):
        if args[:2] == ("api", "repos/{}/issues/9".format(REPO)):
            return json.dumps({"body": _body(VALID)})
        return _GhFake.__call__(fake, *args)

    fake = _GhFake()
    monkeypatch.setenv("GH_TOKEN", "filing-token")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_event_file(tmp_path, "workflow_dispatch", {
            "inputs": {"issue_repo": REPO, "issue_number": "9"}})))
    monkeypatch.setattr(overseer_filing, "gh", dispatch_gh)
    assert overseer_filing.main() == 0
    assert len(fake.writes()) == 5


def test_main_supports_repository_dispatch(monkeypatch, tmp_path):
    fake = _GhFake()

    def dispatch_gh(*args):
        if args[:2] == ("api", "repos/{}/issues/11".format(REPO)):
            return json.dumps({"body": _body(VALID)})
        return _GhFake.__call__(fake, *args)

    monkeypatch.setenv("GH_TOKEN", "filing-token")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "repository_dispatch")
    monkeypatch.setenv(
        "GITHUB_EVENT_PATH",
        str(_event_file(tmp_path, "repository_dispatch", {
            "client_payload": {"issue_repo": REPO, "issue_number": 11}})))
    monkeypatch.setattr(overseer_filing, "gh", dispatch_gh)
    assert overseer_filing.main() == 0
    assert len(fake.writes()) == 5
