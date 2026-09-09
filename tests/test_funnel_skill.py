"""Keep the /funnel skill's brief contract in sync with funnel.py."""

from __future__ import annotations

import json
import pathlib
import re
import sys

from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
FIELD_ROW = re.compile(r"^\| `([^`]+)` \|")


def _brief_keys(monkeypatch, capsys):
    monkeypatch.setattr(funnel, "unattended_merges", lambda now: [])
    monkeypatch.setattr(funnel, "working_tree_touched", lambda now: [])

    assert funnel.cmd_brief([], NOW) == 0
    return set(json.loads(capsys.readouterr().out))


def _documented_fields():
    skill = (ROOT / "skills" / "funnel" / "SKILL.md").read_text()
    in_table = False
    fields = set()
    for line in skill.splitlines():
        if line == "## What the fields mean":
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        match = FIELD_ROW.match(line)
        if match:
            fields.add(match.group(1))
    return fields


def test_every_brief_field_is_named_in_the_funnel_skill(monkeypatch, capsys):
    undocumented = _brief_keys(monkeypatch, capsys) - _documented_fields()

    assert not undocumented, (
        "skills/funnel/SKILL.md is missing funnel brief fields: "
        + ", ".join(sorted(undocumented))
    )
