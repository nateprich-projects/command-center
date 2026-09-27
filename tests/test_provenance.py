"""Provenance markers keep issue voices separate from GitHub account names."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402
from funnel import Item  # noqa: E402


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def marked(marker, **fields):
    return marker + "\n\n```json\n" + json.dumps(fields) + "\n```"


def test_provenance_parser_reads_only_its_own_marker():
    provenance = marked(
        funnel.PROVENANCE_MARKER,
        voice="agent", agent="zcode", run="run-1", at=NOW.isoformat(),
    )
    review = marked(
        funnel.REVIEW_MARKER,
        verdict="approved", ci="green", head_sha="abc", blocking=[],
    )

    assert funnel.parse_provenance(provenance)["agent"] == "zcode"
    assert funnel.parse_verdict(provenance) is None
    assert funnel.parse_verdict(review + "\n\n" + provenance)["verdict"] == "approved"
    assert funnel.parse_provenance(review + "\n\n" + provenance)["voice"] == "agent"


def test_nate_relayed_provenance_preserves_the_verbatim_instruction():
    instruction = "  Approve this plan as asked.\nKeep this line too.  "
    body = funnel.append_provenance(
        "General-chat gate instruction received for `approve`.",
        "nate-relayed", at=NOW, instruction=instruction,
    )

    parsed = funnel.parse_provenance(body)
    assert parsed["voice"] == "nate-relayed"
    assert parsed["instruction"] == instruction


def test_origin_parser_reads_only_its_own_marker():
    origin = marked(
        funnel.ORIGIN_MARKER,
        voice="nate-relayed", agent="claude", run="run-1", at=NOW.isoformat(),
    )
    provenance = marked(
        funnel.PROVENANCE_MARKER,
        voice="agent", agent="claude", run="run-1", at=NOW.isoformat(),
    )
    review = marked(
        funnel.REVIEW_MARKER,
        verdict="approved", ci="green", head_sha="abc", blocking=[],
    )

    assert funnel.parse_origin(origin)["voice"] == "nate-relayed"
    assert funnel.parse_origin(provenance) is None
    assert funnel.parse_origin(review) is None
    assert funnel.parse_origin(origin + "\n\n" + provenance)["voice"] == "nate-relayed"


def test_origin_parser_skips_a_quoted_marker_before_the_real_block():
    origin = marked(
        funnel.ORIGIN_MARKER,
        voice="agent", agent="muse", run="run-2", at=NOW.isoformat(),
    )
    body = (
        "The note quotes {} before the captured block.\n\n{}"
    ).format(funnel.ORIGIN_MARKER, origin)

    assert funnel.parse_origin(body) == {
        "agent": "muse",
        "at": NOW.isoformat(),
        "run": "run-2",
        "voice": "agent",
    }


@pytest.mark.parametrize("voice", ["nate-direct", "unknown", None])
def test_origin_parser_rejects_non_capture_voices(voice):
    assert funnel.parse_origin(marked(funnel.ORIGIN_MARKER, voice=voice)) is None


def test_malformed_or_unknown_provenance_fails_closed():
    assert funnel.parse_provenance("ordinary prose") is None
    assert funnel.parse_provenance(funnel.PROVENANCE_MARKER + "\n{not json") is None
    assert funnel.parse_provenance(marked(
        funnel.PROVENANCE_MARKER, voice="someone-else",
    )) is None


@pytest.mark.parametrize(
    ("voice", "agent", "expected"),
    [
        ("nate-direct", "claude", "Nate (direct)"),
        ("nate-relayed", "claude", "Nate (relayed by claude)"),
        ("agent", "zcode", "zcode"),
    ],
)
def test_render_voice_contract(voice, agent, expected):
    body = marked(
        funnel.PROVENANCE_MARKER,
        voice=voice, agent=agent, run="run-1", at=NOW.isoformat(),
    )
    assert funnel.render_voice(body) == expected


def test_render_voice_does_not_infer_authorship_from_an_incomplete_marker():
    body = marked(funnel.PROVENANCE_MARKER, voice="agent", agent=None)
    assert funnel.render_voice(body) == funnel.UNATTRIBUTED


def test_show_renders_each_voice_and_hides_the_marker(monkeypatch, capsys):
    item = Item(
        repo="nateprich/beta", number=7, title="A project",
        url="https://github.com/nateprich/beta/issues/7", state="OPEN",
        status="Ideas", status_since=NOW,
    )
    comments = [
        {"body": "Direct words\n\n" + marked(
            funnel.PROVENANCE_MARKER, voice="nate-direct", agent="claude",
        )},
        {"body": "A relayed decision\n\n" + marked(
            funnel.PROVENANCE_MARKER, voice="nate-relayed", agent="claude",
        )},
        {"body": "Agent work\n\n" + marked(
            funnel.PROVENANCE_MARKER, voice="agent", agent="zcode",
        )},
        {"body": "An old unmarked comment."},
    ]
    monkeypatch.setattr(funnel, "_gh_json", lambda *args: {"comments": comments})

    assert funnel.cmd_show([item], NOW, item.ref) == 0

    output = capsys.readouterr().out
    assert "Nate (direct): Direct words" in output
    assert "Nate (relayed by claude): A relayed decision" in output
    assert "zcode: Agent work" in output
    assert "UNATTRIBUTED: An old unmarked comment." in output
    assert funnel.PROVENANCE_MARKER not in output


def test_comment_requires_voice_before_loading_github(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(funnel, "load_items", lambda: called.append("loaded"))

    with pytest.raises(SystemExit) as exc:
        funnel.main(["comment", "42", "--body", "hello"])

    assert exc.value.code != 0
    assert called == []
    assert "--voice" in capsys.readouterr().err


def test_comment_posts_body_with_the_requested_voice(monkeypatch):
    item = Item(
        repo="nateprich/beta", number=42, title="A ticket",
        url="https://github.com/nateprich/beta/issues/42", state="OPEN",
    )
    monkeypatch.setattr(funnel, "load_items", lambda: [item])
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.main([
        "comment", "42", "--body", "The gate is clear.",
        "--voice", "nate-direct", "--run", "run-42", "--agent", "claude",
    ]) == 0

    assert calls[0][:6] == (
        "gh", "issue", "comment", "42", "--repo", "nateprich/beta",
    )
    posted = calls[0][-1]
    assert posted.startswith("The gate is clear.\n\n" + funnel.PROVENANCE_MARKER)
    assert funnel.render_voice(posted) == "Nate (direct)"
    assert funnel.parse_provenance(posted)["run"] == "run-42"


def test_review_comment_keeps_verdict_parseable_after_provenance(monkeypatch):
    monkeypatch.setattr(
        funnel, "_gh_json",
        lambda *args: {"state": "OPEN", "headRefOid": "abc123"},
    )
    calls = []

    def run(args, capture_output, text=True):
        calls.append(tuple(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(funnel.subprocess, "run", run)

    assert funnel.cmd_review(
        "owner/repo", 5, "approved", "green", [], None,
        run="run-review", agent="zcode",
    ) == 0

    posted = calls[0][-1]
    assert funnel.parse_verdict(posted)["verdict"] == "approved"
    assert funnel.parse_provenance(posted)["voice"] == "agent"
    assert funnel.parse_provenance(posted)["agent"] == "zcode"
