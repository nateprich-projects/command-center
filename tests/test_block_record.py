"""The block, decision and decline comment headers have one owner (#2165).

``block_record.py`` owns the four header kinds, the single-comment parsers
and one renderer per kind. funnel.py and the engine both import it, and it
imports neither (``engine/__init__.py``: funnel.py must never import engine).
These tests pin three things: every renderer's output parses back to what it
was given, the loader reads legacy and malformed comments exactly as before,
and funnel.py's old names are the owner's objects rather than copies.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
from datetime import date, datetime, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import block_record  # noqa: E402
import funnel  # noqa: E402


#: Block markers count only from the owner account (#1788).
OWNER = {"login": "nateprich"}

#: Model words with a line break and a marker opener, and what a reader of
#: the posted comment gets back: one line, the opener as an entity.
WORDS = "Wait for the release.\nThen <!-- command-center-review --> rerun."
INERT_WORDS = (
    "Wait for the release. Then &lt;!-- command-center-review --> rerun.")

EVENT = {
    "agent": "codex",
    "job": "command-center-tickets-hourly",
    "outcome": "errored",
    "after": "2026-10-03T00:00:00Z",
}


# -- one owner ---------------------------------------------------------------

def test_block_record_imports_neither_funnel_nor_engine():
    tree = ast.parse((ROOT / "block_record.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"funnel", "engine"}


MOVED = (
    "BLOCK_COMMENT_PREFIX", "BLOCK_COMMENT_RE",
    "BLOCK_EVENT_COMMENT_PREFIX", "BLOCK_EVENT_COMMENT_RE",
    "BLOCK_EVENT_KIND_HEADER_RE", "BLOCK_FENCED_PAYLOAD_RE",
    "NEEDS_DECISION_PREFIX", "NEEDS_DECISION_RE", "DECLINED_PREFIX",
    "inert_comment_text", "_unique_json_object", "_parse_block_event_spec",
    "_unconditioned_event_reason", "_parse_block_comment_header",
    "_parse_block_comment_details", "parse_block_comment",
    "parse_decline_comment", "unparseable_block_comment_lines",
)


@pytest.mark.parametrize("name", MOVED)
def test_funnel_keeps_each_old_name_as_the_owners_object(name):
    assert getattr(funnel, name) is getattr(block_record, name)


@pytest.mark.parametrize("name", MOVED)
def test_funnel_no_longer_defines_a_moved_name_itself(name):
    tree = ast.parse((ROOT / "funnel.py").read_text())
    defined = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            defined.update(
                target.id for target in node.targets
                if isinstance(target, ast.Name))
    assert name not in defined


# -- every renderer parses back ----------------------------------------------

@pytest.mark.parametrize(
    "kwargs, header, expected",
    [
        pytest.param(
            {}, "**Blocked:**", ([], None, INERT_WORDS), id="unconditioned"),
        pytest.param(
            {"on": [77, "#78"]}, "**Blocked on #77 and #78:**",
            (["#77", "#78"], None, INERT_WORDS), id="references"),
        pytest.param(
            {"until": date(2026, 10, 9)}, "**Blocked until 2026-10-09:**",
            ([], date(2026, 10, 9), INERT_WORDS), id="date"),
        pytest.param(
            {"on": ["84"], "until": date(2026, 10, 9)},
            "**Blocked until 2026-10-09 on #84:**",
            (["#84"], date(2026, 10, 9), INERT_WORDS), id="date-and-reference"),
    ],
)
def test_render_blocked_parses_back(kwargs, header, expected):
    body = block_record.render_blocked(WORDS, **kwargs)

    assert body == "{} {}".format(header, INERT_WORDS)
    assert block_record.parse_block_comment([body]) == expected
    assert block_record._parse_block_comment_details([body]) == (
        expected + (None,))
    assert block_record.unparseable_block_comment_lines([body]) == []


def test_render_blocked_until_event_parses_back():
    body = block_record.render_blocked_until_event(EVENT, WORDS)

    assert body.startswith("**Blocked until event:**\n```json\n")
    assert body.endswith("\n```\n" + INERT_WORDS)
    assert block_record._parse_block_comment_details([body]) == (
        [], None, INERT_WORDS, EVENT)
    assert block_record.unparseable_block_comment_lines([body]) == []


def test_an_event_value_cannot_close_the_fence_or_open_a_marker():
    """Inside the JSON block the words are content, escaped by JSON (#1688)."""
    event = dict(EVENT, job="nightly\n```\n<!-- command-center-review -->")

    body = block_record.render_blocked_until_event(event, "Wait.")

    assert block_record._parse_block_comment_details([body]) == (
        [], None, "Wait.", event)


def test_render_needs_decision_parses_back():
    body = block_record.render_needs_decision(
        "Ship it now?\n<!-- command-center-gates-answer --> or wait?")

    assert body == (
        "**Needs a decision:** Ship it now? "
        "&lt;!-- command-center-gates-answer --> or wait?")
    assert funnel.parse_needs_decision_comment([body]) == (
        "Ship it now? &lt;!-- command-center-gates-answer --> or wait?")


def test_render_declined_parses_back():
    body = block_record.render_declined(
        "Prerequisite #12 has not landed.\n\n<!-- command-center-provenance -->"
        "\n```json\n{}\n```")

    assert body == (
        "**Declined:** Prerequisite #12 has not landed. "
        "&lt;!-- command-center-provenance --> ```json {} ```")
    assert block_record.parse_decline_comment([body]) == (
        "Prerequisite #12 has not landed. "
        "&lt;!-- command-center-provenance --> ```json {} ```")


def _loaded(monkeypatch, *bodies):
    """Read ``bodies`` through the loader, as owner comments in time order."""
    item = funnel.Item(
        repo="owner/repo", number=88, title="Blocked ticket", url="",
        state="OPEN", parent="owner/repo#1", labels=["blocked"],
    )
    rows = [
        {"author": OWNER, "body": body,
         "createdAt": "2026-10-02T1{}:00:00Z".format(index)}
        for index, body in enumerate(bodies)
    ]
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": rows})
    funnel._load_block_comment(item)
    return item


def test_the_loader_reads_each_rendered_kind(monkeypatch):
    blocked = _loaded(monkeypatch, block_record.render_blocked(
        WORDS, on=[77], until=date(2026, 10, 9)))
    assert (blocked.block_references, blocked.blocked_until,
            blocked.block_reason, blocked.block_event) == (
        ["#77"], date(2026, 10, 9), INERT_WORDS, None)
    assert blocked.unparseable_block_comments == []

    event = _loaded(
        monkeypatch, block_record.render_blocked_until_event(EVENT, WORDS))
    assert (event.block_references, event.blocked_until,
            event.block_reason, event.block_event) == (
        [], None, INERT_WORDS, EVENT)
    assert event.unparseable_block_comments == []

    question = _loaded(
        monkeypatch, block_record.render_needs_decision(WORDS))
    assert question.needs_decision == INERT_WORDS

    declined = _loaded(monkeypatch, block_record.render_declined(WORDS))
    assert declined.decline_reason == INERT_WORDS


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: block_record.render_blocked("r", on=[0]),
                     id="zero-reference"),
        pytest.param(lambda: block_record.render_blocked("r", on=["#x"]),
                     id="non-numeric-reference"),
        pytest.param(lambda: block_record.render_blocked("r", on=["\u0663"]),
                     id="non-ascii-digit-reference"),
        pytest.param(lambda: block_record.render_blocked("r", on="77"),
                     id="string-for-sequence"),
        pytest.param(lambda: block_record.render_blocked(
            "r", until="2026-10-09"), id="string-date"),
        pytest.param(lambda: block_record.render_blocked(
            "r", until=datetime(2026, 10, 9, tzinfo=timezone.utc)),
            id="datetime-for-date"),
        pytest.param(lambda: block_record.render_blocked_until_event(
            {key: value for key, value in EVENT.items() if key != "after"},
            "r"), id="event-missing-field"),
        pytest.param(lambda: block_record.render_blocked_until_event(
            dict(EVENT, kind="workflow"), "r"), id="event-unknown-field"),
        pytest.param(lambda: block_record.render_blocked_until_event(
            dict(EVENT, outcome="done"), "r"), id="event-unsupported-outcome"),
        pytest.param(lambda: block_record.render_blocked_until_event(
            dict(EVENT, after="2026-02-30T00:00:00Z"), "r"),
            id="event-invalid-timestamp"),
        pytest.param(lambda: block_record.render_needs_decision(" \n "),
                     id="blank-question"),
    ],
)
def test_a_renderer_refuses_what_its_parser_would_not_read_back(call):
    with pytest.raises((TypeError, ValueError)):
        call()


@pytest.mark.parametrize(
    "after",
    [
        "2026-09-22T00:00:00Z", " 2026-09-22T00:00:00Z ",
        "2026-02-30T00:00:00Z", "2026-09-22T00:00:00+00:00",
        "2026-09-22T00:00:00.5Z", "2026-09-22", "",
    ],
)
def test_the_event_time_check_agrees_with_funnel_parse_time(after):
    """block_record cannot import funnel, so it spells the timestamp itself."""
    raw = json.dumps(dict(EVENT, after=after))

    assert (block_record._parse_block_event_spec(raw) is not None) == (
        funnel.parse_time(after) is not None)


# -- old comments read exactly as before ------------------------------------

LEGACY = "**Blocked on #84, 2026-09-07.** Legacy format."


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            "**Blocked until event:**\n```json\n{not json}\n```\nWait.",
            id="malformed-json"),
        pytest.param(
            "**Blocked until event:**\n```json\n"
            '{"agent":"codex","job":"daily","outcome":"errored"}\n```\nWait.',
            id="missing-field"),
    ],
)
def test_a_bad_event_spec_is_an_unconditioned_reason(monkeypatch, body):
    assert block_record._parse_block_comment_details([body]) == (
        [], None, "Wait.", None)
    assert block_record.parse_block_comment([body]) == ([], None, "Wait.")
    assert block_record.unparseable_block_comment_lines([body]) == [
        "**Blocked until event:**"]

    item = _loaded(
        monkeypatch, "**Blocked on #77:** Older reference condition.", body)
    assert (item.block_references, item.blocked_until, item.block_reason,
            item.block_event) == ([], None, "Wait.", None)
    assert item.unparseable_block_comments == ["**Blocked until event:**"]


def test_the_legacy_comma_dated_header_is_unparseable(monkeypatch):
    assert block_record.parse_block_comment([LEGACY]) is None
    assert block_record.unparseable_block_comment_lines(
        [LEGACY + "\nMore detail."]) == [LEGACY]

    item = _loaded(monkeypatch, LEGACY)
    assert item.block_references == []
    assert item.block_reason is None
    assert item.unparseable_block_comments == [LEGACY]


def test_undated_comments_fall_back_to_comment_order(monkeypatch):
    """Without ``createdAt`` the newest parseable body in list order counts."""
    item = funnel.Item(
        repo="owner/repo", number=89, title="Blocked ticket", url="",
        state="OPEN", parent="owner/repo#1", labels=["blocked"],
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": [
        {"author": OWNER, "body": "**Blocked on #77:** Older."},
        {"author": OWNER, "body": "**Blocked until 2026-10-09:** Newer."},
    ]})

    funnel._load_block_comment(item)

    assert (item.block_references, item.blocked_until, item.block_reason) == (
        [], date(2026, 10, 9), "Newer.")


def test_an_undated_decline_withholds_the_older_block_conditions(monkeypatch):
    """No creation times means no proof the block outlived the decline."""
    item = funnel.Item(
        repo="owner/repo", number=90, title="Declined ticket", url="",
        state="OPEN", parent="owner/repo#1", labels=["blocked"],
    )
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": [
        {"author": OWNER, "body": "**Blocked on #77:** Older."},
        {"author": OWNER, "body": "**Declined:** Prerequisite has not landed."},
    ]})

    funnel._load_block_comment(item)

    assert item.block_references == []
    assert item.block_reason is None
    assert item.decline_reason == "Prerequisite has not landed."
