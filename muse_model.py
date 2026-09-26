#!/usr/bin/env python3
"""muse_model.py — which Muse model carries a repository, and what it costs.

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
from typing import Dict, Optional, Sequence, Tuple

#: Meta's Discounted Services model. Content sent here is eligible for
#: product improvement, so only repositories on the allowlist may use it.
CONTRIBUTOR_MODEL = "muse-spark-1.3-contributor"

#: The private model. Every repository not named below resolves to this,
#: including one this module has never heard of.
STANDARD_MODEL = "muse-spark-1.3"

#: The repositories cleared for the contributor model, by bare name: repo
#: tiers 1 and 3, never tier 2 (`funnel.REPO_TIERS`).
#:
#: Nate, 2026-09-26 (#1570): "So tier 1 and tier 3, but not tier 2." z.ai's
#: standard tier had used its week in about 46 hours, and Muse cannot carry
#: that run rate at the private model's price; the discount is the test of
#: whether it can. `command-center` is public. The other five are private,
#: and clearing them is his deliberate override of the §6.2 FAIL in
#: docs/meta-model-api-tos-aup-1095.md, as #1299 was on 2026-09-22 for the
#: two football repos before #1315 withdrew all three. `career-toolset` and
#: `jeffy-finance-agent` carry real-world data and stay private.
#:
#: Named here rather than derived from the tiers: a repository new to the
#: funnel is tier 3 by default, and it must not reach a training tier
#: before anyone decided it should. A test holds tier 2 off this list.
#:
#: Membership is exact. A near-miss spelling is not a member, because the
#: failure it would otherwise cause cannot be withdrawn.
CONTRIBUTOR_REPOS = frozenset({
    "command-center", "github-runners", "workbench",
    "Fantasy-GM", "The-League", "AFL",
})

#: Per-million-token rates by model id: input, cached input, output. The
#: two cards are not a flat multiple — contributor discounts a cache read
#: to 2% of a fresh token where standard discounts to 12% — so a scaling
#: factor cannot stand in for two cards on this cache-heavy workload.
#:
#: **The two cards do not have the same standing.** Standard was checked
#: against the account on 2026-09-20 and its anchored total matched the
#: panel to 0.22 of a point. Contributor has never been checked against a
#: bill or a panel: it is the card LEARNINGS.md recorded on 2026-09-10
#: and then retracted on 2026-09-20, kept here because it is the only
#: published figure, not because it was confirmed. Anything gating spend
#: on the contributor card is trusting an unverified number. #1304 was to
#: read the panel once the lanes moved; #1315 withdrew the routing the same
#: day, so no lane sends it contributor traffic to read.
RATE_CARDS: Dict[str, Dict[str, float]] = {
    CONTRIBUTOR_MODEL: {"input": 0.10, "cached_input": 0.002, "output": 0.20},
    STANDARD_MODEL: {"input": 1.25, "cached_input": 0.15, "output": 4.25},
}


#: The owners whose repositories this funnel works, mirroring the logins
#: in ``funnel.OWNERS``. An `owner/name` from any other owner is not one
#: of Nate's repositories, whatever it is called: `someone-else/The-League`
#: is a fork or a collaborator's copy. Not imported from ``funnel`` because
#: this runs as a per-invocation CLI inside a lane; a test asserts the two
#: stay equal instead.
KNOWN_OWNERS = frozenset({"nateprich-projects", "nateprich"})

#: The owner a cleared repository must live under.
#:
#: `nateprich` is a known owner because member repos do appear under the
#: user account, but none of the cleared ones do. Without this,
#: `nateprich/The-League` — a scratch fork, a rename in progress, anything
#: that happens to share the name — would route to the training tier. That
#: is the same class of hole as accepting a filesystem path, found by the
#: same review one round later.
CONTRIBUTOR_OWNER = "nateprich-projects"

#: Characters stripped as transport. Deliberately not ``str.strip()``:
#: that also eats NBSP, vertical tab and U+0085, and "membership is exact"
#: should mean exact rather than exact-modulo-whatever-Unicode-calls-space.
TRANSPORT_WHITESPACE = " \t\n\r"


def repo_name(repo: Optional[str]) -> str:
    """The bare repository name from either `owner/name` or `name`.

    The runners get `owner/name` from ``funnel.py begin`` while a human
    or a test says `name`; both must resolve the same way. Anything else
    returns the empty string, so the caller lands on the standard model
    rather than raising inside a lane that is mid-ticket.

    **A filesystem path is not a repository.** The naive reading of this
    function — take the last segment after a slash — accepts
    ``/Users/nateprich/.claude/command-center``, ``~/.claude/command-center``
    and ``https://github.com/anyone/The-League`` as allowlisted. Both
    runners hold a `REPO` variable that is a *path* to a checkout beside
    the `BEGIN_REPO` that is an `owner/name`, so passing the wrong one is
    a one-character mistake that would route every repository, including
    the excluded ones, to the training tier while this module reported
    exactly what the allowlist promised. At most one slash, and a known
    owner before it.
    """
    return split_ref(repo)[1]


def split_ref(repo: Optional[str]) -> "Tuple[Optional[str], str]":
    """``(owner, name)`` for a repository reference, or ``(None, "")``.

    ``owner`` is ``None`` when the reference was bare, which is what lets
    the caller treat "no owner given" differently from "an owner I was
    not expecting".
    """
    if not isinstance(repo, str):
        return None, ""
    candidate = repo.strip(TRANSPORT_WHITESPACE)
    if "/" not in candidate:
        return None, candidate
    owner, _, name = candidate.partition("/")
    owner = owner.strip(TRANSPORT_WHITESPACE)
    if "/" in name or owner not in KNOWN_OWNERS:
        return None, ""
    return owner, name.strip(TRANSPORT_WHITESPACE)


def model_for(repo: Optional[str]) -> str:
    """The model id that may carry work from ``repo``.

    Fails safe in the only direction that matters: anything not named
    exactly on the allowlist — an unknown repository, a near-miss
    spelling, an empty string, ``None``, or a cleared name under an
    owner it does not live under — gets the private model. A
    repository nobody has cleared is therefore safe by default, and
    adding one is a deliberate edit here rather than an accident
    somewhere else.
    """
    owner, name = split_ref(repo)
    if name not in CONTRIBUTOR_REPOS:
        return STANDARD_MODEL
    if owner is not None and owner != CONTRIBUTOR_OWNER:
        return STANDARD_MODEL
    return CONTRIBUTOR_MODEL


def rate_card(model: Optional[str]) -> Dict[str, float]:
    """A copy of the rate card for ``model``, defaulting to the standard.

    An unrecognised or missing model id prices at the dearer card, which
    is the recoverable direction: over-reading stops the lanes early and
    visibly, while under-reading walks them into the provider's refusal.

    The copy is not ceremony. The only consumer is a spending gate, and
    handing it the live module dict means one careless mutation silently
    re-prices every later reading in the process.
    """
    if isinstance(model, str) and model in RATE_CARDS:
        return dict(RATE_CARDS[model])
    return dict(RATE_CARDS[STANDARD_MODEL])


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print one model id, for the bash runners to put in their argv."""
    parser = argparse.ArgumentParser(
        prog="muse_model.py",
        description="which Muse model carries which repository")
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("model", help="print the model id for a repo")
    model.add_argument("--repo", required=True,
                       help="owner/name, or the bare repository name")
    # The bash runners validate the resolver's answer against this rather
    # than against a list of their own. A second copy of the model ids in
    # each runner would reject a provider version bump the day this module
    # accepted it, and would do it silently.
    sub.add_parser("models", help="print every model id with a rate card")
    args = parser.parse_args(argv)
    if args.command == "model":
        sys.stdout.write(model_for(args.repo) + "\n")
        return 0
    if args.command == "models":
        sys.stdout.write("".join(
            model + "\n" for model in sorted(RATE_CARDS)))
        return 0
    return 2  # pragma: no cover - argparse rejects an unknown subcommand


if __name__ == "__main__":
    raise SystemExit(main())
