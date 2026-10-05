"""Dormant native-Mac capability assessment for #2297.

No live funnel caller imports this module. A receipt is an input for a future
reviewed Project routing change, not authority to change Needs or claim work.
The current ``claude-code-environment`` route therefore remains Claude-only.
"""

from dataclasses import dataclass
from typing import Mapping, Optional

import codex_run


@dataclass(frozen=True)
class MacCapabilityReceipt:
    """One reviewed ticket/action proof, recorded on GitHub after a real run."""

    ticket_ref: str
    capability: str
    evidence_url: str


def _mac_runtime_check() -> Mapping[str, object]:
    """Read this process's actual, exact native Mac Codex rollout."""
    return codex_run.check()


def assess_native_mac_step(
    *,
    ticket_ref: str,
    needs: str,
    agent: str,
    capability: str,
    receipt: Optional[MacCapabilityReceipt],
) -> str:
    """Return the first reason this step cannot use a native Mac Codex run.

    ``eligible-for-review`` means only that a future reviewer may consider a
    specific, externally verified receipt. It is deliberately not a startable
    decision: Project Needs, begin's usage and API reserve, WIP, claim, and
    independent review gates still decide whether work can start and land.
    The parameterless check is the existing exact Mac rollout check; a cloud
    profile or a caller-supplied profile description cannot replace it.
    """
    if needs == "human":
        return "human"
    if needs != "claude-code-environment" or agent != "codex":
        return "wrong-route"
    if (
        receipt is None
        or receipt.ticket_ref != ticket_ref
        or receipt.capability != capability
        or not receipt.capability
        or not receipt.evidence_url.startswith(
            "https://github.com/nateprich-projects/"
        )
        or "#issuecomment-" not in receipt.evidence_url
    ):
        return "unproved-capability"
    settings = _mac_runtime_check()
    if settings.get("ok") is not True:
        return "unverified-mac-runtime"
    return "eligible-for-review"
