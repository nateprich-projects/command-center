#!/usr/bin/env python3
"""Load the versioned reviewer prompt variants from their shared source."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional, Sequence


CHECKOUT = pathlib.Path(__file__).resolve().parents[1]
# Outside routines/: the variants are data this loader owns, and the live
# routine stays byte-identical to main while the baseline is active.
VARIANT_DIR = pathlib.Path("engine") / "review_variants"
MANIFEST = VARIANT_DIR / "manifest.json"
BASE_ROUTINE = pathlib.Path("routines") / "muse-review.md"
#: A variant's question rules follow the routine's one ``evidence`` bullet,
#: the rule they qualify.
RULE_ANCHOR = "- `evidence`"
RUNTIME_ROOT = pathlib.Path(
    os.environ.get("COMMAND_CENTER_RUNTIME_ROOT",
                   str(pathlib.Path.home() / ".claude"))
)


class ReviewPromptError(ValueError):
    """A versioned reviewer prompt is malformed or cannot be selected."""


@dataclass(frozen=True)
class ReviewPrompt:
    variant: str
    routine: str
    judge_rules: tuple[str, ...]


def _read_json(path: pathlib.Path, description: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewPromptError(
            "cannot read the {} JSON".format(description)
        ) from exc
    if not isinstance(value, dict):
        raise ReviewPromptError("the {} JSON must be an object".format(
            description))
    return value


def _validate_manifest(value: dict) -> None:
    if value.get("schema_version") != 1:
        raise ReviewPromptError("unsupported reviewer-variant manifest")
    if not isinstance(value.get("trial_enabled"), bool):
        raise ReviewPromptError("trial_enabled must be a boolean")
    files = value.get("variant_files")
    if not isinstance(files, dict) or not files:
        raise ReviewPromptError("variant_files must be a non-empty object")
    active = value.get("active_variant")
    if not isinstance(active, str) or active not in files:
        raise ReviewPromptError("active_variant is not a known variant")

    rounds = value.get("rounds")
    expected_rounds = [
        {"name": "round-1", "variants": ["baseline", "r1", "r2", "r4"]},
        {"name": "round-2",
         "variants": ["baseline", "best_round_1_single"]},
    ]
    if rounds != expected_rounds:
        raise ReviewPromptError("the reviewer trial rounds are malformed")
    selection = value.get("round_2_selection")
    if selection != {
            "variant": "best_round_1_single",
            "candidates": ["r1", "r2", "r4"],
            "max_selected": 1,
            "evaluation": (
                "Use #2072's paired A+B method and inconclusive rules once "
                "established."
            ),
    }:
        raise ReviewPromptError("the Round 2 selection rule is malformed")
    restore = value.get("restore")
    if restore != {
            "variant": "baseline",
            "runs": 3,
            "replays": [
                {"name": "must-reject", "expected": "rejected"},
                {"name": "must-approve", "expected": "approved"},
            ],
    }:
        raise ReviewPromptError("the baseline restore checks are malformed")
    if files != {
            "baseline": "baseline.json",
            "r1": "r1.json",
            "r2": "r2.json",
            "r4": "r4.json",
    }:
        raise ReviewPromptError("the reviewer variant set is malformed")


def _manifest(repo: str | pathlib.Path = CHECKOUT) -> tuple[pathlib.Path, dict]:
    root = pathlib.Path(repo)
    path = root / MANIFEST
    value = _read_json(path, "reviewer-variant manifest")
    _validate_manifest(value)
    return root, value


def _variant(repo: pathlib.Path, manifest: dict, name: str) -> dict:
    filename = manifest["variant_files"].get(name)
    if not isinstance(filename, str):
        raise ReviewPromptError("unknown reviewer prompt variant")
    relative = pathlib.Path(filename)
    if relative.is_absolute() or len(relative.parts) != 1:
        raise ReviewPromptError("variant file must stay in its versioned directory")
    value = _read_json(repo / VARIANT_DIR / relative,
                       "reviewer prompt variant")
    if value.get("id") != name:
        raise ReviewPromptError("reviewer variant id does not match its file")
    for field in ("question_rules", "judge_rules"):
        rules = value.get(field)
        if not isinstance(rules, list) or any(
                not isinstance(rule, str) or not rule.strip()
                for rule in rules):
            raise ReviewPromptError(
                "{} must be a list of non-empty strings".format(field)
            )
    return value


def render_variant(repo: str | pathlib.Path, name: str) -> ReviewPrompt:
    """Render a prepared variant over the frozen baseline routine."""
    root, manifest = _manifest(repo)
    variant = _variant(root, manifest, name)
    routine_path = root / BASE_ROUTINE
    try:
        source = routine_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReviewPromptError(
            "cannot read {} — refusing to run without a prompt".format(
                BASE_ROUTINE)
        ) from exc
    parts = source.split("\n---\n", 1)
    if len(parts) != 2:
        raise ReviewPromptError(
            "the reviewer routine has no prompt after its --- separator"
        )
    if parts[1].count("PACKET_JSON") != 1:
        raise ReviewPromptError(
            "the reviewer routine must hold PACKET_JSON exactly once"
        )
    rendered = source
    if variant["question_rules"]:
        lines = source.splitlines(keepends=True)
        anchors = [index for index, line in enumerate(lines)
                   if line.startswith(RULE_ANCHOR)]
        if len(anchors) != 1:
            raise ReviewPromptError(
                "the reviewer routine must hold one `evidence` bullet"
            )
        bullets = ["- {}\n".format(rule)
                   for rule in variant["question_rules"]]
        index = anchors[0] + 1
        rendered = "".join(lines[:index] + bullets + lines[index:])
    template = rendered.split("\n---\n", 1)[1]
    if template.count("PACKET_JSON") != 1:
        raise ReviewPromptError(
            "the reviewer routine must hold PACKET_JSON exactly once"
        )
    return ReviewPrompt(
        variant=name,
        routine=template,
        judge_rules=tuple(variant["judge_rules"]),
    )


def load_active_prompt(repo: str | pathlib.Path = CHECKOUT) -> ReviewPrompt:
    """Load the versioned active prompt, refusing pre-gate trial selection."""
    root, manifest = _manifest(repo)
    active = manifest["active_variant"]
    if active != "baseline" and not manifest["trial_enabled"]:
        raise ReviewPromptError(
            "a reviewer trial variant cannot be active before trial wiring is enabled"
        )
    return render_variant(root, active)


def _write_manifest(repo: pathlib.Path, value: dict) -> None:
    path = repo / MANIFEST
    if path.is_symlink():
        raise ReviewPromptError("refusing to replace a symlinked variant manifest")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=".manifest-", suffix=".tmp", delete=False) as handle:
            temporary = pathlib.Path(handle.name)
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.chmod(temporary, mode)
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        raise ReviewPromptError("could not restore the variant manifest") from exc


def restore_baseline(
        must_reject_packet: str | pathlib.Path,
        must_approve_packet: str | pathlib.Path,
        *,
        repo: str | pathlib.Path = CHECKOUT,
        runtime_root: str | pathlib.Path = RUNTIME_ROOT,
        replay_fn: Optional[Callable[..., dict]] = None) -> dict:
    """Restore the baseline selector, then replay both known verdict packets."""
    root, manifest = _manifest(repo)
    if replay_fn is None and root != CHECKOUT:
        raise ReviewPromptError(
            "a non-checkout restore requires an explicit replay function"
        )
    manifest["active_variant"] = "baseline"
    manifest["trial_enabled"] = False
    _write_manifest(root, manifest)

    if replay_fn is None:
        from engine.replay import replay as replay_fn

    restore = manifest["restore"]
    packets = [
        (restore["replays"][0]["name"], must_reject_packet,
         restore["replays"][0]["expected"]),
        (restore["replays"][1]["name"], must_approve_packet,
         restore["replays"][1]["expected"]),
    ]
    results = []
    for name, packet, expected in packets:
        summary = replay_fn(
            packet, None, runs=restore["runs"], expected=expected,
            runtime_root=runtime_root,
        )
        results.append({
            "packet": name,
            "runs": restore["runs"],
            "expected": expected,
            "verdicts": summary.get("verdicts", []),
            "pass": summary.get("pass") is True,
        })
    return {
        "active_variant": "baseline",
        "trial_enabled": False,
        "replays": results,
        "pass": all(item["pass"] for item in results),
    }


def _active_json(repo: str | pathlib.Path = CHECKOUT) -> str:
    prompt = load_active_prompt(repo)
    return json.dumps({
        "variant": prompt.variant,
        "routine": prompt.routine,
        "judge_rules": list(prompt.judge_rules),
    }, separators=(",", ":"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load the active reviewer prompt or restore its baseline"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("active", help="print the active prompt for the engine")
    restore = subparsers.add_parser(
        "restore", help="restore baseline and replay the known verdict packets"
    )
    restore.add_argument("--must-reject", required=True)
    restore.add_argument("--must-approve", required=True)
    restore.add_argument("--runtime-root", default=str(RUNTIME_ROOT),
                         help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "active":
            print(_active_json())
            return 0
        result = restore_baseline(
            args.must_reject, args.must_approve,
            runtime_root=args.runtime_root,
        )
    except (ReviewPromptError, ValueError, OSError) as exc:
        print("review-variant: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
