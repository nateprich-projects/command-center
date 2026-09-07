"""Muse is the first pool with no budget to read, and the first run headlessly.

Both are exceptions to rules written as absolutes, so both need a test that
fails if someone later "tidies" them away.

`AGENTS.md` says missing usage data fails closed — a run that cannot read its
budget does not work. Meta exposes no usage at all, so that rule would refuse
Muse for ever. The distinction the code has to keep is between **could not
read** the budget, which still fails closed, and **there is no budget to read**,
which proceeds knowingly. Collapse those two and either Muse never runs, or a
genuinely broken reader silently waves work through.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import usage  # noqa: E402

NOW = 1_788_800_000.0


def test_the_two_registries_agree_about_muse():
    """`heartbeat.py` and `usage.py` each keep a provider map, and a routine
    that is registered in one but not the other fails halfway through its own
    opening — `funnel begin` accepts the agent, then heartbeat rejects it."""
    assert heartbeat.PROVIDERS["muse"] == "meta"
    assert usage.PROVIDERS["muse"] == "meta"
    assert heartbeat.HARNESSES["muse"] == "muse-code"


def test_an_unmetered_pool_is_not_the_same_as_an_unreadable_one():
    """The whole exception rests on this distinction. `read_agent` returns a
    reading for Muse — so the run proceeds — and `None` for an agent whose
    provider has no reader, which still fails closed."""
    reading = usage.read_agent("muse", NOW)
    assert reading is not None
    assert reading["unmetered"] is True
    assert reading["windows"] == {}

    # An agent nobody registered has no pool: a configuration error, not an
    # empty budget.
    assert usage.read_agent("nonesuch", NOW) is None


def test_an_unmetered_reading_is_never_over_pace():
    """There are no windows, so there is nothing to be over. If this ever
    returns True the reviewer stops for ever and the failure looks like a
    budget problem rather than a code one."""
    verdict = usage.pace(usage.read_agent("muse", NOW), NOW, provider="meta")
    assert verdict["over_pace"] is False
    assert verdict["windows"] == []


def test_a_registered_provider_without_a_reader_still_fails_closed():
    """The exception is scoped to providers that expose nothing, not to every
    provider that has not been wired up yet. Adding a name to `PROVIDERS`
    must not quietly buy an exemption from the budget gate."""
    assert "meta" in usage.UNMETERED_PROVIDERS
    assert "anthropic" not in usage.UNMETERED_PROVIDERS
    assert "openai" not in usage.UNMETERED_PROVIDERS
    assert "zai" not in usage.UNMETERED_PROVIDERS


def test_muse_model_detection_reads_snapshots_not_jsonl():
    """Every other harness writes JSONL; Muse writes whole-file JSON snapshots,
    and its `HEAD.json` carries no model at all. A glob that picks up HEAD
    reports no model and the routing dataset silently loses its most expensive
    lane."""
    source = heartbeat.MODEL_SOURCES["muse"]
    assert source.endswith("snapshot-*.json"), source
    assert "HEAD" not in source


def test_the_runner_uses_the_model_without_the_data_sharing_notice():
    """`muse-spark-1.3-contributor` is the CLI default and carries "Your
    content ... may be used for product improvement". This repository is
    private and privacy is why Muse was chosen over z.ai, so the runner must
    ask for the plain model. A silent revert to the default would be invisible
    in behaviour and wrong in substance."""
    runner = (ROOT / "scripts" / "muse-review").read_text()
    body = "\n".join(l for l in runner.splitlines() if not l.strip().startswith("#"))
    assert "--model muse-spark-1.3" in body
    assert "contributor" not in body


def test_the_runner_opens_the_network_sandbox():
    """The sandbox defaults to `proxy-only`, under which `gh` fails with
    `context deadline exceeded` after about a minute and reports the PR as
    unreadable. The failure is silent and looks like GitHub being down."""
    runner = (ROOT / "scripts" / "muse-review").read_text()
    body = "\n".join(l for l in runner.splitlines() if not l.strip().startswith("#"))
    assert "--sandbox-network enabled" in body
    assert "--disable-write" in body
    # A reviewer *is* shell -- gh, funnel review, heartbeat finish. Disabling it
    # produces a run that cannot take a step.
    assert "--disable-shell" not in body


def test_the_prompt_is_the_routine_file_not_a_copy():
    """zcode's prompt lives in an app UI, hand-pasted, and drifted silently for
    75 minutes (#52). Muse reads `routines/muse.md` at run time, so there is no
    second copy to fall out of step. If this ever becomes an inline string the
    whole drift surface comes back."""
    runner = (ROOT / "scripts" / "muse-review").read_text()
    assert "routines/muse.md" in runner
    routine = (ROOT / "routines" / "muse.md").read_text()
    assert "\n---\n" in routine, "the runner splits the prompt on the --- separator"
    prompt = routine.split("\n---\n", 1)[1]
    assert "funnel.py begin --agent muse --tier escalated" in prompt
