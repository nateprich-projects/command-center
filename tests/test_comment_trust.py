"""Every runner-marker reader is enumerated, so a new one cannot skip trust.

command-center is public: anyone can comment on it. #1787 made verdicts count
only from ``funnel.TRUSTED_COMMENT_AUTHORS`` (the owner account); #1788
applies the same ``trusted_comment`` test wherever a runner marker, a voice,
or evidence is read out of a comment.

This test walks the source for every function that touches a marker: one
that names a marker constant (a ``<!-- command-center-... -->`` marker, a
``**...**`` header prefix, or a regex built from one), or one that calls a
function listed below as a text parser. Each must be classified here. A new
reader fails the test until it is, and a comment reader classified here must
call ``trusted_comment`` or ``trusted_comments`` itself or name the function
that does.

A marker spelled some other way is not found; add its constant to
``MARKER_SEEDS`` when it is introduced.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from typing import Dict, Iterator, Optional, Set

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


#: Marker constants the value patterns below would not find.
MARKER_SEEDS = frozenset({
    "SELF_APPROVED_PREFIX",
    "SELF_APPROVED_LINE",
    "BLOCK_EVENT_KIND_HEADER_RE",
    "RUN_EVIDENCE_FENCE_RE",
})

#: Parse marker text handed to them: one body or a list of bodies. They never
#: see a comment row, so whoever hands them comment text must filter it; their
#: callers are enumerated in turn.
TEXT_PARSERS = frozenset({
    "funnel.py:parse_provenance",
    "funnel.py:parse_verdict",
    "funnel.py:render_voice",
    "funnel.py:parse_self_approval",
    "funnel.py:parse_park_comment",
    "funnel.py:_parse_block_comment_header",
    "funnel.py:_unconditioned_event_reason",
    "funnel.py:_parse_block_comment_details",
    "funnel.py:parse_block_comment",
    "funnel.py:unparseable_block_comment_lines",
    "funnel.py:parse_needs_decision_comment",
    "funnel.py:parse_decline_comment",
    "funnel.py:parse_decline_route_comment",
    "funnel.py:parse_satisfied_block_comment",
    "funnel.py:parse_origin_override",
    "funnel.py:parse_origin",
    "funnel.py:parse_analysis_marker",
    "funnel.py:parse_caused_by",
    "funnel.py:parse_gates_answer",
    "funnel.py:parse_shape_risk_record",
    "engine/review.py:parse_run_evidence_comment",
    "engine/review.py:split_evidence_block",
    "engine/implement.py:routed_for_closed_step",
    "outcomes.py:_provenance",
})

#: Read comment rows and parse a marker or a voice out of them, mapped to the
#: function that applies the trust filter for them.
COMMENT_READERS: Dict[str, str] = {
    "funnel.py:_verdict_from_comment": "funnel.py:_verdict_from_comment",
    "funnel.py:_review_verdicts": "funnel.py:_review_verdicts",
    "funnel.py:_load_block_comment": "funnel.py:_load_block_comment",
    "funnel.py:_self_approval_markers": "funnel.py:_self_approval_markers",
    "funnel.py:_parked_item_json": "funnel.py:_parked_item_json",
    "funnel.py:_latest_park_comment": "funnel.py:_latest_park_comment",
    "funnel.py:_closed_itself_item_json": "funnel.py:_closed_itself_item_json",
    "funnel.py:_cleared_block_item_json": "funnel.py:_cleared_block_item_json",
    "funnel.py:_codex_decline_events": "funnel.py:_decline_routing_comment_rows",
    "funnel.py:_decline_routing_outcome":
        "funnel.py:_decline_routing_comment_rows",
    "funnel.py:render_comment_voice": "funnel.py:render_comment_voice",
    "engine/review.py:ticket_comments": "engine/review.py:ticket_comments",
    "engine/review.py:_shape_pr_comment": "engine/review.py:_shape_pr_comment",
    "engine/review_apply.py:_latest_review_comment":
        "engine/review_apply.py:_latest_review_comment",
    "engine/implement.py:_finish_closed_human_step":
        "engine/implement.py:read_ticket_comment_bodies",
    "outcomes.py:_verdicts": "outcomes.py:_verdicts",
    "outcomes.py:_direct_nate_comment": "outcomes.py:_direct_nate_comment",
}

#: Read comments without parsing a marker, so the walk cannot find them: they
#: hand comment text to a model, read a comment as evidence, or return the
#: bodies a parser above reads. Each must filter. (``engine/review.py``'s
#: ``precheck_verdict`` passes the packet's comments to
#: ``verdict_covers_head``, which filters them.)
COMMENT_TEXT_READERS = frozenset({
    "funnel.py:verdict_covers_head",
    "funnel.py:_decline_routing_comment_rows",
    "engine/shape.py:issue_thread_section",
    "engine/implement.py:read_ticket_comment_bodies",
    "engine/implement.py:_verify_evidence_url",
})

_PLAN_BODY = "an issue or plan body, which only its author and collaborators edit"

#: Read a marker from an issue body, never from a comment. The body is a
#: separate vector (#1769).
BODY_READERS: Dict[str, str] = {
    "funnel.py:_can_close_itself": _PLAN_BODY,
    "funnel.py:_acceptance_waiting_reason": _PLAN_BODY,
    "funnel.py:shaped_self_approvable": _PLAN_BODY,
    "funnel.py:_shaped_risk_holds": _PLAN_BODY,
    "funnel.py:gate_question": _PLAN_BODY,
    "funnel.py:cmd_answer_gates": _PLAN_BODY,
    "funnel.py:_decline_route_withholds_startability": _PLAN_BODY,
    "funnel.py:recorded_cause_regressions": _PLAN_BODY,
    "metrics.py:_project_has_prior_cause": _PLAN_BODY,
    "engine/shape.py:collect": _PLAN_BODY,
    "engine/shape.py:preview_decision": _PLAN_BODY,
    "engine/shape.py:apply_shape": _PLAN_BODY,
    "engine/breakdown.py:apply": _PLAN_BODY,
    "engine/review.py:pr_body_section":
        "a funnel PR's body (#1794), which only its author and collaborators "
        "edit; its evidence block is implementer-reported (#1812)",
    "engine/migrate_canonical_fields.py:infer_values": _PLAN_BODY,
    "engine/migrate_canonical_fields.py:trim_routing_prose": _PLAN_BODY,
    ".github/scripts/watchdog.py:existing_issue":
        "the watchdog's own open issue, found by the marker in its body",
    ".github/scripts/watchdog.py:main":
        "the watchdog's own open issue, found by the marker in its body",
}

#: Render a marker into text the funnel posts, or cut it out of text shown to
#: a person. None reads anything out of a marker.
WRITERS = frozenset({
    "funnel.py:_visible_comment",
    "funnel.py:provenance_block",
    "funnel.py:origin_block",
    "funnel.py:caused_by_block",
    "funnel.py:gates_answer_block",
    "funnel.py:answered_gates_body",
    "funnel.py:_without_gates_record",
    "funnel.py:self_approval_comment",
    "funnel.py:satisfied_block_comment",
    "funnel.py:closed_itself_comment",
    "funnel.py:shape_risk_block",
    "funnel.py:_needs_decision_comment_body",
    "funnel.py:_write_verdict",
    "funnel.py:cmd_park",
    "funnel.py:cmd_capture",
    "funnel.py:cmd_promote",
    "decline_classifier.py:declined_review_routing_comment",
    "decline_classifier.py:declined_unsatisfiable_acceptance_comment",
    "decline_classifier.py:declined_pending_gate_answer_comment",
    "engine/breakdown.py:apply_question",
    "engine/implement.py:render_closed_step_route",
    "engine/implement.py:finish_declined",
    "engine/implement.py:close_declined_defer_note_proof",
    "engine/implement.py:render_evidence_block",
})

TRUST_FILTERS = frozenset({"trusted_comment", "trusted_comments"})


def _sources() -> Iterator[pathlib.Path]:
    """Every Python source the funnel runs, scripts without a suffix too."""
    yield from ROOT.glob("*.py")
    yield from (ROOT / "engine").glob("*.py")
    yield from (ROOT / "funnel-mcp-connector" / "src").rglob("*.py")
    yield from (ROOT / ".github" / "scripts").glob("*.py")
    for path in (ROOT / "scripts").iterdir():
        if not path.is_file() or path.suffix == ".py":
            continue
        first = path.read_text(errors="replace").split("\n", 1)[0]
        if first.startswith("#!") and "python" in first:
            yield path


def _trees() -> Dict[str, ast.Module]:
    return {
        path.relative_to(ROOT).as_posix(): ast.parse(path.read_text())
        for path in sorted(set(_sources()))
    }


def _units(trees: Dict[str, ast.Module]) -> Dict[str, ast.AST]:
    """Module functions and class methods, named ``path:function``."""
    found: Dict[str, ast.AST] = {}
    for rel, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found["{}:{}".format(rel, node.name)] = node
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        found["{}:{}.{}".format(rel, node.name, sub.name)] = sub
    return found


def _names(node: ast.AST) -> Set[str]:
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def _called(node: ast.AST) -> Set[str]:
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                found.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                found.add(sub.func.attr)
    return found


def _marker_constants(trees: Dict[str, ast.Module]) -> Set[str]:
    """Module-level marker names, and every name built from one."""
    markers = set(MARKER_SEEDS)
    changed = True
    while changed:
        changed = False
        for tree in trees.values():
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                literal = (value.value if isinstance(value, ast.Constant)
                           and isinstance(value.value, str) else "")
                marked = (literal.startswith("**")
                          or "<!-- command-center-" in literal
                          or bool(_names(value) & markers))
                for target in targets:
                    if (marked and isinstance(target, ast.Name)
                            and target.id not in markers):
                        markers.add(target.id)
                        changed = True
    return markers


def _short(qualname: str) -> str:
    return qualname.rsplit(":", 1)[1].rsplit(".", 1)[-1]


def _marker_touchers(
    extra: Optional[Dict[str, ast.Module]] = None,
) -> Dict[str, str]:
    """Every function that names a marker or calls a text parser, and why."""
    trees = _trees()
    trees.update(extra or {})
    units = _units(trees)
    markers = _marker_constants(trees)
    parsers = {_short(name) for name in TEXT_PARSERS}
    found: Dict[str, str] = {}
    for qualname, node in units.items():
        named = sorted(_names(node) & markers)
        calls = sorted(_called(node) & parsers - {_short(qualname)})
        if named or calls:
            found[qualname] = ", ".join(named + calls)
    return found


def _classified() -> Dict[str, str]:
    rows: Dict[str, str] = {}
    for kind, names in (
        ("text parser", TEXT_PARSERS),
        ("comment reader", COMMENT_READERS),
        ("body reader", BODY_READERS),
        ("writer", WRITERS),
    ):
        for name in names:
            assert name not in rows, "{} is both a {} and a {}".format(
                name, rows[name], kind)
            rows[name] = kind
    return rows


def test_every_marker_reader_is_classified():
    """A new reader must say whether it reads comments, and filter if so."""
    touchers = _marker_touchers()
    classified = _classified()
    unclassified = sorted(
        "{} ({})".format(name, why) for name, why in touchers.items()
        if name not in classified
    )
    assert unclassified == [], (
        "These functions read or write a runner marker and are not "
        "classified in tests/test_comment_trust.py. A reader of comment rows "
        "must filter them with funnel.trusted_comment (#1788) and go in "
        "COMMENT_READERS:\n  " + "\n  ".join(unclassified))


def test_the_classification_names_only_real_marker_functions():
    """A stale entry would hide a renamed reader from the walk."""
    touchers = _marker_touchers()
    stale = sorted(set(_classified()) - set(touchers))
    assert stale == []


def test_every_comment_reader_applies_the_trust_filter():
    units = _units(_trees())
    missing = []
    for reader, filtered_by in sorted(COMMENT_READERS.items()):
        node = units.get(filtered_by)
        if node is None or not _called(node) & TRUST_FILTERS:
            missing.append("{} (filtered by {})".format(reader, filtered_by))
    for reader in sorted(COMMENT_TEXT_READERS):
        node = units.get(reader)
        if node is None or not _called(node) & TRUST_FILTERS:
            missing.append(reader)
    assert missing == []


def test_the_walk_finds_a_new_reader_that_skips_the_filter():
    """The enumeration is live: an unlisted reader shows up by name.

    One reader calls a listed parser, one matches a marker regex itself, and
    one names a marker built in its own module; a function that touches no
    marker is not reported.
    """
    extra = {"new_module.py": ast.parse(
        "NEW_MARKER = '<!-- command-center-new -->'\n"
        "NEW_RE = re.compile(re.escape(NEW_MARKER))\n"
        "def wake_from_rows(rows):\n"
        "    return [funnel.parse_park_comment(r['body']) for r in rows]\n"
        "def block_from_rows(rows):\n"
        "    return [r for r in rows if funnel.BLOCK_COMMENT_RE.match(r)]\n"
        "def new_from_rows(rows):\n"
        "    return [r for r in rows if NEW_RE.search(r['body'])]\n"
        "def unrelated(rows):\n"
        "    return [r['body'] for r in rows]\n"
    )}

    found = _marker_touchers(extra)

    assert found["new_module.py:wake_from_rows"] == "parse_park_comment"
    assert found["new_module.py:block_from_rows"] == "BLOCK_COMMENT_RE"
    assert found["new_module.py:new_from_rows"] == "NEW_RE"
    assert "new_module.py:unrelated" not in found
    assert set(found) - set(_classified()) == {
        "new_module.py:wake_from_rows",
        "new_module.py:block_from_rows",
        "new_module.py:new_from_rows",
    }


def test_the_trusted_author_set_is_the_owner_alone():
    assert funnel.TRUSTED_COMMENT_AUTHORS == frozenset({"nateprich"})
    assert funnel.trusted_comments([
        {"author": {"login": "nateprich"}, "body": "kept"},
        {"author": {"login": "mallory"}, "body": "dropped"},
        {"body": "no author"},
        "not a row",
    ]) == [{"author": {"login": "nateprich"}, "body": "kept"}]
    assert funnel.trusted_comments(None) == []
