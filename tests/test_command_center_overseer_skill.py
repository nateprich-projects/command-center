"""The command-center overseer skill (#2441, parent #2418).

The skill is the daily routine's instructions, committed in the attached
repository because cloud threads see no ``~/.claude``. These tests prove
the ticket's Accept: the skill names the five-phase loop, the record
path, and the amended filing fallback (REST in the attached repo with a
routing block, #2512 — probe 2 failed, so nothing about filing goes
through the Mac), and it carries no secret material.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402

SKILL = ROOT / "skills" / "command-center-overseer" / "SKILL.md"
LINKED = ROOT / ".claude" / "skills" / "command-center-overseer" / "SKILL.md"

ROUTING_MARKER = "<!-- command-center-routing -->"

#: Secret material, not secret vocabulary: the skill must be able to say
#: "no secrets", "token" and "FantasyPros key" while carrying no value.
SECRET_PATTERNS = (
    r"ghp_[A-Za-z0-9]{8,}",
    r"github_pat_[A-Za-z0-9_]{8,}",
    r"gho_[A-Za-z0-9]{8,}",
    r"sk-(?:proj-)?[A-Za-z0-9]{8,}",
    r"AKIA[0-9A-Z]{16}",
    r"xox[bpas]-[A-Za-z0-9-]+",
    r"BEGIN [A-Z ]*PRIVATE KEY",
    r"(?i)api[_-]?key\s*[:=]\s*[\"']?[A-Za-z0-9_./+\-]{12,}",
    r"(?i)\bsecret\b\s*[:=]\s*[\"']?[^\"'\s]{8,}",
    r"(?i)\btoken\b\s*[:=]\s*[\"']?[A-Za-z0-9_.\-]{12,}",
    r"Bearer\s+[A-Za-z0-9_.\-~+/]{12,}={0,2}",
    r"(?i)fantasypros.{0,30}\bkey\b\s*[:=]\s*\S+",
)


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_file_exists_with_frontmatter_and_link():
    assert SKILL.is_file(), "skills/command-center-overseer/SKILL.md is missing"
    skill = _skill()
    assert skill.startswith("---\nname: command-center-overseer\n"), (
        "the skill must carry the repo's name frontmatter"
    )
    assert "\ndescription: " in skill.split("---", 2)[1], (
        "the skill must carry a description frontmatter line"
    )
    assert LINKED.is_file(), (
        ".claude/skills/command-center-overseer must link the skill, "
        "as the other skills do"
    )
    assert LINKED.read_text(encoding="utf-8") == skill


def test_five_phase_loop_in_order_with_a_daily_step():
    skill = _skill()
    phases = ["Curate", "Describe", "Hypothesize", "Test", "Implement"]
    positions = [skill.index(phase) for phase in phases]
    assert positions == sorted(positions), (
        "the five phases must read as a loop in order: " + ", ".join(phases)
    )
    assert "one phase step" in skill
    assert "daily" in skill.lower()


def test_record_path_and_record_kinds_are_named():
    skill = _skill()
    assert "domains/command-center/" in skill
    for record in (
        "charter.md",
        "data-sources.md",
        "measures/",
        "questions.md",
        "hypotheses.md",
        "findings/",
        "decision-log.md",
    ):
        assert record in skill, "record kind missing: {}".format(record)
    assert "Records hold knowledge; work status stays on issues." in skill


def test_routing_block_parses_with_live_option_values():
    skill = _skill()
    assert ROUTING_MARKER in skill
    rest = skill.split(ROUTING_MARKER, 1)[1]
    fenced = re.search(r"```json\s*\n(.*?)(\r?\n```)", rest, re.DOTALL)
    assert fenced, "no fenced ```json block follows the routing marker"
    block = json.loads(fenced.group(1))
    assert set(block) == {"Risk", "Needs", "Domain"}, (
        "the routing block must carry exactly Risk, Needs and Domain"
    )
    assert block["Risk"] in funnel.RISK_OPTIONS
    assert block["Needs"] in funnel.NEEDS_OPTIONS
    assert block["Domain"] == "command-center"
    assert block["Domain"] in funnel.DOMAIN_OPTIONS


def test_filing_fallback_is_rest_plus_workflow_not_the_mac():
    skill = " ".join(_skill().split())
    for phrase in (
        "repo-scoped REST",
        "overseer-filing",
        "Project 2",
        "Status Ideas, Origin agent, Risk, Needs and Domain",
        "Routing is never defaulted",
        "Nothing about filing goes through the Mac",
        "there is no Mac handoff",
        "no dispatch is needed",
    ):
        assert phrase in skill, "filing fallback missing: {}".format(phrase)
    for option in funnel.DOMAIN_OPTIONS:
        assert "`{}`".format(option) in skill, (
            "the skill must name the live Domain option {}".format(option)
        )


def test_hypotheses_score_through_shared_code():
    skill = _skill()
    for phrase in (
        "hypothesis_scorer.py",
        "score_hypothesis",
        "min(p, 1-p)",
        "try and watch",
        "Record every estimate with its reason",
    ):
        assert phrase in skill, "hypothesis scoring missing: {}".format(phrase)


def test_read_only_data_and_python_floor_are_stated():
    skill = _skill()
    assert "No secrets in the cloud" in skill
    assert "never call" in skill and "FantasyPros" in skill
    assert "3.12" in skill
    assert "3.13" in skill


def test_skill_carries_no_secret_material():
    skill = _skill()
    offenders = []
    for pattern in SECRET_PATTERNS:
        for match in re.finditer(pattern, skill):
            offenders.append("{}: {}".format(pattern, match.group(0)[:40]))
    assert not offenders, (
        "the skill carries secret material:\n  " + "\n  ".join(offenders)
    )


def test_secret_patterns_catch_realistic_values():
    """A grep that matches nothing would make the guard vacuous."""
    probe = " ".join([
        "ghp_" + "A" * 22,
        "github_pat_" + "B" * 22,
        "gho_" + "C" * 22,
        "sk-proj-" + "D" * 22,
        "AKIA" + "E" * 16,
        "xoxb-12345-abcdef",
        "-----BEGIN RSA PRIVATE KEY-----",
        "api_key: " + "F" * 14,
        "secret=" + "G" * 12,
        "token=" + "H" * 14,
        "Bearer " + "I" * 16,
        "fantasypros key=whatever",
    ])
    for pattern in SECRET_PATTERNS:
        assert re.search(pattern, probe), (
            "pattern no longer matches its probe: {}".format(pattern)
        )
