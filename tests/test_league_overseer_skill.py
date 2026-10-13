"""The League overseer skill (#2442, parent #2418).

The skill is the daily routine's instructions, staged in this checkout at
``skills/league-overseer/SKILL.md`` for the League project's cloud thread
(which attaches The-League and Fantasy-GM). These tests prove both Accept
lines against the markdown itself: the phase loop, install note, snapshot
path and filing fallback are present with the amended (off-Mac) fallback,
and a secret-material grep over the skill stays clean.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

SKILL = ROOT / "skills" / "league-overseer" / "SKILL.md"
LINK = ROOT / ".claude" / "skills" / "league-overseer"


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _flat() -> str:
    """The skill with all whitespace runs collapsed, so a line wrap in the
    markdown cannot fail a phrase assertion."""
    return " ".join(_skill().split())


def test_skill_file_exists_and_is_linked():
    assert SKILL.is_file()
    assert LINK.is_symlink()
    assert LINK.resolve() == SKILL.parent.resolve()
    assert LINK.joinpath("SKILL.md").read_text() == _skill()
    assert _skill().startswith("---\nname: league-overseer\n")


def test_phase_loop_names_five_phases_and_one_daily_step():
    flat = _flat()
    for phase in ("Curate", "Describe", "Hypothesize", "Test", "Implement"):
        assert phase in flat
    assert "one phase step" in flat
    assert "frame" in flat.lower()
    assert "verify" in flat.lower()


def test_records_live_in_the_domain_home():
    flat = _flat()
    assert "domains/league/" in _skill()
    assert "homed in Fantasy-GM" in flat
    assert "Fantasy-GM repo" in flat


def test_install_note_requires_python_312_venv_and_pins():
    skill = _skill()
    flat = _flat()
    assert "python3.12 -m venv" in skill
    assert "3.13" in skill
    assert ">=3.12,<3.13" in skill
    assert "the-league" in skill
    assert "leagues" in skill and "pyproject.toml" in skill
    assert "exact commit pins" in flat


def test_snapshot_path_and_read_only_rule():
    skill = _skill()
    flat = _flat()
    assert "data/fp_snapshots" in skill
    assert "snapshots Fantasy-GM commits" in flat
    assert "Committed files only" in skill
    assert "Never call FantasyPros" in skill


def test_filing_fallback_is_rest_plus_workflow_not_the_mac():
    skill = _skill()
    flat = _flat()
    assert "<!-- command-center-routing -->" in skill
    assert "```json" in skill
    assert "over REST" in skill
    assert "overseer-filing" in skill
    assert "#2512" in skill
    assert "Nothing about filing goes through the Mac" in skill
    assert "mac handoff" not in flat.lower()
    assert "Status Ideas" in skill
    assert "Origin agent" in skill


def test_routing_example_validates_against_live_options():
    blocks = re.findall(
        r"```json[ \t]*\r?\n(.*?)(\r?\n```)", _skill(), re.DOTALL)
    assert len(blocks) == 1
    example = json.loads(blocks[0][0])
    assert set(example) == {"Risk", "Needs", "Domain"}
    assert example["Risk"] in funnel.RISK_OPTIONS
    assert example["Needs"] in funnel.NEEDS_OPTIONS
    assert example["Domain"] in funnel.DOMAIN_OPTIONS
    assert example["Domain"] == "League"


_SECRET_PATTERNS = (
    ("github classic PAT", re.compile(r"ghp_[A-Za-z0-9]{8,}")),
    ("github fine-grained PAT",
     re.compile(r"github_pat_[A-Za-z0-9_]{8,}")),
    ("github OAuth token", re.compile(r"gho_[A-Za-z0-9]{8,}")),
    ("provider secret key", re.compile(r"\bsk-[A-Za-z0-9]{8,}")),
    ("api key header", re.compile(r"x-api-key\s*[:=]")),
    ("assigned key material",
     re.compile(r"api[_-]?key\s*[:=]\s*\S+", re.IGNORECASE)),
    ("assigned secret material",
     re.compile(r"(?:key|token|secret|password)\s*=\s*\S+",
                re.IGNORECASE)),
    ("private key block",
     re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")),
    ("bearer token",
     re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]{8,}")),
)


def test_secret_patterns_catch_realistic_values():
    """A grep that matches nothing would make the guard vacuous."""
    probes = (
        "ghp_abcdefgh1234567890",
        "github_pat_abcdefgh1234",
        "gho_abcdefgh12345678",
        "sk-abcdefgh12345678",
        "x-api-key: fp-live-key-value",
        "api_key=supersecretvalue",
        "FANTASYPROS_TOKEN=tok-live-12345",
        "-----BEGIN PRIVATE KEY-----",
        "Authorization: Bearer abcdefgh12345678",
    )
    for name, pattern in _SECRET_PATTERNS:
        assert any(pattern.search(probe) for probe in probes), name


def test_no_secret_material_in_the_skill():
    skill = _skill()
    hits = [name for name, pattern in _SECRET_PATTERNS
            if pattern.search(skill)]
    assert hits == []
