"""The merge-safety read: every merge prerequisite, fresh, fail-closed.

``funnel merge`` enforces these through the gate in ``funnel.py``; this module
is the narrow read the review runner uses instead of the 35-section brief. It
computes exactly the merge prerequisites and nothing else: no reporting
section, no brief cache, no repository-wide PR scan.

Fail-closed is inherited from the gate: an unreadable PR, unknown
mergeability, absent CI, a missing verdict, and a tripped stop counter all
refuse. This module adds no check of its own; it reports what the gate says
in one structured read.

Direction: this package imports from ``funnel.py``, never the reverse.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Sequence

import funnel


#: The merge prerequisites this read establishes, in gate order. The
#: review runner treats this list as the contract: anything not named here
#: is not established by this read. The PR-open precondition is part of
#: reading the PR itself; these six are the merge conditions.
PREREQUISITES = (
    "counter",  # rejected-merge counter below the stop threshold
    "open",  # PR still open
    "conflict",  # branch mergeable, not conflicting with its base
    "binding",  # ticket/<n> branch bound to a ticket whose project is Building
    "ci",  # CI green
    "verdict",  # an approval verdict covers the current head
)


def read(repo: str, pr: int, items: Sequence[funnel.Item],
         now: datetime) -> Dict[str, object]:
    """Every merge prerequisite for one PR, fresh. Nothing else.

    ``items`` is the already-loaded Project board — the same read the gate's
    binding and counter checks use — so this performs one bounded GraphQL batch
    carrying the PR row and its comment tail, and loads no reporting section.
    """
    blockers: List[str] = funnel.merge_blockers(
        repo, pr, list(items), now)
    counter = funnel.rejected_merges(items, now)
    return {
        "safe": not blockers,
        "blockers": blockers,
        "checked": list(PREREQUISITES),
        "stop_auto_merging": bool(counter["stop_auto_merging"]),
        "rejected_merge_count": counter["count"],
        "rejected_merge_refs": list(counter["refs"]),
    }
