"""The code rendering template stays in sync with what the brief emits.

#147 added `human_steps` to the brief while nothing taught the skill to
render it. Ticket #825 moved the rendering section into
`funnel_render.py`; this test guards the same producer/consumer split from
the code side, and pins that the skill actually invokes the template.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel_render  # noqa: E402
from test_funnel_skill_documents_brief import brief_keys  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "funnel" / "SKILL.md"


def test_the_template_names_every_brief_key():
    template = funnel_render.render_template()
    missing = sorted(
        key for key in brief_keys()
        if "`{}`".format(key) not in template
    )
    assert not missing, (
        "these brief keys have no rendering wording in funnel_render.py, "
        "so the template cannot render them:\n  " + "\n  ".join(missing)
    )


def test_the_template_keeps_the_observer_rendering():
    template = funnel_render.render_template()
    required = (
        "`working_tree_touched`",
        "observers",
        "one observer",
        "multiple observers",
        "dirty-only",
        "`human_steps`",
        "`machine_local_steps`",
        "`self_reviewed: true`",
    )
    missing = [phrase for phrase in required if phrase not in template]
    assert not missing, (
        "the code template dropped rendering detail: "
        + ", ".join(missing)
    )


def test_the_skill_invokes_the_code_template():
    skill = SKILL.read_text(encoding="utf-8")
    assert "funnel_render.py" in skill


def test_the_template_prints():
    assert funnel_render.main([]) == 0
    assert funnel_render.main(["--bogus"]) == 2
