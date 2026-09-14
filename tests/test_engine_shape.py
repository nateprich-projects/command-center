"""shape-packet and shape-apply with the plan schema (#810, Phase 2 of #794).

The shape runner shows the model a packet and nothing else; the model's
structured answer is validated, rendered, and decided mechanically. Each
field and each rule term gets a fixture test here so the cutover can lean
on this module without re-deriving what every piece means.
"""

from __future__ import annotations

import io
import json
import pathlib
import stat
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import shape  # noqa: E402
from funnel import Item  # noqa: E402

REPO = "owner/repo"
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def answer(**kw):
    data = {
        "decided_from_precedent": [
            {"claim": "reviews stay human-gated",
             "source": "plan.md ladder"},
        ],
        "decided_by_agent": [
            {"decision": "validate strictly",
             "alternative": "accept unknown fields",
             "why": "unknown fields signal a confused model"},
        ],
        "needs_nate": {
            "exposure": None,
            "gates": None,
            "scope": None,
            "preference": None,
        },
        "proposed_class": "Improve",
        "plan_markdown": "# Plan\n\nDo the thing.\n",
    }
    data.update(kw)
    return data


def idea(number=42, **kw):
    body = kw.pop("body", None)
    if body is None:
        body = ("Captured note.\n\n" + funnel.origin_block(
            "agent", at=NOW, run="capture-run", agent="muse"))
    data = {
        "repo": REPO,
        "number": number,
        "title": "An idea",
        "url": "https://github.com/{}/issues/{}".format(REPO, number),
        "state": "OPEN",
        "status": "Ideas",
        "klass": "Improve",
        "item_id": "project-item-{}".format(number),
        "body": body,
        "labels": ["needs-shaping"],
    }
    data.update(kw)
    return Item(**data)


def stub_gh(monkeypatch, item):
    """Stub the GitHub reads and writes apply_shape performs."""
    calls = []

    def graphql(query, **variables):
        calls.append(("graphql", query, variables))
        if query == funnel.SET_FIELD:
            return {"updateProjectV2ItemFieldValue": {
                "projectV2Item": {"id": item.item_id},
            }}
        if funnel.CLASS_FIELD_ID in query:
            names = list(funnel.LADDER)
        else:
            names = ["Shaped", "Ready"]
        return {"node": {"options": [
            {"id": "opt-{}".format(name), "name": name}
            for name in names
        ]}}

    def run(args, capture_output=False, text=True, **kwargs):
        calls.append(("run", tuple(args)))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    monkeypatch.setattr(funnel.subprocess, "run", run)
    return calls


def gh_calls(calls, *prefix):
    return [call for call in calls
            if call[0] == "run" and call[1][:len(prefix)] == prefix]


# -- answer validation ---------------------------------------------------

def test_a_well_formed_answer_validates():
    found = shape.validate_answer(answer())
    assert found["proposed_class"] == "Improve"
    assert found["needs_nate"] == {
        "exposure": None, "gates": None, "scope": None,
        "preference": None}
    assert found["decided_from_precedent"] == [
        {"claim": "reviews stay human-gated",
         "source": "plan.md ladder"}]


def test_validation_strips_surrounding_whitespace():
    found = shape.validate_answer(answer(
        proposed_class="  Improve  ",
        plan_markdown="\n# Plan\n",
        needs_nate={"exposure": None, "gates": "  Who decides?  ",
                    "scope": None, "preference": None}))
    assert found["proposed_class"] == "Improve"
    assert found["plan_markdown"] == "# Plan"
    assert found["needs_nate"]["gates"] == "Who decides?"


def test_the_answer_must_be_an_object():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer([])
    with pytest.raises(shape.ShapeError):
        shape.validate_answer("plan")


def test_the_answer_needs_every_key_and_no_extras():
    missing = answer()
    del missing["needs_nate"]
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(missing)
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(risk="standard"))


def test_precedent_entries_need_a_claim_and_a_source():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_from_precedent="plan.md"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(
            decided_from_precedent=[{"claim": "no source"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_from_precedent=[
            {"claim": "c", "source": "s", "extra": "x"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(
            decided_from_precedent=[{"claim": "  ", "source": "s"}]))


def test_an_empty_precedent_list_is_honest():
    found = shape.validate_answer(answer(decided_from_precedent=[]))
    assert found["decided_from_precedent"] == []


def test_agent_decisions_keep_reasoning_and_rejected_alternative():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_by_agent="strict"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_by_agent=[
            {"decision": "d", "alternative": "a"}]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(decided_by_agent=[
            {"decision": "d", "alternative": "", "why": "w"}]))
    found = shape.validate_answer(answer(decided_by_agent=[]))
    assert found["decided_by_agent"] == []


def test_needs_nate_needs_all_four_fields():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate=[]))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": None, "scope": None}))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": None, "scope": None,
            "preference": None, "extra": None}))


@pytest.mark.parametrize("bad", ["", "   ", 0, False, [], {}])
def test_needs_nate_rejects_anything_but_null_or_a_question(bad):
    needs = {"exposure": None, "gates": None, "scope": None,
             "preference": None}
    needs["scope"] = bad
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(needs_nate=needs))


def test_proposed_class_must_be_a_ladder_class():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(proposed_class="Broken2"))
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(proposed_class="  "))


def test_plan_markdown_must_be_non_empty():
    with pytest.raises(shape.ShapeError):
        shape.validate_answer(answer(plan_markdown="  \n "))


# -- rendering -------------------------------------------------------------

def test_render_carries_the_plan_and_every_field():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert body.startswith("# Plan\n\nDo the thing.\n")
    assert "## Decided from precedent" in body
    assert "- reviews stay human-gated (source: plan.md ladder)" in body
    assert "## Decided by the agent" in body
    assert ("- validate strictly (rejected: accept unknown fields; "
            "unknown fields signal a confused model)") in body
    assert "## Needs Nate" in body
    assert ("Do the thing.\n\nProposed class: Improve\n\n"
            "## Decided from precedent") in body


def test_render_uses_the_stable_all_clear_lines():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert ("- Exposure: nothing outstanding. "
            "No new credentials or reachable surface.") in body
    assert "- Gates: nothing outstanding. No gate ownership changes." \
        in body
    assert ("- Scope and priority: nothing outstanding. "
            "The scoped change is documented.") in body
    assert ("- Preference: nothing outstanding. "
            "No user-facing choice remains.") in body


def test_render_records_open_questions_verbatim():
    body = shape.render_plan(shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": "Who may write Ready?",
        "scope": None, "preference": None})))
    assert "- Gates: Who may write Ready?" in body
    assert "- Exposure: nothing outstanding." in body


def test_render_marks_empty_decision_lists():
    body = shape.render_plan(shape.validate_answer(answer(
        decided_from_precedent=[], decided_by_agent=[])))
    assert body.count("None recorded.") == 2


def test_rendered_categories_match_the_stable_headings():
    assert [category for _, category in shape.NEEDS_FIELDS] == \
        list(funnel.NEEDS_NATE_CATEGORIES)


def test_an_all_clear_render_passes_the_old_parser():
    # The decision no longer parses; the rendered body stays readable by
    # the readers that still do, through the cutover.
    body = shape.render_plan(shape.validate_answer(answer()))
    assert funnel.shaped_plan_status(body)[0] == "Ready"


def test_an_open_render_holds_under_the_old_parser():
    body = shape.render_plan(shape.validate_answer(answer(needs_nate={
        "exposure": None, "gates": "Who may write Ready?",
        "scope": None, "preference": None})))
    assert funnel.shaped_plan_status(body) == (
        "Shaped", "open question under Gates")


def test_the_all_clear_render_carries_no_authority_signals():
    body = shape.render_plan(shape.validate_answer(answer()))
    assert funnel.needs_nate_signals(body) == []


# -- the mechanical rule -----------------------------------------------------

def test_an_all_clear_agent_plan_is_ready():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="agent")
    assert status == "Ready"
    assert reason == ("needs_nate all null; class Improve self-approvable; "
                      "origin agent")


def test_one_open_need_holds_at_shaped():
    status, reason = shape.decide(
        shape.validate_answer(answer(needs_nate={
            "exposure": None, "gates": "Who may write Ready?",
            "scope": None, "preference": None})),
        klass="Improve", origin_voice="agent")
    assert status == "Shaped"
    assert reason == "open question under Gates"


def test_every_open_need_is_named():
    status, reason = shape.decide(
        shape.validate_answer(answer(needs_nate={
            "exposure": "New surface?", "gates": None,
            "scope": None, "preference": "Which default?"})),
        klass="Improve", origin_voice="agent")
    assert status == "Shaped"
    assert reason == ("open question under Exposure; "
                      "open question under Preference")


def test_a_non_self_approvable_class_holds():
    validated = shape.validate_answer(answer())
    status, reason = shape.decide(
        validated, klass="New", origin_voice="agent")
    assert (status, reason) == (
        "Shaped", "class New is not self-approvable")
    status, reason = shape.decide(
        validated, klass=None, origin_voice="agent")
    assert (status, reason) == (
        "Shaped", "class unset is not self-approvable")


def test_nates_origin_holds():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="nate-relayed")
    assert (status, reason) == ("Shaped", "origin is Nate's")


def test_a_missing_origin_holds():
    status, _ = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice=None)
    assert status == "Shaped"


def test_an_override_to_agents_restores_eligibility():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="nate-relayed",
        override_target="agents")
    assert status == "Ready"
    assert reason.endswith("origin override to agents")


def test_escalated_risk_holds():
    status, reason = shape.decide(
        shape.validate_answer(answer()),
        klass="Improve", origin_voice="agent",
        escalation_reasons=["credentials"])
    assert (status, reason) == (
        "Shaped", "escalated risk (credentials)")


# -- the assembled packet ----------------------------------------------------

def idea_dict(**kw):
    data = {
        "ref": REPO + "#42",
        "number": 42,
        "title": "An idea",
        "url": "https://github.com/{}/issues/42".format(REPO),
        "body": "Captured note.",
        "labels": ["needs-shaping"],
        "status": "Ideas",
        "klass": None,
    }
    data.update(kw)
    return data


def sibling_dict(number, **kw):
    data = {
        "ref": "{}#{}".format(REPO, number),
        "number": number,
        "title": "Sibling plan",
        "status": "Shaped",
        "klass": "Improve",
        "body": "# Plan\n\nA sibling plan.\n",
    }
    data.update(kw)
    return data


def packet(**kw):
    args = {
        "repo": REPO,
        "idea": idea_dict(),
        "origin_voice": "agent",
        "override_target": None,
        "plan_md": "# design record",
        "plan_md_missing": False,
        "agents_md": "# rule book",
        "agents_md_missing": False,
        "siblings": [sibling_dict(89)],
        "collected_at": "2026-09-14T00:00:00+00:00",
    }
    args.update(kw)
    return shape.build_packet(**args)


def test_packet_carries_every_field():
    found = packet()
    assert found["repo"] == REPO
    assert found["idea"]["ref"] == REPO + "#42"
    assert found["idea"]["body"] == "Captured note."
    assert found["idea"]["labels"] == ["needs-shaping"]
    assert found["idea"]["status"] == "Ideas"
    assert found["origin"] == {"voice": "agent",
                               "override_target": None}
    assert found["plan_md"] == "# design record"
    assert found["plan_md_missing"] is False
    assert found["agents_md"] == "# rule book"
    assert found["agents_md_missing"] is False
    assert found["sibling_plans"] == [sibling_dict(89)]
    assert found["collected_at"] == "2026-09-14T00:00:00+00:00"
    json.dumps(found)  # the packet is JSON by contract


def test_packet_marks_missing_instruction_files():
    found = packet(plan_md="", plan_md_missing=True,
                   agents_md="", agents_md_missing=True)
    assert found["plan_md"] == "" and found["plan_md_missing"] is True
    assert found["agents_md"] == "" and found["agents_md_missing"] is True


def test_packet_reports_an_absent_origin_as_null():
    found = packet(origin_voice=None)
    assert found["origin"] == {"voice": None, "override_target": None}


def test_siblings_are_same_repo_open_plans_sorted_by_ref():
    current = idea(42)
    kept_ready = idea(89, status="Ready", klass="New",
                      body="A ready plan.")
    kept_building = idea(91, status="Building", klass="Broken",
                         body="A building plan.")
    dropped_idea = idea(93, status="Ideas", body="Not a plan yet.")
    dropped_closed = idea(95, status="Shaped", state="CLOSED",
                          body="A closed plan.")
    dropped_ticket = idea(97, status="Shaped", parent=REPO + "#89",
                          body="A child ticket, not a plan.")
    dropped_other_repo = idea(89, repo="other/repo", status="Shaped",
                              body="Another repo's plan.")
    items = [current, dropped_other_repo, kept_building, dropped_ticket,
             dropped_closed, dropped_idea, kept_ready]
    found = shape.sibling_plan_items(items, current)
    assert [row.ref for row in found] == [REPO + "#89", REPO + "#91"]


def test_collect_reads_the_idea_and_its_siblings(monkeypatch):
    current = idea(42, klass=None)
    sibling = idea(89, status="Shaped", body="A sibling plan.")
    other_repo = idea(7, repo="other/repo", status="Shaped",
                      body="Another repo's plan.")
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    found = shape.collect(
        REPO, 42, items_loader=lambda: [current, sibling, other_repo],
        now=NOW)
    assert found["repo"] == REPO
    assert found["idea"]["ref"] == REPO + "#42"
    assert found["idea"]["klass"] is None
    assert found["origin"] == {"voice": "agent",
                               "override_target": None}
    assert found["plan_md"] == "plan.md text"
    assert found["agents_md"] == "AGENTS.md text"
    assert [row["ref"] for row in found["sibling_plans"]] == \
        [REPO + "#89"]
    assert found["collected_at"] == NOW.isoformat()


def test_collect_rejects_an_unknown_idea():
    with pytest.raises(funnel.GitHubError):
        shape.collect(REPO, 42, items_loader=lambda: [idea(43)])


# -- applying an answer ---------------------------------------------------------

def test_apply_advances_an_all_clear_agent_plan_to_ready(
        monkeypatch, capsys):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"
    assert "needs-shaping" not in item.labels

    edits = gh_calls(calls, "gh", "issue", "edit")
    assert len(edits) == 2  # the body write, then the label removal
    written = edits[0][1][-1]
    assert written.startswith("# Plan\n\nDo the thing.\n")
    assert "## Decided from precedent" in written
    assert "## Needs Nate" in written
    assert "Proposed class: Improve" in written
    assert funnel.parse_provenance(written) == {
        "agent": "muse", "at": NOW.isoformat(), "run": "shape-run",
        "voice": "agent"}
    assert edits[1][1][-2:] == ("--remove-label", "needs-shaping")

    comments = gh_calls(calls, "gh", "issue", "comment")
    assert len(comments) == 1
    assert funnel.parse_self_approval(comments[0][1][-1]) == (
        "needs_nate all null; class Improve self-approvable; "
        "origin agent; no escalated risk")

    output = capsys.readouterr().out
    assert "owner/repo#42 → Ready" in output
    assert "advanced to Ready: needs_nate all null" in output


def test_apply_holds_a_plan_with_one_open_need_at_shaped(
        monkeypatch, capsys):
    item = idea(42)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(needs_nate={
            "exposure": None, "gates": "Who may write Ready?",
            "scope": None, "preference": None}),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"
    assert gh_calls(calls, "gh", "issue", "comment") == []
    written = gh_calls(calls, "gh", "issue", "edit")[0][1][-1]
    assert "- Gates: Who may write Ready?" in written
    output = capsys.readouterr().out
    assert "held at Shaped: open question under Gates" in output
    assert "It now waits on you: is the plan good?" in output


def test_apply_never_calls_the_needs_section_parser(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the Needs-section parser was called")

    monkeypatch.setattr(funnel, "plan_needs_nate", fail)
    monkeypatch.setattr(funnel, "shaped_plan_status", fail)
    monkeypatch.setattr(funnel, "_needs_nate_sections", fail)
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"


def test_an_open_section_in_prose_does_not_hold_an_all_clear_answer(
        monkeypatch):
    # The fields rule, not the prose: a stale Needs section inside the
    # narrative cannot hold a plan whose record is all null.
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(plan_markdown=(
            "# Plan\n\nDo the thing.\n\n## Needs you\n\n"
            "- Gates: Who decides?\n")),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"


def test_a_clear_section_in_prose_does_not_release_an_open_answer(
        monkeypatch):
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(
            plan_markdown="# Plan\n\n## Needs you\n\nNothing.\n",
            needs_nate={"exposure": None, "gates": "Who decides?",
                        "scope": None, "preference": None}),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"


def test_apply_rejects_a_malformed_answer_before_any_write(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no write may precede validation")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    item = idea(42)
    bad = answer()
    del bad["needs_nate"]
    with pytest.raises(shape.ShapeError):
        shape.apply_shape([item], NOW, item.ref, bad)
    assert item.status == "Ideas" and item.body.startswith("Captured")


def test_apply_writes_the_class_for_an_unclassed_agent_idea(monkeypatch):
    item = idea(42, klass=None)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    writes = [call for call in calls
              if call[0] == "graphql" and call[1] == funnel.SET_FIELD]
    class_writes = [call for call in writes
                    if call[2].get("field") == funnel.CLASS_FIELD_ID]
    assert len(class_writes) == 1
    assert class_writes[0][2]["option"] == "opt-Improve"
    assert item.status == "Ready"


def test_apply_honours_and_carries_an_override_to_agents(
        monkeypatch, capsys):
    override_block = (
        funnel.ORIGIN_OVERRIDE_MARKER
        + '\n\n```json\n{"target": "agents"}\n```')
    body = (
        "Captured note.\n\n"
        + funnel.origin_block(
            "nate-relayed", at=NOW, run="capture-run", agent="muse")
        + "\n\n"
        + funnel.provenance_block(
            "nate-relayed", at=NOW, run="shape-run", agent="muse")
        + "\n\n" + override_block)
    assert funnel.parse_origin_override(body)["target"] == "agents"
    item = idea(42, body=body)
    calls = stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 0
    assert item.status == "Ready"
    written = gh_calls(calls, "gh", "issue", "edit")[0][1][-1]
    assert override_block in written
    assert "origin override to agents" in capsys.readouterr().out


def test_apply_reports_an_unconfirmed_status_without_marking(monkeypatch):
    item = idea(42)

    def graphql(query, **variables):
        raise funnel.GitHubError("boom")

    def run(args, capture_output=False, text=True, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel, "gh_graphql", graphql)
    watched = []
    monkeypatch.setattr(
        funnel.subprocess, "run",
        lambda *args, **kwargs: watched.append(args) or run(*args,
                                                             **kwargs))
    assert shape.apply_shape(
        [item], NOW, item.ref, answer(),
        run="shape-run", agent="muse") == 1
    assert item.status == "Ideas"
    assert "needs-shaping" in item.labels
    verbs = [tuple(args[0][:3]) for args in watched]
    assert ("gh", "issue", "edit") in verbs  # the body write happened
    assert ("gh", "issue", "comment") not in verbs
    assert all("--remove-label" not in args[0] for args in watched)


def test_apply_holds_an_escalated_plan_at_shaped(monkeypatch, capsys):
    item = idea(42)
    stub_gh(monkeypatch, item)
    assert shape.apply_shape(
        [item], NOW, item.ref,
        answer(plan_markdown="# Plan\n\nRotate the api-key monthly.\n"),
        run="shape-run", agent="muse") == 0
    assert item.status == "Shaped"
    assert "escalated risk (credentials)" in capsys.readouterr().out


# -- structural guarantees -----------------------------------------------------

def test_engine_imports_from_funnel_and_never_the_reverse():
    engine_source = (ROOT / "engine" / "shape.py").read_text()
    assert "import funnel" in engine_source
    funnel_source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in funnel_source
    assert "from engine" not in funnel_source


def test_shape_packet_makes_no_writes():
    # shape-apply owns the shaping writes; the packet path must not
    # perform any. If a mutating verb appears in the packet entry point,
    # the read-only contract is broken.
    mutating = ("pr merge", "pr comment", "pr close", "pr review",
                "pr edit", "pr create", "issue close", "issue create",
                "issue comment", "issue edit", "item-add", "item-edit",
                "item-delete", "--yes", "delete-branch")
    source = (ROOT / "shape-packet").read_text()
    offenders = [verb for verb in mutating if verb in source]
    assert offenders == []
    assert "packet_main" in source


def test_collect_performs_no_subprocess_call(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the packet path must not call out")

    monkeypatch.setattr(funnel, "_run_gh", fail)
    monkeypatch.setattr(funnel, "_gh_json", fail)
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    found = shape.collect(
        REPO, 42, items_loader=lambda: [idea(42)], now=NOW)
    assert found["idea"]["ref"] == REPO + "#42"


def test_the_entry_points_are_executable():
    for name in ("shape-packet", "shape-apply"):
        entry = ROOT / name
        assert entry.exists()
        assert entry.stat().st_mode & stat.S_IXUSR
    assert "apply_main" in (ROOT / "shape-apply").read_text()


# -- the CLIs ------------------------------------------------------------------

def test_packet_cli_prints_valid_json_with_every_field(
        monkeypatch, capsys):
    current = idea(42, klass=None)
    sibling = idea(89, status="Shaped", body="A sibling plan.")
    monkeypatch.setattr(
        funnel, "load_items", lambda: [current, sibling])
    monkeypatch.setattr(
        shape, "fetch_repo_text",
        lambda repo, path: ("{} text".format(path), False))
    assert shape.packet_main(["42", "--repo", REPO]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["repo"] == REPO
    assert found["idea"]["ref"] == REPO + "#42"
    assert found["origin"] == {"voice": "agent",
                               "override_target": None}
    assert found["plan_md"] == "plan.md text"
    assert found["agents_md"] == "AGENTS.md text"
    assert [row["ref"] for row in found["sibling_plans"]] == \
        [REPO + "#89"]


def test_packet_cli_reports_an_unknown_idea(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "load_items", lambda: [idea(43)])
    assert shape.packet_main(["42", "--repo", REPO]) == 1
    assert "shape-packet:" in capsys.readouterr().err


def test_apply_cli_reads_the_answer_from_stdin(
        monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(answer())))
    stub_gh(monkeypatch, item)
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-",
         "--run", "shape-run", "--agent", "muse"]) == 0
    assert item.status == "Ready"
    assert "→ Ready" in capsys.readouterr().out


def test_apply_cli_rejects_invalid_json_before_any_read(
        monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("no GitHub read may precede parsing")

    monkeypatch.setattr(funnel, "load_items", fail)
    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-"]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_apply_cli_rejects_a_malformed_answer_without_writing(
        monkeypatch, capsys):
    item = idea(42)
    monkeypatch.setattr(funnel, "load_items", lambda: [item])

    def fail(*args, **kwargs):
        raise AssertionError("no write may precede validation")

    monkeypatch.setattr(funnel, "gh_graphql", fail)
    monkeypatch.setattr(funnel.subprocess, "run", fail)
    bad = answer()
    del bad["plan_markdown"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad)))
    assert shape.apply_main(
        ["42", "--repo", REPO, "--answer", "-"]) == 1
    assert "plan_markdown" in capsys.readouterr().err
    assert item.status == "Ideas"
