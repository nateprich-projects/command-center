#!/usr/bin/env python3
"""muse_model.py — which Muse model carries which repository, and what it costs.

Muse's model catalog marks ``muse-spark-1.3-contributor`` as ``is_default:
true``, so a ``muse exec`` that omits ``--model`` resolves to Meta's
Discounted Services tier, whose catalog row reads "Your content, including
inter-session messages, may be used for product improvement." The unsafe
value is the one a caller gets by saying nothing. Every caller here names
its model, and this module is where the name comes from.

    muse_model.py model --repo nateprich-projects/The-League

Routing and pricing are two views of one fact — which model billed this
work — so both live here. An exposure rule with a second copy drifts, and a
stale copy of this one submits confidential code to a training tier.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, Optional

#: Meta's Discounted Services model. Content sent here is eligible for
#: product improvement, so only repositories on the allowlist may use it.
CONTRIBUTOR_MODEL = "muse-spark-1.3-contributor"

#: The private model. Every repository not named below resolves to this,
#: including one this module has never heard of.
STANDARD_MODEL = "muse-spark-1.3"

#: The repositories Nate cleared for the contributor model on 2026-09-22,
#: by bare name. `command-center` is public; `FF-Weekly-Start-Sit` and
#: `The-League` are private, and routing them here is his deliberate
#: override of the FAIL recorded in docs/meta-model-api-tos-aup-1095.md.
#: That document stands as written and is not to be edited to agree.
#:
#: Membership is exact. A near-miss spelling is not a member, because the
#: failure it would otherwise cause cannot be withdrawn.
CONTRIBUTOR_REPOS = frozenset({
    "command-center",
    "FF-Weekly-Start-Sit",
    "The-League",
})

#: Per-million-token rates by model id: input, cached input, output. The
#: two cards are not a flat multiple — contributor discounts a cache read
#: to 2% of a fresh token where standard discounts to 12% — so a scaling
#: factor cannot stand in for two cards on this cache-heavy workload.
#: Standard confirmed by Nate 2026-09-20; contributor from the same card
#: comparison recorded in usage.py and LEARNINGS.md.
RATE_CARDS: Dict[str, Dict[str, float]] = {
    CONTRIBUTOR_MODEL: {"input": 0.10, "cached_input": 0.002, "output": 0.20},
    STANDARD_MODEL: {"input": 1.25, "cached_input": 0.15, "output": 4.25},
}


def repo_name(repo: Optional[str]) -> str:
    """The bare repository name from either `owner/name` or `name`.

    The runners get `owner/name` from ``funnel.py begin`` while a human
    or a test says `name`; both must resolve the same way. Anything that
    is not a string is not a repository, and returns the empty string so
    the caller lands on the standard model rather than raising inside a
    lane that is mid-ticket.
    """
    if not isinstance(repo, str):
        return ""
    return repo.strip().rsplit("/", 1)[-1]


def model_for(repo: Optional[str]) -> str:
    """The model id that may carry work from ``repo``.

    Fails safe in the only direction that matters: anything not named
    exactly on the allowlist — an unknown repository, a near-miss
    spelling, an empty string, ``None`` — gets the private model. A
    repository nobody has cleared is therefore safe by default, and
    adding one is a deliberate edit here rather than an accident
    somewhere else.
    """
    return CONTRIBUTOR_MODEL if repo_name(repo) in CONTRIBUTOR_REPOS \
        else STANDARD_MODEL


def rate_card(model: Optional[str]) -> Dict[str, float]:
    """The rate card for ``model``, defaulting to the standard card.

    An unrecognised or missing model id prices at the dearer card. The
    only consumer is a spending gate, where over-reading stops the lanes
    early and under-reading walks them into the provider's refusal, so
    the conservative direction is the expensive one.
    """
    if isinstance(model, str) and model in RATE_CARDS:
        return RATE_CARDS[model]
    return RATE_CARDS[STANDARD_MODEL]


def main(argv: Optional[list] = None) -> int:
    """Print one model id, for the bash runners to put in their argv."""
    parser = argparse.ArgumentParser(
        prog="muse_model.py",
        description="which Muse model carries which repository")
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model", help="print the model id for a repo")
    model.add_argument("--repo", required=True,
                       help="owner/name, or the bare repository name")
    args = parser.parse_args(argv)
    if args.command == "model":
        sys.stdout.write(model_for(args.repo) + "\n")
        return 0
    return 2  # pragma: no cover - argparse rejects an unknown subcommand


if __name__ == "__main__":
    raise SystemExit(main())
