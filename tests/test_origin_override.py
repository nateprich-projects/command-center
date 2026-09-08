"""Origin overrides preserve the asymmetric shaping permission."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def marked(marker, **fields):
    return marker + "\n\n```json\n" + json.dumps(fields) + "\n```"


def override(target, voice=None):
    body = marked(funnel.ORIGIN_OVERRIDE_MARKER, target=target)
    if voice is not None:
        body += "\n\n" + marked(
            funnel.PROVENANCE_MARKER,
            voice=voice,
            agent="claude",
            run="run-149",
            at="2026-09-08T16:00:00+00:00",
        )
    return body


@pytest.mark.parametrize("voice", funnel.PROVENANCE_VOICES)
def test_every_voice_may_override_toward_nate(voice):
    assert funnel.parse_origin_override(override("nate", voice))["target"] == "nate"


def test_unattributed_override_toward_nate_is_safe_to_accept():
    assert funnel.parse_origin_override(override("nate"))["target"] == "nate"


@pytest.mark.parametrize("voice", ["nate-direct", "nate-relayed"])
def test_nate_voice_may_override_toward_agents(voice):
    parsed = funnel.parse_origin_override(override("agents", voice))

    assert parsed == {"target": "agents"}


def test_agent_voice_cannot_authorise_its_own_unattended_shaping():
    assert funnel.parse_origin_override(override("agents", "agent")) is None


def test_override_toward_agents_fails_closed_without_nate_provenance():
    assert funnel.parse_origin_override(override("agents")) is None
    malformed = override("agents") + "\n\n" + marked(
        funnel.PROVENANCE_MARKER, voice="unknown"
    )
    assert funnel.parse_origin_override(malformed) is None


def test_override_parser_reads_only_its_exact_marker():
    review = marked(funnel.REVIEW_MARKER, target="agents")
    provenance = marked(funnel.PROVENANCE_MARKER, target="agents")
    origin = marked("<!-- command-center-origin -->", target="agents")

    assert funnel.parse_origin_override(review) is None
    assert funnel.parse_origin_override(provenance) is None
    assert funnel.parse_origin_override(origin) is None
    assert funnel.parse_verdict(override("nate")) is None
    assert funnel.parse_provenance(override("nate")) is None


@pytest.mark.parametrize("target", [None, "agent", "everyone"])
def test_unknown_override_target_fails_closed(target):
    assert funnel.parse_origin_override(
        marked(funnel.ORIGIN_OVERRIDE_MARKER, target=target)
    ) is None
