"""Routine prompts keep the unattended shaping boundary explicit."""

from __future__ import annotations

import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent

ROUTINES = ("codex-work", "claude", "muse", "muse-implement", "zcode")


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


def test_muse_step_one_uses_the_runner_opening_result():
    body = (ROOT / "routines" / "muse.md").read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "the runner has already run the opening command before handing you this prompt" in normalized
    assert "its json result is inserted into the code block below" in normalized
    assert "do not run `begin` again" in normalized
    assert "if it says `\"do\": \"stop\"`, the runner has finished the heartbeat" in normalized


def test_muse_implement_carries_the_writer_contract():
    body = (ROOT / "routines" / "muse-implement.md").read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "work **one escalated-tier ticket**, then stop" in normalized
    assert "funnel.py begin --agent muse --tier escalated" in normalized
    assert "the runner has already run this exact opening command" in normalized
    assert "do not run `begin` again" in normalized
    assert "prior_run.py <issue-number>" in normalized
    assert "gh repo clone <repo-from-the-ticket> ." in normalized
    assert "branch **`ticket/<issue-number>` from `main`**" in normalized
    assert "commit and push after each meaningful step" in normalized
    assert "shell commands run in the background" in normalized
    assert "do not use `/tmp`, `$tmpdir`" in normalized
    assert "use `gh` for github" in normalized
    assert "open one pull request" in normalized
    assert "capture observed defects before finishing" in normalized
    assert "finished by comments:" in normalized
    assert "mid-work discovery: convert, record, stop" in normalized
    assert "never add an implementation mode to `muse-review`" in normalized
    assert "never add an inline second-agent literal" in normalized


def test_claude_reports_codex_automation_drift_without_gating_the_run():
    body = (ROOT / "routines" / "claude.md").read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert (
        "if [ -d \"$home/.codex/automations\" ]; then python3 "
        "/users/nateprich/.claude/command-center-run/scripts/"
        "sync_codex_automations.py --check || true fi"
    ) in normalized
    assert "reuses the sync script's existing detector" in normalized
    assert "report one short `codex automation drift:` diagnostic" in normalized
    assert "do not treat the check's nonzero drift exit as a failed run" in normalized
    assert "do not run write mode or edit the live automations" in normalized
    assert "if `~/.codex/automations` is absent" in normalized


@pytest.mark.parametrize(
    ("routine", "tier_phrase"),
    (("zcode", "standard-tier idea"), ("claude", "escalated idea"),
     ("muse", "standard-tier idea")),
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
    assert "needs you" in normalized
    assert "shaped is not approval" in normalized
    # Muse runs with --disable-write and pipes the plan instead (#366).
    assert ("funnel.py shaped <ref> --plan <file>" in normalized
            or "funnel.py shaped <ref> --plan -" in normalized)


def test_breakdown_docs_record_and_resume_needs_decisions():
    """An undecidable breakdown must become visible to Nate and resume from
    his answer, rather than repeatedly consuming Muse runs on the same plan."""
    documents = (
        ROOT / "skills" / "breakdown" / "SKILL.md",
        ROOT / "routines" / "muse.md",
    )
    for path in documents:
        normalized = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert (
            "python3 /users/nateprich/.claude/command-center-run/funnel.py "
            "comment <ref> --voice agent --needs-decision"
        ) in normalized
        assert "read the issue's comments" in normalized
        assert "an earlier `**needs a decision:**` header" in normalized
        assert "the answer that followed it" in normalized
        assert "act on that answer" in normalized
        assert "create no tickets" in normalized
        assert "--outcome done" in normalized or "finish `done`" in normalized
        assert "answer the breakdown's question?" in normalized
        assert "removes the `blocked` label" in normalized
        assert (
            "comment on the issue saying precisely what is undecided, and leave it"
            not in normalized
        )


def test_needs_guidance_uses_the_parser_ready_all_clear_form():
    """The shaping docs must teach the syntax accepted by shaped_plan_status."""
    example = " ".join(
        """- Exposure: nothing outstanding. No new credentials or reachable surface.
        - Gates: nothing outstanding. No gate ownership changes.
        - Scope and priority: nothing outstanding. The scoped change is documented.
        - Preference: nothing outstanding. No user-facing choice remains.""".split()
    ).lower()
    old_guidance = (
        "explicit answer under every category, including when nothing is outstanding"
    )
    documents = (
        ROOT / "routines" / "muse.md",
        ROOT / "routines" / "claude.md",
        ROOT / "skills" / "shape" / "SKILL.md",
    )

    for path in documents:
        normalized = " ".join(path.read_text(encoding="utf-8").split()).lower()
        assert example in normalized
        assert old_guidance not in normalized
        assert "a self-approvable class with `agent` origin" in normalized
        assert "a `self-approved:` marker that `funnel brief` shows" in normalized
        assert "stays at `shaped`, with the reason printed" in normalized


@pytest.mark.parametrize("routine", ["claude", "muse"])
def test_unattended_shaping_can_recover_an_unclassed_agent_idea(routine):
    body = (ROOT / "routines" / (routine + ".md")).read_text(encoding="utf-8")
    normalized = " ".join(body.split()).lower()

    assert "capture origin is `agent`" in normalized
    assert "--class <broken|maintenance|improve|new|replace>" in normalized
    assert "proposed class:" in normalized
