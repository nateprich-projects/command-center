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


def test_the_template_renders_an_accept_hold_as_held_not_as_a_decision():
    """#1725: a held finished project reads as held at Accept by Nate, with
    what lifts the hold and why, and is never counted as waiting on him."""
    template = funnel_render.render_template()
    section = template.split("Then `held_at_accept`", 1)[1].split("\n\n", 1)[0]
    required = (
        "held at Accept by Nate",
        "`condition`",
        "`reason`",
        "`blocked`",
        "`total_needing_nate`",
    )
    missing = [phrase for phrase in required if phrase not in section]
    assert not missing, (
        "the held_at_accept rendering lost: " + ", ".join(missing)
    )


def test_the_skill_invokes_the_code_template():
    skill = SKILL.read_text(encoding="utf-8")
    assert "funnel_render.py" in skill


def test_the_template_prints():
    assert funnel_render.main([]) == 0
    assert funnel_render.main(["--bogus"]) == 2


def test_skill_and_template_offer_to_work_the_top_item_in_session():
    """Both say the same thing: the session offers to work the top item
    itself rather than handing Nate a launch line to run (#1901)."""
    skill = " ".join(SKILL.read_text(encoding="utf-8").split())
    template = " ".join(funnel_render.render_template().split())
    offer = ("Offer to work the top item yourself, in this session or as a "
             "background task, and start once he says yes.")
    for text in (skill, template):
        assert offer in text
        assert "Do not run it" not in text
