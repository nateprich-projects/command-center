"""AFL overseer skill (#2443, parent #2418).

The skill is the daily routine's instructions, committed in the attached
repository because cloud threads see no ``~/.claude``. These tests prove
the ticket's acceptance seams: the phase loop, install note, snapshot
path and filing fallback are present in the skill markdown, and a grep
over the skill shows no secret material (that grep runs in CI through
this file).
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

SKILL = ROOT / "skills" / "afl-overseer" / "SKILL.md"


def _skill() -> str:
    return SKILL.read_text()


def test_skill_file_exists():
    assert SKILL.is_file()


def test_frontmatter_names_afl_overseer():
    skill = _skill()
    assert skill.startswith("---\n")
    frontmatter = skill.split("---\n", 2)[1]
    assert "name: afl-overseer" in frontmatter
    assert "five phases" in frontmatter
    assert "one step per run" in frontmatter


def test_phase_loop_names_all_five_phases():
    skill = _skill()
    for phase in ("Curate", "Describe", "Hypothesize", "Test", "Implement"):
        assert phase in skill
    assert "one phase step per daily run" in skill


def test_install_note_follows_probe_3_outcome():
    skill = _skill()
    assert "3.12" in skill
    assert "venv" in skill or "virtualenv" in skill
    assert "3.13" in skill
    assert "afl-league" in skill
    assert "pins over git" in skill


def test_snapshot_path_reads_fantasy_gm_snapshots():
    skill = _skill()
    assert "snapshot" in skill
    assert "Fantasy-GM checkout" in skill
    assert "Never call FantasyPros" in skill


def test_filing_fallback_is_rest_or_mac_handoff():
    skill = _skill()
    assert "REST" in skill
    assert "#2417" in skill
    assert "probe 2" in skill
    assert "hand filing to the Mac" in skill


def test_records_live_in_the_domain_home():
    skill = _skill()
    assert "homed in Fantasy-GM" in skill
    assert "charter" in skill
    assert "decision log" in skill
    assert "never in command-center" in skill


def test_domain_afl_matches_funnel_options():
    assert "Domain AFL" in _skill()
    assert "AFL" in funnel.DOMAIN_OPTIONS


def test_no_secrets_bullet():
    skill = _skill()
    assert "No secrets in the cloud" in skill
    assert "FantasyPros key" in skill


def test_skill_carries_no_secret_material():
    skill = _skill()
    for pattern in (r"ghp_", r"gho_", r"\bsk-", r"x-api-key",
                    r"PRIVATE KEY", r"api[_-]?key\s*=",
                    r"token\s*=", r"password\s*="):
        assert re.search(pattern, skill) is None, pattern


def test_amended_fallback_markers_absent():
    skill = _skill()
    assert "command-center-routing" not in skill
    assert "overseer-filing" not in skill
    assert "#2512" not in skill
    assert "Nothing about filing" not in skill
