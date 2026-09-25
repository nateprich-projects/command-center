"""breakdown-packet and breakdown-apply with the ticket schema (#809, #794 Ph2).

The breakdown runner shows the model a packet and performs its answer's
effects. Every validation failure gets a fixture test here, so a malformed
answer can never reach a mutation untested.
"""

from __future__ import annotations

import json
import pathlib
import stat
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from engine import breakdown  # noqa: E402


@pytest.fixture(autouse=True)
def canonical_field_writes(monkeypatch):
    monkeypatch.setattr(
        funnel, "write_project_select",
        lambda item_id, field, value, ref: None,
    )

REPO = "owner/repo"
OTHER = "other/repo"


def raw_ticket(**kw):
    data = {
        "title": "do one thing",
        "body": "What: do it.\n\nAccept: it is done.",
        "risk": "standard",
        "depends_on": [],
        "needs": "none",
    }
    data.update(kw)
    return data


def validate(answer, states=None):
    """Validate with a stub issue lookup; unknown refs do not exist."""
    states = states or {}
    seen = []

    def lookup(ref):
        seen.append(ref)
        return states.get(ref)

    errors, normalized = breakdown.validate_answer(answer, lookup)
    return errors, normalized, seen


def sibling_ticket(index, **kw):
    data = {"title": "t{}".format(index), "body": "b{}".format(index),
            "risk": "standard", "needs": "none", "depends_on": []}
    data.update(kw)
    return data


# -- the sizing standard -------------------------------------------------

def test_sizing_standard_holds_the_unit_and_the_sizing_rules():
    found = breakdown.sizing_standard()
    assert "One ticket is one Codex run" in found
    assert "one concern" in found
    assert "a few hundred lines at most" in found
    assert "one way to tell it worked" in found


def test_sizing_standard_routes_inferred_premises_before_the_build():
    found = breakdown.sizing_standard()
    assert "labelled `inferred`" in found
    assert "first verification step" in found
    assert "separate `Investigate` project in `Ideas`" in found
    assert "existing open project in" in found and "`depends_on`" in found
    assert "A ticket carries no `Class`" in found
    assert "adds no label or field" in found


def test_sizing_standard_excludes_the_protocol_the_runner_owns():
    found = breakdown.sizing_standard()
    assert "gh issue create" not in found
    assert "Native dependency edges" not in found
    assert "Boundary checklist" not in found


def test_sizing_standard_fails_loudly_when_the_markers_move():
    try:
        breakdown.sizing_standard(skill_text="# rewritten\n\nno markers\n")
    except breakdown.BreakdownError as exc:
        assert "sizing standard" in str(exc)
    else:
        raise AssertionError("rewritten skill must not parse silently")
    try:
        breakdown.sizing_standard(skill_text="## Ordering and independence\n"
                                             "## The unit\n")
    except breakdown.BreakdownError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("an empty slice must not pass")


# -- project refs ----------------------------------------------------------

def test_bare_number_resolves_against_the_default_repo():
    assert breakdown.parse_project_ref("809", REPO) == (REPO, 809)


def test_full_ref_needs_no_default():
    assert breakdown.parse_project_ref("owner/repo#809") == (REPO, 809)
    assert breakdown.parse_project_ref("  owner/repo#809  ", "x/y") == (
        REPO, 809)


def test_issue_url_parses():
    url = "https://github.com/owner/repo/issues/809"
    assert breakdown.parse_project_ref(url) == (REPO, 809)


def test_bare_number_without_a_repo_is_a_local_error():
    try:
        breakdown.parse_project_ref("809")
    except breakdown.BreakdownError as exc:
        assert "--repo" in str(exc)
    else:
        raise AssertionError("ambiguous number must not parse")


def test_malformed_refs_are_local_errors():
    for bad in ("", "owner/repo", "owner/repo#0", "owner/repo#abc",
                "#809", "owner/#809", "https://example.com/x"):
        try:
            breakdown.parse_project_ref(bad, REPO)
        except breakdown.BreakdownError:
            continue
        raise AssertionError("{!r} must not parse".format(bad))


# -- the assembled packet --------------------------------------------------

def plan(**kw):
    data = {
        "ref": REPO + "#1",
        "number": 1,
        "title": "the plan",
        "url": "https://github.com/{}/issues/1".format(REPO),
        "body": "# Plan\n\nDo the thing.",
        "state": "OPEN",
    }
    data.update(kw)
    return data


def sibling(number, **kw):
    data = {
        "ref": "{}#{}".format(REPO, number),
        "repo": REPO,
        "number": number,
        "title": "ticket {}".format(number),
        "state": "OPEN",
    }
    data.update(kw)
    return data


def packet(**kw):
    args = {
        "repo": REPO,
        "number": 1,
        "plan": plan(),
        "siblings": [sibling(2), sibling(3, state="CLOSED")],
        "sizing": "## The unit\n\nsizing rules\n",
        "collected_at": "2026-09-14T00:00:00+00:00",
    }
    args.update(kw)
    return breakdown.build_packet(**args)


def test_packet_carries_every_field():
    found = packet()
    assert found["project"]["ref"] == REPO + "#1"
    assert found["project"]["title"] == "the plan"
    assert found["project"]["body"] == "# Plan\n\nDo the thing."
    assert found["project"]["state"] == "OPEN"
    assert [entry["number"] for entry in found["siblings"]] == [2, 3]
    assert found["siblings"][1]["state"] == "CLOSED"
    assert found["sizing_standard"] == "## The unit\n\nsizing rules\n"
    assert found["sizing_standard_source"] == breakdown.SIZING_SKILL_PATH
    assert found["collected_at"] == "2026-09-14T00:00:00+00:00"
    json.dumps(found)  # the packet is JSON by contract


def test_packet_with_no_siblings_says_so_plainly():
    found = packet(siblings=[])
    assert found["siblings"] == []


def test_packet_cli_prints_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(breakdown, "fetch_plan", lambda repo, n: plan())
    monkeypatch.setattr(
        breakdown, "fetch_siblings", lambda repo, n: [sibling(2)])
    monkeypatch.setattr(
        breakdown, "sizing_standard", lambda skill_text=None: "sizing\n")
    assert breakdown.packet_main(["owner/repo#1"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["project"]["ref"] == REPO + "#1"
    assert found["project"]["body"].startswith("# Plan")
    assert [entry["ref"] for entry in found["siblings"]] == [REPO + "#2"]
    assert found["sizing_standard"] == "sizing\n"


def test_packet_cli_reports_a_bad_ref_without_a_traceback(capsys):
    assert breakdown.packet_main(["nonsense", "--repo", REPO]) == 1
    assert "not a project ref" in capsys.readouterr().err


# -- structural guarantees ---------------------------------------------------

def test_engine_imports_from_funnel_and_never_the_reverse():
    engine_source = (ROOT / "engine" / "breakdown.py").read_text()
    assert "import funnel" in engine_source
    funnel_source = (ROOT / "funnel.py").read_text()
    assert "import engine" not in funnel_source
    assert "from engine" not in funnel_source


def test_the_packet_entry_point_makes_no_writes():
    # breakdown-apply owns every mutation; the packet path stays read-only.
    mutating = ("pr merge", "pr comment", "pr close", "pr review",
                "pr edit", "pr create", "issue close", "issue create",
                "issue comment", "issue edit", "item-add", "item-edit",
                "item-delete", "--yes", "delete-branch", "--blocked-by",
                "SET_FIELD", "CLEAR_FIELD")
    source = (ROOT / "breakdown-packet").read_text()
    offenders = [verb for verb in mutating if verb in source]
    assert offenders == []


def test_the_entry_points_are_executable():
    for name in ("breakdown-packet", "breakdown-apply"):
        entry = ROOT / name
        assert entry.exists()
        assert entry.stat().st_mode & stat.S_IXUSR


def test_the_needs_enum_is_funnels_not_a_second_copy():
    assert breakdown.NEEDS_OPTIONS == funnel.NEEDS_OPTIONS


# -- validation: shape -------------------------------------------------------

def test_a_non_object_answer_is_rejected():
    for bad in (None, [], "tickets", 42):
        errors, normalized, _ = validate(bad)
        assert normalized is None
        assert any("JSON object" in error for error in errors)


def test_non_list_tickets_are_rejected():
    errors, normalized, _ = validate({"tickets": "one"})
    assert normalized is None
    assert any("must be a list" in error for error in errors)


def test_a_non_object_ticket_is_rejected():
    errors, normalized, _ = validate({"tickets": ["do it"]})
    assert normalized is None
    assert any("ticket 0 must be an object" in error for error in errors)


def test_empty_tickets_without_a_question_are_rejected():
    for answer in ({"tickets": []}, {"tickets": [], "needs_decision": None},
                   {}, {"tickets": [], "needs_decision": "  "}):
        errors, normalized, _ = validate(answer)
        assert normalized is None
        assert any("at least one ticket" in error for error in errors), answer


def test_tickets_with_a_question_are_rejected():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket()], "needs_decision": "which shape?"})
    assert normalized is None
    assert any("not both" in error for error in errors)


def test_a_non_string_question_is_rejected():
    errors, normalized, _ = validate(
        {"tickets": [], "needs_decision": 42})
    assert normalized is None
    assert any("null or a question string" in error for error in errors)


def test_a_question_alone_is_accepted():
    errors, normalized, seen = validate(
        {"tickets": [], "needs_decision": "  which shape?  "})
    assert errors == []
    assert normalized == {"tickets": [], "needs_decision": "which shape?"}
    assert seen == []  # no dependencies, no lookups


# -- validation: ticket fields ------------------------------------------------

def test_a_blank_title_is_rejected():
    errors, normalized, _ = validate({"tickets": [raw_ticket(title="  ")]})
    assert normalized is None
    assert any("ticket 0 needs a non-empty title" in error
               for error in errors)
    ticket = raw_ticket()
    del ticket["title"]
    errors, _, _ = validate({"tickets": [ticket]})
    assert any("ticket 0 needs a non-empty title" in error
               for error in errors)


def test_a_blank_body_is_rejected():
    errors, normalized, _ = validate({"tickets": [raw_ticket(body="")]})
    assert normalized is None
    assert any("ticket 0 needs a non-empty body" in error
               for error in errors)


def test_an_unknown_risk_is_rejected():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket(risk="Standard")]})
    assert normalized is None
    assert any("risk 'Standard'" in error for error in errors)
    assert any("standard, escalated" in error for error in errors)


def test_a_missing_risk_is_rejected():
    ticket = raw_ticket()
    del ticket["risk"]
    errors, _, _ = validate({"tickets": [ticket]})
    assert any("ticket 0 has risk None" in error for error in errors)


def test_an_unknown_needs_is_rejected():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket(needs="claude")]})
    assert normalized is None
    assert any("needs 'claude'" in error for error in errors)
    assert any("none, agent, human, claude-code-environment, external-event"
               in error
               for error in errors)


def test_a_non_list_depends_on_is_rejected():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket(depends_on="owner/repo#9")]})
    assert normalized is None
    assert any("depends_on" in error for error in errors)


def test_a_missing_depends_on_means_no_dependencies():
    ticket = raw_ticket()
    del ticket["depends_on"]
    errors, normalized, seen = validate({"tickets": [ticket]})
    assert errors == []
    assert normalized["tickets"][0]["depends_on"] == []
    assert seen == []


# -- validation: dependencies ---------------------------------------------------

def test_a_sibling_index_resolves_without_a_lookup():
    errors, normalized, seen = validate({"tickets": [
        raw_ticket(), raw_ticket(depends_on=[0])]})
    assert errors == []
    assert normalized["tickets"][1]["depends_on"] == [("sibling", 0)]
    assert seen == []


def test_an_out_of_range_index_is_rejected():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket(depends_on=[1])]})
    assert normalized is None
    assert any("only 0..0 exist" in error for error in errors)
    errors, _, _ = validate({"tickets": [raw_ticket(depends_on=[-1])]})
    assert any("only 0..0 exist" in error for error in errors)


def test_a_ticket_may_not_depend_on_itself():
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(), raw_ticket(depends_on=[1])]})
    assert normalized is None
    assert any("ticket 1 depends on itself" in error for error in errors)


def test_a_boolean_is_not_a_sibling_index():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket(), raw_ticket(depends_on=[True])]})
    assert normalized is None
    assert any("want a sibling index" in error for error in errors)


def test_malformed_dependency_strings_are_rejected():
    for bad in ("#9", "9", "owner/repo", "owner/repo#0", "owner/repo#abc",
                "owner/repo9", "", None, ["owner/repo#9"]):
        errors, normalized, seen = validate(
            {"tickets": [raw_ticket(depends_on=[bad])]})
        assert normalized is None, bad
        assert any("want a sibling index" in error for error in errors), bad
        assert seen == [], bad  # malformed refs cost no lookup


def test_an_unknown_issue_is_rejected():
    errors, normalized, seen = validate(
        {"tickets": [raw_ticket(depends_on=["owner/repo#9"])]}, states={})
    assert normalized is None
    assert any("owner/repo#9, which is not an existing issue" in error
               for error in errors)
    assert seen == ["owner/repo#9"]


def test_a_closed_issue_is_rejected():
    errors, normalized, _ = validate(
        {"tickets": [raw_ticket(depends_on=["owner/repo#9"])]},
        states={"owner/repo#9": "CLOSED"})
    assert normalized is None
    assert any("owner/repo#9, which is not open" in error
               for error in errors)


def test_an_open_issue_resolves():
    errors, normalized, seen = validate(
        {"tickets": [raw_ticket(depends_on=["other/repo#7"])]},
        states={"other/repo#7": "OPEN"})
    assert errors == []
    assert normalized["tickets"][0]["depends_on"] == [
        ("external", "other/repo#7")]
    assert seen == ["other/repo#7"]


def test_each_unique_external_ref_is_looked_up_once():
    errors, normalized, seen = validate({"tickets": [
        raw_ticket(depends_on=["other/repo#7"]),
        raw_ticket(depends_on=["other/repo#7", 0]),
    ]}, states={"other/repo#7": "OPEN"})
    assert errors == []
    assert seen == ["other/repo#7"]


def test_a_two_ticket_cycle_is_rejected():
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(depends_on=[1]), raw_ticket(depends_on=[0])]})
    assert normalized is None
    assert any("ticket 0 -> ticket 1 -> ticket 0" in error
               for error in errors)


def test_a_three_ticket_cycle_is_rejected():
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(depends_on=[1]),
        raw_ticket(depends_on=[2]),
        raw_ticket(depends_on=[0]),
    ]})
    assert normalized is None
    assert any("depend in a cycle" in error for error in errors)
    assert any("ticket 0 -> ticket 1 -> ticket 2 -> ticket 0" in error
               for error in errors)


def test_a_diamond_is_not_a_cycle():
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(),
        raw_ticket(depends_on=[0]),
        raw_ticket(depends_on=[0]),
        raw_ticket(depends_on=[1, 2]),
    ]})
    assert errors == []


def test_every_enum_value_validates():
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(risk="standard", needs="none"),
        raw_ticket(risk="escalated", needs="human"),
        raw_ticket(risk="escalated", needs="claude-code-environment"),
    ]})
    assert errors == []
    assert [ticket["risk"] for ticket in normalized["tickets"]] == [
        "standard", "escalated", "escalated"]
    assert [ticket["needs"] for ticket in normalized["tickets"]] == [
        "none", "human", "claude-code-environment"]


# -- creation order ----------------------------------------------------------------

def test_independent_tickets_keep_answer_order():
    tickets = [sibling_ticket(0), sibling_ticket(1), sibling_ticket(2)]
    assert breakdown.creation_order(tickets) == [0, 1, 2]


def test_blockers_are_created_before_their_dependents():
    tickets = [
        sibling_ticket(0, depends_on=[("sibling", 1)]),
        sibling_ticket(1),
        sibling_ticket(2, depends_on=[("sibling", 0)]),
    ]
    assert breakdown.creation_order(tickets) == [1, 0, 2]


def test_a_diamond_orders_the_base_first_and_stays_stable():
    tickets = [
        sibling_ticket(0, depends_on=[("sibling", 1),
                                      ("sibling", 2)]),
        sibling_ticket(1, depends_on=[("sibling", 3)]),
        sibling_ticket(2, depends_on=[("sibling", 3)]),
        sibling_ticket(3),
    ]
    assert breakdown.creation_order(tickets) == [3, 1, 2, 0]


def test_external_refs_order_nothing():
    tickets = [sibling_ticket(
        0, depends_on=[("external", "other/repo#7")])]
    assert breakdown.creation_order(tickets) == [0]


# -- dependency rendering ---------------------------------------------------------

def test_issue_url_renders_the_blocked_by_shape():
    assert breakdown.issue_url("other/repo#7") == \
        "https://github.com/other/repo/issues/7"


def test_blocked_by_values_keep_answer_order_across_kinds():
    ticket = sibling_ticket(2, depends_on=[("external", "other/repo#7"),
                                           ("sibling", 0)])
    assert breakdown.blocked_by_values(ticket, {0: 101}) == [
        "https://github.com/other/repo/issues/7", "101"]


def test_display_blockers_shorten_only_same_repo_refs():
    ticket = sibling_ticket(1, depends_on=[("sibling", 0),
                                           ("external", "other/repo#7"),
                                           ("external", "owner/repo#9")])
    assert breakdown.display_blockers(
        ticket, {0: "owner/repo#101"}, REPO) == [
            "#101", "other/repo#7", "#9"]


def test_coverage_comment_names_what_the_runner_wrote():
    found = breakdown.coverage_comment_body("owner/repo#1", [
        {"ref": "owner/repo#101", "title": "first",
         "risk": "standard", "needs": "none", "blocked_by": []},
        {"ref": "owner/repo#102", "title": "second",
         "risk": "escalated", "needs": "human",
         "blocked_by": ["#101", "other/repo#7"]},
    ])
    assert found == (
        "Breakdown of owner/repo#1 created 2 tickets:\n"
        "- owner/repo#101: first (Risk: standard, Needs: none)\n"
        "- owner/repo#102: second (Risk: escalated, Needs: human; "
        "blocked by #101, other/repo#7)")


def test_coverage_comment_singularises_one_ticket():
    found = breakdown.coverage_comment_body("owner/repo#1", [
        {"ref": "owner/repo#101", "title": "only",
         "risk": "standard", "needs": "none", "blocked_by": []},
    ])
    assert found.splitlines()[0] == \
        "Breakdown of owner/repo#1 created 1 ticket:"


def test_created_numbers_come_from_the_last_line():
    assert breakdown.parse_created_number(
        "https://github.com/owner/repo/issues/101\n") == 101
    assert breakdown.parse_created_number(
        "some chatter\nhttps://github.com/owner/repo/issues/102\n") == 102


def test_unreadable_create_output_is_a_github_error():
    for bad in ("", "   \n ", "created, trust me"):
        try:
            breakdown.parse_created_number(bad)
        except funnel.GitHubError:
            continue
        raise AssertionError("{!r} must not parse".format(bad))


def create_subissue_with_freeze_state(monkeypatch, *, freeze_state="OPEN",
                                      repo=funnel.REPO, parent=1164, body=None,
                                      blocked_by=()):
    """Run the real breakdown create path against a tiny gh fixture."""
    calls = []

    def fake_bounded(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["issue", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"state": freeze_state}),
                stderr="",
            )
        if command[1:3] == ["issue", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/{}/issues/901\n".format(repo),
                stderr="",
            )
        raise AssertionError("unexpected gh command: {!r}".format(command))

    monkeypatch.setattr(funnel, "_run_bounded_subprocess", fake_bounded)
    funnel.reset_api_usage()
    ticket_body = body or (
        "Parent: #{}\n\nWhat: update routines/muse.md.\n\n"
        "Accept: the new rule is present."
    ).format(parent)
    result = breakdown.create_ticket(
        repo, parent,
        {"title": "update frozen work", "body": ticket_body,
         "risk": "standard"},
        blocked_by,
    )
    return calls, result


@pytest.mark.parametrize(("section", "marker_kind"), [
    ("What", "path"),
    ("Accept", "parser"),
])
def test_frozen_breakdown_ticket_is_emitted_blocked_on_794(
        monkeypatch, section, marker_kind):
    if marker_kind == "path":
        marker = "routines/muse.md"
    else:
        _paths, parsers, _exempt = funnel._canonical_freeze_lists()
        marker = parsers[0]
    if section == "What":
        body = ("Parent: #1164\n\nWhat: update {}.\n\n"
                "Accept: the new behavior is present.").format(marker)
    else:
        body = ("Parent: #1164\n\nWhat: update the parser.\n\n"
                "Accept: preserve the {} parser behavior.").format(marker)
    calls, result = create_subissue_with_freeze_state(
        monkeypatch, body=body,
    )

    assert result[0] == 901
    assert calls[0] == [
        "gh", "issue", "view", "794", "--repo", funnel.REPO,
        "--json", "state",
    ]
    created = calls[1]
    blocker_index = created.index("--blocked-by")
    assert created[blocker_index + 1] == "794"
    body_text = created[created.index("--body") + 1]
    assert funnel.FREEZE_BLOCKER_SENTENCE in body_text
    assert marker in body_text
    assert body_text.endswith(funnel.FREEZE_BLOCKER_SENTENCE)
    assert "Risk: standard" not in body_text


@pytest.mark.parametrize("parent", [794, 1044])
def test_exempt_parent_breakdown_tickets_bypass_the_freeze_guard(
        monkeypatch, parent):
    calls, _result = create_subissue_with_freeze_state(
        monkeypatch, parent=parent)

    assert len(calls) == 1
    assert calls[0][1:3] == ["issue", "create"]
    assert "--blocked-by" not in calls[0]
    assert funnel.FREEZE_BLOCKER_SENTENCE not in calls[0][calls[0].index("--body") + 1]


def test_frozen_breakdown_guard_goes_inert_when_794_is_closed(monkeypatch):
    calls, _result = create_subissue_with_freeze_state(
        monkeypatch, freeze_state="CLOSED")

    assert calls[0][1:3] == ["issue", "view"]
    assert "--blocked-by" not in calls[1]
    assert funnel.FREEZE_BLOCKER_SENTENCE not in calls[1][calls[1].index("--body") + 1]


def test_non_frozen_breakdown_ticket_does_not_read_the_freeze(monkeypatch):
    calls, _result = create_subissue_with_freeze_state(
        monkeypatch,
        body=("Parent: #1164\n\nWhat: update funnel.py.\n\n"
              "Accept: the change is covered by tests."),
    )

    assert len(calls) == 1
    assert calls[0][1:3] == ["issue", "create"]
    assert "--blocked-by" not in calls[0]


# -- apply ---------------------------------------------------------------------

def stub_apply(monkeypatch, **kw):
    """Replace every GitHub effect with a recorder. Returns the calls dict.

    The Project add itself stays real: `gh` is stubbed one layer down so
    every apply test proves the Needs id comes from item-add's answer.
    """
    calls: dict = {"created": [], "added": [], "needs": [], "comments": [],
                   "labels": [], "plans": [], "sibling_reads": []}
    next_number = {"n": 100}

    def fake_plan(repo, number):
        calls["plans"].append((repo, number))
        return kw.get("plan", plan(state="OPEN"))

    def fake_create(repo, parent, ticket, blocked_by):
        if (kw.get("fail_on") == ticket["title"]
                or ticket["title"] in kw.get("fail_titles", set())):
            raise funnel.GitHubError("creation refused")
        next_number["n"] += 1
        calls["created"].append({
            "repo": repo, "parent": parent, "ticket": ticket,
            "blocked_by": list(blocked_by),
            "body": ticket["body"],
            "number": next_number["n"],
        })
        number = next_number["n"]
        ref = "{}#{}".format(repo, number)
        if kw.get("record_created_siblings"):
            kw["siblings"].append({
                "ref": ref, "repo": repo, "number": number,
                "title": ticket["title"], "state": "OPEN",
            })
        return (number, ref,
                "https://github.com/{}/issues/{}".format(repo, number))

    def fake_siblings(repo, parent):
        calls["sibling_reads"].append((repo, parent))
        return kw.get("siblings", [])

    def fake_run_gh(command, **kwargs):
        assert command[1:3] == ["project", "item-add"], command
        calls["added"].append(list(command))
        url = command[command.index("--url") + 1]
        number = url.rsplit("/", 1)[-1]
        if kw.get("project_add_error_on") == number:
            return SimpleNamespace(
                returncode=1, stdout="",
                stderr=kw.get("project_add_error", "not permitted"))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "item-{}".format(number)}),
            stderr="")

    def fake_needs(item_id, needs, ref):
        calls["needs"].append((item_id, needs, ref))

    def fake_comment(repo, number, body, **provenance):
        calls["comments"].append((repo, number, body))

    def fake_label(repo, number):
        calls["labels"].append((repo, number))

    monkeypatch.setattr(breakdown, "fetch_plan", fake_plan)
    monkeypatch.setattr(breakdown, "fetch_siblings", fake_siblings)
    monkeypatch.setattr(breakdown, "create_ticket", fake_create)
    monkeypatch.setattr(funnel, "_run_gh", fake_run_gh)
    monkeypatch.setattr(breakdown, "write_needs", fake_needs)
    monkeypatch.setattr(breakdown, "post_comment", fake_comment)
    monkeypatch.setattr(breakdown, "apply_blocked_label", fake_label)
    return calls


def test_the_create_path_writes_every_ticket_in_order(monkeypatch):
    calls = stub_apply(monkeypatch)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="second", risk="escalated", needs="human",
                   depends_on=[1]),
        raw_ticket(title="first", body="What: base.\n\nRisk: escalated"),
    ]})
    assert errors == []
    assert normalized is not None
    result = breakdown.apply(REPO, 1, normalized)
    assert [row["ticket"]["title"] for row in calls["created"]] == [
        "first", "second"]  # the blocker goes first, not the answer order
    assert calls["created"][0]["blocked_by"] == []
    assert calls["created"][1]["blocked_by"] == ["101"]
    assert calls["created"][0]["parent"] == 1
    assert calls["created"][0]["repo"] == REPO
    # The code-owned Risk line, not the model's conflicting one.
    assert calls["created"][0]["body"] == "What: base."
    assert "Risk:" not in calls["created"][1]["body"]
    assert calls["needs"] == [("item-101", "none", "owner/repo#101"),
                              ("item-102", "human", "owner/repo#102")]
    assert len(calls["comments"]) == 1
    repo, number, body = calls["comments"][0]
    assert (repo, number) == (REPO, 1)
    assert body == breakdown.coverage_comment_body(
        "owner/repo#1", result["created"])
    assert "owner/repo#101" in body and "blocked by #101" in body
    assert calls["labels"] == []
    assert result["project"] == "owner/repo#1"
    assert [row["ref"] for row in result["created"]] == [
        "owner/repo#101", "owner/repo#102"]
    json.dumps(result)


def test_a_cross_project_reference_creates_the_native_edge(monkeypatch):
    calls = stub_apply(monkeypatch)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(depends_on=["other/repo#7"]),
    ]}, states={"other/repo#7": "OPEN"})
    assert errors == []
    assert normalized is not None
    breakdown.apply(REPO, 1, normalized)
    assert calls["created"][0]["blocked_by"] == [
        "https://github.com/other/repo/issues/7"]
    assert "other/repo#7" in calls["comments"][0][2]


def test_the_question_path_posts_and_labels_without_tickets(monkeypatch):
    calls = stub_apply(monkeypatch)
    errors, normalized, _ = validate(
        {"tickets": [], "needs_decision": "tabs or spaces?"})
    assert errors == []
    assert normalized is not None
    result = breakdown.apply(REPO, 1, normalized)
    assert calls["created"] == []
    assert calls["needs"] == []
    assert len(calls["comments"]) == 1
    repo, number, body = calls["comments"][0]
    assert (repo, number) == (REPO, 1)
    assert body.startswith("**Needs a decision:** tabs or spaces?")
    assert calls["labels"] == [(REPO, 1)]
    assert result == {"project": "owner/repo#1",
                      "needs_decision": "tabs or spaces?"}


# -- the answered-Gates marker (#1274) -----------------------------------------
#
# #1167's shape: a project held at a breakdown question that Nate answered in
# session. The comment thread still carries the question, so a lane that reads
# only the thread asks it again — observed at 21:57:38Z, four minutes after the
# block cleared at 21:53:02Z. The plan body is the one place the answer can
# live where every lane sees it.

GATES_ANSWER_PLAN = """# Reach the funnel from general chat

## Needs Nate

- Gates: answered — see the marker below.

{marker}

```json
{payload}
```
"""

VALID_GATES_ANSWER = {
    "answer": "No. The connector never answers a gate; it relays mine.",
    "at": "2026-09-21T21:53:02Z",
    "decider": "nate",
}


def gates_answer_plan(payload):
    """A #1167-shaped plan body carrying one answered-Gates marker."""
    return plan(body=GATES_ANSWER_PLAN.format(
        marker=funnel.GATES_ANSWER_MARKER,
        payload=json.dumps(payload, indent=2),
    ))


def test_an_answered_gates_marker_posts_nothing_and_labels_nothing(monkeypatch):
    calls = stub_apply(monkeypatch, plan=gates_answer_plan(VALID_GATES_ANSWER))
    errors, normalized, _ = validate(
        {"tickets": [],
         "needs_decision": "Should the connector answer gates on Nate's behalf?"})
    assert errors == []
    assert normalized is not None

    result = breakdown.apply(REPO, 1, normalized)

    assert calls["comments"] == []
    assert calls["labels"] == []
    assert calls["created"] == []
    assert calls["needs"] == []
    assert result == {
        "project": "owner/repo#1",
        "needs_decision": "Should the connector answer gates on Nate's behalf?",
        "already_answered": VALID_GATES_ANSWER,
    }
    json.dumps(result)


def test_the_same_answer_without_a_marker_posts_and_blocks_as_before(monkeypatch):
    calls = stub_apply(monkeypatch)
    errors, normalized, _ = validate(
        {"tickets": [],
         "needs_decision": "Should the connector answer gates on Nate's behalf?"})
    assert errors == []
    assert normalized is not None

    result = breakdown.apply(REPO, 1, normalized)

    assert len(calls["comments"]) == 1
    assert calls["comments"][0][2].startswith("**Needs a decision:**")
    assert calls["labels"] == [(REPO, 1)]
    assert "already_answered" not in result


@pytest.mark.parametrize("payload", [
    {"at": "2026-09-21T21:53:02Z", "decider": "nate"},
    {"answer": "   ", "at": "2026-09-21T21:53:02Z", "decider": "nate"},
    {"answer": "No.", "decider": "nate"},
    {"answer": "No.", "at": "not a timestamp", "decider": "nate"},
    {"answer": "No.", "at": "2026-09-21T21:53:02Z"},
    {"answer": "No.", "at": "2026-09-21T21:53:02Z", "decider": ""},
])
def test_a_malformed_gates_marker_posts_and_blocks(monkeypatch, payload):
    """A degraded read waits toward Nate. One re-ask beats a dropped question."""
    calls = stub_apply(monkeypatch, plan=gates_answer_plan(payload))
    errors, normalized, _ = validate(
        {"tickets": [], "needs_decision": "which shape?"})
    assert errors == []
    assert normalized is not None

    result = breakdown.apply(REPO, 1, normalized)

    assert len(calls["comments"]) == 1
    assert calls["labels"] == [(REPO, 1)]
    assert "already_answered" not in result


def test_an_unparseable_gates_marker_posts_and_blocks(monkeypatch):
    body = "{}\n\n```json\n{{not json at all}}\n```\n".format(
        funnel.GATES_ANSWER_MARKER)
    calls = stub_apply(monkeypatch, plan=plan(body=body))
    errors, normalized, _ = validate(
        {"tickets": [], "needs_decision": "which shape?"})
    assert errors == []
    assert normalized is not None

    breakdown.apply(REPO, 1, normalized)

    assert len(calls["comments"]) == 1
    assert calls["labels"] == [(REPO, 1)]


def test_a_marker_never_suppresses_the_create_path(monkeypatch):
    """The marker settles a question. It has no say over tickets."""
    calls = stub_apply(monkeypatch, plan=gates_answer_plan(VALID_GATES_ANSWER))
    errors, normalized, _ = validate({"tickets": [raw_ticket()]})
    assert errors == []
    assert normalized is not None

    result = breakdown.apply(REPO, 1, normalized)

    assert len(calls["created"]) == 1
    assert len(calls["comments"]) == 1  # the coverage comment
    assert calls["labels"] == []
    assert "already_answered" not in result


def test_resume_creates_only_missing_siblings_and_repairs_project_fields(
        monkeypatch):
    siblings = [{
        "ref": "{}#{}".format(REPO, 200 + index),
        "repo": REPO,
        "number": 200 + index,
        "title": "slice {}".format(index),
        "state": "OPEN",
    } for index in range(1, 13)]
    calls = stub_apply(monkeypatch, siblings=siblings)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="slice {}".format(index), needs="human")
        for index in range(1, 17)
    ]})
    assert errors == []
    assert normalized is not None

    result = breakdown.apply(REPO, 1, normalized)

    assert [row["ticket"]["title"] for row in calls["created"]] == [
        "slice 13", "slice 14", "slice 15", "slice 16"]
    assert len(calls["added"]) == 16
    assert [row[2] for row in calls["needs"]] == [
        "{}#{}".format(REPO, 200 + index) for index in range(1, 13)
    ] + ["{}#{}".format(REPO, number) for number in range(101, 105)]
    assert [row["ref"] for row in result["created"]] == [
        "{}#{}".format(REPO, 200 + index) for index in range(1, 13)
    ] + ["{}#{}".format(REPO, number) for number in range(101, 105)]
    assert len(calls["comments"]) == 1
    assert calls["comments"][0][2] == breakdown.coverage_comment_body(
        "{}#1".format(REPO), result["created"])
    assert all(row["ref"] in calls["comments"][0][2]
               for row in result["created"])


def test_rerun_after_mid_apply_failure_finishes_without_duplicates(
        monkeypatch):
    siblings = []
    fail_titles = {"second"}
    calls = stub_apply(
        monkeypatch, siblings=siblings, fail_titles=fail_titles,
        record_created_siblings=True)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="first"), raw_ticket(title="second"),
    ]})
    assert errors == []
    assert normalized is not None

    with pytest.raises(funnel.GitHubError, match="already created: owner/repo#101"):
        breakdown.apply(REPO, 1, normalized)
    assert [row["title"] for row in siblings] == ["first"]

    calls["created"].clear()
    calls["added"].clear()
    calls["needs"].clear()
    calls["comments"].clear()
    fail_titles.clear()
    result = breakdown.apply(REPO, 1, normalized)

    assert [row["ticket"]["title"] for row in calls["created"]] == ["second"]
    assert [row[2] for row in calls["needs"]] == [
        "owner/repo#101", "owner/repo#102"]
    assert [row["ref"] for row in result["created"]] == [
        "owner/repo#101", "owner/repo#102"]
    assert len(calls["comments"]) == 1


def test_ambiguous_titles_do_not_guess_and_explicit_refs_stay_explicit(
        monkeypatch):
    duplicate_siblings = [{
        "ref": "owner/repo#{}".format(number), "repo": REPO,
        "number": number, "title": "same title", "state": "OPEN",
    } for number in (201, 202)]
    assert breakdown.match_existing_siblings(
        [{"title": "same title"}], duplicate_siblings) == {}

    calls = stub_apply(monkeypatch, siblings=duplicate_siblings)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="next", depends_on=["owner/repo#202"]),
    ]}, states={"owner/repo#202": "OPEN"})
    assert errors == []
    assert normalized is not None

    breakdown.apply(REPO, 1, normalized)

    assert calls["created"][0]["blocked_by"] == [
        "https://github.com/owner/repo/issues/202"]


def test_a_closed_project_takes_no_breakdown(monkeypatch):
    calls = stub_apply(monkeypatch, plan=plan(state="CLOSED"))
    errors, normalized, _ = validate({"tickets": [raw_ticket()]})
    assert errors == []
    assert normalized is not None
    try:
        breakdown.apply(REPO, 1, normalized)
    except funnel.GitHubError as exc:
        assert "closed project takes no breakdown" in str(exc)
    else:
        raise AssertionError("a closed project must refuse")
    assert calls["created"] == []
    assert calls["comments"] == []


def test_a_partial_failure_names_the_tickets_already_created(monkeypatch):
    stub_apply(monkeypatch, fail_on="second")
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="first"),
        raw_ticket(title="second"),
    ]})
    assert errors == []
    assert normalized is not None
    try:
        breakdown.apply(REPO, 1, normalized)
    except funnel.GitHubError as exc:
        assert "already created: owner/repo#101" in str(exc)
    else:
        raise AssertionError("the second failure must surface")


def test_a_genuine_project_add_failure_keeps_the_created_issue_list(
        monkeypatch):
    calls = stub_apply(
        monkeypatch, project_add_error_on="102",
        project_add_error="not permitted")
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="first"),
        raw_ticket(title="second"),
    ]})
    assert errors == []
    assert normalized is not None

    try:
        breakdown.apply(REPO, 1, normalized)
    except funnel.GitHubError as exc:
        assert "not permitted" in str(exc)
        assert "already created: owner/repo#101, owner/repo#102" in str(exc)
    else:
        raise AssertionError("a genuine Project add failure must abort")

    assert calls["needs"] == [("item-101", "none", "owner/repo#101")]
    assert calls["comments"] == []


def test_apply_main_rejects_an_invalid_answer_with_exit_2(
        monkeypatch, tmp_path, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("validation must precede every mutation")

    monkeypatch.setattr(breakdown, "create_ticket", fail_if_called)
    monkeypatch.setattr(breakdown, "fetch_issue_state", lambda ref: "OPEN")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket(risk="wild")]}))
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer)]) == 2
    assert "risk 'wild'" in capsys.readouterr().err


def test_apply_main_rejects_unparsable_json_with_exit_2(
        monkeypatch, tmp_path, capsys):
    answer = tmp_path / "answer.json"
    answer.write_text("{not json")
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer)]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_apply_main_creates_and_prints_the_result(
        monkeypatch, tmp_path, capsys):
    calls = stub_apply(monkeypatch)
    monkeypatch.setattr(breakdown, "fetch_issue_state", lambda ref: "OPEN")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket(title="only")]}))
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer)]) == 0
    found = json.loads(capsys.readouterr().out)
    assert found["project"] == "owner/repo#1"
    assert [row["ref"] for row in found["created"]] == ["owner/repo#101"]
    assert len(calls["created"]) == 1


def test_the_needs_id_comes_from_item_add(monkeypatch):
    # Regression for the #851 rejection: Issue.projectItems is empty for
    # org-repo issues in this user-owned Project, so the id must come from
    # `gh project item-add` answering for the created issue's URL.
    calls = stub_apply(monkeypatch)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="first"),
        raw_ticket(title="second"),
    ]})
    assert errors == []
    assert normalized is not None
    breakdown.apply(REPO, 1, normalized)
    assert calls["added"] == [
        ["gh", "project", "item-add", str(funnel.PROJECT_NUMBER),
         "--owner", funnel.PROJECT_OWNER,
         "--url", "https://github.com/owner/repo/issues/101",
         "--format", "json"],
        ["gh", "project", "item-add", str(funnel.PROJECT_NUMBER),
         "--owner", funnel.PROJECT_OWNER,
         "--url", "https://github.com/owner/repo/issues/102",
         "--format", "json"],
    ]
    assert calls["needs"] == [("item-101", "none", "owner/repo#101"),
                              ("item-102", "none", "owner/repo#102")]


def test_already_existing_project_add_confirms_membership_and_continues(
        monkeypatch):
    calls = stub_apply(
        monkeypatch,
        project_add_error_on="101",
        project_add_error=(
            "GraphQL: Content already exists in this project "
            "(addProjectV2Item)"),
    )
    membership_reads = []

    def project_items(include_details=True):
        membership_reads.append(include_details)
        return [SimpleNamespace(
            url="https://github.com/owner/repo/issues/101",
            item_id="existing-item-101",
        )]

    monkeypatch.setattr(funnel, "load_items", project_items)
    errors, normalized, _ = validate({"tickets": [
        raw_ticket(title="first"),
        raw_ticket(title="second"),
    ]})
    assert errors == []
    assert normalized is not None

    result = breakdown.apply(REPO, 1, normalized)

    assert [row["ref"] for row in result["created"]] == [
        "owner/repo#101", "owner/repo#102"]
    assert membership_reads == [False]
    assert calls["needs"] == [
        ("existing-item-101", "none", "owner/repo#101"),
        ("item-102", "none", "owner/repo#102"),
    ]
    assert len(calls["comments"]) == 1


def test_already_existing_project_add_requires_membership_confirmation(
        monkeypatch):
    monkeypatch.setattr(
        funnel, "_run_gh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="",
            stderr="GraphQL: Content already exists in this project"))
    monkeypatch.setattr(funnel, "load_items", lambda include_details=False: [])

    try:
        breakdown.add_to_project("https://github.com/owner/repo/issues/7")
    except funnel.GitHubError as exc:
        assert "did not confirm membership" in str(exc)
    else:
        raise AssertionError("the duplicate signal alone must not prove success")


def test_a_failed_project_add_names_the_created_issue(monkeypatch):
    monkeypatch.setattr(
        funnel, "_run_gh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="not permitted"))
    try:
        breakdown.add_to_project("https://github.com/owner/repo/issues/7")
    except funnel.GitHubError as exc:
        assert "https://github.com/owner/repo/issues/7" in str(exc)
        assert "not permitted" in str(exc)
    else:
        raise AssertionError("a failed add must surface")


def test_an_item_add_without_an_id_is_a_github_error(monkeypatch):
    for bad in ("", "{}", '{"id": null}', "[]", "not json"):
        monkeypatch.setattr(
            funnel, "_run_gh",
            lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=bad, stderr=""))
        try:
            breakdown.add_to_project(
                "https://github.com/owner/repo/issues/7")
        except funnel.GitHubError as exc:
            assert "no item id" in str(exc)
        else:
            raise AssertionError("{!r} must not yield an id".format(bad))


def test_issue_state_reads_missing_as_none_and_transients_as_errors(
        monkeypatch):
    def run_for(stdout, stderr, returncode):
        return lambda *args, **kwargs: SimpleNamespace(
            returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(funnel, "_run_gh", run_for(
        "", "GraphQL: Could not resolve to an issue or pull request with "
        "the number of 999999. (repository.issue)", 1))
    assert breakdown.fetch_issue_state("owner/repo#999999") is None

    monkeypatch.setattr(
        funnel, "_run_gh", run_for("", "HTTP 502 Bad Gateway", 1))
    try:
        breakdown.fetch_issue_state("owner/repo#7")
    except funnel.GitHubError as exc:
        assert "could not read the state" in str(exc)
    else:
        raise AssertionError("a transient failure must surface, not read "
                             "as a missing issue")

    monkeypatch.setattr(
        funnel, "_run_gh",
        run_for('{"number": 7, "state": "OPEN"}', "", 0))
    assert breakdown.fetch_issue_state("owner/repo#7") == "OPEN"
    assert breakdown.fetch_issue_state("bogus") is None


def test_apply_main_reports_an_ambiguous_repo_without_a_traceback(
        monkeypatch, tmp_path, capsys):
    def boom(repo):
        raise funnel.GitHubError(
            "multiple member repos: a, b; --repo is required")

    monkeypatch.setattr(funnel, "resolve_repo", boom)
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket()]}))
    assert breakdown.apply_main(["7", "--answer", str(answer)]) == 1
    err = capsys.readouterr().err
    assert "multiple member repos" in err
    assert "Traceback" not in err


# -- the runner protocol: --attempt and --validate-only ----------------------

def test_validation_exit_maps_attempts_to_retry_then_final():
    """#811: without --attempt the single-shot exit 2 stands; with it, a
    malformed answer is retryable below attempt 2 and final at it."""
    assert breakdown.validation_exit(None) == 2
    assert breakdown.validation_exit(1) == 3
    assert breakdown.validation_exit(2) == 1
    assert breakdown.validation_exit(3) == 1


def test_apply_main_with_attempt_1_asks_for_a_retry(
        monkeypatch, tmp_path, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("validation must precede every mutation")

    monkeypatch.setattr(breakdown, "create_ticket", fail_if_called)
    monkeypatch.setattr(breakdown, "fetch_issue_state", lambda ref: "OPEN")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket(risk="wild")]}))
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer),
         "--attempt", "1"]) == 3
    assert "risk 'wild'" in capsys.readouterr().err


def test_apply_main_with_attempt_2_is_final(
        monkeypatch, tmp_path, capsys):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("validation must precede every mutation")

    monkeypatch.setattr(breakdown, "create_ticket", fail_if_called)
    monkeypatch.setattr(breakdown, "fetch_issue_state", lambda ref: "OPEN")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket(risk="wild")]}))
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer),
         "--attempt", "2"]) == 1
    assert "risk 'wild'" in capsys.readouterr().err


def test_apply_main_with_attempt_rejects_unparsable_json(
        monkeypatch, tmp_path, capsys):
    answer = tmp_path / "answer.json"
    answer.write_text("{not json")
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer),
         "--attempt", "1"]) == 3
    assert "not valid JSON" in capsys.readouterr().err
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer),
         "--attempt", "2"]) == 1


def test_apply_main_validate_only_prints_without_creating(
        monkeypatch, tmp_path, capsys):
    """Validate and report, with no ticket, no edge, no comment, and no
    label; the runner uses this to validate a retried answer."""
    calls = stub_apply(monkeypatch)
    monkeypatch.setattr(breakdown, "fetch_issue_state", lambda ref: "OPEN")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket(title="only")]}))
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer),
         "--validate-only"]) == 0
    found = json.loads(capsys.readouterr().out)
    assert [row["title"] for row in found["tickets"]] == ["only"]
    assert found["needs_decision"] is None
    assert all(value == [] for value in calls.values())


def test_apply_main_validate_only_rejects_a_malformed_answer(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(breakdown, "fetch_issue_state", lambda ref: "OPEN")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket(risk="wild")]}))
    assert breakdown.apply_main(
        ["owner/repo#1", "--answer", str(answer), "--validate-only",
         "--attempt", "1"]) == 3
    assert "risk 'wild'" in capsys.readouterr().err


def test_apply_main_rejects_an_attempt_below_1(tmp_path):
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"tickets": [raw_ticket()]}))
    try:
        breakdown.apply_main(
            ["owner/repo#1", "--answer", str(answer), "--attempt", "0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--attempt 0 must not parse")
