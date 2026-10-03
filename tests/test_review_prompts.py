"""Versioned reviewer prompt variants remain bounded and restorable."""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import review_prompts  # noqa: E402


def _variant_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "routines").mkdir(parents=True)
    shutil.copy2(ROOT / "routines" / "muse-review.md",
                 repo / "routines" / "muse-review.md")
    shutil.copytree(
        ROOT / "routines" / "muse-review-variants",
        repo / "routines" / "muse-review-variants",
    )
    return repo


def test_rounds_are_four_first_round_variants_and_one_round_two_winner():
    manifest = json.loads(
        (ROOT / "routines" / "muse-review-variants" / "manifest.json")
        .read_text())

    assert manifest["rounds"] == [
        {"name": "round-1", "variants": ["baseline", "r1", "r2", "r4"]},
        {"name": "round-2",
         "variants": ["baseline", "best_round_1_single"]},
    ]
    assert manifest["round_2_selection"]["candidates"] == ["r1", "r2", "r4"]
    assert manifest["round_2_selection"]["max_selected"] == 1
    assert manifest["active_variant"] == "baseline"
    assert manifest["trial_enabled"] is False


def test_baseline_prompt_is_byte_identical_to_the_pretrial_prompt():
    prompt = review_prompts.load_active_prompt(ROOT)

    assert hashlib.sha256(prompt.routine.encode()).hexdigest() == (
        "455cbbeaf7415cd028be865328c2a4888260b729c46ff80484fe5dd5faf87b04"
    )


@pytest.mark.parametrize(
    "name,question,judge,absent",
    [
        (
            "r1",
            "If `evidence` says the diff rewrites fix #N, confirm it still prevents that fix's failure; cite its test.",
            "If `evidence` has a `rewrites prior fix: #N` line, confirm the diff still prevents that fix's failure and cite its test; mark unmet each assigned requirement it bears on when it does not.",
            "test_weakening",
        ),
        (
            "r2",
            "A `test_weakening` entry no ticket or Departure authorises is blocking.",
            "A deleted, skipped or weakened test in `test_weakening` that no ticket or Departure authorises is blocking: mark unmet each assigned requirement it bears on.",
            "rewrites fix #N",
        ),
        (
            "r4",
            "So is `passes-on-base` on any other ticket.",
            "On any other ticket `reproduction: passes-on-base` is weighed, not blocking.",
            "test_weakening",
        ),
    ],
)
def test_each_variant_carries_only_its_recovered_rule(name, question, judge,
                                                       absent):
    prompt = review_prompts.render_variant(ROOT, name)

    assert "- " + question in prompt.routine
    assert prompt.judge_rules == (judge,)
    assert absent not in prompt.routine
    assert "<!-- REVIEW_VARIANT_RULES -->" not in prompt.routine
    assert prompt.routine.count("PACKET_JSON") == 1


def test_active_trial_variant_fails_closed_until_trial_wiring_is_enabled(
        tmp_path):
    repo = _variant_repo(tmp_path)
    manifest_path = repo / "routines" / "muse-review-variants" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["active_variant"] = "r1"
    manifest["trial_enabled"] = False
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(review_prompts.ReviewPromptError,
                       match="cannot be active before trial wiring"):
        review_prompts.load_active_prompt(repo)


def test_restore_baseline_disables_trial_then_replays_both_known_verdicts(
        tmp_path):
    repo = _variant_repo(tmp_path)
    manifest_path = repo / "routines" / "muse-review-variants" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["active_variant"] = "r2"
    manifest["trial_enabled"] = True
    manifest_path.write_text(json.dumps(manifest))
    calls = []

    def replay(packet, routine, **kwargs):
        calls.append((packet, routine, kwargs))
        verdict = kwargs["expected"]
        return {"verdicts": [verdict] * kwargs["runs"], "pass": True}

    result = review_prompts.restore_baseline(
        "private/must-reject.json", "private/must-approve.json",
        repo=repo, replay_fn=replay,
    )

    restored = json.loads(manifest_path.read_text())
    assert restored["active_variant"] == "baseline"
    assert restored["trial_enabled"] is False
    assert [(packet, kwargs["expected"], kwargs["runs"])
            for packet, _routine, kwargs in calls] == [
                ("private/must-reject.json", "rejected", 3),
                ("private/must-approve.json", "approved", 3),
            ]
    assert all(routine is None for _packet, routine, _kwargs in calls)
    assert all(kwargs["runtime_root"] == review_prompts.RUNTIME_ROOT
               for _packet, _routine, kwargs in calls)
    assert result["active_variant"] == "baseline"
    assert result["pass"] is True
    assert result["replays"] == [
        {"packet": "must-reject", "runs": 3, "expected": "rejected",
         "verdicts": ["rejected"] * 3, "pass": True},
        {"packet": "must-approve", "runs": 3, "expected": "approved",
         "verdicts": ["approved"] * 3, "pass": True},
    ]
