"""Every key `funnel brief` emits must be documented in the /funnel skill.

The defect this guards against is a producer/consumer split: #147 added
`human_steps` to the brief and nothing taught `skills/funnel/SKILL.md` to render
it, so the data was correct and invisible for as long as anyone looked. That
mattered because #141 had just removed the only other way a human-step ticket
surfaced — being handed to an engineer run that wedged on it.

Seven keys were undocumented when this test was written, not one, so the split
is structural rather than a single oversight.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402

SKILL = pathlib.Path(__file__).resolve().parent.parent / "skills" / "funnel" / "SKILL.md"


def brief_keys() -> set:
    """The brief's top-level keys, read from the source rather than a live run.

    A live `funnel brief` needs GitHub and would make this test a network test.
    """
    source = (pathlib.Path(funnel.__file__)).read_text()
    start = source.index('"total_needing_nate"')
    depth, i = 1, source.index("{", source.rindex("{", 0, start))
    # Walk the dict literal that `cmd_brief` builds, collecting its keys.
    keys, j = set(), i + 1
    while depth and j < len(source):
        ch = source[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == '"' and depth == 1:
            end = source.index('"', j + 1)
            token = source[j + 1:end]
            after = source[end + 1:end + 2]
            if after == ":":
                keys.add(token)
            j = end
        j += 1
    return keys


def test_every_brief_key_is_documented_in_the_skill():
    documented = SKILL.read_text()
    missing = sorted(
        key for key in brief_keys()
        if "`{}`".format(key) not in documented
    )
    assert not missing, (
        "these keys are emitted by `funnel brief` and never mentioned in "
        "skills/funnel/SKILL.md, so nothing renders them:\n  "
        + "\n  ".join(missing)
        + "\n\nDocument each in the field table and say when to surface it."
    )


def test_the_keys_were_actually_found():
    """A parser that silently found nothing would make the guard vacuous."""
    keys = brief_keys()
    assert "human_steps" in keys
    assert "total_needing_nate" in keys
    assert len(keys) > 10
