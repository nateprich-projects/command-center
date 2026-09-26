"""Routine prompts keep the unattended shaping boundary explicit."""

from __future__ import annotations

import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent

ROUTINES = ("claude", "zcode")


@pytest.mark.parametrize("routine", ROUTINES)
def test_finish_uses_the_run_id_from_this_runs_begin(routine):
    """Routine retries must not reuse a prior begin id in the same session."""
    body = (ROOT / "routines" / (routine + ".md")).read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "pass the run id printed by this run's `begin` output as `--run <id>`" in normalized
    assert "never an id from an earlier `begin` in the same session" in normalized
    assert "if `heartbeat finish` refuses a run/work mismatch" in normalized
    assert "use that id in `--run` and retry" in normalized
    assert "never wrap the id in `run=$(...)`" in normalized


@pytest.mark.parametrize(
    ("routine", "tier_phrase"),
    (("zcode", "standard-tier idea"), ("claude", "escalated idea")),
)
def test_shaping_is_the_third_ordered_job_with_a_safe_unattended_boundary(
    routine, tier_phrase
):
    body = (ROOT / "routines" / (routine + ".md")).read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "review, then breakdown, then shaping" in normalized
    assert tier_phrase in normalized
    assert "do not grill" in normalized
    assert "settle what precedent covers" in normalized
    assert "cite the source" in normalized
    assert "needs nate" in normalized
    assert "shaped is not approval" in normalized
    # Muse runs with --disable-write and pipes the plan instead (#366).
    assert ("funnel.py shaped <ref> --plan <file>" in normalized
            or "funnel.py shaped <ref> --plan -" in normalized)


def test_needs_guidance_keeps_null_categories_out_of_prose():
    """Canonical fields carry an all-clear state without boilerplate prose."""
    # skills/shape holds only the decision-record rules since #814; the
    # routines keep the protocol until their own runner is deleted (#807
    # removed routines/muse.md; routines/claude.md follows with its lane).
    documents = (
        ROOT / "routines" / "claude.md",
    )

    for path in documents:
        normalized = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert "actual open questions in `needs nate`" in normalized
        assert "null categories stay out of prose" in normalized
        assert "produce `needs: none`" in normalized
        assert "a self-approvable class with `agent` origin" in normalized
        assert "a `self-approved:` marker that `funnel brief` shows" in normalized
        assert "stays at `shaped`, with the reason printed" in normalized


def test_unattended_shaping_can_recover_an_unclassed_agent_idea():
    body = (ROOT / "routines" / "claude.md").read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "capture origin is `agent`" in normalized
    assert "--class <broken|maintenance|improve|new|replace>" in normalized
    assert "proposed class:" in normalized


def test_muse_shape_uses_the_issue_thread_to_correct_factual_premises():
    body = (ROOT / "routines" / "muse-shape.md").read_text(
        encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "read all `issue_thread` comments" in normalized
    assert "their reasoning and `idea.body` corrections supersede premise labels" \
        in normalized
    assert "do not repeat falsified mechanisms" in normalized
    assert "put them in `rejected` and cite the falsification" in normalized


@pytest.mark.parametrize("routine", ("claude", "zcode"))
def test_every_capture_line_names_its_repo(routine):
    """With two member repos, a capture without --repo refuses and the
    observation is lost (#668). The resolver defaults from the run's binding;
    the routine passes the flag anyway."""
    body = (ROOT / "routines" / (routine + ".md")).read_text(encoding="utf-8")
    lines = [l for l in body.splitlines() if "funnel.py capture" in l]
    assert lines
    assert all("--repo" in l for l in lines)


def test_no_runner_or_routine_invokes_a_live_brief():
    """Only the publisher runs ``brief``; runners and routines read the
    published snapshot instead (#824). Prose may still name the brief as
    an artifact — ``Do not run `funnel brief``` and ```funnel brief` shows``
    — but no script may invoke it."""
    offenders = []
    for directory in ("scripts", "routines"):
        for path in sorted((ROOT / directory).iterdir()):
            if not path.is_file():
                continue
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "funnel.py brief" in line:
                    offenders.append(
                        "{}:{}: {}".format(path.name, number, line.strip())
                    )
    assert not offenders, (
        "these runner/routine lines invoke a live brief; read the published "
        "snapshot instead:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("routine", ("claude", "zcode"))
def test_breakdown_reads_awaiting_breakdown_from_the_snapshot(routine):
    """The routine breakdown path lists its work from the published
    snapshot's brief section, never from a live brief (#824)."""
    body = (ROOT / "routines" / (routine + ".md")).read_text(encoding="utf-8")
    assert "funnel.py snapshot" in body
    assert ".brief.awaiting_breakdown" in body
