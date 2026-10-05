"""Versioned cutover switch for isolated Codex implementation calls.

The existing Nate-owned automation keeps its direct path before the app UI
switch. Only the dedicated macOS Codex account uses the two-verb broker. A
rollback changes this flag back to False and returns automations to Nate;
there is no local state file or credential in the model shell.
"""

from __future__ import annotations

import os
import sys
from typing import Optional


ISOLATED_CODEX_UID = 506
BROKER_ROUTE_ENABLED = True


def use_broker(agent: str, *, uid: Optional[int] = None,
               system: Optional[str] = None) -> bool:
    """Route only the real isolated Mac account, never a cloud UID collision."""
    return (agent == "codex" and BROKER_ROUTE_ENABLED
            and (sys.platform if system is None else system) == "darwin"
            and (os.getuid() if uid is None else uid) == ISOLATED_CODEX_UID)
